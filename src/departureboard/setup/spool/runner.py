"""The page's side: a setup.api.Runner that asks the setup service, and the dashboard's Poster."""

from __future__ import annotations

import copy
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from ...core.errors import safe_message
from .. import discover, keytest
from .. import lookups as setup
from ..lookups import Place, SetupError, Station, Stop
from .checks import (
    _FINISHED,
    VERSION,
    Refused,
    _key_result,
    _list,
    _place,
    _stop,
    _walked,
    check_reply,
    check_status,
    parse,
)
from .folder import MAX_REPLY_BYTES, MAX_REQUEST_BYTES, WAITS, Spool

log = logging.getLogger(__name__)

START_WAIT_S = 90.0  # for the search's first status
SILENT_S = 60.0  # a search unheard from for this long has stopped
ACTIVE_EVERY_S = 60.0  # the page says a browser is using the dashboard at most this often

NO_ANSWER = ("The setup service didn't answer in time. Try again; if it keeps happening, "
             "check departureboard-fetch.path is enabled on the Pi.")
STOPPED = "The setup service stopped before the search finished. Try again."
UNFIT = "The setup service sent an answer that doesn't fit. Try again."
GONE = "The search's answer was left unread too long and has gone. Search again."


def new_id() -> str:
    return secrets.token_hex(16)


def _place_args(place) -> dict:
    return {"lat": place.lat, "lon": place.lon}


class SpoolRunner:
    """Asks the setup service, through the spool, to do setup's internet
    work, and waits for its answers. For api.SetupApi(runner=...)."""

    def __init__(self, folder: Path | str, *, waits: Mapping[str, float] = WAITS,
                 start_wait: float = START_WAIT_S, silent: float = SILENT_S, poll: float = 0.1,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.spool = Spool(folder)
        self.spool.make()
        self._waits = dict(waits)
        self._start_wait = start_wait
        self._silent = silent
        self._poll = poll
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep

    def post(self, request_id: str, kind: str, args: dict) -> None:
        self.spool.sweep(self.clock())
        self.spool.write(self.spool.requests, request_id,
                         {"v": VERSION, "id": request_id, "made": self.clock(), "kind": kind, "args": args},
                         MAX_REQUEST_BYTES)

    def withdraw(self, request_id: str) -> None:
        """Delete a request the service hasn't taken, so nothing in it waits there."""
        (self.spool.requests / f"{request_id}.json").unlink(missing_ok=True)

    def reply(self, request_id: str, delete: bool, where: Path | None = None) -> dict | None:
        """The reply to `request_id` in replies/ (or `where`), checked, or None
        if there is none yet. SetupError if it doesn't fit."""
        try:
            body = Spool.read((where or self.spool.replies) / f"{request_id}.json", MAX_REPLY_BYTES, delete)
            return None if body is None else check_reply(parse(body), request_id)
        except Refused as why:
            log.warning("Refused the setup service's reply: %s", why)
            raise SetupError(UNFIT) from None

    def _ask(self, kind: str, args: dict):
        request_id = new_id()
        self.post(request_id, kind, args)
        deadline = self.monotonic() + self._waits[kind]
        try:
            while (reply := self.reply(request_id, delete=True)) is None:
                if self.monotonic() >= deadline:
                    log.warning("The setup service didn't answer a %s request in time", kind)
                    raise SetupError(NO_ANSWER)
                self.sleep(self._poll)
        finally:
            self.withdraw(request_id)
        if not reply["ok"]:
            if reply["why"] == "setup":
                raise SetupError(safe_message(reply["error"]))
            raise RuntimeError("The setup service failed")  # api.SetupApi answers a plain 500
        return reply["value"]

    def _check(self, value, check):
        try:
            return check(value)
        except Refused as why:
            log.warning("Refused the setup service's reply: %s", why)
            raise SetupError(UNFIT) from None

    # api.Runner.

    def lookup_postcode(self, postcode: str) -> Place:
        return self._check(self._ask("postcode", {"postcode": postcode}), _place)

    def search_address(self, query: str) -> list[Place]:
        value = self._ask("address", {"q": query})
        return self._check(value, lambda v: [_place(p) for p in _list(v, "places", setup.MAX_ADDRESS_RESULTS)])

    def nearby_stops(self, lat: float, lon: float, *, within_m: float, limit: int) -> list[Stop]:
        value = self._ask("stops", {"home": {"lat": lat, "lon": lon}, "within_m": within_m, "limit": limit})
        return self._check(value, lambda v: [_stop(s, within_m) for s in _list(v, "stops", limit)])

    def walk_minutes(self, home: Place, places: Mapping[str, Station | Stop]) -> dict[str, tuple[int, str]]:
        if not places:
            return {}
        value = self._ask("walk", {"home": _place_args(home), "places": {k: _place_args(p) for k, p in places.items()}})
        return self._check(value, lambda v: _walked(v, set(places)))

    def test_key(self, name: str, value: str | None, app_id: str | None) -> keytest.KeyResult:
        reply = self._ask("testkey", {"name": name, "value": value or "", "app_id": app_id or ""})
        return self._check(reply, _key_result)

    def make_job(self, stops: Mapping[str, str], pasted_key: str, *, routes: bool,
                 walk_minutes: Mapping[str, int]) -> SpoolJob:
        return SpoolJob(self, {"stops": dict(stops), "routes": routes, "walk": dict(walk_minutes),
                               "key": pasted_key}, self._start_wait, self._silent)


class SpoolJob:
    """The search, run by the setup service: a discover.SetupJob's start,
    status and cancel, read from the status the service keeps writing."""

    def __init__(self, runner: SpoolRunner, args: dict, start_wait: float, silent: float) -> None:
        self.id = new_id()
        self._runner = runner
        self._args = args
        self._start_wait = start_wait
        self._silent = silent
        self._lock = threading.Lock()
        self._status = {"state": "queued", "step": "", "done": 0, "total": 0, "message": "", "result": None}
        self._finished = False
        self._started: float | None = None
        self._beat: tuple[int, float] | None = None  # the last heartbeat, and when it changed

    def start(self) -> None:
        with self._lock:
            if self._started is not None:
                raise RuntimeError("This setup job has already been started")
            self._runner.post(self.id, "search", self._args)
            self._args = {}  # the pasted key, if any, is in the spool for the service alone now
            self._started = self._runner.monotonic()

    def cancel(self) -> None:
        with self._lock:
            if self._started is None or self._finished:
                return
        self._runner.post(new_id(), "cancel", {"job": self.id})

    def status(self) -> dict:
        with self._lock:
            if self._started is not None and not self._finished:
                self._update(self._started)
            return copy.deepcopy(self._status)

    def _end(self, message: str) -> None:
        self._status.update(state="failed", message=message, result=None)
        self._finished = True
        self._runner.withdraw(self.id)
        (self._runner.spool.status / f"{self.id}.json").unlink(missing_ok=True)

    def _update(self, started: float) -> None:
        now = self._runner.monotonic()
        try:
            reply = self._runner.reply(self.id, delete=False, where=self._runner.spool.status)
        except SetupError as error:
            self._end(str(error))
            return
        if reply is None:
            if self._beat is not None:  # swept away after going unread for long
                self._end(GONE)
            elif now - started > self._start_wait:
                log.warning("The setup service didn't start the search in time")
                self._end(NO_ANSWER)
            return
        if not reply["ok"]:
            self._end(safe_message(reply["error"]) if reply["why"] == "setup" else "Something went wrong; try again")
            return
        try:
            status = check_status(reply["value"])
        except Refused as why:
            log.warning("Refused the setup service's search status: %s", why)
            self._end(UNFIT)
            return
        beat = reply.get("beat", 0)
        if self._beat is None or self._beat[0] != beat:
            self._beat = (beat, now)
        self._status = status
        if status["state"] in _FINISHED and not discover.roads_running(status):
            self._finished = True  # kept here from now on; the file has done its job
            (self._runner.spool.status / f"{self.id}.json").unlink(missing_ok=True)
        elif now - self._beat[1] > self._silent:
            log.warning("The setup service stopped mid-search")
            self._end(STOPPED)


class Poster:
    """Tells the service, through the spool, that a browser is using the
    dashboard: an `active` request, which gets no reply and starts the
    service if it has stopped. At most one every `every` seconds, however
    often it is asked. For the dashboard's mirror (handoff.SnapshotMirror)."""

    def __init__(self, folder: Path | str, *, every: float = ACTIVE_EVERY_S, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.spool = Spool(folder)
        self.spool.make()
        self._every = every
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._posted: float | None = None

    def active(self) -> bool:
        """Post an `active` request, unless one went in the last `every`
        seconds. Whether one went now. Never raises: a browser's request
        mustn't fail for it."""
        with self._lock:
            now = self._monotonic()
            if self._posted is not None and now - self._posted < self._every:
                return False
            self._posted = now
        request_id = new_id()
        try:
            self.spool.sweep(self._clock())
            self.spool.write(self.spool.requests, request_id,
                             {"v": VERSION, "id": request_id, "made": self._clock(), "kind": "active", "args": {}},
                             MAX_REQUEST_BYTES)
        except (OSError, ValueError) as error:
            log.warning("Can't tell the fetch service a browser is using the dashboard: %s", type(error).__name__)
            return False
        return True

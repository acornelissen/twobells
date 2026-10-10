"""The service's side: the Worker that takes the page's requests from the spool and answers them."""

from __future__ import annotations

import fcntl
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from ...core import files
from ...core.errors import safe_message
from .. import discover, keytest
from .. import lookups as setup
from ..api import KEY_TEST_EVERY, MIN_SCRUBBED, Job, MakeJob, RateLimit, _scrub
from ..lookups import Place, SetupError, Stop
from .checks import _FINISHED, MAX_NOTES, VERSION, Refused, check_request, parse
from .folder import MAX_REPLY_BYTES, MAX_REQUEST_BYTES, Spool, _remove

log = logging.getLogger(__name__)

HEARTBEAT_S = 5.0
IDLE_S = 60.0  # the service stops after this long with nothing to do
POLL_S = 0.2
WORKER_LOCK = "worker.lock"
SEARCH_NICE = 10  # the search's thread gives way to the wizard's lookups (and the board)

NO_SETTINGS = ("The board's settings can't be read, so setup can't look anything up. "
               "On the Pi, journalctl -u departureboard-fetch says why.")

_FILE = re.compile(r"^([0-9a-f]{32})\.json$")


def _place_out(place: Place) -> dict:
    return {"lat": place.lat, "lon": place.lon, "label": place.label, "country": place.country,
            "district": place.admin_district}


def _stop_out(stop: Stop) -> dict:
    return {"atco": stop.atco, "name": stop.name, "indicator": stop.indicator, "bearing": stop.bearing,
            "lat": stop.lat, "lon": stop.lon, "distance_m": stop.distance_m}


def _status_out(status: dict) -> dict:
    out = {key: status.get(key) for key in ("state", "step", "done", "total", "message", "result")}
    out["message"] = safe_message(out["message"]) if out["message"] else ""
    result = out["result"]
    if out["state"] == "done" and isinstance(result, dict):
        notes = [safe_message(n) for n in result.get("notes", [])]
        if len(notes) > MAX_NOTES:
            notes = [*notes[:MAX_NOTES - 1], f"And {len(notes) - MAX_NOTES + 1} more notes"]
        out["result"] = {**result, "notes": notes}
        if isinstance(status.get("roads"), dict):
            out["roads"] = {key: status["roads"].get(key) for key in ("state", "done", "total")}
    else:
        out["result"] = None
    return out


def _failed(message: str) -> dict:
    return {"state": "failed", "step": "", "done": 0, "total": 0, "message": message, "result": None}


class Worker:
    """The fetch service: takes requests from the spool, does them, writes
    the replies, and runs at most one search at a time (discover.SetupJob's
    lock in the board's state folder also keeps it to one with the command
    line's). run() keeps going until it has had nothing to do for `idle`
    seconds and no search is running, nor finding its roads once done (a
    new search stops those). With `unavailable`, the reason it
    can't do any (its config can't be read), it answers each request with
    that instead, so the page says so rather than waiting."""

    def __init__(
        self,
        folder: Path | str,
        *,
        job_folder: Path | str | None,
        load_secrets: Callable[[], Mapping[str, str]],
        settings: Callable[[], Mapping] = dict,
        lookup_postcode: Callable[[str], Place] = setup.lookup_postcode,
        search_address: Callable[[str], list[Place]] = setup.search_address,
        nearby_stops: Callable[..., list[Stop]] = setup.nearby_stops,
        walk_minutes: Callable[..., dict[str, tuple[int, str]]] = setup.walk_minutes,
        make_job: MakeJob = discover.SetupJob,
        test_key: Callable[..., keytest.KeyResult] = keytest.test_key,
        # Behind the page's own limit, in case something else writes requests; looser so the two never disagree.
        key_tests: RateLimit | None = None,
        idle: float = IDLE_S,
        poll: float = POLL_S,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        stop: threading.Event | None = None,
        unavailable: str = "",
        on_active: Callable[[], None] | None = None,
    ) -> None:
        """`folder` is the spool; `job_folder` the board's state folder, where
        the search builds the timetable. `load_secrets` gives the saved keys,
        as the other units' environment does, and `settings` the board's
        settings, for what a key test asks about. Setting `stop` ends run(),
        and a search running is cancelled and said to be. `on_active` is
        called for each `active` request (a browser is using the dashboard),
        which gets no reply."""
        self.spool = Spool(folder)
        self._job_folder = None if job_folder is None else Path(job_folder)
        self._unavailable = unavailable
        self._on_active = on_active
        self._load_secrets = load_secrets
        self._settings = settings
        self._lookup_postcode = lookup_postcode
        self._search_address = search_address
        self._nearby_stops = nearby_stops
        self._walk_minutes = walk_minutes
        self._make_job = make_job
        self._test_key = test_key
        self._key_tests = key_tests or RateLimit(KEY_TEST_EVERY / 2, clock=monotonic)
        self._idle = idle
        self._poll = poll
        self._clock = clock
        self._monotonic = monotonic
        self._stop = stop or threading.Event()
        # The search's status is written from two threads (see _keep_reporting); this keeps them apart.
        self._lock = threading.RLock()
        self._job: Job | None = None
        self._job_id = ""
        self._job_secrets: list[str] = []
        self._shown: dict | None = None
        self._beat = 0
        self._written = 0.0
        self._cancelled: list[str] = []  # cancels that came before their search

    def run(self, tend: Callable[[], bool] | None = None) -> int:
        """Answer requests until stopped, or idle for `idle` seconds with no
        search running and no request waiting. `tend`, if given, is called
        each time round once the lock is held; while it says True the
        service stays up, however idle."""
        self.spool.make()
        fd = files.open_lock(self.spool.folder / WORKER_LOCK)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:  # it answers what's waiting; this stops cleanly, so systemd doesn't count a failure
                log.info("Another fetch service is already answering")
                return 0
            self.spool.sweep(self._clock())
            finished = threading.Event()
            heartbeat = threading.Thread(target=self._keep_reporting, args=(finished,), name="setup-heartbeat",
                                         daemon=True)
            heartbeat.start()
            try:
                last = self._monotonic()
                while not self._stop.is_set():
                    busy = tend() if tend is not None else False
                    if self.step() or self._job is not None or busy:
                        last = self._monotonic()
                    elif self._monotonic() - last >= self._idle and not self._waiting():
                        break  # never with a request waiting: one may have come since the step
                    self._stop.wait(self._poll)
            finally:
                finished.set()
                heartbeat.join()
            self._stop_search()
            return 0
        finally:
            os.close(fd)

    def step(self) -> bool:
        """Answer every waiting request and report the search. Whether any request came."""
        self.spool.sweep(self._clock())  # an answer the page gave up on goes soon after
        handled = False
        for path in self._waiting():
            handled = True
            self._take(path)
        self._report()
        return handled

    def _keep_reporting(self, finished: threading.Event) -> None:
        """The search's heartbeat, in a thread of its own until `finished`. A
        lookup holds the main thread for up to its wait (NaPTAN's, longer than
        the page's SILENT_S), and the page must not take that for a stop."""
        while not finished.wait(self._poll):
            if self._stop.is_set():  # at once, though a lookup may still hold the main thread
                self._stop_search()
            else:
                self._report()

    def _stop_search(self) -> None:
        """Stopped mid-search: cancel it and say so, rather than go quiet.
        A search already done keeps its result: only its roads stop, and
        the next build of the timetable finds the rest."""
        with self._lock:
            if self._job is not None:
                self._job.cancel()
                status = self._job.status()
                if status.get("state") == "done":
                    shown = _status_out(status)
                    if discover.roads_running(shown):
                        shown["roads"] = {**shown["roads"], "state": "cancelled"}
                else:
                    shown = _failed("Cancelled")
                self._write_status(shown, force=True)
                self._job, self._job_secrets = None, []

    def _waiting(self) -> list[Path]:
        try:
            entries = [self.spool.requests / name for name in os.listdir(self.spool.requests)]
        except OSError:
            return []
        dated = []
        for path in entries:
            try:
                dated.append((os.lstat(path).st_mtime_ns, path.name, path))
            except OSError:
                continue
        return [path for _, _, path in sorted(dated)]

    def _take(self, path: Path) -> None:
        """Read one request, deleting it, then do it. Anything that isn't a
        fresh, well-formed request is deleted with no reply."""
        match = _FILE.match(path.name)
        if match is None:
            _remove(path)
            log.warning("Deleted a file in the setup spool that isn't a request")
            return
        request_id = match.group(1)
        try:
            body = Spool.read(path, MAX_REQUEST_BYTES, delete=True)
            if body is None:
                return
            kind, args = check_request(parse(body), request_id, self._clock())
        except Refused as why:
            log.warning("Refused a setup request: %s", why)
            return
        if kind == "active":  # each minute a browser is open: not worth a line in the log each time
            if self._on_active is not None:
                self._on_active()
            return
        log.info("Setup request: %s", kind)
        if self._unavailable:
            if kind != "cancel":
                self._reply(request_id, {"ok": False, "why": "setup", "error": self._unavailable},
                            self.spool.status if kind == "search" else None)
            return
        pasted = [args.get(name, "") for name in ("value", "app_id", "key")]
        if kind == "cancel":
            self._cancel(args["job"])
        elif kind == "search":
            self._search(request_id, args)
        else:
            self._answer(request_id, kind, args, pasted)

    def _secrets(self, pasted) -> list[str]:
        try:
            env = self._load_secrets()
            values = [str(env.get(name, "")).strip() for name in keytest.KEY_NAMES]
        except Exception:  # noqa: BLE001 - the answer is scrubbed of the pasted ones still
            values = []
        return sorted({v for v in [*values, *pasted] if len(v) >= MIN_SCRUBBED}, key=len, reverse=True)

    def _answer(self, request_id: str, kind: str, args: dict, pasted) -> None:
        try:
            reply = {"ok": True, "value": self._do(kind, args)}
        except SetupError as error:
            reply = {"ok": False, "why": "setup", "error": safe_message(_scrub(str(error), self._secrets(pasted)))}
        except Exception as error:  # noqa: BLE001 - said plainly; never its text, which may hold a value
            log.warning("Setup %s failed: %s", kind, type(error).__name__)
            reply = {"ok": False, "why": "fault", "error": "Something went wrong"}
        self._reply(request_id, _scrub(reply, self._secrets(pasted)))

    def _reply(self, request_id: str, reply: dict, where: Path | None = None) -> None:
        """Write a reply in replies/, or `where`."""
        try:
            self.spool.write(where or self.spool.replies, request_id, {"v": VERSION, "id": request_id, **reply},
                             MAX_REPLY_BYTES)
        except (OSError, ValueError) as error:
            log.warning("Can't write a setup reply: %s", type(error).__name__)

    def _do(self, kind: str, args: dict):
        if kind == "postcode":
            return _place_out(self._lookup_postcode(args["postcode"]))
        if kind == "address":
            return [_place_out(p) for p in self._search_address(args["q"])]
        if kind == "stops":
            lat, lon = args["home"]
            return [_stop_out(s) for s in self._nearby_stops(lat, lon, within_m=args["within_m"],
                                                            limit=args["limit"])]
        if kind == "walk":
            lat, lon = args["home"]
            home = Place(lat, lon, "")
            places = {key: Place(lat, lon, "") for key, (lat, lon) in args["places"].items()}
            return {key: [minutes, source] for key, (minutes, source) in self._walk_minutes(home, places).items()}
        if kind == "testkey":
            return self._key_test(args)
        raise ValueError(f"No such setup request: {kind}")

    def _key_test(self, args: dict) -> dict:
        name = args["name"]
        wait = self._key_tests.take("TAPI" if name in keytest.COSTS_A_LOOKUP else name)
        if wait:
            raise SetupError(f"Wait {math.ceil(wait)} s before testing this key again")
        env = dict(self._load_secrets())
        if args["app_id"]:
            env["TAPI_APP_ID"] = args["app_id"]
        try:
            settings = self._settings()
        except Exception:  # noqa: BLE001 - the test asks about Glossop instead
            settings = {}
        result = self._test_key(name, args["value"] or None, settings, env)
        return {"state": result.state, "message": result.message}

    # The search.

    def _cancel(self, job_id: str) -> None:
        with self._lock:
            if self._job is not None and self._job_id == job_id:
                self._job.cancel()
            else:
                self._cancelled = [*self._cancelled[-31:], job_id]

    def _not_started(self, request_id: str, message: str) -> None:
        """The status of a search that didn't start: failed, saying why."""
        self._reply(request_id, {"ok": True, "beat": 0, "value": _failed(message)}, self.spool.status)

    def _search(self, request_id: str, args: dict) -> None:
        if request_id in self._cancelled:
            self._not_started(request_id, "Cancelled")
            return
        if self._job is not None:
            if not discover.roads_running(self._job.status()):
                self._not_started(request_id, "A search is already running; wait for it or cancel it")
                return
            # Done but for its roads: they stop, as this search's timetable replaces its.
            self._job.cancel()
        saved = ""
        try:
            saved = str(self._load_secrets().get("BODS_API_KEY", "")).strip()
        except Exception as error:  # noqa: BLE001 - said below as no key
            log.warning("Can't read the keys: %s", type(error).__name__)
        key = args["key"] or saved
        if not key:
            self._not_started(request_id, "Paste your Bus Open Data Service key first; finding bus companies needs it")
            return
        try:
            job = self._make_job(self._job_folder, args["stops"], key, routes=args["routes"],
                                 walk_minutes=args["walk"], nice=SEARCH_NICE)
            job.start()
        except discover.SetupBusy:
            self._not_started(request_id, "A setup search is already running for this board")
            return
        except Exception as error:  # noqa: BLE001 - said plainly; never its text
            log.warning("Setup search failed to start: %s", type(error).__name__)
            self._not_started(request_id, "Something went wrong; try again")
            return
        job_secrets = self._secrets([args["key"]])
        with self._lock:
            self._job, self._job_id, self._shown, self._job_secrets = job, request_id, None, job_secrets
        self._report()

    def _report(self) -> None:
        """Write the search's status, if one is running: when it changes, and
        every HEARTBEAT_S besides."""
        with self._lock:
            if self._job is None:
                return
            status = self._job.status()
            self._write_status(_status_out(status))
            if status.get("state") in _FINISHED and not discover.roads_running(status):
                self._job, self._job_secrets = None, []  # kept, and the service up, while its roads are found

    def _write_status(self, shown: dict, force: bool = False) -> None:
        now = self._monotonic()
        if not force and shown == self._shown and now - self._written < HEARTBEAT_S:
            return
        self._beat += 1
        self._shown, self._written = shown, now
        self._reply(self._job_id, {"ok": True, "beat": self._beat, "value": _scrub(shown, self._job_secrets)},
                    self.spool.status)

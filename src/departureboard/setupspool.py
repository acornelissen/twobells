"""The setup wizard's internet work, done by a separate setup service on the Pi.

Why. The settings page's service (departureboard-web.service) reaches only
the home network: its kernel IP filter denies everything else, so a flaw in
the page can't be used to talk to the internet. But the wizard's lookups
(postcodes.io, Nominatim, NaPTAN, the OpenStreetMap foot router), its key
tests and its search for bus companies need the internet. On the Pi they
run in departureboard-setup.service instead: outbound only, started when
setup is used, and gone a minute after it was last asked anything. The
dashboard (Docker, a laptop) has no such filter and does them in-process.

How the two talk. Through files in a spool folder only the service user
can open (0700):

  tmp/       files being written, by either side
  requests/  the page's requests, one file each: <id>.json
  replies/   the service's replies: <id>.json

Where. In memory, never on the SD card: a request can hold home's exact
position or a pasted key, and flash can keep what was deleted. On the Pi
both units name the same systemd RuntimeDirectory, /run/departureboard-setup
(a tmpfs, kept while either runs and until the Pi restarts), and find it in
$RUNTIME_DIRECTORY. Without that, run by hand, they use that folder if it
is there, and otherwise setup-spool/ in the board's state folder: on disk,
for the tests and a Mac (see spool_folder).

Every file is written new in tmp/ (0600, never through a link) and renamed
into place whole, so neither side ever reads half of one. A systemd path
unit, departureboard-setup.path, starts the service whenever requests/ isn't
empty: the settings page needs no sudo, polkit rule or access to systemd,
only a file in a folder it already owns, and it can't choose what the
service runs or with what settings. The service takes each request,
deleting it as it reads it (so a pasted key is kept as briefly as it
can), checks it, does it, and writes the reply. The page waits for the
reply, up to a limit per kind, deletes it once read, and deletes its own
request if it is still there when it gives up. The search's reply is its
status, rewritten as it goes and every few seconds besides, so the page can
tell a stopped service from a slow search.

Trust. Only the service user can write in the spool, but each side still
treats what the other wrote as untrusted: a file must be a plain file the
service user owns, with one link, under its size cap, named by a request id,
fresh, and strict JSON in exactly the shape expected for its kind. Anything
else is deleted (or, for an awaited reply, refused). Requests carry only
what was typed or pasted: never a saved key (the service reads those as the
other units do) and never settings. Logs name a request's kind, never its
contents: no position, postcode, address or key.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

from . import discover, keytest, routesetup, setup
from .settingsform import _KEY_VALUE
from .setup import Place, SetupError, Station, Stop
from .setupapi import KEY_TEST_EVERY, MAX_WALK_MINUTES, MIN_SCRUBBED, Invalid, RateLimit, _scrub, _stops
from .status import safe_message

log = logging.getLogger(__name__)

SPOOL = "setup-spool"  # in the board's state folder, when there's no folder in memory
RUNTIME = Path("/run/departureboard-setup")  # the units' RuntimeDirectory
VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024  # the biggest, a walk to 25 places, is about 2 kB
MAX_REPLY_BYTES = 512 * 1024  # the biggest, a search with route strips, is tens of kB
MAX_DEPTH = 12
REQUEST_MAX_AGE_S = 150.0  # longer than any wait: past this, whoever asked has given up
REPLY_MAX_AGE_S = 600.0  # a reply nobody collected
TMP_MAX_AGE_S = 60.0
CLOCK_SKEW_S = 5.0  # a request from the future, by more than this, is refused
# How long the page waits for each answer: the service may first need to
# start (several seconds on a Pi Zero), and NaPTAN's area files are big.
WAITS = {"postcode": 30.0, "address": 30.0, "stops": 120.0, "walk": 30.0, "testkey": 45.0}
START_WAIT_S = 90.0  # for the search's first status
SILENT_S = 60.0  # a search unheard from for this long has stopped
HEARTBEAT_S = 5.0
IDLE_S = 60.0  # the service stops after this long with nothing to do
POLL_S = 0.2
MAX_NOTES = 30
WORKER_LOCK = "worker.lock"

NO_ANSWER = ("The setup service didn't answer in time. Try again; if it keeps happening, "
             "check departureboard-setup.path is enabled on the Pi.")
STOPPED = "The setup service stopped before the search finished. Try again."
UNFIT = "The setup service sent an answer that doesn't fit. Try again."
GONE = "The search's answer was left unread too long and has gone. Search again."

_ID = re.compile(r"^[0-9a-f]{32}$")
_FILE = re.compile(r"^([0-9a-f]{32})\.json$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CRS = re.compile(r"^[A-Z]{3}$")
_ATCO = re.compile(r"^[0-9]{3}[0-9A-Za-z]{1,9}$")
_LABEL = re.compile(r"^[A-Za-z0-9]{1,3}$")
_NOC = re.compile(r"^[A-Za-z0-9]{2,8}$")
_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./+-]{0,15}$")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_STATES = ("queued", "running", "done", "failed")
_FINISHED = ("done", "failed")


class Refused(Exception):
    """A spool file that isn't what it should be. The text names no value."""


class SpoolError(Exception):
    """The spool folder can't be used safely."""


def new_id() -> str:
    return secrets.token_hex(16)


# Checking what the other side wrote. Each says what was wrong by field, never by value.


def _obj(value, required, optional=()) -> dict:
    if not isinstance(value, dict):
        raise Refused("not an object")
    if set(value) - set(required) - set(optional):
        raise Refused("unexpected fields")
    if any(name not in value for name in required):
        raise Refused("missing fields")
    return value


def _str(value, what: str, high: int, low: int = 0, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high or _CONTROL.search(value):
        raise Refused(f"{what} isn't plain text of the right length")
    if pattern is not None and not pattern.match(value):
        raise Refused(f"{what} isn't in the right form")
    return value


def _int(value, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refused(f"{what} isn't a whole number in range")
    return value


def _num(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Refused(f"{what} isn't a number")
    return float(value)


def _list(value, what: str, high: int) -> list:
    if not isinstance(value, list) or len(value) > high:
        raise Refused(f"{what} isn't a short enough list")
    return value


def _dict(value, what: str, high: int) -> dict:
    if not isinstance(value, dict) or len(value) > high:
        raise Refused(f"{what} isn't a small enough object")
    return value


def _point(value, what: str) -> tuple[float, float]:
    fields = _obj(value, ("lat", "lon"))
    lat, lon = _num(fields["lat"], what), _num(fields["lon"], what)
    if not setup.in_uk(lat, lon):
        raise Refused(f"{what} isn't in the UK")
    return lat, lon


def _key(value, what: str) -> str:
    """A pasted key, or "" for none."""
    if value == "":
        return ""
    return _str(value, what, 200, 1, _KEY_VALUE)


def _no_constant(name: str):
    raise ValueError(f"{name} is not a number")


def _depth(value, level: int = 1) -> int:
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level)
    return level


def parse(body: bytes):
    """Strict JSON: UTF-8, no NaN or Infinity, not too deep."""
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise Refused("not JSON") from None
    if _depth(value) > MAX_DEPTH:
        raise Refused("nested too deeply")
    return value


# Requests, as the service checks them: (kind, its arguments checked).


def _postcode_args(args) -> dict:
    fields = _obj(args, ("postcode",))
    return {"postcode": _str(fields["postcode"], "postcode", 16, 1)}


def _address_args(args) -> dict:
    fields = _obj(args, ("q",))
    return {"q": _str(fields["q"], "q", setup.ADDRESS_LENGTH[1], setup.ADDRESS_LENGTH[0])}


def _stops_args(args) -> dict:
    fields = _obj(args, ("home", "within_m", "limit"))
    within = _num(fields["within_m"], "within_m")
    if not setup.STOP_RADIUS_M[0] <= within <= setup.STOP_RADIUS_M[1]:
        raise Refused("within_m is out of range")
    return {"home": _point(fields["home"], "home"), "within_m": within,
            "limit": _int(fields["limit"], "limit", 1, setup.MAX_STOPS)}


def _walk_args(args) -> dict:
    fields = _obj(args, ("home", "places"))
    places = {}
    for key, place in _dict(fields["places"], "places", setup.MAX_WALK_DESTINATIONS).items():
        if not (_CRS.match(key) or _ATCO.match(key)):
            raise Refused("a place isn't named by a station or stop code")
        places[key] = _point(place, "place")
    return {"home": _point(fields["home"], "home"), "places": places}


def _testkey_args(args) -> dict:
    fields = _obj(args, ("name", "value", "app_id"))
    name = fields["name"]
    if name not in keytest.KEY_NAMES:
        raise Refused("name isn't a key's")
    app_id = _key(fields["app_id"], "app_id")
    if app_id and name != "TAPI_APP_KEY":
        raise Refused("only TransportAPI's app key is tested with an app ID")
    return {"name": name, "value": _key(fields["value"], "value"), "app_id": app_id}


def _search_args(args) -> dict:
    fields = _obj(args, ("stops", "routes", "walk", "key"))
    try:
        stops = _stops(fields["stops"], least=1)
    except Invalid:
        raise Refused("stops aren't labelled stop codes") from None
    if not isinstance(fields["routes"], bool):
        raise Refused("routes isn't true or false")
    walk = _dict(fields["walk"], "walk", len(stops))
    if set(walk) - set(stops):
        raise Refused("walk isn't by stop label")
    return {"stops": stops, "routes": fields["routes"],
            "walk": {label: _int(m, "walk", 0, MAX_WALK_MINUTES) for label, m in walk.items()},
            "key": _key(fields["key"], "key")}


def _cancel_args(args) -> dict:
    fields = _obj(args, ("job",))
    return {"job": _str(fields["job"], "job", 32, 32, _ID)}


_REQUESTS: dict[str, Callable[[object], dict]] = {
    "postcode": _postcode_args,
    "address": _address_args,
    "stops": _stops_args,
    "walk": _walk_args,
    "testkey": _testkey_args,
    "search": _search_args,
    "cancel": _cancel_args,
}


def check_request(data, file_id: str, now: float) -> tuple[str, dict]:
    """(kind, arguments) of a request named `file_id`, or Refused."""
    top = _obj(data, ("v", "id", "made", "kind", "args"))
    if top["v"] != VERSION or top["id"] != file_id:
        raise Refused("wrong version or id")
    made = _num(top["made"], "made")
    if not now - REQUEST_MAX_AGE_S <= made <= now + CLOCK_SKEW_S:
        raise Refused("stale")
    kind = top["kind"]
    if kind not in _REQUESTS:
        raise Refused("unknown kind")
    return kind, _REQUESTS[kind](top["args"])


# Replies, as the page checks them.


def _place(value) -> Place:
    fields = _obj(value, ("lat", "lon", "label", "country", "district"))
    lat, lon = _point({"lat": fields["lat"], "lon": fields["lon"]}, "place")
    return Place(lat, lon, _str(fields["label"], "label", 200), _str(fields["country"], "country", 40),
                 _str(fields["district"], "district", 100))


def _stop(value, within_m: float) -> Stop:
    fields = _obj(value, ("atco", "name", "indicator", "bearing", "lat", "lon", "distance_m"))
    lat, lon = _point({"lat": fields["lat"], "lon": fields["lon"]}, "stop")
    bearing = fields["bearing"]
    if bearing not in setup.BEARINGS and bearing != "":
        raise Refused("bearing isn't a compass point")
    return Stop(_str(fields["atco"], "atco", 12, 4, _ATCO), _str(fields["name"], "name", 200, 1),
                _str(fields["indicator"], "indicator", 60), bearing, lat, lon,
                _int(fields["distance_m"], "distance_m", 0, math.ceil(within_m)))


def _walked(value, asked) -> dict[str, tuple[int, str]]:
    found = {}
    for key, pair in _dict(value, "walk", len(asked)).items():
        if key not in asked:
            raise Refused("a walk to somewhere not asked about")
        if not isinstance(pair, list) or len(pair) != 2:
            raise Refused("a walk isn't minutes and source")
        minutes, source = pair
        if source not in ("route", "estimate"):
            raise Refused("a walk's source isn't route or estimate")
        found[key] = (_int(minutes, "minutes", 0, 100_000), source)
    return found


def _key_result(value) -> keytest.KeyResult:
    fields = _obj(value, ("state", "message"))
    if fields["state"] not in keytest.STATES:
        raise Refused("state isn't a key test's")
    return keytest.KeyResult(fields["state"], safe_message(_str(fields["message"], "message", 400)))


def _lines(value, what: str) -> list[str]:
    return [_str(line, what, 16, 1, _LINE) for line in _list(value, what, 200)]


def _route(value) -> dict:
    fields = _obj(value, ("stop", "stops"))
    stops = _list(fields["stops"], "route stops", routesetup.MAX_STOPS)
    if len(stops) < 2:
        raise Refused("a route strip has fewer than two stops")
    out = []
    for stop in stops:
        entry = _obj(stop, ("name", "lat", "lon"))
        lat, lon = _point({"lat": entry["lat"], "lon": entry["lon"]}, "route stop")
        out.append({"name": _str(entry["name"], "route stop", 80, 1), "lat": lat, "lon": lon})
    return {"stop": _str(fields["stop"], "route stop label", 3, 1, _LABEL), "stops": out}


def _result(value) -> dict:
    """A finished search's result, in discover.SetupJob's shape."""
    fields = _obj(value, ("fragment", "lines", "document_operators", "stop_lines", "datasets_scanned",
                          "journeys_found", "timetable_journeys", "notes"))
    bus = _obj(_obj(fields["fragment"], ("bus",))["bus"], ("source", "operators"), ("routes",))
    if bus["source"] != "bods":
        raise Refused("source isn't bods")
    out_bus: dict = {"source": "bods",
                     "operators": [_str(n, "operator", 8, 2, _NOC) for n in _list(bus["operators"], "operators", 50)]}
    if "routes" in bus:
        out_bus["routes"] = {_str(line, "line", 16, 1, _LINE): _route(route)
                             for line, route in _dict(bus["routes"], "routes", 100).items()}
    return {
        "fragment": {"bus": out_bus},
        "lines": {_str(n, "operator", 8, 2, _NOC): _lines(lines, "line")
                  for n, lines in _dict(fields["lines"], "lines", 50).items()},
        "document_operators": {
            _str(n, "operator", 8, 2, _NOC): [_str(c, "operator", 8, 2, _NOC) for c in _list(codes, "codes", 20)]
            for n, codes in _dict(fields["document_operators"], "document_operators", 50).items()},
        "stop_lines": {_str(label, "label", 3, 1, _LABEL): _lines(lines, "line")
                       for label, lines in _dict(fields["stop_lines"], "stop_lines", setup.MAX_BOARD_STOPS).items()},
        "datasets_scanned": _int(fields["datasets_scanned"], "datasets_scanned", 0, 10_000),
        "journeys_found": _int(fields["journeys_found"], "journeys_found", 0, 100_000_000),
        "timetable_journeys": _int(fields["timetable_journeys"], "timetable_journeys", 0, 100_000_000),
        "notes": [safe_message(_str(n, "note", 400)) for n in _list(fields["notes"], "notes", MAX_NOTES)],
    }


def check_status(value) -> dict:
    """A search's status, as discover.SetupJob.status gives it."""
    fields = _obj(value, ("state", "step", "done", "total", "message", "result"))
    state = fields["state"]
    if state not in _STATES:
        raise Refused("state isn't a search's")
    result = fields["result"]
    if state == "done":
        result = _result(result)
    elif result is not None:
        raise Refused("a result before the search is done")
    return {"state": state, "step": _str(fields["step"], "step", 100),
            "done": _int(fields["done"], "done", 0, 1_000_000), "total": _int(fields["total"], "total", 0, 1_000_000),
            "message": safe_message(_str(fields["message"], "message", 400)), "result": result}


def check_reply(data, request_id: str) -> dict:
    """{ok, value} or {ok, error, why} of the reply to `request_id`, or Refused."""
    top = _obj(data, ("v", "id", "ok"), ("value", "error", "why", "beat"))
    if top["v"] != VERSION or top["id"] != request_id:
        raise Refused("wrong version or id")
    if top["ok"] is True:
        if "value" not in top or "error" in top or "why" in top:
            raise Refused("a reply that is neither answer nor error")
        if "beat" in top:
            _int(top["beat"], "beat", 0, 10**9)
        return top
    if top["ok"] is not False or "value" in top:
        raise Refused("a reply that is neither answer nor error")
    _str(top.get("error"), "error", 400, 1)
    if top.get("why") not in ("setup", "fault"):
        raise Refused("an error of no known kind")
    return top


# The spool folder and its files.


def spool_folder(state: Path | None, environ: Mapping[str, str] | None = None) -> Path | None:
    """Where the spool is: systemd's $RUNTIME_DIRECTORY (memory); else
    RUNTIME if it is there (run by hand on the Pi); else setup-spool in the
    `state` folder (on disk: the tests, a Mac); None without one.
    SpoolError if $RUNTIME_DIRECTORY isn't one absolute path."""
    given = (os.environ if environ is None else environ).get("RUNTIME_DIRECTORY", "")
    if given:
        if not os.path.isabs(given) or ":" in given:
            raise SpoolError("RUNTIME_DIRECTORY must be one absolute folder for the setup spool")
        return Path(given)
    if os.path.lexists(RUNTIME):  # Spool.make refuses it unless it's a real folder of ours
        return RUNTIME
    return None if state is None else state / SPOOL


class Spool:
    """The three folders, and reading and writing files in them safely."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.tmp = self.folder / "tmp"
        self.requests = self.folder / "requests"
        self.replies = self.folder / "replies"

    def make(self) -> None:
        """Make the folders (0700) if need be, and check each is a real folder of ours."""
        for path in (self.folder, self.tmp, self.requests, self.replies):
            try:
                path.mkdir(mode=0o700, exist_ok=True)
                info = os.lstat(path)
            except OSError as error:
                raise SpoolError(f"Can't make the setup spool folder {path}: {error.strerror or error}") from None
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise SpoolError(f"{path} must be a folder owned by this user, not a link")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(path, 0o700)

    def write(self, where: Path, file_id: str, data: dict, cap: int) -> None:
        """Write `data` as <file_id>.json in `where`, whole: made new in tmp/ (0600), then renamed."""
        body = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > cap:
            raise ValueError("Too big for the setup spool")
        tmp = self.tmp / f"{file_id}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(body)
            os.replace(tmp, where / f"{file_id}.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def read(path: Path, cap: int, delete: bool) -> bytes | None:
        """A file's bytes; None if there is none. Refused if it isn't a plain
        file of ours, with one link, within `cap`. With `delete`, it is gone
        afterwards whatever it held."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:  # a link, or something else that won't open as a file
            if delete:
                _remove(path)
            raise Refused("not a plain file") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_size > cap):
                body = None
            else:
                body = os.read(fd, cap + 1)
        finally:
            os.close(fd)
            if delete:
                path.unlink(missing_ok=True)
        if body is None or len(body) > cap:
            raise Refused("not a plain file of ours within its size")
        return body

    def sweep(self, now: float) -> None:
        """Delete what has been left too long: half-written files, requests no
        one is waiting for, replies no one collected."""
        for where, age in ((self.tmp, TMP_MAX_AGE_S), (self.requests, REQUEST_MAX_AGE_S),
                           (self.replies, REPLY_MAX_AGE_S)):
            try:
                names = os.listdir(where)
            except OSError:
                continue
            for name in names:
                path = where / name
                try:
                    old = now - os.lstat(path).st_mtime > age
                except OSError:
                    continue
                if old:
                    _remove(path)


def _remove(path: Path) -> None:
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


# The page's side: a setupapi.Runner that asks the setup service.


def _place_args(place) -> dict:
    return {"lat": place.lat, "lon": place.lon}


class SpoolRunner:
    """Asks the setup service, through the spool, to do setup's internet
    work, and waits for its answers. For setupapi.SetupApi(runner=...)."""

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

    def reply(self, request_id: str, delete: bool) -> dict | None:
        """The reply to `request_id`, checked, or None if there is none yet.
        SetupError if it doesn't fit."""
        try:
            body = Spool.read(self.spool.replies / f"{request_id}.json", MAX_REPLY_BYTES, delete)
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
            raise RuntimeError("The setup service failed")  # setupapi answers a plain 500
        return reply["value"]

    def _check(self, value, check):
        try:
            return check(value)
        except Refused as why:
            log.warning("Refused the setup service's reply: %s", why)
            raise SetupError(UNFIT) from None

    # setupapi.Runner.

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
                 walk_minutes: Mapping[str, int]) -> "SpoolJob":
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
                self._update()
            return copy.deepcopy(self._status)

    def _end(self, message: str) -> None:
        self._status.update(state="failed", message=message, result=None)
        self._finished = True
        self._runner.withdraw(self.id)
        (self._runner.spool.replies / f"{self.id}.json").unlink(missing_ok=True)

    def _update(self) -> None:
        now = self._runner.monotonic()
        try:
            reply = self._runner.reply(self.id, delete=False)
        except SetupError as error:
            self._end(str(error))
            return
        if reply is None:
            if self._beat is not None:  # swept away after going unread for long
                self._end(GONE)
            elif now - self._started > self._start_wait:
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
        if status["state"] in _FINISHED:
            self._finished = True  # kept here from now on; the file has done its job
            (self._runner.spool.replies / f"{self.id}.json").unlink(missing_ok=True)
        elif now - self._beat[1] > self._silent:
            log.warning("The setup service stopped mid-search")
            self._end(STOPPED)


# The service's side.


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
            notes = notes[: MAX_NOTES - 1] + [f"And {len(notes) - MAX_NOTES + 1} more notes"]
        out["result"] = {**result, "notes": notes}
    else:
        out["result"] = None
    return out


def _failed(message: str) -> dict:
    return {"state": "failed", "step": "", "done": 0, "total": 0, "message": message, "result": None}


class Worker:
    """The setup service: takes requests from the spool, does them, writes
    the replies, and runs at most one search at a time (discover.SetupJob's
    lock in the board's state folder also keeps it to one with the command
    line's). run() keeps going until it has had nothing to do for `idle`
    seconds and no search is running."""

    def __init__(
        self,
        folder: Path | str,
        *,
        job_folder: Path | str,
        load_secrets: Callable[[], Mapping[str, str]],
        settings: Callable[[], Mapping] = dict,
        lookup_postcode: Callable[[str], Place] = setup.lookup_postcode,
        search_address: Callable[[str], list[Place]] = setup.search_address,
        nearby_stops: Callable[..., list[Stop]] = setup.nearby_stops,
        walk_minutes: Callable[..., dict[str, tuple[int, str]]] = setup.walk_minutes,
        make_job: Callable[..., object] = discover.SetupJob,
        test_key: Callable[..., keytest.KeyResult] = keytest.test_key,
        # Behind the page's own limit, in case something else writes requests; looser so the two never disagree.
        key_tests: RateLimit | None = None,
        idle: float = IDLE_S,
        poll: float = POLL_S,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        stop: threading.Event | None = None,
    ) -> None:
        """`folder` is the spool; `job_folder` the board's state folder, where
        the search builds the timetable. `load_secrets` gives the saved keys,
        as the other units' environment does, and `settings` the board's
        settings, for what a key test asks about. Setting `stop` ends run()."""
        self.spool = Spool(folder)
        self._job_folder = Path(job_folder)
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
        self._job = None
        self._job_id = ""
        self._job_secrets: list[str] = []
        self._shown: dict | None = None
        self._beat = 0
        self._written = 0.0
        self._cancelled: list[str] = []  # cancels that came before their search

    def run(self) -> int:
        self.spool.make()
        fd = os.open(self.spool.folder / WORKER_LOCK, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log.info("Another setup service is already answering")
                return 0
            self.spool.sweep(self._clock())
            last = self._monotonic()
            while not self._stop.is_set():
                if self.step() or self._job is not None:
                    last = self._monotonic()
                elif self._monotonic() - last >= self._idle:
                    break
                self._stop.wait(self._poll)
            if self._job is not None:  # stopped mid-search: say so rather than go quiet
                self._job.cancel()
                self._write_status(_failed("Cancelled"), force=True)
            return 0
        finally:
            os.close(fd)

    def step(self) -> bool:
        """Answer every waiting request and report the search. Whether any request came."""
        handled = False
        for path in self._waiting():
            handled = True
            self._take(path)
        if self._job is not None:
            self._report()
        return handled

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
        log.info("Setup request: %s", kind)
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

    def _reply(self, request_id: str, reply: dict) -> None:
        try:
            self.spool.write(self.spool.replies, request_id, {"v": VERSION, "id": request_id, **reply},
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
            home = Place(*args["home"], "")
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
        if self._job is not None and self._job_id == job_id:
            self._job.cancel()
        else:
            self._cancelled = [*self._cancelled[-31:], job_id]

    def _search(self, request_id: str, args: dict) -> None:
        if request_id in self._cancelled:
            self._reply(request_id, {"ok": True, "beat": 0, "value": _failed("Cancelled")})
            return
        if self._job is not None:
            self._reply(request_id, {"ok": True, "beat": 0,
                                     "value": _failed("A search is already running; wait for it or cancel it")})
            return
        saved = ""
        try:
            saved = str(self._load_secrets().get("BODS_API_KEY", "")).strip()
        except Exception as error:  # noqa: BLE001 - said below as no key
            log.warning("Can't read the keys: %s", type(error).__name__)
        key = args["key"] or saved
        if not key:
            self._reply(request_id, {"ok": True, "beat": 0, "value": _failed(
                "Paste your Bus Open Data Service key first; finding bus companies needs it")})
            return
        try:
            job = self._make_job(self._job_folder, args["stops"], key, routes=args["routes"],
                                 walk_minutes=args["walk"])
            job.start()
        except discover.SetupBusy:
            self._reply(request_id, {"ok": True, "beat": 0,
                                     "value": _failed("A setup search is already running for this board")})
            return
        except Exception as error:  # noqa: BLE001 - said plainly; never its text
            log.warning("Setup search failed to start: %s", type(error).__name__)
            self._reply(request_id, {"ok": True, "beat": 0, "value": _failed("Something went wrong; try again")})
            return
        self._job, self._job_id, self._shown = job, request_id, None
        self._job_secrets = self._secrets([args["key"]])
        self._report()

    def _report(self) -> None:
        status = self._job.status()
        self._write_status(_status_out(status))
        if status.get("state") in _FINISHED:
            self._job, self._job_secrets = None, []

    def _write_status(self, shown: dict, force: bool = False) -> None:
        now = self._monotonic()
        if not force and shown == self._shown and now - self._written < HEARTBEAT_S:
            return
        self._beat += 1
        self._shown, self._written = shown, now
        self._reply(self._job_id, {"ok": True, "beat": self._beat, "value": _scrub(shown, self._job_secrets)})

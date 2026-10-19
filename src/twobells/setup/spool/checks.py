"""The spool's wire format: checking what the other side wrote.

Requests, as the service checks them, and replies, as the page checks
them. Each check says what was wrong by field, never by value.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable

from ...core.bounded import finite
from ...core.errors import safe_message
from ...core.places import in_uk
from ...settings.schema import ATCO as _ATCO
from ...settings.schema import CONTROL as _CONTROL
from ...settings.schema import KEY_VALUE as _KEY_VALUE
from ...settings.schema import LABEL as _LABEL
from ...settings.schema import NOC as _NOC
from ...timetable import routesetup
from .. import discover, inputs, keytest
from .. import lookups as setup
from ..api import too_deep
from ..inputs import MAX_WALK_MINUTES, Invalid
from ..lookups import Place, Stop

VERSION = 1
MAX_DEPTH = 12
REQUEST_MAX_AGE_S = 150.0  # longer than any wait: past this, whoever asked has given up
CLOCK_SKEW_S = 5.0  # a request from the future, by more than this, is refused
MAX_NOTES = 30

_ID = re.compile(r"^[0-9a-f]{32}$")
_CRS = re.compile(r"^[A-Z]{3}$")  # upper case, as setup sends it
_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./+-]{0,15}$")
_STATES = ("queued", "running", "done", "failed")
FINISHED = ("done", "failed")


class Refused(Exception):
    """A spool file that isn't what it should be. The text names no value."""


def check_obj(value, required, optional=()) -> dict:
    if not isinstance(value, dict):
        raise Refused("not an object")
    if set(value) - set(required) - set(optional):
        raise Refused("unexpected fields")
    if any(name not in value for name in required):
        raise Refused("missing fields")
    return value


def check_str(value, what: str, high: int, low: int = 0, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high or _CONTROL.search(value):
        raise Refused(f"{what} isn't plain text of the right length")
    if pattern is not None and not pattern.fullmatch(value):
        raise Refused(f"{what} isn't in the right form")
    return value


def check_int(value, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refused(f"{what} isn't a whole number in range")
    return value


def check_num(value, what: str) -> float:
    number = finite(value)
    if number is None:
        raise Refused(f"{what} isn't a number")
    return number


def check_list(value, what: str, high: int) -> list:
    if not isinstance(value, list) or len(value) > high:
        raise Refused(f"{what} isn't a short enough list")
    return value


def check_dict(value, what: str, high: int) -> dict:
    if not isinstance(value, dict) or len(value) > high:
        raise Refused(f"{what} isn't a small enough object")
    return value


def _point(value, what: str) -> tuple[float, float]:
    fields = check_obj(value, ("lat", "lon"))
    lat, lon = check_num(fields["lat"], what), check_num(fields["lon"], what)
    if not in_uk(lat, lon):
        raise Refused(f"{what} isn't in the UK")
    return lat, lon


def _key(value, what: str) -> str:
    """A pasted key, or "" for none."""
    if value == "":
        return ""
    return check_str(value, what, 200, 1, _KEY_VALUE)


def _no_constant(name: str):
    raise ValueError(f"{name} is not a number")


def parse(body: bytes, max_depth: int = MAX_DEPTH):
    """Strict JSON: UTF-8, no NaN or Infinity, no deeper than `max_depth`."""
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise Refused("not JSON") from None
    if too_deep(value, max_depth):
        raise Refused("nested too deeply")
    return value


# Requests, as the service checks them: (kind, its arguments checked).


def _postcode_args(args) -> dict:
    fields = check_obj(args, ("postcode",))
    return {"postcode": check_str(fields["postcode"], "postcode", 16, 1)}


def _address_args(args) -> dict:
    fields = check_obj(args, ("q",))
    return {"q": check_str(fields["q"], "q", setup.ADDRESS_LENGTH[1], setup.ADDRESS_LENGTH[0])}


def _stops_args(args) -> dict:
    fields = check_obj(args, ("home", "within_m", "limit"))
    within = check_num(fields["within_m"], "within_m")
    if not setup.STOP_RADIUS_M[0] <= within <= setup.STOP_RADIUS_M[1]:
        raise Refused("within_m is out of range")
    return {"home": _point(fields["home"], "home"), "within_m": within,
            "limit": check_int(fields["limit"], "limit", 1, setup.MAX_STOPS)}


def _walk_args(args) -> dict:
    fields = check_obj(args, ("home", "places"))
    places = {}
    for key, place in check_dict(fields["places"], "places", setup.MAX_WALK_DESTINATIONS).items():
        if not (_CRS.fullmatch(key) or _ATCO.fullmatch(key)):
            raise Refused("a place isn't named by a station or stop code")
        places[key] = _point(place, "place")
    return {"home": _point(fields["home"], "home"), "places": places}


def _testkey_args(args) -> dict:
    fields = check_obj(args, ("name", "value", "app_id"))
    name = fields["name"]
    if name not in keytest.KEY_NAMES:
        raise Refused("name isn't a key's")
    app_id = _key(fields["app_id"], "app_id")
    if app_id and name != "TAPI_APP_KEY":
        raise Refused("only TransportAPI's app key is tested with an app ID")
    return {"name": name, "value": _key(fields["value"], "value"), "app_id": app_id}


def _search_args(args) -> dict:
    fields = check_obj(args, ("stops", "routes", "walk", "key"))
    try:
        stops = inputs.stops(fields["stops"], least=1)
    except Invalid:
        raise Refused("stops aren't labelled stop codes") from None
    if not isinstance(fields["routes"], bool):
        raise Refused("routes isn't true or false")
    walk = check_dict(fields["walk"], "walk", len(stops))
    if set(walk) - set(stops):
        raise Refused("walk isn't by stop label")
    return {"stops": stops, "routes": fields["routes"],
            "walk": {label: check_int(m, "walk", 0, MAX_WALK_MINUTES) for label, m in walk.items()},
            "key": _key(fields["key"], "key")}


def _cancel_args(args) -> dict:
    fields = check_obj(args, ("job",))
    return {"job": check_str(fields["job"], "job", 32, 32, _ID)}


def _active_args(args) -> dict:
    return check_obj(args, ())  # nothing: it says only that a browser is using the dashboard


_REQUESTS: dict[str, Callable[[object], dict]] = {
    "postcode": _postcode_args,
    "address": _address_args,
    "stops": _stops_args,
    "walk": _walk_args,
    "testkey": _testkey_args,
    "search": _search_args,
    "cancel": _cancel_args,
    "active": _active_args,
}


def check_request(data, file_id: str, now: float) -> tuple[str, dict]:
    """(kind, arguments) of a request named `file_id`, or Refused."""
    top = check_obj(data, ("v", "id", "made", "kind", "args"))
    if top["v"] != VERSION or top["id"] != file_id:
        raise Refused("wrong version or id")
    made = check_num(top["made"], "made")
    if not now - REQUEST_MAX_AGE_S <= made <= now + CLOCK_SKEW_S:
        raise Refused("stale")
    kind = top["kind"]
    if kind not in _REQUESTS:
        raise Refused("unknown kind")
    return kind, _REQUESTS[kind](top["args"])


# Replies, as the page checks them.


def check_place(value) -> Place:
    fields = check_obj(value, ("lat", "lon", "label", "country", "district"))
    lat, lon = _point({"lat": fields["lat"], "lon": fields["lon"]}, "place")
    return Place(lat, lon, check_str(fields["label"], "label", 200), check_str(fields["country"], "country", 40),
                 check_str(fields["district"], "district", 100))


def check_stop(value, within_m: float) -> Stop:
    fields = check_obj(value, ("atco", "name", "indicator", "bearing", "lat", "lon", "distance_m"))
    lat, lon = _point({"lat": fields["lat"], "lon": fields["lon"]}, "stop")
    bearing = fields["bearing"]
    if bearing not in setup.BEARINGS and bearing != "":
        raise Refused("bearing isn't a compass point")
    return Stop(check_str(fields["atco"], "atco", 12, 4, _ATCO), check_str(fields["name"], "name", 200, 1),
                check_str(fields["indicator"], "indicator", 60), bearing, lat, lon,
                check_int(fields["distance_m"], "distance_m", 0, math.ceil(within_m)))


def check_walked(value, asked) -> dict[str, tuple[int, str]]:
    found = {}
    for key, pair in check_dict(value, "walk", len(asked)).items():
        if key not in asked:
            raise Refused("a walk to somewhere not asked about")
        if not isinstance(pair, list) or len(pair) != 2:
            raise Refused("a walk isn't minutes and source")
        minutes, source = pair
        if source not in ("route", "estimate"):
            raise Refused("a walk's source isn't route or estimate")
        found[key] = (check_int(minutes, "minutes", 0, 100_000), source)
    return found


def check_key_result(value) -> keytest.KeyResult:
    fields = check_obj(value, ("state", "message"))
    if fields["state"] not in keytest.STATES:
        raise Refused("state isn't a key test's")
    return keytest.KeyResult(fields["state"], safe_message(check_str(fields["message"], "message", 400)))


def _lines(value, what: str) -> list[str]:
    return [check_str(line, what, 16, 1, _LINE) for line in check_list(value, what, 200)]


def _route(value) -> dict:
    fields = check_obj(value, ("stop", "stops"))
    stops = check_list(fields["stops"], "route stops", routesetup.MAX_STOPS)
    if len(stops) < 2:
        raise Refused("a route strip has fewer than two stops")
    out = []
    for stop in stops:
        entry = check_obj(stop, ("name", "lat", "lon"))
        lat, lon = _point({"lat": entry["lat"], "lon": entry["lon"]}, "route stop")
        out.append({"name": check_str(entry["name"], "route stop", 80, 1), "lat": lat, "lon": lon})
    return {"stop": check_str(fields["stop"], "route stop label", 3, 1, _LABEL), "stops": out}


def _result(value) -> dict:
    """A finished search's result, in discover.SetupJob's shape."""
    fields = check_obj(value, ("fragment", "lines", "document_operators", "stop_lines", "datasets_scanned",
                               "journeys_found", "timetable_journeys", "notes"))
    bus = check_obj(check_obj(fields["fragment"], ("bus",))["bus"], ("source", "operators"), ("routes",))
    if bus["source"] != "bods":
        raise Refused("source isn't bods")
    out_bus: dict = {"source": "bods",
                     "operators": [check_str(n, "operator", 8, 2, _NOC)
                                   for n in check_list(bus["operators"], "operators", 50)]}
    if "routes" in bus:
        out_bus["routes"] = {check_str(line, "line", 16, 1, _LINE): _route(route)
                             for line, route in check_dict(bus["routes"], "routes", 100).items()}
    return {
        "fragment": {"bus": out_bus},
        "lines": {check_str(n, "operator", 8, 2, _NOC): _lines(lines, "line")
                  for n, lines in check_dict(fields["lines"], "lines", 50).items()},
        "document_operators": {
            check_str(n, "operator", 8, 2, _NOC): [check_str(c, "operator", 8, 2, _NOC)
                                                   for c in check_list(codes, "codes", 20)]
            for n, codes in check_dict(fields["document_operators"], "document_operators", 50).items()},
        "stop_lines": {check_str(label, "label", 3, 1, _LABEL): _lines(lines, "line")
                       for label, lines
                       in check_dict(fields["stop_lines"], "stop_lines", setup.MAX_BOARD_STOPS).items()},
        "datasets_scanned": check_int(fields["datasets_scanned"], "datasets_scanned", 0, 10_000),
        "journeys_found": check_int(fields["journeys_found"], "journeys_found", 0, 100_000_000),
        "timetable_journeys": check_int(fields["timetable_journeys"], "timetable_journeys", 0, 100_000_000),
        "notes": [safe_message(check_str(n, "note", 400)) for n in check_list(fields["notes"], "notes", MAX_NOTES)],
    }


def _roads(value) -> dict:
    fields = check_obj(value, ("state", "done", "total"))
    if fields["state"] not in discover.ROADS_STATES:
        raise Refused("the roads' state isn't one they have")
    total = check_int(fields["total"], "roads total", 0, 1_000_000)
    return {"state": fields["state"], "done": check_int(fields["done"], "roads done", 0, total), "total": total}


def check_status(value) -> dict:
    """A search's status, as discover.SetupJob.status gives it: once done,
    perhaps with its roads."""
    fields = check_obj(value, ("state", "step", "done", "total", "message", "result"), ("roads",))
    state = fields["state"]
    if state not in _STATES:
        raise Refused("state isn't a search's")
    result = fields["result"]
    if state == "done":
        result = _result(result)
    elif result is not None:
        raise Refused("a result before the search is done")
    status = {"state": state, "step": check_str(fields["step"], "step", 100),
              "done": check_int(fields["done"], "done", 0, 1_000_000),
              "total": check_int(fields["total"], "total", 0, 1_000_000),
              "message": safe_message(check_str(fields["message"], "message", 400)), "result": result}
    if fields.get("roads") is not None:
        if state != "done":
            raise Refused("roads before the search is done")
        status["roads"] = _roads(fields["roads"])
    return status


def check_reply(data, request_id: str) -> dict:
    """{ok, value} or {ok, error, why} of the reply to `request_id`, or Refused."""
    top = check_obj(data, ("v", "id", "ok"), ("value", "error", "why", "beat"))
    if top["v"] != VERSION or top["id"] != request_id:
        raise Refused("wrong version or id")
    if top["ok"] is True:
        if "value" not in top or "error" in top or "why" in top:
            raise Refused("a reply that is neither answer nor error")
        if "beat" in top:
            check_int(top["beat"], "beat", 0, 10**9)
        return top
    if top["ok"] is not False or "value" in top:
        raise Refused("a reply that is neither answer nor error")
    check_str(top.get("error"), "error", 400, 1)
    if top.get("why") not in ("setup", "fault"):
        raise Refused("an error of no known kind")
    return top

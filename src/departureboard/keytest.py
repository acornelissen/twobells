"""Testing one key with one harmless call to the service it is for.

`test_key(name, value, config_data, env)` says whether a key works, in one
of six states, with a plain-English message:

- works: the service answered as it does for a good key;
- wrong_key: it refused the key (HTTP 401 or 403);
- not_subscribed: the Rail Data Marketplace knows the key but it is for
  another product ("Invalid ApiKey for given resource");
- service_down: no answer in time, no connection, or a 5xx;
- missing: no key to test;
- unknown: an answer that says neither way.

Each test asks for as little as it can: the departure board for the
configured station (or Glossop) with one row; for Service Details, that
board (with the Live Departure Board key) and then one train from it; the
Knowledgebase incidents address from [dashboard] kb_incidents_url, read no
further than its first part; BODS bus positions in a box a few hundred
metres across around the route strips' own stops (positions the board
already sends BODS) or a fixed point in Glossop; and one stop's timetable
from TransportAPI, which costs one of its 30 free lookups a day, so a page
asks before testing that.

A key never appears in a message: messages are built from fixed words and
checked values only (a station code, an HTTP status), never the service's
own text; then the keys are replaced and errors.safe_message applied anyway.
Logs get the key's name and the state, nothing more.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request

from . import bods, darwin, http, transportapi
from .disruptions import kb_url_is_placeholder
from .errors import safe_message

log = logging.getLogger(__name__)

STATES = ("works", "wrong_key", "not_subscribed", "service_down", "missing", "unknown")
KEY_NAMES = ("RDM_API_KEY", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY", "BODS_API_KEY", "TAPI_APP_ID", "TAPI_APP_KEY")
# Testing these spends one of TransportAPI's 30 free lookups for the day.
COSTS_A_LOOKUP = frozenset({"TAPI_APP_ID", "TAPI_APP_KEY"})

TIMEOUT = 10.0  # seconds per call; someone is waiting on the page
MAX_BYTES = 256 * 1024  # read no more of any answer than this
_ERROR_BYTES = 4096  # of an error's body, to tell a wrong key from an unsubscribed one
_MIN_REPLACED = 4  # a key shorter than this can't be told from ordinary words

DEFAULT_CRS = "GLO"
# Glossop, Henry Street: a public stop and its position for the test call, used when
# none is configured.
DEFAULT_ATCO = "1000DGHS0900"
DEFAULT_POINT = (53.44384, -1.948999)
BOX_MARGIN = 0.003  # degrees, a few hundred metres
_MAX_BOX = 0.1  # degrees across; wider than this and only one stop is used

_CRS = re.compile(r"[A-Za-z]{3}")
_ATCO = re.compile(r"[0-9]{3}[0-9A-Za-z]{1,9}")
# A Darwin service ID in the right shape that no train has: GetServiceDetails
# checks the key first, so a refusal says as much as with a real train.
_NO_TRAIN = "000000000000000"
# The Rail Data Marketplace's gateway, for a real key on a product it isn't subscribed to.
_OTHER_PRODUCT = "invalid apikey for given resource"
_ALLOWANCE = ("limit", "quota", "exceeded")

Fetch = Callable[[str, Mapping[str, str]], bytes]


@dataclass(frozen=True)
class KeyResult:
    state: str  # one of STATES
    message: str  # plain English; never the key


@dataclass(frozen=True)
class _Service:
    owner: str  # who answers, like "The Rail Data Marketplace"
    wrong: str  # what to check when the key is refused
    product: str = ""  # the Rail Data Marketplace product, which has a key of its own
    this: str = "this key"


_RDM = "The Rail Data Marketplace"
_CONSUMER_KEY = "Check you copied the Consumer key, not the Consumer secret."
_SERVICES = {
    "RDM_API_KEY": _Service(_RDM, _CONSUMER_KEY, "Live Departure Board"),
    "RDM_DETAILS_API_KEY": _Service(_RDM, _CONSUMER_KEY, "Service Details"),
    "RDM_KB_API_KEY": _Service(_RDM, _CONSUMER_KEY, "Knowledgebase Incidents"),
    "BODS_API_KEY": _Service("The Bus Open Data Service", "Copy the API key again from Account settings on its site."),
    "TAPI_APP_ID": _Service("TransportAPI", "Copy the app_id and app_key again, both from the same app.",
                            this="these keys"),
}
_SERVICES["TAPI_APP_KEY"] = _SERVICES["TAPI_APP_ID"]


def _fetch(url: str, headers: Mapping[str, str]) -> bytes:
    """An answer's body, at most MAX_BYTES of it: a test needs no more."""
    request = Request(url, headers={"User-Agent": http.USER_AGENT, "Accept": "application/json", **headers})  # noqa: S310
    with http.urlopen(request, timeout=TIMEOUT) as response:
        return response.read(MAX_BYTES)


class _Outcome(Exception):
    """Ends a test early with its result."""

    def __init__(self, result: KeyResult):
        super().__init__(result.state)
        self.result = result


_SLOW = "took too long to answer"
_UNREACHABLE = "couldn't be reached. It may be down, or there may be no internet here"


def _call(fetch: Fetch, url: str, headers: Mapping[str, str], service: _Service) -> bytes:
    """One request; a failure becomes the result it means, as an _Outcome."""
    try:
        return fetch(url, headers)
    except HTTPError as error:
        raise _Outcome(_refused(error, service)) from None
    except TimeoutError:  # socket.timeout too
        raise _Outcome(_down(service, _SLOW)) from None
    except URLError as error:
        raise _Outcome(_down(service, _SLOW if isinstance(error.reason, TimeoutError) else _UNREACHABLE)) from None
    except OSError:
        raise _Outcome(_down(service, _UNREACHABLE)) from None


def _down(service: _Service, what: str) -> KeyResult:
    return KeyResult("service_down", f"{service.owner} {what}. Try again in a few minutes.")


def _error_body(error: HTTPError) -> str:
    try:
        return error.read(_ERROR_BYTES).decode("utf-8", "replace").lower()
    except Exception:  # noqa: BLE001 - an unreadable body just tells us nothing
        return ""


def _refused(error: HTTPError, service: _Service) -> KeyResult:
    """What an HTTP error says about the key. The service's own text is read
    for clues, never repeated."""
    code = error.code
    if code in (401, 403):
        body = _error_body(error)
        if service.product and _OTHER_PRODUCT in body:
            return KeyResult("not_subscribed", "This key is for another Rail Data Marketplace product. "
                                               f"Subscribe to {service.product} and copy its own Consumer key.")
        if any(word in body for word in _ALLOWANCE):
            return _allowance(service)
        return _wrong(service)
    if code == 429:
        return _allowance(service)
    if 500 <= code <= 599:
        return _down(service, f"isn't working properly just now (error {code})")
    return KeyResult("unknown", f"{service.owner} gave an unexpected answer (error {code}), "
                                "so this can't tell if the key works.")


def _wrong(service: _Service) -> KeyResult:
    return KeyResult("wrong_key", f"{service.owner} refused {service.this}. {service.wrong}")


def _allowance(service: _Service) -> KeyResult:
    return KeyResult("unknown", f"{service.owner} says its allowance is used up for now, "
                                "so the key can't be checked. Try again tomorrow.")


def _unreadable(service: _Service) -> KeyResult:
    return KeyResult("unknown", f"{service.owner} sent an answer this can't read, so it can't tell if the key works.")


def _json(body: bytes, service: _Service):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _Outcome(_unreadable(service)) from None


# What to ask about, from the settings. Hand edits can put anything anywhere,
# so each value is checked, with a fixed default for anything odd.


def _section(config_data: Mapping, name: str) -> Mapping:
    """A table of the settings; empty if it is missing or switched off (its
    settings are kept for switching it back on, but nothing is sent to them)."""
    section = config_data.get(name) if isinstance(config_data, Mapping) else None
    if not isinstance(section, Mapping) or section.get("enabled", True) is not True:
        return {}
    return section


def _https(value, default: str) -> str:
    """A configured address if it is a plain https one, else `default`."""
    if not isinstance(value, str):
        return default
    url = value.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        return default
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or any(c.isspace() or not c.isprintable() for c in url)):
        return default
    return url


def _crs(config_data: Mapping) -> str:
    crs = _section(config_data, "train").get("crs")
    if isinstance(crs, str) and _CRS.fullmatch(crs.strip()):
        return crs.strip().upper()
    return DEFAULT_CRS


def _board_url(config_data: Mapping) -> str:
    base = _https(_section(config_data, "train").get("base_url"), darwin.DEFAULT_BASE_URL)
    return f"{base}/GetDepartureBoard/{_crs(config_data)}?{urlencode({'numRows': 1})}"


def _own_stops(config_data: Mapping) -> list[tuple[float, float]]:
    """Where the route strips' own stops are (each strip's last, as config.toml has it)."""
    routes = _section(config_data, "bus").get("routes")
    points = []
    for route in routes.values() if isinstance(routes, Mapping) else ():
        stops = route.get("stops") if isinstance(route, Mapping) else None
        last = stops[-1] if isinstance(stops, list) and stops else None
        if not isinstance(last, Mapping):
            continue
        lat, lon = last.get("lat"), last.get("lon")
        if (all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon))
                and 49.0 <= lat <= 61.0 and -8.7 <= lon <= 2.0):
            points.append((float(lat), float(lon)))
    return points


def _box(config_data: Mapping) -> tuple[float, float, float, float]:
    """(west, south, east, north), a few hundred metres around the stops or the fixed point."""
    points = _own_stops(config_data) or [DEFAULT_POINT]
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    if max(lats) - min(lats) > _MAX_BOX or max(lons) - min(lons) > _MAX_BOX:
        lats, lons = lats[:1], lons[:1]
    return (min(lons) - BOX_MARGIN, min(lats) - BOX_MARGIN, max(lons) + BOX_MARGIN, max(lats) + BOX_MARGIN)


def _stop(config_data: Mapping) -> str:
    stops = _section(config_data, "bus").get("stops")
    for atco in stops.values() if isinstance(stops, Mapping) else ():
        if isinstance(atco, str) and _ATCO.fullmatch(atco.strip()):
            return atco.strip()
    return DEFAULT_ATCO


# One test per service. Each returns its result or raises _Outcome.


def _darwin(keys: Mapping[str, str], config_data: Mapping, fetch: Fetch) -> KeyResult:
    service = _SERVICES["RDM_API_KEY"]
    board = _json(_call(fetch, _board_url(config_data), {"x-apikey": keys["RDM_API_KEY"]}, service), service)
    if not isinstance(board, dict):
        return _unreadable(service)
    return KeyResult("works", f"It works: Live Departure Board answered for {_crs(config_data)}.")


def _a_train(keys: Mapping[str, str], config_data: Mapping, fetch: Fetch) -> str | None:
    """A train's service ID from the board, asked with the Live Departure
    Board key; None without that key, a train, or an answer."""
    if not keys["RDM_API_KEY"]:
        return None
    service = _SERVICES["RDM_API_KEY"]
    try:
        raw = _json(_call(fetch, _board_url(config_data), {"x-apikey": keys["RDM_API_KEY"]}, service), service)
        board = darwin.parse_board_rich(raw)
    except (_Outcome, ValueError):
        return None
    return next((s.service_id for s in board.services if s.service_id), None)


def _details(keys: Mapping[str, str], config_data: Mapping, fetch: Fetch) -> KeyResult:
    service = _SERVICES["RDM_DETAILS_API_KEY"]
    train = _a_train(keys, config_data, fetch)
    url = f"{darwin.DETAILS_BASE_URL}/GetServiceDetails/{quote(train or _NO_TRAIN, safe='')}"
    def ask(url: str, headers: Mapping[str, str]) -> bytes | None:
        try:
            return fetch(url, headers)
        except HTTPError as error:
            # Darwin itself, past the gateway, answers a train it doesn't know
            # with its own 500 ({"Message": ...}); the gateway's errors are faults.
            if error.code >= 500:
                text = _error_body(error)
                if '"message"' in text and '"fault"' not in text:
                    return None
                raise HTTPError(error.url, error.code, error.reason, error.headers, io.BytesIO(text.encode())) from None
            raise

    try:
        body = _call(ask, url, {"x-apikey": keys["RDM_DETAILS_API_KEY"]}, service)
    except _Outcome as outcome:
        if outcome.result.state in ("wrong_key", "not_subscribed", "service_down"):
            raise
        body = None  # past the gateway, so the key is good; the train isn't (made up, or gone since)
    if train is not None and body is not None:
        try:
            darwin.parse_service_details(_json(body, service))
            return KeyResult("works", f"It works: Service Details answered for a train from {_crs(config_data)}.")
        except (_Outcome, darwin.ServiceGone):
            pass
    if keys["RDM_API_KEY"]:
        why = f"with no trains from {_crs(config_data)} just now it can't be fully checked. Try again later."
    else:
        why = "it can't be fully checked without a Live Departure Board key to find a train."
    return KeyResult("unknown", f"Service Details accepted the key, but {why}")


def _kb(keys: Mapping[str, str], config_data: Mapping, fetch: Fetch) -> KeyResult:
    service = _SERVICES["RDM_KB_API_KEY"]
    raw = _section(config_data, "dashboard").get("kb_incidents_url")
    url = _https(raw, "")
    if not url or kb_url_is_placeholder(url):
        if isinstance(raw, str) and raw.strip() and not url:
            return KeyResult("unknown", "The Knowledgebase Incidents address isn't an https address, "
                                        "so this key can't be tested.")
        return KeyResult("unknown", "Set the Knowledgebase Incidents address on the Dashboard tab, from the "
                                    "product's Specification tab, to test this key.")
    body = _call(fetch, url, {"x-apikey": keys["RDM_KB_API_KEY"], "Accept": "application/xml"}, service)
    if not body.lstrip().startswith(b"<"):
        return KeyResult("unknown", "kb_incidents_url answered, but not with incidents. "
                                    "Check it is the incidents product's URL.")
    return KeyResult("works", "It works: Knowledgebase Incidents answered.")


def _bods(keys: Mapping[str, str], config_data: Mapping, fetch: Fetch) -> KeyResult:
    service = _SERVICES["BODS_API_KEY"]
    params = {"boundingBox": ",".join(f"{v:.5f}" for v in _box(config_data)), "api_key": keys["BODS_API_KEY"]}
    url = f"{bods.DEFAULT_BASE_URL}/datafeed/?{urlencode(params)}"
    # BODS answers text/xml and refuses a request for application/xml (406).
    body = _call(fetch, url, {"Accept": "text/xml, application/xml;q=0.9, */*;q=0.1"}, service)
    if not body.lstrip().startswith(b"<"):
        return _unreadable(service)
    return KeyResult("works", "It works: the Bus Open Data Service sent bus positions.")


def _tapi(keys: Mapping[str, str], config_data: Mapping, fetch: Fetch) -> KeyResult:
    service = _SERVICES["TAPI_APP_KEY"]
    base = _https(_section(config_data, "bus").get("base_url"), transportapi.DEFAULT_BASE_URL)
    params = {"app_id": keys["TAPI_APP_ID"], "app_key": keys["TAPI_APP_KEY"], "live": "true", "group": "no",
              "limit": 1}
    url = f"{base}/uk/bus/stop_timetables/{_stop(config_data)}.json?{urlencode(params)}"
    answer = _json(_call(fetch, url, {}, service), service)
    if not isinstance(answer, dict):
        return _unreadable(service)
    if "error" in answer:  # how TransportAPI says no without an HTTP error
        if any(word in str(answer["error"]).lower() for word in _ALLOWANCE):
            return _allowance(service)
        return _wrong(service)
    return KeyResult("works", "It works: TransportAPI sent a stop's bus times. That used 1 of today's lookups.")


_TESTS = {"RDM_API_KEY": _darwin, "RDM_DETAILS_API_KEY": _details, "RDM_KB_API_KEY": _kb,
          "BODS_API_KEY": _bods, "TAPI_APP_ID": _tapi, "TAPI_APP_KEY": _tapi}


def _missing(name: str, keys: Mapping[str, str]) -> KeyResult | None:
    if name in COSTS_A_LOOKUP:
        for key, what in (("TAPI_APP_ID", "app ID"), ("TAPI_APP_KEY", "app key")):
            if not keys[key]:
                return KeyResult("missing", f"There's no TransportAPI {what} to test. "
                                            "Paste both: they're tested together.")
        return None
    if not keys[name]:
        return KeyResult("missing", "There's no key to test. Paste one, then test it.")
    return None


def test_key(name: str, value: str | None, config_data: Mapping, env: Mapping[str, str],
             fetch: Fetch = _fetch) -> KeyResult:
    """Whether key `name` works: `value` if given (pasted, not saved), else
    the one saved in `env`. TransportAPI's app ID and key are tested as a
    pair, the other one from `env`. `config_data` is the board's settings,
    read for the station, stop and addresses to ask about."""
    if name not in KEY_NAMES:
        raise ValueError("Not one of the board's keys")
    keys = {key: str(env.get(key, "")).strip() for key in KEY_NAMES}
    if value is not None and value.strip():
        keys[name] = value.strip()
    try:
        result = _missing(name, keys) or _TESTS[name](keys, config_data, fetch)
    except _Outcome as outcome:
        result = outcome.result
    except Exception as error:  # noqa: BLE001 - a fault, said plainly, never with its text
        log.warning("Testing %s failed: %s", name, type(error).__name__)
        result = KeyResult("unknown", "Something went wrong while testing the key. Try again.")
    message = result.message
    for secret in sorted((v for v in keys.values() if len(v) >= _MIN_REPLACED), key=len, reverse=True):
        message = message.replace(secret, "…")
    log.info("Tested %s: %s", name, result.state)
    return KeyResult(result.state, safe_message(message))


test_key.__test__ = False  # named like a test, but not one for pytest to collect where it is imported

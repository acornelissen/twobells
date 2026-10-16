"""The arrival board at one station, of trains from another: LDBWS's
GetArrBoardWithDetails, with the Live Departure Board's key and address.

The dashboard's Delay Repay watch (dashboard/repay.py) asks it, without a
Service Details key, when a train that has left your station is due at the
filter station: one ask covers every train from your station. It is read
with the departure board's checks (sources/darwin.py): a value of the wrong
type or shape is left out, never raised. Rail replacement buses are left
out: their times aren't for Delay Repay.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

from .darwin import (
    _CLOCK,
    _CRS,
    _SERVICE_ID,
    DEFAULT_BASE_URL,
    HttpGet,
    _as_list,
    _get_details,
    _matching,
    _point_lists,
    _time,
)

ROWS = 10  # trains from your station due there next; few are ever followed at once
_MAX_SERVICES = 50


class ArrivalsRefused(Exception):
    """The key isn't allowed to ask for arrival boards (HTTP 401 or 403)."""


@dataclass(frozen=True)
class Arrival:
    """A train on the arrival board."""

    service_id: str | None
    departs: str | None  # HH:MM, when it was booked to leave the station it is from
    sta: str  # HH:MM, booked to get here
    eta: str | None  # HH:MM or one of Darwin's words, like "On time"
    ata: str | None  # the same, if Darwin says it has got here
    cancelled: bool


def _arrival(raw, from_crs: str) -> Arrival | None:
    if not isinstance(raw, dict):
        return None
    sta = _matching(_CLOCK, raw.get("sta"))
    if sta is None:
        return None
    departs = None
    for points in _point_lists(raw.get("previousCallingPoints")):
        departs = next((p.scheduled for p in points if p.crs == from_crs), None)
        if departs is not None:
            break
    eta = _time(raw.get("eta"))
    return Arrival(
        service_id=_matching(_SERVICE_ID, raw.get("serviceID")),
        departs=departs,
        sta=sta,
        eta=eta,
        ata=_time(raw.get("ata")),
        cancelled=raw.get("isCancelled") is True or eta == "Cancelled",
    )


def parse_arrivals(board, from_crs: str) -> list[Arrival]:
    """The trains on an arrival board, in Darwin's order. Raises ValueError
    only if it isn't a board."""
    if not isinstance(board, dict):
        raise ValueError("Darwin sent something other than an arrival board")
    found = (_arrival(raw, from_crs) for raw in _as_list(board.get("trainServices"))[:_MAX_SERVICES])
    return [arrival for arrival in found if arrival is not None]


class ArrivalBoard:
    """Asks for the trains due at one station from another."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, http_get: HttpGet | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get or _get_details  # capped, as the departure board

    def arrivals(self, crs: str, from_crs: str) -> list[Arrival]:
        """The trains from `from_crs` due at `crs`. Raises ValueError for a
        code that isn't one, ArrivalsRefused, or whatever the request raised."""
        if _matching(_CRS, crs) is None or _matching(_CRS, from_crs) is None:
            raise ValueError("Not a CRS code")
        params = urlencode({"numRows": ROWS, "filterCrs": from_crs, "filterType": "from"})
        url = f"{self._base_url}/GetArrBoardWithDetails/{quote(crs, safe='')}?{params}"
        try:
            raw = self._http_get(url, {"x-apikey": self._api_key})
        except HTTPError as error:
            if error.code in (401, 403):
                raise ArrivalsRefused(f"The arrival board refused the key (HTTP {error.code})") from None
            raise
        return parse_arrivals(raw, from_crs)

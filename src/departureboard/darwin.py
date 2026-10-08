"""National Rail Darwin Live Departure Board (LDBWS) via the Rail Data Marketplace."""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urlencode

from .http import get_json
from .model import Departure, Status

DEFAULT_BASE_URL = "https://api1.raildata.org.uk/1010-live-departure-board-dep/LDBWS/api/20220120"

_HHMM = re.compile(r"^\d{2}:\d{2}$")

HttpGet = Callable[[str, dict], dict]


def _status_and_expected(service: dict) -> tuple[Status, str | None]:
    etd = (service.get("etd") or "").strip()
    if service.get("isCancelled") or etd == "Cancelled":
        return Status.CANCELLED, None
    if etd == "On time" or etd == service.get("std"):
        return Status.ON_TIME, None
    if etd == "Delayed":
        return Status.LATE, None
    if _HHMM.match(etd):
        return Status.LATE, etd
    return Status.UNKNOWN, None


def _parse_service(service: dict) -> Departure:
    status, expected = _status_and_expected(service)
    destinations = service.get("destination") or []
    return Departure(
        mode="train",
        service=service.get("operator") or "",
        destination=" & ".join(d.get("locationName", "") for d in destinations),
        scheduled=service["std"],
        expected=expected,
        status=status,
        platform=service.get("platform"),
    )


def parse_board(board: dict) -> list[Departure]:
    return [_parse_service(s) for s in board.get("trainServices") or []]


class DarwinSource:
    name = "train"

    def __init__(
        self,
        api_key: str,
        crs: str,
        filter_crs: str | None = None,
        rows: int = 3,
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGet = get_json,
    ) -> None:
        self._api_key = api_key
        self._crs = crs
        self._filter_crs = filter_crs
        self._rows = rows
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get

    def _url(self) -> str:
        params = {"numRows": self._rows}
        if self._filter_crs:
            params.update(filterCrs=self._filter_crs, filterType="to")
        return f"{self._base_url}/GetDepartureBoard/{self._crs}?{urlencode(params)}"

    def fetch(self) -> list[Departure]:
        board = self._http_get(self._url(), {"x-apikey": self._api_key})
        return parse_board(board)

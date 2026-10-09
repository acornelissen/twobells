"""Bus Open Data Service (BODS): live bus positions from the DfT, free with an API key.

Positions come as SIRI-VM XML, refreshed by BODS every 10 seconds. Only the
area around the configured route strips is asked for, so responses stay small.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode

from . import safexml
from .http import get_bytes

DEFAULT_BASE_URL = "https://data.bus-data.dft.gov.uk/api/v1"
# A busy town's worth of vehicles is well under this; anything bigger is refused.
MAX_RESPONSE_BYTES = 2_000_000
# BODS keeps sending a vehicle's last position long after it stops reporting
# (parked overnight, tracker off), so anything older than this is ignored.
MAX_AGE = timedelta(minutes=5)

_NS = {"siri": "http://www.siri.org.uk/siri"}
_API_KEY = re.compile(r"\bapi_key=[^&\s\"']*")

HttpGetBytes = Callable[[str, int], bytes]


def redact_key(text: str) -> str:
    return _API_KEY.sub("api_key=REDACTED", text)


@dataclass(frozen=True)
class Vehicle:
    line: str  # as on the front of the bus, e.g. "341"
    destination: str
    direction: str
    lat: float
    lon: float
    recorded_at: str  # ISO 8601, as sent
    bearing: float | None = None
    vehicle_ref: str = ""


def _text(node, path: str) -> str:
    found = node.find(path, _NS)
    return (found.text or "").strip() if found is not None else ""


def parse_siri_vm(body: bytes) -> list[Vehicle]:
    """Vehicles from a SIRI-VM document. Activities missing a position are skipped."""
    root = safexml.fromstring(body)  # refuses a DOCTYPE, so no entity tricks
    vehicles = []
    for activity in root.iterfind(".//siri:VehicleActivity", _NS):
        journey = activity.find("siri:MonitoredVehicleJourney", _NS)
        if journey is None:
            continue
        try:
            lat = float(_text(journey, "siri:VehicleLocation/siri:Latitude"))
            lon = float(_text(journey, "siri:VehicleLocation/siri:Longitude"))
        except ValueError:
            continue
        bearing = _text(journey, "siri:Bearing")
        vehicles.append(
            Vehicle(
                line=_text(journey, "siri:PublishedLineName") or _text(journey, "siri:LineRef"),
                destination=_text(journey, "siri:DestinationName"),
                direction=_text(journey, "siri:DirectionRef").lower(),
                lat=lat,
                lon=lon,
                recorded_at=_text(activity, "siri:RecordedAtTime"),
                bearing=float(bearing) if bearing else None,
                vehicle_ref=_text(journey, "siri:VehicleRef"),
            )
        )
    return vehicles


class BodsVehicleSource:
    """Positions of the given lines inside a bounding box (west, south, east, north)."""

    name = "vehicles"

    def __init__(
        self,
        api_key: str,
        box: tuple[float, float, float, float],
        lines: set[str],
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGetBytes = get_bytes,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._api_key = api_key
        self._box = box
        self._lines = set(lines)
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get
        self._now = now

    def _url(self) -> str:
        params = {
            "boundingBox": ",".join(f"{v:.5f}" for v in self._box),
            "api_key": self._api_key,
        }
        return f"{self._base_url}/datafeed/?{urlencode(params)}"

    def fetch(self) -> list[Vehicle]:
        try:
            body = self._http_get(self._url(), MAX_RESPONSE_BYTES)
        except Exception as error:  # noqa: BLE001 - re-raised without the key
            raise RuntimeError(redact_key(str(error))) from None
        now = self._now()
        return [v for v in parse_siri_vm(body) if v.line in self._lines and _fresh(v, now)]


def _fresh(vehicle: Vehicle, now: datetime) -> bool:
    try:
        recorded = datetime.fromisoformat(vehicle.recorded_at)
    except ValueError:
        return False
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return now - recorded <= MAX_AGE

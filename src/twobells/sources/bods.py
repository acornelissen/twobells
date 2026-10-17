"""Bus Open Data Service (BODS): live bus positions from the DfT, free with an API key.

Positions come as SIRI-VM XML, refreshed by BODS every 10 seconds. Only the
area around the configured route strips is asked for, so responses stay small.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from ..core import safexml
from ..core.net import get_bytes

DEFAULT_BASE_URL = "https://data.bus-data.dft.gov.uk/api/v1"
# A busy town's worth of vehicles is well under this; anything bigger is refused.
MAX_RESPONSE_BYTES = 2_000_000
# BODS keeps sending a vehicle's last position long after it stops reporting
# (parked overnight, tracker off), so anything older than this is ignored.
MAX_AGE = timedelta(minutes=10)

_NS = {"siri": "http://www.siri.org.uk/siri"}
_API_KEY = re.compile(r"\bapi_key=[^&\s\"']*")
# VehicleRef is the operator's own name for a bus. Around Glossop it comes as a
# fleet number ("3017"), a registration ("CD25EFG", "CD25_EFG"), or both
# ("3017_-_AB25_CDE"). An older plate is read only with its space ("R123 ABC",
# "ABC 123D", "X 1"); anything else gives neither.
_MAX_VEHICLE_REF = 32
_FLEET_NUMBER = re.compile(r"[0-9]{1,6}")
_FLEET_AND_REG = re.compile(r"([0-9]{1,6})_-_(.+)")
_CURRENT_REG = re.compile(r"([A-Z]{2}[0-9]{2})[ _]?([A-Z]{3})")  # since 2001: "AB25 CDE"
_OTHER_REG = re.compile(r"[A-Z0-9]{1,4}[ _][A-Z0-9]{1,4}")  # older ones, spaced: "R123 ABC", "ABC 123D"

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
    # Which timetabled journey it says it is running, for livebus to match.
    operator: str = ""  # national operator code, e.g. "BNML"
    origin_ref: str = ""  # ATCO code of the journey's first stop
    origin_aimed: datetime | None = None  # timetabled departure from there, with its offset
    journey_ref: str = ""  # DatedVehicleJourneyRef, the ticket machine's journey code
    block: str = ""
    origin_name: str = ""  # OriginName, as sent: often with underscores for spaces
    # From vehicle_ref, checked (fleet_and_registration); "" when it doesn't say.
    fleet_number: str = ""  # "3017"
    registration: str = ""  # "AB25 CDE", spaced as on the number plate


def _text(node, path: str) -> str:
    found = node.find(path, _NS)
    return (found.text or "").strip() if found is not None else ""


def _aware_time(text: str) -> datetime | None:
    """An ISO 8601 time with its UTC offset; None if missing, unreadable or without
    an offset, since a bare time could be either UTC or local."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _bearing(text: str) -> float | None:
    """Degrees; None if missing or unreadable, so one bad bearing costs only itself."""
    try:
        bearing = float(text)
    except ValueError:
        return None
    return bearing if math.isfinite(bearing) else None


def _degrees(text: str, limit: float) -> float | None:
    """A latitude (limit 90) or longitude (limit 180); None if missing,
    unreadable, not finite ("nan", "inf") or off the map."""
    try:
        degrees = float(text)
    except ValueError:
        return None
    return degrees if math.isfinite(degrees) and -limit <= degrees <= limit else None


def _registration(text: str) -> str:
    """A number plate as written on one ("AB25 CDE"), or "" if text isn't one.
    Unspaced, only the current style is read: older ones can't be split safely.
    Plain ASCII only: upper() turns some letters into others ("\ufb00" to "FF")."""
    if not text.isascii():
        return ""
    text = text.upper()
    if found := _CURRENT_REG.fullmatch(text):
        return f"{found[1]} {found[2]}"
    if _OTHER_REG.fullmatch(text) and any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
        return text.replace("_", " ")
    return ""


def fleet_and_registration(vehicle_ref: str) -> tuple[str, str]:
    """The fleet number and the registration a VehicleRef gives, each "" when
    it doesn't give one: "3017_-_AB25_CDE" is ("3017", "AB25 CDE"), "CD25EFG"
    ("", "CD25 EFG"), "10234" ("10234", ""). Anything long or odd gives neither."""
    if len(vehicle_ref) > _MAX_VEHICLE_REF:
        return "", ""
    if _FLEET_NUMBER.fullmatch(vehicle_ref):
        return vehicle_ref, ""
    if found := _FLEET_AND_REG.fullmatch(vehicle_ref):
        return found[1], _registration(found[2])
    return "", _registration(vehicle_ref)


def parse_siri_vm(body: bytes) -> list[Vehicle]:
    """Vehicles from a SIRI-VM document. Activities missing a position, or
    with one that isn't on the map, are skipped."""
    root = safexml.fromstring(body)  # refuses a DOCTYPE, so no entity tricks
    vehicles = []
    for activity in root.iterfind(".//siri:VehicleActivity", _NS):
        journey = activity.find("siri:MonitoredVehicleJourney", _NS)
        if journey is None:
            continue
        lat = _degrees(_text(journey, "siri:VehicleLocation/siri:Latitude"), 90)
        lon = _degrees(_text(journey, "siri:VehicleLocation/siri:Longitude"), 180)
        if lat is None or lon is None:
            continue
        vehicle_ref = _text(journey, "siri:VehicleRef")
        fleet_number, registration = fleet_and_registration(vehicle_ref)
        vehicles.append(
            Vehicle(
                line=_text(journey, "siri:PublishedLineName") or _text(journey, "siri:LineRef"),
                destination=_text(journey, "siri:DestinationName"),
                direction=_text(journey, "siri:DirectionRef").lower(),
                lat=lat,
                lon=lon,
                recorded_at=_text(activity, "siri:RecordedAtTime"),
                bearing=_bearing(_text(journey, "siri:Bearing")),
                vehicle_ref=vehicle_ref,
                operator=_text(journey, "siri:OperatorRef"),
                origin_ref=_text(journey, "siri:OriginRef"),
                origin_aimed=_aware_time(_text(journey, "siri:OriginAimedDepartureTime")),
                journey_ref=_text(journey, "siri:FramedVehicleJourneyRef/siri:DatedVehicleJourneyRef"),
                block=_text(journey, "siri:BlockRef"),
                origin_name=_text(journey, "siri:OriginName"),
                fleet_number=fleet_number,
                registration=registration,
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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api_key = api_key
        self._box = box
        self._lines = {_line_key(line) for line in lines}
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
        return [v for v in parse_siri_vm(body) if _line_key(v.line) in self._lines and _fresh(v, now)]


def _line_key(line: str) -> str:
    """A line as compared: "X1" and "x1" are one line, as livebus.match has them."""
    return line.strip().casefold()


def _fresh(vehicle: Vehicle, now: datetime) -> bool:
    try:
        recorded = datetime.fromisoformat(vehicle.recorded_at)
    except ValueError:
        return False
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=UTC)
    return now - recorded <= MAX_AGE

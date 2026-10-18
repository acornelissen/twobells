"""Made-up mornings at busy places, for the README's pictures.

    from doc_scenes import SCENES

Each scene is a Thursday morning at 07:31 at a real place, with its real
station, stop, line and operator names and invented times: the same
departures for the dashboard (tools/serve_doc_dashboard.py --scene) and the
board (tools/build_doc_images.py). Nothing in them is anyone's home: the
walks are from somewhere near the station.

- piccadilly: Manchester Piccadilly's main-line trains (Avanti West Coast,
  TransPennine Express, Northern, CrossCountry), Metrolink trams from
  Piccadilly, Bee Network buses from Piccadilly Gardens.
- bank: London's Underground (Central, Northern, Waterloo & City) and DLR at
  Bank, and the District line at Monument, with a line's status and a lift
  out of service; London buses from Bank and Monument.
- greenwich: Southeastern and Thameslink trains from Greenwich, the DLR from
  Cutty Sark, Thames Clippers river buses from Greenwich Pier with the cable
  car closed by the wind, and London buses from the town centre.

The trains' rows are made by the dashboard's own code (dashboard/snapshot.py),
from Darwin's shapes, so their words and announcements are the app's; so are
the trams' and London's. The buses' rows are written here, as the live bus
rows would come.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from twobells.core import calling
from twobells.core.model import Departure, Status, Strip, iso, shift, strip_key
from twobells.core.walk import WalkTimes
from twobells.dashboard import snapshot as snap
from twobells.sources.darwin import CallingPoint, Message, TrainBoard, TrainService
from twobells.sources.tfl import Arrival, label_for
from twobells.sources.tfl import Message as TflMessage
from twobells.sources.tfl import departure as tfl_departure
from twobells.sources.trams import departure as tram_departure
from twobells.timetable.route import where
from twobells.timetable.trams import TramCall

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


@dataclass(frozen=True)
class Train:
    time: str
    operator: str
    destination: str
    platform: str | None
    calling: tuple[tuple[str, int], ...]  # each stop after this station, and the minutes it takes to get there
    coaches: int
    expected: str | None = None  # late: when it is expected
    delay: str | None = None  # Darwin's delay reason
    cancelled: str | None = None  # Darwin's cancel reason: it is cancelled
    via: str | None = None
    platform_changed: bool = False


@dataclass(frozen=True)
class Bus:
    time: str
    line: str
    destination: str
    stop: str  # the stop's label
    expected: str | None = None  # live: when it is expected; None: timetable only
    route: tuple[str, ...] = ()  # the stops before yours, yours, and the next, for its strip
    at: float | None = None  # where the bus is along `route`, in stops; None: not on it yet


@dataclass(frozen=True)
class Tram:
    time: str
    line: str
    destination: str
    stop: str


@dataclass(frozen=True)
class Metro:
    """A London train, river bus or cable car, live from TfL."""

    seconds: int  # after the scene's time
    line: str  # TfL's name for the line: "Central", "DLR", "RB1"
    line_id: str
    mode: str  # tube, dlr, river-bus or cable-car
    destination: str
    stop: str
    platform: str | None = None
    bound: str | None = None
    where: str | None = None


@dataclass(frozen=True)
class BusStop:
    name: str
    letter: str
    walk: int


@dataclass(frozen=True)
class MetroStop:
    code: str
    name: str
    walk: int


@dataclass(frozen=True)
class Scene:
    name: str
    title: str
    now: datetime
    station: str | None = None
    crs: str | None = None
    train_walk: int | None = None
    trains: tuple[Train, ...] = ()
    train_messages: tuple[Message, ...] = ()
    bus_stops: dict[str, BusStop] | None = None
    buses: tuple[Bus, ...] = ()
    metro_stops: dict[str, MetroStop] | None = None
    trams: tuple[Tram, ...] = ()  # Metrolink's, from the timetable
    london: tuple[Metro, ...] = ()  # TfL's, live
    metro_messages: tuple[TflMessage, ...] = ()

    def walk(self) -> WalkTimes:
        return WalkTimes(train=self.train_walk,
                         bus={label: stop.walk for label, stop in (self.bus_stops or {}).items()},
                         metro={label: stop.walk for label, stop in (self.metro_stops or {}).items()})

    def modes(self) -> list[str]:
        return [mode for mode, found in (("train", self.trains), ("bus", self.buses),
                                         ("metro", self.trams or self.london)) if found]


# A Thursday morning in October; the scenes' times are invented.
MORNING = datetime.fromisoformat("2026-10-15T07:31:30+01:00")


def _stops(start: int, step: int, *names: str) -> tuple[tuple[str, int], ...]:
    """Stops `step` minutes apart, the first `start` minutes out."""
    return tuple((name, start + step * i) for i, name in enumerate(names))


_TO_EUSTON_VIA_STOKE = (("Stockport", 8), ("Macclesfield", 21), ("Stoke-on-Trent", 39),
                        ("Milton Keynes Central", 102), ("London Euston", 128))
_TO_EUSTON_VIA_CREWE = (("Stockport", 8), ("Wilmslow", 18), ("Crewe", 33), ("London Euston", 121))
_TO_BUXTON = _stops(5, 3, "Levenshulme", "Heaton Chapel", "Stockport", "Davenport", "Woodsmoor", "Hazel Grove",
                    "Middlewood", "Disley", "New Mills Newtown", "Furness Vale", "Whaley Bridge",
                    "Chapel-en-le-Frith", "Dove Holes", "Buxton")
_TO_HADFIELD = _stops(4, 3, "Ashburys", "Gorton", "Guide Bridge", "Flowery Field", "Newton for Hyde", "Godley",
                      "Hattersley", "Broadbottom", "Dinting", "Glossop", "Hadfield")

PICCADILLY = Scene(
    name="piccadilly",
    title="Manchester Piccadilly",
    now=MORNING,
    station="Manchester Piccadilly",
    crs="MAN",
    train_walk=6,
    trains=(
        Train("07:38", "TransPennine Express", "Manchester Airport", "14", (("Manchester Airport", 15),), 5),
        Train("07:41", "Northern", "Liverpool Lime Street", "13",
              _stops(3, 4, "Manchester Oxford Road", "Deansgate", "Urmston", "Irlam", "Birchwood",
                     "Warrington Central", "Widnes", "Liverpool South Parkway", "Liverpool Lime Street"),
              4, expected="07:45", via="via Warrington Central",
              delay="This train has been delayed by a late running train being in front of this one"),
        Train("07:45", "Avanti West Coast", "London Euston", "6", _TO_EUSTON_VIA_STOKE, 11, via="via Stoke-on-Trent"),
        Train("07:49", "Northern", "Buxton", "1", _TO_BUXTON, 2,
              cancelled="This train has been cancelled because of a shortage of train crew"),
        Train("07:52", "CrossCountry", "Bournemouth", "9",
              (("Stockport", 8), ("Macclesfield", 20), ("Stoke-on-Trent", 37), ("Stafford", 54),
               ("Wolverhampton", 70), ("Birmingham New Street", 88), ("Leamington Spa", 115), ("Banbury", 132),
               ("Oxford", 150), ("Reading", 178), ("Basingstoke", 196), ("Winchester", 213),
               ("Southampton Airport Parkway", 222), ("Southampton Central", 230), ("Brockenhurst", 248),
               ("Bournemouth", 268)),
              5, platform_changed=True),
        Train("07:56", "TransPennine Express", "Newcastle", "2",
              (("Stalybridge", 12), ("Huddersfield", 30), ("Leeds", 52), ("York", 78), ("Northallerton", 100),
               ("Darlington", 112), ("Durham", 130), ("Newcastle", 145)),
              5, expected="08:01", delay="This train has been delayed by a fault with the signalling system"),
        Train("08:01", "Northern", "Hadfield", "1", _TO_HADFIELD, 3),
        Train("08:05", "Avanti West Coast", "London Euston", "5", _TO_EUSTON_VIA_CREWE, 9, via="via Crewe"),
        Train("08:08", "Northern", "Crewe", "14", (("Manchester Airport", 16), ("Wilmslow", 26), ("Crewe", 48)), 4,
              via="via Manchester Airport"),
        Train("08:11", "CrossCountry", "Paignton", "6",
              (("Stockport", 8), ("Macclesfield", 20), ("Stoke-on-Trent", 37), ("Stafford", 54),
               ("Wolverhampton", 70), ("Birmingham New Street", 88), ("Cheltenham Spa", 128),
               ("Bristol Parkway", 160), ("Bristol Temple Meads", 172), ("Taunton", 205),
               ("Exeter St Davids", 235), ("Newton Abbot", 256), ("Torquay", 270), ("Paignton", 276)),
              5),
    ),
    train_messages=(
        Message("Disruption between Manchester Piccadilly and Buxton until 10:00. Some trains may be cancelled.",
                "train", "minor", "service"),
        Message("The lift between the concourse and platforms 13 and 14 is out of order. Please ask staff if you "
                "need help.", "station", None, "station"),
    ),
    bus_stops={"D": BusStop("Piccadilly Gardens", "D", 4), "H": BusStop("Piccadilly Gardens", "H", 5)},
    buses=(
        Bus("07:37", "192", "Hazel Grove", "D", expected="07:38"),
        Bus("07:39", "50", "MediaCityUK", "H", expected="07:39",
            route=("Fairfield Street", "Piccadilly Station", "Newton Street", "Piccadilly Gardens", "Mosley Street"),
            at=1.4),
        Bus("07:42", "216", "Ashton-under-Lyne", "D"),
        Bus("07:44", "86", "Chorlton", "H", expected="07:47"),
        Bus("07:46", "17", "Rochdale", "D", expected="07:46"),
        Bus("07:50", "V1", "Leigh", "H"),
        Bus("07:52", "192", "Hazel Grove", "D", expected="07:52"),
        Bus("07:55", "59", "Oldham", "D"),
    ),
    metro_stops={"T": MetroStop("9400ZZMAPIC", "Piccadilly", 5)},
    trams=(
        Tram("07:37", "Blue Line", "Eccles", "T"),
        Tram("07:39", "Yellow Line", "Bury", "T"),
        Tram("07:42", "Purple Line", "Altrincham", "T"),
        Tram("07:44", "Blue Line", "Ashton-under-Lyne", "T"),
        Tram("07:47", "Blue Line", "Eccles", "T"),
        Tram("07:49", "Yellow Line", "Bury", "T"),
        Tram("07:52", "Purple Line", "Altrincham", "T"),
        Tram("07:54", "Blue Line", "Ashton-under-Lyne", "T"),
    ),
)

_PAST_BANK = ("Liverpool Street Station", "Old Broad Street", "Threadneedle Street", "Bank Station",
              "Mansion House Station")
_PAST_MONUMENT = ("Moorgate Station", "Princes Street", "King William Street", "Monument Station", "London Bridge")

BANK = Scene(
    name="bank",
    title="Bank",
    now=MORNING,
    bus_stops={"K": BusStop("Bank Station", "K", 3), "M": BusStop("Monument Station", "M", 5)},
    buses=(
        Bus("07:39", "21", "Lewisham", "M", expected="07:40", route=_PAST_MONUMENT, at=0.6),
        Bus("07:41", "11", "Fulham Town Hall", "K", expected="07:41", route=_PAST_BANK, at=0.1),
        Bus("07:43", "26", "Waterloo", "K", expected="07:45", route=_PAST_BANK, at=None),
        Bus("07:46", "133", "Streatham Station", "M", expected="07:46", route=_PAST_MONUMENT, at=None),
        Bus("07:48", "25", "City Thameslink", "K", expected="07:49", route=_PAST_BANK, at=None),
        Bus("07:51", "43", "London Bridge", "M", expected="07:51", route=_PAST_MONUMENT, at=None),
        Bus("07:54", "8", "Tottenham Court Road", "K"),
        Bus("07:57", "141", "London Bridge", "M"),
    ),
    metro_stops={"T": MetroStop("940GZZLUBNK", "Bank", 3), "U": MetroStop("940GZZDLBNK", "Bank DLR", 5),
                 "M": MetroStop("940GZZLUMMT", "Monument", 4)},
    london=(
        Metro(240, "Northern", "northern", "tube", "Edgware", "T", "4", "Northbound",
              "Between Borough and London Bridge"),
        Metro(300, "Waterloo & City", "waterloo-city", "tube", "Waterloo", "T", "8", "Westbound", "At Platform"),
        Metro(360, "Central", "central", "tube", "Epping", "T", "6", "Eastbound", "At St. Paul's"),
        Metro(420, "DLR", "dlr", "dlr", "Lewisham", "U", "10", None, "At Platform"),
        Metro(480, "District", "district", "tube", "Upminster", "M", "2", "Eastbound", "At Cannon Street"),
        Metro(540, "Central", "central", "tube", "West Ruislip", "T", "5", "Westbound", "Left Liverpool Street"),
        Metro(600, "Northern", "northern", "tube", "Morden", "T", "3", "Southbound", "At Moorgate"),
        Metro(660, "DLR", "dlr", "dlr", "Woolwich Arsenal", "U", "9", None, "Approaching Bank"),
        Metro(720, "Waterloo & City", "waterloo-city", "tube", "Waterloo", "T", "8", "Westbound", "At Waterloo"),
        Metro(780, "Northern", "northern", "tube", "High Barnet", "T", "4", "Northbound", "At London Bridge"),
    ),
    metro_messages=(
        TflMessage("Central line: Minor delays between White City and Liverpool Street while we fix a signal "
                   "failure. Tickets are valid on local buses.", "minor", "service"),
        TflMessage("Bank: One of the lifts between the street and the Northern line is out of service until 12:00. "
                   "Ask staff for step-free help.", "normal", "station"),
    ),
)

_TO_CANNON_STREET = (("Deptford", 2), ("London Bridge", 9), ("London Cannon Street", 13))
_TO_DARTFORD = _stops(2, 3, "Maze Hill", "Westcombe Park", "Charlton", "Woolwich Dockyard", "Woolwich Arsenal",
                      "Plumstead", "Abbey Wood", "Belvedere", "Erith", "Slade Green", "Dartford")
_TO_LUTON = (("Deptford", 2), ("London Bridge", 9), ("London Blackfriars", 14), ("City Thameslink", 16),
             ("Farringdon", 19), ("St Pancras International", 23), ("Kentish Town", 28),
             ("West Hampstead Thameslink", 32), ("Cricklewood", 35), ("Hendon", 38), ("Mill Hill Broadway", 42),
             ("Elstree & Borehamwood", 47), ("Radlett", 51), ("St Albans City", 57), ("Harpenden", 62),
             ("Luton Airport Parkway", 68), ("Luton", 72))
_TO_RAINHAM = _stops(2, 3, "Maze Hill", "Westcombe Park", "Charlton", "Woolwich Arsenal", "Abbey Wood", "Belvedere",
                     "Erith", "Slade Green", "Dartford", "Greenhithe for Bluewater", "Gravesend", "Strood",
                     "Rochester", "Chatham", "Gillingham (Kent)", "Rainham (Kent)")
_PAST_CHURCH_STREET = ("Maze Hill Station", "Old Royal Naval College", "Greenwich Market", "Greenwich Church Street",
                       "Creek Road")
_PAST_CUTTY_SARK = ("Greenwich Station", "Stockwell Street", "Greenwich Town Centre", "Cutty Sark", "Creek Road")

GREENWICH = Scene(
    name="greenwich",
    title="Greenwich",
    now=MORNING,
    station="Greenwich",
    crs="GNW",
    train_walk=7,
    trains=(
        Train("07:40", "Southeastern", "London Cannon Street", "1", _TO_CANNON_STREET, 10),
        Train("07:43", "Thameslink", "Luton", "1", _TO_LUTON, 8, expected="07:47",
              delay="This train has been delayed by a late running train being in front of this one"),
        Train("07:46", "Southeastern", "Dartford", "2", _TO_DARTFORD, 8, via="via Woolwich Arsenal"),
        Train("07:49", "Southeastern", "London Cannon Street", "1", _TO_CANNON_STREET, 10,
              cancelled="This train has been cancelled because of a fault on this train"),
        Train("07:52", "Thameslink", "Rainham (Kent)", "2", _TO_RAINHAM, 8),
        Train("07:55", "Southeastern", "London Cannon Street", "1", _TO_CANNON_STREET, 12),
        Train("07:58", "Southeastern", "Dartford", "2", _TO_DARTFORD, 8, via="via Woolwich Arsenal"),
        Train("08:06", "Southeastern", "London Cannon Street", "1", _TO_CANNON_STREET, 10),
    ),
    bus_stops={"F": BusStop("Greenwich Church Street", "F", 3), "N": BusStop("Cutty Sark", "N", 4)},
    buses=(
        Bus("07:36", "188", "Russell Square", "F", expected="07:37", route=_PAST_CHURCH_STREET, at=1.3),
        Bus("07:39", "199", "Canada Water", "N", expected="07:39", route=_PAST_CUTTY_SARK, at=0.5),
        Bus("07:42", "177", "Peckham", "F", expected="07:43", route=_PAST_CHURCH_STREET, at=0.2),
        Bus("07:44", "129", "Lewisham", "N", expected="07:44", route=_PAST_CUTTY_SARK, at=None),
        Bus("07:48", "286", "Sidcup", "F"),
        Bus("07:51", "188", "Russell Square", "F"),
    ),
    metro_stops={"C": MetroStop("940GZZDLCUT", "Cutty Sark (for Maritime Greenwich)", 4),
                 "P": MetroStop("930GGNW", "Greenwich Pier", 5),
                 "G": MetroStop("940GZZALGWP", "Greenwich Peninsula", 32)},
    london=(
        Metro(330, "RB1", "rb1", "river-bus", "Battersea Power Station Pier", "P"),
        Metro(420, "DLR", "dlr", "dlr", "Bank", "C", "2", None, "Approaching Cutty Sark"),
        Metro(540, "DLR", "dlr", "dlr", "Lewisham", "C", "1", None, "At Island Gardens"),
        Metro(660, "DLR", "dlr", "dlr", "Stratford", "C", "2", None, "At Deptford Bridge"),
        Metro(780, "DLR", "dlr", "dlr", "Lewisham", "C", "1", None, "At Mudchute"),
        Metro(930, "RB1", "rb1", "river-bus", "Barking Riverside Pier", "P"),
        Metro(1020, "DLR", "dlr", "dlr", "Bank", "C", "2", None, "At Lewisham"),
    ),
    metro_messages=(
        TflMessage("IFS Cloud Cable Car: Suspended because of high winds. Use the Jubilee line between North "
                   "Greenwich and Canning Town.", "severe", "service"),
    ),
)

SCENES = {scene.name: scene for scene in (PICCADILLY, BANK, GREENWICH)}


# The board's departures.


def _train_departure(train: Train) -> Departure:
    if train.cancelled:
        status = Status.CANCELLED
    elif train.expected:
        status = Status.LATE
    else:
        status = Status.ON_TIME
    line = None if train.cancelled else calling.line([name for name, _ in train.calling], (), train.coaches)
    return Departure("train", train.operator, train.destination, train.time, train.expected, status, train.platform,
                     via=train.via, platform_changed=train.platform_changed, calling_at=line)


def _bus_departure(bus: Bus) -> Departure:
    if bus.expected is None:
        status = Status.UNKNOWN
    elif bus.expected != bus.time:
        status = Status.LATE
    else:
        status = Status.ON_TIME
    expected = bus.expected if status is Status.LATE else None
    return Departure("bus", bus.line, bus.destination, bus.time, expected, status, stop=bus.stop)


def _arrival(metro: Metro, now: datetime) -> Arrival:
    return Arrival(stop=metro.stop, line_id=metro.line_id, line=metro.line, mode=metro.mode,
                   destination=metro.destination, expected=now + timedelta(seconds=metro.seconds),
                   platform=metro.platform, bound=metro.bound, where=metro.where)


def _tram_call(tram: Tram) -> TramCall:
    hours, minutes = tram.time.split(":")
    return TramCall(stop=tram.stop, minute=int(hours) * 60 + int(minutes), line=tram.line,
                    destination=tram.destination, service=f"{tram.line}|{tram.time}")


def _at(scene: Scene, hhmm: str) -> datetime:
    hours, minutes = hhmm.split(":")
    return scene.now.replace(hour=int(hours), minute=int(minutes), second=0)


def _where(bus: Bus) -> str:
    """Where the bus is, in the words of its strip: "2 stops away"."""
    return where(bus.route[:-1], bus.at, False)


def board_departures(scene: Scene) -> dict[str, list[Departure]]:
    """The scene's departures by mode, as the board's sources give them."""
    found = {
        "train": [_train_departure(t) for t in scene.trains],
        "bus": [_bus_departure(b) for b in scene.buses],
        "metro": ([tram_departure(_at(scene, t.time), _tram_call(t)) for t in scene.trams]
                  + [tfl_departure(_arrival(m, scene.now), scene.now.tzinfo) for m in scene.london]),
    }
    return {mode: found[mode] for mode in scene.modes()}


def board_strips(scene: Scene) -> dict[tuple[str, str, str, str], Strip]:
    """Where each live bus is, for a screen's rows."""
    strips = {}
    for bus in scene.buses:
        if bus.expected is not None and bus.route:
            strips[strip_key(_bus_departure(bus))] = Strip(_where(bus), len(bus.route), len(bus.route) - 2, bus.at)
    return strips


def board_labels(scene: Scene) -> dict[str, tuple[str, str]]:
    """London's metro section's heading, as its source gives it."""
    if not scene.london:
        return {}
    label = label_for([stop.code for stop in (scene.metro_stops or {}).values()])
    return {"metro": (label, "Nothing due")}


def board_notices(scene: Scene) -> list[str]:
    """The disruption the board shows: the trains' and the lines'."""
    return ([m.text for m in scene.train_messages if m.kind == "service"]
            + [m.text for m in scene.metro_messages if m.kind == "service"])


# The dashboard's snapshot.


def _service(scene: Scene, train: Train) -> TrainService:
    points = tuple(CallingPoint(name, None, shift(train.time, minutes),
                                shift(train.expected, minutes) if train.expected else None, None, False, None)
                   for name, minutes in train.calling)
    return TrainService(departure=_train_departure(train), service_id=None, origin=scene.station or "",
                        operator_code="", length=train.coaches, cancel_reason=train.cancelled,
                        delay_reason=train.delay if train.expected else None, calling_points=points)


def _bus_row(scene: Scene, bus: Bus, walk: WalkTimes) -> dict:
    dep = _bus_departure(bus)
    stop = (scene.bus_stops or {})[bus.stop]
    scheduled_at = _at(scene, bus.time)
    live = bus.expected is not None
    expected_at = _at(scene, bus.expected) if bus.expected else None
    strip = None
    if bus.route:
        yours = len(bus.route) - 2
        strip = {"stops": list(bus.route), "yours": yours, "bus_at": bus.at if live else None,
                 "where": _where(bus) if live else "No live position"}
    seen = scene.now - timedelta(seconds=20)
    soon = scheduled_at - scene.now <= timedelta(minutes=5)
    return {
        "id": snap.row_id("bus", f"{scene.name}|{bus.stop}|{bus.line}|{bus.time}"),
        "line": bus.line,
        "destination": bus.destination,
        "stop": bus.stop,
        "stop_name": f"{stop.name} (Stop {stop.letter})",
        "scheduled": bus.time,
        "scheduled_at": iso(scheduled_at),
        "expected": bus.expected if live else None,
        "expected_at": iso(expected_at),
        "status": dep.status.value,
        "status_text": snap.status_text(dep),
        "status_spoken": snap.status_spoken(dep),
        "minutes_late": dep.minutes_late if dep.status is Status.LATE else None,
        "live": live,
        "walk_min": walk.minutes(dep),
        "leave_at": iso(snap.leave_at(dep, scheduled_at, expected_at, walk)),
        "vehicle": ({"age_s": 20, "recorded_at": iso(seen), "bearing": None, "fleet_number": None,
                     "registration": None} if live else None),
        "strip": strip,
        "history": {"phrase": None, "no_show_phrase": None},
        "arrives": None,
        "inbound": None,
        "ghost": not live and soon,
    }


def _status(scene: Scene, seconds: int = 30) -> dict:
    return snap.status_block(scene.now - timedelta(seconds=seconds))


def _trains_part(scene: Scene, walk: WalkTimes) -> dict | None:
    if not scene.trains:
        return None
    board = TrainBoard([_service(scene, t) for t in scene.trains], list(scene.train_messages), None, scene.station)
    part, _ = snap.trains_part(board, scene.now, _status(scene), scene.station or "", None, walk, scene.crs or "")
    return part


def _buses_part(scene: Scene, walk: WalkTimes) -> dict | None:
    if not scene.buses:
        return None
    built = scene.now.replace(hour=3, minute=0, second=0)
    return {
        "status": _status(scene),
        "positions": _status(scene, 20),
        "timetable": {"state": "ready", "built": iso(built), "valid_until": "2027-01-31", "journeys": 1240},
        "stops": {label: {"name": stop.name, "letter": stop.letter, "towards": None, "walk_min": stop.walk}
                  for label, stop in (scene.bus_stops or {}).items()},
        "rows": [_bus_row(scene, bus, walk) for bus in scene.buses],
        "last_buses": [],
    }


def _metro_part(scene: Scene, walk: WalkTimes) -> dict | None:
    stops = {label: stop.code for label, stop in (scene.metro_stops or {}).items()}
    names = {stop.code: stop.name for stop in (scene.metro_stops or {}).values()}
    if scene.trams:
        built = scene.now.replace(hour=3, minute=0, second=0)
        leaving = [(_at(scene, t.time), _tram_call(t)) for t in scene.trams]
        return snap.metro_part(leaving, _status(scene), snap.tram_timetable_block("ready", built), stops, names, walk)
    if scene.london:
        arrivals = [_arrival(m, scene.now) for m in scene.london]
        return snap.tfl_part(arrivals, _status(scene), scene.metro_messages, label_for(list(stops.values())), stops,
                             names, walk, scene.now.tzinfo)
    return None


def scene_snapshot(scene: Scene) -> dict:
    """The dashboard's snapshot of the scene: its departures, with the sample
    morning's weather moved to its day, and no map (the sample's map is
    Glossop's)."""
    base = json.loads((FIXTURES / "dashboard_snapshot.json").read_text(encoding="utf-8"))
    sample_day = base["generated_at"][:10]
    weather = json.loads(json.dumps(base["weather"]).replace(sample_day, scene.now.date().isoformat()))
    walk = scene.walk()
    return {
        **base,
        "generated_at": iso(scene.now),
        "title": scene.title,
        "walk": {"train": walk.train, "bus": dict(walk.bus), "metro": dict(walk.metro)},
        "show": [item for item in base["show"] if item != "area_map"],
        "leave_modes": scene.modes(),
        "trains": _trains_part(scene, walk),
        "buses": _buses_part(scene, walk),
        "metro": _metro_part(scene, walk),
        "weather": weather,
        "disruptions": {"status": {"bus": _status(scene), "train": _status(scene)}, "items": []},
        "timetable_changes": [],
    }

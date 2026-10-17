"""Serves the dashboard on its sample data, for the screenshots in docs/images/.

    .venv/bin/python tools/serve_doc_dashboard.py [--port 8931]

The page is this checkout's dashboard_static; the API answers with the test
fixtures (tests/fixtures/dashboard_*.json), a Glossop morning. The page keeps
time by the server's Date header, so this server's clock starts at the
sample's generated_at and runs on from there: the countdowns read as they
would at 07:01. The first train waits at Hadfield, so the line shows it,
and the 08:12, whose sample position is an hour early, isn't placed. The
area map's buses, sampled the day before, are moved to the same morning.

It listens on 127.0.0.1 only. Open http://127.0.0.1:8931/ and take the
pictures by hand; ?kiosk gives the wall tablet's page, and /performance the
Performance page. With --quiet the snapshot says a quiet window is on, so
the wall tablet's page dims to its night clock. --scheme light (or dark)
serves the pages with that scheme's colours whatever the browser prefers,
so the light pictures can be taken from a browser set to dark. --trams adds
a Trams section, made up (Glossop has none): the timetable's trams from
Piccadilly that morning, for the pictures of trams. --london adds London's
instead, live from TfL, made up too: Bank's Underground and DLR that
morning, with a line's status and a lift out of order. --leave bus has
Next to catch pick from the buses alone, and --leave none leaves it out, for
the pictures of the time and the weather in its place.

--scene piccadilly (or bank, or greenwich) serves a made-up morning at a
busy place instead of Glossop's, for the README's pictures: its trains,
buses and trams or London's metro, as tools/doc_scenes.py has them. Its
clock starts at 07:31:30, and it has no area map.

--phone also serves /frame: a page holding the dashboard in a frame 390
CSS pixels wide and 844 high, a phone's, for the phone-width pictures from
a browser window that can't be made that narrow; --frame 1280x800 gives
the frame another size, such as a wall tablet's. The frame is its own
viewport, so the page's media queries answer to its size. /frame?kiosk
frames /?kiosk, and so on. Choose the look on / first: the frame shares
its storage. Only this tool frames the page; the app's own server forbids
it (frame-ancestors 'none'). --offset 1200 moves the frame up 1200 CSS
pixels, so a part far down the page, like the Trams section, shows at the
top of a browser window that won't scroll; make the frame tall enough to
hold the whole page (--frame 1376x2400).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from functools import partial
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from doc_scenes import SCENES, scene_snapshot  # noqa: E402

STATIC = ROOT / "src" / "twobells" / "dashboard_static"
FIXTURES = ROOT / "tests" / "fixtures"


def with_scheme(body: bytes, scheme: str | None) -> bytes:
    """A stylesheet or page with `scheme`'s colours whatever the browser
    prefers: its dark tokens' media query made to match never (light) or
    always (dark), and its color-scheme meta saying that one scheme."""
    if scheme is None:
        return body
    always = b"all" if scheme == "dark" else b"not all"
    body = body.replace(b"@media (prefers-color-scheme: dark)", b"@media " + always)
    return body.replace(b'content="light dark"', b'content="' + scheme.encode() + b'"')


def sample_snapshot() -> dict:
    snapshot = json.loads((FIXTURES / "dashboard_snapshot.json").read_text(encoding="utf-8"))
    first, *rest = snapshot["trains"]["rows"]
    first["position"] = {"where": "At Hadfield, leaves at 07:11",
                         "strip": {"stops": ["Hadfield", "Glossop", "Dinting"], "yours": 1, "train_at": 0}}
    for row in rest:
        row["position"] = None
    return snapshot


def sample_area(generated_at: str) -> dict:
    area = json.loads((FIXTURES / "dashboard_area.json").read_text(encoding="utf-8"))
    at = datetime.fromisoformat(generated_at)
    area["fetched_at"] = generated_at
    for vehicle in area["vehicles"]:
        vehicle["recorded_at"] = (at - timedelta(seconds=vehicle["age_s"])).isoformat()
    return area


# A made-up morning at Piccadilly's tram stop, as TfGM's timetable would give it.
_TRAMS = (("07:05", "Purple Line", "Altrincham", False), ("07:08", "Blue Line", "Bury", False),
          ("07:11", "Replacement Bus", "Etihad Campus", True), ("07:14", "Purple Line", "Altrincham", False),
          ("07:17", "Blue Line", "Ashton-under-Lyne", False), ("07:20", "Blue Line", "Bury", False))
_TRAM_WALK = 3


def sample_trams(generated_at: str) -> dict:
    day = generated_at[:10]
    zone = generated_at[-6:]
    rows = []
    for i, (time_, line, destination, bus) in enumerate(_TRAMS):
        leaves = datetime.fromisoformat(f"{day}T{time_}:00{zone}")
        rows.append({
            "id": f"{i + 1:016x}", "line": line, "destination": destination, "stop": "T", "stop_name": "Piccadilly",
            "scheduled": time_, "scheduled_at": leaves.isoformat(), "expected": None, "expected_at": None,
            "status": "unknown", "status_text": "Timetable only", "status_spoken": "Timetable only", "live": False,
            "replacement_bus": bus, "walk_min": _TRAM_WALK,
            "leave_at": (leaves - timedelta(minutes=_TRAM_WALK)).isoformat(),
        })
    return {"label": "Trams", "status": {"last_success": generated_at, "stale": False, "error": None, "note": None},
            "timetable": {"state": "ready", "built": f"{day}T03:00:00{zone}"},
            "stops": {"T": {"name": "Piccadilly", "code": "9400ZZMAPIC", "walk_min": _TRAM_WALK}}, "rows": rows}


# A made-up morning at Bank, as TfL's predictions would give it: (seconds after
# generated_at, line, line id, kind, destination, via, label, platform, bound, where).
_LONDON = (
    (150, "Northern line", "northern", "tube", "Edgware", "via Bank", "T", "4", "Northbound",
     "Between Borough and London Bridge"),
    (270, "Central line", "central", "tube", "Epping", None, "T", "6", "Eastbound", "At St. Paul's"),
    (330, "DLR", "dlr", "dlr", "Lewisham", None, "U", "10", None, "Approaching Bank"),
    (450, "Central line", "central", "tube", "West Ruislip", None, "T", "5", "Westbound", "Left Liverpool Street"),
    (510, "Northern line", "northern", "tube", "Morden", "via Bank", "T", "3", "Southbound", "At Moorgate"),
    (690, "DLR", "dlr", "dlr", "Stratford", None, "U", "9", None, "At Tower Gateway"),
)
_LONDON_WALK = {"T": 3, "U": 5}


def sample_london(generated_at: str) -> dict:
    now = datetime.fromisoformat(generated_at)
    rows = []
    for i, (seconds, line, line_id, kind, destination, via, stop, platform, bound, where) in enumerate(_LONDON):
        due = now + timedelta(seconds=seconds)
        walk = _LONDON_WALK[stop]
        rows.append({
            "id": f"{0x100 + i:016x}", "line": line, "line_id": line_id, "kind": kind, "destination": destination,
            "via": via, "stop": stop, "stop_name": {"T": "Bank", "U": "Bank DLR"}[stop], "platform": platform,
            "bound": bound, "where": where, "scheduled": due.strftime("%H:%M"), "scheduled_at": due.isoformat(),
            "expected": due.strftime("%H:%M"), "expected_at": due.isoformat(), "status": "on_time",
            "status_text": "Live", "status_spoken": "Live", "live": True, "replacement_bus": False,
            "walk_min": walk, "leave_at": (due - timedelta(minutes=walk)).isoformat(),
        })
    return {
        "label": "Metro", "live": True, "timetable": None,
        "status": {"last_success": generated_at, "stale": False, "error": None, "note": None},
        "stops": {"T": {"name": "Bank", "code": "940GZZLUBNK", "walk_min": 3},
                  "U": {"name": "Bank DLR", "code": "940GZZDLBNK", "walk_min": 5}},
        "messages": [
            {"text": "Central line: Minor delays between White City and Liverpool Street while we fix a signal "
                     "failure. Tickets are valid on local buses.", "category": None, "severity": "minor",
             "kind": "service"},
            {"text": "Bank: The lift between the street and the Northern line is out of service today. Use Monument "
                     "for step-free access.", "category": None, "severity": "normal", "kind": "station"},
        ],
        "rows": rows,
    }


# A phone's viewport, in CSS pixels.
PHONE = (390, 844)


def frame_size(raw: str) -> tuple[int, int]:
    """WIDTHxHEIGHT in CSS pixels, like 1280x800."""
    width, sep, height = raw.partition("x")
    if not (sep and width.isdigit() and height.isdigit() and 100 <= int(width) <= 4000
            and 100 <= int(height) <= 4000):
        raise argparse.ArgumentTypeError(f"{raw!r} isn't WIDTHxHEIGHT, like 1280x800")
    return int(width), int(height)


def frame_page(query: str, size: tuple[int, int], offset: int = 0) -> bytes:
    """The /frame page: the dashboard, with `query`, in a frame of `size`
    at the top left, so a screenshot crops to it, moved up `offset` CSS
    pixels."""
    width, height = size
    src = "/?" + query if query else "/"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{width} by {height}</title>'
            f"<style>body{{margin:0;background:#808080;overflow:hidden}}iframe{{display:block;border:0;"
            f"position:relative;top:-{offset}px;width:{width}px;height:{height}px}}</style></head>"
            f'<body><iframe src="{escape(src)}" title="The dashboard, {width} by {height}"></iframe></body></html>'
            ).encode()


class Handler(SimpleHTTPRequestHandler):
    snapshot: bytes = b""
    area: bytes = b""
    started_at: float = 0.0  # the sample's time, in seconds since the epoch
    started: float = 0.0  # time.monotonic() when this server started
    scheme: str | None = None  # "light" or "dark" whatever the browser prefers; None: as it prefers
    frame: tuple[int, int] | None = None  # serve /frame, this size
    offset: int = 0  # /frame's frame moved up this many CSS pixels

    def date_time_string(self, timestamp=None):
        now = self.started_at + time.monotonic() - self.started
        return format_datetime(datetime.fromtimestamp(now, UTC), usegmt=True)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if self.frame and path == "/frame":
            return self._send(frame_page(query, self.frame, self.offset), "text/html; charset=utf-8")
        answers = {
            "/api/snapshot": lambda: self.snapshot,
            "/api/area": lambda: self.area,
        }
        if path in answers:
            return self._json(answers[path]())
        if path.startswith("/api/bus/"):
            return self._json((FIXTURES / "dashboard_bus.json").read_bytes())
        if path.startswith("/api/history/"):
            return self._json((FIXTURES / "dashboard_history.json").read_bytes())
        if path == "/api/performance":
            return self._json((FIXTURES / "dashboard_performance.json").read_bytes())
        if path.startswith("/api/"):  # /api/events: the page falls back to polling
            return self.send_error(404)
        pages = {"/": "/index.html", "/performance": "/performance.html"}
        self.path = pages.get(path) or path.removeprefix("/static")
        found = STATIC / self.path.lstrip("/")
        if self.scheme and found.suffix in (".css", ".html") and found.resolve().is_relative_to(STATIC) \
                and found.is_file():
            kind = "text/css" if found.suffix == ".css" else "text/html; charset=utf-8"
            return self._send(with_scheme(found.read_bytes(), self.scheme), kind)
        return super().do_GET()

    def _json(self, body: bytes) -> None:
        self._send(body, "application/json")

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--quiet", action="store_true", help="in a quiet window: a wall tablet dims")
    parser.add_argument("--scheme", choices=("light", "dark"), help="these colours, whatever the browser prefers")
    parser.add_argument("--phone", action="store_const", const=PHONE, dest="frame",
                        help="also serve /frame, the page in a phone-sized frame")
    parser.add_argument("--frame", type=frame_size, metavar="WIDTHxHEIGHT",
                        help="also serve /frame, the page in a frame this size")
    parser.add_argument("--offset", type=int, default=0, choices=range(4001), metavar="PIXELS",
                        help="/frame's frame moved up this far, to show a part lower down")
    parser.add_argument("--trams", action="store_true", help="a made-up Trams section too")
    parser.add_argument("--london", action="store_true", help="a made-up section of London's metro instead")
    parser.add_argument("--leave", metavar="MODES",
                        help="the modes Next to catch picks from, separated by commas, or none")
    parser.add_argument("--scene", choices=sorted(SCENES), help="a made-up morning at a busy place instead")
    args = parser.parse_args()
    if args.scene and (args.trams or args.london):
        parser.error("--scene has its own trams or metro: leave out --trams and --london")
    snapshot = scene_snapshot(SCENES[args.scene]) if args.scene else sample_snapshot()
    snapshot["quiet"] = args.quiet
    if args.trams:
        snapshot["metro"] = sample_trams(snapshot["generated_at"])
        snapshot["walk"]["metro"] = {"T": _TRAM_WALK}
    if args.london:
        snapshot["metro"] = sample_london(snapshot["generated_at"])
        snapshot["walk"]["metro"] = dict(_LONDON_WALK)
    if args.leave is not None:
        snapshot["leave_modes"] = [] if args.leave == "none" else args.leave.split(",")
    Handler.snapshot = json.dumps(snapshot).encode()
    Handler.area = json.dumps(sample_area(snapshot["generated_at"])).encode()
    Handler.started_at = datetime.fromisoformat(snapshot["generated_at"]).timestamp()
    Handler.started = time.monotonic()
    Handler.scheme = args.scheme
    Handler.frame = args.frame
    Handler.offset = args.offset
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(STATIC)))
    print(f"The sample dashboard is on http://127.0.0.1:{args.port}/")
    if args.frame:
        print(f"In a frame {args.frame[0]} by {args.frame[1]} on http://127.0.0.1:{args.port}/frame")
    server.serve_forever()


if __name__ == "__main__":
    main()

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
Performance page.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "twobells" / "dashboard_static"
FIXTURES = ROOT / "tests" / "fixtures"


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


class Handler(SimpleHTTPRequestHandler):
    snapshot: bytes = b""
    area: bytes = b""
    started_at: float = 0.0  # the sample's time, in seconds since the epoch
    started: float = 0.0  # time.monotonic() when this server started

    def date_time_string(self, timestamp=None):
        now = self.started_at + time.monotonic() - self.started
        return format_datetime(datetime.fromtimestamp(now, UTC), usegmt=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
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
        return super().do_GET()

    def _json(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8931)
    args = parser.parse_args()
    snapshot = sample_snapshot()
    Handler.snapshot = json.dumps(snapshot).encode()
    Handler.area = json.dumps(sample_area(snapshot["generated_at"])).encode()
    Handler.started_at = datetime.fromisoformat(snapshot["generated_at"]).timestamp()
    Handler.started = time.monotonic()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(STATIC)))
    print(f"The sample dashboard is on http://127.0.0.1:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()

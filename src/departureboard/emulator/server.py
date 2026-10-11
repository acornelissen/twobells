"""Panel emulator: a Display that serves frames to a browser page.

The page draws the board at true physical proportions. E-paper panels show in
e-paper colours and play the slow colour refresh, so redraw behaviour can be
judged on a laptop; a screen shows in screen colours and swaps frames at once.
"""

from __future__ import annotations

import errno
import io
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlparse

from PIL.Image import Image

from ..board.displays.screen import SCREEN_RGB
from ..board.panel import PHAT, Panel
from ..board.render import Palette

# Clear of ClaudeCodeBrowserX, which listens on 8765 and 8766.
DEFAULT_PORT = 8780

# The emulator only listens on loopback, but a DNS-rebinding page can still
# reach it under its own name; any other Host header is refused.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

# Approximate reflectance colours of a four-colour e-paper panel under room light.
EPAPER = {
    "white": (233, 231, 222),
    "black": (36, 36, 38),
    "red": (176, 38, 38),
    "yellow": (224, 180, 32),
}


# Millimetres for the page: (outline width, height, active area width, height).
# The pHAT's board is drawn in detail by the page itself.
FRAMES_MM = {
    "waveshare-10in85g": (270.56, 105.92, 259.76, 91.68),  # Waveshare's drawing
    "hdmi-1280x400": (207.78, 69.80, 191.5, 59.8),  # active area from the 7.9" diagonal
}


class EmulatorDisplay:
    def __init__(self, accent: str = "red", mode: str = "live", panel: Panel = PHAT) -> None:
        self.palette = Palette(white=0, black=1, accent=2)
        self.panel = panel
        self._accent = accent
        self._mode = mode
        self._lock = threading.Lock()
        self._png: bytes | None = None
        self._refreshes = 0
        self._ticks = 0
        self._board_time: str | None = None
        self._last_tick_rendered: bool | None = None

    def _palette(self) -> list[int]:
        rgb = EPAPER if self.panel.epaper else SCREEN_RGB
        colours = [(0, 0, 0)] * 256
        colours[self.palette.white] = rgb["white"]
        colours[self.palette.black] = rgb["black"]
        colours[self.palette.accent] = rgb[self._accent]
        return [value for rgb in colours for value in rgb]

    def show(self, image: Image) -> None:
        frame = image.copy()
        frame.putpalette(self._palette())
        buffer = io.BytesIO()
        frame.convert("RGB").save(buffer, format="PNG")
        with self._lock:
            self._png = buffer.getvalue()
            self._refreshes += 1

    def tick(self, now: datetime, rendered: bool) -> None:
        with self._lock:
            self._ticks += 1
            self._board_time = now.strftime("%a %H:%M" if self.panel.epaper else "%a %H:%M:%S")
            self._last_tick_rendered = rendered

    def frame_png(self) -> bytes | None:
        with self._lock:
            return self._png

    def status(self) -> dict:
        with self._lock:
            return {
                "version": self._refreshes,
                "refreshes": self._refreshes,
                "ticks": self._ticks,
                "board_time": self._board_time,
                "last_tick_rendered": self._last_tick_rendered,
                "accent": self._accent,
                "mode": self._mode,
                "panel": {
                    "name": self.panel.name,
                    "width": self.panel.width,
                    "height": self.panel.height,
                    "epaper": self.panel.epaper,
                    "frame_mm": FRAMES_MM.get(self.panel.name),
                },
            }


def _host_name(host: str) -> str:
    """The name in a Host header, without its port."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.split(":", 1)[0]


def _page() -> bytes:
    return files("departureboard.emulator").joinpath("emulator.html").read_bytes()


def make_server(
    display: EmulatorDisplay,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    attempts: int = 10,
):
    """Bind to `port`, or the next free one within `attempts` ports, stopping at 65535."""
    handler = _handler(display, _page())
    last = min(port + max(attempts, 1), 65536) - 1  # past the last port there is, binding raises OverflowError
    for candidate in range(port, last + 1):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    span = str(port) if last <= port else f"{port}-{last}"
    raise OSError(errno.EADDRINUSE, f"Port {span} already in use")


def _handler(display: EmulatorDisplay, page: bytes):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if _host_name(self.headers.get("Host", "")) not in LOOPBACK_HOSTS:
                self._send(403, "text/plain", b"forbidden")
            elif path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/status.json":
                self._send(200, "application/json", json.dumps(display.status()).encode())
            elif path == "/frame.png" and (frame := display.frame_png()) is not None:
                self._send(200, "image/png", frame)
            else:
                self._send(404, "text/plain", b"not found")

        def log_message(self, format, *args) -> None:  # noqa: A002 - quiet the console
            pass

    return Handler

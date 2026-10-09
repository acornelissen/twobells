"""Inky pHAT emulator: a Display that serves frames to a browser page.

The page draws the board at true physical proportions in e-paper colours and
plays the slow colour refresh, so redraw behaviour can be judged on a laptop.
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

from .render import Palette

# Clear of ClaudeCodeBrowserX, which listens on 8765 and 8766.
DEFAULT_PORT = 8780

# Approximate reflectance colours of a four-colour e-paper panel under room light.
EPAPER = {
    "white": (233, 231, 222),
    "black": (36, 36, 38),
    "red": (176, 38, 38),
    "yellow": (224, 180, 32),
}


class EmulatorDisplay:
    def __init__(self, accent: str = "red", mode: str = "live") -> None:
        self.palette = Palette(white=0, black=1, accent=2)
        self._accent = accent
        self._mode = mode
        self._lock = threading.Lock()
        self._png: bytes | None = None
        self._refreshes = 0
        self._ticks = 0
        self._board_time: str | None = None
        self._last_tick_rendered: bool | None = None

    def _epaper_palette(self) -> list[int]:
        colours = [(0, 0, 0)] * 256
        colours[self.palette.white] = EPAPER["white"]
        colours[self.palette.black] = EPAPER["black"]
        colours[self.palette.accent] = EPAPER[self._accent]
        return [value for rgb in colours for value in rgb]

    def show(self, image: Image) -> None:
        frame = image.copy()
        frame.putpalette(self._epaper_palette())
        buffer = io.BytesIO()
        frame.convert("RGB").save(buffer, format="PNG")
        with self._lock:
            self._png = buffer.getvalue()
            self._refreshes += 1

    def tick(self, now: datetime, rendered: bool) -> None:
        with self._lock:
            self._ticks += 1
            self._board_time = now.strftime("%a %H:%M")
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
            }


def _page() -> bytes:
    return files("departureboard").joinpath("emulator.html").read_bytes()


def make_server(
    display: EmulatorDisplay,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    attempts: int = 10,
):
    """Bind to `port`, or the next free one within `attempts` ports."""
    handler = _handler(display, _page())
    for candidate in range(port, port + attempts):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    last = port + attempts - 1
    span = str(port) if attempts == 1 else f"{port}-{last}"
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

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/status.json":
                self._send(200, "application/json", json.dumps(display.status()).encode())
            elif path == "/frame.png" and display.frame_png() is not None:
                self._send(200, "image/png", display.frame_png())
            else:
                self._send(404, "text/plain", b"not found")

        def log_message(self, format, *args) -> None:  # noqa: A002 - quiet the console
            pass

    return Handler

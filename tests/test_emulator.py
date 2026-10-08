import io
import json
import threading
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from PIL import Image

from departureboard.emulator import EPAPER, EmulatorDisplay, make_server

NOW = datetime(2026, 10, 8, 7, 3)


def frame(display, accent_pixel=False):
    p = display.palette
    image = Image.new("P", (250, 122), p.white)
    image.putpixel((0, 0), p.black)
    if accent_pixel:
        image.putpixel((1, 0), p.accent)
    return image


def test_starts_with_no_frame():
    display = EmulatorDisplay()
    assert display.status()["version"] == 0
    assert display.frame_png() is None


def test_show_bumps_version_and_counts_refreshes():
    display = EmulatorDisplay()
    display.show(frame(display))
    display.show(frame(display))
    status = display.status()
    assert status["version"] == 2
    assert status["refreshes"] == 2


def test_frame_uses_epaper_colours():
    display = EmulatorDisplay(accent="red")
    display.show(frame(display, accent_pixel=True))
    image = Image.open(io.BytesIO(display.frame_png())).convert("RGB")
    assert image.size == (250, 122)
    assert image.getpixel((0, 0)) == EPAPER["black"]
    assert image.getpixel((1, 0)) == EPAPER["red"]
    assert image.getpixel((10, 10)) == EPAPER["white"]


def test_yellow_accent():
    display = EmulatorDisplay(accent="yellow")
    display.show(frame(display, accent_pixel=True))
    image = Image.open(io.BytesIO(display.frame_png())).convert("RGB")
    assert image.getpixel((1, 0)) == EPAPER["yellow"]
    assert display.status()["accent"] == "yellow"


def test_tick_records_board_time_and_whether_it_redrew():
    display = EmulatorDisplay()
    display.tick(NOW, rendered=False)
    status = display.status()
    assert status["board_time"] == "Thu 07:03"
    assert status["ticks"] == 1
    assert status["last_tick_rendered"] is False


def test_status_carries_mode_label():
    assert EmulatorDisplay(mode="demo").status()["mode"] == "demo"


@pytest.fixture
def served():
    display = EmulatorDisplay()
    server = make_server(display, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield display, base
    server.shutdown()
    server.server_close()


def test_serves_page(served):
    _, base = served
    with urlopen(base + "/") as response:
        assert response.headers["Content-Type"].startswith("text/html")
        body = response.read().decode()
    assert "status.json" in body and "frame.png" in body


def test_serves_status_json(served):
    display, base = served
    display.show(frame(display))
    with urlopen(base + "/status.json") as response:
        assert json.load(response)["version"] == 1


def test_serves_frame_png(served):
    display, base = served
    display.show(frame(display))
    with urlopen(base + "/frame.png?v=1") as response:
        assert response.headers["Content-Type"] == "image/png"
        assert Image.open(io.BytesIO(response.read())).size == (250, 122)


def test_frame_404_before_first_render(served):
    _, base = served
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/frame.png")
    assert error.value.code == 404


def test_unknown_path_is_404(served):
    _, base = served
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/nope")
    assert error.value.code == 404

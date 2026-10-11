import errno
import io
import json
import math
import re
import shutil
import subprocess
import threading
from datetime import datetime
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from PIL import Image

from departureboard.emulator.server import EPAPER, EmulatorDisplay, make_server

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


def get_with_host(base, path, host):
    connection = HTTPConnection(base.removeprefix("http://"), timeout=5)
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8780", "127.0.0.1.evil.example", "10.0.0.5:8780"])
@pytest.mark.parametrize("path", ["/", "/status.json", "/frame.png"])
def test_refuses_other_host_names(served, path, host):
    # A DNS-rebinding page reaches 127.0.0.1 under its own name; refuse it.
    display, base = served
    display.show(frame(display))
    status, body = get_with_host(base, path, host)
    assert status == 403
    assert b"version" not in body and not body.startswith(b"\x89PNG")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8780", "localhost", "LOCALHOST:8780", "[::1]", "[::1]:8780"])
def test_serves_loopback_host_names(served, host):
    _, base = served
    status, _ = get_with_host(base, "/status.json", host)
    assert status == 200


def test_refuses_a_request_without_a_host(served):
    _, base = served
    connection = HTTPConnection(base.removeprefix("http://"), timeout=5)
    try:
        connection.putrequest("GET", "/status.json", skip_host=True)
        connection.endheaders()
        assert connection.getresponse().status == 403
    finally:
        connection.close()


def test_falls_back_to_next_free_port_when_taken():
    display = EmulatorDisplay()
    first = make_server(display, port=0)
    taken = first.server_address[1]
    try:
        second = make_server(display, port=taken, attempts=5)
        try:
            assert second.server_address[1] != taken
        finally:
            second.server_close()
    finally:
        first.server_close()


def test_reports_port_range_when_all_attempts_are_taken():
    display = EmulatorDisplay()
    first = make_server(display, port=0)
    taken = first.server_address[1]
    try:
        with pytest.raises(OSError, match=f"{taken}"):
            make_server(display, port=taken, attempts=1)
    finally:
        first.server_close()


def test_trying_the_next_ports_stops_at_the_last_port_there_is(monkeypatch):
    from departureboard.emulator import server as module

    tried = []

    def taken(address, handler):
        tried.append(address[1])
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(module, "ThreadingHTTPServer", taken)
    with pytest.raises(OSError, match="Port 65534-65535 already in use"):
        make_server(EmulatorDisplay(), port=65534, attempts=10)
    assert tried == [65534, 65535]


def test_default_port_avoids_claudecodebrowserx():
    # ClaudeCodeBrowserX listens on 8765 (HTTP) and 8766 (WebSocket).
    from departureboard.emulator.server import DEFAULT_PORT

    attempts = 10
    assert not {8765, 8766} & set(range(DEFAULT_PORT, DEFAULT_PORT + attempts))


def test_emulate_command_uses_the_shared_default_port():
    from departureboard.cli.main import _parser
    from departureboard.emulator.server import DEFAULT_PORT

    assert _parser().parse_args(["emulate", "--demo"]).port == DEFAULT_PORT


def test_status_describes_the_phat_by_default():
    panel = EmulatorDisplay().status()["panel"]
    assert panel == {"name": "inky-phat", "width": 250, "height": 122, "epaper": True, "frame_mm": None}


def test_status_gives_bigger_panels_their_physical_size():
    from departureboard.board.panel import WAVESHARE_10IN85G

    panel = EmulatorDisplay(panel=WAVESHARE_10IN85G).status()["panel"]
    assert (panel["width"], panel["height"]) == (1360, 480)
    outline_w, outline_h, active_w, active_h = panel["frame_mm"]
    assert outline_w > active_w and outline_h > active_h
    assert abs(active_w / active_h - 1360 / 480) < 0.01  # square pixels


def test_screen_frames_use_screen_colours():
    from departureboard.board.displays.screen import SCREEN_RGB
    from departureboard.board.panel import HDMI_1280X400

    display = EmulatorDisplay(accent="yellow", panel=HDMI_1280X400)
    image = Image.new("P", HDMI_1280X400.size, display.palette.black)
    image.putpixel((0, 0), display.palette.accent)
    display.show(image)
    shown = Image.open(io.BytesIO(display.frame_png())).convert("RGB")
    assert shown.getpixel((0, 0)) == SCREEN_RGB["yellow"]
    assert shown.getpixel((1, 0)) == SCREEN_RGB["black"]


def test_screen_board_time_has_seconds():
    from departureboard.board.panel import HDMI_1280X400

    display = EmulatorDisplay(panel=HDMI_1280X400)
    display.tick(NOW.replace(second=15), rendered=True)
    assert display.status()["board_time"] == "Thu 07:03:15"


def test_page_lays_itself_out_from_the_status_panel(served):
    _, base = served
    with urlopen(base + "/") as response:
        body = response.read().decode()
    for name in ("inky-phat", "waveshare-10in85g", "hdmi-1280x400", "hdmi-1920x440"):
        assert name in body


def waveform(speed, reduce=False):
    """The page's simulated e-paper refresh, run in node: [colour, ms] states."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node to run the page's script")
    from departureboard.emulator.server import _page

    found = re.search(r'<script id="waveform">(.*?)</script>', _page().decode(), re.S)
    assert found, "page has no waveform script"
    call = f"\nconsole.log(JSON.stringify(waveform({speed}, {json.dumps(reduce)})));"
    result = subprocess.run([node, "-e", found.group(1) + call], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def changes(steps):
    """When the panel changes: entering each state, then the new image appearing."""
    times, elapsed = [], 0.0
    for _, ms in steps:
        times.append(elapsed)
        elapsed += ms
    return [*times, elapsed]


@pytest.mark.parametrize("speed", [20000, 2500, 0])
@pytest.mark.parametrize("reduce", [False, True])
def test_refresh_never_flashes_more_than_three_times_a_second(speed, reduce):
    # WCAG 2.3.1: a flash is a pair of opposing changes; three a second is the limit.
    times = changes(waveform(speed, reduce))
    for start in times:
        in_one_second = [t for t in times if start <= t < start + 1000]
        assert math.ceil(len(in_one_second) / 2) <= 3, (speed, times)


def test_realistic_refresh_still_flickers_like_e_paper():
    colours = [colour for colour, _ in waveform(20000)]
    assert colours == ["black", "white"] * 3 + ["accent", "white"] * 2


def test_reduced_motion_refresh_does_not_flicker():
    for speed in (20000, 2500):
        assert [colour for colour, _ in waveform(speed, reduce=True)] == ["white"]


def test_page_asks_the_browser_about_reduced_motion():
    from departureboard.emulator.server import _page

    page = _page().decode()
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in page
    assert "@media (prefers-reduced-motion: reduce)" in page

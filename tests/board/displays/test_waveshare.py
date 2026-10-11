import re

import pytest
from PIL import Image

from tests.paths import ROOT
from twobells.board.displays.waveshare import BLACK, RED, WHITE, YELLOW, Controller, WaveshareDisplay, pack
from twobells.board.panel import WAVESHARE_10IN85G

SIZE = WAVESHARE_10IN85G.size


class FakeBus:
    """Records what the controller sends, as (op, value) pairs."""

    def __init__(self, busy_polls=0):
        self.ops = []
        self.busy_polls = busy_polls
        self.closed = False

    def power(self, on):
        self.ops.append(("power", on))

    def reset(self):
        self.ops.append(("reset", None))

    def select(self, master, slave):
        self.ops.append(("select", (master, slave)))

    def command(self, value):
        self.ops.append(("command", value))

    def data(self, values):
        self.ops.append(("data", bytes(values)))

    def busy(self):
        if self.busy_polls:
            self.busy_polls -= 1
            return True
        return False

    def close(self):
        self.closed = True

    def commands(self):
        return [v for op, v in self.ops if op == "command"]

    def data_after(self, command, select):
        """Data sent for `command` while `select` was the chip select state."""
        current, found = None, []
        for i, (op, value) in enumerate(self.ops):
            if op == "select":
                current = value
            elif op == "command" and value == command and current == select:
                found.append(self.ops[i + 1][1])
        return found


def controller(bus, **kwargs):
    return Controller(bus, sleep=lambda s: None, **kwargs)


def image(fill=WHITE):
    return Image.new("P", SIZE, fill)


def test_pack_puts_four_pixels_in_a_byte_leftmost_first():
    img = image(WHITE)
    for x, colour in enumerate((BLACK, WHITE, YELLOW, RED)):
        img.putpixel((x, 0), colour)
    packed = pack(img)
    assert len(packed) == SIZE[0] * SIZE[1] // 4
    assert packed[0] == 0b00_01_10_11
    assert packed[1] == 0b01_01_01_01  # all white


def test_pack_rejects_the_wrong_size_or_mode():
    with pytest.raises(ValueError):
        pack(Image.new("P", (250, 122)))
    with pytest.raises(ValueError):
        pack(Image.new("RGB", SIZE))


def test_pack_rejects_colours_the_panel_does_not_have():
    img = image()
    img.putpixel((5, 5), 4)
    with pytest.raises(ValueError, match="four colours"):
        pack(img)


def test_wake_powers_up_resets_then_initialises_both_controllers():
    bus = FakeBus()
    controller(bus).wake()
    assert bus.ops[:2] == [("power", True), ("reset", None)]
    assert bus.ops[2] == ("select", (True, True))
    assert bus.commands()[0] == 0x4D
    assert bus.commands()[-1] == 0x04  # power on
    assert bus.ops[-1] == ("select", (False, False))


def test_wake_sets_each_controller_to_half_the_panel():
    bus = FakeBus()
    controller(bus).wake()
    [resolution] = bus.data_after(0x61, (True, True))
    assert resolution == bytes((680 >> 8, 680 & 0xFF, 480 >> 8, 480 & 0xFF))


def test_left_half_goes_to_the_master_and_right_half_to_the_slave():
    img = image(WHITE)
    img.paste(RED, (680, 0, 1360, 480))  # right half red
    bus = FakeBus()
    controller(bus).write(pack(img))
    [left] = bus.data_after(0x10, (True, False))
    [right] = bus.data_after(0x10, (False, True))
    assert left == bytes([0b01010101]) * (170 * 480)
    assert right == bytes([0b11111111]) * (170 * 480)


def test_refresh_waits_for_the_panel():
    bus = FakeBus(busy_polls=3)
    controller(bus).refresh()
    assert bus.commands() == [0x12]
    assert bus.busy_polls == 0


def test_a_stuck_panel_times_out_instead_of_hanging():
    ticks = iter(range(1000))
    bus = FakeBus(busy_polls=10**6)
    epd = Controller(bus, sleep=lambda s: None, clock=lambda: next(ticks), busy_timeout=5)
    with pytest.raises(TimeoutError):
        epd.refresh()


class Clock:
    """A pretend time.monotonic that only moves when the controller sleeps."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


class SlowBus(FakeBus):
    """A panel busy for so many seconds after a reset, power on and refresh."""

    def __init__(self, clock, reset_s=0.0, power_on_s=0.0, refresh_s=0.0):
        super().__init__()
        self._clock = clock
        self._busy_for = {"reset": reset_s, 0x04: power_on_s, 0x12: refresh_s}
        self._until = 0.0

    def reset(self):
        super().reset()
        self._until = self._clock() + self._busy_for["reset"]

    def command(self, value):
        super().command(value)
        if value in self._busy_for:
            self._until = self._clock() + self._busy_for[value]

    def busy(self):
        return self._clock() < self._until


def _unit_timeout_s():
    """twobells.service's TimeoutStartSec, which a whole run must fit under."""
    text = (ROOT / "deploy" / "pi" / "twobells.service").read_text()
    [seconds] = re.findall(r"^TimeoutStartSec=(\d+)$", text, re.MULTILINE)
    return int(seconds)


def slow_show(**busy):
    clock = Clock()
    bus = SlowBus(clock, **busy)
    display = WaveshareDisplay(controller=lambda: Controller(bus, sleep=clock.sleep, clock=clock))
    return display, bus, clock


def test_a_usual_refresh_goes_through():
    display, bus, clock = slow_show(reset_s=0.5, power_on_s=0.5, refresh_s=25)
    display.show(image())
    assert 0x07 in bus.commands() and bus.closed


def test_a_slow_panel_gives_up_in_time_for_the_run_to_finish_under_the_units_timeout():
    # Slow at every step but never quite stuck: each wait alone is under a
    # minute, but together they must not outlast the unit and be killed.
    display, bus, clock = slow_show(reset_s=40, power_on_s=40, refresh_s=55)
    with pytest.raises(TimeoutError, match="e-paper"):
        display.show(image())
    assert 0x07 in bus.commands() and bus.closed  # still put to sleep
    # What is left is for starting up, fetching and drawing.
    assert clock.now <= _unit_timeout_s() - 45


def test_sleep_powers_down_and_releases_the_bus():
    bus = FakeBus()
    controller(bus).sleep()
    assert bus.commands() == [0x02, 0x07]
    assert ("power", False) in bus.ops
    assert bus.closed


def test_display_wakes_writes_refreshes_and_sleeps():
    bus = FakeBus()
    WaveshareDisplay(controller=lambda: controller(bus)).show(image())
    commands = bus.commands()
    assert commands.index(0x04) < commands.index(0x10) < commands.index(0x12) < commands.index(0x07)
    assert bus.closed


def test_display_still_sleeps_the_panel_when_a_refresh_fails():
    bus = FakeBus(busy_polls=10**6)
    ticks = iter(range(1000))
    epd = Controller(bus, sleep=lambda s: None, clock=lambda: next(ticks), busy_timeout=5)
    with pytest.raises(TimeoutError):
        WaveshareDisplay(controller=lambda: epd).show(image())
    assert 0x07 in bus.commands()
    assert bus.closed


def test_palette_uses_the_panel_colour_codes():
    palette = WaveshareDisplay(accent="yellow", controller=lambda: None).palette
    assert (palette.white, palette.black, palette.accent) == (WHITE, BLACK, YELLOW)
    assert WaveshareDisplay(accent="red", controller=lambda: None).palette.accent == RED


def test_unknown_accent_is_refused():
    with pytest.raises(ValueError, match="red, yellow"):
        WaveshareDisplay(accent="green", controller=lambda: None)


def test_rotates_for_upside_down_mounting():
    bus = FakeBus()
    img = image(WHITE)
    img.putpixel((0, 0), RED)
    WaveshareDisplay(rotate_180=True, controller=lambda: controller(bus)).show(img)
    [right] = bus.data_after(0x10, (False, True))
    assert right[-1] == 0b01_01_01_11  # bottom-right pixel is now red


def test_renders_straight_onto_the_panel():
    from twobells.board.themes import THEMES
    from twobells.board.view import BoardView, Section

    display = WaveshareDisplay(accent="yellow", controller=lambda: controller(FakeBus()))
    view = BoardView("GLO > MAN", Section("Trains", (), "No trains", True), Section("Buses", (), "No buses"), "07:03")
    for theme in THEMES.values():
        display.show(theme.render(view, display.palette, display.panel))

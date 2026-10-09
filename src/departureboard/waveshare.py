"""Waveshare 10.85" e-Paper (G): 1360x480, black/white/yellow/red, about 21 s a refresh.

The panel is two 680x480 controllers side by side, each with its own chip
select. This follows the command sequence in Waveshare's epd10in85g.py, but
talks to the HAT through spidev and gpiozero rather than loading Waveshare's
prebuilt DEV_Config shared library.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

from PIL.Image import Image

from .panel import WAVESHARE_10IN85G
from .render import Palette

# The panel's 2-bit colour codes. Images are drawn with these as palette
# indices, so packing four pixels to a byte needs no colour matching.
BLACK, WHITE, YELLOW, RED = 0, 1, 2, 3
_ACCENTS = {"yellow": YELLOW, "red": RED}

_HALF_WIDTH = WAVESHARE_10IN85G.width // 2
_HEIGHT = WAVESHARE_10IN85G.height
_ROW_BYTES = WAVESHARE_10IN85G.width // 4
_HALF_ROW_BYTES = _ROW_BYTES // 2

# (command, data) sent to both controllers at once, from Waveshare's EPD.Init().
_INIT = (
    (0x4D, (0x78,)),
    (0xE0, (0x01,)),
    (0xE5, (0x08,)),
    (0xA2, (0x01,)),
    (0x00, (0x2F, 0x21)),
    (0xA2, (0x02,)),
    (0x00, (0x2F, 0x21)),
    (0xA2, (0x00,)),
    (0x01, (0x07, 0x00)),
    (0x06, (0x0D, 0x12, 0x30, 0x20, 0x19, 0x3D, 0x0C)),
    (0x30, (0x08,)),
    (0x50, (0x37,)),
    # Resolution of one controller: 680 x 480.
    (0x61, (_HALF_WIDTH >> 8, _HALF_WIDTH & 0xFF, _HEIGHT >> 8, _HEIGHT & 0xFF)),
    (0x65, (0x00, 0x00, 0x00, 0x00)),
    (0xE3, (0x88,)),
    (0xE9, (0x01,)),
    (0xB8, (0xB5,)),
)
_POWER_ON = 0x04
_WRITE_IMAGE = 0x10
_REFRESH = 0x12
_POWER_OFF = 0x02
_DEEP_SLEEP = 0x07


def pack(image: Image) -> bytes:
    """Four pixels to a byte, leftmost in the top bits, as the panel reads them."""
    if image.mode != "P" or image.size != WAVESHARE_10IN85G.size:
        raise ValueError(f"Expected a {WAVESHARE_10IN85G.size} paletted image, got {image.mode} {image.size}")
    low, high = image.getextrema()
    if high > RED:
        raise ValueError(f"Pixel value {high} is not one of the panel's four colours")
    return image.tobytes("raw", "P;2")


class Bus(Protocol):
    def power(self, on: bool) -> None: ...
    def reset(self) -> None: ...
    def select(self, master: bool, slave: bool) -> None: ...
    def command(self, value: int) -> None: ...
    def data(self, values: bytes) -> None: ...
    def busy(self) -> bool: ...
    def close(self) -> None: ...


class GpioBus:
    """SPI plus the HAT's control pins. Chip selects are driven by hand, so SPI
    needs `dtoverlay=spi0-0cs` to leave GPIO 7 and 8 free."""

    # BCM pin numbers from Waveshare's epdconfig.py.
    CS_MASTER, CS_SLAVE, DC, RST, BUSY, PWR = 8, 7, 25, 17, 24, 18

    def __init__(self, spi_bus: int = 0, spi_device: int = 0, speed_hz: int = 4_000_000) -> None:
        # Imported lazily: only present on the Pi.
        import spidev
        from gpiozero import DigitalInputDevice, DigitalOutputDevice

        self._cs_master = DigitalOutputDevice(self.CS_MASTER, initial_value=True)
        self._cs_slave = DigitalOutputDevice(self.CS_SLAVE, initial_value=True)
        self._dc = DigitalOutputDevice(self.DC)
        self._rst = DigitalOutputDevice(self.RST, initial_value=True)
        self._pwr = DigitalOutputDevice(self.PWR)
        self._busy = DigitalInputDevice(self.BUSY, pull_up=False)
        self._spi = spidev.SpiDev()
        self._spi.open(spi_bus, spi_device)
        self._spi.max_speed_hz = speed_hz
        self._spi.mode = 0b00

    def power(self, on: bool) -> None:
        self._pwr.value = on

    def reset(self) -> None:
        self._rst.on()
        time.sleep(0.2)
        self._rst.off()
        time.sleep(0.002)
        self._rst.on()
        time.sleep(0.2)

    def select(self, master: bool, slave: bool) -> None:
        # Active low.
        self._cs_master.value = not master
        self._cs_slave.value = not slave

    def command(self, value: int) -> None:
        self._dc.off()
        self._spi.writebytes([value])

    def data(self, values: bytes) -> None:
        self._dc.on()
        self._spi.writebytes2(values)

    def busy(self) -> bool:
        return not self._busy.value  # low while the panel is working

    def close(self) -> None:
        self._spi.close()
        for pin in (self._cs_master, self._cs_slave, self._dc, self._rst, self._pwr, self._busy):
            pin.close()


class Controller:
    """The panel's command sequence: wake, write both halves, refresh, sleep."""

    def __init__(
        self,
        bus: Bus,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        busy_timeout: float = 60,
    ) -> None:
        self._bus = bus
        self._sleep = sleep
        self._clock = clock
        self._busy_timeout = busy_timeout

    def _wait(self) -> None:
        self._sleep(0.1)
        deadline = self._clock() + self._busy_timeout
        while self._bus.busy():
            if self._clock() > deadline:
                raise TimeoutError(f"e-paper still busy after {self._busy_timeout:.0f} s")
            self._sleep(0.02)

    def _send(self, command: int, data: tuple[int, ...] | bytes = ()) -> None:
        self._bus.command(command)
        if data:
            self._bus.data(bytes(data))

    def wake(self) -> None:
        self._bus.power(True)
        self._bus.reset()
        self._wait()
        self._bus.select(True, True)
        for command, data in _INIT:
            self._send(command, data)
        self._sleep(0.2)
        self._send(_POWER_ON)
        self._sleep(0.5)
        self._wait()
        self._bus.select(False, False)

    def write(self, packed: bytes) -> None:
        rows = [packed[i : i + _ROW_BYTES] for i in range(0, len(packed), _ROW_BYTES)]
        for master, half in ((True, slice(0, _HALF_ROW_BYTES)), (False, slice(_HALF_ROW_BYTES, None))):
            self._bus.select(master, not master)
            self._send(_WRITE_IMAGE, b"".join(row[half] for row in rows))
            self._bus.select(False, False)

    def refresh(self) -> None:
        self._bus.select(True, True)
        self._send(_REFRESH, (0x00,))
        self._bus.select(False, False)
        self._wait()

    def sleep(self) -> None:
        self._bus.select(True, True)
        self._send(_POWER_OFF, (0x00,))
        self._sleep(0.1)
        self._send(_DEEP_SLEEP, (0xA5,))
        self._bus.select(False, False)
        self._sleep(2)
        self._bus.power(False)
        self._bus.close()


class WaveshareDisplay:
    def __init__(
        self,
        accent: str = "red",
        rotate_180: bool = False,
        controller: Callable[[], Controller] | None = None,
    ) -> None:
        if accent not in _ACCENTS:
            raise ValueError(f"Unknown accent {accent!r}; choose from {', '.join(sorted(_ACCENTS))}")
        self.panel = WAVESHARE_10IN85G
        self.palette = Palette(white=WHITE, black=BLACK, accent=_ACCENTS[accent])
        self._rotate = rotate_180
        # A fresh bus each refresh: the panel is powered down in between.
        self._controller = controller or (lambda: Controller(GpioBus()))

    def show(self, image: Image) -> None:
        if self._rotate:
            image = image.rotate(180)
        packed = pack(image)
        epd = self._controller()
        try:
            epd.wake()
            epd.write(packed)
            epd.refresh()
        finally:
            # Waveshare: leaving the panel powered for long can damage it.
            epd.sleep()

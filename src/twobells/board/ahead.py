"""A screen's board for each tick, made a tick ahead so the frames never wait for it.

Making a tick's board (fetching whatever is due, working out the view, drawing
it) takes 30 to 130 ms on a Pi 4 with the bar screen: more than a frame
(SCROLL_FRAME_S). Made on the frame loop's thread at each tick, it held the
step due then back, or skipped it, so scrolling text hitched once a second.
Here a worker thread makes tick N+1's board during tick N, and at the tick the
frame loop only shows it, with the scrolling text and turning tiles where they
are at that moment.

Who owns what, and when:

- The worker thread (Ahead) owns the board, the state, the store, the
  speaker and the reload hook from when it is started until stop() has
  joined it. It reads the display's panel, palette and rotation, which are
  fixed when the display is made, and never draws on it.
- The frame loop's thread (show_ahead, the main thread) owns the display and
  so the screen: pygame is only touched from the thread that opened it. It
  never touches the state while the worker runs; once stop() returns, the
  state is its own again, to save.
- They hand over through two queues: the moment of the next tick one way,
  that tick's board (a Tick, never changed after) or the fault making it the
  other. One tick is asked for at a time, so a slow board is never queued
  behind another.

A board not made by its tick (a slow fetch, say) is shown the moment it is,
the text scrolling on meanwhile; the next is asked for the tick after the
board's clock then, so the board is never more than one tick behind.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .scroll import Frame, board_seconds

if TYPE_CHECKING:
    from .app import RunResult, ScrollingDisplay

# How long each board took to make and show, and any frames skipped, at
# DEBUG: `twobells run --loop --timings` logs them.
timings = logging.getLogger("twobells.timings")


@dataclass(frozen=True)
class Tick:
    """A tick's board, made by the worker for the frame loop to show."""

    now: datetime  # the board's clock for the tick
    result: RunResult
    frame: Frame | None  # None when it hasn't changed, so isn't drawn again
    made_s: float  # how long making it took


class Ahead:
    """The worker: one thread making each tick's board with `make(now, prime)`, when asked."""

    def __init__(self, make: Callable[[datetime, bool], Tick]) -> None:
        self._make = make
        self._asked: queue.Queue[tuple[datetime, bool] | None] = queue.Queue()
        self._made: queue.Queue[Tick | BaseException] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="twobells-ahead", daemon=True)
        self._thread.start()

    def ask(self, now: datetime, prime: bool = False) -> None:
        """Make the board for `now`."""
        self._asked.put((now, prime))

    def take(self, until: float, wall: Callable[[], float]) -> Tick | None:
        """The board asked for, once it is made, or None if it isn't by
        `until` on the wall clock. A fault making it is raised here."""
        while True:
            try:
                made = self._made.get(timeout=max(0.0, until - wall()))
            except queue.Empty:
                if wall() >= until:
                    return None
                continue  # a simulated clock stands still while it waits
            if isinstance(made, BaseException):
                raise made
            return made

    def stop(self) -> None:
        """Wait for the board being made, if one is, and end the thread:
        the state is the caller's again."""
        if self._thread.is_alive():
            self._asked.put(None)
            self._thread.join()

    def _work(self) -> None:
        while (asked := self._asked.get()) is not None:
            try:
                made: Tick | BaseException = self._make(*asked)
            except BaseException as error:  # noqa: BLE001 - raised on the frame loop's thread, by take()
                made = error
            self._made.put(made)
            if isinstance(made, BaseException):
                return


def _frame_at(at: float, tick_seconds: float, frame_seconds: float) -> tuple[int, float]:
    """Which frame of its tick wall time `at` is in, counted from the tick
    (a whisker short of a frame is on it, as sleep_to_the_tick wakes), and
    when the next frame is due."""
    into = at % tick_seconds
    frame = int(into / frame_seconds + 1e-3)
    return frame, at - into + (frame + 1) * frame_seconds


def show_ahead(display: ScrollingDisplay, ahead: Ahead, clock: Callable[[], datetime], tick_seconds: float,
               frame_seconds: float, sleep: Callable[[float], None], wall: Callable[[], float],
               ticks: int | None = None, on_tick: Callable | None = None, prime: bool = False) -> None:
    """Show a board each tick (or `ticks` of them), made ahead by `ahead`,
    and while anything moves, a frame every `frame_seconds` between, each
    moving it to the board's clock: the tick's time plus which frame of the
    tick it is. The wall clock says which frame is due, so a late frame is
    skipped rather than drawn late. `sleep` lines up with whole multiples of
    what it is given, as the screen loop's does. A clock with `advance()`
    is simulated."""
    per_tick = round(tick_seconds / frame_seconds)
    shown = 0
    now = clock()
    ahead.ask(now, prime)
    asked = True  # a board is being made, for this tick or the next
    while True:
        # A tick: the board asked for is due.
        base, last, waiting = board_seconds(now), -1, asked
        while True:
            frame, next_at = _frame_at(wall(), tick_seconds, frame_seconds)
            if frame % per_tick <= last:
                break  # the next tick is due
            frame %= per_tick
            if last >= 0 and frame > last + 1:
                timings.debug("%d frames skipped", frame - last - 1)
            seconds = base + frame * frame_seconds
            tick = ahead.take(wall() + frame_seconds / 2, wall) if waiting else None
            if tick is not None:
                _show(display, tick, seconds, wall() % tick_seconds)
            if display.scrolling:
                display.scroll(seconds)
            last = frame
            if waiting and tick is None:
                # Late: shown the moment it is made, the text moving on meanwhile.
                tick = ahead.take(next_at, wall)
                if tick is None:
                    continue  # the next frame is due
                _show(display, tick, seconds, wall() % tick_seconds)
            if tick is not None:
                waiting, shown = False, shown + 1
                if on_tick is not None:
                    on_tick(tick.now, tick.result)
                asked = ticks is None or shown < ticks
                if asked:
                    ahead.ask(now + timedelta(seconds=tick_seconds))
            sleep(frame_seconds if display.scrolling else tick_seconds)
        if not asked:
            return
        if hasattr(clock, "advance"):
            clock.advance()  # type: ignore[attr-defined]
        now = clock()


def _show(display: ScrollingDisplay, tick: Tick, seconds: float, into: float) -> None:
    """Show `tick`'s board, `into` seconds after the wall clock's tick."""
    started = time.perf_counter()
    if tick.frame is not None:
        display.show_frame(tick.frame, seconds)
    if timings.isEnabledFor(logging.DEBUG):
        timings.debug("%s made in %.1f ms, shown %.1f ms into its second in %.1f ms", tick.now.strftime("%H:%M:%S"),
                      tick.made_s * 1000, into * 1000, (time.perf_counter() - started) * 1000)

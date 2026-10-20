import threading
import time
from datetime import datetime, timedelta

import pytest
from PIL import Image

from twobells.board.ahead import Ahead, Tick, show_ahead
from twobells.board.scroll import SCROLL_FRAME_S, Frame, board_seconds

T0 = datetime(2026, 10, 8, 7, 0)
PER_SECOND = round(1 / SCROLL_FRAME_S)


def a_tick(now, made_s=0.0):
    return Tick(now, result=None, frame=Frame(Image.new("P", (4, 4))), made_s=made_s)  # type: ignore[arg-type]


# The worker: a thread of its own, one board at a time.


def test_the_worker_makes_each_board_on_its_own_thread_and_hands_it_over():
    threads = []

    def make(now, prime):
        threads.append(threading.current_thread())
        return a_tick(now)

    ahead = Ahead(make)
    try:
        ahead.ask(T0, True)
        tick = ahead.take(time.time() + 5, time.time)
        assert tick is not None and tick.now == T0
        ahead.ask(T0 + timedelta(seconds=1))
        assert ahead.take(time.time() + 5, time.time).now == T0 + timedelta(seconds=1)
    finally:
        ahead.stop()
    assert threads[0] is threads[1] and threads[0] is not threading.current_thread()


def test_a_fault_making_a_board_is_raised_by_take_and_ends_the_worker():
    def make(now, prime):
        raise OSError("no such file")

    ahead = Ahead(make)
    ahead.ask(T0)
    with pytest.raises(OSError, match="no such file"):
        ahead.take(time.time() + 5, time.time)
    ahead._thread.join(5)
    assert not ahead._thread.is_alive()
    ahead.stop()  # nothing left to stop


def test_take_gives_none_at_its_time_and_the_board_once_it_is_made():
    go = threading.Event()

    def make(now, prime):
        go.wait(5)
        return a_tick(now)

    ahead = Ahead(make)
    try:
        ahead.ask(T0)
        assert ahead.take(time.time() + 0.02, time.time) is None
        go.set()
        assert ahead.take(time.time() + 5, time.time).now == T0
    finally:
        ahead.stop()


def test_stop_waits_for_the_board_being_made_so_the_state_is_the_callers_again():
    state = {}
    started = threading.Event()

    def make(now, prime):
        started.set()
        time.sleep(0.05)
        state["made"] = now
        return a_tick(now)

    ahead = Ahead(make)
    ahead.ask(T0)
    started.wait(5)
    ahead.stop()
    assert state == {"made": T0}
    assert not ahead._thread.is_alive()


# The frame loop, on a simulated wall clock, with a worker whose boards take
# a set time to make on that clock.


class WallClock:
    """The wall clock, and a sleep that wakes at the next whole multiple of what it is given."""

    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        wait = seconds - self.at % seconds
        if wait < seconds / 1000:
            wait += seconds
        self.at += wait


class TimedAhead:
    """A worker each of whose boards is made the moment it is asked for, on
    the wall clock, or as many seconds after as `costs` says, by the board's
    clock: the frame loop's waiting moves the clock on to then."""

    def __init__(self, wall, costs=None):
        self.wall, self.costs, self.asked = wall, costs or {}, []
        self._made_at, self._now = None, None

    def ask(self, now, prime=False):
        self.asked.append(now)
        self._made_at = self.wall() + self.costs.get(now, 0)
        self._now = now

    def take(self, until, wall):
        assert wall is self.wall
        if self._made_at is not None and self._made_at <= until:
            self.wall.at = max(self.wall.at, self._made_at)
            self._made_at = None
            return a_tick(self._now)
        self.wall.at = max(self.wall.at, until)
        return None


class Recording:
    scrolling = True

    def __init__(self, wall):
        self.wall, self.frames, self.scrolled = wall, [], []

    def show_frame(self, frame, seconds):
        self.frames.append((self.wall(), seconds))

    def scroll(self, seconds):
        self.scrolled.append((self.wall(), seconds))


def wall_clock_board(wall, start=1000.0):
    """The board's clock, read off the wall clock: T0 at `start`."""
    return lambda: T0 + timedelta(seconds=int(wall() - start))


def test_a_board_made_late_is_shown_the_moment_it_is_with_the_text_moving_on_meanwhile():
    wall = WallClock(1000.0)
    late = T0 + timedelta(seconds=1)
    ahead = TimedAhead(wall, {late: 1.3})  # asked for at 1000.0, made at 1001.3
    screen = Recording(wall)
    show_ahead(screen, ahead, wall_clock_board(wall), 1, SCROLL_FRAME_S, wall.sleep, wall, ticks=3)
    base = board_seconds(T0)
    [first, shown_late, third] = screen.frames
    assert first == (1000.0, base)
    # Not skipped: shown once made, at the moment's place in the scroll.
    assert shown_late[0] == pytest.approx(1001.3)
    assert shown_late[1] == pytest.approx(base + 1 + 4 * SCROLL_FRAME_S)  # the frame it is made in
    # The text went on moving to the board's clock while it was made.
    waiting = [seconds for at, seconds in screen.scrolled if 1001.0 <= at < 1001.3]
    assert waiting == pytest.approx([base + 1 + k * SCROLL_FRAME_S for k in range(4)])
    assert ahead.asked == [T0, late, T0 + timedelta(seconds=2)]
    assert third == (1002.0, base + 2)


def test_a_board_more_than_a_tick_late_is_shown_and_the_next_is_for_the_tick_after_the_clock():
    wall = WallClock(1000.0)
    late = T0 + timedelta(seconds=1)
    ahead = TimedAhead(wall, {late: 2.5})  # made at 1002.5, in the tick after its own
    screen = Recording(wall)
    show_ahead(screen, ahead, wall_clock_board(wall), 1, SCROLL_FRAME_S, wall.sleep, wall, ticks=3)
    assert [at for at, _ in screen.frames] == pytest.approx([1000.0, 1002.5, 1003.0])
    # Its 07:00:01 shows until 07:00:03's board, the next tick's: never more than a tick behind.
    assert ahead.asked == [T0, late, T0 + timedelta(seconds=3)]


def test_the_next_board_is_asked_for_by_the_clock_so_summer_time_ending_is_caught_up_at_once():
    wall = WallClock(1000.0)
    ahead = TimedAhead(wall)
    back = T0 + timedelta(seconds=2) - timedelta(hours=1)
    times = iter([T0, T0 + timedelta(seconds=1), back, back + timedelta(seconds=1)])
    screen = Recording(wall)
    show_ahead(screen, ahead, lambda: next(times), 1, SCROLL_FRAME_S, wall.sleep, wall, ticks=4)
    assert ahead.asked == [T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2), back + timedelta(seconds=1)]
    # The text keeps to the board's clock as it reads, back an hour.
    assert screen.frames[2][1] == board_seconds(back)


def test_with_nothing_moving_the_loop_wakes_only_for_each_tick():
    wall = WallClock(1000.0)
    screen = Recording(wall)
    screen.scrolling = False
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        wall.sleep(seconds)

    show_ahead(screen, TimedAhead(wall), wall_clock_board(wall), 1, SCROLL_FRAME_S, sleep, wall, ticks=2)
    assert sleeps == [1, 1] and screen.scrolled == []
    assert len(screen.frames) == 2


def test_each_tick_shown_is_handed_to_on_tick():
    wall = WallClock(1000.0)
    seen = []
    show_ahead(Recording(wall), TimedAhead(wall), wall_clock_board(wall), 1, SCROLL_FRAME_S, wall.sleep, wall,
               ticks=2, on_tick=lambda now, result: seen.append(now))
    assert seen == [T0, T0 + timedelta(seconds=1)]

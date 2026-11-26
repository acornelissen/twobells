"""The e-paper timer's run on a Pi (cli/run.py's _single), with its runtime
folder, /run/twobells: what it trusts after the Pi starts, and what it keeps
there while the state can't be saved."""

from datetime import datetime, timedelta

import pytest

from twobells.cli.main import main
from twobells.core.model import Departure, Status

THU_0700 = datetime(2026, 10, 8, 7, 0)  # in the trains' window, Mon-Fri 06:30-09:30


class Timer:
    """The timer's runs: the board's clock, Darwin (one train, or failing),
    the card (working, or full), and what each run showed and drew."""

    def __init__(self, tmp_path, monkeypatch):
        from twobells.board.displays.display import PngDisplay
        from twobells.board.state import StateStore
        from twobells.cli import run
        from twobells.sources.darwin import DarwinSource

        self.now = THU_0700
        self.error: Exception | None = None
        self.full = False
        self.calls = 0
        self.drawn = 0
        self.results = []
        self.folder = tmp_path / "run"
        self.folder.mkdir(mode=0o700)  # as systemd makes it for twobells.service
        self.state = tmp_path / "state.json"
        monkeypatch.setenv("RUNTIME_DIRECTORY", str(self.folder))

        def fetch(source):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return [Departure("train", "Northern", "Manchester Piccadilly", "09:45", None, Status.ON_TIME)]

        real_save, real_run_once = StateStore.save, run.run_once

        def save(store, state):
            if self.full:
                raise OSError(28, "No space left on device")
            real_save(store, state)

        def run_once(*args, **kwargs):
            self.results.append(real_run_once(*args, **kwargs))
            return self.results[-1]

        monkeypatch.setattr(DarwinSource, "fetch", fetch)
        monkeypatch.setattr(StateStore, "save", save)
        monkeypatch.setattr(run, "run_once", run_once)
        monkeypatch.setattr(run, "local_now", lambda tz: self.now)
        monkeypatch.setattr(PngDisplay, "show", lambda display, image: setattr(self, "drawn", self.drawn + 1))
        config = tmp_path / "config.toml"
        config.write_text(f'state_file = "{self.state}"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
        secrets = tmp_path / "secrets.env"
        secrets.write_text("RDM_API_KEY=k\n")
        self.args = ["run", "--config", str(config), "--secrets", str(secrets), "--png", str(tmp_path / "out.png")]

    def run(self, at=None) -> int:
        if at is not None:
            self.now = at
        return main(self.args)

    @property
    def marked(self) -> bool:
        return self.results[-1].view.section("train").stale

    def boot(self):
        """The Pi restarts: /run is emptied, and systemd makes the folder afresh."""
        for path in self.folder.iterdir():
            path.unlink()


@pytest.fixture
def timer(tmp_path, monkeypatch):
    return Timer(tmp_path, monkeypatch)


# --- 1. Saved departures aren't trusted until fetched since the Pi started ---


def test_the_timer_trusts_what_it_fetched_since_the_pi_started(timer):
    assert timer.run() == 0 and timer.calls == 1
    timer.error = OSError("network down")
    assert timer.run(THU_0700 + timedelta(minutes=1)) == 0
    assert timer.calls == 1 and not timer.marked  # not due yet, and fetched since the start


def test_after_a_boot_with_a_wrong_clock_and_the_network_down_what_was_saved_is_marked(timer):
    timer.run()
    timer.boot()
    # It is really hours later; the clock without a battery says a minute.
    timer.error = OSError("network down")
    for minute in (1, 2, 3, 10):
        assert timer.run(THU_0700 + timedelta(minutes=minute)) == 0
        assert timer.marked, minute
    assert [r.left for r in timer.results[-1].view.section("train").rows] == ["09:45"]  # still shown, marked


def test_after_a_boot_the_first_fetch_that_works_clears_the_mark(timer):
    timer.run()
    timer.boot()
    timer.run(THU_0700 + timedelta(minutes=1))
    assert timer.marked and timer.calls == 1  # not due: nothing extra fetched for the start
    timer.run(THU_0700 + timedelta(minutes=2))
    assert timer.calls == 2 and not timer.marked
    timer.error = OSError("network down")
    timer.run(THU_0700 + timedelta(minutes=3))
    assert not timer.marked  # fetched since the start, and still fresh


def test_off_the_pi_the_timer_trusts_what_it_saved_as_ever(timer, monkeypatch):
    monkeypatch.delenv("RUNTIME_DIRECTORY")  # Docker, a desktop, a hand-run `twobells run`
    timer.run()
    timer.error = OSError("network down")
    timer.run(THU_0700 + timedelta(minutes=1))
    assert not timer.marked
    assert list(timer.folder.iterdir()) == []


def test_a_fetch_whose_state_couldnt_be_saved_doesnt_count(timer):
    # The next run reads the card afresh, and it still has what came before the start.
    timer.run()
    timer.boot()
    timer.full = True
    assert timer.run(THU_0700 + timedelta(minutes=2)) != 0
    assert timer.calls == 2 and not timer.marked  # this run shows what it fetched
    timer.error = OSError("network down")
    timer.run(THU_0700 + timedelta(minutes=3))
    assert timer.marked  # what the card has is from before the start
    timer.full = timer.error = None
    timer.run(THU_0700 + timedelta(minutes=5))
    timer.error = OSError("network down")
    timer.run(THU_0700 + timedelta(minutes=6))
    assert not timer.marked


def test_a_bad_record_counts_as_nothing_fetched_since_the_start(timer):
    timer.run()
    (timer.folder / "since-boot.json").write_text('{"fetched": ["train"], "by": "hand"}')
    timer.error = OSError("network down")
    timer.run(THU_0700 + timedelta(minutes=1))
    assert timer.marked


def test_the_first_run_after_an_update_marks_until_each_source_is_due_with_two_redraws_at_most(timer):
    # The Pi has been up for days; the release before kept no record.
    timer.run()
    (timer.folder / "since-boot.json").unlink()
    drawn, calls = timer.drawn, timer.calls
    for minute in range(1, 30):
        timer.run(THU_0700 + timedelta(minutes=minute))
    assert timer.calls - calls == 14  # every 2 minutes, as ever: nothing extra fetched
    assert timer.drawn - drawn <= 2 and not timer.marked

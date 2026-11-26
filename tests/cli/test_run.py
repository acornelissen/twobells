"""The e-paper timer's run on a Pi (cli/run.py's _single), with its runtime
folder, /run/twobells: what it trusts after the Pi starts, and what it keeps
there while the state can't be saved."""

import json
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


# --- 2. "Not updating" marks kept in memory while the state can't be saved ---


def at(hhmm):
    return THU_0700.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))


def train(departs):
    return Departure("train", "Northern", "Manchester Piccadilly", departs, None, Status.ON_TIME)


def failing_into_the_close_on_a_full_card(timer):
    """Trains fetched at 09:18, then the card full and the network down from
    09:20: marked from 09:24 (6 minutes), the window shutting at 09:30."""
    timer.run(at("09:18"))
    timer.full, timer.error = True, OSError("network down")
    for minute in range(20, 30):
        assert timer.run(at(f"09:{minute}")) != 0
    assert timer.marked


def test_a_mark_showing_as_the_window_closes_stays_on_a_card_that_cant_be_saved(timer):
    failing_into_the_close_on_a_full_card(timer)
    drawn = timer.drawn
    for hhmm in ("09:31", "09:32", "09:40", "09:44"):  # before the train goes, at 09:45
        assert timer.run(at(hhmm)) != 0
        assert timer.marked, hhmm
    assert timer.drawn == drawn  # nothing new to draw, as the mark stays
    assert [r.left for r in timer.results[-1].view.section("train").rows] == ["09:45"]


def test_marks_kept_in_memory_go_to_the_card_once_it_saves_and_go_from_memory(timer):
    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert json.loads((timer.folder / "not-updating.json").read_text()) == {
        "train": {"last_success": "2026-10-08T09:18:00"}}
    timer.full = False
    assert timer.run(at("09:40")) == 0 and timer.marked
    assert not (timer.folder / "not-updating.json").exists()  # the card has it now
    assert json.loads(timer.state.read_text())["sources"]["train"]["not_updating"] == {
        "last_success": "2026-10-08T09:18:00"}
    assert timer.run(at("09:41")) == 0 and timer.marked


def test_the_kept_redraw_is_as_a_release_that_knows_nothing_of_the_marks_reads_it(timer):
    # Rolled back, the release before reads last-render.json as ever, and never opens the others.
    from twobells.board.unsaved import KeptRender

    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert sorted(path.name for path in timer.folder.iterdir()) == [
        "last-render.json", "not-updating.json", "since-boot.json"]
    assert set(json.loads((timer.folder / "last-render.json").read_text())) == {"signature", "at", "page"}
    assert KeptRender(timer.folder).read() is not None


def test_a_run_whose_draw_fails_keeps_no_marks_in_memory(timer, monkeypatch):
    from twobells.board.displays.display import PngDisplay

    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert (timer.folder / "not-updating.json").exists()

    def broken(display, image):
        raise OSError("GPIO busy")

    monkeypatch.setattr(PngDisplay, "show", broken)
    (timer.folder / "last-render.json").unlink()  # so it draws
    with pytest.raises(OSError, match="GPIO busy"):
        timer.run(at("09:32"))
    assert not (timer.folder / "not-updating.json").exists()


def test_marks_are_never_kept_in_memory_while_the_card_saves(timer):
    timer.error = OSError("network down")
    for hhmm in ("09:18", "09:26", "09:29", "09:31"):
        assert timer.run(at(hhmm)) == 0
    assert timer.marked
    assert not (timer.folder / "not-updating.json").exists() and not (timer.folder / "last-render.json").exists()


def test_marks_kept_for_a_card_that_has_moved_on_since_are_not_shown(timer):
    # Rolled back to a release that knows nothing of the file, then forward
    # again: the card has a fetch since, which the marks kept were not for.
    from twobells.board.state import departure_to_dict

    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert (timer.folder / "not-updating.json").exists()
    saved = json.loads(timer.state.read_text())
    entry = saved["sources"]["train"]
    entry.update(last_success="2026-10-08T09:35:00", last_attempt="2026-10-08T09:35:00",
                 departures=[departure_to_dict(train("10:15"))])
    entry.pop("not_updating", None)
    timer.state.write_text(json.dumps(saved))
    timer.full = False
    timer.run(at("09:40"))
    assert not timer.marked
    assert [r.left for r in timer.results[-1].view.section("train").rows] == ["10:15"]


@pytest.mark.parametrize("kept", [
    [], {"train": None}, {"train": {"last_success": "09:18"}}, {"train": {"last_success": None, "x": 1}},
    {"train": {"last_success": "2026-10-08T09:18:00+01:00"}}, {"Train": {"last_success": None}},
    {"../state": {"last_success": None}}, {f"s{chr(97 + n)}": {"last_success": None} for n in range(9)},
])
def test_marks_kept_in_memory_of_any_other_shape_are_ignored(timer, kept):
    timer.run(at("09:18"))
    timer.full = True
    (timer.folder / "not-updating.json").write_text(json.dumps(kept))
    timer.run(at("09:31"))
    assert not timer.marked  # the card's own: data fine as the window closed

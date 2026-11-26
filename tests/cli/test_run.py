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
            path.rmdir() if path.is_dir() else path.unlink()


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


def test_a_fetch_whose_state_couldnt_be_saved_nor_kept_in_memory_doesnt_count(timer, monkeypatch):
    # The next run reads the card afresh, and it still has what came before the start.
    timer.run()
    timer.boot()
    timer.full = True
    unwritable(timer, monkeypatch, "unsaved-sources.json", "full")
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


def unwritable(timer, monkeypatch, name, how):
    """`name` in the runtime folder can't be written from now on: a folder in
    its place, or /run full (ENOSPC for that file alone)."""
    import errno

    from twobells.core import files

    if how == "folder":
        (timer.folder / name).unlink(missing_ok=True)
        (timer.folder / name).mkdir()
        return
    real = files.write_atomic

    def full(path, data, mode):
        if path.name == name:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(path, data, mode)

    monkeypatch.setattr(files, "write_atomic", full)


@pytest.mark.parametrize("how", ["folder", "full"])
def test_a_record_that_cant_be_written_after_a_boot_trusts_the_card_as_before(timer, monkeypatch, how, caplog):
    # Without a record that holds, "nothing fetched since" would be read every
    # run: marked, then fetched, then marked again, and outside the windows a
    # fetch for the start every interval.
    timer.run(at("09:18"))
    timer.boot()
    unwritable(timer, monkeypatch, "since-boot.json", how)
    drawn = timer.drawn
    with caplog.at_level("WARNING"):
        for minute in range(30):
            assert timer.run(THU_2100 + timedelta(minutes=minute)) == 0
            assert not timer.marked, minute
    assert timer.calls == 1 and timer.drawn - drawn <= 1
    warned = [r for r in caplog.records if "since the Pi started" in r.getMessage()]
    assert len(warned) == 30  # one line a run, not more


@pytest.mark.parametrize("how", ["folder", "full"])
def test_a_record_that_stops_taking_writes_in_a_window_never_flaps(timer, monkeypatch, how):
    timer.run(at("09:18"))
    timer.boot()
    timer.error = OSError("network down")
    timer.run(THU_2100)  # the boot's try fails: the record names nothing fetched
    unwritable(timer, monkeypatch, "since-boot.json", how)
    timer.error = None
    friday = datetime(2026, 10, 9, 6, 30)
    drawn, calls = timer.drawn, timer.calls
    for minute in range(30):
        timer.run(friday + timedelta(minutes=minute))
        assert not timer.marked, minute
    assert timer.calls - calls == 15 and timer.drawn - drawn <= 1  # every 2 minutes, as ever


@pytest.mark.parametrize("name", ["last-render.json", "unsaved-sources.json"])
@pytest.mark.parametrize("how", ["folder", "full"])
def test_a_file_kept_for_a_card_that_cant_be_saved_that_cant_be_written_does_no_harm(timer, monkeypatch, name, how):
    timer.run(at("09:18"))
    unwritable(timer, monkeypatch, name, how)
    drawn = timer.drawn
    for minute in range(20, 30):  # a working card: what isn't kept isn't needed
        assert timer.run(at(f"09:{minute}")) == 0
    assert timer.drawn == drawn and not timer.marked
    timer.full = True
    for minute in range(31, 40):
        assert timer.run(at(f"09:{minute}")) != 0  # drawn and failing, as off the Pi; never a crash
    # Without what was fetched the redraw is still kept; without the redraw,
    # each run draws, as on a full card with no runtime folder at all.
    assert timer.drawn - drawn <= (1 if name == "unsaved-sources.json" else 9)


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
    kept = json.loads((timer.folder / "unsaved-sources.json").read_text())
    assert kept["train"]["not_updating"] == {"last_success": "2026-10-08T09:18:00"}
    timer.full = False
    assert timer.run(at("09:40")) == 0 and timer.marked
    assert not (timer.folder / "unsaved-sources.json").exists()  # the card has it now
    assert json.loads(timer.state.read_text())["sources"]["train"]["not_updating"] == {
        "last_success": "2026-10-08T09:18:00"}
    assert timer.run(at("09:41")) == 0 and timer.marked


def test_the_kept_redraw_is_as_a_release_that_knows_nothing_of_the_marks_reads_it(timer):
    # Rolled back, the release before reads last-render.json as ever, and never opens the others.
    from twobells.board.unsaved import KeptRender

    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert sorted(path.name for path in timer.folder.iterdir()) == [
        "last-render.json", "since-boot.json", "unsaved-sources.json"]
    assert set(json.loads((timer.folder / "last-render.json").read_text())) == {"signature", "at", "page"}
    assert KeptRender(timer.folder).read() is not None


def test_a_run_whose_draw_fails_keeps_no_marks_in_memory(timer, monkeypatch):
    from twobells.board.displays.display import PngDisplay

    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert (timer.folder / "unsaved-sources.json").exists()

    def broken(display, image):
        raise OSError("GPIO busy")

    monkeypatch.setattr(PngDisplay, "show", broken)
    (timer.folder / "last-render.json").unlink()  # so it draws
    with pytest.raises(OSError, match="GPIO busy"):
        timer.run(at("09:32"))
    assert not (timer.folder / "unsaved-sources.json").exists()


def test_marks_are_never_kept_in_memory_while_the_card_saves(timer):
    timer.error = OSError("network down")
    for hhmm in ("09:18", "09:26", "09:29", "09:31"):
        assert timer.run(at(hhmm)) == 0
    assert timer.marked
    assert not (timer.folder / "unsaved-sources.json").exists() and not (timer.folder / "last-render.json").exists()


def test_what_was_kept_for_a_card_that_has_moved_on_since_is_not_used(timer):
    # Rolled back to a release that knows nothing of the file, then forward
    # again: the card has a fetch since, which what was kept was not for.
    from twobells.board.state import departure_to_dict

    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    assert (timer.folder / "unsaved-sources.json").exists()
    saved = json.loads(timer.state.read_text())
    entry = saved["sources"]["train"]
    entry.update(last_success="2026-10-08T09:35:00", last_attempt="2026-10-08T09:35:00",
                 departures=[departure_to_dict(train("10:15"))])
    entry.pop("not_updating", None)
    timer.state.write_text(json.dumps(saved))
    timer.full = False
    timer.run(at("09:40"))
    assert [r.left for r in timer.results[-1].view.section("train").rows] == ["10:15"]  # the card's
    assert "not_updating" not in json.loads(timer.state.read_text())["sources"]["train"]
    # Nor can this release vouch that the older one's fetch came since the
    # boot: marked, as the network is down, until one of its own works.
    assert timer.marked
    timer.error = None
    timer.run(datetime(2026, 10, 9, 6, 30))
    assert not timer.marked


MARKED = {"not_updating": {"last_success": None}}


@pytest.mark.parametrize("kept", [
    [], {"train": None}, {"train": {"not_updating": {"last_success": "09:18"}}},
    {"train": {"not_updating": {"last_success": None, "x": 1}}},
    {"train": {"not_updating": {"last_success": "2026-10-08T09:18:00+01:00"}}}, {"Train": MARKED},
    {"../state": MARKED}, {f"s{chr(97 + n)}": MARKED for n in range(9)}, {"train": {**MARKED, "x": 1}},
])
def test_marks_kept_in_memory_of_any_other_shape_are_ignored(timer, kept):
    timer.run(at("09:18"))
    timer.full = True
    (timer.folder / "unsaved-sources.json").write_text(json.dumps(kept))
    timer.run(at("09:31"))
    assert not timer.marked  # the card's own: data fine as the window closed


def test_a_card_that_cant_be_saved_keeps_each_source_to_its_interval(timer):
    timer.run(at("07:00"))
    timer.full = True
    drawn = timer.drawn
    for minute in range(1, 21):
        assert timer.run(at("07:00") + timedelta(minutes=minute)) != 0
        assert not timer.marked, minute
    assert timer.calls == 11  # every 2 minutes, not every minute
    assert timer.drawn == drawn


def test_a_fetch_kept_in_memory_counts_as_fetched_since_the_boot(timer):
    timer.run()
    timer.boot()
    timer.full = True
    assert timer.run(THU_0700 + timedelta(minutes=2)) != 0
    timer.error = OSError("network down")
    for minute in (3, 4, 5):
        timer.run(THU_0700 + timedelta(minutes=minute))
        assert not timer.marked, minute  # what it showed at 07:02, kept in memory
    assert timer.calls == 3  # 07:04 was due: it failed, and what came at 07:02 is still fresh


# --- 3. The first run after a boot fetches each free source once, its window open or not ---

THU_2100 = at("21:00")  # the trains' window shut since 09:30


def test_after_a_boot_in_a_closed_window_with_the_network_up_one_fetch_clears_the_mark(timer):
    timer.run(at("09:18"))
    timer.boot()
    drawn = timer.drawn
    assert timer.run(THU_2100) == 0
    assert timer.calls == 2 and not timer.marked
    for minute in range(1, 60):
        timer.run(THU_2100 + timedelta(minutes=minute))
    assert timer.calls == 2 and not timer.marked  # once, not every minute
    assert timer.drawn - drawn <= 1


def test_after_a_boot_in_a_closed_window_with_the_network_down_it_tries_once_and_waits_for_the_window(timer):
    timer.run(at("09:18"))
    timer.boot()
    timer.error = OSError("network down")
    for minute in range(60):
        timer.run(THU_2100 + timedelta(minutes=minute))
        assert timer.marked, minute
    assert timer.calls == 2  # the one try for the boot
    timer.error = None
    timer.run(datetime(2026, 10, 9, 6, 30))  # Friday's window opens
    assert timer.calls == 3 and not timer.marked


def test_after_a_boot_with_a_wrong_clock_saying_the_window_is_shut_and_the_network_down_it_tries_once(timer):
    # Really 07:30, in the window; the clock without a battery says 06:00, before it.
    timer.run(at("06:30"))
    timer.boot()
    timer.error = OSError("network down")
    for minute in range(25):
        timer.run(at("06:00") + timedelta(minutes=minute))
        assert timer.marked, minute
    assert timer.calls == 2
    timer.run(at("06:30"))  # its window, by the clock: as ever, every 2 minutes, with the mark
    assert timer.calls == 3 and timer.marked


def test_after_a_boot_in_a_closed_window_on_a_full_card_it_tries_once_only(timer):
    timer.run(at("09:18"))
    timer.boot()
    timer.full = True
    assert timer.run(THU_2100) != 0
    assert timer.calls == 2 and not timer.marked  # this run shows what it fetched
    for minute in range(1, 30):
        timer.run(THU_2100 + timedelta(minutes=minute))
    assert timer.calls == 2 and not timer.marked  # what it fetched is kept in memory


def test_the_first_run_after_an_update_outside_the_windows_fetches_once_and_redraws_once_at_most(timer):
    timer.run(at("09:18"))
    (timer.folder / "since-boot.json").unlink()  # the release before kept no record
    drawn = timer.drawn
    for minute in range(30):
        timer.run(THU_2100 + timedelta(minutes=minute))
    assert timer.calls == 2 and not timer.marked and timer.drawn - drawn <= 1


def test_after_a_boot_in_a_closed_window_transportapi_spends_nothing_and_stays_marked(timer, monkeypatch, tmp_path):
    from twobells.sources.transportapi import TransportApiBusSource

    lookups = []
    monkeypatch.setattr(TransportApiBusSource, "fetch_raw",
                        lambda source, atco: lookups.append(atco) or {"departures": {"all": []}})
    (tmp_path / "config.toml").write_text(
        f'state_file = "{timer.state}"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
        '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "atco-a"\n')
    (tmp_path / "secrets.env").write_text("RDM_API_KEY=k\nTAPI_APP_ID=id\nTAPI_APP_KEY=key\n")
    timer.run(at("07:30"))
    assert lookups == ["atco-a"]
    timer.boot()
    for minute in range(30):
        timer.run(THU_2100 + timedelta(minutes=minute))
        view = timer.results[-1].view
        assert view.section("bus").stale and not view.section("train").stale, minute
    assert lookups == ["atco-a"] and timer.calls == 2  # the trains' one fetch for the boot; no bus lookup
    assert json.loads(timer.state.read_text())["budgets"]["bus"]["used"] == 1
    timer.run(datetime(2026, 10, 9, 7, 0))  # Friday, the bus window open
    assert lookups == ["atco-a", "atco-a"] and not timer.results[-1].view.section("bus").stale


def test_after_a_boot_in_a_closed_window_a_panel_that_fails_every_run_still_tries_once_only(timer, monkeypatch):
    from twobells.board.displays.display import PngDisplay

    timer.run(at("09:18"))
    timer.boot()

    def broken(display, image):
        raise OSError("GPIO busy")

    monkeypatch.setattr(PngDisplay, "show", broken)
    for minute in range(30):
        with pytest.raises(OSError, match="GPIO busy"):
            timer.run(THU_2100 + timedelta(minutes=minute))  # the panel may be half drawn: each run draws
    assert timer.calls == 2  # the one fetch for the boot, not one every interval
    assert json.loads((timer.folder / "since-boot.json").read_text())["tried"] == ["train"]


def test_a_rollback_reads_the_state_as_ever_whatever_the_runtime_folder_holds(timer):
    # A release that knows none of these files reads only state.json, and
    # last-render.json as it wrote it; neither has anything new in it.
    failing_into_the_close_on_a_full_card(timer)
    timer.run(at("09:31"))
    timer.full = False
    timer.run(at("09:32"))
    saved = json.loads(timer.state.read_text())
    assert set(saved) == {"sources", "last_render"}
    assert set(saved["sources"]["train"]) <= {"departures", "last_attempt", "last_success", "last_error",
                                              "not_updating", "messages"}
    record = json.loads((timer.folder / "since-boot.json").read_text())
    assert record == {"fetched": {"train": "2026-10-08T09:18:00"}, "tried": []}

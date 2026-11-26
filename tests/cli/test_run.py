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


def test_a_record_that_takes_the_first_write_but_not_the_last_says_so(timer, monkeypatch, caplog):
    # The next run marks what this one fetched: the log says why.
    import errno

    from twobells.core import files

    timer.run()
    timer.boot()
    real, writes = files.write_atomic, []

    def second_fails(path, data, mode):
        if path.name == "since-boot.json":
            writes.append(1)
            if len(writes) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
        real(path, data, mode)

    monkeypatch.setattr(files, "write_atomic", second_fails)
    with caplog.at_level("WARNING"):
        timer.run(THU_0700 + timedelta(minutes=2))
    assert len(writes) == 2
    warned = [r.getMessage() for r in caplog.records if "since the Pi started" in r.getMessage()]
    assert len(warned) == 1 and "No space left" in warned[0]


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


@pytest.mark.parametrize("card", [None, "{"])  # a fresh install that never saved; a state.json that won't read
@pytest.mark.parametrize("start, calls", [(at("07:00"), 11), (datetime(2026, 10, 10, 12, 0), 1)])  # Thu; Sat
def test_a_card_that_has_never_saved_fetches_everything_once_not_every_minute(timer, card, start, calls):
    # With nothing on the card, the first run fetches every source whatever
    # the windows say; after it, what it fetched is kept in memory.
    if card is not None:
        timer.state.write_text(card)
    timer.full = True
    for minute in range(21):
        assert timer.run(start + timedelta(minutes=minute)) != 0
    assert timer.calls == calls  # every 2 minutes in the window; outside it, the first run's only


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


# --- 4. The forecast on a card that can't be saved ---


class Forecast:
    """Open-Meteo for the timer's board, in the trains' window: rain likely
    from 08:00 that day, or failing."""

    def __init__(self, timer, monkeypatch, tmp_path):
        from twobells.sources.weather import Hour, OpenMeteoSource

        self.calls = 0
        self.error: Exception | None = None

        def fetch(source):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return [Hour(THU_0700.replace(hour=hour), 70 if hour >= 8 else 5, 0.0) for hour in range(24)]

        monkeypatch.setattr(OpenMeteoSource, "fetch", fetch)
        (tmp_path / "config.toml").write_text(
            f'state_file = "{timer.state}"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
            '[weather]\nlat = 53.44\nlon = -1.95\n')


@pytest.fixture
def forecast(timer, monkeypatch, tmp_path):
    return Forecast(timer, monkeypatch, tmp_path)


def rain(timer):
    return timer.results[-1].view.rain


@pytest.mark.parametrize("saved_first", [True, False])  # a card that failed after a save; one that never saved
def test_a_card_that_cant_be_saved_asks_for_the_forecast_at_its_interval_not_every_minute(timer, forecast,
                                                                                          saved_first):
    timer.full = not saved_first
    timer.run(at("07:00"))
    timer.full = True
    for minute in range(1, 60):
        assert timer.run(at("07:00") + timedelta(minutes=minute)) != 0
        assert rain(timer) == "Rain from 08:00 (70%)", minute  # the forecast it fetched, kept in memory
    assert forecast.calls == 2  # 07:00 and 07:30, every 30 minutes
    assert timer.calls == 30  # the trains every 2 minutes, as ever


def test_a_forecast_that_fails_on_a_card_that_cant_be_saved_waits_its_interval_to_try_again(timer, forecast):
    timer.full = True
    forecast.error = OSError("network down")
    for minute in range(60):
        assert timer.run(at("07:00") + timedelta(minutes=minute)) != 0
    assert forecast.calls == 2  # 07:00 and 07:30, not every minute


def test_the_forecast_kept_in_memory_goes_to_the_card_once_it_saves_with_no_second_fetch(timer, forecast):
    timer.full = True
    for minute in range(21):
        timer.run(at("07:00") + timedelta(minutes=minute))
    assert forecast.calls == 1
    kept = json.loads((timer.folder / "unsaved-sources.json").read_text())
    assert kept["weather"]["last_attempt"] == "2026-10-08T07:00:00"
    timer.full = False
    assert timer.run(at("07:21")) == 0
    assert not (timer.folder / "unsaved-sources.json").exists()  # the card has it now
    saved = json.loads(timer.state.read_text())["weather"]
    assert saved["last_attempt"] == saved["last_success"] == "2026-10-08T07:00:00" and len(saved["hours"]) == 24
    for minute in range(22, 30):
        timer.run(at(f"07:{minute}"))
    assert forecast.calls == 1 and rain(timer) == "Rain from 08:00 (70%)"
    timer.run(at("07:30"))
    assert forecast.calls == 2


# --- 5. Bank holidays on a card that can't be saved ---


class GovUk:
    """gov.uk's bank holidays for the timer's runs, counted, or failing, by
    the board's clock (they go by the Pi's own). While the card is full it
    takes nothing: neither the state nor the holidays' cache."""

    def __init__(self, timer, monkeypatch):
        import errno

        from tests.paths import FIXTURES
        from twobells.cli import common
        from twobells.core import files
        from twobells.sources import holidays

        self.calls = 0
        self.error: Exception | None = None
        self.cache = timer.state.parent / holidays.CACHE_NAME
        body = (FIXTURES / "govuk_bank_holidays.json").read_bytes()

        def get_bytes(url, most):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return body

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return timer.now

        real = files.write_atomic

        def write(path, data, mode):
            if timer.full and timer.folder not in path.parents:
                raise OSError(errno.ENOSPC, "No space left on device")
            real(path, data, mode)

        monkeypatch.setattr(holidays, "get_bytes", get_bytes)
        monkeypatch.setattr(common, "datetime", Clock)
        monkeypatch.setattr(files, "write_atomic", write)

    def stale(self):
        """A cache on the card from 8 days ago: due again."""
        then = (THU_0700 - timedelta(days=8)).isoformat()
        self.cache.write_text(json.dumps({"fetched": then, "checked": then, "dates": ["2026-08-31"]}))


@pytest.fixture
def gov_uk(timer, monkeypatch):
    return GovUk(timer, monkeypatch)


@pytest.mark.parametrize("cache", ["missing", "stale"])
@pytest.mark.parametrize("saved_first", [True, False])  # a card that failed after a save; one that never saved
def test_a_card_that_cant_be_saved_asks_gov_uk_for_the_bank_holidays_once_not_every_run(timer, gov_uk, cache,
                                                                                        saved_first):
    if cache == "stale":
        gov_uk.stale()
    if saved_first:
        timer.full, gov_uk.error = True, OSError("network down")  # so the card has no fresh list
        timer.run(THU_0700 - timedelta(hours=7))
        timer.full, gov_uk.error, gov_uk.calls = False, None, 0
        timer.run(THU_0700 - timedelta(hours=7) + timedelta(minutes=1))  # a save that works
        assert gov_uk.calls == 0
    timer.full = True
    for minute in range(30):
        assert timer.run(THU_0700 + timedelta(minutes=minute)) != 0
    assert gov_uk.calls == 1  # the first run's, not one a run
    assert timer.calls - (1 if saved_first else 0) == 15  # the trains every 2 minutes, as ever


def test_a_bank_holiday_check_that_fails_on_a_card_that_cant_be_saved_waits_to_try_again(timer, gov_uk):
    from twobells.sources.holidays import RETRY

    timer.full = True
    gov_uk.error = OSError("network down")
    for minute in range(0, 6 * 60, 5):
        timer.run(THU_0700 + timedelta(minutes=minute))
    assert gov_uk.calls == 1  # not every run
    timer.run(THU_0700 + RETRY)
    assert gov_uk.calls == 2


def test_bank_holidays_kept_in_memory_go_to_the_card_once_it_saves_with_no_second_fetch(timer, gov_uk):
    timer.full = True
    for minute in range(5):
        timer.run(THU_0700 + timedelta(minutes=minute))
    assert gov_uk.calls == 1 and not gov_uk.cache.exists()
    kept = json.loads((timer.folder / "unsaved-sources.json").read_text())
    assert kept["holidays"]["checked"] == kept["holidays"]["fetched"] == "2026-10-08T07:00:00"
    timer.full = False
    assert timer.run(THU_0700 + timedelta(minutes=5)) == 0
    assert not (timer.folder / "unsaved-sources.json").exists()  # the card has it now
    assert json.loads(gov_uk.cache.read_text())["checked"] == "2026-10-08T07:00:00"
    for minute in range(6, 30):
        timer.run(THU_0700 + timedelta(minutes=minute))
    assert gov_uk.calls == 1


def test_bank_holidays_kept_in_memory_still_shut_the_board_s_windows(timer, gov_uk):
    # Fetched on the Friday with the card full; the Monday is the August bank
    # holiday, when the trains' Mon-Fri window is shut, and gov.uk is down.
    timer.full = True
    timer.run(datetime(2026, 8, 28, 7, 0))
    assert gov_uk.calls == 1 and timer.calls == 1
    gov_uk.error = OSError("network down")
    for minute in range(10):
        timer.run(datetime(2026, 8, 31, 7, minute))
    assert gov_uk.calls == 1 and timer.calls == 1  # no trains on a bank holiday


# --- 6. The bus timetable's live positions in a city, where one box's answer is too big ---


class City:
    """BODS for the timer's BODS bus, by the board's clock: an answer for the
    one box round the stops is over the cap (AnswerTooBig), a small box's has
    no buses. Counts the boxes asked for."""

    def __init__(self, timer, monkeypatch, tmp_path):
        from twobells.sources import bodsbus
        from twobells.sources.bods import AnswerTooBig
        from twobells.timetable import build as timetable
        from twobells.timetable.model import Calendar, Call, Journey, Schedule, StopPosition

        city = self
        self.boxes: list[tuple[float, float, float, float]] = []

        class Vehicles:
            def __init__(self, api_key, box, lines, base_url, http_get, now):
                self.box = box

            def fetch(self, max_bytes=0):
                city.boxes.append(self.box)
                if city.big(self.box):
                    raise AnswerTooBig("BODS's answer for the box was over 2,000,000 bytes")
                return []

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return timer.now.replace(tzinfo=tz)

        monkeypatch.setattr(bodsbus, "BodsVehicleSource", Vehicles)
        monkeypatch.setattr(bodsbus, "datetime", Clock)
        # Henry Street, Glossop, with stops before it 5 km round: one box of 12 by 13 km.
        stops = {f"G{n}": StopPosition(f"Way {n}", 53.44384 + lat, -1.948999 + lon)
                 for n, (lat, lon) in enumerate([(-0.05, -0.05), (0.05, -0.05), (-0.05, 0.05), (0.05, 0.05)])}
        henry = StopPosition("Henry Street", 53.44384, -1.948999)
        journey = Journey("341", "BNGN", "Hadfield", "4173", (*(Call(atco, 600) for atco in stops),
                                                             Call("1000DGHS0900", 610)), Calendar(frozenset(range(7))))
        timetable.save(timer.state.parent, Schedule(datetime(2026, 10, 5, 3, 0), None, {"A": "1000DGHS0900"},
                                                    (journey,), {**stops, "1000DGHS0900": henry}))
        (tmp_path / "config.toml").write_text(
            f'state_file = "{timer.state}"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
            '[bus]\nsource = "bods"\noperators = ["BNML"]\nwindows = ["Mon-Sun 06:00-23:00"]\n'
            '[bus.stops]\nA = "1000DGHS0900"\n')
        (tmp_path / "secrets.env").write_text("RDM_API_KEY=k\nBODS_API_KEY=b\n")

    @staticmethod
    def big(box) -> bool:
        from twobells.sources.bodsbus import SMALL_BOX_DEGREES

        west, south, east, north = box
        return north - south > SMALL_BOX_DEGREES[0] + 1e-9

    @property
    def big_asks(self) -> int:
        return sum(1 for box in self.boxes if self.big(box))


@pytest.fixture
def city(timer, monkeypatch, tmp_path):
    return City(timer, monkeypatch, tmp_path)


@pytest.mark.parametrize("full", [False, True])
def test_the_timer_asks_for_a_box_found_too_big_once_in_6_hours_not_every_check(timer, city, full):
    from twobells.sources.bodsbus import BIG_BOX_RETRY

    timer.full = full
    for minutes in range(0, 7 * 60, 10):
        timer.run(THU_0700 + timedelta(minutes=minutes))
    assert len({box for box in city.boxes if not city.big(box)}) > 1  # small boxes every check
    assert city.big_asks == 2  # 07:00, then once 6 hours on
    assert at("13:00") == THU_0700 + BIG_BOX_RETRY


def test_the_box_found_too_big_is_saved_in_the_bus_entry_and_kept_in_memory_on_a_full_card(timer, city):
    timer.run(at("07:00"))
    saved = json.loads(timer.state.read_text())["sources"]["bus"]["big_box"]
    assert saved["at"] == "2026-10-08T07:00:00" and len(saved["box"]) == 4
    timer.full = True
    timer.run(at("07:02"))
    assert json.loads((timer.folder / "unsaved-sources.json").read_text())["bus"]["big_box"] == saved
    timer.full = False
    timer.run(at("07:04"))
    assert json.loads(timer.state.read_text())["sources"]["bus"]["big_box"] == saved
    assert city.big_asks == 1


def test_a_state_from_before_the_box_was_saved_asks_for_it_once(timer, city):
    timer.state.write_text(json.dumps({"sources": {"bus": {"last_attempt": "2026-10-08T06:50:00"}}}))  # 0.6.0's
    for minute in range(0, 30, 2):
        timer.run(THU_0700 + timedelta(minutes=minute))
    assert city.big_asks == 1


def test_a_clock_gone_back_past_the_box_found_too_big_asks_for_it_whole(timer, city):
    timer.run(at("08:00"))
    timer.run(at("07:00"))  # a Pi with no clock battery, say
    assert city.big_asks == 2
    timer.run(at("07:02"))
    assert city.big_asks == 2  # and found too big again, at 07:00


def test_a_box_found_too_big_for_other_stops_asks_for_this_one_whole(timer, city):
    timer.run(at("07:00"))
    saved = json.loads(timer.state.read_text())
    saved["sources"]["bus"]["big_box"]["box"][3] += 0.01  # the timetable since rebuilt with another stop, say
    timer.state.write_text(json.dumps(saved))
    timer.run(at("07:02"))
    assert city.big_asks == 2


HOSTILE_BOXES = [
    None, [], {"box": [-2.0, 53.39, -1.9, 53.5]}, {"box": "x", "at": "2026-10-08T07:00:00"},
    {"box": [-2.0, 53.39, -1.9], "at": "2026-10-08T07:00:00"},
    {"box": [-1.9, 53.39, -2.0, 53.5], "at": "2026-10-08T07:00:00"},
    {"box": [-2.0, 53.39, -1.9, 95.0], "at": "2026-10-08T07:00:00"},
    {"box": [-2.0, 53.39, -1.9, 53.5], "at": "9999-12-31T23:59:00"},
    {"box": [-2.0, 53.39, -1.9, 53.5], "at": "2026-10-08T07:00:00+01:00"},
    {"box": [-2.0, 53.39, -1.9, 53.5], "at": "2026-10-08T07:00:00", "x": 1},
]


@pytest.mark.parametrize("hostile", HOSTILE_BOXES)
def test_a_box_found_too_big_saved_of_any_other_shape_is_none(timer, city, hostile):
    timer.run(at("07:00"))
    saved = json.loads(timer.state.read_text())
    saved["sources"]["bus"]["big_box"] = hostile
    timer.state.write_text(json.dumps(saved))
    assert timer.run(at("07:02")) == 0
    assert city.big_asks == 2  # asked for whole, as with nothing remembered
    assert json.loads(timer.state.read_text())["sources"]["bus"]["big_box"]["at"] == "2026-10-08T07:02:00"


@pytest.mark.parametrize("hostile", HOSTILE_BOXES)
def test_a_box_found_too_big_kept_in_memory_of_any_other_shape_is_none(timer, city, hostile):
    timer.full = True
    timer.run(at("07:00"))
    kept = json.loads((timer.folder / "unsaved-sources.json").read_text())
    kept["bus"]["big_box"] = hostile
    (timer.folder / "unsaved-sources.json").write_text(json.dumps(kept))
    assert timer.run(at("07:02")) != 0
    assert city.big_asks == 2


# --- 7. A running screen (--loop) that systemd restarts on a card that can't be saved ---


class Screen:
    """twobells-screen.service's runs, on the timer's board, card and clock:
    each start runs `ticks` ticks of a second, its clock moving on a second
    a tick, then stops, as a fault would stop it for systemd to restart.
    Counts the writes of what is kept in memory."""

    def __init__(self, timer, monkeypatch):
        from twobells.cli import run
        from twobells.core import files

        self.timer = timer
        self.writes = 0
        real = files.write_atomic

        def write(path, data, mode):
            if path.name == "unsaved-sources.json":
                self.writes += 1
            real(path, data, mode)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                moment = timer.now.replace(tzinfo=tz)
                timer.now += timedelta(seconds=1)
                return moment

        monkeypatch.setattr(files, "write_atomic", write)
        monkeypatch.setattr(run, "datetime", Clock)
        monkeypatch.setattr(run, "sleep_to_the_tick", lambda seconds: None)

    def start(self, at_time, ticks=1) -> int:
        self.timer.now = at_time
        return main([*self.timer.args, "--loop", "--tick", "1", "--ticks", str(ticks)])


@pytest.fixture
def screen(timer, monkeypatch):
    return Screen(timer, monkeypatch)


@pytest.mark.parametrize("saved_first", [True, False])  # a card that failed after a save; one that never saved
def test_a_screen_restarted_over_and_over_on_a_card_that_cant_be_saved_keeps_each_source_to_its_interval(
        timer, screen, saved_first):
    timer.full = not saved_first
    screen.start(THU_0700)
    timer.full = True
    for minute in range(1, 21):  # systemd starting it again each minute, after each fault
        screen.start(THU_0700 + timedelta(minutes=minute))
    assert timer.calls == 11  # every 2 minutes, not at every start


def test_what_a_screen_keeps_in_memory_goes_once_the_card_saves(timer, screen):
    timer.full = True
    screen.start(THU_0700)
    assert (timer.folder / "unsaved-sources.json").exists()
    timer.full = False
    screen.start(THU_0700 + timedelta(minutes=1))
    assert not (timer.folder / "unsaved-sources.json").exists()  # the card has it
    assert json.loads(timer.state.read_text())["sources"]["train"]["last_success"] == "2026-10-08T07:00:00"
    screen.start(THU_0700 + timedelta(minutes=1, seconds=30))
    assert timer.calls == 1


def test_a_screen_on_a_card_that_cant_be_saved_keeps_what_was_fetched_only_as_it_changes(timer, screen):
    timer.full = True
    assert screen.start(THU_0700, ticks=600) == 0  # ten minutes
    assert timer.calls == 6  # 07:00, then every 2 minutes less the slack
    assert 1 <= screen.writes <= timer.calls  # as each fetch changes it; never every tick, nor as it stops


def test_a_screen_whose_card_saves_keeps_nothing_in_memory(timer, screen):
    for minute in range(3):
        screen.start(THU_0700 + timedelta(minutes=minute), ticks=5)
    assert screen.writes == 0 and not (timer.folder / "unsaved-sources.json").exists()
    assert timer.calls == 2


def test_a_screen_keeps_neither_its_last_redraw_nor_a_note_of_what_it_fetched_since_the_pi_started(timer, screen):
    # A screen starts blank and counts from its own start: those are the e-paper timer's alone.
    timer.full = True
    screen.start(THU_0700)
    assert sorted(path.name for path in timer.folder.iterdir()) == ["unsaved-sources.json"]


def test_a_screen_restarted_on_a_card_that_cant_be_saved_asks_gov_uk_once(timer, screen, gov_uk):
    timer.full = True
    for minute in range(10):
        screen.start(THU_0700 + timedelta(minutes=minute))
    assert gov_uk.calls == 1

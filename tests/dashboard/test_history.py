import sqlite3
import stat
import threading
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from twobells.dashboard.history import (
    FILE_NAME,
    Following,
    History,
    Measures,
    Observed,
    Report,
    Stats,
    Watched,
    day_type,
    measure,
    no_show_phrase,
    phrase,
)

LONDON = ZoneInfo("Europe/London")
FRIDAY = date(2026, 10, 9)
TRAIN = "train|GLO>MAN|Manchester Piccadilly"
BUS = "bus|A|341|Hyde"


def _at(hhmm, day=FRIDAY, fold=0):
    """`hhmm` on `day` in London; `fold` 1 for the second pass of the hour the clocks go back."""
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, fold=fold, tzinfo=LONDON)


def _train(scheduled="07:42", day=FRIDAY, status="on_time", delay=0, expected=None, route=TRAIN, reason=None,
           fold=0):
    leaves = _at(scheduled, day, fold)
    return Observed(route, leaves, expected or leaves, status, delay, True, "Northern",
                    "Manchester Piccadilly", "1", reason, "C12345")


def _bus(scheduled="07:20", day=FRIDAY, status="on_time", delay=0, expected=None, live=True):
    leaves = _at(scheduled, day)
    return Observed(BUS, leaves, expected or leaves, status, delay, live, "341", "Hyde", None, None, "vj-1")


def _rows(folder, name=FILE_NAME):
    """Every row as stored, final or not, read beside the History's own connection."""
    with sqlite3.connect(folder / name) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM outcome ORDER BY day, scheduled")]


@pytest.fixture
def history(tmp_path):
    history = History(tmp_path)
    yield history
    history.close()


# Deciding outcomes.

def test_not_final_while_still_on_the_board(history, tmp_path):
    history.observe("train", [_train(status="late", delay=3, expected=_at("07:45"))], _at("07:40"))
    history.observe("train", [_train(status="late", delay=3, expected=_at("07:45"))], _at("07:44"))
    history.observe("train", [_train(status="late", delay=3, expected=_at("07:45"))], _at("07:46"))
    assert history.recent("train", TRAIN, "07:42") == []
    [row] = _rows(tmp_path)
    assert (row["final"], row["first_seen"], row["last_seen"]) == (0, "2026-10-09T06:40:00+00:00", "2026-10-09T06:46:00+00:00")


def test_final_after_it_drops_off_at_its_time(history):
    history.observe("train", [_train(status="late", delay=4, expected=_at("07:46"), reason="A signal fault")],
                    _at("07:44"))
    history.observe("train", [], _at("07:45"))  # within a minute of 07:46
    assert history.recent("train", TRAIN, "07:42") == [
        {"day": "2026-10-09", "status": "late", "delay_min": 4, "live": True, "reason": "A signal fault"},
    ]


def test_not_final_when_it_drops_off_early(history, tmp_path):
    history.observe("train", [_train()], _at("07:30"))
    history.observe("train", [_train(scheduled="07:50")], _at("07:35"))  # pushed off, or filtered out
    assert history.recent("train", TRAIN, "07:42") == []
    assert [row["final"] for row in _rows(tmp_path)] == [0, 0]


def test_a_departure_missing_from_a_poll_of_the_other_mode_is_left_alone(history, tmp_path):
    history.observe("train", [_train()], _at("07:41"))
    history.observe("bus", [], _at("07:43"))
    assert [row["final"] for row in _rows(tmp_path)] == [0]


def test_cancelled_at_last_sight_is_a_cancelled_outcome(history):
    history.observe("train", [_train()], _at("07:30"))
    history.observe("train", [_train(status="cancelled", delay=None, reason="A shortage of crew")], _at("07:40"))
    history.observe("train", [], _at("07:43"))
    [outcome] = history.recent("train", TRAIN, "07:42")
    assert (outcome["status"], outcome["delay_min"], outcome["reason"]) == ("cancelled", None, "A shortage of crew")


def test_deleted_if_nobody_saw_it_near_its_time(history, tmp_path):
    history.observe("train", [_train()], _at("07:36"))  # six minutes before
    history.observe("train", [], _at("07:43"))
    assert _rows(tmp_path) == []


def test_kept_if_seen_just_within_the_window(history):
    history.observe("train", [_train()], _at("07:37"))  # five minutes before
    history.observe("train", [], _at("07:43"))
    assert len(history.recent("train", TRAIN, "07:42")) == 1


def test_buses_need_watching_closer_to_their_time(history, tmp_path):
    history.observe("bus", [_bus()], _at("07:16"))  # four minutes before: fine for a train, not a bus
    history.observe("bus", [], _at("07:21"))
    assert _rows(tmp_path) == []
    history.observe("bus", [_bus(scheduled="07:40")], _at("07:37"))
    history.observe("bus", [], _at("07:41"))
    assert len(history.recent("bus", BUS, "07:40")) == 1


def test_sweep_after_a_restart(tmp_path):
    first = History(tmp_path)
    delayed = _train(status="late", delay=None, expected=_at("07:42"))  # "Delayed", no time
    later = _train(scheduled="08:42")  # an hour off: never watched near its time
    first.observe("train", [delayed, later], _at("07:41"))
    first.close()
    again = History(tmp_path)  # the board was off while both left
    again.sweep(_at("08:11"))
    assert again.recent("train", TRAIN, "07:42") == []  # not yet half an hour stale
    again.sweep(_at("08:13"))
    assert again.recent("train", TRAIN, "07:42") == [
        {"day": "2026-10-09", "status": "late", "delay_min": None, "live": True, "reason": None},
    ]
    again.sweep(_at("09:13"))
    assert [row["scheduled"] for row in _rows(tmp_path)] == ["07:42"]  # 08:42 deleted, unknown
    again.close()


def test_upserts_are_idempotent(history, tmp_path):
    for _ in range(3):
        history.observe("train", [_train(status="late", delay=2, expected=_at("07:44"))], _at("07:40"))
    [row] = _rows(tmp_path)
    assert (row["status"], row["delay_min"], row["departs_at"]) == ("late", 2, "2026-10-09T06:44:00+00:00")


def test_final_rows_never_change(history, tmp_path):
    history.observe("train", [_train()], _at("07:41"))
    history.observe("train", [], _at("07:43"))
    before = _rows(tmp_path)
    history.observe("train", [_train(status="cancelled", delay=None)], _at("07:44"))  # it shows up again
    history.sweep(_at("12:00"))
    assert _rows(tmp_path) == before
    assert before[0]["final"] == 1


def test_purge_forgets_after_90_days(history, tmp_path):
    for day in (FRIDAY - timedelta(days=91), FRIDAY - timedelta(days=90), FRIDAY):
        history.observe("train", [_train(day=day)], _at("07:41", day))
        history.observe("train", [], _at("07:43", day))
    history.purge(_at("12:00"))
    assert [row["day"] for row in _rows(tmp_path)] == ["2026-07-11", "2026-10-09"]


def test_midnight_keeps_the_scheduled_day(history):
    late = _train(scheduled="23:55", status="late", delay=15, expected=_at("00:10", FRIDAY + timedelta(days=1)))
    history.observe("train", [late], _at("23:58"))
    history.observe("train", [late], _at("00:05", FRIDAY + timedelta(days=1)))
    history.observe("train", [], _at("00:12", FRIDAY + timedelta(days=1)))
    assert history.recent("train", TRAIN, "23:55") == [
        {"day": "2026-10-09", "status": "late", "delay_min": 15, "live": True, "reason": None},
    ]
    assert history.stats("train", TRAIN, "23:55", "weekday", late_minutes=3).late == 1


def test_the_hour_the_clocks_go_back_keeps_both_passes(history, tmp_path):
    # 25 October 2026: 01:00 to 01:59 comes twice, in BST and then in GMT (fold 1). The 01:30 of
    # each pass is a departure of its own, and what has left goes by the time that passed.
    sunday = date(2026, 10, 25)
    history.observe("train", [_train("01:30", sunday), _train("01:45", sunday)], _at("01:29", sunday))
    history.observe("train", [_train("01:45", sunday)], _at("01:31", sunday))  # the first 01:30 has left
    history.observe("train", [_train("01:45", sunday)], _at("01:44", sunday))
    # 02:00 BST is 01:00 GMT: the first 01:45 left while nobody looked, a minute after it was seen.
    again = _train("01:30", sunday, "late", 5, _at("01:35", sunday, fold=1), fold=1)
    history.observe("train", [again], _at("01:00", sunday, fold=1))
    assert [outcome["status"] for outcome in history.recent("train", TRAIN, "01:45")] == ["on_time"]
    history.observe("train", [again], _at("01:34", sunday, fold=1))
    history.observe("train", [], _at("01:36", sunday, fold=1))
    assert sorted((o["day"], o["status"], o["delay_min"]) for o in history.recent("train", TRAIN, "01:30")) == [
        ("2026-10-25", "late", 5), ("2026-10-25", "on_time", 0)]
    assert sorted((row["scheduled"], row["leaves"]) for row in _rows(tmp_path) if row["scheduled"] == "01:30") == [
        ("01:30", "2026-10-25T00:30:00+00:00"), ("01:30", "2026-10-25T01:30:00+00:00")]
    assert day_type(sunday) == "sunday"


def test_left_and_unwatched_go_by_the_time_that_passed(history, tmp_path):
    # 29 March 2026: 01:00 GMT is 02:00 BST. Seen at 00:59 GMT, the 02:03 BST leaves 4 minutes later:
    # watched, not an hour and 4 minutes before it left.
    sunday = date(2026, 3, 29)
    history.observe("train", [_train("02:03", sunday)], _at("00:59", sunday))
    history.observe("train", [], _at("02:04", sunday))
    assert [outcome["status"] for outcome in history.recent("train", TRAIN, "02:03")] == ["on_time"]


def test_observe_from_two_threads(history, tmp_path):
    errors = []

    def poll(mode, make):
        try:
            for minute in range(60):
                polled_at = _at("06:00") + timedelta(minutes=minute)
                rows = [make(scheduled=f"{6 + (minute + ahead) // 60:02d}:{(minute + ahead) % 60:02d}")
                        for ahead in range(1, 4)]
                history.observe(mode, rows, polled_at)
                history.stats(mode, TRAIN if mode == "train" else BUS, "06:30", "weekday", late_minutes=3)
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=poll, args=("train", _train)), threading.Thread(target=poll, args=("bus", _bus))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(history.recent("train", TRAIN, "06:30")) == 1
    assert len(history.recent("bus", BUS, "06:30")) == 1


# Keeping it.

def test_the_file_is_owner_and_group_only(tmp_path):
    (tmp_path / FILE_NAME).touch(mode=0o666)
    history = History(tmp_path)
    history.observe("train", [_train()], _at("07:40"))
    for name in (FILE_NAME, FILE_NAME + "-wal", FILE_NAME + "-shm"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o640, name
    history.close()


def test_the_schema(tmp_path):
    History(tmp_path).close()
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"outcome", "arrival"}
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO outcome (mode, day, scheduled, leaves, route, service, destination, status, live,"
                       " first_seen, last_seen, departs_at) VALUES ('tram', '', '', '', '', '', '', '', 0, '', '', '')")


def test_refuses_a_newer_schema(tmp_path):
    History(tmp_path).close()
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        db.execute("PRAGMA user_version=4")
    with pytest.raises(sqlite3.DatabaseError, match="version 4"):
        History(tmp_path)


# Read by another process: the web server on a Pi, while the fetch service writes.


def test_a_reader_reads_what_the_writer_wrote_while_it_writes(history, tmp_path):
    reader = History.reader(tmp_path)
    history.observe("train", [_train(status="late", delay=4, expected=_at("07:46"))], _at("07:44"))
    history.observe("train", [], _at("07:45"))
    assert reader.recent("train", TRAIN, "07:42") == history.recent("train", TRAIN, "07:42") != []
    reader.close()


def test_a_reader_never_writes(history, tmp_path):
    reader = History.reader(tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.observe("train", [_train()], _at("07:40"))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.purge(_at("07:40"), 0)
    reader.close()


def test_a_reader_makes_nothing_where_there_is_no_history(tmp_path):
    assert History.reader(tmp_path) is None
    assert History.reader(tmp_path / "missing") is None
    assert list(tmp_path.iterdir()) == []


def test_a_reader_opens_only_a_set_up_plain_file(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    History(other).close()
    (tmp_path / FILE_NAME).symlink_to(other / FILE_NAME)
    assert History.reader(tmp_path) is None  # never through a link
    (tmp_path / FILE_NAME).unlink()
    sqlite3.connect(tmp_path / FILE_NAME).close()  # an empty file: the writer hasn't set it up yet
    assert History.reader(tmp_path) is None
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)  # and still untouched


def test_the_history_reader_opens_it_once_it_is_there(tmp_path):
    from twobells.dashboard.history import HistoryReader

    reader = HistoryReader(tmp_path)
    assert reader.recent("train", TRAIN, "07:42") == []
    assert not (tmp_path / FILE_NAME).exists()
    history = History(tmp_path)
    history.observe("train", [_train(status="cancelled")], _at("07:40"))
    history.observe("train", [], _at("07:41"))
    assert reader.recent("train", TRAIN, "07:42")[0]["status"] == "cancelled"
    reader.close()
    history.close()


@pytest.mark.parametrize("change", [
    {"status": "early"},
    {"leaves": "07:42"},
    {"leaves": datetime(2026, 10, 9, 7, 42)},  # naive: in no zone
    {"delay_min": "3"},
    {"departs_at": datetime(2026, 10, 9, 7, 42)},
])
def test_observe_refuses_nonsense_and_writes_nothing(history, tmp_path, change):
    with pytest.raises(ValueError):
        history.observe("train", [_train(scheduled="07:00"), _train()._replace(**change)], _at("07:40"))
    assert _rows(tmp_path) == []


def test_observe_refuses_an_unknown_mode(history):
    with pytest.raises(ValueError, match="mode"):
        history.observe("tram", [], _at("07:40"))


def test_long_text_is_cut_short(history, tmp_path):
    history.observe("train", [_train(reason="x" * 5000)], _at("07:40"))
    assert len(_rows(tmp_path)[0]["reason"]) == 200


# Stats and words.

def test_day_types():
    assert day_type(FRIDAY) == "weekday"
    assert day_type(date(2026, 10, 10)) == "saturday"
    assert day_type(date(2026, 10, 11)) == "sunday"
    assert day_type(date(2026, 8, 31)) == "weekday"
    assert day_type(date(2026, 8, 31), frozenset({date(2026, 8, 31)})) == "sunday"  # a bank holiday Monday


def _record(history, day, status="on_time", delay=0, scheduled="07:42"):
    expected = _at(scheduled, day) + timedelta(minutes=delay or 0)
    history.observe("train", [_train(scheduled, day, status, delay, expected)], expected - timedelta(minutes=1))
    history.observe("train", [], expected + timedelta(minutes=1))


def test_stats_count_the_last_n_matching_days(history):
    days = [FRIDAY - timedelta(days=back) for back in range(28)]
    for day in days:
        _record(history, day, delay=8 if day.weekday() == 0 else 0)  # late every Monday
    # 20 weekdays: the last 10 have two Mondays.
    assert history.stats("train", TRAIN, "07:42", "weekday", late_minutes=3) == Stats(10, 2, 0, 0, 8, 8.0, 8)
    assert history.stats("train", TRAIN, "07:42", "saturday", late_minutes=3).days == 4
    assert history.stats("train", TRAIN, "07:42", "weekday", n=3, late_minutes=3).days == 3


def test_stats_with_fewer_than_n_days(history):
    _record(history, FRIDAY, delay=6)
    _record(history, FRIDAY - timedelta(days=1), delay=2)  # under the late threshold
    _record(history, FRIDAY - timedelta(days=2), status="cancelled", delay=None)
    _record(history, FRIDAY - timedelta(days=3), status="late", delay=None)  # "Delayed", no time
    _record(history, FRIDAY - timedelta(days=4), delay=10)  # Monday
    assert history.stats("train", TRAIN, "07:42", "weekday", late_minutes=3) == Stats(5, 2, 1, 1, 1, 8.0, 10)
    assert history.stats("train", TRAIN, "07:42", "sunday", late_minutes=3) == Stats(0, 0, 0, 0, 0, None, None)


def test_a_bank_holiday_counts_as_a_sunday(history):
    _record(history, date(2026, 8, 31), delay=9)  # Summer bank holiday
    _record(history, date(2026, 8, 30))  # Sunday
    holidays = frozenset({date(2026, 8, 31)})
    sundays = history.stats("train", TRAIN, "07:42", "sunday", late_minutes=3, holidays=holidays)
    weekdays = history.stats("train", TRAIN, "07:42", "weekday", late_minutes=3, holidays=holidays)
    assert (sundays.days, sundays.late, weekdays.days) == (2, 1, 0)


def test_stats_ignore_undecided_days_and_other_routes(history):
    _record(history, FRIDAY, delay=5)
    _record(history, FRIDAY, delay=5, scheduled="08:42")
    history.observe("train", [_train(day=FRIDAY + timedelta(days=3))], _at("07:40", FRIDAY + timedelta(days=3)))
    stats = history.stats("train", TRAIN, "07:42", "weekday", late_minutes=3)
    assert (stats.days, stats.late) == (1, 1)


@pytest.mark.parametrize("stats, kind, words", [
    (Stats(10, 4, 0, 0, 6, 6.4, 12), "weekday", "Late 4 of the last 10 weekdays (avg 6 min)"),
    (Stats(10, 1, 0, 0, 9, 5.5, 6), "weekday", "Late 1 of the last 10 weekdays (avg 6 min)"),
    (Stats(7, 0, 0, 0, 7, None, None), "saturday", "On time the last 7 Saturdays"),
    (Stats(10, 2, 1, 0, 7, 4.0, 5), "weekday", "Cancelled 1 and late 2 of the last 10 weekdays"),
    (Stats(4, 0, 1, 0, 3, None, None), "sunday", "Cancelled 1 of the last 4 Sundays"),
    (Stats(5, 0, 0, 1, 4, None, None), "weekday", "On time 4 of the last 5 weekdays"),
    (Stats(3, 0, 0, 0, 3, None, None), "weekday", "On time the last 3 weekdays"),
    (Stats(2, 2, 0, 0, 0, 9.0, 10), "weekday", None),
    (Stats(0, 0, 0, 0, 0, None, None), "sunday", None),
    (Stats(3, 0, 0, 3, 0, None, None), "weekday", None),  # nothing known
])
def test_phrase(stats, kind, words):
    assert phrase(stats, kind) == words


def test_recent_is_newest_first_and_limited(history):
    for back in range(5):
        _record(history, FRIDAY - timedelta(days=back))
    assert [outcome["day"] for outcome in history.recent("train", TRAIN, "07:42", days=3)] == [
        "2026-10-09", "2026-10-08", "2026-10-07",
    ]


# Days without a live tracker, for the ghost bus warning.


def _record_bus(history, day, live, scheduled="07:20"):
    """A bus seen a minute before its time, with or without a tracker, then gone."""
    status, delay = ("on_time", 0) if live else ("unknown", None)
    history.observe("bus", [_bus(scheduled, day, status, delay, live=live)], _at(scheduled, day) - timedelta(minutes=1))
    history.observe("bus", [], _at(scheduled, day) + timedelta(minutes=1))


def test_no_shows_count_final_days_without_a_live_tracker(history):
    days = [FRIDAY - timedelta(days=back) for back in range(28)]
    for day in days:
        _record_bus(history, day, live=day.weekday() not in (0, 2))  # no tracker Mondays and Wednesdays
    # The last 10 weekdays hold two Mondays and two Wednesdays.
    assert history.no_shows("bus", BUS, "07:20", "weekday") == (4, 10)
    assert history.no_shows("bus", BUS, "07:20", "weekday", n=3) == (1, 3)  # Fri, Thu, Wed
    assert history.no_shows("bus", BUS, "07:20", "saturday") == (0, 4)
    assert history.no_shows("bus", BUS, "08:20", "weekday") == (0, 0)
    assert history.no_shows("train", BUS, "07:20", "weekday") == (0, 0)  # the other mode


def test_no_shows_ignore_undecided_days_and_follow_bank_holidays(history):
    _record_bus(history, FRIDAY, live=False)
    history.observe("bus", [_bus(day=FRIDAY + timedelta(days=3), status="unknown", delay=None, live=False)],
                    _at("07:00", FRIDAY + timedelta(days=3)))  # Monday, still on the board
    assert history.no_shows("bus", BUS, "07:20", "weekday") == (1, 1)
    _record_bus(history, date(2026, 8, 31), live=False)  # Summer bank holiday
    holidays = frozenset({date(2026, 8, 31)})
    assert history.no_shows("bus", BUS, "07:20", "sunday", holidays=holidays) == (1, 1)
    assert history.no_shows("bus", BUS, "07:20", "weekday", holidays=holidays) == (1, 1)
    with pytest.raises(ValueError):
        history.no_shows("bus", BUS, "07:20", "holiday")
    with pytest.raises(ValueError):
        history.no_shows("tram", BUS, "07:20", "weekday")


@pytest.mark.parametrize("without, days, kind, words", [
    (3, 10, "weekday", "No tracker on 3 of the last 10 weekdays"),
    (1, 4, "saturday", "No tracker on 1 of the last 4 Saturdays"),
    (5, 5, "sunday", "No tracker on any of the last 5 Sundays"),
    (0, 10, "weekday", None),  # always tracked: nothing to say
    (2, 2, "weekday", None),  # too few days to say anything
])
def test_no_show_phrase(without, days, kind, words):
    assert no_show_phrase(without, days, kind) == words


# The industry's measures, for the Performance page.


def test_measure_counts_the_industry_s_measures():
    outcomes = [
        ("2026-10-07", "on_time", 0),  # right time
        ("2026-10-07", "on_time", -1),  # early: right time too
        ("2026-10-08", "late", 2),  # T-3, not right time
        ("2026-10-08", "late", 3),  # T-15, not T-3
        ("2026-10-09", "late", 14),
        ("2026-10-09", "late", 15),  # misses every measure
        ("2026-10-09", "cancelled", None),  # counted, and misses every measure
        ("2026-10-09", "late", None),  # "Delayed", no time: not counted
        ("2026-10-09", "unknown", None),
    ]
    assert measure(outcomes, late_minutes=3) == Measures(
        days=3, counted=7, right_time=2, within_3=3, within_15=5, on_time=3, cancelled=1, unknown=2)
    assert measure(outcomes, late_minutes=15).on_time == 5  # on time by the dashboard's own threshold
    assert measure([], late_minutes=3) == Measures(0, 0, 0, 0, 0, 0, 0, 0)


def test_outcomes_are_final_departures_since_a_day(history):
    for back in range(10):
        _record(history, FRIDAY - timedelta(days=back), "late" if back else "on_time", delay=back)
    _record(history, FRIDAY, "late", delay=5, scheduled="08:42")
    history.observe("train", [_train(day=FRIDAY + timedelta(days=3))], _at("07:40", FRIDAY + timedelta(days=3)))
    other = Observed("train|GLO|Hadfield", _at("09:00"), _at("09:00"), "on_time", 0, True, "Northern",
                     "Hadfield", "2", None, None)
    history.observe("train", [other], _at("08:59"))
    history.observe("train", [], _at("09:01"))
    _record_bus(history, FRIDAY, live=False)

    found = history.outcomes("train", FRIDAY - timedelta(days=2))
    assert sorted(found) == [
        (TRAIN, "2026-10-07", "late", 2), (TRAIN, "2026-10-08", "late", 1), (TRAIN, "2026-10-09", "late", 5),
        (TRAIN, "2026-10-09", "on_time", 0), ("train|GLO|Hadfield", "2026-10-09", "on_time", 0),
    ]
    assert len(history.outcomes("train", FRIDAY - timedelta(days=2), TRAIN)) == 4
    assert history.outcomes("bus", FRIDAY) == [(BUS, "2026-10-09", "unknown", None)]
    with pytest.raises(ValueError):
        history.outcomes("tram", FRIDAY)
    with pytest.raises(ValueError):
        history.outcomes("train", _at("07:00"))


def test_the_history_reader_reads_outcomes_once_there_is_a_history(tmp_path):
    from twobells.dashboard.history import HistoryReader

    reader = HistoryReader(tmp_path)
    assert reader.outcomes("train", FRIDAY) == []
    history = History(tmp_path)
    _record(history, FRIDAY, "late", delay=4)
    assert reader.outcomes("train", FRIDAY) == [(TRAIN, "2026-10-09", "late", 4)]
    reader.close()
    history.close()


# The platform a train usually leaves from.


def _days_at(history, platforms, start=FRIDAY):
    """One final day per platform given (None for none known), the last given the newest."""
    for back, platform in enumerate(reversed(platforms)):
        day = start - timedelta(days=back)
        history.observe("train", [_train(day=day)._replace(platform=platform)], _at("07:41", day))
        history.observe("train", [], _at("07:43", day))


def test_usual_platform_is_the_one_used_on_most_days(history):
    _days_at(history, ["1", "2", "1", "1"])
    assert history.usual_platform("train", TRAIN, "07:42") == "1"


def test_usual_platform_needs_a_few_days(history):
    _days_at(history, ["1", "1"])
    assert history.usual_platform("train", TRAIN, "07:42") is None
    _days_at(history, ["1", "1", "1"])
    assert history.usual_platform("train", TRAIN, "07:42") == "1"


def test_usual_platform_needs_one_platform_on_more_than_half_the_days(history):
    _days_at(history, ["1", "2", "1", "2", "3", "1"])
    assert history.usual_platform("train", TRAIN, "07:42") is None


def test_usual_platform_counts_only_days_with_a_platform_known(history):
    _days_at(history, [None, None, "2", None, "2", "2"])
    assert history.usual_platform("train", TRAIN, "07:42") == "2"


def test_usual_platform_goes_by_the_last_ten_days(history):
    _days_at(history, ["1"] * 8 + ["2"] * 6)
    assert history.usual_platform("train", TRAIN, "07:42") == "2"


def test_usual_platform_leaves_out_undecided_days(history):
    _days_at(history, ["1", "1"])
    history.observe("train", [_train(day=FRIDAY + timedelta(days=1))._replace(platform="1")],
                    _at("07:41", FRIDAY + timedelta(days=1)))
    assert history.usual_platform("train", TRAIN, "07:42") is None


# Version 1, as it was made: the outcomes alone.

VERSION_1 = """
CREATE TABLE outcome (
    mode TEXT NOT NULL CHECK (mode IN ('train', 'bus')),
    day TEXT NOT NULL,
    scheduled TEXT NOT NULL,
    route TEXT NOT NULL,
    identity TEXT,
    service TEXT NOT NULL,
    destination TEXT NOT NULL,
    platform TEXT,
    status TEXT NOT NULL,
    delay_min INTEGER,
    live INTEGER NOT NULL,
    reason TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    departs_at TEXT NOT NULL,
    final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mode, day, route, scheduled)
);
CREATE INDEX outcome_route ON outcome (mode, route, scheduled, final, day);
"""


def _version_1(folder):
    """A version 1 history, in WAL mode as History left it, with two outcomes: one final, one not."""
    db = sqlite3.connect(folder / FILE_NAME)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(VERSION_1 + "PRAGMA user_version=1;")
    with db:
        db.executemany(
            "INSERT INTO outcome VALUES ('train', '2026-10-08', ?, ?, 'C1', 'Northern', 'Manchester Piccadilly',"
            " '1', ?, ?, 1, NULL, ?, ?, ?, ?)",
            [("07:42", TRAIN, "late", 6, "2026-10-08T07:30:00", "2026-10-08T07:47:00", "2026-10-08T07:48:00", 1),
             ("08:42", TRAIN, "on_time", 0, "2026-10-08T08:30:00", "2026-10-08T08:40:00", "2026-10-08T08:42:00", 0)],
        )
    db.close()


def test_a_version_1_history_gains_arrivals_and_keeps_its_rows(tmp_path):
    _version_1(tmp_path)
    before = _rows(tmp_path)
    assert len(before) == 2
    assert History.reader(tmp_path) is None  # a reader waits for the writer to bring it up to date
    history = History(tmp_path)
    # Every row kept, its local times kept in UTC, and keyed by when it was booked to leave.
    assert _rows(tmp_path) == [
        {**before[0], "leaves": "2026-10-08T06:42:00+00:00", "first_seen": "2026-10-08T06:30:00+00:00",
         "last_seen": "2026-10-08T06:47:00+00:00", "departs_at": "2026-10-08T06:48:00+00:00"},
        {**before[1], "leaves": "2026-10-08T07:42:00+00:00", "first_seen": "2026-10-08T07:30:00+00:00",
         "last_seen": "2026-10-08T07:40:00+00:00", "departs_at": "2026-10-08T07:42:00+00:00"},
    ]
    assert history.recent("train", TRAIN, "07:42") == [
        {"day": "2026-10-08", "status": "late", "delay_min": 6, "live": True, "reason": None}]
    # The undecided one is decided as before; arrivals are kept now.
    history.sweep(_at("09:30", date(2026, 10, 8)))
    assert [row["final"] for row in _rows(tmp_path)] == [1, 1]
    history.watch([_arriving()], _at("07:40"))
    assert len(_arrival_rows(tmp_path)) == 1
    history.close()
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
    reader = History.reader(tmp_path)
    assert reader is not None and reader.recent("train", TRAIN, "07:42")[0]["delay_min"] == 6
    reader.close()
    History(tmp_path).close()  # and opening it again changes nothing
    assert len(_rows(tmp_path)) == 2 and len(_arrival_rows(tmp_path)) == 1


def test_a_version_1_history_is_copied_as_it_was_before_it_is_moved_on(tmp_path):
    # So going back to a release that reads version 1 only can have it back.
    _version_1(tmp_path)
    before = _rows(tmp_path)
    History(tmp_path).close()
    copy = tmp_path / f"{FILE_NAME}.v1"
    assert stat.S_IMODE(copy.stat().st_mode) == 0o640
    with sqlite3.connect(copy) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'arrival'").fetchall() == []
    assert _rows(tmp_path, copy.name) == before


def test_the_version_1_copy_is_kept_once_and_never_replaced(tmp_path):
    copy = tmp_path / f"{FILE_NAME}.v1"
    copy.write_bytes(b"the first copy")
    _version_1(tmp_path)
    History(tmp_path).close()
    assert copy.read_bytes() == b"the first copy"


def test_a_new_history_and_one_already_moved_on_are_not_copied(tmp_path):
    History(tmp_path).close()
    History(tmp_path).close()
    assert sorted(path.name for path in tmp_path.iterdir()) == [FILE_NAME]


# Version 2, as History wrote it (tests/fixtures/history_v2.sql): naive local times, keyed by day and time.


def _version_2(folder):
    db = sqlite3.connect(folder / FILE_NAME)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript((FIXTURES / "history_v2.sql").read_text())
    db.close()


def _kept(text):
    """A naive London time as version 2 kept it, as version 3 keeps it: in UTC."""
    return None if text is None else datetime.fromisoformat(text).replace(tzinfo=LONDON).astimezone(UTC).isoformat()


def test_a_version_2_history_is_moved_on_and_keeps_every_row(tmp_path):
    _version_2(tmp_path)
    outcomes, arrivals = _rows(tmp_path), _arrival_rows(tmp_path)
    assert (len(outcomes), len(arrivals)) == (3, 4)
    assert History.reader(tmp_path) is None  # a reader waits for the writer to bring it up to date
    history = History(tmp_path, LONDON)
    assert _rows(tmp_path) == [
        {**row, "leaves": _kept(f"{row['day']}T{row['scheduled']}"),
         **{name: _kept(row[name]) for name in ("first_seen", "last_seen", "departs_at")}}
        for row in outcomes]
    assert _arrival_rows(tmp_path) == [
        {**row, "leaves": _kept(f"{row['day']}T{row['scheduled']}"), "followed": 0,
         **{name: _kept(row[name]) for name in ("booked", "expected", "actual", "last_seen")}}
        for row in arrivals]
    assert _rows(tmp_path)[1]["leaves"] == "2026-10-08T06:42:00+00:00"  # 07:42 BST
    # It reads as it did.
    assert history.recent("train", TRAIN, "07:42") == [
        {"day": "2026-10-08", "status": "late", "delay_min": 6, "live": True, "reason": "A signal fault"}]
    assert [(a["day"], a["scheduled"], a["booked"], a["arrived"], a["reported"])
            for a in history.arrivals("MAN", date(2026, 10, 1))] == [
        ("2026-10-09", "22:25", "2026-10-09T21:57:00+00:00", "2026-10-09T22:14:00+00:00", False),
        ("2026-10-08", "07:42", "2026-10-08T07:05:00+00:00", "2026-10-08T07:22:00+00:00", True)]
    # The train still followed is followed, and reported, by its new key.
    [train] = history.following(_at("00:25", date(2026, 10, 10)))
    assert (train.day, train.scheduled, train.leaves) == (date(2026, 10, 9), "23:55", _at("23:55"))
    history.report(train, Report(None, _at("00:33", date(2026, 10, 10)), False), _at("00:34", date(2026, 10, 10)))
    assert history.following(_at("00:35", date(2026, 10, 10))) == []
    assert len(history.arrivals("MAN", date(2026, 10, 1))) == 3
    # The undecided outcome is decided as before: seen 15 minutes before its time, it isn't known.
    history.sweep(_at("01:00", date(2026, 10, 10)))
    assert [(row["scheduled"], row["final"]) for row in _rows(tmp_path)] == [("07:20", 1), ("07:42", 1)]
    history.close()
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert {name for (name,) in db.execute("SELECT name FROM sqlite_master")} >= {
            "outcome", "arrival", "outcome_route", "arrival_open", "arrival_day"}
        assert not {name for (name,) in db.execute("SELECT name FROM sqlite_master") if name.startswith("old_")}
    reader = History.reader(tmp_path)
    assert reader is not None and len(reader.arrivals("MAN", date(2026, 10, 1))) == 3
    reader.close()
    History(tmp_path).close()  # and opening it again changes nothing
    assert (len(_rows(tmp_path)), len(_arrival_rows(tmp_path))) == (2, 4)


def test_a_version_2_history_is_copied_as_it_was_before_it_is_moved_on(tmp_path):
    _version_2(tmp_path)
    outcomes, arrivals = _rows(tmp_path), _arrival_rows(tmp_path)
    History(tmp_path).close()
    copy = tmp_path / f"{FILE_NAME}.v2"
    assert stat.S_IMODE(copy.stat().st_mode) == 0o640
    with sqlite3.connect(copy) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
    assert _rows(tmp_path, copy.name) == outcomes
    assert _arrival_rows(tmp_path, copy.name) == arrivals


def test_a_history_that_cannot_be_moved_on_is_left_as_it_was(tmp_path):
    _version_2(tmp_path)
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        db.execute("UPDATE outcome SET first_seen = 'not a time' WHERE mode = 'bus'")
    outcomes = _rows(tmp_path)
    with pytest.raises(ValueError):
        History(tmp_path)
    assert _rows(tmp_path) == outcomes  # all of it or nothing
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)


# Arrivals at the filter station, for the Delay Repay watch.


def _arriving(scheduled="07:42", booked="08:05", expected="08:05", actual=None, cancelled=False, day=FRIDAY,
              identity="C12345"):
    return Watched(TRAIN, _at(scheduled, day), "MAN", "Manchester Piccadilly", identity, "Northern",
                   "Manchester Piccadilly", _at(booked, day), expected and _at(expected, day),
                   actual and _at(actual, day), cancelled)


def _arrival_rows(folder, name=FILE_NAME):
    with sqlite3.connect(folder / name) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM arrival ORDER BY day, scheduled")]


def _following(history, now):
    return [(f.scheduled, f.expected.astimezone(LONDON).strftime("%H:%M")) for f in history.following(now)]


def test_a_train_on_the_board_is_watched_and_looked_for_when_due(history, tmp_path):
    history.watch([_arriving(expected="08:21")], _at("07:40"))
    [row] = _arrival_rows(tmp_path)
    assert (row["booked"], row["expected"], row["known"], row["final"]) == (
        "2026-10-09T07:05:00+00:00", "2026-10-09T07:21:00+00:00", 1, 0)
    assert (row["station"], row["station_name"], row["operator"]) == ("MAN", "Manchester Piccadilly", "Northern")
    # Looked for once it is due within 10 minutes, and not before.
    assert _following(history, _at("08:10")) == []
    assert history.following(_at("08:11")) == [
        Following(FRIDAY, TRAIN, "07:42", _at("07:42"), "MAN", "C12345", _at("08:05"), _at("08:21"))]


def test_a_reported_arrival_is_final_at_once(history, tmp_path):
    history.watch([_arriving(expected="08:21")], _at("07:40"))
    [train] = history.following(_at("08:15"))
    history.report(train, Report(_at("08:24"), _at("08:24"), False), _at("08:25"))
    [row] = _arrival_rows(tmp_path)
    assert (row["actual"], row["final"]) == ("2026-10-09T07:24:00+00:00", 1)
    assert history.following(_at("08:25")) == []
    # Final rows never change.
    history.report(train, Report(None, _at("08:40"), False), _at("08:41"))
    history.watch([_arriving(actual="08:50")], _at("08:51"))
    assert _arrival_rows(tmp_path)[0]["actual"] == "2026-10-09T07:24:00+00:00"
    assert history.arrivals("MAN", FRIDAY) == [{
        "day": "2026-10-09", "scheduled": "07:42", "route": TRAIN, "station_name": "Manchester Piccadilly",
        "operator": "Northern", "destination": "Manchester Piccadilly", "booked": "2026-10-09T07:05:00+00:00",
        "arrived": "2026-10-09T07:24:00+00:00", "reported": True}]


def test_an_estimate_seen_near_its_time_is_the_arrival_once_it_has_passed(history, tmp_path):
    history.watch([_arriving(expected="08:21")], _at("08:18"))  # still on the board, not followed
    history.settle(_at("08:22"))  # only a minute past it: not yet
    assert _arrival_rows(tmp_path)[0]["final"] == 0
    history.settle(_at("08:23"))
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["arrived"], arrived["reported"]) == ("2026-10-09T07:21:00+00:00", False)


def test_a_followed_train_waits_for_darwin_until_let_go(history, tmp_path):
    history.watch([_arriving(expected="08:21")], _at("07:40"))
    [train] = history.following(_at("08:15"))
    history.report(train, Report(_at("08:23"), None, False), _at("08:20"))  # Darwin answers for it
    history.settle(_at("08:40"))
    assert _arrival_rows(tmp_path)[0]["final"] == 0  # Darwin may yet say when it got there
    history.let_go(train)  # Darwin no longer knows it, or asking failed
    history.settle(_at("08:41"))
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["arrived"], arrived["reported"]) == ("2026-10-09T07:23:00+00:00", False)


def test_a_followed_train_takes_its_estimate_after_an_hour(history, tmp_path):
    history.watch([_arriving(expected="08:21")], _at("07:40"))
    [train] = history.following(_at("08:15"))
    history.report(train, Report(_at("08:23"), None, False), _at("08:20"))
    history.report(train, Report(_at("08:23"), None, False), _at("09:20"))  # still no time from Darwin
    history.sweep(_at("09:22"))
    assert _arrival_rows(tmp_path)[0]["final"] == 0
    history.sweep(_at("09:23"))
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["arrived"], arrived["reported"]) == ("2026-10-09T07:23:00+00:00", False)


def test_an_arrival_nobody_saw_near_its_time_is_deleted(history, tmp_path):
    # Last seen as it left your station at 07:42, due at 08:21: nobody saw it get there.
    history.watch([_arriving(expected="08:21")], _at("07:42"))
    history.settle(_at("08:30"))
    assert _arrival_rows(tmp_path)[0]["final"] == 0  # it may still be reported
    history.settle(_at("09:21"))
    assert _arrival_rows(tmp_path) == []


def test_delayed_with_no_time_is_never_taken_as_the_arrival(history, tmp_path):
    history.watch([_arriving(expected="08:21")], _at("07:40"))
    [train] = history.following(_at("08:15"))
    history.report(train, Report(None, None, False), _at("08:20"))  # "Delayed"
    [row] = _arrival_rows(tmp_path)
    assert (row["expected"], row["known"]) == ("2026-10-09T07:21:00+00:00", 0)  # the last time is kept
    history.settle(_at("08:30"))
    assert _arrival_rows(tmp_path)[0]["final"] == 0
    assert _following(history, _at("08:30")) == [("07:42", "08:21")]  # still looked for
    history.sweep(_at("09:22"))  # upkeep settles them too
    assert _arrival_rows(tmp_path) == []


def test_the_hour_the_clocks_go_back_keeps_the_trains_of_both_passes(history):
    # 25 October 2026: the 01:20 of the first pass (BST) and of the second (GMT) are two trains.
    sunday = date(2026, 10, 25)
    first = _arriving("01:20", "01:50", actual="01:55", day=sunday)
    second = first._replace(leaves=_at("01:20", sunday, fold=1), booked=_at("01:50", sunday, fold=1),
                            actual=_at("01:52", sunday, fold=1))
    history.watch([first], _at("01:56", sunday))
    history.watch([second], _at("01:53", sunday, fold=1))
    assert sorted((a["booked"], a["arrived"]) for a in history.arrivals("MAN", sunday)) == [
        ("2026-10-25T00:50:00+00:00", "2026-10-25T00:55:00+00:00"),
        ("2026-10-25T01:50:00+00:00", "2026-10-25T01:52:00+00:00")]


def test_cancelled_there_is_final_and_never_listed(history, tmp_path):
    history.watch([_arriving(cancelled=True, expected=None)], _at("07:40"))
    assert _arrival_rows(tmp_path)[0]["final"] == 1
    assert history.following(_at("08:05")) == [] and history.arrivals("MAN", FRIDAY) == []


def test_arrivals_are_newest_first_for_one_station_since_a_day(history):
    history.watch([_arriving(actual="08:05", day=FRIDAY - timedelta(days=d)) for d in (0, 1, 40)]
                  + [_arriving(scheduled="06:42", booked="07:05", actual="07:30")], _at("08:10"))
    history.watch([_arriving()._replace(station="HDF", actual=_at("08:06"))], _at("08:10"))
    found = history.arrivals("MAN", FRIDAY - timedelta(days=28))
    assert [(a["day"], a["scheduled"]) for a in found] == [
        ("2026-10-09", "07:42"), ("2026-10-09", "06:42"), ("2026-10-08", "07:42")]
    assert len(history.arrivals("HDF", FRIDAY)) == 1
    history.purge(_at("08:10"), 3)  # the one 40 days back goes
    assert len(history.arrivals("MAN", date(2026, 1, 1))) == 3
    with pytest.raises(ValueError):
        history.arrivals("MAN", _at("07:00"))


@pytest.mark.parametrize("change", [
    {"leaves": "07:42"},
    {"leaves": datetime(2026, 10, 9, 7, 42)},  # naive: in no zone
    {"station": "man"},
    {"station": "MAN|x"},
    {"booked": datetime(2026, 10, 9, 8, 5)},
    {"actual": date(2026, 10, 9)},
])
def test_watch_refuses_nonsense_and_writes_nothing(history, tmp_path, change):
    with pytest.raises(ValueError):
        history.watch([_arriving(scheduled="06:42"), _arriving()._replace(**change)], _at("07:40"))
    assert _arrival_rows(tmp_path) == []


def test_the_history_reader_reads_arrivals(tmp_path):
    from twobells.dashboard.history import HistoryReader

    reader = HistoryReader(tmp_path)
    assert reader.arrivals("MAN", FRIDAY) == []
    history = History(tmp_path)
    history.watch([_arriving(actual="08:30")], _at("08:31"))
    assert reader.arrivals("MAN", FRIDAY)[0]["arrived"] == "2026-10-09T07:30:00+00:00"
    reader.close()
    history.close()

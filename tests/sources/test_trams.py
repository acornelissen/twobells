"""Trams from the saved Metrolink timetable (timetable/trams.py)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.timetable.test_trams import NOW, STOPS, read
from twobells.core.model import Status
from twobells.sources.trams import TramTimetableSource
from twobells.timetable import trams

ZONE = ZoneInfo("Europe/London")


def source(folder, at=NOW, stops=STOPS, **options):
    return TramTimetableSource(folder, stops, ZONE, clock=lambda: at.replace(tzinfo=ZONE), **options)


def test_trams_are_booked_times_from_the_timetable(tmp_path):
    trams.save(tmp_path, read())
    found = source(tmp_path).fetch()
    assert [(d.mode, d.scheduled, d.destination, d.stop, d.replacement_bus) for d in found] == [
        ("metro", "08:05", "Altrincham", "A", True),
        ("metro", "08:09", "Bury", "A", False),
        ("metro", "00:10", "Bury", "A", False),
    ]
    # No live time: the views say they are the timetable's.
    assert {(d.status, d.expected) for d in found} == {(Status.UNKNOWN, None)}
    assert found[1].service == "Blue Line"
    assert [d.destination for d in source(tmp_path, rows=1).fetch()] == ["Altrincham"]
    assert [d.destination for d in source(tmp_path, directions=["bury"]).fetch()] == ["Bury", "Bury"]


def test_leaving_says_when_each_goes_in_the_board_s_zone(tmp_path):
    trams.save(tmp_path, read())
    [(leaves, call)] = source(tmp_path).leaving(1)
    assert leaves == datetime(2026, 10, 19, 8, 5, tzinfo=ZONE) and call.bus


def test_without_a_usable_timetable_it_says_why(tmp_path):
    with pytest.raises(RuntimeError, match="there is no tram timetable yet"):
        source(tmp_path).fetch()
    trams.save(tmp_path, read())
    assert source(tmp_path).unusable() is None
    assert source(tmp_path, stops={"A": "9400ZZMAGMX"}).unusable() == "the tram timetable is for other stops"
    assert source(tmp_path, at=NOW + timedelta(days=30)).unusable() == "the tram timetable is over four weeks old"


def test_a_new_timetable_is_read_when_the_file_changes(tmp_path):
    tram_source = source(tmp_path)
    trams.save(tmp_path, read())
    assert len(tram_source.fetch()) == 3
    (tmp_path / trams.FILE_NAME).unlink()
    assert tram_source.unusable() == "there is no tram timetable yet"

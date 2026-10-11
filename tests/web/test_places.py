from dataclasses import replace

from tests.timetable.test_build import _long_schedule, _peak
from twobells.timetable import build as timetable
from twobells.timetable.model import StopPosition
from twobells.web import places


def test_the_stops_are_named_from_a_big_timetable_without_holding_its_journeys(tmp_path):
    # A Pi's web server has 128 MB: the pages want the stops' names, not
    # the thousands of journeys a timetable can hold (or their roads).
    schedule = _long_schedule()
    schedule = replace(schedule, positions={**schedule.positions, "1800SB12341": StopPosition("Station Road", 53.4, -1.9)})
    timetable.save(tmp_path, schedule)
    size = (tmp_path / timetable.FILE_NAME).stat().st_size
    names, peak = _peak(lambda: places.stop_names(tmp_path))
    assert names["1800SB12341"] == "Station Road" and len(names) == len(schedule.positions)
    assert peak < size / 5
    kept = places.schedule(tmp_path)
    assert kept is not None and kept.journeys == () and kept.shapes == {} and kept.built == schedule.built

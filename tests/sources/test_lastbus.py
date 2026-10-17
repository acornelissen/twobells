from datetime import date, datetime, time

from tests.sources.test_livebus import ASHTON_INT, GEORGE, HADFIELD, HENRY_A, HOWARD, MARKET_HALL, ST_MARYS_W, schedule
from twobells.sources import holidays
from twobells.sources.lastbus import LastBus, day_groups, last_buses, last_of_all, screen_line
from twobells.timetable.model import Calendar, Call, Journey

FRIDAY = date(2026, 10, 9)
SATURDAY = date(2026, 10, 10)
SUNDAY = date(2026, 10, 11)
CHRISTMAS = date(2026, 12, 25)  # a Friday
EVERY_DAY = Calendar(days=frozenset(range(7)))
WEEKDAYS = Calendar(days=frozenset(range(5)))
SUNDAYS = Calendar(days=frozenset({6}))
NOT_ON_CHRISTMAS = Calendar(days=frozenset(range(7)), bank_holidays_off=frozenset({"ChristmasDay"}))
CHRISTMAS_ONLY = Calendar(days=frozenset(), bank_holidays_on=frozenset({"ChristmasDay"}))


def east(minute, calendar=EVERY_DAY, line="341", destination="Hadfield", **kwargs):
    """Calls at stop A (Henry Street) at `minute` of its service day, then on to Hadfield."""
    calls = (Call(HENRY_A, minute), Call(HOWARD, minute + 1), Call(HADFIELD, minute + 15))
    return Journey(line, "BNML", destination, f"E{minute}", calls, calendar, **kwargs)


def west(minute, calendar=EVERY_DAY):
    """Starts at stop A and calls at stop B (St Marys Road) three minutes later."""
    calls = (Call(HENRY_A, minute), Call(MARKET_HALL, minute + 1), Call(GEORGE, minute + 2),
             Call(ST_MARYS_W, minute + 3), Call(ASHTON_INT, minute + 45))
    return Journey("237", "BNML", "Ashton", f"W{minute}", calls, calendar)


def local(day, hh, mm):
    return datetime.combine(day, time(hh, mm))


def found(plan, now, names=None, directions=None):
    today = now.date()
    days = (date.fromordinal(today.toordinal() - 1), today)
    groups = {day: day_groups(plan, day, names or {}, directions) for day in days}
    return last_buses(groups, now)


def said(buses):
    return [(b.line, b.destination, b.stop, b.leaves.strftime("%H:%M"), b.remaining) for b in buses]


def test_each_line_and_destination_has_its_last_bus_of_the_day():
    plan = schedule(east(20 * 60), east(23 * 60 + 5), west(22 * 60), west(22 * 60 + 30))
    # Westbound buses call at stop A, then B; the last leaves B last.
    assert said(found(plan, local(FRIDAY, 12, 0))) == [
        ("237", "Ashton", "B", "22:33", 4),
        ("341", "Hadfield", "A", "23:05", 2),
    ]


def test_buses_still_to_come_are_counted_and_a_gone_last_bus_stays():
    plan = schedule(east(20 * 60), east(23 * 60 + 5))
    assert said(found(plan, local(FRIDAY, 21, 0))) == [("341", "Hadfield", "A", "23:05", 1)]
    # A minute after its time it may still be at the stop, late.
    assert said(found(plan, local(FRIDAY, 23, 6))) == [("341", "Hadfield", "A", "23:05", 1)]
    assert said(found(plan, local(FRIDAY, 23, 7))) == [("341", "Hadfield", "A", "23:05", 0)]


def test_a_destination_of_its_own_is_a_line_of_its_own():
    plan = schedule(east(22 * 60), east(23 * 60, destination="Glossop"))
    assert [(b.destination, b.leaves.strftime("%H:%M")) for b in found(plan, local(FRIDAY, 12, 0))] == [
        ("Hadfield", "22:00"), ("Glossop", "23:00"),
    ]


def test_journeys_ending_at_your_stop_or_kept_for_an_inbound_bus_are_not_last_buses():
    ends_here = Journey("341", "BNML", "Glossop", "X1", (Call(HOWARD, 1400), Call(HENRY_A, 1410)), EVERY_DAY)
    inbound = east(23 * 60 + 30, inbound_only=True)
    plan = schedule(east(22 * 60), ends_here, inbound)
    assert said(found(plan, local(FRIDAY, 12, 0))) == [("341", "Hadfield", "A", "22:00", 1)]


def test_only_the_board_s_directions():
    plan = schedule(east(22 * 60), west(23 * 60))
    assert [b.line for b in found(plan, local(FRIDAY, 12, 0), directions=["ashton"])] == ["237"]


def test_the_last_bus_after_midnight_is_the_evening_s():
    # 00:20 on a journey that started the evening before (its service day).
    plan = schedule(east(22 * 60), east(24 * 60 + 20))
    evening = found(plan, local(FRIDAY, 23, 0))
    assert said(evening) == [("341", "Hadfield", "A", "00:20", 1)]
    assert evening[0].service_day == FRIDAY and evening[0].leaves == local(SATURDAY, 0, 20)
    # Past midnight, Friday's last bus is still to come.
    small_hours = found(plan, local(SATURDAY, 0, 10))
    assert small_hours[0].service_day == FRIDAY and small_hours[0].remaining == 1
    # Once it has gone, Saturday's: tonight's, still to come.
    after = found(plan, local(SATURDAY, 0, 30))
    assert after[0].service_day == SATURDAY and after[0].leaves == local(SUNDAY, 0, 20)
    assert after[0].remaining == 2


def test_a_bus_after_midnight_the_timetable_gives_the_next_day_is_tonight_s():
    # Saturday's 22:00 and 23:10, and a 00:05 the timetable puts on Sunday,
    # not as Saturday's journey running past midnight: it is Saturday night's
    # last bus all the same, and not Sunday's.
    saturdays = Calendar(days=frozenset({5}))
    plan = schedule(east(22 * 60, calendar=saturdays), east(23 * 60 + 10, calendar=saturdays),
                    east(5, calendar=SUNDAYS), east(9 * 60, calendar=SUNDAYS), east(18 * 60, calendar=SUNDAYS))
    evening = found(plan, local(SATURDAY, 23, 5))
    assert said(evening) == [("341", "Hadfield", "A", "00:05", 2)]
    assert evening[0].service_day == SATURDAY and evening[0].leaves == local(SUNDAY, 0, 5)
    assert screen_line(evening, local(SATURDAY, 23, 5)) == "Last bus 00:05"
    assert said(found(plan, local(SATURDAY, 23, 30))) == [("341", "Hadfield", "A", "00:05", 1)]
    small_hours = found(plan, local(SUNDAY, 0, 1))
    assert small_hours[0].service_day == SATURDAY and small_hours[0].remaining == 1
    # Once it has gone, Sunday's own: its 00:05 was Saturday night's.
    assert said(found(plan, local(SUNDAY, 0, 10))) == [("341", "Hadfield", "A", "18:00", 2)]
    # A Sunday the 00:05 doesn't run (Saturdays only, say), Saturday's own last bus stays.
    alone = schedule(east(22 * 60, calendar=saturdays), east(23 * 60 + 10, calendar=saturdays))
    assert said(found(alone, local(SATURDAY, 23, 5))) == [("341", "Hadfield", "A", "23:10", 1)]


def test_a_last_bus_gone_last_night_is_dropped_when_none_runs_today():
    plan = schedule(east(23 * 60, calendar=Calendar(days=frozenset({4}))))  # Fridays only
    assert said(found(plan, local(FRIDAY, 23, 30))) == [("341", "Hadfield", "A", "23:00", 0)]
    assert found(plan, local(SATURDAY, 0, 30)) == []


def test_sunday_has_its_own_last_bus():
    plan = schedule(east(23 * 60, calendar=WEEKDAYS), east(19 * 60, calendar=SUNDAYS))
    assert said(found(plan, local(FRIDAY, 12, 0))) == [("341", "Hadfield", "A", "23:00", 1)]
    assert said(found(plan, local(SATURDAY, 12, 0))) == []
    assert said(found(plan, local(SUNDAY, 12, 0))) == [("341", "Hadfield", "A", "19:00", 1)]


def test_a_bank_holiday_follows_the_timetable_s_own_rules():
    plan = schedule(east(18 * 60), east(23 * 60, calendar=NOT_ON_CHRISTMAS), east(20 * 60, calendar=CHRISTMAS_ONLY))
    names = {day: holidays.holiday_names(day, {}) for day in (date(2026, 12, 24), CHRISTMAS)}
    assert said(found(plan, local(CHRISTMAS, 12, 0), names)) == [("341", "Hadfield", "A", "20:00", 2)]
    # The day before, the usual last bus.
    assert said(found(plan, local(date(2026, 12, 24), 12, 0), names)) == [("341", "Hadfield", "A", "23:00", 2)]


def test_calendars_are_asked_once_a_day_each(monkeypatch):
    from twobells.sources import lastbus

    asked = []

    def runs_on(calendar, day, names):
        asked.append(calendar)
        return True

    monkeypatch.setattr(lastbus, "runs_on", runs_on)
    day_groups(schedule(east(600), east(700), east(800), west(900, calendar=WEEKDAYS)), FRIDAY)
    assert sorted(map(id, asked)) == sorted(map(id, (EVERY_DAY, WEEKDAYS)))


def bus(hh, mm, remaining, day=FRIDAY, line="341"):
    return LastBus(line, "Hadfield", "A", day, local(day, hh, mm), remaining)


def test_shown_under_the_buses_in_the_evening_or_once_few_are_left():
    assert not bus(23, 5, 4).shown(local(FRIDAY, 18, 59))
    assert bus(23, 5, 4).shown(local(FRIDAY, 19, 0))
    assert bus(23, 5, 3).shown(local(FRIDAY, 12, 0))
    assert bus(8, 30, 0).shown(local(FRIDAY, 12, 0))  # the last has gone: none left today


def test_the_last_bus_of_all_is_the_latest_still_to_come():
    assert last_of_all([bus(22, 0, 1, line="237"), bus(23, 5, 2), bus(21, 0, 0, line="6")]) == bus(23, 5, 2)
    assert last_of_all([bus(21, 0, 0)]) is None
    # After midnight the evening's last bus, not tonight's.
    small_hours = LastBus("341", "Hadfield", "A", FRIDAY, local(SATURDAY, 0, 45), 1)
    assert last_of_all([small_hours, bus(23, 5, 9, day=SATURDAY, line="237")]) == small_hours


def test_a_screen_says_the_last_bus_in_the_hour_before_it():
    buses = [bus(22, 0, 1, line="237"), bus(23, 5, 2)]
    assert screen_line(buses, local(FRIDAY, 22, 4)) is None
    assert screen_line(buses, local(FRIDAY, 22, 5)) == "Last bus 23:05"
    assert screen_line([bus(23, 5, 0)], local(FRIDAY, 23, 10)) is None

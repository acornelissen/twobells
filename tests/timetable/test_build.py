import fcntl
import io
import os
import re
import stat
import subprocess
import sys
import threading
import tracemalloc
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from departureboard.core import safexml
from departureboard.sources.holidays import Event, holiday_names
from departureboard.timetable import build as timetable
from departureboard.timetable.build import (
    FILE_NAME,
    FORMAT,
    LOCK_NAME,
    TimetableBusy,
    TimetableError,
    add_roads,
    build,
    build_schedule,
    download,
    due,
    journeys_on,
    load,
    parse_journeys,
    refresh,
    runs_on,
    save,
)
from departureboard.timetable.model import Calendar, Call, Schedule, StopPosition, metres
from departureboard.timetable.routesetup import DEFAULT_LIMITS, DocumentLimits
from tests.paths import FIXTURES
from tests.sources.test_livebus import (
    ARUNDEL,
    GRAPES,
    HALFWAY,
    HOWARD,
    JUNCTION,
    MARKET_HALL,
    POSITIONS,
    ST_MARYS_E,
    SUNLAWS,
    at,
    bus,
)

TXC_341 = (FIXTURES / "txc_bnml_341.xml").read_bytes()
TXC_237 = (FIXTURES / "txc_bnml_237.xml").read_bytes()
TXC_61 = (FIXTURES / "txc_hipk_61.xml").read_bytes()
STOP_A = "1000DGHS0900"
STOP_B = "100000008350"
OURS = {STOP_A, STOP_B}


def _minute(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _at(journey, atco):
    return next(call.minute for call in journey.calls if call.atco == atco)


def _by_code(journeys):
    return {journey.journey_code: journey for journey in journeys}


# A small TransXChange document for the cases the real samples don't show.

def _txc(links, *, departure="10:00:00", profile="", journey="", pattern="", operators=None, stops="", extra="",
         start="2026-01-01", end=None, revision=None):
    """`links` is a list of (from, to, run time, from wait, to wait) on one pattern.
    Its service S1 runs from `start` until `end`, if given; `revision` is the
    document's RevisionNumber."""
    timing = "".join(
        f'<JourneyPatternTimingLink id="l{i}"><From>{f"<WaitTime>{fw}</WaitTime>" if fw else ""}'
        f"<StopPointRef>{a}</StopPointRef></From><To>{f'<WaitTime>{tw}</WaitTime>' if tw else ''}"
        f"<StopPointRef>{b}</StopPointRef></To><RunTime>{run}</RunTime></JourneyPatternTimingLink>"
        for i, (a, b, run, fw, tw) in enumerate(links)
    )
    if operators is None:
        operators = '<Operator id="o1"><NationalOperatorCode>TEST</NationalOperatorCode></Operator>'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<TransXChange xmlns="http://www.transxchange.org.uk/"{f' RevisionNumber="{revision}"' if revision is not None else ""}>
 <StopPoints>{stops}</StopPoints>
 {extra}
 <JourneyPatternSections><JourneyPatternSection id="s1">{timing}</JourneyPatternSection></JourneyPatternSections>
 <Operators>{operators}</Operators>
 <Services><Service><ServiceCode>S1</ServiceCode>
  <Lines><Line id="L1"><LineName>99</LineName></Line></Lines>
  <OperatingPeriod><StartDate>{start}</StartDate>{f"<EndDate>{end}</EndDate>" if end else ""}</OperatingPeriod>
  {profile}
  <RegisteredOperatorRef>o1</RegisteredOperatorRef>
  <StandardService><JourneyPattern id="p1">{pattern}<JourneyPatternSectionRefs>s1</JourneyPatternSectionRefs></JourneyPattern></StandardService>
 </Service></Services>
 <VehicleJourneys><VehicleJourney>
  <VehicleJourneyCode>vj1</VehicleJourneyCode><ServiceRef>S1</ServiceRef><LineRef>L1</LineRef>
  <JourneyPatternRef>p1</JourneyPatternRef><DepartureTime>{departure}</DepartureTime>{journey}
 </VehicleJourney></VehicleJourneys>
</TransXChange>""".encode()


LINKS = [("X1", "X2", "PT2M", "", ""), ("X2", STOP_A, "PT3M", "", ""), (STOP_A, "X3", "PT1M", "", "")]


def _one(xml, atcos=OURS, noc=""):
    journeys = parse_journeys(xml, atcos, noc)
    assert len(journeys) == 1
    return journeys[0]


# Journeys and their times, from the real timetables.

def test_journeys_into_stop_a_on_the_341():
    journeys = _by_code(parse_journeys(TXC_341, OURS))
    # Its busiest pattern into Glossop ends at Stop A; the 09:06 from Hyde gets there at 10:06.
    into = journeys["1007"]
    assert _at(into, STOP_A) == _minute("10:06")
    assert into.calls[-1] == Call(STOP_A, _minute("10:06"))
    assert into.line == "341"
    assert into.operator == "BNGN"  # the document's national operator code, not the dataset's BNML
    assert into.destination == "Henry Street"
    assert [_at(journeys[code], STOP_A) for code in ("1011", "1015")] == [_minute("12:06"), _minute("14:06")]


def test_journeys_starting_at_stop_a_are_departures_from_it():
    journeys = _by_code(parse_journeys(TXC_341, OURS))
    out = journeys["1012"]
    assert out.origin == Call(STOP_A, _minute("12:11"))
    assert out.destination == "Hyde Bus Station"
    # Run times from Stop A to Market Hall, George Street, St Marys Road: 0, 0, 1 minutes.
    assert [call.minute for call in out.calls[:4]] == [731, 731, 731, 732]


def test_every_stop_is_kept_in_order():
    into = _by_code(parse_journeys(TXC_341, OURS))["1007"]
    assert len(into.calls) == 23
    assert into.calls[0].atco == "1000DGMC3417"
    minutes = [call.minute for call in into.calls]
    assert minutes == sorted(minutes)


def test_a_journey_through_stop_b_after_midnight():
    journeys = _by_code(parse_journeys(TXC_237, OURS))
    late = journeys["4191"]
    # Leaves Ashton at 23:31; 38 minutes of run times later it reaches Stop B.
    assert late.origin.minute == _minute("23:31")
    assert late.calls[-1] == Call(STOP_B, 24 * 60 + 9)
    through = journeys["4171"]
    assert _at(through, STOP_B) == _minute("18:12")
    assert through.calls[-1].atco != STOP_B  # it carries on beyond your stop


def test_the_61_at_stop_b():
    journeys = _by_code(parse_journeys(TXC_61, OURS))
    assert _at(journeys["10850"], STOP_B) == _minute("09:15")
    assert journeys["10815"].origin == Call(STOP_B, _minute("08:15"))
    assert journeys["10815"].destination == "New Mills Bus Station (Stop 1)"
    assert {j.operator for j in journeys.values()} == {"HIPK"}  # NationalOperatorCode, not OperatorCode BOWE


def test_only_journeys_calling_at_your_stops():
    assert parse_journeys(TXC_341, {STOP_B}) == []
    assert {j.line for j in parse_journeys(TXC_237, {STOP_A})} == {"237"}
    assert len(parse_journeys(TXC_237, {STOP_A})) == 2


def test_journey_code_is_the_ticket_machine_code():
    assert set(_by_code(parse_journeys(TXC_61, OURS))) == {"10815", "10850", "10930", "11003"}


# Times.

def test_wait_time_on_the_from_is_spent_before_leaving():
    links = [("X1", "X2", "PT2M", "", ""), ("X2", STOP_A, "PT3M", "PT1M", ""), (STOP_A, "X3", "PT1M", "PT2M", "")]
    journey = _one(_txc(links))
    assert [call.minute for call in journey.calls] == [600, 603, 608, 609]


def test_wait_time_on_the_to_is_spent_at_that_stop():
    links = [("X1", "X2", "PT2M", "", "PT1M"), ("X2", STOP_A, "PT3M", "", "PT2M"), (STOP_A, "X3", "PT1M", "", "PT5M")]
    journey = _one(_txc(links))
    # The last stop is where it ends: it reaches it, and its wait there doesn't count.
    assert [call.minute for call in journey.calls] == [600, 603, 608, 609]


def test_a_from_wait_wins_over_the_to_wait_for_the_same_stop():
    links = [("X1", STOP_A, "PT2M", "", "PT5M"), (STOP_A, "X3", "PT1M", "PT1M", "")]
    assert [call.minute for call in _one(_txc(links)).calls] == [600, 603, 604]


def test_a_wait_before_the_first_stop_is_ignored():
    # DepartureTime is when it leaves the first stop.
    links = [(STOP_A, "X2", "PT2M", "PT3M", "")]
    assert _one(_txc(links)).origin == Call(STOP_A, 600)


def test_seconds_are_added_up_and_each_call_rounded_down():
    links = [("X1", "X2", "PT1M30S", "", ""), ("X2", STOP_A, "PT1M30S", "", ""), (STOP_A, "X3", "PT29S", "", "")]
    journey = _one(_txc(links, departure="10:00:45"))
    # 10:00:45, 10:02:15, 10:03:45, 10:04:14
    assert [call.minute for call in journey.calls] == [600, 602, 603, 604]


def test_hours_in_a_run_time():
    links = [("X1", STOP_A, "PT1H5M", "", "")]
    assert _one(_txc(links)).calls[-1].minute == 665


def test_a_departure_day_shift_counts_past_midnight():
    journey = _one(_txc(LINKS, departure="00:20:00", journey="<DepartureDayShift>1</DepartureDayShift>"))
    assert journey.origin.minute == 1460


def test_a_vehicle_journey_timing_link_overrides_the_pattern():
    timing = (
        "<VehicleJourneyTimingLink><JourneyPatternTimingLinkRef>l1</JourneyPatternTimingLinkRef>"
        "<RunTime>PT10M</RunTime></VehicleJourneyTimingLink>"
    )
    journey = _one(_txc(LINKS, journey=timing))
    assert [call.minute for call in journey.calls] == [600, 602, 612, 613]


def test_journeys_with_bad_times_are_skipped():
    assert parse_journeys(_txc(LINKS, departure="25:00:00"), OURS) == []
    assert parse_journeys(_txc(LINKS, departure="soon"), OURS) == []


# Names and codes.

def test_destination_falls_back_to_the_pattern_then_the_last_stop():
    stops = "<AnnotatedStopPointRef><StopPointRef>X3</StopPointRef><CommonName>Old Glossop</CommonName></AnnotatedStopPointRef>"
    assert _one(_txc(LINKS, stops=stops)).destination == "Old Glossop"
    pattern = "<DestinationDisplay>Pattern End</DestinationDisplay>"
    assert _one(_txc(LINKS, stops=stops, pattern=pattern)).destination == "Pattern End"
    journey = "<DestinationDisplay>Journey End</DestinationDisplay>"
    assert _one(_txc(LINKS, stops=stops, pattern=pattern, journey=journey)).destination == "Journey End"
    assert _one(_txc(LINKS)).destination == "X3"  # no name anywhere: its code


def test_journey_code_falls_back_to_the_vehicle_journey_code():
    assert _one(_txc(LINKS)).journey_code == "vj1"
    ticket = "<Operational><TicketMachine><JourneyCode>1735</JourneyCode></TicketMachine></Operational>"
    assert _one(_txc(LINKS, journey=ticket)).journey_code == "1735"


def test_operator_comes_from_the_journey_then_the_document_then_the_dataset():
    two = (
        '<Operator id="o1"><NationalOperatorCode>AAAA</NationalOperatorCode></Operator>'
        '<Operator id="o2"><OperatorCode>BBBB</OperatorCode></Operator>'
    )
    assert _one(_txc(LINKS, operators=two)).operator == "AAAA"  # the service's registered operator
    assert _one(_txc(LINKS, operators=two, journey="<OperatorRef>o2</OperatorRef>")).operator == "BBBB"
    assert _one(_txc(LINKS, operators=""), noc="HIPK").operator == "HIPK"


def test_a_doctype_is_refused():
    with pytest.raises(TimetableError, match="DOCTYPE"):
        parse_journeys(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', OURS)


def test_not_xml_is_an_error():
    with pytest.raises(TimetableError):
        parse_journeys(b"<TransXChange", OURS)


# Calendars from the real timetables.

def test_the_341_runs_on_weekdays_but_not_bank_holidays():
    calendar = parse_journeys(TXC_341, OURS)[0].calendar
    assert calendar.days == {0, 1, 2, 3, 4}
    assert calendar.start == date(2026, 2, 17) and calendar.end == date(2031, 2, 23)
    assert {"ChristmasDay", "SpringBank", "ChristmasEve", "NewYearsEve", "BoxingDayHoliday"} <= calendar.bank_holidays_off
    assert calendar.bank_holidays_on == frozenset()


def test_the_237_summer_timetable_is_not_on_school_days():
    calendar = parse_journeys(TXC_237, OURS)[0].calendar
    assert calendar.end == date(2026, 8, 29)
    assert calendar.school_days_only == ()
    assert calendar.not_on_school_days[0] == (date(2026, 9, 1), date(2026, 9, 4))
    assert runs_on(calendar, date(2026, 8, 3))  # a Monday in the summer holidays
    assert not runs_on(calendar, date(2026, 8, 1))  # Saturday
    assert not runs_on(calendar, date(2026, 9, 7))  # past its end


# runs_on.

WEEKDAYS = Calendar(days=frozenset(range(5)))
GOVUK = {date(2026, 12, 25): Event("Christmas Day", ""), date(2026, 12, 28): Event("Boxing Day", "Substitute day"),
         date(2026, 5, 25): Event("Spring bank holiday", "")}


def _names(*days):
    return {day: holiday_names(day, GOVUK) for day in days}


def test_journeys_on_a_day():
    journeys = tuple(parse_journeys(TXC_61, OURS))
    schedule = Schedule(datetime(2026, 10, 9, 3, 0), None, {"B": STOP_B}, journeys)
    assert journeys_on(schedule, date(2026, 10, 9)) == list(journeys)
    assert journeys_on(schedule, date(2026, 10, 10)) == []  # Saturday
    assert journeys_on(schedule, date(2026, 12, 24), _names(date(2026, 12, 24))) == []


def test_regular_days():
    assert runs_on(WEEKDAYS, date(2026, 10, 9))  # Friday
    assert not runs_on(WEEKDAYS, date(2026, 10, 10))  # Saturday


def test_operating_period_bounds_everything():
    calendar = Calendar(days=frozenset(range(7)), start=date(2026, 10, 1), end=date(2026, 10, 31),
                        dates_on=((date(2026, 11, 1), date(2026, 11, 1)),))
    assert runs_on(calendar, date(2026, 10, 1)) and runs_on(calendar, date(2026, 10, 31))
    assert not runs_on(calendar, date(2026, 9, 30))
    assert not runs_on(calendar, date(2026, 11, 1))  # even on a day it would specially run


def test_bank_holidays_off_and_on():
    off = Calendar(days=frozenset(range(5)), bank_holidays_off=frozenset({"SpringBank", "BoxingDayHoliday"}))
    on = Calendar(days=frozenset({6}), bank_holidays_on=frozenset({"SpringBank", "BoxingDayHoliday", "ChristmasEve"}))
    names = _names(date(2026, 5, 25), date(2026, 12, 28), date(2026, 12, 24))
    assert not runs_on(off, date(2026, 5, 25), names)
    assert runs_on(on, date(2026, 5, 25), names)  # a Sunday service on the Monday
    assert not runs_on(off, date(2026, 12, 28), names)  # the substitute for Boxing Day
    assert runs_on(on, date(2026, 12, 28), names)
    assert runs_on(on, date(2026, 12, 24), names)  # Christmas Eve, a Thursday
    assert not runs_on(on, date(2026, 12, 23), names)


def test_a_bank_holiday_listed_neither_way_follows_the_regular_days():
    calendar = Calendar(days=frozenset(range(5)), bank_holidays_off=frozenset({"ChristmasDay"}))
    assert runs_on(calendar, date(2026, 5, 25), _names(date(2026, 5, 25)))


def test_christmas_eve_off():
    calendar = Calendar(days=frozenset(range(5)), bank_holidays_off=frozenset({"ChristmasEve"}))
    assert not runs_on(calendar, date(2026, 12, 24), _names(date(2026, 12, 24)))
    assert runs_on(calendar, date(2026, 12, 23), _names(date(2026, 12, 23)))


def test_without_holiday_names_bank_holidays_are_ordinary_days():
    calendar = Calendar(days=frozenset(range(5)), bank_holidays_off=frozenset({"SpringBank"}))
    assert runs_on(calendar, date(2026, 5, 25))


def test_special_days():
    calendar = Calendar(
        days=frozenset(range(5)),
        bank_holidays_off=frozenset({"SpringBank"}),
        dates_on=((date(2026, 5, 25), date(2026, 5, 25)), (date(2026, 10, 10), date(2026, 10, 11))),
        dates_off=((date(2026, 10, 12), date(2026, 10, 13)),),
        school_days_only=((date(2026, 9, 1), date(2026, 9, 30)),),
    )
    names = _names(date(2026, 5, 25))
    assert runs_on(calendar, date(2026, 5, 25), names)  # a special day beats the bank holiday
    assert runs_on(calendar, date(2026, 10, 10))  # a Saturday, and beyond the school days
    assert not runs_on(calendar, date(2026, 10, 12))
    assert not runs_on(calendar, date(2026, 10, 13))


def test_special_days_off_beat_special_days_on():
    both = ((date(2026, 10, 9), date(2026, 10, 9)),)
    assert not runs_on(Calendar(days=frozenset(range(7)), dates_on=both, dates_off=both), date(2026, 10, 9))


def test_school_days_only():
    calendar = Calendar(days=frozenset(range(5)), school_days_only=((date(2026, 9, 1), date(2026, 10, 23)),))
    assert runs_on(calendar, date(2026, 10, 9))
    assert not runs_on(calendar, date(2026, 10, 10))  # a Saturday in term still isn't a weekday
    assert not runs_on(calendar, date(2026, 10, 26))  # half term


def test_not_on_school_days():
    calendar = Calendar(days=frozenset(range(5)), not_on_school_days=((date(2026, 9, 1), date(2026, 10, 23)),))
    assert not runs_on(calendar, date(2026, 10, 9))
    assert runs_on(calendar, date(2026, 10, 26))


def test_bank_holiday_operation_beats_school_rules():
    calendar = Calendar(days=frozenset(), bank_holidays_on=frozenset({"SpringBank"}),
                        school_days_only=((date(2026, 9, 1), date(2026, 9, 30)),))
    assert runs_on(calendar, date(2026, 5, 25), _names(date(2026, 5, 25)))


# Calendars from operating profiles.

def _profile(body):
    return f"<OperatingProfile>{body}</OperatingProfile>"


def _calendar(profile="", journey="", extra=""):
    return _one(_txc(LINKS, profile=profile, journey=journey, extra=extra)).calendar


@pytest.mark.parametrize("days, expected", [
    ("<MondayToFriday/>", {0, 1, 2, 3, 4}),
    ("<MondayToSaturday/>", {0, 1, 2, 3, 4, 5}),
    ("<MondayToSunday/>", {0, 1, 2, 3, 4, 5, 6}),
    ("<MondayToThursday/>", {0, 1, 2, 3}),
    ("<Weekend/>", {5, 6}),
    ("<NotSaturday/>", {0, 1, 2, 3, 4, 6}),
    ("<Saturday/><Sunday/>", {5, 6}),
    ("<Tuesday/>", {1}),
])
def test_days_of_week(days, expected):
    calendar = _calendar(_profile(f"<RegularDayType><DaysOfWeek>{days}</DaysOfWeek></RegularDayType>"))
    assert calendar.days == expected


def test_holidays_only_has_no_regular_days():
    profile = _profile(
        "<RegularDayType><HolidaysOnly/></RegularDayType>"
        "<BankHolidayOperation><DaysOfOperation><AllBankHolidays/></DaysOfOperation></BankHolidayOperation>"
    )
    calendar = _calendar(profile)
    assert calendar.days == frozenset()
    assert {"GoodFriday", "SpringBank", "BoxingDayHoliday", "LateSummerBankHolidayNotScotland"} <= calendar.bank_holidays_on
    assert "ChristmasEve" not in calendar.bank_holidays_on


def test_no_operating_profile_runs_every_day():
    calendar = _calendar()
    assert calendar.days == frozenset(range(7))
    assert calendar.start == date(2026, 1, 1) and calendar.end is None


def test_holiday_groups_are_expanded():
    profile = _profile(
        "<RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek></RegularDayType>"
        "<BankHolidayOperation><DaysOfNonOperation><Christmas/><EarlyRunOff/><HolidayMondays/></DaysOfNonOperation>"
        "<DaysOfOperation><GoodFriday/></DaysOfOperation></BankHolidayOperation>"
    )
    calendar = _calendar(profile)
    assert {"ChristmasDay", "BoxingDay", "ChristmasEve", "NewYearsEve", "EasterMonday", "SpringBank"} <= calendar.bank_holidays_off
    assert "GoodFriday" not in calendar.bank_holidays_off
    assert calendar.bank_holidays_on == {"GoodFriday"}


def test_other_public_holidays_are_dates():
    profile = _profile(
        "<RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek></RegularDayType>"
        "<BankHolidayOperation><DaysOfNonOperation><OtherPublicHoliday><Description>Coronation</Description>"
        "<Date>2023-05-08</Date></OtherPublicHoliday></DaysOfNonOperation></BankHolidayOperation>"
    )
    assert _calendar(profile).dates_off == ((date(2023, 5, 8), date(2023, 5, 8)),)


def test_special_days_operation():
    profile = _profile(
        "<RegularDayType><DaysOfWeek><Sunday/></DaysOfWeek></RegularDayType>"
        "<SpecialDaysOperation>"
        "<DaysOfOperation><DateRange><StartDate>2026-12-27</StartDate><EndDate>2026-12-31</EndDate></DateRange></DaysOfOperation>"
        "<DaysOfNonOperation><DateRange><StartDate>2026-12-20</StartDate></DateRange></DaysOfNonOperation>"
        "</SpecialDaysOperation>"
    )
    calendar = _calendar(profile)
    assert calendar.dates_on == ((date(2026, 12, 27), date(2026, 12, 31)),)
    assert calendar.dates_off == ((date(2026, 12, 20), date(2026, 12, 20)),)  # no end: one day


def test_a_journeys_own_profile_overrides_the_services():
    service = _profile("<RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek></RegularDayType>")
    journey = _profile("<RegularDayType><DaysOfWeek><Saturday/></DaysOfWeek></RegularDayType>")
    calendar = _calendar(service, journey=journey)
    assert calendar.days == {5}
    assert calendar.start == date(2026, 1, 1)  # the service's operating period still applies


SCHOOLS = """<ServicedOrganisations>
 <ServicedOrganisation><OrganisationCode>TERM</OrganisationCode>
  <WorkingDays><DateRange><StartDate>2026-09-01</StartDate><EndDate>2026-10-23</EndDate></DateRange>
  <DateRange><StartDate>2026-11-02</StartDate><EndDate>2026-12-18</EndDate></DateRange></WorkingDays>
 </ServicedOrganisation>
 <ServicedOrganisation><OrganisationCode>HOLS</OrganisationCode>
  <Holidays><DateRange><StartDate>2026-10-26</StartDate><EndDate>2026-10-30</EndDate></DateRange></Holidays>
 </ServicedOrganisation>
 <ServicedOrganisation><OrganisationCode>NONE</OrganisationCode></ServicedOrganisation>
</ServicedOrganisations>"""


def _school(kind, group, ref):
    return _profile(
        "<RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek></RegularDayType>"
        f"<ServicedOrganisationDayType><{kind}><{group}><ServicedOrganisationRef>{ref}</ServicedOrganisationRef>"
        f"</{group}></{kind}></ServicedOrganisationDayType>"
    )


TERM = ((date(2026, 9, 1), date(2026, 10, 23)), (date(2026, 11, 2), date(2026, 12, 18)))
HALF_TERM = ((date(2026, 10, 26), date(2026, 10, 30)),)


def test_school_days_only_from_working_days():
    calendar = _calendar(_school("DaysOfOperation", "WorkingDays", "TERM"), extra=SCHOOLS)
    assert calendar.school_days_only == TERM
    assert runs_on(calendar, date(2026, 10, 9)) and not runs_on(calendar, date(2026, 10, 26))


def test_not_on_school_days_from_working_days():
    calendar = _calendar(_school("DaysOfNonOperation", "WorkingDays", "TERM"), extra=SCHOOLS)
    assert calendar.not_on_school_days == TERM
    assert not runs_on(calendar, date(2026, 10, 9)) and runs_on(calendar, date(2026, 10, 26))


def test_school_holidays_are_used_the_same_way():
    only = _calendar(_school("DaysOfOperation", "Holidays", "HOLS"), extra=SCHOOLS)
    assert only.school_days_only == HALF_TERM  # runs only within these days
    never = _calendar(_school("DaysOfNonOperation", "Holidays", "HOLS"), extra=SCHOOLS)
    assert never.not_on_school_days == HALF_TERM


def test_the_other_list_is_used_inverted_when_one_is_missing():
    # Running in the holidays of an organisation that gives only working days:
    # never on those working days.
    calendar = _calendar(_school("DaysOfOperation", "Holidays", "TERM"), extra=SCHOOLS)
    assert calendar.not_on_school_days == TERM and calendar.school_days_only == ()
    # Not running on the working days of one that gives only holidays: only in the holidays.
    calendar = _calendar(_school("DaysOfNonOperation", "WorkingDays", "HOLS"), extra=SCHOOLS)
    assert calendar.school_days_only == HALF_TERM


def test_school_days_only_with_no_known_school_days_runs_on_none():
    calendar = _calendar(_school("DaysOfOperation", "WorkingDays", "NONE"), extra=SCHOOLS)
    assert calendar.days == frozenset()
    assert not any(runs_on(calendar, date(2026, 10, 5) + timedelta(days=n)) for n in range(7))
    # Not running on unknown school days restricts nothing.
    assert _calendar(_school("DaysOfNonOperation", "WorkingDays", "MISSING"), extra=SCHOOLS).days == set(range(5))


# Building a schedule, with BODS and NaPTAN faked.

NOW = datetime(2026, 10, 9, 3, 0)
STOPS = {"A": STOP_A, "B": STOP_B}
BNML_LINK = "https://data.bus-data.dft.gov.uk/timetable/dataset/1/download/"
HIPK_LINK = "https://data.bus-data.dft.gov.uk/timetable/dataset/2/download/"
ELSEWHERE = b'<?xml version="1.0"?><TransXChange xmlns="http://www.transxchange.org.uk/"><broken'


def _zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeBods:
    def __init__(self):
        self.datasets = {"BNML": [BNML_LINK], "HIPK": [HIPK_LINK]}
        # A document that names neither stop is never parsed: this one would fail if it were.
        self.blobs = {BNML_LINK: _zip({"341.xml": TXC_341, "237.xml": TXC_237, "201.xml": ELSEWHERE}), HIPK_LINK: TXC_61}
        self.asked = []
        self.fetched = []
        self.naptan_asked = None
        self.naptan_error = None

    def find(self, api_key, noc, line=None):
        self.asked.append((api_key, noc, line))
        return self.datasets.get(noc, [])

    def fetch(self, url, file):
        self.fetched.append((url, file))
        file.write(self.blobs[url])

    def naptan(self, atcos):
        self.naptan_asked = set(atcos)
        if self.naptan_error:
            raise self.naptan_error
        return {STOP_A: ("Henry Street", 53.44384, -1.948999), "1000DGHSW936": ("Market Hall", 53.443239, -1.949828)}

    def build(self, now=NOW, operators=("BNML", "HIPK"), **options):
        return build_schedule(STOPS, operators, "secret-key", now, find_datasets=self.find, fetch=self.fetch,
                              naptan=self.naptan, **options)


def test_build_collects_journeys_at_your_stops_from_every_operator(tmp_path):
    bods = FakeBods()
    schedule = bods.build(scratch=tmp_path)
    assert schedule.built == NOW
    assert schedule.stops == STOPS
    assert {(j.line, j.operator) for j in schedule.journeys} == {("341", "BNGN"), ("61", "HIPK")}
    assert len(schedule.journeys) == 9
    assert bods.asked == [("secret-key", "BNML", None), ("secret-key", "HIPK", None)]
    assert [url for url, _ in bods.fetched] == [BNML_LINK, HIPK_LINK]
    assert list(tmp_path.iterdir()) == []  # downloads are gone once used


def test_a_timetable_that_has_ended_is_left_out_and_sets_valid_until():
    # The 237 summer timetable ended on 29 August; in October it is left out.
    assert FakeBods().build().valid_until == date(2031, 2, 23)  # the 341's end; the 61 has none
    summer = FakeBods().build(now=datetime(2026, 8, 1, 3, 0))
    assert "237" in {j.line for j in summer.journeys}
    assert summer.valid_until == date(2026, 8, 29)


def test_positions_cover_the_stretch_before_your_stops_and_the_stop_after():
    bods = FakeBods()
    schedule = bods.build()
    into = next(j for j in schedule.journeys if j.journey_code == "1007")  # 23 calls, ending at Stop A
    out = next(j for j in schedule.journeys if j.journey_code == "1012")  # starting at Stop A
    assert {c.atco for c in into.calls[-21:]} <= bods.naptan_asked  # Stop A and the 20 before it
    assert into.calls[0].atco not in bods.naptan_asked and into.calls[1].atco not in bods.naptan_asked
    assert out.calls[1].atco in bods.naptan_asked  # the stop after yours
    assert out.calls[2].atco not in bods.naptan_asked
    assert set(schedule.positions) == bods.naptan_asked
    # NaPTAN's position where it has one, else the timetable's own.
    assert schedule.positions[STOP_A] == StopPosition("Henry Street", 53.44384, -1.948999)
    assert schedule.positions[STOP_B] == StopPosition("Glossop, Henry Street (Stop B)", 53.443841, -1.94921)


def test_without_naptan_the_timetables_positions_are_used(caplog):
    bods = FakeBods()
    bods.naptan_error = OSError("naptan down")
    schedule = bods.build()
    assert set(schedule.positions) == bods.naptan_asked
    assert schedule.positions[STOP_A] == StopPosition("Henry Street", 53.44384, -1.948999)
    assert schedule.positions["1000DGHSW936"].name == "Market Hall"
    assert "naptan down" in caplog.text


def test_duplicate_journeys_are_kept_once():
    bods = FakeBods()
    bods.blobs[BNML_LINK] = _zip({"341.xml": TXC_341, "341-again.xml": TXC_341})
    bods.datasets["HIPK"] = []
    assert len(bods.build().journeys) == 5


def _versions(*documents):
    """A schedule built from these documents, one data set of BNML's."""
    bods = FakeBods()
    bods.blobs[BNML_LINK] = _zip({f"{i}.xml": document for i, document in enumerate(documents)})
    bods.datasets["HIPK"] = []
    return bods.build()


def _departures(schedule, day):
    return sorted(f"{_at(j, STOP_A) // 60:02d}:{_at(j, STOP_A) % 60:02d}" for j in journeys_on(schedule, day))


def test_a_later_version_of_a_service_replaces_the_earlier_from_the_day_it_starts():
    # BODS keeps the old version, open-ended, beside the one replacing it.
    old = _txc(LINKS, departure="07:37:00", start="2026-01-01")  # at Stop A 07:42
    new = _txc(LINKS, departure="07:40:00", start="2026-10-20")  # at Stop A 07:45
    schedule = _versions(old, new)
    assert _departures(schedule, date(2026, 10, 25)) == ["07:45"]
    assert _departures(schedule, date(2026, 10, 19)) == ["07:42"]
    assert schedule.valid_until == date(2026, 10, 19)  # built again once the old one has ended
    assert set(_versions(new, old).journeys) == set(schedule.journeys)  # whichever comes first


def test_the_higher_revision_wins_between_versions_starting_the_same_day():
    first = _txc(LINKS, departure="07:37:00", start="2026-10-01", revision=1)
    second = _txc(LINKS, departure="07:40:00", start="2026-10-01", revision=2)
    assert _departures(_versions(second, first), date(2026, 10, 25)) == ["07:45"]


def test_versions_of_the_same_revision_starting_the_same_day_are_both_kept():
    # One version of a service may be split across documents.
    one = _txc(LINKS, departure="07:37:00", start="2026-10-01", revision=1)
    other = _txc(LINKS, departure="07:40:00", start="2026-10-01", revision=1)
    assert _departures(_versions(one, other), date(2026, 10, 25)) == ["07:42", "07:45"]


def test_a_temporary_version_switches_the_earlier_one_off_only_while_it_runs():
    # Roadworks: a timetable for two weeks, then the usual one again.
    usual = _txc(LINKS, departure="07:37:00", start="2026-01-01")  # at Stop A 07:42
    roadworks = _txc(LINKS, departure="07:40:00", start="2026-10-20", end="2026-11-03")  # 07:45
    schedule = _versions(usual, roadworks)
    assert _departures(schedule, date(2026, 10, 19)) == ["07:42"]
    assert _departures(schedule, date(2026, 10, 20)) == ["07:45"]
    assert _departures(schedule, date(2026, 10, 25)) == ["07:45"]
    assert _departures(schedule, date(2026, 11, 3)) == ["07:45"]
    assert _departures(schedule, date(2026, 11, 4)) == ["07:42"]
    assert _departures(schedule, date(2026, 11, 10)) == ["07:42"]
    assert schedule.valid_until == date(2026, 11, 3)  # built again once the roadworks end


def test_a_temporary_version_beats_the_earlier_one_s_own_extra_days():
    usual = _txc(LINKS, departure="07:37:00", start="2026-01-01", profile=_profile(
        "<RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek></RegularDayType>"
        "<SpecialDaysOperation><DaysOfOperation><DateRange><StartDate>2026-10-25</StartDate></DateRange>"
        "</DaysOfOperation></SpecialDaysOperation>"))
    roadworks = _txc(LINKS, departure="07:40:00", start="2026-10-20", end="2026-11-03")
    assert _departures(_versions(usual, roadworks), date(2026, 10, 25)) == ["07:45"]  # a Sunday


def test_a_temporary_version_after_the_earlier_one_has_been_replaced_changes_nothing():
    old = _txc(LINKS, departure="07:37:00", start="2026-01-01")
    new = _txc(LINKS, departure="07:40:00", start="2026-10-20")
    later = _txc(LINKS, departure="07:43:00", start="2026-12-01", end="2026-12-14")
    schedule = _versions(old, new, later)
    replaced = [j for j in schedule.journeys if j.calendar.start == date(2026, 1, 1)]
    assert [j.calendar.end for j in replaced] == [date(2026, 10, 19)]
    assert replaced[0].calendar.dates_off == ()  # nothing to switch off past its end
    assert _departures(schedule, date(2026, 12, 5)) == ["07:48"]
    assert _departures(schedule, date(2026, 12, 15)) == ["07:45"]


def test_a_later_version_that_has_already_ended_leaves_the_earlier_one_running():
    usual = _txc(LINKS, departure="07:37:00", start="2026-01-01")
    ended = _txc(LINKS, departure="07:40:00", start="2026-06-01", end="2026-09-30")
    schedule = _versions(usual, ended)
    assert _departures(schedule, date(2026, 10, 25)) == ["07:42"]


def test_other_services_are_not_ended_by_a_later_one():
    old = _txc(LINKS, departure="07:37:00", start="2026-01-01")
    other = _txc(LINKS, departure="07:40:00", start="2026-10-20").replace(b"S1<", b"S2<")
    assert _departures(_versions(old, other), date(2026, 10, 25)) == ["07:42", "07:45"]


def test_lines_narrow_the_data_sets_and_each_link_is_fetched_once():
    bods = FakeBods()
    bods.datasets["HIPK"] = [HIPK_LINK, BNML_LINK]
    bods.build(lines=["61", "341"])
    assert ("secret-key", "BNML", "341") in bods.asked and ("secret-key", "HIPK", "61") in bods.asked
    assert [url for url, _ in bods.fetched] == [BNML_LINK, HIPK_LINK]


def test_one_download_at_a_time():
    bods = FakeBods()
    held = []
    original = bods.fetch

    def fetch(url, file):
        held.append(file)
        assert all(earlier.closed for earlier in held[:-1])  # the last one's file is already gone
        original(url, file)

    bods.fetch = fetch
    bods.build()
    assert len(held) == 2


def test_documents_are_unpacked_one_at_a_time(tmp_path):
    from departureboard.timetable.build import _documents

    blob = _zip({"a.xml": b"<a/>", "b.xml": b"<b/>"})
    documents = _documents([("X", "https://x/1")], lambda url, file: file.write(blob), tmp_path)
    assert next(documents) == ("X", "a.xml", b"<a/>")
    assert next(documents) == ("X", "b.xml", b"<b/>")


def test_a_document_for_your_stops_too_big_to_parse_is_skipped_and_the_rest_built(caplog):
    # The 61's document is the smallest.
    limit = len(TXC_341) - 1
    assert len(TXC_61) <= limit < len(TXC_237)
    schedule = FakeBods().build(limits=DocumentLimits(parse=limit))
    assert {j.line for j in schedule.journeys} == {"61"}
    assert f"Skipped 341.xml: {len(TXC_341):,} bytes, more than the {limit:,} bytes this install can read" in caplog.text


def test_a_document_too_big_to_read_is_skipped_and_the_rest_built(caplog):
    bods = FakeBods()
    big = TXC_237 + b" " * len(TXC_341)
    bods.blobs[BNML_LINK] = _zip({"341.xml": TXC_341, "big.xml": big})
    schedule = bods.build(limits=DocumentLimits(read=len(TXC_341)))
    assert {j.line for j in schedule.journeys} == {"341", "61"}
    assert f"Skipped big.xml: {len(big):,} bytes" in caplog.text


def test_the_journey_limit_counts_every_document_s_journeys():
    # 5 of the 341, then the 237's 4 (ended, so let go once read), then 4 of the 61.
    assert len(FakeBods().build(limits=DocumentLimits(journeys=9)).journeys) == 9
    with pytest.raises(TimetableError, match="More than 8 journeys call at your stops; refusing"):
        FakeBods().build(limits=DocumentLimits(journeys=8))


def test_a_document_with_more_journeys_at_your_stops_than_the_limit_fails_as_they_are_read(monkeypatch):
    # Checked as each is kept, not once the document is read: a document can
    # hold more of them than the memory there is.
    read = []
    journey = timetable._journey
    monkeypatch.setattr(timetable, "_journey", lambda *args, **options: read.append(1) or journey(*args, **options))
    with pytest.raises(TimetableError, match="More than 2 journeys call at your stops; refusing"):
        FakeBods().build(limits=DocumentLimits(journeys=2))
    assert len(read) == 3  # of the 341's 5


def test_a_build_with_every_document_for_your_stops_skipped_fails():
    with pytest.raises(TimetableError, match="No timetabled journeys"):
        FakeBods().build(limits=DocumentLimits(parse=100))


def test_a_big_document_that_never_mentions_your_stops_is_skipped_unread():
    biggest = max(len(TXC_341), len(TXC_237), len(TXC_61))
    bods = FakeBods()
    bods.blobs[BNML_LINK] = _zip({"341.xml": TXC_341, "big.xml": ELSEWHERE + b" " * biggest})
    assert {j.line for j in bods.build(limits=DocumentLimits(parse=biggest)).journeys} == {"341", "61"}


def test_no_journeys_at_your_stops_is_an_error():
    bods = FakeBods()
    bods.datasets = {"BNML": [HIPK_LINK]}
    with pytest.raises(TimetableError, match="No timetabled journeys"):
        build_schedule({"A": "NOWHERE"}, ["BNML"], "k", NOW, find_datasets=bods.find, fetch=bods.fetch,
                       naptan=bods.naptan)


@pytest.mark.parametrize("url", ["http://example.com/x", "file:///etc/passwd", "ftp://example.com/x"])
def test_download_refuses_anything_but_https(url):
    with pytest.raises(TimetableError, match="https"):
        download(url, io.BytesIO())


def test_download_is_capped(monkeypatch):
    from departureboard.core import net

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(net, "urlopen", lambda request, timeout: Response(b"x" * 300_000))
    file = io.BytesIO()
    download("https://example.com/ok", file, max_bytes=300_000)
    assert len(file.getvalue()) == 300_000
    with pytest.raises(TimetableError, match="larger"):
        download("https://example.com/big", io.BytesIO(), max_bytes=299_999)


def test_download_gets_long_enough_for_a_big_zip_on_a_slow_line(monkeypatch):
    # A timetable zip is tens of MB; the usual deadline would cut it off.
    import time

    from departureboard.core import net

    seen = []
    monkeypatch.setattr(net, "urlopen", lambda request, timeout: seen.append(request) or io.BytesIO(b"zip"))
    before = time.monotonic()
    download("https://example.com/ok", io.BytesIO())
    assert before + net.DOWNLOAD_DEADLINE <= seen[0].deadline <= time.monotonic() + net.DOWNLOAD_DEADLINE


# Keeping it.

def _schedule(built=NOW, stops=STOPS, valid_until=None, operators=("BNML",)):
    journeys = tuple(parse_journeys(TXC_61, OURS))
    return Schedule(built, valid_until, dict(stops), journeys, {STOP_B: StopPosition("Henry Street", 53.4, -1.9)},
                    operators=operators, format=FORMAT)


def test_save_and_load_round_trip(tmp_path):
    schedule = _schedule()
    save(tmp_path / "state", schedule)
    path = tmp_path / "state" / FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert load(tmp_path / "state") == schedule
    assert not (tmp_path / "state" / "bus-timetable.tmp").exists()


def test_a_timetable_too_big_to_load_is_not_saved_and_the_old_one_is_kept(tmp_path, monkeypatch):
    old = _schedule()
    save(tmp_path, old)
    big = replace(old, built=NOW + timedelta(days=1))
    monkeypatch.setattr(timetable, "MAX_FILE_BYTES", len(big.to_json()) - 1)
    with pytest.raises(TimetableError, match="larger than"):
        save(tmp_path, big)
    monkeypatch.undo()
    assert load(tmp_path) == old


def test_a_timetable_just_small_enough_is_saved_and_loads(tmp_path, monkeypatch):
    schedule = _schedule()
    monkeypatch.setattr(timetable, "MAX_FILE_BYTES", len(schedule.to_json()))
    save(tmp_path, schedule)
    assert load(tmp_path) == schedule


def test_load_without_a_file_or_with_a_bad_one(tmp_path):
    assert load(tmp_path) is None
    (tmp_path / FILE_NAME).write_text("{ not json")
    assert load(tmp_path) is None
    (tmp_path / FILE_NAME).write_text("[]")
    assert load(tmp_path) is None
    (tmp_path / FILE_NAME).write_text('{"built": "2026-10-09T03:00:00"}')
    assert load(tmp_path) is None


def test_a_timetable_with_bad_roads_loads_with_straight_lines(tmp_path, caplog):
    import json

    save(tmp_path, _schedule())
    data = json.loads((tmp_path / FILE_NAME).read_text())
    data["shapes"] = {"A>B": [[53.4, -1.9]], "B>C": [[float("nan"), -1.9]]}
    (tmp_path / FILE_NAME).write_text(json.dumps(data))
    loaded = load(tmp_path)
    assert loaded is not None and loaded.journeys == _schedule().journeys
    assert loaded.shapes == {}
    assert "finite" in caplog.text


class FakeBuild:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, stops, operators, api_key, now, **options):
        self.calls.append((dict(stops), list(operators), api_key, now, options))
        if self.error:
            raise self.error
        return self.result or _schedule(built=now, stops=stops)


def test_refresh_builds_when_there_is_none(tmp_path):
    build = FakeBuild()
    schedule = refresh(tmp_path, STOPS, ["BNML"], NOW, "k", lines=["341"], build=build)
    assert schedule == load(tmp_path)
    assert build.calls == [(STOPS, ["BNML"], "k", NOW,
                            {"lines": ["341"], "scratch": tmp_path, "limits": DocumentLimits()})]


def test_refresh_builds_within_the_limits_it_is_given(tmp_path):
    build = FakeBuild()
    limits = DocumentLimits(read=100, parse=25)
    refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build, limits=limits)
    assert build.calls[0][4]["limits"] == limits


def test_refresh_keeps_a_fresh_schedule_without_building(tmp_path):
    save(tmp_path, _schedule(built=NOW))
    build = FakeBuild()
    for days in range(7):
        assert refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(days=days, hours=23), "k", build=build) == _schedule()
    assert build.calls == []


@pytest.mark.parametrize("saved, now, stops", [
    (_schedule(built=NOW), NOW + timedelta(days=7), STOPS),  # a week old
    (_schedule(built=NOW), NOW, {"A": STOP_A}),  # stops changed
    (_schedule(built=NOW, valid_until=date(2026, 10, 9)), NOW + timedelta(days=1), STOPS),  # a timetable ended
    (_schedule(built=NOW + timedelta(days=30)), NOW, STOPS),  # built when the clock was wrong
    (_schedule(operators=("BNML", "HIPK")), NOW, STOPS),  # operators changed
    (_schedule(operators=()), NOW, STOPS),  # saved before operators were recorded
])
def test_refresh_rebuilds_when_due(tmp_path, saved, now, stops):
    save(tmp_path, saved)
    build = FakeBuild()
    schedule = refresh(tmp_path, stops, ["BNML"], now, "k", build=build)
    assert len(build.calls) == 1
    assert schedule.built == now and load(tmp_path).built == now


def test_refresh_can_be_forced(tmp_path):
    save(tmp_path, _schedule())
    build = FakeBuild()
    refresh(tmp_path, STOPS, ["BNML", "HIPK"], NOW, "k", force=True, build=build)
    assert len(build.calls) == 1


def test_a_failed_refresh_keeps_the_old_schedule_and_never_logs_the_key(tmp_path, caplog):
    old = _schedule(built=NOW - timedelta(days=8))
    save(tmp_path, old)
    build = FakeBuild(error=OSError("GET https://data.bus-data.dft.gov.uk/api/v1/dataset/?api_key=SECRET failed"))
    assert refresh(tmp_path, STOPS, ["BNML"], NOW, "SECRET", build=build) == old
    assert load(tmp_path) == old
    assert "not rebuilt" in caplog.text
    assert "SECRET" not in caplog.text


def test_a_failed_first_build_leaves_nothing(tmp_path):
    assert refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=FakeBuild(error=TimetableError("no"))) is None
    assert not (tmp_path / FILE_NAME).exists()


def test_a_failed_build_is_tried_again_after_six_hours_not_every_hour(tmp_path, caplog):
    caplog.set_level("INFO")
    old = _schedule(built=NOW - timedelta(days=8))
    save(tmp_path, old)
    build = FakeBuild(error=TimetableError("BODS down"))
    for hours in range(6):
        assert refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(hours=hours, minutes=59), "k", build=build) == old
    assert len(build.calls) == 1
    assert "failed at 03:59: trying again after 09:59" in caplog.text
    build.error = None
    later = NOW + timedelta(hours=6, minutes=59)
    assert refresh(tmp_path, STOPS, ["BNML"], later, "k", build=build).built == later
    assert len(build.calls) == 2
    assert [p.name for p in tmp_path.iterdir()] == [FILE_NAME]  # a build that works forgets the failure


def test_a_failed_first_build_waits_too(tmp_path):
    build = FakeBuild(error=TimetableError("no"))
    refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build)
    assert refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(hours=1), "k", build=build) is None
    assert len(build.calls) == 1


@pytest.mark.parametrize("stops, operators, force", [
    (STOPS, ["BNML"], True),  # asked to
    ({"A": STOP_A}, ["BNML"], False),  # stops changed
    (STOPS, ["BNML", "HIPK"], False),  # operators changed
])
def test_a_failed_build_is_tried_again_at_once_when_forced_or_the_stops_or_operators_change(
        tmp_path, stops, operators, force):
    build = FakeBuild(error=TimetableError("no"))
    refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build)
    refresh(tmp_path, stops, operators, NOW + timedelta(minutes=5), "k", force=force, build=build)
    assert len(build.calls) == 2


def test_a_failed_build_is_tried_again_at_once_with_another_key(tmp_path):
    # A refused key fails the build; saving the right one mustn't wait 6 hours.
    build = FakeBuild(error=TimetableError("no"))
    refresh(tmp_path, STOPS, ["BNML"], NOW, "wrong-key", build=build)
    refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(minutes=5), "wrong-key", build=build)
    assert len(build.calls) == 1
    refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(minutes=10), "right-key", build=build)
    assert len(build.calls) == 2
    assert "key" not in (tmp_path / timetable.FAILED_FILE).read_text()  # only a digest


def test_a_cancelled_build_is_not_kept_as_failed(tmp_path):
    old = _schedule(built=NOW - timedelta(days=8))
    save(tmp_path, old)
    build = FakeBuild(error=TimetableError("Cancelled"))
    assert refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build, cancelled=lambda: True) == old
    assert not (tmp_path / timetable.FAILED_FILE).exists()
    build.error = None
    assert refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(minutes=5), "k", build=build).built == NOW + timedelta(
        minutes=5)
    assert len(build.calls) == 2


def test_a_failure_recorded_when_the_clock_was_ahead_does_not_hold_back_a_build(tmp_path):
    build = FakeBuild(error=TimetableError("no"))
    refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(days=30), "k", build=build)
    refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build)
    assert len(build.calls) == 2


@pytest.mark.parametrize("text", ["", "{", "[]", '{"at": "soon", "for": "x"}', '{"at": 5}', "x" * 5000],
                         ids=["empty", "half", "list", "bad time", "no digest", "too big"])
def test_a_failed_build_record_that_doesnt_fit_is_ignored(tmp_path, text):
    (tmp_path / timetable.FAILED_FILE).write_text(text)
    build = FakeBuild()
    refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build)
    assert len(build.calls) == 1


def test_the_failed_build_record_is_private_and_a_link_in_its_place_is_refused(tmp_path):
    build = FakeBuild(error=TimetableError("no"))
    refresh(tmp_path, STOPS, ["BNML"], NOW, "k", build=build)
    path = tmp_path / timetable.FAILED_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert STOP_A not in path.read_text()  # only a digest of what it was for
    elsewhere = tmp_path / "elsewhere.json"
    path.rename(elsewhere)
    path.symlink_to(elsewhere)
    refresh(tmp_path, STOPS, ["BNML"], NOW + timedelta(minutes=5), "k", build=build)
    assert len(build.calls) == 2


def test_refresh_end_to_end_with_fakes(tmp_path):
    bods = FakeBods()

    def build(*args, **options):
        return build_schedule(*args, find_datasets=bods.find, fetch=bods.fetch, naptan=bods.naptan, **options)

    schedule = refresh(tmp_path / "state", STOPS, ["BNML", "HIPK"], NOW, "k", build=build)
    assert len(schedule.journeys) == 9
    assert load(tmp_path / "state") == schedule
    assert [p.name for p in (tmp_path / "state").iterdir()] == [FILE_NAME]


def test_operators_are_compared_as_a_set():
    assert due(_schedule(operators=("BNML", "HIPK")), STOPS, NOW, ["HIPK", "BNML", "HIPK"]) is None
    assert due(_schedule(operators=("BNML",)), STOPS, NOW, ["BNML", "HIPK"]) == "your operators changed"
    assert due(_schedule(operators=("BNML",)), STOPS, NOW) is None  # not asked about operators


def test_the_build_records_its_operators(tmp_path):
    bods = FakeBods()
    schedule = build_schedule(STOPS, ["HIPK", "BNML"], "k", NOW, find_datasets=bods.find, fetch=bods.fetch,
                              naptan=bods.naptan, scratch=tmp_path)
    assert schedule.operators == ("BNML", "HIPK")
    save(tmp_path, schedule)
    assert load(tmp_path).operators == ("BNML", "HIPK")


def test_a_timetable_that_starts_later_is_kept_but_never_runs_before_it_starts():
    # The 341's timetable starts on 17 February: built on the 1st, it is kept
    # for the timetable change notice, and runs from the 17th only.
    early = FakeBods().build(now=datetime(2026, 2, 1, 3, 0))
    route = [j for j in early.journeys if j.line == "341"]
    assert route and all(j.calendar.start == date(2026, 2, 17) for j in route)
    assert not [j for j in journeys_on(early, date(2026, 2, 16)) if j.line == "341"]
    assert [j for j in journeys_on(early, date(2026, 2, 17)) if j.line == "341"]
    from departureboard.dashboard.area import timetable_changes

    assert {"line": "341", "operator": "BNGN", "from": "2026-02-17"} in timetable_changes(early, date(2026, 2, 1))


# Inbound-only journeys: buses coming in to a stand near your stop.

FAR_STAND = "1000DGHSFAR1"
FAR = StopPosition("Far Stand", 53.44384, -1.942968)  # about 400 m east of Stop A
PLACES = {**POSITIONS, FAR_STAND: FAR}
COMING_IN = [JUNCTION, GRAPES, SUNLAWS, ST_MARYS_E, ARUNDEL]  # 10:00 to 10:08
LEFT_JUNCTION = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)  # 10:00 BST


def _doc(journeys, places=PLACES, ends=None):
    """A TransXChange document of (code, line, departure, stops) journeys, two
    minutes between stops, placing the stops it has in `places`. Each journey
    is a service of its own, running every day from 1 January 2026 until its
    date in `ends` (by code), if any."""
    ends = ends or {}
    atcos = sorted({atco for *_, calls in journeys for atco in calls})
    stops = "".join(
        f"<AnnotatedStopPointRef><StopPointRef>{atco}</StopPointRef>"
        f"<CommonName>{places[atco].name if atco in places else atco}</CommonName>"
        + (f"<Location><Longitude>{places[atco].lon}</Longitude><Latitude>{places[atco].lat}</Latitude></Location>"
           if atco in places else "")
        + "</AnnotatedStopPointRef>"
        for atco in atcos
    )
    sections = "".join(
        f'<JourneyPatternSection id="s{i}">'
        + "".join(
            f'<JourneyPatternTimingLink id="l{i}-{k}"><From><StopPointRef>{a}</StopPointRef></From>'
            f"<To><StopPointRef>{b}</StopPointRef></To><RunTime>PT2M</RunTime></JourneyPatternTimingLink>"
            for k, (a, b) in enumerate(pairwise(calls))
        )
        + "</JourneyPatternSection>"
        for i, (*_, calls) in enumerate(journeys)
    )
    services = "".join(
        f'<Service><ServiceCode>S{i}</ServiceCode><Lines><Line id="L{line}"><LineName>{line}</LineName></Line></Lines>'
        f"<OperatingPeriod><StartDate>2026-01-01</StartDate>"
        f"{f'<EndDate>{ends[code]}</EndDate>' if code in ends else ''}</OperatingPeriod>"
        f"<RegisteredOperatorRef>o1</RegisteredOperatorRef><StandardService>"
        f'<JourneyPattern id="p{i}"><JourneyPatternSectionRefs>s{i}</JourneyPatternSectionRefs></JourneyPattern>'
        f"</StandardService></Service>"
        for i, (code, line, *_) in enumerate(journeys)
    )
    vehicle_journeys = "".join(
        f"<VehicleJourney><VehicleJourneyCode>{code}</VehicleJourneyCode><ServiceRef>S{i}</ServiceRef>"
        f"<LineRef>L{line}</LineRef><JourneyPatternRef>p{i}</JourneyPatternRef>"
        f"<DepartureTime>{departure}</DepartureTime></VehicleJourney>"
        for i, (code, line, departure, _) in enumerate(journeys)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<TransXChange xmlns="http://www.transxchange.org.uk/">
 <StopPoints>{stops}</StopPoints>
 <JourneyPatternSections>{sections}</JourneyPatternSections>
 <Operators><Operator id="o1"><NationalOperatorCode>BNML</NationalOperatorCode></Operator></Operators>
 <Services>{services}</Services>
 <VehicleJourneys>{vehicle_journeys}</VehicleJourneys>
</TransXChange>""".encode()


def _stand_doc(stand=MARKET_HALL, line="237", places=PLACES):
    """The 237 leaves Stop A at 10:15. A bus of `line` comes in from The
    Junction at 10:00 to `stand` at 10:10, without calling at Stop A."""
    return _doc([("out", "237", "10:15:00", [STOP_A, HOWARD, "1000DHAD0001"]),
                 ("in", line, "10:00:00", [*COMING_IN, stand])], places)


def _build(document, naptan=None, limits=DEFAULT_LIMITS):
    """The schedule for Stop A alone from one document, and the stops NaPTAN
    was asked for. `naptan` is what NaPTAN knows; None if it can't be reached."""
    asked = set()

    def lookup(atcos):
        asked.update(atcos)
        if naptan is None:
            raise OSError("naptan down")
        return {atco: (place.name, place.lat, place.lon) for atco, place in naptan.items() if atco in asked}

    schedule = build_schedule({"A": STOP_A}, ["BNML"], "k", NOW, find_datasets=lambda key, noc, line: [BNML_LINK],
                              fetch=lambda url, file: file.write(document), naptan=lookup, limits=limits)
    return schedule, asked


def _codes(schedule):
    return [(journey.journey_code, journey.inbound_only) for journey in schedule.journeys]


def test_a_journey_ending_at_a_stand_near_your_stop_is_kept_inbound_only():
    assert 80 <= metres(POSITIONS[MARKET_HALL], POSITIONS[STOP_A]) <= 90
    schedule, _ = _build(_stand_doc(), naptan=PLACES)
    assert _codes(schedule) == [("out", False), ("in", True)]
    coming = _by_code(schedule.journeys)["in"]
    assert coming.calls[0] == Call(JUNCTION, 600) and coming.calls[-1] == Call(MARKET_HALL, 610)
    assert schedule.departure_journeys == (_by_code(schedule.journeys)["out"],)
    assert {*COMING_IN, MARKET_HALL} <= set(schedule.positions)  # to place its bus on the way in
    # Asked only for journeys calling at your stops, there are none.
    assert [j.journey_code for j in parse_journeys(_stand_doc(), {STOP_A})] == ["out"]


def test_the_bus_coming_in_to_the_stand_is_the_inbound_bus_and_never_a_departure(tmp_path):
    from departureboard.dashboard.enrich import inbound
    from departureboard.dashboard.snapshot import bus_observations
    from departureboard.sources.bodsbus import BusBoard
    from departureboard.sources.livebus import live_board

    schedule, _ = _build(_stand_doc(), naptan=PLACES)
    save(tmp_path, schedule)
    schedule = load(tmp_path)
    assert _codes(schedule) == [("out", False), ("in", True)]
    vehicle = bus(*HALFWAY, at(10, 9), line="237", origin=JUNCTION, aimed=LEFT_JUNCTION)  # 4 min late
    rows, placed = live_board(schedule, at(10, 9), [vehicle], lambda journey, day: True, 10)
    assert [(row.departure.stop, row.journey.journey_code, row.departure.scheduled) for row in rows] == [
        ("A", "out", "10:15")
    ]
    assert [(p.journey.journey_code, p.vehicle) for p in placed] == [("in", vehicle)]
    assert inbound(rows[0], rows, placed, schedule.positions) == {
        "from": "The Junction", "due": "10:14", "late_min": 4, "where": "3 stops away",
        "text": "Bus arriving from The Junction, 4 min late (due 10:14)",
    }
    observed = bus_observations(BusBoard(rows, schedule, True, None, placed))
    assert [o.identity for o in observed] == [rows[0].key]


def test_inbound_only_journeys_never_set_valid_until():
    # Their timetable ending can't make the board's run out.
    document = _doc([("out", "237", "10:15:00", [STOP_A, HOWARD]), ("in", "237", "10:00:00", [*COMING_IN, MARKET_HALL])],
                    ends={"in": "2026-10-20"})
    schedule, _ = _build(document, naptan=PLACES)
    assert _codes(schedule) == [("out", False), ("in", True)]
    assert schedule.valid_until is None
    ending, _ = _build(_doc([("out", "237", "10:15:00", [STOP_A, HOWARD])], ends={"out": "2026-10-20"}), naptan=PLACES)
    assert ending.valid_until == date(2026, 10, 20)


def test_a_stand_400_m_away_is_not_kept():
    assert 395 <= metres(FAR, POSITIONS[STOP_A]) <= 405
    for naptan in (PLACES, None):  # placed by NaPTAN, or by the timetable alone
        schedule, _ = _build(_stand_doc(FAR_STAND), naptan=naptan)
        assert _codes(schedule) == [("out", False)]
        assert not {FAR_STAND, *COMING_IN} & set(schedule.positions)


def test_a_stand_the_timetable_does_not_place_is_measured_by_naptan():
    near, _ = _build(_stand_doc(places={}), naptan=PLACES)
    assert _codes(near) == [("out", False), ("in", True)]
    far, asked = _build(_stand_doc(FAR_STAND, places={}), naptan=PLACES)
    assert FAR_STAND in asked
    assert _codes(far) == [("out", False)]
    assert not {FAR_STAND, *COMING_IN} & set(far.positions)
    # Placed by neither, it can't be measured.
    unknown, _ = _build(_stand_doc(places={}), naptan=None)
    assert _codes(unknown) == [("out", False)]


def test_a_stand_the_timetable_does_not_place_must_be_in_your_stops_area():
    elsewhere = "2500DGHS0001"  # NaPTAN would put it beside Stop A, but it is never looked at
    schedule, asked = _build(_stand_doc(elsewhere, places={}), naptan={**PLACES, elsewhere: POSITIONS[MARKET_HALL]})
    assert _codes(schedule) == [("out", False)]
    assert elsewhere not in asked


def test_only_lines_leaving_your_stops_count():
    schedule, _ = _build(_stand_doc(line="236"), naptan=PLACES)
    assert _codes(schedule) == [("out", False)]


def test_positions_cover_the_stretch_before_the_stand():
    way_in = [f"1000DGXX{n:04d}" for n in range(24)]
    places = {**PLACES, **{atco: StopPosition(atco, 53.40 + n / 1000, -1.95) for n, atco in enumerate(way_in)}}
    document = _doc([("out", "237", "10:15:00", [STOP_A, HOWARD]),
                     ("in", "237", "09:00:00", [*way_in, MARKET_HALL])], places={})
    schedule, asked = _build(document, naptan=places)
    assert _codes(schedule) == [("out", False), ("in", True)]
    assert set(way_in[-20:]) | {MARKET_HALL} <= set(schedule.positions)  # the stand and the 20 before it
    assert not set(way_in[:-20]) & asked


def test_inbound_only_journeys_are_capped(monkeypatch, caplog):
    from departureboard.timetable import build as timetable

    monkeypatch.setattr(timetable, "MAX_INBOUND_JOURNEYS", 1)
    document = _doc([("out", "237", "10:15:00", [STOP_A, HOWARD]),
                     ("in", "237", "10:00:00", [*COMING_IN, MARKET_HALL]),
                     ("earlier", "237", "09:30:00", [*COMING_IN, MARKET_HALL])])
    schedule, _ = _build(document, naptan=PLACES)
    assert _codes(schedule) == [("out", False), ("in", True)]
    assert "Left out 1 journeys ending near your stops" in caplog.text


def test_inbound_only_journeys_are_capped_at_half_the_journey_limit(caplog):
    # They are held as they are read too, so count against the install's memory.
    document = _doc([("out", "237", "10:15:00", [STOP_A, HOWARD]),
                     ("in", "237", "10:00:00", [*COMING_IN, MARKET_HALL]),
                     ("earlier", "237", "09:30:00", [*COMING_IN, MARKET_HALL])])
    schedule, _ = _build(document, naptan=PLACES, limits=DocumentLimits(journeys=2))
    assert _codes(schedule) == [("out", False), ("in", True)]
    assert "Left out 1 journeys ending near your stops: more than 1" in caplog.text


def test_the_build_keeps_each_stop_s_sign_letter_and_its_format():
    from departureboard.timetable.build import FORMAT

    bods = FakeBods()
    bods.naptan = lambda atcos: {STOP_A: ("Henry Street", 53.44384, -1.948999, "Stop A")}
    schedule = build_schedule(STOPS, ["BNML"], "k", NOW, find_datasets=bods.find, fetch=bods.fetch, naptan=bods.naptan)
    assert schedule.positions[STOP_A].indicator == "Stop A"
    assert schedule.format == FORMAT
    assert load_round_trip(schedule).positions[STOP_A].indicator == "Stop A"


def test_a_timetable_from_an_older_version_is_due_once():
    from dataclasses import replace

    old = replace(_schedule(built=NOW), format=1)
    assert due(old, STOPS, NOW, ["BNML"]) == "it was built by an older version"
    assert due(replace(old, format=timetable_format()), STOPS, NOW, ["BNML"]) is None


def load_round_trip(schedule):
    from departureboard.timetable.model import Schedule
    return Schedule.from_json(schedule.to_json())


def timetable_format():
    from departureboard.timetable.build import FORMAT
    return FORMAT


# The roads between stops, for the maps.


def _routed(document, route=None, previous=None):
    """The schedule for Stop A from one document, every stop placed, with
    the roads `route` finds (the router itself if not given)."""
    options = {"route": route} if route is not None else {}
    return add_roads(_unrouted(document), previous=previous, **options)


def _unrouted(document):
    return build_schedule({"A": STOP_A}, ["BNML"], "k", NOW, find_datasets=lambda key, noc, line: [BNML_LINK],
                          fetch=lambda url, file: file.write(document),
                          naptan=lambda atcos: {a: (p.name, p.lat, p.lon) for a, p in PLACES.items() if a in atcos})


ROADS_DOC = _doc([("out", "237", "10:15:00", [STOP_A, HOWARD, "1000DHAD0001"]),
                  ("again", "237", "11:15:00", [STOP_A, HOWARD, "1000DHAD0001"]),
                  ("in", "237", "10:00:00", [*COMING_IN, MARKET_HALL])])
ROADS_PAIRS = {(STOP_A, HOWARD), (JUNCTION, GRAPES), (GRAPES, SUNLAWS), (SUNLAWS, ST_MARYS_E),
               (ST_MARYS_E, ARUNDEL), (ARUNDEL, MARKET_HALL)}


def _roads_found(pairs, progress=None):
    return {pair: [(53.44351, -1.95562)] for pair in pairs}


def _failing(pairs, progress=None):
    raise RuntimeError("router exploded")


def test_building_asks_no_router():
    schedule = _unrouted(ROADS_DOC)
    assert schedule.shapes == {} and schedule.format == FORMAT


def test_the_road_between_each_pair_of_positioned_stops_is_asked_for_once():
    asked = []

    def route(pairs, progress):
        asked.append(dict(pairs))
        return _roads_found(pairs) | {("NOT", "ASKED"): [(53.0, -1.0)]}

    schedule = _routed(ROADS_DOC, route)
    assert len(asked) == 1
    pairs = asked[0]
    # The departures' and the inbound-only journey's, each once; Hadfield has no position.
    assert next(iter(pairs)) == (STOP_A, HOWARD)
    assert set(pairs) == ROADS_PAIRS
    assert pairs[(ARUNDEL, MARKET_HALL)] == ((POSITIONS[ARUNDEL].lat, POSITIONS[ARUNDEL].lon),
                                             (POSITIONS[MARKET_HALL].lat, POSITIONS[MARKET_HALL].lon))
    assert schedule.shapes[f"{STOP_A}>{HOWARD}"] == ((53.44351, -1.95562),)
    assert len(schedule.shapes) == 6  # not the pair it wasn't asked about
    assert load_round_trip(schedule) == schedule


@pytest.mark.parametrize("route", [
    _failing,
    lambda pairs, progress: None,
    lambda pairs, progress: dict.fromkeys(pairs, "not points"),
])
def test_a_routing_failure_leaves_the_roads_straight(route, caplog):
    schedule = _routed(_stand_doc(), route)
    assert _codes(schedule) == [("out", False), ("in", True)] and schedule.shapes == {}
    assert "drawing straight lines" in caplog.text


def test_by_default_the_router_is_asked_and_survives_being_down(caplog):
    # The tests' network is down (conftest): the router is asked once, then every road is straight.
    schedule = _routed(_stand_doc())
    assert schedule.shapes == {} and schedule.format == FORMAT
    assert "the router isn't answering" in caplog.text


def test_last_week_s_roads_are_kept_for_the_same_stops_and_only_new_ones_asked_for():
    last_week = _routed(ROADS_DOC, _roads_found)
    moved = replace(last_week.positions[ARUNDEL], lat=last_week.positions[ARUNDEL].lat + 0.001)
    shapes = {**last_week.shapes, f"{SUNLAWS}>{ST_MARYS_E}": ((53.4, -1.9),)}
    del shapes[f"{JUNCTION}>{GRAPES}"]  # left straight last week: asked about again
    last_week = replace(last_week, positions={**last_week.positions, ARUNDEL: moved}, shapes=shapes)
    asked = []

    def route(pairs, progress):
        asked.extend(pairs)
        return {pair: [(53.5, -1.8)] for pair in pairs}

    schedule = _routed(ROADS_DOC, route, previous=last_week)
    # Arundel has moved, so both its roads are asked for again.
    assert set(asked) == {(JUNCTION, GRAPES), (ST_MARYS_E, ARUNDEL), (ARUNDEL, MARKET_HALL)}
    assert schedule.shapes[f"{SUNLAWS}>{ST_MARYS_E}"] == ((53.4, -1.9),)  # last week's
    assert schedule.shapes[f"{ARUNDEL}>{MARKET_HALL}"] == ((53.5, -1.8),)  # this week's
    assert len(schedule.shapes) == 6


def test_last_week_s_roads_are_kept_even_when_the_router_fails():
    last_week = _routed(ROADS_DOC, _roads_found)
    assert _routed(ROADS_DOC, _failing, previous=last_week).shapes == last_week.shapes


# The timetable is saved before its roads are asked for.


def _road_build(stops, operators, api_key, now, **options):
    return _unrouted(ROADS_DOC)


def test_refresh_saves_the_timetable_before_asking_for_its_roads(tmp_path):
    saved_when_asked = []

    def route(pairs, progress):
        saved_when_asked.append(load(tmp_path))
        return _roads_found(pairs)

    schedule = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=route)
    assert saved_when_asked[0] is not None and saved_when_asked[0].shapes == {}
    assert len(schedule.shapes) == 6 and load(tmp_path) == schedule


def test_roads_that_would_make_the_timetable_too_big_to_load_are_left_out_for_good(tmp_path, monkeypatch, caplog):
    unrouted = replace(_road_build(None, None, None, NOW), roads_pending=False)
    monkeypatch.setattr(timetable, "MAX_FILE_BYTES", len(unrouted.to_json()) + 1)
    asked = []

    def route(pairs, progress):
        asked.append(pairs)
        return {pair: [(53.44351, -1.95562)] * 50 for pair in pairs}

    schedule = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=route)
    assert schedule.shapes == {} and not schedule.roads_pending
    assert load(tmp_path) == schedule
    assert "larger than" in caplog.text
    refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW + timedelta(hours=1), "k", build=_road_build, route=route)
    assert len(asked) == 1  # not asked again every hour


def test_a_timetable_is_kept_when_finding_its_roads_is_stopped(tmp_path):
    # As when the timetable service runs out of time while asking for roads.
    def route(pairs, progress):
        raise SystemExit("stopped")

    with pytest.raises(SystemExit):
        refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=route)
    kept = load(tmp_path)
    assert kept is not None and kept.journeys and kept.shapes == {}
    assert due(kept, {"A": STOP_A}, NOW, ["BNML"]) is None  # not built again until next week


def test_a_timetable_is_kept_without_roads_when_saving_them_fails(tmp_path, monkeypatch, caplog):
    from departureboard.timetable import build as timetable

    real_save = timetable.save
    saves = []

    def save(folder, schedule):
        saves.append(schedule)
        if len(saves) == 2:
            raise OSError("disk full")
        real_save(folder, schedule)

    monkeypatch.setattr(timetable, "save", save)
    schedule = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=_roads_found)
    assert len(saves) == 2 and schedule.shapes == {} and load(tmp_path) == schedule
    assert "disk full" in caplog.text


def test_refresh_reuses_the_saved_timetable_s_roads(tmp_path):
    first = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=_roads_found)
    asked = []

    def route(pairs, progress):
        asked.extend(pairs)
        return {}

    again = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", force=True, build=_road_build, route=route)
    assert asked == [] and len(again.shapes) == 6 and again.shapes == first.shapes


# A timetable saved before its roads are found says so, so that whoever
# builds next finds them: the wizard's search may be stopped, or the
# dashboard start again with new settings, while they are being found.


def test_a_timetable_is_saved_with_its_roads_to_find_until_they_are_found(tmp_path):
    seen = []

    def route(pairs, progress):
        seen.append(load(tmp_path).roads_pending)
        return _roads_found(pairs)

    schedule = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=route)
    assert seen == [True]
    assert schedule.roads_pending is False and load(tmp_path) == schedule


def test_refresh_says_when_the_timetable_is_saved_before_finding_its_roads(tmp_path):
    told = []

    def route(pairs, progress):
        told.append("roads")
        return _roads_found(pairs)

    refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=route,
            saved=lambda schedule: told.append(("saved", load(tmp_path) == schedule, len(schedule.shapes))))
    assert told == [("saved", True, 0), "roads"]


def test_a_failed_build_says_nothing_was_saved(tmp_path):
    def failing(*args, **options):
        raise TimetableError("BODS is down")

    told = []
    assert refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=failing, saved=told.append) is None
    assert told == []


def test_roads_stopped_part_way_are_kept_and_the_rest_found_next_time(tmp_path):
    first_two = sorted(ROADS_PAIRS)[:2]

    def stopped(pairs, progress):
        return _roads_found([pair for pair in pairs if pair in first_two])

    kept = refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=stopped,
                   cancelled=lambda: True)
    assert kept.roads_pending is True and len(kept.shapes) == 2 and load(tmp_path) == kept
    asked = []

    def route(pairs, progress):
        asked.extend(pairs)
        return _roads_found(pairs)

    def no_build(*args, **options):
        raise AssertionError("not due, so not built again")

    later = NOW + timedelta(hours=1)
    found = refresh(tmp_path, {"A": STOP_A}, ["BNML"], later, "k", build=no_build, route=route)
    assert set(asked) == ROADS_PAIRS - set(first_two)  # only those it hadn't found
    assert found.roads_pending is False and len(found.shapes) == 6 and load(tmp_path) == found
    assert found.built == kept.built  # the same timetable, with its roads
    asked.clear()
    assert refresh(tmp_path, {"A": STOP_A}, ["BNML"], later, "k", build=no_build, route=route) == found
    assert asked == []  # nothing left to find


def test_a_timetable_saved_before_its_roads_gets_them_from_the_next_build(tmp_path):
    # As when the process stops while finding them: the hourly upkeep or timer finds them.
    save(tmp_path, replace(_unrouted(ROADS_DOC), roads_pending=True))
    found = build(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", route=_roads_found)
    assert found.roads_pending is False and len(found.shapes) == 6 and load(tmp_path) == found


def test_how_far_the_roads_have_got_is_kept_while_they_are_found(tmp_path, monkeypatch):
    seen = []

    def route(pairs, progress):
        for done in range(4):
            progress(done, 3)
            seen.append(timetable.roads_progress(tmp_path))
        return _roads_found(pairs)

    monkeypatch.setattr(timetable, "ROADS_EVERY_S", 0)
    refresh(tmp_path, {"A": STOP_A}, ["BNML"], NOW, "k", build=_road_build, route=route)
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert timetable.roads_progress(tmp_path) is None  # gone once they're found
    assert not (tmp_path / timetable.ROADS_FILE).exists()


def test_how_far_the_roads_have_got_is_written_now_and_then_and_the_last(tmp_path):
    clock = [1000.0]
    progress = timetable.RoadsProgress(tmp_path, clock=lambda: clock[0])
    progress(0, 10)
    progress(1, 10)  # too soon after the last: an SD card is spared
    assert timetable.roads_progress(tmp_path, now=lambda: clock[0]) == (0, 10)
    clock[0] += timetable.ROADS_EVERY_S
    progress(2, 10)
    progress(10, 10)  # the last, at once
    assert timetable.roads_progress(tmp_path, now=lambda: clock[0]) == (10, 10)
    assert stat.S_IMODE((tmp_path / timetable.ROADS_FILE).stat().st_mode) == 0o640
    progress.clear()
    assert not (tmp_path / timetable.ROADS_FILE).exists()


def test_roads_progress_left_by_a_process_that_stopped_is_ignored(tmp_path):
    timetable.RoadsProgress(tmp_path, clock=lambda: 1000.0)(5, 10)
    assert timetable.roads_progress(tmp_path, now=lambda: 1000.0 + timetable.ROADS_STALE_S - 1) == (5, 10)
    assert timetable.roads_progress(tmp_path, now=lambda: 1000.0 + timetable.ROADS_STALE_S + 1) is None


@pytest.mark.parametrize("text", ["", "[]", '{"done": 1, "total": 2}', '{"done": "1", "total": 2, "at": 1000}',
                                  '{"done": 3, "total": 2, "at": 1000}', '{"done": -1, "total": 2, "at": 1000}',
                                  '{"done": true, "total": 2, "at": 1000}', "x" * 1000])
def test_roads_progress_that_doesnt_fit_is_ignored(tmp_path, text):
    (tmp_path / timetable.ROADS_FILE).write_text(text)
    assert timetable.roads_progress(tmp_path, now=lambda: 1000.0) is None


def test_roads_progress_through_a_link_is_ignored(tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"done": 1, "total": 2, "at": 1000}')
    (tmp_path / timetable.ROADS_FILE).symlink_to(elsewhere)
    assert timetable.roads_progress(tmp_path, now=lambda: 1000.0) is None


def test_a_timetable_from_before_road_shapes_is_due_once():
    from dataclasses import replace

    assert FORMAT >= 3
    assert due(replace(_schedule(built=NOW), format=2), STOPS, NOW, ["BNML"]) == "it was built by an older version"


# One build at a time.


def _hold_lock(folder):
    """Hold the build lock as another build would; closing the file lets go."""
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_build_refreshes_holding_the_lock_and_lets_go(tmp_path):
    fake = FakeBuild()
    schedule = build(tmp_path, STOPS, ["BNML"], NOW, "k", lines=["341"], build=fake)
    assert schedule == load(tmp_path)
    assert fake.calls == [(STOPS, ["BNML"], "k", NOW,
                           {"lines": ["341"], "scratch": tmp_path, "limits": DocumentLimits()})]
    build(tmp_path, STOPS, ["BNML"], NOW, "k", force=True, build=fake)  # let go: free for the next
    assert len(fake.calls) == 2


def test_two_builders_racing_one_builds_and_the_other_skips(tmp_path):
    inside, release = threading.Event(), threading.Event()

    def slow(stops, operators, api_key, now, **options):
        inside.set()
        assert release.wait(5)
        return _schedule(built=now, stops=stops)

    results = []
    first = threading.Thread(target=lambda: results.append(build(tmp_path, STOPS, ["BNML"], NOW, "k", build=slow)))
    first.start()
    assert inside.wait(5)
    second = FakeBuild()
    with pytest.raises(TimetableBusy, match="Another bus timetable build is running"):
        build(tmp_path, STOPS, ["BNML"], NOW, "k", force=True, build=second)
    release.set()
    first.join(5)
    assert second.calls == [] and results == [load(tmp_path)]


def test_a_build_in_another_process_makes_this_one_skip(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, os, sys\n"
         f"fd = os.open({str(tmp_path / LOCK_NAME)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    fake = FakeBuild()
    try:
        assert holder.stdout.readline() == "held\n"
        with pytest.raises(TimetableBusy):
            build(tmp_path, STOPS, ["BNML"], NOW, "k", build=fake)
        assert fake.calls == []
    finally:
        holder.communicate("", timeout=5)  # it exits, and the system lets go of its lock
    build(tmp_path, STOPS, ["BNML"], NOW, "k", build=fake)
    assert len(fake.calls) == 1


def test_a_waiting_build_runs_once_the_other_is_done(tmp_path, monkeypatch):
    monkeypatch.setattr(timetable, "LOCK_WAIT_S", 0.01)
    fd = _hold_lock(tmp_path)
    waited = threading.Event()
    fake = FakeBuild()
    results = []
    waiter = threading.Thread(target=lambda: results.append(build(
        tmp_path, STOPS, ["BNML"], NOW, "k", force=True, wait=True, waiting=waited.set, build=fake)))
    waiter.start()
    assert waited.wait(5)
    assert fake.calls == [] and waiter.is_alive()
    os.close(fd)
    waiter.join(5)
    assert len(fake.calls) == 1 and results == [load(tmp_path)]


def test_a_waiting_build_stops_waiting_when_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(timetable, "LOCK_WAIT_S", 0.01)
    fd = _hold_lock(tmp_path)
    looks = []

    def cancelled():
        looks.append(1)
        return len(looks) >= 3

    fake = FakeBuild()
    with pytest.raises(TimetableBusy):
        build(tmp_path, STOPS, ["BNML"], NOW, "k", wait=True, cancelled=cancelled, build=fake)
    os.close(fd)
    assert fake.calls == [] and len(looks) == 3


def test_the_lock_file_is_private_and_a_link_in_its_place_is_refused(tmp_path):
    build(tmp_path / "store", STOPS, ["BNML"], NOW, "k", build=FakeBuild())
    assert stat.S_IMODE((tmp_path / "store" / LOCK_NAME).stat().st_mode) == 0o600
    elsewhere = tmp_path / "elsewhere"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / LOCK_NAME).symlink_to(elsewhere)
    fake = FakeBuild()
    with pytest.raises(TimetableError, match="Can't open"):
        build(planted, STOPS, ["BNML"], NOW, "k", build=fake)
    assert fake.calls == [] and not elsewhere.exists()


def test_a_store_that_is_not_a_folder_is_a_timetable_error(tmp_path):
    (tmp_path / "store").write_text("")
    fake = FakeBuild()
    with pytest.raises(TimetableError, match="Can't open"):
        build(tmp_path / "store", STOPS, ["BNML"], NOW, "k", build=fake)
    assert fake.calls == []


# Reading a document in two passes, one element at a time.

def _whole_tree(xml, atcos, noc="", yours=None):
    """The document read as the timetable used to read it, parsed whole: the
    oracle the two passes must agree with."""
    ns = timetable._NS
    root = safexml.fromstring(xml)
    revision = root.get("RevisionNumber", "").strip()
    doc = timetable._Document(revision=int(revision) if revision.isdigit() and len(revision) < 10 else 0)
    for node in root.iterfind("t:StopPoints/t:AnnotatedStopPointRef", ns):
        timetable._read_stop_ref(node, doc)
    for node in root.iterfind("t:StopPoints/t:StopPoint", ns):
        atco, name = timetable._stop_point_name(node)
        doc.names[atco] = name
    for node in root.iterfind("t:Operators/*", ns):
        timetable._read_operator(node, doc)
    for node in root.iterfind("t:ServicedOrganisations/t:ServicedOrganisation", ns):
        timetable._read_school(node, doc)
    for node in root.iterfind("t:JourneyPatternSections/t:JourneyPatternSection", ns):
        timetable._read_section(node, doc)
    for node in root.iterfind("t:Services/t:Service", ns):
        timetable._read_service(node, doc)
    vehicle_journeys = root.findall("t:VehicleJourneys/t:VehicleJourney", ns)
    pattern_refs = {timetable._text(node, "t:VehicleJourneyCode"): timetable._text(node, "t:JourneyPatternRef")
                    for node in vehicle_journeys}
    stands = timetable._stands(doc, atcos, yours) if yours is not None else set()
    return timetable._journeys_kept(vehicle_journeys, doc, atcos, noc, pattern_refs, stands), doc.positions


def _every_stop(xml):
    return {ref.decode() for ref in re.findall(rb"<StopPointRef>([^<]+)</StopPointRef>", xml)}


_TWO_OPERATORS = ('<Operator id="o1"><NationalOperatorCode>AAAA</NationalOperatorCode></Operator>'
                  '<LicensedOperator id="o2"><OperatorCode>BBBB</OperatorCode></LicensedOperator>')
_TWO_NAMES = (f"<AnnotatedStopPointRef><StopPointRef>{STOP_A}</StopPointRef><CommonName>Old</CommonName>"
              f"</AnnotatedStopPointRef><StopPoint><AtcoCode>{STOP_A}</AtcoCode><Descriptor><CommonName>New"
              "</CommonName></Descriptor></StopPoint>")
# Every sample document: the real ones, and the made-up ones for what they don't show.
_SAMPLES = {
    **{path.name: path.read_bytes() for path in sorted(FIXTURES.glob("txc_*.xml"))},
    "waits": _txc([("X1", STOP_A, "PT2M", "PT1M", "PT30S"), (STOP_A, "X2", "PT1M30S", "PT2M", "")]),
    "school days": _txc(LINKS, profile=_school("DaysOfOperation", "WorkingDays", "TERM"), extra=SCHOOLS),
    "own profile": _txc(LINKS, profile=_profile("<RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek>"
                                                "</RegularDayType>"),
                        journey=_profile("<RegularDayType><DaysOfWeek><Saturday/></DaysOfWeek></RegularDayType>")),
    "two operators": _txc(LINKS, operators=_TWO_OPERATORS, journey="<OperatorRef>o2</OperatorRef>"),
    "revision": _txc(LINKS, revision=3, end="2026-12-31"),
    "a stop named twice": _txc(LINKS, stops=_TWO_NAMES),
    "a stand": _stand_doc(),
}


@pytest.mark.parametrize("name", list(_SAMPLES))
def test_two_passes_read_a_document_as_parsing_it_whole_did(name):
    xml = _SAMPLES[name]
    for atcos in (OURS, {STOP_A}, _every_stop(xml), set()):
        for yours in (None, {}, {STOP_A: POSITIONS[STOP_A]}):
            read = timetable._read_document(xml, atcos, "NOC", yours)
            expected = _whole_tree(xml, atcos, "NOC", yours)
            assert read == expected
            assert list(read[1].items()) == list(expected[1].items())  # in the same order too
    # Each sample has journeys to compare, but the route setup's, whose journeys have no times.
    assert timetable._read_document(xml, _every_stop(xml), "NOC", {})[0] or name == "txc_glossop_synthetic.xml"


def test_journeys_before_the_services_they_run_are_read_all_the_same():
    # The second pass reads the journeys once everything they refer to is known.
    xml = _txc(LINKS)
    head, rest = xml.split(b" <JourneyPatternSections>", 1)
    middle, rest = rest.split(b" <VehicleJourneys>", 1)
    journeys, tail = rest.split(b"</VehicleJourneys>", 1)
    reordered = head + b" <VehicleJourneys>" + journeys + b"</VehicleJourneys> <JourneyPatternSections>" + middle + tail
    assert parse_journeys(xml, OURS)
    assert parse_journeys(reordered, OURS) == parse_journeys(xml, OURS)


def test_a_journey_may_take_its_pattern_from_a_journey_after_it():
    xml = _txc(LINKS).replace(
        b"<VehicleJourneys><VehicleJourney>",
        b"<VehicleJourneys><VehicleJourney><VehicleJourneyCode>early</VehicleJourneyCode><ServiceRef>S1</ServiceRef>"
        b"<LineRef>L1</LineRef><VehicleJourneyRef>vj1</VehicleJourneyRef><DepartureTime>08:00:00</DepartureTime>"
        b"</VehicleJourney><VehicleJourney>",
    )
    journeys = _by_code(parse_journeys(xml, OURS))
    assert set(journeys) == {"early", "vj1"}
    assert _at(journeys["early"], STOP_A) == _minute("08:05")


def _large_237(size: int) -> bytes:
    """The 237's document grown to about `size` bytes with copies of its journeys."""
    head, rest = TXC_237.split(b"<VehicleJourneys>", 1)
    journeys, tail = rest.split(b"</VehicleJourneys>", 1)
    copies = size // len(journeys)
    return b"".join([head, b"<VehicleJourneys>",
                     *(journeys.replace(b"<VehicleJourneyCode>", f"<VehicleJourneyCode>c{n}-".encode())
                       for n in range(copies)),
                     b"</VehicleJourneys>", tail])


def test_a_large_document_is_never_held_as_a_whole_tree():
    # Parsed whole, a document took about 8 times its size; read in two
    # passes, one element is held at a time beside what is kept from it.
    xml = _large_237(4_000_000)
    tracemalloc.start()
    try:
        found, _ = timetable._read_document(xml, {"1000NOWHERE1"}, "NOC")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found == []
    assert peak < len(xml) / 2  # about a fifth; whole, it was nearly 8 times


def _peak(read):
    """What `read()` gives, and the most memory it took while it ran."""
    tracemalloc.start()
    try:
        found = read()
        return found, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_document_takes_at_most_itself_again_and_its_journeys_found_to_parse():
    # What each install's caps and journey limit are sized by (see
    # tests/test_deploy.py): a document parsed is held (counted there), with
    # at most as much again, and each journey found, as it is found. Only a
    # document mentioning your stops is parsed, so it is measured with stops
    # that match: one, and every stop it has.
    xml = _large_237(4_000_000)
    for atcos in ({STOP_A}, _every_stop(TXC_237)):
        (found, _), peak = _peak(lambda atcos=atcos: timetable._read_document(xml, atcos, "NOC", {}))
        assert len(found) > 1000
        assert peak <= len(xml) + len(found) * timetable.JOURNEY_BYTES


PROFILE = ("<OperatingProfile><RegularDayType><DaysOfWeek><MondayToFriday/></DaysOfWeek></RegularDayType>"
           "<BankHolidayOperation><DaysOfNonOperation><ChristmasDay/><BoxingDay/><GoodFriday/>"
           "</DaysOfNonOperation></BankHolidayOperation></OperatingProfile>")


def _long_route(calls, journeys):
    """A document of `journeys` journeys of `calls` stops each, all on one
    pattern, each with an operating profile and a ticket machine code of its
    own, as BODS's are."""
    stops = [f"370{n:09d}" for n in range(calls)]
    links = [(a, b, "PT1M30S", "", "") for a, b in pairwise(stops)]
    one = (f"<VehicleJourney>{PROFILE}<VehicleJourneyCode>vj{{n}}</VehicleJourneyCode>"
           "<Operational><TicketMachine><JourneyCode>{code}</JourneyCode></TicketMachine></Operational>"
           "<ServiceRef>S1</ServiceRef><LineRef>L1</LineRef><JourneyPatternRef>p1</JourneyPatternRef>"
           "<DepartureTime>{hour:02d}:{minute:02d}:00</DepartureTime></VehicleJourney>")
    many = "".join(one.format(n=n, code=1000 + n, hour=5 + n // 60 % 18, minute=n % 60) for n in range(journeys))
    xml = _txc(links).decode()
    start, end = xml.index("<VehicleJourneys>"), xml.index("</VehicleJourneys>")
    return (xml[:start] + "<VehicleJourneys>" + many + xml[end:]).encode(), stops[calls // 2]


def test_each_journey_kept_takes_no_more_than_the_installs_are_sized_for():
    # JOURNEY_BYTES was measured on BODS data set 2100 (First South
    # Yorkshire): its largest documents' journeys, of 80 calls on average,
    # took 14.6 KB each on Python 3.12 and 15.6 KB on 3.11, held as the build
    # holds them (tracemalloc); 24 calls, 6.6 to 7.0 KB. Journeys made up as
    # long take less, so this guards against the build holding more of each.
    xml, stop = _long_route(80, 500)

    def kept():
        found, _ = timetable._read_document(xml, {stop}, "NOC", {})
        return found, {(v, j.line, j.operator, j.calls, j.calendar): (v, j) for v, j in found}

    tracemalloc.start()
    try:
        found, held = kept()
        taken = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    assert len(held) == len(found) == 500
    assert all(len(journey.calls) == 80 for _, journey in found)
    assert taken / len(found) <= timetable.JOURNEY_BYTES

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from departureboard.holidays import Event, holiday_names
from departureboard.schedule_model import Calendar, Call, Schedule
from departureboard.timetable import TimetableError, journeys_on, parse_journeys, runs_on

FIXTURES = Path(__file__).parent / "fixtures"
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

def _txc(links, *, departure="10:00:00", profile="", journey="", pattern="", operators=None, stops="", extra=""):
    """`links` is a list of (from, to, run time, from wait, to wait) on one pattern."""
    timing = "".join(
        f'<JourneyPatternTimingLink id="l{i}"><From>{f"<WaitTime>{fw}</WaitTime>" if fw else ""}'
        f"<StopPointRef>{a}</StopPointRef></From><To>{f'<WaitTime>{tw}</WaitTime>' if tw else ''}"
        f"<StopPointRef>{b}</StopPointRef></To><RunTime>{run}</RunTime></JourneyPatternTimingLink>"
        for i, (a, b, run, fw, tw) in enumerate(links)
    )
    if operators is None:
        operators = '<Operator id="o1"><NationalOperatorCode>TEST</NationalOperatorCode></Operator>'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<TransXChange xmlns="http://www.transxchange.org.uk/">
 <StopPoints>{stops}</StopPoints>
 {extra}
 <JourneyPatternSections><JourneyPatternSection id="s1">{timing}</JourneyPatternSection></JourneyPatternSections>
 <Operators>{operators}</Operators>
 <Services><Service><ServiceCode>S1</ServiceCode>
  <Lines><Line id="L1"><LineName>99</LineName></Line></Lines>
  <OperatingPeriod><StartDate>2026-01-01</StartDate></OperatingPeriod>
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

from datetime import date, datetime

from departureboard.schedule_model import Calendar, Call, Journey, Schedule, StopPosition


def test_schedule_round_trips_through_json():
    calendar = Calendar(
        days=frozenset({0, 1, 2, 3, 4}),
        start=date(2026, 9, 27),
        bank_holidays_off=frozenset({"ChristmasDay", "MayDay"}),
        school_days_only=((date(2026, 9, 1), date(2026, 9, 4)),),
    )
    journey = Journey("341", "BNML", "Hyde", "10620", (Call("1000DGHS0900", 380), Call("1800ANIN0K1", 1460)), calendar)
    schedule = Schedule(
        built=datetime(2026, 10, 9, 3, 0),
        valid_until=None,
        stops={"A": "1000DGHS0900"},
        journeys=(journey,),
        positions={"1000DGHS0900": StopPosition("Henry Street", 53.44384, -1.948999)},
    )
    back = Schedule.from_json(schedule.to_json())
    assert back == schedule
    assert back.journeys[0].origin == Call("1000DGHS0900", 380)

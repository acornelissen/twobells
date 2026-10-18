from twobells.board.route import RouteView, target
from twobells.core.model import Departure, Status

# The stops of the strip in tests/timetable/test_route.py, the last one yours.
NAMES = ("The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street")


def bus(line, scheduled, stop="A"):
    return Departure("bus", line, "Hyde", scheduled, None, Status.ON_TIME, stop=stop)


def test_target_counts_earlier_buses_of_the_same_line_at_the_same_stop():
    buses = [bus("341", "07:20"), bus("237", "07:22"), bus("341", "07:25", stop="B"), bus("341", "07:35")]
    assert target(buses, buses[0]) == 0
    assert target(buses, buses[3]) == 1


def test_route_view_signature_changes_as_the_bus_moves():
    a = RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", NAMES, 2.4, "3 stops away")
    b = RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", NAMES, 2.6, "3 stops away")
    assert a.signature() != b.signature()

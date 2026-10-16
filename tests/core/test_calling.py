"""The station board's "Calling at" line."""

from twobells.core.calling import calling_at, formed_of, line, not_calling_at, stop_list

GLOSSOP_LINE = ["Dinting", "Broadbottom", "Hattersley", "Hyde Central", "Manchester Piccadilly"]


def test_many_stops_read_as_a_list_ending_with_and():
    assert line(GLOSSOP_LINE, coaches=3) == (
        "Calling at: Dinting, Broadbottom, Hattersley, Hyde Central and Manchester Piccadilly. "
        "This train is formed of 3 coaches."
    )


def test_one_stop_is_that_stop_only():
    assert line(["Manchester Piccadilly"], coaches=3) == (
        "Calling at: Manchester Piccadilly only. This train is formed of 3 coaches."
    )


def test_two_stops_are_joined_by_and():
    assert calling_at(["Dinting", "Hadfield"]) == "Calling at: Dinting and Hadfield."


def test_cancelled_stops_are_left_out_and_said_after():
    assert line(["Dinting", "Broadbottom"], ["Hattersley", "Godley"], 3) == (
        "Calling at: Dinting and Broadbottom. Does not call at Hattersley or Godley. "
        "This train is formed of 3 coaches."
    )
    assert not_calling_at(["Hattersley"]) == "Does not call at Hattersley."


def test_without_a_length_it_says_nothing_of_coaches():
    assert line(GLOSSOP_LINE[-2:]) == "Calling at: Hyde Central and Manchester Piccadilly."
    assert formed_of(None) is None
    assert formed_of(0) is None


def test_one_coach_is_a_coach():
    assert formed_of(1) == "This train is formed of 1 coach."


def test_a_replacement_bus_says_so_rather_than_coaches():
    assert line(["Dinting", "Manchester Piccadilly"], coaches=3, replacement_bus=True) == (
        "Calling at: Dinting and Manchester Piccadilly. This is a replacement bus."
    )


def test_a_ferry_says_so_rather_than_coaches():
    assert line(["Ryde Pier Head"], coaches=3, ferry=True) == "Calling at: Ryde Pier Head only. This is a ferry."
    assert formed_of(None, ferry=True) == "This is a ferry."


def test_a_train_calling_nowhere_has_no_line():
    assert line([]) is None
    assert line([], ["Dinting", "Manchester Piccadilly"], 3) is None


def test_a_list_of_names_for_an_announcement():
    assert stop_list([]) == ""
    assert stop_list(["Dinting"]) == "Dinting"
    assert stop_list(["Dinting", "Godley", "Gorton"]) == "Dinting, Godley and Gorton"
    assert stop_list(["Dinting", "Godley"], "or") == "Dinting or Godley"

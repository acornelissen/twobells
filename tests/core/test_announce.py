"""Station announcements, in a UK station's classic words."""

from twobells.core.announce import (
    Train,
    announcements,
    cancelled,
    delayed,
    departs,
    next_train,
    platform_alteration,
    reason_sentence,
)

TRAIN = Train(time="08:12", destination="Manchester Piccadilly", operator="Northern", platform="1",
              calling=("Dinting", "Broadbottom", "Manchester Piccadilly"), coaches=3)
CREW = "This train has been cancelled because of a shortage of train crew"


def test_the_next_train_says_its_platform_service_stops_and_length():
    assert next_train(TRAIN) == (
        "The next train to depart from platform 1 will be the 08:12 Northern service to Manchester Piccadilly. "
        "Calling at Dinting, Broadbottom and Manchester Piccadilly. This train is formed of 3 coaches."
    )


def test_the_next_train_without_a_platform_says_none():
    found = next_train(Train(time="08:12", destination="Hadfield", operator="Northern",
                             calling=("Hadfield",), coaches=1))
    assert found == ("The next train to depart will be the 08:12 Northern service to Hadfield. "
                     "Calling at Hadfield only. This train is formed of 1 coach.")


def test_what_is_not_known_is_left_out():
    bare = Train(time="08:12", destination="Hadfield")
    assert next_train(bare) == "The next train to depart will be the 08:12 service to Hadfield."
    assert departs(bare) == "The 08:12 service to Hadfield is due to depart."


def test_route_words_cancelled_stops_and_a_delay_are_said():
    train = Train(time="08:12", destination="Manchester Piccadilly", operator="Northern", platform="2",
                  via="via Guide Bridge", calling=("Dinting", "Manchester Piccadilly"), skipped=("Hattersley",))
    assert next_train(train, late=True) == (
        "The next train to depart from platform 2 will be the delayed 08:12 Northern service to Manchester "
        "Piccadilly via Guide Bridge. Calling at Dinting and Manchester Piccadilly. Does not call at Hattersley."
    )


def test_a_train_whose_every_later_stop_is_cancelled_says_so():
    train = Train(time="08:12", destination="Manchester Piccadilly", operator="Northern", platform="2",
                  skipped=("Dinting", "Manchester Piccadilly"), coaches=3)
    assert next_train(train) == (
        "The next train to depart from platform 2 will be the 08:12 Northern service to Manchester Piccadilly. "
        "Does not call at Dinting or Manchester Piccadilly. This train is formed of 3 coaches.")
    assert departs(train).endswith(" Does not call at Dinting or Manchester Piccadilly. This train is formed of 3 coaches.")


def test_a_later_train_is_not_called_the_next():
    assert departs(TRAIN) == (
        "The 08:12 Northern service to Manchester Piccadilly will depart from platform 1. "
        "Calling at Dinting, Broadbottom and Manchester Piccadilly. This train is formed of 3 coaches."
    )


def test_a_replacement_bus_is_a_service_with_no_platform_or_coaches():
    bus = Train(time="08:12", destination="Glossop", operator="Northern", platform="1", replacement_bus=True,
                calling=("Dinting", "Glossop"), coaches=3)
    assert next_train(bus) == ("The next service to depart will be the 08:12 Northern service to Glossop. "
                               "Calling at Dinting and Glossop. This is a replacement bus.")
    assert departs(bus) == ("The 08:12 Northern service to Glossop is due to depart. "
                            "Calling at Dinting and Glossop. This is a replacement bus.")
    assert cancelled(bus) == ("We are sorry to announce that the 08:12 Northern replacement bus service to Glossop "
                              "has been cancelled.")
    assert platform_alteration(bus) is None


def test_a_ferry_is_the_next_ferry_with_no_platform_or_coaches():
    ferry = Train(time="07:12", destination="Ryde Pier Head", operator="Wightlink", platform="3", ferry=True,
                  calling=("Ryde Pier Head",), coaches=2)
    assert next_train(ferry) == ("The next ferry to depart will be the 07:12 Wightlink service to Ryde Pier Head. "
                                 "Calling at Ryde Pier Head only.")
    assert departs(ferry) == ("The 07:12 Wightlink service to Ryde Pier Head is due to depart. "
                              "Calling at Ryde Pier Head only. This is a ferry.")
    assert delayed(ferry, 5) == ("We are sorry to announce that the 07:12 Wightlink ferry service to Ryde Pier Head "
                                 "is delayed by approximately 5 minutes.")
    assert platform_alteration(ferry) is None
    assert [kind for kind, _ in announcements(ferry, platform_changed=True, is_next=True)] == ["next"]


def test_a_cancellation_with_darwin_s_reason_says_what_it_is_due_to():
    assert cancelled(TRAIN, CREW) == (
        "We are sorry to announce that the 08:12 Northern service to Manchester Piccadilly has been cancelled. "
        "This is due to a shortage of train crew."
    )


def test_a_cancellation_without_a_reason_is_the_apology_alone():
    assert cancelled(TRAIN) == (
        "We are sorry to announce that the 08:12 Northern service to Manchester Piccadilly has been cancelled."
    )
    assert cancelled(TRAIN, "  ") == cancelled(TRAIN)


def test_a_delay_says_about_how_long_and_why():
    assert delayed(TRAIN, 10, "This train has been delayed by a broken down train") == (
        "We are sorry to announce that the 08:12 Northern service to Manchester Piccadilly is delayed by "
        "approximately 10 minutes. This is due to a broken down train."
    )
    assert delayed(TRAIN, 1).endswith("is delayed by approximately 1 minute.")
    assert delayed(TRAIN).endswith("Manchester Piccadilly is delayed.")


def test_darwin_s_reasons_are_said_as_a_station_says_them():
    assert reason_sentence("This train has been delayed by a fault with the signalling system near Guide Bridge.") == (
        "This is due to a fault with the signalling system near Guide Bridge.")
    assert reason_sentence("This train has been cancelled because of Network Rail engineering work") == (
        "This is due to Network Rail engineering work.")
    assert reason_sentence("This train has been partially cancelled because of a points failure") == (
        "This is due to a points failure.")
    # Anything else is said as Darwin gives it, as a sentence.
    assert reason_sentence("Signalling problems near Guide Bridge") == "Signalling problems near Guide Bridge."
    assert reason_sentence("flooding.") == "Flooding."
    assert reason_sentence(None) is None and reason_sentence(" . ") is None


def test_a_platform_change_is_a_platform_alteration():
    moved = Train(time="08:12", destination="Manchester Piccadilly", operator="Northern", platform="2")
    assert platform_alteration(moved) == (
        "Platform alteration: the 08:12 Northern service to Manchester Piccadilly will now depart from platform 2."
    )
    assert platform_alteration(Train(time="08:12", destination="Hadfield")) is None


def test_a_cancelled_train_has_its_apology_alone():
    found = announcements(TRAIN, is_cancelled=True, late=True, platform_changed=True, cancel_reason=CREW,
                          delay_reason="This train has been delayed by a broken down train", is_next=True)
    assert found == [("cancelled", cancelled(TRAIN, CREW))]


def test_a_train_has_its_alteration_and_delay_before_its_departure():
    found = announcements(TRAIN, late=True, minutes_late=10, platform_changed=True, is_next=True)
    assert [kind for kind, _ in found] == ["platform", "delayed", "next"]
    assert found[1][1] == delayed(TRAIN, 10)
    assert found[2][1] == next_train(TRAIN, late=True)


def test_a_train_on_time_after_the_next_has_its_departure_alone():
    assert announcements(TRAIN) == [("departs", departs(TRAIN))]
    # Late with no time known is still a delay; a train not late by a minute isn't.
    assert [kind for kind, _ in announcements(TRAIN, late=True)] == ["delayed", "departs"]
    assert [kind for kind, _ in announcements(TRAIN, late=True, minutes_late=0)] == ["departs"]


def test_a_platform_change_with_no_platform_has_no_alteration():
    bare = Train(time="08:12", destination="Hadfield")
    assert [kind for kind, _ in announcements(bare, platform_changed=True, is_next=True)] == ["next"]

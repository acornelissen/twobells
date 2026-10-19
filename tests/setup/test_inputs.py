"""The wizard's input checks and the parts of its replies (setup/inputs.py)."""

import re

from twobells.setup.inputs import suggested_labels
from twobells.setup.lookups import MAX_STOPS, Stop

STOPS = [
    Stop("1800SB12341", "Station Road", "Stop A", "N", 53.4615, -1.9662, 64),
    Stop("1800SB12342", "Station Road", "Stop B", "S", 53.4614, -1.9665, 85),
    Stop("1800SB12343", "Woolley Bridge", "opp", "", 53.4625, -1.9690, 231),
]


def test_stops_come_with_labels_from_their_signs_when_they_can():
    assert suggested_labels(STOPS[:2]) == ["A", "B"]
    assert suggested_labels(STOPS) == ["A", "B", "C"]  # "opp" has no letter: the first no sign has
    assert suggested_labels([STOPS[1], STOPS[0]]) == ["B", "A"]
    # The stops near Norfolk Square, Glossop: a stop without a letter never takes one from a stop with.
    found = [Stop(f"1000DGHS{i:04d}", "Henry Street", indicator, "", 53.4437, -1.9490, 40 + i)
             for i, indicator in enumerate(["Stop C", "Adj", "Stop B", "Stop A", "adj"])]
    assert suggested_labels(found) == ["C", "D", "B", "A", "E"]


def test_every_stop_found_has_its_own_suggested_label():
    # As many as a search finds, none with a letter: past Z, a letter and a digit.
    found = [Stop(f"1000DGHS{i:04d}", "Henry Street", "opp", "", 53.4437, -1.9490, i)
             for i in range(MAX_STOPS)]
    labels = suggested_labels(found)
    assert len(set(labels)) == len(found) and all(re.fullmatch(r"[A-Za-z0-9]{1,3}", label) for label in labels)
    assert labels[:3] == ["A", "B", "C"] and labels[26] == "A1"

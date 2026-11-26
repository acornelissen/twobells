import json

import pytest

from twobells.board.app import Started
from twobells.board.sinceboot import MAX_BYTES, NAME, SinceBoot

SOURCES = ("train", "bus")
AT = "2026-10-08T07:00:00"
RECORD = {"fetched": {"train": AT}, "tried": ["bus"]}
STATE = {"sources": {"train": {"last_success": AT}, "bus": {}}}


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


def test_nothing_is_done_since_the_start_until_kept(run):
    record = SinceBoot(run, SOURCES)
    assert record.read(STATE) == Started()  # the Pi has just started: /run is empty
    assert record.keep(Started({"train"}, {"bus"}), STATE)
    assert SinceBoot(run, SOURCES).read(STATE) == Started({"train"}, {"bus"})
    assert json.loads((run / NAME).read_text()) == RECORD
    assert (run / NAME).stat().st_mode & 0o777 == 0o600


def test_a_source_counts_as_fetched_only_while_the_state_has_that_fetch(run):
    SinceBoot(run, SOURCES).keep(Started({"train"}, set()), STATE)
    # The run's save failed, and what it fetched wasn't kept either: the next run reads what came before.
    before = {"sources": {"train": {"last_success": "2026-10-08T06:00:00"}}}
    assert SinceBoot(run, SOURCES).read(before) == Started()
    assert SinceBoot(run, SOURCES).read({}) == Started()


def test_a_source_with_no_success_is_not_counted_as_fetched(run):
    SinceBoot(run, SOURCES).keep(Started({"train", "bus"}, set()), STATE)
    assert json.loads((run / NAME).read_text())["fetched"] == {"train": AT}


def test_only_the_board_s_sources_count(run):
    state = {"sources": {"train": {"last_success": AT}, "metro": {"last_success": AT}}}
    SinceBoot(run, ("train", "bus", "metro")).keep(Started({"train", "metro", "elsewhere"}, {"metro"}), state)
    assert json.loads((run / NAME).read_text()) == {"fetched": {"metro": AT, "train": AT}, "tried": ["metro"]}
    # [metro] taken off the board: its name is no longer one of its sources'.
    assert SinceBoot(run, SOURCES).read(state) == Started({"train"}, set())


@pytest.mark.parametrize("record", [
    None, [], "train", {}, {"fetched": {"train": AT}}, {"fetched": ["train"], "tried": []},
    {**RECORD, "extra": 1}, {**RECORD, "fetched": {"train": 1}}, {**RECORD, "fetched": {"train": None}},
    {**RECORD, "fetched": {"train": "07:00"}}, {**RECORD, "fetched": {"train": AT + "+01:00"}},
    {**RECORD, "tried": None}, {**RECORD, "tried": ["bus", "bus"]}, {**RECORD, "fetched": {"Train": AT}},
    {**RECORD, "fetched": {"../state": AT}}, {**RECORD, "tried": ["train\n"]}, {**RECORD, "tried": [""]},
    {**RECORD, "tried": ["a" * 17]}, {**RECORD, "tried": [None]}, {**RECORD, "tried": [["train"]]},
    {**RECORD, "tried": [f"s{chr(97 + n)}" for n in range(9)]},
    {**RECORD, "fetched": {f"s{chr(97 + n)}": AT for n in range(9)}},
])
def test_a_record_of_any_other_shape_counts_as_nothing_done(run, record):
    (run / NAME).write_text(json.dumps(record))
    assert SinceBoot(run, SOURCES).read(STATE) == Started()


@pytest.mark.parametrize("body", [b"{", b"\xff\xfe", json.dumps(RECORD).encode() + b" " * MAX_BYTES])
def test_a_record_that_doesnt_read_back_or_is_too_big_counts_as_nothing_done(run, body):
    (run / NAME).write_bytes(body)
    assert SinceBoot(run, SOURCES).read(STATE) == Started()


def test_a_link_in_its_place_counts_as_nothing_done(run, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(RECORD))
    (run / NAME).symlink_to(elsewhere)
    assert SinceBoot(run, SOURCES).read(STATE) == Started()


def test_a_record_that_cant_be_written_says_so(run):
    (run / NAME).mkdir()
    record = SinceBoot(run, SOURCES)
    assert not record.keep(Started(), STATE) and record.problem

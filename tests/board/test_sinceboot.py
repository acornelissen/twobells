import json

import pytest

from twobells.board.app import Started
from twobells.board.sinceboot import MAX_BYTES, NAME, SinceBoot

SOURCES = ("train", "bus")
RECORD = {"fetched": ["train"], "tried": ["bus"]}


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


def test_nothing_is_done_since_the_start_until_kept(run):
    record = SinceBoot(run, SOURCES)
    assert record.read() == Started()  # the Pi has just started: /run is empty
    record.keep(Started({"train"}, {"bus"}))
    assert SinceBoot(run, SOURCES).read() == Started({"train"}, {"bus"})
    assert json.loads((run / NAME).read_text()) == RECORD
    assert (run / NAME).stat().st_mode & 0o777 == 0o600


def test_only_the_board_s_sources_count(run):
    SinceBoot(run, ("train", "bus", "metro")).keep(Started({"train", "metro", "elsewhere"}, {"metro"}))
    assert json.loads((run / NAME).read_text()) == {"fetched": ["metro", "train"], "tried": ["metro"]}
    # [metro] taken off the board: its name is no longer one of its sources'.
    assert SinceBoot(run, SOURCES).read() == Started({"train"}, set())


@pytest.mark.parametrize("record", [
    None, [], "train", {}, {"fetched": ["train"]}, {"fetched": "train", "tried": []},
    {**RECORD, "extra": 1}, {**RECORD, "fetched": [1]}, {**RECORD, "tried": None},
    {**RECORD, "fetched": ["train", "train"]}, {**RECORD, "fetched": ["Train"]}, {**RECORD, "fetched": ["../state"]},
    {**RECORD, "fetched": ["train\n"]}, {**RECORD, "tried": [""]}, {**RECORD, "fetched": ["a" * 17]},
    {**RECORD, "fetched": [None]}, {**RECORD, "fetched": [["train"]]},
    {**RECORD, "tried": [f"s{chr(97 + n)}" for n in range(9)]},
])
def test_a_record_of_any_other_shape_counts_as_nothing_done(run, record):
    (run / NAME).write_text(json.dumps(record))
    assert SinceBoot(run, SOURCES).read() == Started()


@pytest.mark.parametrize("body", [b"{", b"\xff\xfe", json.dumps(RECORD).encode() + b" " * MAX_BYTES])
def test_a_record_that_doesnt_read_back_or_is_too_big_counts_as_nothing_done(run, body):
    (run / NAME).write_bytes(body)
    assert SinceBoot(run, SOURCES).read() == Started()


def test_a_link_in_its_place_counts_as_nothing_done(run, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(RECORD))
    (run / NAME).symlink_to(elsewhere)
    assert SinceBoot(run, SOURCES).read() == Started()

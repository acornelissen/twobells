import json

import pytest

from twobells.board.sinceboot import MAX_BYTES, NAME, SinceBoot

SOURCES = ("train", "bus")


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


def test_nothing_is_fetched_since_the_start_until_kept(run):
    record = SinceBoot(run, SOURCES)
    assert record.fetched() == set()  # the Pi has just started: /run is empty
    record.keep({"train"})
    assert SinceBoot(run, SOURCES).fetched() == {"train"}
    assert json.loads((run / NAME).read_text()) == {"fetched": ["train"]}
    assert (run / NAME).stat().st_mode & 0o777 == 0o600


def test_only_the_board_s_sources_count(run):
    SinceBoot(run, ("train", "bus", "metro")).keep({"train", "metro", "elsewhere"})
    assert json.loads((run / NAME).read_text()) == {"fetched": ["metro", "train"]}
    # [metro] taken off the board: its name is no longer one of its sources'.
    assert SinceBoot(run, SOURCES).fetched() == {"train"}


@pytest.mark.parametrize("record", [
    None, [], "train", {}, {"fetched": "train"}, {"fetched": ["train"], "extra": 1}, {"fetched": [1]},
    {"fetched": ["train", "train"]}, {"fetched": ["Train"]}, {"fetched": ["../state"]}, {"fetched": ["train\n"]},
    {"fetched": [""]}, {"fetched": ["a" * 17]}, {"fetched": [None]}, {"fetched": [["train"]]},
    {"fetched": [f"s{chr(97 + n)}" for n in range(9)]},
])
def test_a_record_of_any_other_shape_counts_as_nothing_fetched(run, record):
    (run / NAME).write_text(json.dumps(record))
    assert SinceBoot(run, SOURCES).fetched() == set()


@pytest.mark.parametrize("body", [b"{", b"\xff\xfe", b'{"fetched": ["train"]}' + b" " * MAX_BYTES])
def test_a_record_that_doesnt_read_back_or_is_too_big_counts_as_nothing_fetched(run, body):
    (run / NAME).write_bytes(body)
    assert SinceBoot(run, SOURCES).fetched() == set()


def test_a_link_in_its_place_counts_as_nothing_fetched(run, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"fetched": ["train"]}))
    (run / NAME).symlink_to(elsewhere)
    assert SinceBoot(run, SOURCES).fetched() == set()

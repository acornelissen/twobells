import json
import os

import pytest

from twobells.board.state import departure_to_dict
from twobells.board.unsaved import MAX_BYTES, NAME, SOURCES_NAME, KeptRender, KeptSources
from twobells.core.model import Departure, Status

DRAWN = {"signature": "a" * 64, "at": "2026-10-08T07:00:00", "page": "departures"}


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


def test_a_redraw_kept_reads_back_until_forgotten(run):
    kept = KeptRender(run)
    assert kept.read() is None
    kept.keep(DRAWN)
    assert kept.read() == DRAWN
    assert (run / NAME).stat().st_mode & 0o777 == 0o600
    kept.keep({**DRAWN, "page": "code"})
    assert kept.read()["page"] == "code"
    kept.forget()
    assert kept.read() is None and not (run / NAME).exists()
    kept.forget()  # none to forget


@pytest.mark.parametrize("drawn", [
    None, [], "x", {**DRAWN, "extra": 1}, {k: v for k, v in DRAWN.items() if k != "page"},
    {**DRAWN, "signature": "A" * 64}, {**DRAWN, "signature": "a" * 63}, {**DRAWN, "signature": "a" * 64 + "\n"},
    {**DRAWN, "signature": 1}, {**DRAWN, "page": "other"}, {**DRAWN, "at": "2026-10-08T07:00:00.123456"},
    {**DRAWN, "at": "2026-10-08T07:00:00+01:00"}, {**DRAWN, "at": "2026-13-45T07:00:00"}, {**DRAWN, "at": None},
])
def test_only_a_redraw_of_the_shape_written_counts(run, drawn):
    (run / NAME).write_text(json.dumps(drawn))
    kept = KeptRender(run)
    assert kept.read() is None
    kept.keep(drawn)  # nor is anything else written
    assert not (run / NAME).exists()


@pytest.mark.parametrize("body", [b"{", b"\xff\xfe", b" " * (MAX_BYTES + 1), json.dumps(DRAWN).encode() + b" " * MAX_BYTES])
def test_a_file_that_doesnt_read_back_or_is_too_big_counts_as_none(run, body):
    (run / NAME).write_bytes(body)
    assert KeptRender(run).read() is None


def test_a_link_in_its_place_is_never_followed(run, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(DRAWN))
    (run / NAME).symlink_to(elsewhere)
    kept = KeptRender(run)
    assert kept.read() is None
    kept.keep(DRAWN)  # replaces the link, never writes through it
    assert not (run / NAME).is_symlink() and kept.read() == DRAWN
    assert json.loads(elsewhere.read_text()) == DRAWN


def test_a_hard_link_a_folder_or_a_pipe_in_its_place_counts_as_none(run, tmp_path):
    other = run / "other.json"
    other.write_text(json.dumps(DRAWN))
    os.link(other, run / NAME)
    assert KeptRender(run).read() is None
    os.unlink(run / NAME)
    (run / NAME).mkdir()
    assert KeptRender(run).read() is None
    (run / NAME).rmdir()
    os.mkfifo(run / NAME)
    assert KeptRender(run).read() is None  # without waiting for a writer


MARK = {"last_success": "2026-10-08T09:18:00"}
ENTRY = {
    "departures": [departure_to_dict(Departure("train", "Northern", "Manchester Piccadilly", "09:45", None,
                                               Status.ON_TIME))],
    "last_attempt": "2026-10-08T09:29:00", "last_success": "2026-10-08T09:18:00",
    "last_error": {"at": "2026-10-08T09:29:00", "message": "network down"},
    "messages": ["Lifts out of use."], "note": "Timetabled times only", "not_updating": MARK,
}


def test_what_each_source_fetched_goes_back_in_place_of_the_card_s_for_the_board_s_sources_only(run):
    state = {"sources": {"train": {**ENTRY, "other": 1}, "bus": {}, "metro": {"last_attempt": "2026-10-08T09:29:00"}}}
    assert KeptSources(run, ("train", "metro")).keep(state)
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY,
                                                            "metro": {"last_attempt": "2026-10-08T09:29:00"}}
    assert (run / SOURCES_NAME).stat().st_mode & 0o777 == 0o600
    card = {"sources": {"train": {"last_attempt": "2026-10-08T09:18:00", "last_error": {"at": "x"}, "other": 2},
                        "bus": {"departures": []}}}
    KeptSources(run, ("train", "bus")).carry(card)  # [metro] since taken off the board
    assert card == {"sources": {"train": {**ENTRY, "other": 2}, "bus": {"departures": []}}}


def test_a_card_tried_since_keeps_its_own(run):
    # By a release that knows nothing of the file, say.
    KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}})
    newer = {"last_attempt": "2026-10-08T09:35:00", "last_success": "2026-10-08T09:35:00"}
    card = {"sources": {"train": dict(newer)}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": newer}}


def test_nothing_to_keep_keeps_no_file(run):
    KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}})
    assert KeptSources(run, ("train",)).keep({"sources": {"train": {}}})
    assert not (run / SOURCES_NAME).exists()


@pytest.mark.parametrize("change", [
    {"not_updating": None}, {"not_updating": {"last_success": "soon"}}, {"not_updating": {"last_success": None, "x": 1}},
    {"not_updating": {"last_success": "2026-10-08T09:18:00+01:00"}}, {"last_attempt": "09:29"},
    {"last_success": "2026-10-08T09:18:00.1234567"}, {"last_error": {"at": "2026-10-08T09:29:00"}},
    {"last_error": {"at": "2026-10-08T09:29:00", "message": 1}}, {"messages": "Lifts out of use."},
    {"messages": ["x" * 2001]}, {"messages": ["x"] * 17}, {"note": None}, {"departures": [{"service": "x"}]},
    {"departures": [ENTRY["departures"][0]] * 33}, {"departures": [{**ENTRY["departures"][0], "scheduled": "9:45"}]},
    {"other": 1},
])
def test_entries_kept_with_one_part_not_of_the_shape_are_not_put_back(run, change):
    (run / SOURCES_NAME).write_text(json.dumps({"bus": ENTRY, "train": {**ENTRY, **change}}))
    card = {"sources": {"train": {"not_updating": MARK}}}
    KeptSources(run, ("train", "bus")).carry(card)
    assert card == {"sources": {"train": {"not_updating": MARK}}}  # neither: the card's stands


@pytest.mark.parametrize("kept", [[], {"Train": ENTRY}, {"../state": ENTRY}, {"train": None},
                                  {f"s{chr(97 + n)}": ENTRY for n in range(9)}])
def test_a_file_not_of_the_shape_is_not_put_back(run, kept):
    (run / SOURCES_NAME).write_text(json.dumps(kept))
    card = {"sources": {}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {}}


def test_an_entry_not_of_the_shape_is_never_kept(run):
    KeptSources(run, ("train", "bus")).keep({"sources": {"train": {"not_updating": {"last_success": "soon"}},
                                                         "bus": {"last_attempt": "2026-10-08T09:29:00"}}})
    assert json.loads((run / SOURCES_NAME).read_text()) == {"bus": {"last_attempt": "2026-10-08T09:29:00"}}

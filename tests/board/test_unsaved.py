import json
import os

import pytest

from twobells.board.unsaved import MARKS_NAME, MAX_BYTES, NAME, KeptMarks, KeptRender

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


def test_marks_kept_go_back_in_place_of_the_card_s_for_the_board_s_sources_only(run):
    state = {"sources": {"train": {"not_updating": MARK, "departures": []}, "bus": {}, "metro": {"not_updating": MARK}}}
    KeptMarks(run, ("train", "metro")).keep(state)
    assert json.loads((run / MARKS_NAME).read_text()) == {"train": MARK, "metro": MARK}
    assert (run / MARKS_NAME).stat().st_mode & 0o777 == 0o600
    card = {"sources": {"train": {"not_updating": {"last_success": None}}, "bus": {"departures": []}}}
    KeptMarks(run, ("train", "bus")).carry(card)  # [metro] since taken off the board
    assert card == {"sources": {"train": {"not_updating": MARK}, "bus": {"departures": []}}}


def test_no_marks_keeps_no_file(run):
    KeptMarks(run, ("train",)).keep({"sources": {"train": {"not_updating": MARK}}})
    KeptMarks(run, ("train",)).keep({"sources": {"train": {}}})
    assert not (run / MARKS_NAME).exists()
    card = {"sources": {"train": {"not_updating": MARK}}}
    KeptMarks(run, ("train",)).carry(card)
    assert card == {"sources": {"train": {"not_updating": MARK}}}  # the card's own stays


def test_a_mark_that_isnt_one_is_never_kept(run):
    KeptMarks(run, ("train",)).keep({"sources": {"train": {"not_updating": {"last_success": "soon"}}}})
    assert not (run / MARKS_NAME).exists()

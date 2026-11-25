import json
import os

import pytest

from twobells.board.unsaved import MAX_BYTES, NAME, KeptRender

DRAWN = {"signature": "a" * 64, "at": "2026-10-08T07:00:00", "page": "departures"}


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


def test_it_is_kept_in_systemd_s_runtime_directory_only(run, tmp_path):
    assert KeptRender.here({}) is None  # Docker, a desktop: nothing kept
    assert KeptRender.here({"RUNTIME_DIRECTORY": str(run)}) is not None
    for given in ("run", f"{run}:{tmp_path}", str(tmp_path / "missing")):
        assert KeptRender.here({"RUNTIME_DIRECTORY": given}) is None, given


def test_a_folder_others_can_open_or_a_link_to_one_is_not_used(run, tmp_path):
    os.chmod(run, 0o750)
    assert KeptRender.here({"RUNTIME_DIRECTORY": str(run)}) is None
    os.chmod(run, 0o700)
    link = tmp_path / "link"
    link.symlink_to(run)
    assert KeptRender.here({"RUNTIME_DIRECTORY": str(link)}) is None


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

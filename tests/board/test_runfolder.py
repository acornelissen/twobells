import json
import os

import pytest

from twobells.board.runfolder import RunFile, here


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


def test_it_is_systemd_s_runtime_directory_only(run, tmp_path):
    assert here({}) is None  # Docker, a desktop: nothing kept
    assert here({"RUNTIME_DIRECTORY": str(run)}) == run
    for given in ("run", f"{run}:{tmp_path}", str(tmp_path / "missing")):
        assert here({"RUNTIME_DIRECTORY": given}) is None, given


def test_a_folder_named_with_a_colon_is_not_used(tmp_path):
    # systemd joins several RuntimeDirectory= folders with ":"; this one is a real folder of this user's alone.
    folder = tmp_path / "run:twobells"
    folder.mkdir(mode=0o700)
    assert here({"RUNTIME_DIRECTORY": str(folder)}) is None


def test_a_folder_others_can_open_or_a_link_to_one_is_not_used(run, tmp_path):
    os.chmod(run, 0o750)
    assert here({"RUNTIME_DIRECTORY": str(run)}) is None
    os.chmod(run, 0o700)
    link = tmp_path / "link"
    link.symlink_to(run)
    assert here({"RUNTIME_DIRECTORY": str(link)}) is None


def numbers(value):
    return isinstance(value, list) and all(isinstance(n, int) for n in value)


def test_a_file_of_its_shape_reads_back_until_forgotten(run):
    kept = RunFile(run, "n.json", 32, numbers, "numbers")
    assert kept.read() is None
    kept.keep([1, 2])
    assert kept.read() == [1, 2]
    assert (run / "n.json").stat().st_mode & 0o777 == 0o600
    kept.forget()
    assert kept.read() is None and not (run / "n.json").exists()
    kept.forget()  # none to forget


def test_one_not_of_its_shape_or_too_big_is_never_written_and_what_was_kept_goes(run):
    kept = RunFile(run, "n.json", 32, numbers, "numbers")
    for value in (["x"], list(range(20))):
        kept.keep([1])
        kept.keep(value)
        assert not (run / "n.json").exists(), value


@pytest.mark.parametrize("body", [b'["x"]', b"[", b"\xff\xfe", b"[1]" + b" " * 32])
def test_what_isnt_of_its_shape_or_is_too_big_reads_as_none(run, body):
    (run / "n.json").write_bytes(body)
    assert RunFile(run, "n.json", 32, numbers, "numbers").read() is None


def test_json_nested_too_deep_to_read_reads_as_none(run):
    (run / "n.json").write_bytes(b"[" * 30000 + b"]" * 30000)
    assert RunFile(run, "n.json", 65536, numbers, "numbers").read() is None  # not RecursionError


def test_a_link_in_its_place_is_never_followed(run, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("[1]")
    (run / "n.json").symlink_to(elsewhere)
    kept = RunFile(run, "n.json", 32, numbers, "numbers")
    assert kept.read() is None
    kept.keep([2])  # replaces the link, never writes through it
    assert not (run / "n.json").is_symlink() and kept.read() == [2]
    assert json.loads(elsewhere.read_text()) == [1]


def test_a_hard_link_a_folder_or_a_pipe_in_its_place_reads_as_none(run):
    other = run / "other.json"
    other.write_text("[1]")
    os.link(other, run / "n.json")
    assert RunFile(run, "n.json", 32, numbers, "numbers").read() is None
    os.unlink(run / "n.json")
    (run / "n.json").mkdir()
    assert RunFile(run, "n.json", 32, numbers, "numbers").read() is None
    (run / "n.json").rmdir()
    os.mkfifo(run / "n.json")
    assert RunFile(run, "n.json", 32, numbers, "numbers").read() is None  # without waiting for a writer

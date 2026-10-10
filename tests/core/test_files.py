import os
import stat

import pytest

from departureboard.core import files


def test_a_file_is_replaced_whole_with_the_mode_given_whatever_the_umask(tmp_path):
    path = tmp_path / "deep" / "web.json"
    old = os.umask(0)
    try:
        files.write_atomic(path, "first", 0o600)
        files.write_atomic(path, b"second", 0o640)
    finally:
        os.umask(old)
    assert path.read_text() == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert os.listdir(path.parent) == ["web.json"]  # nothing left aside


def test_it_is_on_disk_before_it_replaces_the_old_file(tmp_path, monkeypatch):
    calls = []
    replace = files.os.replace
    monkeypatch.setattr(files.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(files.os, "replace", lambda src, dst: calls.append("replace") or replace(src, dst))
    files.write_atomic(tmp_path / "state.json", "{}", 0o640)
    assert calls == ["fsync", "replace"]


@pytest.mark.skipif(not files.NOFOLLOW, reason="needs O_NOFOLLOW")
def test_a_link_put_where_the_file_is_written_aside_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (tmp_path / "secrets.env.tmp").symlink_to(target)
    with pytest.raises(OSError):
        files.write_atomic(tmp_path / "secrets.env", "KEY=x", 0o600)
    assert target.read_text() == "keep"


@pytest.mark.skipif(not files.NOFOLLOW, reason="needs O_NOFOLLOW")
def test_a_lock_file_is_private_and_no_link(tmp_path):
    os.close(files.open_lock(tmp_path / "a.lock"))
    assert stat.S_IMODE((tmp_path / "a.lock").stat().st_mode) == 0o600 & ~_umask()
    (tmp_path / "b.lock").symlink_to(tmp_path / "a.lock")
    with pytest.raises(OSError):
        files.open_lock(tmp_path / "b.lock")


def _umask() -> int:
    old = os.umask(0)
    os.umask(old)
    return old

"""Files the app writes for itself: whole or not at all, and never through a
link someone put in their place."""

from __future__ import annotations

import os
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # refuse a link at the path itself
CLOEXEC = getattr(os, "O_CLOEXEC", 0)  # not handed on to a child process


def write_atomic(path: Path, data: str | bytes, mode: int) -> None:
    """Replace `path` whole: written aside with `mode` (whatever the umask, or
    a leftover file's mode), on disk, then renamed over it, so a reader or a
    power cut never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW | CLOEXEC, mode)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(data.encode("utf-8") if isinstance(data, str) else data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def open_lock(path: Path, mode: int = 0o600) -> int:
    """A file descriptor for the lock file at `path`, made if need be, for the
    caller to flock and close."""
    return os.open(path, os.O_RDWR | os.O_CREAT | NOFOLLOW | CLOEXEC, mode)

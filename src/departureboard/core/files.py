"""Files the app writes for itself: whole or not at all, and never through a
link someone put in their place."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # refuse a link at the path itself
CLOEXEC = getattr(os, "O_CLOEXEC", 0)  # not handed on to a child process


def write_atomic(path: Path, data: str | bytes, mode: int) -> None:
    """Replace `path` whole: written aside with `mode` (whatever the umask), on
    disk, then renamed over it, so a reader or a power cut never sees half a
    file. The file aside is new each time, under a name no one can guess, so
    writers at once (threads or processes) each land whole, the last one
    winning, and a link planted beside it is never written through."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data.encode("utf-8") if isinstance(data, str) else data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def open_lock(path: Path, mode: int = 0o600) -> int:
    """A file descriptor for the lock file at `path`, made if need be, for the
    caller to flock and close."""
    return os.open(path, os.O_RDWR | os.O_CREAT | NOFOLLOW | CLOEXEC, mode)

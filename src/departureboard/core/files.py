"""Files the app writes for itself: whole or not at all, and never through a
link someone put in their place."""

from __future__ import annotations

import os
import re
import secrets
import stat
import time
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # refuse a link at the path itself
CLOEXEC = getattr(os, "O_CLOEXEC", 0)  # not handed on to a child process
# What follows a file's name in the name of the file written aside for it.
_ASIDE = re.compile(r"\.[0-9a-f]{16}\.tmp")
# A file aside this old is a write cut off, never one still going: even a 50 MB
# timetable is written and renamed within a minute or so on a Pi's SD card.
STALE_ASIDE_SECONDS = 600.0


def write_atomic(path: Path, data: str | bytes, mode: int) -> None:
    """Replace `path` whole: written aside with `mode` (whatever the umask), on
    disk, then renamed over it, so a reader or a power cut never sees half a
    file. The file aside is new each time, under a name no one can guess, so
    writers at once (threads or processes) each land whole, the last one
    winning, and a link planted beside it is never written through. One a
    write cut off left aside for good is cleared away by the next write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear_stale_aside(path)
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


def _clear_stale_aside(path: Path) -> None:
    """Remove what writes to `path` cut off by a power cut or a kill left
    aside: a copy of secrets.env holds the keys, and a timetable's is 50 MB.
    Only plain files of this user's, named exactly as write_atomic names
    them for this file, and old enough that no writer is still at them. The
    name is compared as text, never as a pattern, and nothing is followed
    through a link. Housekeeping only: a folder that can't be read or a file
    that can't go is left for the next write."""
    now = time.time()
    try:
        with os.scandir(path.parent) as entries:
            for entry in entries:
                name = entry.name
                if not name.startswith(path.name) or not _ASIDE.fullmatch(name, len(path.name)):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                            and now - info.st_mtime > STALE_ASIDE_SECONDS):
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def open_lock(path: Path, mode: int = 0o600) -> int:
    """A file descriptor for the lock file at `path`, made if need be, for the
    caller to flock and close."""
    return os.open(path, os.O_RDWR | os.O_CREAT | NOFOLLOW | CLOEXEC, mode)

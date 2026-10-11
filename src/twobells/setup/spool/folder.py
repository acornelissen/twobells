"""The spool folder and its files: where it is, and reading and writing them safely."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path

from ...core import files
from .checks import REQUEST_MAX_AGE_S, Refused

RUNTIME = Path("/run/twobells-fetch")  # the units' RuntimeDirectory
MAX_REQUEST_BYTES = 16 * 1024  # the biggest, a walk to 25 places, is about 2 kB
MAX_REPLY_BYTES = 512 * 1024  # the biggest, a search with route strips, is tens of kB
TMP_MAX_AGE_S = 60.0
# How long the page waits for each answer: the service may first need to
# start (several seconds on a Pi Zero), and NaPTAN's area files are big.
WAITS = {"postcode": 30.0, "address": 30.0, "stops": 120.0, "walk": 30.0, "testkey": 45.0}
# An answer nobody collected. A lookup's can hold home's exact position, and
# past the longest wait no page is waiting for it.
REPLY_MAX_AGE_S = max(WAITS.values())
STATUS_MAX_AGE_S = 600.0  # a finished search's status, unread


class SpoolError(Exception):
    """The spool folder can't be used safely."""


def spool_folder(environ: Mapping[str, str] | None = None) -> Path:
    """Where the spool is: systemd's $RUNTIME_DIRECTORY (memory); else
    RUNTIME if it is there (run by hand on the Pi). SpoolError if there is
    neither, as any other folder may be on the SD card, or if
    $RUNTIME_DIRECTORY isn't one absolute path."""
    given = (os.environ if environ is None else environ).get("RUNTIME_DIRECTORY", "")
    if given:
        if not os.path.isabs(given) or ":" in given:
            raise SpoolError("RUNTIME_DIRECTORY must be one absolute folder for the setup spool")
        return Path(given)
    if os.path.lexists(RUNTIME):  # Spool.make refuses it unless it's a real folder of ours
        return RUNTIME
    raise SpoolError(f"No folder in memory for the setup spool: {RUNTIME} isn't there. On the Pi, "
                     "start twobells-web.service, which makes it; elsewhere, set RUNTIME_DIRECTORY "
                     "to a folder that isn't on an SD card.")


class Spool:
    """The three folders, and reading and writing files in them safely."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.tmp = self.folder / "tmp"
        self.requests = self.folder / "requests"
        self.replies = self.folder / "replies"
        self.status = self.folder / "status"
        self.snapshots = self.folder / "snapshots"

    def make(self) -> None:
        """Make the folders (0700) if need be, and check each is a real folder of ours."""
        for path in (self.folder, self.tmp, self.requests, self.replies, self.status, self.snapshots):
            try:
                path.mkdir(mode=0o700, exist_ok=True)
                info = os.lstat(path)
            except OSError as error:
                raise SpoolError(f"Can't make the setup spool folder {path}: {error.strerror or error}") from None
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise SpoolError(f"{path} must be a folder owned by this user, not a link")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(path, 0o700)

    def write(self, where: Path, file_id: str, data: dict, cap: int) -> None:
        """Write `data` as <file_id>.json in `where`, whole: made new in tmp/ (0600), then renamed."""
        body = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > cap:
            raise ValueError("Too big for the setup spool")
        tmp = self.tmp / f"{file_id}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | files.NOFOLLOW | files.CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(body)
            os.replace(tmp, where / f"{file_id}.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def read(path: Path, cap: int, delete: bool) -> bytes | None:
        """A file's bytes; None if there is none. Refused if it isn't a plain
        file of ours, with one link, within `cap`. With `delete`, it is gone
        afterwards whatever it held."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | files.NOFOLLOW | files.CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:  # a link, or something else that won't open as a file
            if delete:
                _remove(path)
            raise Refused("not a plain file") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_size > cap):
                body = None
            else:
                body = os.read(fd, cap + 1)
        finally:
            os.close(fd)
            if delete:
                path.unlink(missing_ok=True)
        if body is None or len(body) > cap:
            raise Refused("not a plain file of ours within its size")
        return body

    def sweep(self, now: float) -> None:
        """Delete what has been left too long: half-written files, requests no
        one is waiting for, replies and search statuses no one collected."""
        for where, age in ((self.tmp, TMP_MAX_AGE_S), (self.requests, REQUEST_MAX_AGE_S),
                           (self.replies, REPLY_MAX_AGE_S), (self.status, STATUS_MAX_AGE_S)):
            try:
                names = os.listdir(where)
            except OSError:
                continue
            for name in names:
                path = where / name
                try:
                    old = now - os.lstat(path).st_mtime > age
                except OSError:
                    continue
                if old:
                    _remove(path)


def _remove(path: Path) -> None:
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass

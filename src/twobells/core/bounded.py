"""Reading what came from outside within bounds: a zip's list of files,
checked before zipfile reads it, and text a line at a time."""

from __future__ import annotations

import struct
import zipfile
from collections.abc import Iterator
from typing import IO, Protocol

# The end of a zip (PKWARE's APPNOTE.TXT, 4.3.14 to 4.3.16): an end record of
# 22 bytes, then a comment of up to 65,535. A ZIP64 zip (over 65,535 files,
# or 4 GB) has the real figures in a ZIP64 end record of 56 bytes, then a
# 20-byte locator, just before it.
_END = struct.Struct("<4s4H2LH")  # signature, disks, files here and in all, list bytes, its offset, comment
_END_SIGNATURE = b"PK\x05\x06"
_MAX_COMMENT = 65_535
_LOCATOR = struct.Struct("<4sLQL")  # signature, disk, ZIP64 end record's offset, disks
_LOCATOR_SIGNATURE = b"PK\x06\x07"
_END64 = struct.Struct("<4sQ2H2L4Q")  # signature, size, versions, disks, files here and in all, list bytes, offset
_END64_SIGNATURE = b"PK\x06\x06"
# The most zipfile reads to find those before it reads the list: the last 22
# bytes, the last 64 KiB, the locator, and the ZIP64 record in two places.
_END_READS = _END.size + _END.size + _MAX_COMMENT + _LOCATOR.size + 2 * _END64.size


class ZipSource(Protocol):
    """What a zip is read from: a file, or a wrapper reading through one."""

    def read(self, size: int = -1, /) -> bytes: ...
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def tell(self) -> int: ...


class ZipTooBig(Exception):
    """A zip lists more files, or more bytes of them, than its reader allows."""


def zip_listing(file: ZipSource) -> tuple[int, int] | None:
    """(files, bytes) of the list of files in the zip in `file`, as its end
    record gives them, or None if it has none. The record is found as
    zipfile finds it: the last signature in the file's last 64 KiB and 22
    bytes, with the ZIP64 record just before its locator, if there is one."""
    file.seek(0, 2)
    start = max(file.tell() - _END.size - _MAX_COMMENT, 0)
    file.seek(start)
    tail = file.read(_END.size + _MAX_COMMENT)
    at = tail.rfind(_END_SIGNATURE)
    if at < 0 or len(tail) - at < _END.size:
        return None
    files, list_bytes = _END.unpack_from(tail, at)[4:6]
    locator_at = start + at - _LOCATOR.size
    if locator_at - _END64.size >= 0:
        file.seek(locator_at - _END64.size)
        end64 = file.read(_END64.size + _LOCATOR.size)
        if end64[_END64.size:].startswith(_LOCATOR_SIGNATURE) and end64.startswith(_END64_SIGNATURE):
            files, list_bytes = _END64.unpack_from(end64)[7:9]
    # A ZIP64 zip whose record isn't just there gives 65,535 files here: too many for any reader.
    return files, list_bytes


def open_zip(file: ZipSource, max_files: int, max_list_bytes: int) -> zipfile.ZipFile:
    """The zip in `file`. zipfile reads its whole list of files into memory
    before any file, and a small hostile zip can list hundreds of thousands,
    so ZipTooBig if its end record lists more than `max_files`, or more than
    `max_list_bytes` of them, before zipfile reads a byte of it. zipfile then
    reads the list within that many bytes, so were its own reading of the end
    record to differ from zip_listing's, it still couldn't read more; and the
    files are counted again once read. zipfile.BadZipFile if it isn't a zip."""
    listing = zip_listing(file)
    if listing and (listing[0] > max_files or listing[1] > max_list_bytes):
        raise ZipTooBig
    file.seek(0)
    budget = _Budget(file, _END_READS + max_list_bytes)
    archive = zipfile.ZipFile(budget)
    budget.left = None  # the files themselves are read as wanted
    if len(archive.infolist()) > max_files:
        archive.close()
        raise ZipTooBig
    return archive


class _Budget:
    """A file read through, giving ZipTooBig once more than `left` bytes have
    been read, until `left` is None."""

    def __init__(self, file: ZipSource, left: int) -> None:
        self._file = file
        self.left: int | None = left

    def read(self, size: int = -1) -> bytes:
        if self.left is None:
            return self._file.read(size)
        data = self._file.read(self.left + 1 if size < 0 else min(size, self.left + 1))
        self.left -= len(data)
        if self.left < 0:
            raise ZipTooBig
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def seekable(self) -> bool:
        return True


def lines(text: IO[str], max_line: int) -> Iterator[str]:
    """Each line of `text`, never reading more than `max_line` + 1 characters
    at once: a hostile file's one line can be hundreds of MB. A longer line
    is read past a piece at a time, and comes out as ""."""
    while line := text.readline(max_line + 1):
        if len(line) <= max_line:
            yield line
            continue
        while line and not line.endswith(("\n", "\r")):
            line = text.readline(max_line + 1)
        yield ""

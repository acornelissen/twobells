import io
import struct
import zipfile

import pytest

from twobells.core import bounded
from twobells.core.bounded import ZipTooBig, lines, open_zip, zip_listing


def _zip(names, comment=b""):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in names:
            archive.writestr(name, b"")
        archive.comment = comment
    return out


def _list_bytes(file):
    """The bytes of a zip's list of files, as zipfile read them."""
    with zipfile.ZipFile(file) as archive:
        return sum(46 + len(i.filename.encode()) + len(i.extra) + len(i.comment) for i in archive.infolist())


def test_a_zip_s_files_and_list_bytes_are_read_from_its_end_record():
    file = _zip(["a.xml", "b.xml", "longer name.xml"])
    assert zip_listing(file) == (3, _list_bytes(file))


@pytest.mark.parametrize("comment", [b"x", b"PK" * 100, b"x" * 65_535], ids=["short", "pk", "longest"])
def test_the_end_record_is_found_before_a_comment(comment):
    file = _zip(["a.xml", "b.xml"], comment)
    assert zip_listing(file) == (2, _list_bytes(file))


def test_a_zip64_zip_s_figures_are_read_from_its_zip64_end_record(monkeypatch):
    # zipfile writes ZIP64 end records past 65,535 files, its plain end
    # record saying 65,535; made so with three.
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 2)
    data = bytearray(_zip(["a.xml", "b.xml", "c.xml"]).getvalue())
    monkeypatch.undo()
    assert data[-22 - 20 - 56:-22 - 20].startswith(b"PK\x06\x06") and data[-22 - 20:-22].startswith(b"PK\x06\x07")
    data[-14:-10] = struct.pack("<2H", 0xFFFF, 0xFFFF)  # files on this disk and in all
    file = io.BytesIO(bytes(data))
    assert zip_listing(file) == (3, _list_bytes(file))
    with open_zip(file, 3, 1000) as archive:
        assert archive.namelist() == ["a.xml", "b.xml", "c.xml"]
    with pytest.raises(ZipTooBig):
        open_zip(file, 2, 1000)


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x05\x06 too short"], ids=["empty", "text", "cut short"])
def test_a_file_with_no_end_record_has_no_listing(data):
    assert zip_listing(io.BytesIO(data)) is None
    with pytest.raises(zipfile.BadZipFile):
        open_zip(io.BytesIO(data), 10, 1000)


def test_a_zip_within_its_bounds_is_opened_and_read():
    file = _zip(["a.xml", "b.xml"])
    with open_zip(file, 2, _list_bytes(file)) as archive:
        assert archive.read("b.xml") == b""


def test_a_zip_listing_too_many_files_is_refused():
    with pytest.raises(ZipTooBig):
        open_zip(_zip(["a", "b", "c"]), 2, 1000)


def test_a_zip_listing_too_many_bytes_of_files_is_refused():
    # A name can be 65,535 bytes: a few files can list megabytes.
    with pytest.raises(ZipTooBig):
        open_zip(_zip(["a" * 1000]), 10, 1000)


def test_a_zip_whose_end_record_gives_too_few_files_is_still_refused():
    data = bytearray(_zip(["a", "b", "c"]).getvalue())
    data[-14:-10] = struct.pack("<2H", 1, 1)  # files on this disk and in all
    with pytest.raises(ZipTooBig):
        open_zip(io.BytesIO(bytes(data)), 2, 1000)


def test_zipfile_reads_no_more_of_a_list_than_allowed_whatever_end_record_it_finds(monkeypatch):
    # Were zipfile to find an end record other than zip_listing's.
    file = _zip(f"{i:08d}" for i in range(4000))  # 54 bytes a file listed
    monkeypatch.setattr(bounded, "zip_listing", lambda file: (1, 10))
    with pytest.raises(ZipTooBig):
        open_zip(file, 5000, 10)
    with open_zip(file, 5000, _list_bytes(file)) as archive:
        assert len(archive.infolist()) == 4000


def test_lines_longer_than_the_most_come_out_empty_and_the_rest_as_they_are():
    text = io.StringIO("head\r\n" + "x" * 100 + "\n" + "y" * 9 + "\r" + "last", newline="")
    assert list(lines(text, 10)) == ["head\r\n", "", "y" * 9 + "\r", "last"]


# A number from JSON or TOML.


@pytest.mark.parametrize("value, number", [(3, 3.0), (-1.5, -1.5), (0, 0.0), (10**300, 1e300)])
def test_a_finite_number_is_read_as_a_float(value, number):
    assert bounded.finite(value) == number and isinstance(bounded.finite(value), float)


@pytest.mark.parametrize("value", [10**400, -(10**400), float("nan"), float("inf"), float("-inf"), True, False,
                                   "1.5", None, [1], {"x": 1}])
def test_anything_else_is_not_a_finite_number(value):
    assert bounded.finite(value) is None

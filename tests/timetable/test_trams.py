"""The Metrolink timetable from TfGM's GTFS, read from small made-up files
shaped like the real one (checked October 2026: agency METL, route_type 0,
platforms coded 9400ZZMA and the stop's three letters and a digit, headsigns
ending "(Manchester Metrolink)", replacement buses as tram routes)."""

import io
import json
import zipfile
from datetime import date, datetime, timedelta

import pytest

from twobells.timetable import trams
from twobells.timetable.build import TimetableBusy, TimetableError, TimetableTooBig

STOPS = {"A": "9400ZZMAPIC"}
NOW = datetime(2026, 10, 19, 7, 55)  # a Monday

PARTS = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone,agency_lang,agency_phone,agency_fare_url,"
                  "agency_email,agency_noc\n"
                  "1,Arriva,https://example.org,Europe/London,,,,,ANWE\n"
                  "2,Metrolink,https://tfgm.com/public-transport/tram,Europe/London,,,,,METL\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_desc,route_type,route_url,route_color,"
                  "route_text_color\n"
                  'B1,1,375,"Southport - Wigan, via Ormskirk",,3,,,\n'
                  "T1,2,Blue Line,Ashton-under-Lyne - Bury,,0,,,\n"
                  "T2,2,Replacement Bus,Piccadilly - Altrincham,,0,,,\n"
                  "X1,1,Not a tram,Somewhere,,0,,,\n",  # route_type 0, but not Metrolink's
    "stops.txt": "stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,zone_id,stop_url,location_type,"
                 "parent_station,wheelchair_boarding\n"
                 "10,9400ZZMAPIC1,Piccadilly (Manchester Metrolink),,53.477,-2.2306,,,,,0\n"
                 "11,9400ZZMAPIC2,Piccadilly (Manchester Metrolink),,53.477,-2.2306,,,,,0\n"
                 "20,9400ZZMAVIC1,Victoria (Manchester Metrolink),,53.487,-2.242,,,,,0\n"
                 "30,9400ZZMABRY1,Bury (Manchester Metrolink),,53.59,-2.29,,,,,0\n"
                 "40,1800SB12341,Station Road,,53.46,-1.96,,,,,0\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id,block_id,shape_id,"
                 "wheelchair_accessible\n"
                 "T1,WK,t1,Bury (Manchester Metrolink),,0,,,0\n"
                 "T1,WK,t2,,,0,,,0\n"  # no headsign: its last stop names it
                 "T1,WK,t3,Piccadilly (Manchester Metrolink),,1,,,0\n"  # ends at Piccadilly
                 "T2,WK,t4,Altrincham (Manchester Metrolink),,0,,,0\n"
                 "T1,SU,t5,Bury (Manchester Metrolink),,0,,,0\n"
                 "T1,WK,t6,Bury (Manchester Metrolink),,0,,,0\n"
                 "B1,WK,b1,Wigan,,0,,,0\n"
                 "X1,WK,x1,Elsewhere,,0,,,0\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign,pickup_type,"
                      "drop_off_type,shape_dist_traveled,timepoint\n"
                      "t1,08:09:30,08:09:30,10,1,,0,1,0.0,1\n"
                      "t1,08:14:00,08:14:00,20,2,,0,0,1.0,1\n"
                      "t1,08:40:00,08:40:00,30,3,,1,0,2.0,1\n"
                      "t2,24:10:00,24:10:00,11,1,,0,1,0.0,1\n"  # after midnight, on Monday's service day
                      "t2,24:30:00,24:30:00,30,2,,1,0,1.0,1\n"
                      "t3,08:00:00,08:00:00,30,1,,0,1,0.0,1\n"
                      "t3,08:20:00,08:20:00,10,2,,1,0,1.0,1\n"
                      "t4,08:05:00,08:05:00,10,1,,0,1,0.0,1\n"
                      "t4,08:45:00,08:45:00,20,2,,1,0,1.0,1\n"
                      "t5,09:00:00,09:00:00,10,1,,0,1,0.0,1\n"
                      "t5,09:30:00,09:30:00,30,2,,1,0,1.0,1\n"
                      "t6,99:99:99,99:99:99,10,1,,0,1,0.0,1\n"  # not a time
                      "t6,10:30:00,10:30:00,30,2,,1,0,1.0,1\n"
                      "b1,08:01:00,08:01:00,10,1,,0,1,0.0,1\n"
                      "b1,09:00:00,09:00:00,40,2,,1,0,1.0,1\n"
                      "x1,08:02:00,08:02:00,10,1,,0,1,0.0,1\n"
                      "x1,09:00:00,09:00:00,20,2,,1,0,1.0,1\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "WK,1,1,1,1,1,0,0,20261001,20261130\n"
                    "SU,0,0,0,0,0,0,1,20261001,20261130\n",
    "calendar_dates.txt": "service_id,date,exception_type\n"
                          "WK,20261020,2\n"  # not on Tuesday 20th
                          "SU,20261020,1\n",  # Sunday's on it instead
    "feed_info.txt": "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version\n"
                     "TfGM,https://tfgm.com,en,20261001,20261130,V1\n",
}


def gtfs(parts=PARTS) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return out.getvalue()


def read(parts=PARTS, stops=STOPS):
    with zipfile.ZipFile(io.BytesIO(gtfs(parts))) as archive:
        return trams.read_gtfs(archive, stops, NOW)


def test_only_metrolink_s_departures_from_your_stops_are_kept():
    timetable = read()
    assert [(c.minute, c.line, c.destination, c.bus) for c in timetable.calls] == [
        (485, "Replacement Bus", "Altrincham", True),  # a replacement bus, listed as a tram route
        (489, "Blue Line", "Bury", False),  # 08:09:30, seconds dropped; "(Manchester Metrolink)" too
        (540, "Blue Line", "Bury", False),
        (1450, "Blue Line", "Bury", False),  # 24:10, named by its last stop
    ]  # not t3, which ends here, nor t6, without a time, nor anyone else's buses
    assert {c.stop for c in timetable.calls} == {"A"}
    assert timetable.feed == "V1" and timetable.valid_until == date(2026, 11, 30)
    assert timetable.stops == STOPS and timetable.built == NOW


def test_the_departures_from_now_go_by_each_service_s_days():
    timetable = read()
    leaving = [(at.strftime("%a %H:%M"), call.destination) for at, call in timetable.leaving(NOW, 10)]
    assert leaving == [("Mon 08:05", "Altrincham"), ("Mon 08:09", "Bury"), ("Tue 00:10", "Bury")]
    # Tuesday 20th has Sunday's service instead, and Monday's after midnight still.
    tuesday = [(at.strftime("%a %H:%M"), call.destination) for at, call in timetable.leaving(
        datetime(2026, 10, 20, 0, 5), 10)]
    assert tuesday == [("Tue 00:10", "Bury"), ("Tue 09:00", "Bury")]
    assert [c.destination for _, c in timetable.leaving(NOW, 1)] == ["Altrincham"]
    assert timetable.leaving(NOW, 10, directions=["alt"])[0][1].destination == "Altrincham"


def test_two_departures_the_same_are_one():
    parts = dict(PARTS)
    parts["trips.txt"] += "T2,WK,t7,Altrincham (Manchester Metrolink),,0,,,0\n"
    parts["stop_times.txt"] += "t7,08:05:00,08:05:00,10,1,,0,1,0.0,1\nt7,08:45:00,08:45:00,20,2,,1,0,1.0,1\n"
    assert len(read(parts).calls) == 5
    assert [c.destination for _, c in read(parts).leaving(NOW, 2)] == ["Altrincham", "Bury"]


def test_a_file_without_metrolink_is_refused():
    parts = dict(PARTS, **{"agency.txt": PARTS["agency.txt"].replace("METL", "XXXX").replace("Metrolink,", "Tram,")})
    with pytest.raises(trams.NotMetrolink):
        read(parts)


def test_a_missing_part_is_refused():
    parts = {k: v for k, v in PARTS.items() if k != "stop_times.txt"}
    with pytest.raises(TimetableError, match=r"no stop_times\.txt"):
        read(parts)


def test_bad_rows_are_left_out_not_raised():
    parts = dict(PARTS)
    parts["stop_times.txt"] += ("t1,08:30:00,08:30:00,10,notanumber,,0,0,0,1\n"
                                "t1\n"
                                + "t1," + "x" * 5000 + "\n")
    parts["calendar.txt"] += "WK,1,1,1,1,1,0,0,notadate,20261130\n"
    assert len(read(parts).calls) == 4


def test_too_many_departures_is_refused(monkeypatch):
    monkeypatch.setattr(trams, "MAX_CALLS", 2)
    with pytest.raises(TimetableTooBig):
        read()


def test_saved_and_read_back_whole(tmp_path):
    timetable = read()
    trams.save(tmp_path, timetable)
    assert trams.load(tmp_path) == timetable
    assert oct(((tmp_path / trams.FILE_NAME).stat().st_mode) & 0o777) == "0o640"


@pytest.mark.parametrize("edit", [
    lambda d: d.update(stops={"A": "not a code"}),
    lambda d: d["calls"].append(["Z", 1, 0, 0, "WK", 0]),
    lambda d: d["calls"].append(["A", 99999, 0, 0, "WK", 0]),
    lambda d: d["services"]["WK"].update(days=[9]),
    lambda d: d.pop("calls"),
])
def test_a_saved_file_that_isn_t_one_is_ignored(tmp_path, edit):
    data = json.loads(trams.to_json(read()))
    edit(data)
    (tmp_path / trams.FILE_NAME).write_text(json.dumps(data))
    assert trams.load(tmp_path) is None


def test_it_is_due_weekly_and_when_the_stops_change():
    timetable = read()
    assert trams.due(None, STOPS, NOW) == "there is none"
    assert trams.due(timetable, STOPS, NOW + timedelta(days=6)) is None
    assert trams.due(timetable, STOPS, NOW + timedelta(days=7)) == "it is a week old"
    assert trams.due(timetable, {"A": "9400ZZMAGMX"}, NOW) == "your tram stops changed"
    assert trams.unusable(timetable, STOPS, NOW + timedelta(days=27)) is None
    assert trams.unusable(timetable, STOPS, NOW + timedelta(days=29)) == "the tram timetable is over four weeks old"
    assert trams.unusable(None, STOPS, NOW) == "there is no tram timetable yet"


def _fetch(body: bytes, urls: list):
    def fetch(url, file):
        urls.append(url)
        file.write(body)
    return fetch


def test_refresh_downloads_builds_and_saves_only_when_due(tmp_path):
    urls = []
    built = trams.refresh(tmp_path, STOPS, NOW, fetch=_fetch(gtfs(), urls))
    assert urls == [trams.GTFS_URL] and built == trams.load(tmp_path)
    assert trams.refresh(tmp_path, STOPS, NOW + timedelta(hours=1), fetch=_fetch(gtfs(), urls)) == built
    assert len(urls) == 1  # not due
    trams.refresh(tmp_path, STOPS, NOW + timedelta(hours=1), fetch=_fetch(gtfs(), urls), force=True)
    assert len(urls) == 2
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_failed_build_keeps_the_saved_one_and_waits_before_trying_again(tmp_path):
    urls = []
    with pytest.raises(TimetableError, match=r"isn't a GTFS file"):
        trams.refresh(tmp_path, STOPS, NOW, fetch=_fetch(b"not a zip", urls))
    assert trams.load(tmp_path) is None
    assert trams.refresh(tmp_path, STOPS, NOW + timedelta(hours=1), fetch=_fetch(gtfs(), urls)) is None
    assert len(urls) == 1  # waiting
    assert trams.refresh(tmp_path, STOPS, NOW + timedelta(hours=7), fetch=_fetch(gtfs(), urls)) is not None
    assert not (tmp_path / trams.FAILED_FILE).exists()


def test_a_download_failing_says_so_without_its_address(tmp_path):
    def fails(url, file):
        raise OSError(f"connection refused for {url}")
    with pytest.raises(TimetableError) as failed:
        trams.refresh(tmp_path, STOPS, NOW, fetch=fails)
    assert "couldn't be had" in str(failed.value)


def test_one_build_at_a_time(tmp_path):
    with trams._lock(tmp_path), pytest.raises(TimetableBusy):
        trams.refresh(tmp_path, STOPS, NOW, fetch=_fetch(gtfs(), []))


@pytest.mark.parametrize("stops", [{}, {"A": "1800SB12341"}, {str(i): "9400ZZMAPIC" for i in range(5)}])
def test_refresh_takes_only_metrolink_stops(tmp_path, stops):
    with pytest.raises(TimetableError, match="Metrolink stop codes"):
        trams.refresh(tmp_path, stops, NOW, fetch=_fetch(gtfs(), []))


def test_a_part_that_unzips_to_more_than_it_may_is_refused(monkeypatch):
    monkeypatch.setattr(trams, "_MAX_OTHER_PART_BYTES", 100)
    with pytest.raises(TimetableTooBig):
        read()


def _peak_bytes(work) -> int:
    """The most memory `work()` held at once, by tracemalloc."""
    import tracemalloc

    tracemalloc.start()
    try:
        work()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("name", ["stop_times.txt", "agency.txt"])
def test_an_over_long_line_is_read_past_a_piece_at_a_time(name):
    # A hostile file's line can unzip to hundreds of MB from a small zip: it is never held whole.
    parts = dict(PARTS)
    header, rest = parts[name].split("\n", 1)
    parts[name] = header + "\n" + "x" * 20_000_000 + "\n" + rest
    archive = zipfile.ZipFile(io.BytesIO(gtfs(parts)))
    found = []
    assert _peak_bytes(lambda: found.append(trams.read_gtfs(archive, STOPS, NOW))) < 2_000_000
    assert len(found[0].calls) == 4  # the lines after it are read as before


@pytest.mark.parametrize("packing", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_a_part_packed_other_than_stored_or_deflated_is_refused_unread(packing):
    # zipfile unpacks bzip2 and LZMA a whole read at a time, with no cap on
    # what comes out: a few hundred bytes of bzip2 can make 290 MB in one read.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in PARTS.items():
            if name != "agency.txt":
                archive.writestr(name, text)
        archive.writestr("agency.txt", PARTS["agency.txt"] + "0" * 20_000_000, compress_type=packing)
    archive = zipfile.ZipFile(io.BytesIO(out.getvalue()))
    failed = []

    def work():
        try:
            trams.read_gtfs(archive, STOPS, NOW)
        except TimetableError as error:
            failed.append(error)

    assert _peak_bytes(work) < 2_000_000
    assert len(failed) == 1


def test_an_over_long_header_leaves_its_part_with_no_columns():
    parts = dict(PARTS)
    parts["agency.txt"] = "x" * 10_000 + "\n" + parts["agency.txt"]
    with pytest.raises(trams.NotMetrolink):
        read(parts)


def test_a_file_of_too_many_parts_is_refused_before_they_are_read(tmp_path):
    # zipfile holds every part it lists before any is read: 20,000 empty ones would take megabytes.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, text in PARTS.items():
            archive.writestr(name, text)
        for i in range(20_000):
            archive.writestr(f"{i}", b"")
    body = out.getvalue()
    failed = []

    def build():
        try:
            trams.refresh(tmp_path, STOPS, NOW, fetch=_fetch(body, []))
        except TimetableError as error:
            failed.append(str(error))

    assert _peak_bytes(build) < 2_000_000
    assert failed == [f"The tram timetable has more than {trams.MAX_PARTS} parts"]


def test_a_file_with_parts_beside_gtfs_s_own_is_read(tmp_path):
    parts = {**PARTS, **{f"extra{i}.txt": "a\n" for i in range(trams.MAX_PARTS - len(PARTS))}}
    assert trams.refresh(tmp_path, STOPS, NOW, fetch=_fetch(gtfs(parts), [])) is not None

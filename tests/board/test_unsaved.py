import json
import os

import pytest

from twobells.board.state import departure_to_dict
from twobells.board.unsaved import MAX_BYTES, NAME, SOURCES_MAX_BYTES, SOURCES_NAME, KeptRender, KeptSources
from twobells.core.model import Departure, Status

DRAWN = {"signature": "a" * 64, "at": "2026-10-08T07:00:00", "page": "departures"}


@pytest.fixture
def run(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=, RuntimeDirectoryMode=0700
    return folder


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


MARK = {"last_success": "2026-10-08T09:18:00"}
ENTRY = {
    "departures": [departure_to_dict(Departure("train", "Northern", "Manchester Piccadilly", "09:45", None,
                                               Status.ON_TIME))],
    "last_attempt": "2026-10-08T09:29:00", "last_success": "2026-10-08T09:18:00",
    "last_error": {"at": "2026-10-08T09:29:00", "message": "network down"},
    "messages": ["Lifts out of use."], "note": "Timetabled times only", "not_updating": MARK,
}


def test_what_each_source_fetched_goes_back_in_place_of_the_card_s_for_the_board_s_sources_only(run):
    state = {"sources": {"train": {**ENTRY, "other": 1}, "bus": {}, "metro": {"last_attempt": "2026-10-08T09:29:00"}}}
    assert KeptSources(run, ("train", "metro")).keep(state)
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY,
                                                            "metro": {"last_attempt": "2026-10-08T09:29:00"}}
    assert (run / SOURCES_NAME).stat().st_mode & 0o777 == 0o600
    card = {"sources": {"train": {"last_attempt": "2026-10-08T09:18:00", "last_error": {"at": "x"}, "other": 2},
                        "bus": {"departures": []}}}
    KeptSources(run, ("train", "bus")).carry(card)  # [metro] since taken off the board
    assert card == {"sources": {"train": {**ENTRY, "other": 2}, "bus": {"departures": []}}}


def test_a_card_tried_since_keeps_its_own(run):
    # By a release that knows nothing of the file, say.
    KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}})
    newer = {"last_attempt": "2026-10-08T09:35:00", "last_success": "2026-10-08T09:35:00"}
    card = {"sources": {"train": dict(newer)}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": newer}}


def test_a_kept_fetch_that_cleared_the_error_and_note_clears_the_card_s(run):
    fetched = {"last_attempt": "2026-10-08T09:29:00", "last_success": "2026-10-08T09:29:00"}
    KeptSources(run, ("train",)).keep({"sources": {"train": fetched}})
    card = {"sources": {"train": {"last_attempt": "2026-10-08T09:18:00", "note": "From TransportAPI",
                                  "last_error": {"at": "2026-10-08T09:18:00", "message": "network down"}}}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": fetched}}


def test_a_card_tried_at_the_same_time_as_what_was_kept_takes_what_was_kept(run):
    # A paid lookup saved as spent before it was made, then fetched, its last save failing.
    KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}})
    card = {"sources": {"train": {"last_attempt": ENTRY["last_attempt"]}}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": ENTRY}}


def test_nothing_to_keep_keeps_no_file(run):
    KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}})
    assert KeptSources(run, ("train",)).keep({"sources": {"train": {}}})
    assert not (run / SOURCES_NAME).exists()


@pytest.mark.parametrize("change", [
    {"not_updating": None}, {"not_updating": {"last_success": "soon"}}, {"not_updating": {"last_success": None, "x": 1}},
    {"not_updating": {"last_success": "2026-10-08T09:18:00+01:00"}}, {"last_attempt": "09:29"},
    {"last_success": "2026-10-08T09:18:00.1234567"}, {"last_error": {"at": "2026-10-08T09:29:00"}},
    {"last_success": "9999-12-31T23:59:00"}, {"last_attempt": "0001-01-01T00:00:00"},
    {"last_error": {"at": "2026-10-08T09:29:00", "message": 1}}, {"messages": "Lifts out of use."},
    {"messages": ["x" * 2001]}, {"messages": ["x"] * 17}, {"note": None}, {"departures": [{"service": "x"}]},
    {"departures": [ENTRY["departures"][0]] * 33}, {"departures": [{**ENTRY["departures"][0], "scheduled": "9:45"}]},
    {"other": 1},
])
def test_entries_kept_with_one_part_not_of_the_shape_are_not_put_back(run, change):
    (run / SOURCES_NAME).write_text(json.dumps({"bus": ENTRY, "train": {**ENTRY, **change}}))
    card = {"sources": {"train": {"not_updating": MARK}}}
    KeptSources(run, ("train", "bus")).carry(card)
    assert card == {"sources": {"train": {"not_updating": MARK}}}  # neither: the card's stands


@pytest.mark.parametrize("kept", [[], {"Train": ENTRY}, {"../state": ENTRY}, {"train": None},
                                  {f"s{chr(97 + n)}": ENTRY for n in range(9)}])
def test_a_file_not_of_the_shape_is_not_put_back(run, kept):
    (run / SOURCES_NAME).write_text(json.dumps(kept))
    card = {"sources": {}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {}}


def test_an_entry_not_of_the_shape_is_never_kept(run):
    KeptSources(run, ("train", "bus")).keep({"sources": {"train": {"not_updating": {"last_success": "soon"}},
                                                         "bus": {"last_attempt": "2026-10-08T09:29:00"}}})
    assert json.loads((run / SOURCES_NAME).read_text()) == {"bus": {"last_attempt": "2026-10-08T09:29:00"}}


# --- The forecast, kept beside the sources ---

HOUR = {"start": "2026-10-08T08:00:00", "probability": 70, "precipitation": 0.4}
FORECAST = {"hours": [HOUR, {**HOUR, "start": "2026-10-08T09:00:00", "probability": None, "precipitation": None}],
            "last_attempt": "2026-10-08T09:30:00", "last_success": "2026-10-08T09:00:00"}


def test_the_forecast_goes_back_in_place_of_the_card_s(run):
    state = {"sources": {"train": ENTRY}, "weather": {**FORECAST, "other": 1}}
    assert KeptSources(run, ("train",)).keep(state)
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY, "weather": FORECAST}
    card = {"sources": {}, "weather": {"last_attempt": "2026-10-08T07:00:00", "hours": [], "other": 2}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": ENTRY}, "weather": {**FORECAST, "other": 2}}
    card = {"sources": {}, "weather": None}  # a hand-edited state.json, say
    KeptSources(run, ()).carry(card)
    assert card == {"sources": {}, "weather": FORECAST}


def test_a_failed_forecast_kept_puts_back_its_try_and_no_older_forecast(run):
    tried = {"last_attempt": "2026-10-08T09:30:00"}
    KeptSources(run, ()).keep({"weather": tried})
    card = {"weather": {"last_attempt": "2026-10-08T09:00:00", "last_success": "2026-10-08T09:00:00",
                        "hours": [HOUR]}}
    KeptSources(run, ()).carry(card)
    assert card["weather"] == tried  # as the run that kept it had it


def test_a_card_whose_forecast_was_tried_since_keeps_its_own(run):
    KeptSources(run, ()).keep({"weather": FORECAST})
    newer = {"last_attempt": "2026-10-08T10:00:00", "last_success": "2026-10-08T10:00:00", "hours": []}
    card = {"sources": {}, "weather": dict(newer)}
    KeptSources(run, ()).carry(card)
    assert card["weather"] == newer


def test_a_file_from_before_the_forecast_was_kept_still_goes_back(run):
    (run / SOURCES_NAME).write_text(json.dumps({"train": ENTRY}))
    card = {"sources": {}, "weather": dict(FORECAST)}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": ENTRY}, "weather": FORECAST}  # the card's forecast stands


@pytest.mark.parametrize("change", [
    {"hours": None}, {"hours": [HOUR] * 73}, {"hours": [{**HOUR, "x": 1}]}, {"hours": [{"start": HOUR["start"]}]},
    {"hours": ["2026-10-08T08:00:00"]}, {"hours": [{**HOUR, "start": "2026-10-08T08:00:00+01:00"}]},
    {"hours": [{**HOUR, "start": "9999-12-31T23:00:00"}]}, {"hours": [{**HOUR, "start": "1969-12-31T23:00:00"}]},
    {"hours": [{**HOUR, "start": 1}]}, {"hours": [{**HOUR, "probability": 101}]},
    {"hours": [{**HOUR, "probability": -1}]}, {"hours": [{**HOUR, "probability": 50.5}]},
    {"hours": [{**HOUR, "probability": True}]}, {"hours": [{**HOUR, "precipitation": -0.1}]},
    {"hours": [{**HOUR, "precipitation": "0.4"}]}, {"hours": [{**HOUR, "precipitation": False}]},
    {"hours": [{**HOUR, "precipitation": 10**400}]}, {"last_attempt": "09:30"},
    {"last_success": "3000-01-01T00:00:00"}, {"last_success": None}, {"other": 1},
])
def test_a_forecast_kept_with_one_part_not_of_the_shape_puts_nothing_back(run, change):
    (run / SOURCES_NAME).write_text(json.dumps({"train": ENTRY, "weather": {**FORECAST, **change}}))
    card = {"sources": {}, "weather": {"last_attempt": "2026-10-08T07:00:00"}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {}, "weather": {"last_attempt": "2026-10-08T07:00:00"}}  # the card's stands


@pytest.mark.parametrize("change", [{"hours": [{**HOUR, "probability": 101}]}, {"last_success": "soon"}])
def test_a_forecast_not_of_the_shape_is_never_kept(run, change):
    KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}, "weather": {**FORECAST, **change}})
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY}  # the sources still are


@pytest.mark.parametrize("body", [
    '{"weather": NaN}',
    '{"weather": {"hours": [{"start": "2026-10-08T08:00:00", "probability": 70, "precipitation": NaN}]}}',
])
def test_a_forecast_that_isnt_json_as_written_puts_nothing_back(run, body):
    (run / SOURCES_NAME).write_text(body)
    card = {"sources": {}}
    KeptSources(run, ()).carry(card)
    assert card == {"sources": {}}


def test_eight_sources_and_the_forecast_are_the_most_kept(run):
    sources = {f"s{chr(97 + n)}": {"last_attempt": "2026-10-08T09:29:00"} for n in range(8)}
    (run / SOURCES_NAME).write_text(json.dumps({**sources, "weather": FORECAST}))
    card = {"sources": {}}
    KeptSources(run, sources).carry(card)
    assert card == {"sources": sources, "weather": FORECAST}


def test_the_longest_forecast_with_full_sections_keeps_well_within_the_file_s_size(run):
    hours = [{**HOUR, "start": f"2026-10-{8 + h // 24:02}T{h % 24:02}:00:00", "precipitation": 12.345678901234567}
             for h in range(72)]
    entry = {**ENTRY, "departures": ENTRY["departures"] * 6}
    state = {"sources": {"train": entry, "bus": entry, "metro": entry}, "weather": {**FORECAST, "hours": hours}}
    assert KeptSources(run, ("train", "bus", "metro")).keep(state)
    assert (run / SOURCES_NAME).stat().st_size < SOURCES_MAX_BYTES // 4
    card = {}
    KeptSources(run, ("train", "bus", "metro")).carry(card)
    assert card == state


# --- The bank holidays' cache, kept beside the sources ---


def gov_uk_list(years: int) -> bytes:
    """A gov.uk answer as big as the real one is over `years` years: 8 a year
    in England and Wales, 9 in Scotland, 10 in Northern Ireland (the real
    one, in 2026, had 83, 94 and 103), with titles as long as theirs."""
    titles = {"england-and-wales": 8, "scotland": 9, "northern-ireland": 10}
    answer = {}
    for division, count in titles.items():
        events = [{"title": f"Battle of the Boyne (Orangemen’s Day) {n}", "date": f"{2018 + year}-{1 + n:02}-{1 + n:02}",
                   "notes": "Substitute day" if n % 3 == 0 else "", "bunting": True}
                  for year in range(years) for n in range(count)]
        answer[division] = {"division": division, "events": events}
    return json.dumps(answer).encode()


def held(tmp_path, monkeypatch, years: int = 11) -> dict:
    """What sources/holidays.py hands back of the cache on a card that won't take it."""
    import errno
    from datetime import datetime

    from twobells.core import files
    from twobells.sources import holidays

    def full(path, data, mode):
        raise OSError(errno.ENOSPC, "No space left on device")

    unsaved = holidays.Unsaved()
    with monkeypatch.context() as patched:
        patched.setattr(files, "write_atomic", full)
        holidays.bank_holidays(tmp_path / "card", datetime(2026, 10, 8, 7, 0, 0, 123456),
                               lambda url, most: gov_uk_list(years), unsaved=unsaved)
    assert unsaved.record is not None
    return unsaved.record


def test_the_bank_holidays_are_kept_beside_the_sources_and_read_back(run, tmp_path, monkeypatch):
    record = held(tmp_path, monkeypatch)
    state = {"sources": {"train": ENTRY}, "weather": FORECAST}
    assert KeptSources(run, ("train",)).keep(state, record)
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY, "weather": FORECAST, "holidays": record}
    assert KeptSources(run, ()).holidays() == record
    card = {"sources": {}}
    KeptSources(run, ("train",)).carry(card)
    assert card == state  # the holidays go back through sources/holidays.py, not into the state
    assert KeptSources(run, ("train",)).keep({}, record)  # on their own, when the state saved but they didn't
    assert json.loads((run / SOURCES_NAME).read_text()) == {"holidays": record}


def test_a_file_from_before_the_bank_holidays_were_kept_still_goes_back(run):
    (run / SOURCES_NAME).write_text(json.dumps({"train": ENTRY, "weather": FORECAST}))  # as 0.6.0 keeps it
    assert KeptSources(run, ()).holidays() is None
    card = {"sources": {}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {"train": ENTRY}, "weather": FORECAST}


@pytest.mark.parametrize("change", [
    {"checked": "2026-10-08T07:00:00+01:00"}, {"checked": "3000-01-01T00:00:00"}, {"checked": None},
    {"fetched": 1}, {"dates": []}, {"divisions": {"england-and-wales": ["2026-02-30"]}},
    {"events": [{"date": "2026-12-25", "title": "Christmas Day"}]}, {"other": 1},
])
def test_bank_holidays_kept_not_exactly_as_handed_back_put_nothing_back(run, tmp_path, monkeypatch, change):
    record = held(tmp_path, monkeypatch, years=1)
    (run / SOURCES_NAME).write_text(json.dumps({"train": ENTRY, "holidays": {**record, **change}}))
    assert KeptSources(run, ()).holidays() is None
    card = {"sources": {}}
    KeptSources(run, ("train",)).carry(card)
    assert card == {"sources": {}}  # as for a forecast not of the shape: the card's stands
    assert KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}}, {**record, **change})
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY}  # nor is it ever kept


@pytest.mark.parametrize("body", [b'{"holidays": NaN}', b'{"holidays": [1]}', b'{"holidays": null}'])
def test_bank_holidays_that_arent_a_record_put_nothing_back(run, body):
    (run / SOURCES_NAME).write_bytes(body)
    assert KeptSources(run, ()).holidays() is None


def test_the_real_list_of_bank_holidays_keeps_with_full_sections_and_the_longest_forecast(run, tmp_path, monkeypatch):
    # About 11 kB of 64: three sections of 6 and 72 hours of forecast come to about 12.5 kB more.
    hours = [{**HOUR, "start": f"2026-10-{8 + h // 24:02}T{h % 24:02}:00:00", "precipitation": 12.345678901234567}
             for h in range(72)]
    entry = {**ENTRY, "departures": ENTRY["departures"] * 6}
    state = {"sources": {"train": entry, "bus": entry, "metro": entry}, "weather": {**FORECAST, "hours": hours}}
    record = held(tmp_path, monkeypatch)
    assert len(json.dumps(record)) > 10_000
    assert KeptSources(run, ("train", "bus", "metro")).keep(state, record)
    assert (run / SOURCES_NAME).stat().st_size < SOURCES_MAX_BYTES // 2
    assert KeptSources(run, ()).holidays() == record


def test_bank_holidays_too_big_to_keep_with_the_sources_are_left_out_and_the_sources_kept(run, tmp_path, monkeypatch,
                                                                                         caplog):
    record = held(tmp_path, monkeypatch, years=80)  # gov.uk's answer may be 1 MB
    assert len(json.dumps(record)) > SOURCES_MAX_BYTES
    with caplog.at_level("WARNING"):
        assert KeptSources(run, ("train",)).keep({"sources": {"train": ENTRY}}, record)
    assert json.loads((run / SOURCES_NAME).read_text()) == {"train": ENTRY}
    assert "without the bank holidays: it is too big to keep" in caplog.text

import re
from datetime import date, datetime

import pytest

from departureboard.status import render_status, safe_message


@pytest.mark.parametrize("text, secret", [
    ("GET https://transportapi.com/v3/uk/bus/stop/x.json?app_id=myid&app_key=s3cr3t failed", "s3cr3t"),
    ("GET https://transportapi.com/v3/uk/bus/stop/x.json?app_id=myid1234 failed", "myid1234"),
    ("<urlopen error data.bus-data.dft.gov.uk/api/v1/datafeed/?api_key=abc123def>", "abc123def"),
    ("refused key=hunter2hunter2 at the gate", "hunter2hunter2"),
    ("refused token=t0k3nvalue", "t0k3nvalue"),
    ("headers {'x-apikey': 'darwinsecret99'}", "darwinsecret99"),
    ("x-apikey: darwinsecret99 rejected", "darwinsecret99"),
    ("X-ApiKey=darwinsecret99", "darwinsecret99"),
])
def test_credentials_are_redacted(text, secret):
    message = safe_message(RuntimeError(text))
    assert secret not in message
    assert "REDACTED" in message or "?…" in message


def test_urls_lose_their_query_strings():
    message = safe_message(RuntimeError("Failed: https://example.com/a/b?boundingBox=1,2,3,4&api_key=zzz9"))
    assert message == "Failed: https://example.com/a/b?…"


def test_messages_are_one_short_line():
    message = safe_message(OSError("first line\nsecond\tline\x00 " + "x" * 400))
    assert "\n" not in message and "\t" not in message and "\x00" not in message
    assert len(message) <= 160
    assert message.startswith("first line second line")
    assert message.endswith("…")


def test_a_redacted_secret_cannot_survive_by_being_cut_short():
    # The cut comes after the redaction, so no part of the key is left behind.
    message = safe_message(RuntimeError("x" * 150 + " key=abcdefghijklmnop"))
    assert "abcdef" not in message


def test_an_error_without_words_is_named_by_its_type():
    assert safe_message(TimeoutError()) == "TimeoutError"


def test_ordinary_messages_pass_through():
    assert safe_message(OSError("HTTP Error 503: Service Unavailable")) == "HTTP Error 503: Service Unavailable"


NOW = datetime(2026, 10, 8, 8, 1)  # a Thursday
DATA = {
    "state_file": "/var/lib/departureboard/state.json",
    "train": {"crs": "GLO", "windows": ["Mon-Fri 06:30-09:30"], "stale_after_minutes": 6},
    "bus": {"windows": ["Mon-Fri 07:00-08:30"], "stale_after_minutes": 30, "daily_budget": 28},
}
STATE = {
    "sources": {
        "train": {
            "last_attempt": "2026-10-08T08:00:00",
            "last_success": "2026-10-08T07:58:00",
            "last_error": {"at": "2026-10-08T08:00:00", "message": "HTTP Error 503: <b>Service</b> Unavailable"},
            "departures": [],
        },
        "bus": {
            "last_attempt": "2026-10-08T07:36:00",
            "last_success": "2026-10-08T07:24:00",
            "last_error": {"at": "2026-10-08T07:00:00", "message": "an old failure, since put right"},
        },
    },
    "budgets": {"bus": {"day": "2026-10-08", "used": 12}},
    "last_render": {"signature": "abc", "at": "2026-10-08T07:58:00"},
}


def text_of(page):
    """The words a screen reader would read, tags dropped."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def section(page, heading):
    start = page.index(f"<h3>{heading}</h3>")
    end = page.find("<h3>", start + 1)
    return text_of(page[start:end if end > 0 else None])


def test_the_panel_has_its_own_heading():
    page = render_status(DATA, STATE, NOW)
    assert page.startswith('<section class="status" aria-labelledby="status-title"><h2 id="status-title">Status</h2>')
    assert "As of 08:01" in page


def test_each_source_says_when_it_last_updated_and_whether_it_is_checking():
    page = render_status(DATA, STATE, NOW)
    trains = section(page, "Trains")
    assert "Last update 07:58, 3 min ago" in trains
    assert "Checking now Yes" in trains
    assert "not updating" not in trains


def test_an_error_newer_than_the_last_update_is_shown_as_a_problem():
    page = render_status(DATA, STATE, NOW)
    trains = section(page, "Trains")
    assert "Last error Problem: 08:00, 1 min ago: HTTP Error 503" in trains
    assert "&lt;b&gt;Service&lt;/b&gt;" in page and "<b>Service" not in page


def test_an_error_older_than_the_last_update_is_not_shown():
    assert "old failure" not in render_status(DATA, STATE, NOW)


def test_a_source_quiet_for_longer_than_allowed_is_not_updating():
    buses = section(render_status(DATA, STATE, NOW), "Buses")
    assert "Last update 07:24, 37 min ago Problem: not updating; nothing new for over 30 min." in buses


def test_bus_hits_today_are_counted_against_the_daily_budget():
    buses = section(render_status(DATA, STATE, NOW), "Buses")
    assert "TransportAPI hits today 12 of 28" in buses
    assert "Problem" not in buses.split("TransportAPI")[1]


def test_yesterdays_hits_do_not_count_and_a_spent_budget_is_a_problem():
    yesterday = {**STATE, "budgets": {"bus": {"day": "2026-10-07", "used": 28}}}
    assert "hits today 0 of 28" in section(render_status(DATA, yesterday, NOW), "Buses")
    spent = {**STATE, "budgets": {"bus": {"day": "2026-10-08", "used": 28}}}
    assert "28 of 28 Problem: all used" in section(render_status(DATA, spent, NOW), "Buses")


def test_outside_its_windows_a_source_is_not_checking_and_not_a_problem():
    evening = datetime(2026, 10, 8, 20, 0)
    trains = section(render_status(DATA, STATE, evening), "Trains")
    assert "Checking now No, outside its checking times" in trains
    assert "not updating" not in trains


def test_the_last_redraw_is_shown():
    assert "Last redrawn 07:58, 3 min ago" in section(render_status(DATA, STATE, NOW), "Display")


def test_older_times_name_the_day():
    later = datetime(2026, 10, 10, 9, 0)
    assert "Last redrawn Thu 8 Oct 07:58, 2 days ago" in text_of(render_status(DATA, STATE, later))
    next_year = datetime(2027, 1, 2, 9, 0)
    assert "Last redrawn Thu 8 Oct 2026 07:58, 86 days ago" in text_of(render_status(DATA, STATE, next_year))


def test_bus_positions_are_shown_when_the_board_tracks_them():
    state = {**STATE, "vehicles": {
        "last_success": "2026-10-08T08:00:30",
        "last_error": {"at": "2026-10-08T08:01:00", "message": "RuntimeError: timed out"},
    }}
    positions = section(render_status(DATA, state, NOW), "Bus positions")
    assert "Last update 08:00, just now" in positions
    assert "Problem: 08:01, just now: RuntimeError: timed out" in positions
    assert "<h3>Bus positions</h3>" not in render_status(DATA, STATE, NOW)


def test_keys_left_in_saved_messages_are_never_shown():
    state = {"sources": {"bus": {"last_error": {
        "at": "2026-10-08T08:00:00", "message": "GET /x.json?app_id=myid&app_key=tapi-secret-456 refused",
    }}}}
    page = render_status(DATA, state, NOW)
    assert "tapi-secret-456" not in page and "myid" not in page
    assert "Problem:" in page


def test_a_bank_holiday_uses_sunday_times():
    holiday_morning = datetime(2026, 8, 31, 8, 1)
    page = render_status(DATA, STATE, holiday_morning, frozenset({date(2026, 8, 31)}))
    assert "Today is a bank holiday, so Sunday’s checking times apply." in page
    assert "Checking now No, outside its checking times" in section(page, "Trains")
    ignored = render_status({**DATA, "bank_holidays": False}, STATE, holiday_morning, frozenset({date(2026, 8, 31)}))
    assert "bank holiday" not in ignored
    assert "Checking now Yes" in section(ignored, "Trains")


def test_no_state_yet_is_said_plainly():
    page = render_status(DATA, {}, NOW)
    assert "No status yet" in page and "<h3>" not in page


def test_an_unreadable_state_file_is_a_problem():
    page = render_status(DATA, None, NOW)
    assert "Problem:" in page and "can’t be read" in page


@pytest.mark.parametrize("state", [
    {"sources": {"train": "nonsense", "bus": {"last_success": 5, "last_error": "x"}}},
    {"sources": {"train": {"last_success": "yesterday", "last_error": {"at": [], "message": {}}}}},
    {"budgets": {"bus": {"day": "2026-10-08", "used": "lots"}}, "last_render": {"at": None}},
    {"vehicles": {"last_success": "2026-13-45T99:00"}},
])
def test_hand_edited_nonsense_in_the_state_does_not_break_the_panel(state):
    page = render_status(DATA, state, NOW)
    assert "<h2" in page and "Never" in page


def test_unreadable_checking_times_are_said_plainly():
    data = {**DATA, "train": {"windows": ["mornings"]}}
    assert "Can’t tell" in section(render_status(data, STATE, NOW), "Trains")


def test_a_source_s_note_is_shown_as_a_problem():
    state = {**STATE, "sources": {**STATE["sources"], "bus": {
        **STATE["sources"]["bus"], "note": "From TransportAPI: <the> bus timetable has run out"}}}
    page = render_status(DATA, state, NOW)
    assert "Note Problem: From TransportAPI: &lt;the&gt; bus timetable has run out" in section(page, "Buses")
    assert "<the>" not in page

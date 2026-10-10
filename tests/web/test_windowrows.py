"""Time windows as rows of days and times: checking times and quiet hours."""

import html
import re

import pytest

from departureboard.core.polling import parse_window
from departureboard.settings import schema
from departureboard.web.windowrows import anchor, days_spec, read, render

FIELDS = {f.name: f for f in schema.FIELDS}
TRAINS = FIELDS["train_windows"]
QUIET = FIELDS["quiet_windows"]


def submitted(page):
    """What a browser sends for the rows as drawn: every text box, the ticked boxes, the hidden values."""
    sent = {name: html.unescape(value)
            for name, value in re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="text" value="([^"]*)"', page)}
    sent |= dict.fromkeys(re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="checkbox" value="on" checked', page),
                          "on")
    sent |= {name: html.unescape(value)
             for name, value in re.findall(r'<input type="hidden" name="([\w-]+)" value="([^"]*)"', page)}
    return sent


def drawn(f, saved, errors=None, sent=None):
    return render(f, {f.name: saved, **(sent or {})}, errors or {})


def legends(page):
    return re.findall(r"<legend>([^<]+)</legend>", page)


# Every window parse_window takes shows as rows and comes back the same


@pytest.mark.parametrize("saved", [
    "Mon-Fri 06:30-09:30",
    "Sat 10:00-12:00",
    "Sat,Sun 10:00-12:00",
    "Mon,Wed,Fri 07:00-08:00",
    "Fri-Mon 22:00-01:00",  # wraps round the week, and runs past midnight
    "Mon-Sun 22:00-02:00",
    "mon-fri 06:30-09:30",
    "Sun,Mon-Wed 05:00-06:00",
    "Mon-Fri 06:30-09:30\nSat,Sun 09:00-11:00",
])
def test_every_saved_window_round_trips_through_its_rows(saved):
    sent = submitted(drawn(TRAINS, saved))
    # Unchanged, each window keeps the spelling it was saved with.
    assert read(TRAINS.name, sent, saved) == (saved, {})
    # Read afresh, with no saved spelling to keep, each row is the same window.
    joined, errors = read(TRAINS.name, sent, "")
    assert errors == {}
    assert [parse_window(w) for w in joined.splitlines()] == [parse_window(w) for w in saved.splitlines()]


def test_a_row_ticks_its_days_and_shows_its_times():
    sent = submitted(drawn(TRAINS, "Fri-Mon 22:00-01:00"))
    ticked = {name for name, value in sent.items() if value == "on"}
    assert ticked == {"train_windows-1-mon", "train_windows-1-fri", "train_windows-1-sat", "train_windows-1-sun"}
    assert (sent["train_windows-1-from"], sent["train_windows-1-to"]) == ("22:00", "01:00")


@pytest.mark.parametrize("days, spec", [
    ({0}, "Mon"),
    ({0, 1}, "Mon,Tue"),
    ({5, 6}, "Sat,Sun"),
    ({0, 1, 2, 3, 4}, "Mon-Fri"),
    (set(range(7)), "Mon-Sun"),
    ({0, 2, 4}, "Mon,Wed,Fri"),
    ({0, 4, 5, 6}, "Mon,Fri-Sun"),
    ({0, 1, 2, 4, 5}, "Mon-Wed,Fri,Sat"),
])
def test_days_in_a_row_are_run_together(days, spec):
    assert days_spec(frozenset(days)) == spec
    assert parse_window(f"{spec} 06:00-07:00").days == frozenset(days)


def test_a_new_row_is_saved_as_a_window_parse_window_reads():
    sent = submitted(drawn(TRAINS, ""))
    sent |= {f"train_windows-1-{day}": "on" for day in ("mon", "tue", "wed", "thu", "fri")}
    sent |= {"train_windows-1-from": "6:30", "train_windows-1-to": "0930"}  # said loosely, saved exactly
    sent |= {"train_windows-2-sat": "on", "train_windows-2-sun": "on",
             "train_windows-2-from": " 10.00 ", "train_windows-2-to": "12:00"}
    assert read(TRAINS.name, sent, "") == ("Mon-Fri 06:30-09:30\nSat,Sun 10:00-12:00", {})


def test_a_row_changed_is_saved_in_the_rows_own_words():
    saved = "Sat,Sun 10:00-12:00"
    sent = submitted(drawn(TRAINS, saved)) | {"train_windows-1-to": "13:00"}
    assert read(TRAINS.name, sent, saved) == ("Sat,Sun 10:00-13:00", {})


def test_a_to_before_its_from_runs_past_midnight():
    sent = {"train_windows-1-fri": "on", "train_windows-1-from": "22:00", "train_windows-1-to": "02:00"}
    joined, errors = read(TRAINS.name, sent, "")
    assert (joined, errors) == ("Fri 22:00-02:00", {})
    window = parse_window(joined)
    assert window.start > window.end


# Spare rows, and leaving a row empty


def test_two_spare_rows_follow_the_saved_ones_and_empty_rows_are_left_out():
    saved = "Mon-Fri 06:30-09:30"
    page = drawn(TRAINS, saved)
    assert [legend for legend in legends(page) if legend != "Days"] == [
        "Checking time 1", "Checking time 2", "Checking time 3"]
    assert "Leave a row empty to remove it." in page
    sent = submitted(page)
    assert (sent["train_windows-3-from"], sent["train_windows-3-to"]) == ("", "")
    assert read(TRAINS.name, sent, saved) == (saved, {})
    emptied = {name: value for name, value in sent.items() if not name.startswith("train_windows-1-")}
    emptied |= {"train_windows-1-from": " ", "train_windows-1-to": ""}
    assert read(TRAINS.name, emptied, saved) == ("", {})


def test_a_form_sent_without_the_rows_leaves_the_windows_as_they_are():
    # An older page, or a form whose section wasn't drawn: no rows at all is no change.
    saved = "Mon-Fri 06:30-09:30\nSat 10:00-12:00"
    assert read(TRAINS.name, {"title": "Home"}, saved) == (saved, {})


# Mistakes, by the part of the row that is wrong


@pytest.mark.parametrize("row, part, message", [
    ({"from": "06:30", "to": "09:30"}, "days", "Tick at least one day"),
    ({"mon": "on", "from": "", "to": "09:30"}, "from", "time to start"),
    ({"mon": "on", "from": "06:30", "to": ""}, "to", "time to end"),
    ({"mon": "on", "from": "25:00", "to": "09:30"}, "from", "24-hour"),
    ({"mon": "on", "from": "06:30", "to": "9:75"}, "to", "24-hour"),
    ({"mon": "on", "from": "half six", "to": "09:30"}, "from", "24-hour"),
    ({"mon": "on", "from": "06:30:00", "to": "09:30"}, "from", "24-hour"),
    ({"mon": "yes", "from": "06:30", "to": "09:30"}, "days", "Tick"),
])
def test_a_row_with_a_mistake_is_refused_by_the_part_that_is_wrong(row, part, message):
    sent = {"train_windows-1-from": "", "train_windows-1-to": ""}
    sent |= {f"train_windows-1-{name}": value for name, value in row.items()}
    joined, errors = read(TRAINS.name, sent, "")
    assert list(errors) == [f"train_windows-1-{part}"] and message in errors[f"train_windows-1-{part}"]


def test_each_row_s_mistakes_are_its_own():
    sent = {"train_windows-1-mon": "on", "train_windows-1-from": "06:30", "train_windows-1-to": "09:30",
            "train_windows-2-from": "07:00", "train_windows-2-to": "08:00",
            "train_windows-3-sat": "on", "train_windows-3-from": "7", "train_windows-3-to": ""}
    _, errors = read(TRAINS.name, sent, "")
    assert set(errors) == {"train_windows-2-days", "train_windows-3-from", "train_windows-3-to"}


def test_a_mistake_is_shown_by_its_part_and_linked_to_a_control_there():
    sent = {"train_windows-1-mon": "on", "train_windows-1-from": "6:3", "train_windows-1-to": "09:30",
            "train_windows-2-from": "07:00", "train_windows-2-to": "08:00"}
    _, errors = read(TRAINS.name, sent, "")
    page = drawn(TRAINS, "Mon-Fri 06:30-09:30", errors, sent)
    for key in errors:
        assert f'id="{anchor(TRAINS.name, key)}"' in page
    assert anchor(TRAINS.name, "train_windows-2-days") == "train_windows-2-mon"
    assert anchor(TRAINS.name, "train_windows-1-from") == "train_windows-1-from"
    assert re.search(r'<input id="train_windows-1-from" [^>]*aria-describedby="train_windows-times '
                     r'train_windows-1-from-error" aria-invalid="true">', page)
    assert '<p class="error" id="train_windows-1-from-error"><span class="tag">Error:</span> Enter a 24-hour' in page
    assert ('<fieldset class="days has-error" aria-describedby="train_windows-2-days-error"><legend>Days</legend>'
            in page)
    # Sent back, the rows show what was typed, not what was saved.
    assert submitted(page) == sent


def test_with_no_window_at_all_the_error_links_to_the_first_row():
    page = drawn(TRAINS, "", {TRAINS.name: "Add at least one checking time"}, {"train_windows-1-from": ""})
    assert anchor(TRAINS.name, TRAINS.name) == "train_windows-1-mon"
    assert '<p class="error" id="train_windows-error"><span class="tag">Error:</span> Add at least one' in page
    assert 'aria-describedby="train_windows-error"><legend>Days</legend>' in page


# A saved window that can't be shown as days and times


def test_a_saved_window_that_cant_be_read_is_shown_as_it_is_and_must_be_removed():
    saved = "Mon-Fri 06:30-09:30\nmornings <b>"
    page = drawn(TRAINS, saved)
    assert "<code>mornings &lt;b&gt;</code>" in page and "<b>" not in page
    sent = submitted(page)
    assert sent["train_windows-2-text"] == "mornings <b>"
    joined, errors = read(TRAINS.name, sent, saved)
    assert list(errors) == ["train_windows-2-remove"] and "Remove" in errors["train_windows-2-remove"]
    assert read(TRAINS.name, sent | {"train_windows-2-remove": "on"}, saved) == ("Mon-Fri 06:30-09:30", {})


def test_a_hidden_window_sent_back_is_checked_like_any_other():
    # One row is one window: a line break can't smuggle in another.
    sent = {"train_windows-1-text": "Mon-Fri 06:30-09:30\r\nSun 01:00-02:00"}
    _, errors = read(TRAINS.name, sent, "")
    assert list(errors) == ["train_windows-1-remove"]


# Accessible names and groups


def test_every_control_has_a_label_and_each_row_and_its_days_a_legend():
    page = drawn(TRAINS, "Mon-Fri 06:30-09:30\nmornings")
    ids = re.findall(r'<input id="([\w-]+)"', page)
    assert len(ids) == len(set(ids)) and set(ids) == set(re.findall(r'<label for="([\w-]+)">', page))
    # Rows 1, 3 and 4 have days; row 2 is the window shown as it was saved.
    assert legends(page) == ["Checking time 1", "Days", "Checking time 2", "Checking time 3", "Days",
                             "Checking time 4", "Days"]
    assert page.count("<fieldset") == page.count("<legend>") == page.count("</fieldset>") == 7
    assert '<label for="train_windows-1-mon">Mon</label>' in page and '<label for="train_windows-4-sun">Sun</label>' in page
    assert '<label for="train_windows-1-from">From</label>' in page and '<label for="train_windows-1-to">To</label>' in page
    assert '<p class="hint" id="train_windows-times">' in page and "24-hour, like 06:30" in page
    assert page.count('aria-describedby="train_windows-times"') == 6  # every time box: 3 rows, From and To
    assert re.search(r'<input id="train_windows-1-from" name="train_windows-1-from" type="text" value="06:30" '
                     r'class="w-5" autocomplete="off" spellcheck="false"', page)
    assert "inputmode" not in page  # a numeric keypad has no colon


def test_the_days_sit_side_by_side_each_a_44px_target_on_a_touch_screen():
    from importlib.resources import files

    css = files("departureboard.web").joinpath("web.css").read_text()
    assert re.search(r"\.day-boxes \{[^}]*display: flex;[^}]*flex-wrap: wrap", css)
    coarse = css[css.index("@media (pointer: coarse)"):]
    assert re.search(r"\.day \{ min-height: 2\.75rem;", coarse)


def test_the_label_heads_the_rows_with_its_hint_then_how_to_write_times():
    page = drawn(TRAINS, "")
    assert page.startswith(f'<div class="field windows"><h3>When to check</h3>'
                           f'<p class="hint" id="train_windows-hint">{html.escape(TRAINS.hint)}</p>'
                           '<p class="hint" id="train_windows-times">')


def test_quiet_hours_rows_are_called_so_and_may_all_be_left_empty():
    page = drawn(QUIET, "")
    assert legends(page) == ["Quiet hours 1", "Days", "Quiet hours 2", "Days"]
    assert read(QUIET.name, submitted(page), "Mon-Sun 01:00-05:00") == ("", {})

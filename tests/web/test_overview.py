"""The settings' Overview, drawn from fake snapshots, board states and stores."""

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from departureboard.timetable import build as timetable
from departureboard.timetable.build import FORMAT
from departureboard.timetable.model import Schedule, StopPosition
from departureboard.web import overview
from departureboard.web.overview import KeyTest

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 8, 8, 1)  # a Thursday, local time
AT = "2026-10-08T07:58:00+01:00"  # 07:58 in London, as the snapshot says it


def block(last_success=AT, stale=False, error=None, note=None):
    return {"last_success": last_success, "stale": stale, "error": error, "note": note}


def snapshot(**changes):
    found = {
        "quiet": False,
        "trains": {"status": block(), "positions": block()},
        "buses": {"status": block(), "positions": block(),
                  "timetable": {"state": "ready", "built": "2026-10-08T03:10:00+01:00"}},
        "weather": {"status": block()},
        "disruptions": {"status": {"bus": block(), "train": block()}},
    }
    found.update(changes)
    return found


def text_of(page):
    """The words a screen reader would read, tags dropped."""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))).strip()


def feed(page, name):
    """One feed's words, by its name."""
    for item in re.findall(r'<li class="feed[^"]*">(.*?)</li>', page):
        if f"<strong>{name}</strong>" in item:
            return text_of(item)
    raise AssertionError(f"no feed {name}")


def health(feeds, problems=0):
    return overview.render_health([("On the dashboard", feeds)], NOW, "/settings", problems)


# Health


def test_all_working_says_so_as_of_now_with_a_way_to_reload():
    page = health(overview.dashboard_feeds(snapshot(), NOW, LONDON))
    line = re.search(r'<p class="health-line">(.*?)</p>', page).group(1)
    assert text_of(line) == "All working As of 08:01. Reload"
    assert 'class="pill pill-ok"' in line and '<a href="/settings">Reload</a>' in line
    names = re.findall(r"<strong>(.*?)</strong>", page)
    assert names == ["Trains", "Where each train is", "Bus times", "Live bus positions", "Weather",
                     "Engineering works", "Bus disruptions"]
    assert feed(page, "Trains") == "Trains Working Last update 07:58, 3 min ago Checking now Yes"
    assert "Timetable built 03:10, 4 h 51 min ago" in feed(page, "Bus times")
    assert "What it means" not in page and "<h3>" not in page  # one group needs no heading


def test_while_all_is_working_each_feed_waits_behind_a_disclosure():
    page = health(overview.dashboard_feeds(snapshot(), NOW, LONDON))
    folded = re.search(r'<details class="feeds-more"><summary>Show each feed</summary>(.*?)</details>', page, re.S)
    assert folded and '<ul class="feeds">' in folded.group(1)
    assert page.index('class="health-line"') < page.index("<details")
    assert "<details open" not in page


@pytest.mark.parametrize("problems, changes", [
    (0, {"weather": {"status": block(error="HTTP Error 503")}}),  # a feed's own problem
    (1, {}),  # a problem found elsewhere: the dashboard isn't running, say
])
def test_with_a_problem_each_feed_shows_at_once(problems, changes):
    page = health(overview.dashboard_feeds(snapshot(**changes), NOW, LONDON), problems)
    assert "problem" in text_of(re.search(r'<p class="health-line">(.*?)</p>', page).group(1))
    assert "<details" not in page and "Show each feed" not in page
    assert '<ul class="feeds">' in page


def test_a_note_goes_under_the_health_line_as_words():
    page = overview.render_health([("On the board", [])], NOW, "/settings", note="Today is <a> bank holiday.")
    assert '<p class="hint">Today is &lt;a&gt; bank holiday.</p>' in page
    assert "<p class=\"hint\">" not in overview.render_health([], NOW, "/settings")


def test_a_stale_feed_is_a_problem_saying_what_it_means():
    page = health(overview.dashboard_feeds(snapshot(weather={"status": block(stale=True)}), NOW, LONDON))
    assert "1 problem" in text_of(re.search(r'<p class="health-line">(.*?)</p>', page).group(1))
    assert 'pill-bad' in page
    assert feed(page, "Weather") == ("Weather Problem Last update 07:58, 3 min ago Checking now Yes "
                                     "What it means No rain warning, and the forecast may be out of date.")


def test_an_error_is_said_in_safe_words_and_escaped():
    error = "HTTP Error 403 at https://api.example/x?api_key=s3cr3tvalue <b>refused</b>"
    page = health(overview.dashboard_feeds(snapshot(trains={"status": block(stale=True, error=error)}), NOW, LONDON))
    words = feed(page, "Trains")
    assert "Last error HTTP Error 403 at https://api.example/x?…" in words
    assert "s3cr3tvalue" not in page and "<b>" not in page
    assert "What it means The dashboard's train times may be out of date." in words
    assert "2 problems" not in page and "1 problem" in page


def test_problems_found_elsewhere_count_too():
    page = health(overview.dashboard_feeds(snapshot(weather={"status": block(stale=True)}), NOW, LONDON), 1)
    assert "2 problems" in page


def test_in_quiet_hours_the_feeds_rest_and_all_is_still_working():
    page = health(overview.dashboard_feeds(snapshot(quiet=True), NOW, LONDON))
    assert "All working" in page
    assert feed(page, "Trains") == ("Trains Resting Last update 07:58, 3 min ago "
                                    "Checking now No, it rests in its quiet hours unless the dashboard is open")


def test_stopped_updates_are_a_problem():
    page = health(overview.dashboard_feeds(snapshot(), NOW, LONDON, updating=False))
    assert "1 problem" in page
    assert "Last error The dashboard's checks have stopped" in feed(page, "Dashboard updates")


def test_a_feed_not_heard_from_yet_is_waiting_not_a_problem():
    page = health(overview.dashboard_feeds(snapshot(weather={"status": block(last_success=None)}), NOW, LONDON))
    assert feed(page, "Weather").startswith("Weather Waiting Last update Never")
    assert "All working" in page


def test_feeds_not_set_up_are_left_out():
    page = health(overview.dashboard_feeds(snapshot(trains={"status": block(), "positions": None},
                                                    weather=None, disruptions=None), NOW, LONDON))
    assert re.findall(r"<strong>(.*?)</strong>", page) == ["Trains", "Bus times", "Live bus positions"]


def test_live_positions_failing_is_their_own_problem():
    note = "Timetabled times only; live positions failed: down"
    buses = snapshot()["buses"] | {"status": block(note=note), "positions": block(stale=True, error=note)}
    page = health(overview.dashboard_feeds(snapshot(buses=buses), NOW, LONDON))
    assert "Note" not in feed(page, "Bus times") and "Working" in feed(page, "Bus times")
    positions = feed(page, "Live bus positions")
    assert "Problem" in positions and f"Last error {note}" in positions
    assert "without live positions" in positions


@pytest.mark.parametrize("state, note, said, pill", [
    ("missing", "There is no bus timetable yet", "Last error There is no bus timetable yet", "Problem"),
    ("building", "Building the bus timetable; this takes a few minutes",
     "Note Building the bus timetable; this takes a few minutes", "Waiting"),
])
def test_the_bus_timetable_missing_is_a_problem_and_building_it_is_waiting(state, note, said, pill):
    buses = {"status": block(last_success=None, note=note), "positions": block(last_success=None),
             "timetable": {"state": state, "built": None}}
    words = feed(health(overview.dashboard_feeds(snapshot(buses=buses), NOW, LONDON)), "Bus times")
    assert said in words and f"Bus times {pill}" in words


def test_buses_the_dashboard_doesnt_show_are_said_plainly_not_as_a_problem():
    note = "The board's buses come from TransportAPI, which the dashboard doesn't show"
    buses = {"status": block(last_success=None, note=note), "positions": None,
             "timetable": {"state": "missing", "built": None}}
    page = health(overview.dashboard_feeds(snapshot(buses=buses), NOW, LONDON))
    assert feed(page, "Buses") == f"Buses Not shown Last update Never Checking now No Note {note}"
    assert "All working" in page


@pytest.mark.parametrize("hostile", [
    {"trains": "nonsense"},
    {"trains": {"status": {"last_success": 5, "stale": "yes", "error": ["x"], "note": {"a": 1}}}},
    {"trains": {"status": {"last_success": "not a time", "error": "x" * 5000}}},
    {"quiet": "yes", "buses": {"timetable": "ready"}, "disruptions": {"status": []}},
])
def test_whatever_the_snapshot_holds_is_checked_as_it_is_read(hostile):
    page = health(overview.dashboard_feeds(snapshot(**hostile), NOW, LONDON))
    assert "<script" not in page and len(page) < 20_000


def test_no_snapshot_means_nothing_checked_yet():
    page = overview.render_health([("On the dashboard", overview.dashboard_feeds(None, NOW, LONDON))], NOW,
                                  "/settings")
    assert "Nothing checked yet" in page and "<ul" not in page


# The board's feeds, on a Pi

DATA = {
    "train": {"crs": "GLO", "windows": ["Mon-Fri 06:30-09:30"], "stale_after_minutes": 6},
    "bus": {"source": "transportapi", "windows": ["Mon-Fri 07:00-07:30"], "stale_after_minutes": 30,
            "daily_budget": 28},
}
STATE = {
    "sources": {
        "train": {
            "last_success": "2026-10-08T07:58:00",
            "last_error": {"at": "2026-10-08T08:00:00", "message": "HTTP Error 503: <b>Service</b> Unavailable"},
        },
        "bus": {"last_success": "2026-10-08T07:24:00"},
    },
    "budgets": {"bus": {"day": "2026-10-08", "used": 12}},
    "last_render": {"at": "2026-10-08T07:58:00"},
}


def board(data=DATA, state=STATE):
    return overview.render_health([("On the board", overview.board_feeds(data, state, NOW))], NOW, "/settings")


def test_the_boards_feeds_and_its_display():
    page = board()
    assert re.findall(r"<strong>(.*?)</strong>", page) == ["Trains", "Buses", "Display"]
    trains = feed(page, "Trains")
    assert "Problem" in trains and "<b>" not in page  # its words, as text
    assert "Last error 08:00, 1 min ago: HTTP Error 503: <b>Service</b> Unavailable" in trains
    assert "What it means The board's train times may be out of date." in trains
    assert feed(page, "Buses") == ("Buses Resting Last update 07:24, 37 min ago Checking now No, outside its "
                                   "checking times TransportAPI lookups today 12 of 28")
    assert feed(page, "Display") == "Display Working Last redrawn 07:58, 3 min ago"
    assert "1 problem" in page


def test_the_board_not_updating_in_its_checking_times_is_a_problem():
    state = {**STATE, "sources": {"train": {"last_success": "2026-10-08T07:40:00"}}}
    trains = feed(board(state=state), "Trains")
    assert "Last error Not updating: nothing new for over 6 min" in trains


def test_every_transportapi_lookup_used_is_a_problem():
    state = {**STATE, "budgets": {"bus": {"day": "2026-10-08", "used": 28}}}
    buses = feed(board(state=state), "Buses")
    assert "Problem" in buses and "Every TransportAPI lookup for today is used" in buses
    assert "after midnight" in buses


def test_a_board_state_that_cant_be_read_or_isnt_there_yet():
    assert "The board's state file can't be read" in feed(board(state=None), "Board")
    assert "Board Waiting" in feed(board(state={}), "Board")


def test_a_bank_holiday_uses_sunday_times_unless_they_are_ignored():
    holiday_morning = datetime(2026, 8, 31, 8, 1)  # a Monday
    holidays = frozenset({holiday_morning.date()})
    state = {"sources": {"train": {"last_success": "2026-08-31T07:58:00"}}}
    trains = overview.board_feeds(DATA, state, holiday_morning, holidays)[0]
    assert trains.checking == "No, outside its checking times"
    ignored = overview.board_feeds({**DATA, "bank_holidays": False}, state, holiday_morning, holidays)[0]
    assert ignored.checking == "Yes"


def test_the_boards_bus_positions_are_a_feed_of_their_own():
    state = {**STATE, "vehicles": {"last_success": "2026-10-08T08:00:30",
                                   "last_error": {"at": "2026-10-08T08:01:00", "message": "RuntimeError: timed out"}}}
    positions = feed(board(state=state), "Live bus positions")
    assert "Last update 08:00, just now" in positions
    assert "Last error 08:01, just now: RuntimeError: timed out" in positions
    assert "Live bus positions" not in board()


def test_a_board_source_switched_off_is_left_out_whatever_it_saved_before():
    state = {**STATE, "vehicles": {"last_success": "2026-10-08T08:00:30"}}
    names = lambda data: [f.name for f in overview.board_feeds(data, state, NOW)]  # noqa: E731
    assert names({**DATA, "bus": {**DATA["bus"], "enabled": False}}) == ["Trains", "Display"]
    assert names({**DATA, "train": {**DATA["train"], "enabled": False}}) == ["Buses", "Live bus positions", "Display"]
    # Anything but false is for saving to report; the feed shows.
    assert "Trains" in names({**DATA, "train": {**DATA["train"], "enabled": "no"}})


def test_keys_left_in_the_boards_saved_messages_are_never_shown():
    state = {"sources": {"bus": {"last_error": {
        "at": "2026-10-08T08:00:00", "message": "GET /x.json?app_id=myid&app_key=tapi-secret-456 refused",
    }}}}
    page = board(state=state)
    assert "tapi-secret-456" not in page and "myid" not in page
    assert "Last error" in feed(page, "Buses")


@pytest.mark.parametrize("state", [
    {"sources": {"train": "nonsense", "bus": {"last_success": 5, "last_error": "x"}}},
    {"sources": {"train": {"last_success": "yesterday", "last_error": {"at": [], "message": {}}}}},
    {"budgets": {"bus": {"day": "2026-10-08", "used": "lots"}}, "last_render": {"at": None}},
    {"vehicles": {"last_success": "2026-13-45T99:00"}},
])
def test_hand_edited_nonsense_in_the_boards_state_does_not_break_the_feeds(state):
    page = board(state=state)
    assert "Never" in page and "<strong>Display</strong>" in page


def test_the_boards_unreadable_checking_times_are_said_plainly():
    data = {**DATA, "train": {"windows": ["mornings"]}}
    assert "Can’t tell" in feed(board(data=data), "Trains")


def test_a_board_sources_note_is_shown_as_words():
    state = {"sources": {"bus": {"note": "From TransportAPI: <the> bus timetable has run out"}}}
    page = board(state=state)
    assert "Note From TransportAPI: <the> bus timetable has run out" in feed(page, "Buses")
    assert "<the>" not in page


def test_both_views_are_grouped_under_their_names():
    page = overview.render_health([("On the board", overview.board_feeds(DATA, STATE, NOW)),
                                   ("On the dashboard", overview.dashboard_feeds(snapshot(), NOW, LONDON))],
                                  NOW, "/settings")
    assert page.index("<h3>On the board</h3>") < page.index("<h3>On the dashboard</h3>")



# The network guard (web/netguard.py), on a Pi


@pytest.mark.parametrize("found, pill, said", [
    ("on", "pill-ok", "Network guard On Checked When the web server started "
                      "What it means This server can't connect out to the internet."),
    ("off", "pill-bad", "Network guard Off Checked When the web server started "
                        "What to do On the Pi, restart it: sudo systemctl restart departureboard-web. If it stays "
                        "off, sudo journalctl -u departureboard-web says why. "
                        "What it means The server still refuses visitors outside your home network itself; "
                        "nothing stops it connecting out."),
    ("unknown", "pill-neutral", "Network guard Can't tell Checked When the web server started "
                                "What it means There was no route to the internet to check with, so whether "
                                "anything stops this server connecting out can't be told."),
])
def test_the_network_guard_says_whether_the_server_can_connect_out(found, pill, said):
    page = overview.render_health([("On this device", [overview.guard_feed(found)])], NOW, "/settings")
    assert feed(page, "Network guard") == said
    item = re.search(r'<li class="feed[^"]*">(.*?)</li>', page).group(1)
    assert f'class="pill {pill}"' in item
    assert "Last update" not in item and "Checking now" not in item  # a check at start, not a feed


@pytest.mark.parametrize("found, line, folded", [
    ("on", "All working", True),
    ("off", "1 problem", False),  # nothing stops it connecting out: a problem, shown at once
    ("unknown", "All working", True),  # not a problem in itself
])
def test_the_guard_off_is_a_problem_and_cant_tell_is_not(found, line, folded):
    page = overview.render_health([("On the dashboard", overview.dashboard_feeds(snapshot(), NOW, LONDON)),
                                   ("On this device", [overview.guard_feed(found)])], NOW, "/settings")
    assert line in text_of(re.search(r'<p class="health-line">(.*?)</p>', page).group(1))
    assert ("<details" in page) is folded
    assert page.index("<h3>On the dashboard</h3>") < page.index("<h3>On this device</h3>")


# Your place

def place(tmp_path, data, now=NOW):
    return overview.render_place(data, tmp_path, now, LONDON)


def save_timetable(folder, built=datetime(2026, 10, 8, 3, 10), stops=None, operators=("BNML",)):
    stops = stops or {"A": "1800GLA0001", "B": "1800GLA0002"}
    positions = {"1800GLA0001": StopPosition("Henry Street", 53.4, -1.9, "Stop A"),
                 "1800GLA0002": StopPosition("<b>Market</b> & Co", 53.4, -1.9, "")}
    timetable.save(folder, Schedule(built, None, stops, (), positions, operators, FORMAT))


def test_the_station_with_its_name_walk_and_filter(tmp_path):
    data = {"train": {"crs": "glo", "walk_minutes": 12, "filter_crs": "MAN"}}
    rows = text_of(place(tmp_path, data))
    assert ("Station Glossop (GLO), 12 min walk; only trains calling at Manchester Piccadilly (MAN)") in rows
    data = {"train": {"crs": "GLO", "enabled": False}}
    assert "Station Glossop (GLO); switched off" in text_of(place(tmp_path, data))
    assert "Station Not set up" in text_of(place(tmp_path, {}))


def test_the_stops_with_their_names_from_the_timetable_escaped(tmp_path):
    save_timetable(tmp_path)
    data = {"bus": {"source": "bods", "operators": ["BNML"], "stops": {"A": "1800GLA0001", "B": "1800GLA0002",
                                                                       "C": "1800GLA0003"},
                    "walk_minutes": {"A": 5, "*": 3}}}
    page = place(tmp_path, data)
    stops = re.search(r'<ul class="stops">(.*?)</ul>', page).group(1)
    assert "<li>A: Henry Street, 5 min walk</li>" in stops
    assert "<li>B: &lt;b&gt;Market&lt;/b&gt; &amp; Co, 3 min walk</li>" in stops
    assert "<li>C: 1800GLA0003, 3 min walk</li>" in stops  # not in the timetable: its code


def test_a_fresh_timetable_is_said_quietly_and_a_stale_one_warns(tmp_path):
    save_timetable(tmp_path)
    data = {"bus": {"source": "bods", "operators": ["BNML"], "stops": {"A": "1800GLA0001", "B": "1800GLA0002"}}}
    rows = place(tmp_path, data)
    assert "Out of date" not in rows and "Bus timetable Saved" in text_of(rows)
    rows = place(tmp_path, data, now=NOW + timedelta(days=8))
    assert "pill-warn" in rows
    assert "Out of date Saved" in text_of(rows) and "Due to be built again: it is a week old." in text_of(rows)
    changed = {"bus": {**data["bus"], "stops": {"A": "1800GLA0001"}}}
    assert "Due to be built again: your stops changed." in text_of(place(tmp_path, changed))


def test_a_timetable_that_cant_be_read_warns(tmp_path):
    (tmp_path / timetable.FILE_NAME).write_text("{}")
    data = {"bus": {"source": "bods", "stops": {"A": "1800GLA0001"}}}
    assert "Due to be built again: it can't be read." in text_of(place(tmp_path, data))


def test_home_and_the_weather(tmp_path):
    data = {"weather": {"lat": 53.44, "lon": -1.95}}
    rows = text_of(place(tmp_path, data))
    assert "Home Set (to about 1 km)" in rows and "Weather On" in rows
    data = {"weather": {"lat": 53.44, "lon": -1.95, "enabled": False}}
    assert "Weather Off" in text_of(place(tmp_path, data))
    assert "Home Not set" in text_of(place(tmp_path, {}))


# Keys

ALL_SET = {"RDM_API_KEY": "rail-key-1234567", "BODS_API_KEY": "bods-key-7654321"}


def keys(data, env, views=("board",), tested=None):
    return overview.render_keys(data, views, env, "/settings/keys", tested)


def test_each_keys_state_in_one_line_never_its_value():
    data = {"train": {"crs": "GLO"}, "bus": {"source": "bods"}}
    page = keys(data, ALL_SET, views=("dashboard",))
    rows = text_of(page)
    assert "Rail Data Marketplace key Set" in rows and "Bus data key Set" in rows
    assert "Rail Data Marketplace key for where each train is Not set Optional: where each train is." in rows
    assert "rail-key" not in page and "bods-key" not in page
    assert '<a href="/settings/keys">Change or test keys</a>' in page


def test_a_key_not_set_says_what_needs_it():
    data = {"train": {"crs": "GLO"}, "bus": {"source": "transportapi"}}
    rows = text_of(keys(data, {}))
    assert "Rail Data Marketplace key Needed for trains" in rows
    assert "TransportAPI app key Needed for the board's buses" in rows
    assert "Bus data key Not set" in rows  # the board's buses come from TransportAPI
    off = {"train": {"crs": "GLO", "enabled": False}}
    assert "Rail Data Marketplace key Not set" in text_of(keys(off, {}))


def test_a_saved_test_says_whether_the_key_works():
    tested = {"RDM_API_KEY": KeyTest("works", datetime(2026, 10, 8, 7, 5)),
              "BODS_API_KEY": KeyTest("wrong_key", datetime(2026, 10, 7, 21, 0))}
    rows = text_of(keys({}, ALL_SET, tested=tested))
    assert "Rail Data Marketplace key Works Tested 8 Oct 07:05" in rows
    assert "Bus data key Refused Tested 7 Oct 21:00" in rows


# Shortcuts and the wall tablet


@pytest.mark.parametrize("host, address", [
    ("glossop.local:8090", "http://glossop.local:8090/?kiosk"),
    ("192.168.1.20:8090", "http://192.168.1.20:8090/?kiosk"),
    ("[fd00::1]:8090", "http://[fd00::1]:8090/?kiosk"),
    ("board", "http://board/?kiosk"),
    ("localhost:8090", None),
    ("127.0.0.1:8090", None),
    ("[::1]:8090", None),
    ('evil"><script>x</script>', None),
    ("a b", None),
    ("", None),
    (None, None),
])
def test_the_wall_tablets_address_is_the_host_used_if_another_device_can_reach_it(host, address):
    assert overview.kiosk_address(host) == address


def test_the_shortcuts_change_all_or_one_part_and_open_the_dashboard(tmp_path):
    data = {"train": {"crs": "GLO"}, "bus": {"stops": {"A": "1800GLA0001"}}}
    page = overview.render_actions(data, "/settings/setup/", True, "glossop.local:8090")
    links = re.findall(r'<a href="([^"]+)">([^<]+)</a>', page)
    assert links == [("/settings/setup/", "Change home, station or stops"),
                     ("/settings/setup/?step=stops", "Change stops only"),
                     ("/settings/setup/?step=station", "Change station only"),
                     ("/", "Open the dashboard")]
    assert "<code>http://glossop.local:8090/?kiosk</code>" in page


def test_without_the_dashboard_there_is_no_link_to_it_or_wall_tablet(tmp_path):
    page = overview.render_actions({}, "/settings/setup/", False, "glossop.local:8090")
    assert re.findall(r'<a href="([^"]+)">', page) == ["/settings/setup/"]
    assert "kiosk" not in page


def test_on_this_computer_itself_the_tablets_address_is_explained_not_made_up():
    page = overview.render_actions({}, "/settings/setup/", True, "localhost:8090")
    assert "localhost" not in page and "<code>/?kiosk</code>" in page

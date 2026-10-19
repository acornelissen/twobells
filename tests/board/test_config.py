from datetime import date, timedelta

import pytest

from tests.paths import ROOT
from twobells.board.config import load_config
from twobells.core.errors import ConfigError
from twobells.sources.bodsbus import BodsBusSource
from twobells.sources.darwin import DarwinSource
from twobells.sources.transportapi import TransportApiBusSource

TEMPLATE = ROOT / "examples/config.example.toml"  # the blank template, with CHANGE_ME placeholders
GLOSSOP = ROOT / "examples" / "glossop.toml"  # the worked example, filled in
ENV = {"RDM_API_KEY": "rdm", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "key"}
# The examples' buses come from the BODS timetable, with TransportAPI standing in.
BODS_ENV = {**ENV, "BODS_API_KEY": "bods"}


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


@pytest.fixture
def example():
    """The worked example: a whole, real board (Glossop's)."""
    return GLOSSOP


def filled_template(tmp_path):
    """The template with its placeholders filled in, as a new user would."""
    text = TEMPLATE.read_text()
    text = (text.replace('crs = "CHANGE_ME"', 'crs = "ABC"').replace('A = "CHANGE_ME"', 'A = "1000ABCD0001"')
            .replace('operators = ["CHANGE_ME"]', 'operators = ["ABCD"]'))
    return write(tmp_path, text)


def in_tmp(tmp_path, example):
    """`example` with its store in tmp_path: no bus timetable there yet."""
    text = example.read_text().replace("/var/lib/twobells/state.json", str(tmp_path / "state.json"))
    return write(tmp_path, text)


@pytest.fixture(params=["template", "glossop"])
def any_example(request, tmp_path):
    return filled_template(tmp_path) if request.param == "template" else GLOSSOP


def test_example_config_loads(any_example):
    settings = load_config(any_example, BODS_ENV)
    names = [c.source.name for c in settings.board.sources]
    assert names == ["train", "bus"]
    assert isinstance(settings.board.sources[0].source, DarwinSource)
    assert isinstance(settings.board.sources[1].source, BodsBusSource)  # the examples lead with BODS
    assert settings.timezone == "Europe/London"
    assert (settings.board.theme.name, settings.accent) == ("sign", "yellow")
    assert settings.bank_holiday_division == "england-and-wales"


def test_template_refuses_to_run_until_its_placeholders_are_filled():
    with pytest.raises(ConfigError, match="CHANGE_ME"):
        load_config(TEMPLATE, ENV)


def test_template_without_a_bus_stop_names_the_placeholder(tmp_path):
    text = TEMPLATE.read_text().replace('crs = "CHANGE_ME"', 'crs = "ABC"')
    with pytest.raises(ConfigError, match="CHANGE_ME"):
        load_config(write(tmp_path, text), ENV)


def _values(data):
    for value in data.values() if isinstance(data, dict) else data:
        if isinstance(value, (dict, list)):
            yield from _values(value)
        else:
            yield value


def test_template_assumes_no_place(tmp_path):
    import tomllib

    settings = load_config(filled_template(tmp_path), BODS_ENV)
    assert settings.board.title == "Departures"
    assert settings.board.weather is None  # no position until one is given
    # Nothing it sets (comments aside) is Glossop's.
    glossop = {"GLO", "MAN", "BNML", "HIPK", "1000DGHS0900", "100000008350", 53.44, -1.95}
    values = set(_values(tomllib.loads(TEMPLATE.read_text())))
    assert not values & glossop
    assert not any("Glossop" in str(v) or "Manchester" in str(v) for v in values)


def test_title_defaults_to_departures(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "ABC"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).board.title == "Departures"


@pytest.mark.parametrize("crs", ["CHANGE_ME", "GL", "GLOS", "G1O"])
def test_station_code_must_be_three_letters(tmp_path, crs):
    path = write(tmp_path, f'[train]\ncrs = "{crs}"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match="3-letter"):
        load_config(path, ENV)


def test_example_transportapi_stand_in_fits_inside_its_budget(any_example):
    # Until there is a timetable TransportAPI stands in, at a hit per stop each check.
    import tomllib

    bus = load_config(any_example, BODS_ENV).board.sources[1]
    stops = len(tomllib.loads(any_example.read_text())["bus"]["stops"])
    policy = bus.fallback_policy
    calls = 0
    for window in policy.windows:
        span = (window.end.hour * 60 + window.end.minute) - (window.start.hour * 60 + window.start.minute)
        calls += span // int(policy.interval.total_seconds() // 60) + 1
    assert calls * stops <= bus.daily_budget


def test_bus_section_is_optional(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, {"RDM_API_KEY": "rdm"})
    assert [c.source.name for c in settings.board.sources] == ["train"]


def test_missing_secret_names_the_variable(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match="RDM_API_KEY"):
        load_config(path, {})


def test_bad_window_is_reported(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["mornings"]\n')
    with pytest.raises(ConfigError, match="mornings"):
        load_config(path, ENV)


def test_example_watches_both_henry_street_stops(tmp_path, example):
    bus = load_config(in_tmp(tmp_path, example), BODS_ENV).board.sources[1]
    assert bus.source.falling_back and bus.source.cost == 2  # TransportAPI asks both, with no timetable yet


def test_bus_without_stops_is_rejected(tmp_path):
    path = write(tmp_path, '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n')
    with pytest.raises(ConfigError, match="stops"):
        load_config(path, ENV)


def test_bus_stop_left_as_placeholder_is_rejected(tmp_path):
    path = write(tmp_path, '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "CHANGE_ME"\n')
    with pytest.raises(ConfigError, match="CHANGE_ME"):
        load_config(path, ENV)


def test_bus_stops_can_be_overridden(tmp_path, example):
    settings = load_config(in_tmp(tmp_path, example), BODS_ENV, bus_stops={"V": "1000DGVS3332"})
    assert settings.board.sources[1].source.cost == 1


def test_loading_only_bus_does_not_need_the_rail_key(example):
    env = {"BODS_API_KEY": "bods"}
    settings = load_config(example, env, only={"bus"})
    assert [c.source.name for c in settings.board.sources] == ["bus"]


def test_minimum_redraw_gap_defaults_to_two_minutes(example):
    assert load_config(example, BODS_ENV).board.min_redraw == timedelta(minutes=2)


def test_minimum_redraw_gap_can_be_set(tmp_path):
    path = write(tmp_path, 'min_redraw_minutes = 3\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).board.min_redraw == timedelta(minutes=3)


def test_old_refresh_every_setting_is_ignored(tmp_path):
    path = write(tmp_path, 'refresh_every_minutes = 10\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    board = load_config(path, ENV).board
    assert not hasattr(board, "refresh_every")


def test_walk_minutes_for_station_and_each_stop(tmp_path):
    path = write(tmp_path, (
        '[train]\ncrs = "GLO"\nwalk_minutes = 7\nwindows = ["Mon-Fri 06:30-09:30"]\n'
        '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n'
        '[bus.stops]\nA = "1000DGHS0900"\nB = "100000008350"\n'
        '[bus.walk_minutes]\nA = 7\nB = 6\n'
    ))
    walk = load_config(path, ENV).board.walk
    assert walk.train == 7
    assert dict(walk.bus) == {"A": 7, "B": 6}


def test_single_bus_walk_time_applies_to_every_stop(tmp_path):
    path = write(tmp_path, (
        '[bus]\nsource = "transportapi"\nwalk_minutes = 5\nwindows = ["Mon-Fri 07:00-08:00"]\n'
        '[bus.stops]\nA = "1000DGHS0900"\n'
    ))
    assert dict(load_config(path, ENV).board.walk.bus) == {"*": 5}


def test_walk_times_are_optional(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    walk = load_config(path, ENV).board.walk
    assert walk.train is None and dict(walk.bus) == {}


def test_example_config_has_walk_times(example):
    walk = load_config(example, BODS_ENV).board.walk
    assert walk.train is not None and set(walk.bus) == {"A", "B"}


def test_theme_defaults_to_the_departure_board_look_in_yellow(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, ENV)
    assert settings.board.theme.name == "sign"
    assert settings.accent == "yellow"


def test_plain_theme_defaults_to_red(tmp_path):
    path = write(tmp_path, 'theme = "plain"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, ENV)
    assert (settings.board.theme.name, settings.accent) == ("plain", "red")


def test_the_tft_look_on_a_screen_defaults_to_yellow(tmp_path):
    path = write(tmp_path, 'theme = "tft"\ndisplay = "hdmi-1920x440"\n[train]\ncrs = "GLO"\n'
                           'windows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, ENV)
    assert (settings.board.theme.name, settings.accent) == ("tft", "yellow")


def test_the_split_flap_look_on_a_screen_defaults_to_yellow(tmp_path):
    path = write(tmp_path, 'theme = "flap"\ndisplay = "hdmi-1280x400"\n[train]\ncrs = "GLO"\n'
                           'windows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, ENV)
    assert (settings.board.theme.name, settings.accent) == ("flap", "yellow")


@pytest.mark.parametrize("display", ["inky", "inky-phat-v1", "waveshare-10in85g"])
@pytest.mark.parametrize("look", ["tft", "flap"])
def test_a_screen_only_look_on_e_paper_is_a_config_error(tmp_path, display, look):
    path = write(tmp_path, f'theme = "{look}"\ndisplay = "{display}"\n[train]\ncrs = "GLO"\n'
                           'windows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match=f"{look} look is for a screen; choose from sign, plain"):
        load_config(path, ENV)


def test_explicit_accent_wins(tmp_path):
    path = write(tmp_path, 'accent = "red"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).accent == "red"


def test_unknown_theme_is_a_config_error(tmp_path):
    path = write(tmp_path, 'theme = "neon"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match="neon"):
        load_config(path, ENV)


def test_display_defaults_to_the_inky(example):
    assert load_config(example, BODS_ENV).display == "inky"


def test_waveshare_display_can_be_chosen(tmp_path):
    path = write(tmp_path, 'display = "waveshare-10in85g"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).display == "waveshare-10in85g"


def test_unknown_display_is_a_config_error(tmp_path):
    path = write(tmp_path, 'display = "crt"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match=r"crt.*inky"):
        load_config(path, ENV)


def _rotate_config(tmp_path, lines):
    return write(tmp_path, lines + '\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')


def test_rotate_defaults_to_upright(example):
    assert load_config(example, BODS_ENV).rotate == 0


def test_old_rotate_180_setting_still_turns_the_board(tmp_path):
    assert load_config(_rotate_config(tmp_path, "rotate_180 = true"), ENV).rotate == 180


def test_screen_can_turn_a_quarter(tmp_path):
    path = _rotate_config(tmp_path, 'display = "hdmi-1280x400"\nrotate = 90')
    settings = load_config(path, ENV)
    assert (settings.display, settings.rotate) == ("hdmi-1280x400", 90)


def test_epaper_cannot_turn_a_quarter(tmp_path):
    with pytest.raises(ConfigError, match="0, 180"):
        load_config(_rotate_config(tmp_path, "rotate = 90"), ENV)


ROUTE = '''
display = "hdmi-1280x400"
rotate = 90
[bus]
source = "transportapi"
windows = ["Mon-Fri 07:00-08:30"]
[bus.stops]
A = "1000DGHS0900"
[bus.routes.341]
stop = "A"
stops = [
  { name = "Arundel Street", lat = 53.443519, lon = -1.95326 },
  { name = "Henry Street", lat = 53.44384, lon = -1.948999 },
]
'''


def test_route_strip_is_loaded_for_a_screen(tmp_path):
    tracker = load_config(write(tmp_path, ROUTE), BODS_ENV).board.tracker
    strip = tracker.routes["341"]
    assert strip.stop == "A"
    assert [s.name for s in strip.stops] == ["Arundel Street", "Henry Street"]
    assert tracker.policy.interval == timedelta(seconds=30)
    assert tracker.policy.windows == load_config(write(tmp_path, ROUTE), BODS_ENV).board.sources[0].policy.windows


def test_route_strip_needs_the_bods_key(tmp_path):
    with pytest.raises(ConfigError, match="BODS_API_KEY"):
        load_config(write(tmp_path, ROUTE), ENV)


def test_epaper_ignores_routes_and_needs_no_bods_key(tmp_path):
    path = write(tmp_path, ROUTE.replace('display = "hdmi-1280x400"\nrotate = 90', 'display = "inky"'))
    assert load_config(path, ENV).board.tracker is None


def test_display_override_turns_the_route_page_on(tmp_path):
    path = write(tmp_path, ROUTE.replace('display = "hdmi-1280x400"\nrotate = 90', 'display = "inky"'))
    assert load_config(path, BODS_ENV, display="hdmi-1280x400").board.tracker is not None


def test_a_screen_shows_where_trains_and_buses_are(example):
    positions = load_config(example, BODS_ENV, display="hdmi-1280x400").board.positions
    assert positions is not None and positions.details is None  # buses only, without a details key


def test_a_screen_with_a_details_key_asks_where_the_trains_are(example):
    env = {**BODS_ENV, "RDM_DETAILS_API_KEY": "details"}
    assert load_config(example, env, display="hdmi-1280x400").board.positions.details is not None


def test_e_paper_never_asks_where_anything_is(example):
    env = {**BODS_ENV, "RDM_DETAILS_API_KEY": "details"}
    assert load_config(example, env, display="inky").board.positions is None


def trains_source(settings):
    [source] = [c.source for c in settings.board.sources if isinstance(c.source, DarwinSource)]
    return source


@pytest.mark.parametrize("display", ["hdmi-1280x400", "hdmi-1920x440"])
def test_a_screen_asks_for_the_board_with_calling_points(example, display):
    # For the first train's "Calling at" line, which a screen scrolls.
    assert trains_source(load_config(example, BODS_ENV, display=display)).with_details is True


@pytest.mark.parametrize("display", ["inky", "waveshare-10in85g"])
def test_e_paper_asks_for_the_plain_board(example, display):
    assert trains_source(load_config(example, BODS_ENV, display=display)).with_details is False


def test_a_probe_asks_where_nothing_is(example):
    env = {**BODS_ENV, "RDM_DETAILS_API_KEY": "details"}
    assert load_config(example, env, only={"train"}, display="hdmi-1280x400").board.positions is None


def test_a_details_key_that_could_split_a_header_is_refused(example):
    env = {**BODS_ENV, "RDM_DETAILS_API_KEY": "abc\r\nX-Evil: 1"}
    with pytest.raises(ConfigError, match="RDM_DETAILS_API_KEY"):
        load_config(example, env, display="hdmi-1280x400")


def test_route_stop_must_be_a_configured_stop(tmp_path):
    with pytest.raises(ConfigError, match="not one of"):
        load_config(write(tmp_path, ROUTE.replace('stop = "A"', 'stop = "Z"')), BODS_ENV)


def test_route_needs_two_stops(tmp_path):
    one = ROUTE.replace('  { name = "Arundel Street", lat = 53.443519, lon = -1.95326 },\n', "")
    with pytest.raises(ConfigError, match="two stops"):
        load_config(write(tmp_path, one), BODS_ENV)


def test_route_stop_needs_a_real_position(tmp_path):
    bad = ROUTE.replace("lat = 53.443519", "lat = 153.4")
    with pytest.raises(ConfigError, match="impossible"):
        load_config(write(tmp_path, bad), BODS_ENV)


def test_positions_cannot_be_polled_faster_than_bods_updates(tmp_path):
    fast = ROUTE.replace('[bus.stops]', 'position_interval_seconds = 5\n[bus.stops]')
    with pytest.raises(ConfigError, match="at least 10"):
        load_config(write(tmp_path, fast), BODS_ENV)


def _with_state(tmp_path, body):
    return write(tmp_path, f'state_file = "{tmp_path / "state" / "state.json"}"\n' + body)


def test_saved_settings_are_layered_over_config(tmp_path):
    path = _with_state(tmp_path, 'title = "Base"\n[train]\ncrs = "GLO"\nrows = 3\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text('title = "Saved"\n[train]\nrows = 2\n')
    settings = load_config(path, ENV)
    assert settings.board.title == "Saved"
    train = settings.board.sources[0]
    assert train.source.name == "train"
    assert train.policy.windows  # untouched keys of a table survive


def test_overlay_cannot_move_the_state_file(tmp_path):
    from twobells.settings.store import Store

    path = _with_state(tmp_path, "")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text('state_file = "/etc/evil.json"\n')
    assert Store.open(path).data()["state_file"] == str(tmp_path / "state" / "state.json")


def test_saved_settings_cannot_send_the_keys_to_another_address(tmp_path, caplog):
    from twobells.sources.darwin import DEFAULT_BASE_URL as DARWIN_URL
    from twobells.sources.transportapi import DEFAULT_BASE_URL as TAPI_URL

    path = _with_state(tmp_path, TRAIN + BUS)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text(
        'display = "hdmi-1280x400"\n'
        '[train]\nrows = 2\nbase_url = "https://example.invalid/rail"\n'
        '[bus]\nsource = "transportapi"\nbase_url = "https://example.invalid/bus"\n'
        '[dashboard]\nstate_dir = "/example.invalid"\n'
    )
    caplog.set_level("WARNING")
    settings = load_config(path, ENV)
    train, bus = (config.source for config in settings.board.sources)
    assert train._base_url == DARWIN_URL and bus._base_url == TAPI_URL
    assert train._rows == 2  # what the settings site saves still counts
    assert settings.display == "inky"  # set in config.toml only, as the page says
    logged = " ".join(r.getMessage() for r in caplog.records)
    for name in ("[train] base_url", "[bus] base_url", "display", "[dashboard] state_dir"):
        assert name in logged
    assert "example.invalid" not in logged  # the key is named, never its value


def test_addresses_can_still_be_set_in_config_toml(tmp_path):
    path = write(tmp_path, TRAIN + 'base_url = "https://example.test/rail"\n')
    assert load_config(path, ENV).board.sources[0].source._base_url == "https://example.test/rail"


def test_broken_overlay_is_a_config_error(tmp_path):
    path = _with_state(tmp_path, "")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text("title = \n")
    with pytest.raises(ConfigError, match=r"settings\.toml"):
        load_config(path, ENV)


def test_empty_values_saved_by_the_page_mean_none(tmp_path):
    path = write(tmp_path, (
        'accent = ""\n'
        '[train]\ncrs = "GLO"\nfilter_crs = ""\nwalk_minutes = ""\nwindows = ["Mon-Fri 06:30-09:30"]\n'
        '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n[bus.walk_minutes]\n'
    ))
    settings = load_config(path, ENV)
    assert settings.accent == "yellow"
    assert settings.board.walk.train is None and dict(settings.board.walk.bus) == {}
    assert "filterCrs" not in settings.board.sources[0].source._url()


TRAIN = '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
BUS = '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n'


@pytest.mark.parametrize(
    "text, message",
    [
        ('[train]\nwindows = ["Mon-Fri 06:30-09:30"]\n', "crs"),
        ('[train]\ncrs = 5\nwindows = ["Mon-Fri 06:30-09:30"]\n', "crs"),
        ('train = "GLO"\n', r"\[train\]"),
        (TRAIN + 'interval_minutes = "5"\n', "interval_minutes"),
        (TRAIN + "interval_minutes = 0\n", "interval_minutes"),
        (TRAIN + "rows = 0\n", "rows"),
        (TRAIN + 'stale_after_minutes = "6"\n', "stale_after_minutes"),
        (TRAIN + 'walk_minutes = "5"\n', "walk_minutes"),
        (TRAIN + 'filter_crs = ["MAN"]\n', "filter_crs"),
        ('[train]\ncrs = "GLO"\nwindows = "Mon-Fri 06:30-09:30"\n', "windows"),
        ('min_redraw_minutes = "2"\n' + TRAIN, "min_redraw_minutes"),
        ("min_redraw_minutes = 0\n" + TRAIN, "min_redraw_minutes"),
        ('accent = "blue"\n' + TRAIN, "accent"),
        ('title = 5\n' + TRAIN, "title"),
        ('timezone = "Mars/Olympus"\n' + TRAIN, "timezone"),
        ('display = ["inky"]\n' + TRAIN, "display"),
        ("state_file = 5\n" + TRAIN, "state_file"),
        ('[bus]\nsource = "transportapi"\nstops = ["1000DGHS0900"]\nwindows = ["Mon-Fri 07:00-08:00"]\n', "stops"),
        ('[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = 1000\n', "A"),
        ('[bus]\nsource = "transportapi"\nwalk_minutes = "5"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "walk_minutes"),
        (BUS + '[bus.walk_minutes]\nA = "7"\n', "walk_minutes"),
        ('[bus]\nsource = "transportapi"\ndaily_budget = "28"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "daily_budget"),
        ('[bus]\nsource = "transportapi"\ndirections = "Ashton"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "directions"),
        ('display = "hdmi-1280x400"\n' + BUS + '[bus.routes]\n341 = "Glossop"\n', "341"),
        ('display = "hdmi-1280x400"\n[bus]\nsource = "transportapi"\nroutes = "341"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "routes"),
        ('display = "hdmi-1280x400"\n' + BUS + '[bus.routes.341]\nstop = "A"\nstops = "Arundel Street"\n', "341"),
        (ROUTE.replace("lat = 53.443519", f"lat = {'9' * 400}"), "each stop needs"),  # too big for a float
        (ROUTE.replace("lat = 53.443519", "lat = true"), "each stop needs"),
        (ROUTE.replace("lat = 53.443519", 'lat = "53.443519"'), "each stop needs"),
        ("title = \n", "config.toml"),
        ('bank_holidays = "yes"\n' + TRAIN, "bank_holidays"),
        ('scrolling = "off"\n' + TRAIN, "scrolling"),
    ],
)
def test_wrong_types_and_ranges_are_config_errors(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), BODS_ENV)


def test_broken_config_toml_is_a_config_error_when_finding_the_overlay(tmp_path):
    from twobells.settings.store import Store

    with pytest.raises(ConfigError, match=r"config\.toml"):
        Store.open(write(tmp_path, "title = \n"))


def test_no_state_file_means_the_store_s_overlay_not_the_working_directory_s(tmp_path, monkeypatch):
    from twobells.settings.store import Store, platform_folder

    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings.toml").write_text('title = "From cwd"\n')
    path = write(tmp_path, 'title = "Base"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).board.title == "Base"
    assert Store.open(path).settings_file == platform_folder() / "settings.toml"


HOLIDAY = date(2026, 8, 31)


def test_bank_holidays_reach_every_policy_by_default(tmp_path):
    settings = load_config(write(tmp_path, ROUTE + TRAIN), BODS_ENV, holidays=frozenset({HOLIDAY}))
    policies = [c.policy for c in settings.board.sources] + [settings.board.tracker.policy]
    assert [p.holidays for p in policies] == [frozenset({HOLIDAY})] * 3


def test_bank_holidays_can_be_ignored(tmp_path):
    path = write(tmp_path, "bank_holidays = false\n" + TRAIN + BUS)
    settings = load_config(path, ENV, holidays=frozenset({HOLIDAY}))
    assert [c.policy.holidays for c in settings.board.sources] == [frozenset(), frozenset()]


def test_a_screen_scrolls_long_text_unless_scrolling_is_off(tmp_path):
    assert load_config(write(tmp_path, TRAIN), ENV).board.scrolling is True
    assert load_config(write(tmp_path, "scrolling = false\n" + TRAIN), ENV).board.scrolling is False


def test_without_known_holidays_policies_have_none(tmp_path):
    settings = load_config(write(tmp_path, TRAIN), ENV)
    assert settings.board.sources[0].policy.holidays == frozenset()


def test_bank_holiday_division_defaults_to_england_and_wales(tmp_path):
    assert load_config(write(tmp_path, TRAIN), ENV).bank_holiday_division == "england-and-wales"


@pytest.mark.parametrize("division", ["england-and-wales", "scotland", "northern-ireland"])
def test_bank_holiday_division_can_be_set(tmp_path, division):
    path = write(tmp_path, f'bank_holiday_division = "{division}"\n' + TRAIN)
    assert load_config(path, ENV).bank_holiday_division == division


@pytest.mark.parametrize("value", ['"wales"', '"Scotland"', '""', "1", '["scotland"]'])
def test_bank_holiday_division_must_be_one_of_gov_uks(tmp_path, value):
    path = write(tmp_path, f"bank_holiday_division = {value}\n" + TRAIN)
    with pytest.raises(ConfigError, match="bank_holiday_division"):
        load_config(path, ENV)


TRAIN_AND_BUS = """
[train]
crs = "GLO"
windows = ["Mon-Fri 06:30-09:30"]
[bus]
source = "transportapi"
windows = ["Mon-Fri 17:00-18:00"]
[bus.stops]
A = "1000DGHS0900"
"""


def test_weather_is_off_without_a_section(example):
    assert load_config(example, BODS_ENV).board.weather is None


def test_example_weather_section_loads_when_uncommented(tmp_path):
    lines = GLOSSOP.read_text().splitlines()
    start = lines.index("# [weather]")
    lines[start:] = [line.removeprefix("# ") for line in lines[start:]]
    weather = load_config(write(tmp_path, "\n".join(lines)), BODS_ENV).board.weather
    assert weather is not None and weather.threshold == 50
    assert "latitude=53.44" in weather.source.url() and "longitude=-1.95" in weather.source.url()


def test_template_weather_section_loads_when_uncommented_and_filled(tmp_path):
    lines = filled_template(tmp_path).read_text().splitlines()
    start = lines.index("# [weather]")
    lines[start:] = [line.removeprefix("# ").replace("CHANGE_ME", "51.5") for line in lines[start:]]
    weather = load_config(write(tmp_path, "\n".join(lines)), BODS_ENV).board.weather
    assert weather is not None and weather.threshold == 50
    assert "latitude=51.5" in weather.source.url()


def test_weather_polls_every_30_minutes_in_the_train_and_bus_windows(tmp_path):
    path = write(tmp_path, TRAIN_AND_BUS + "[weather]\nlat = 53.44\nlon = -1.95\nthreshold_percent = 70\n")
    settings = load_config(path, ENV)
    weather = settings.board.weather
    assert weather.threshold == 70
    assert weather.policy.interval == timedelta(minutes=30)
    windows = [w for c in settings.board.sources for w in c.policy.windows]
    assert list(weather.policy.windows) == windows and len(windows) == 2
    assert "timezone=Europe%2FLondon" in weather.source.url()


def test_weather_follows_the_boards_timezone(tmp_path):
    path = write(tmp_path, 'timezone = "Europe/Dublin"\n' + TRAIN_AND_BUS + "[weather]\nlat = 53.3\nlon = -6.3\n")
    assert "timezone=Europe%2FDublin" in load_config(path, ENV).board.weather.source.url()


def test_a_cleared_weather_position_turns_it_off(tmp_path):
    path = write(tmp_path, TRAIN_AND_BUS + '[weather]\nlat = ""\nlon = ""\nthreshold_percent = 50\n')
    assert load_config(path, ENV).board.weather is None


@pytest.mark.parametrize(
    "weather",
    [
        "lat = 53.44",  # lon missing
        "lon = -1.95",
        'lat = "53.44"\nlon = -1.95',
        "lat = 91\nlon = -1.95",
        "lat = 53.44\nlon = -181",
        "lat = true\nlon = -1.95",
        "lat = 53.44\nlon = -1.95\nthreshold_percent = 0",
        "lat = 53.44\nlon = -1.95\nthreshold_percent = 101",
        "lat = 53.44\nlon = -1.95\nthreshold_percent = 50.5",
    ],
)
def test_bad_weather_settings_are_config_errors(tmp_path, weather):
    with pytest.raises(ConfigError, match=r"\[weather\]"):
        load_config(write(tmp_path, TRAIN_AND_BUS + "[weather]\n" + weather + "\n"), ENV)


def test_weather_must_be_a_table(tmp_path):
    with pytest.raises(ConfigError, match=r"\[weather\]"):
        load_config(write(tmp_path, 'weather = "rain"\n' + TRAIN_AND_BUS), ENV)


def test_a_probe_of_one_source_builds_no_forecast(tmp_path):
    path = write(tmp_path, TRAIN_AND_BUS + "[weather]\nlat = 53.44\nlon = -1.95\n")
    assert load_config(path, ENV, only={"train"}).board.weather is None


def test_rain_warning_can_be_turned_off_without_losing_the_location(tmp_path):
    base = '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n[weather]\nlat = 53.44\nlon = -1.95\n'
    assert load_config(write(tmp_path, base), ENV).board.weather is not None
    assert load_config(write(tmp_path, base + "enabled = false\n"), ENV).board.weather is None
    with pytest.raises(ConfigError, match="enabled"):
        load_config(write(tmp_path, base + 'enabled = "no"\n'), ENV)


def test_trains_or_buses_switched_off_are_ignored_as_if_absent(tmp_path):
    names = lambda text: [c.source.name for c in load_config(write(tmp_path, text), ENV).board.sources]  # noqa: E731
    assert names(TRAIN_AND_BUS) == ["train", "bus"]
    assert names(TRAIN_AND_BUS.replace("[bus]\nsource = \"transportapi\"\n", "[bus]\nsource = \"transportapi\"\nenabled = false\n")) == ["train"]
    assert names(TRAIN_AND_BUS.replace("[train]\n", "[train]\nenabled = false\n")) == ["bus"]
    assert names(TRAIN_AND_BUS.replace("[train]\n", "[train]\nenabled = true\n")) == ["train", "bus"]
    # Whatever is in a section switched off, placeholders and walk times included, isn't read.
    off = '[train]\nenabled = false\ncrs = "CHANGE_ME"\nwalk_minutes = "far"\n[bus]\nsource = "transportapi"\nenabled = false\nstops = {}\n'
    settings = load_config(write(tmp_path, off), ENV)
    assert settings.board.sources == [] and settings.board.walk.train is None


@pytest.mark.parametrize("section", ["train", "bus", "weather"])
@pytest.mark.parametrize("value", ['"no"', "0", "1", '"false"'])
def test_enabled_must_be_true_or_false(tmp_path, section, value):
    text = TRAIN_AND_BUS + "[weather]\nlat = 53.44\nlon = -1.95\n"
    text = text.replace(f"[{section}]\n", f"[{section}]\nenabled = {value}\n")
    with pytest.raises(ConfigError, match=rf"\[{section}\] enabled must be true or false, not "):
        load_config(write(tmp_path, text), ENV)


def test_text_size_sets_the_plain_look_and_is_checked(tmp_path):
    base = 'theme = "plain"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
    assert load_config(write(tmp_path, base), ENV).board.theme.text_size == 11
    assert load_config(write(tmp_path, "text_size = 12\n" + base), ENV).board.theme.text_size == 12
    for bad in ("text_size = 30\n", 'text_size = "big"\n'):
        with pytest.raises(ConfigError, match="text_size"):
            load_config(write(tmp_path, bad + base), ENV)


def test_the_clock_page_is_off_unless_switched_on_and_is_checked(tmp_path):
    # The older setting, from before [board] show: read while there is no list.
    base = 'display = "hdmi-1280x400"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
    assert "clock_page" not in load_config(write(tmp_path, base), ENV).board.show
    assert "clock_page" in load_config(write(tmp_path, "clock_page = true\n" + base), ENV).board.show
    for bad in ('clock_page = "yes"\n', "clock_page = 1\n"):
        with pytest.raises(ConfigError, match="clock_page must be true or false"):
            load_config(write(tmp_path, bad + base), ENV)


BOARD_BASE = 'display = "hdmi-1280x400"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'


def test_without_a_show_list_the_board_shows_everything_but_the_clock_page(tmp_path):
    shown = load_config(write(tmp_path, BOARD_BASE), ENV).board.show
    assert shown == {"platforms", "where", "disruption", "rain", "last_bus", "bus_page", "calling_at"}


def test_the_board_shows_just_what_its_list_names(tmp_path):
    board = load_config(write(tmp_path, BOARD_BASE + '[board]\nshow = ["platforms", "clock_page"]\n'), ENV).board
    assert board.show == {"platforms", "clock_page"}
    assert load_config(write(tmp_path, BOARD_BASE + "[board]\nshow = []\n"), ENV).board.show == set()


def test_the_board_says_when_to_leave_for_the_modes_its_leave_list_names(tmp_path):
    both = BOARD_BASE + '[metro]\nstops = { T = "9400ZZMAPIC" }\n'
    assert load_config(write(tmp_path, both), ENV).board.leave == ("train", "metro")
    board = load_config(write(tmp_path, both + '[board]\nleave_modes = ["metro", "bus"]\n'), ENV).board
    assert board.leave == ("metro",)  # buses aren't set up
    assert load_config(write(tmp_path, both + "[board]\nleave_modes = []\n"), ENV).board.leave == ()
    with pytest.raises(ConfigError, match="leave_modes"):
        load_config(write(tmp_path, both + '[board]\nleave_modes = ["ferry"]\n'), ENV)

def test_a_speaking_screen_keeps_the_quiet_hours(tmp_path):
    from twobells.core.polling import parse_window

    speaker = '[board]\nshow = ["announce_speaker"]\n'
    # Off unless chosen, and without the speaker the quiet hours aren't read at all.
    assert load_config(write(tmp_path, BOARD_BASE), ENV).board.quiet == ()
    bad = '[dashboard]\nquiet_windows = ["Someday"]\n'
    assert load_config(write(tmp_path, BOARD_BASE + bad), ENV).board.quiet == ()
    # With it: the dashboard's quiet hours, its default without any.
    board = load_config(write(tmp_path, BOARD_BASE + speaker), ENV).board
    assert board.show == {"announce_speaker"}
    assert board.quiet == (parse_window("Mon-Sun 01:00-05:00"),)
    text = BOARD_BASE + speaker + '[dashboard]\nquiet_windows = ["Mon-Sun 22:00-07:00"]\n'
    assert load_config(write(tmp_path, text), ENV).board.quiet == (parse_window("Mon-Sun 22:00-07:00"),)
    with pytest.raises(ConfigError, match="Unrecognised window"):
        load_config(write(tmp_path, BOARD_BASE + speaker + bad), ENV)
    # E-paper never speaks, whatever the list says.
    epaper = BOARD_BASE.replace("hdmi-1280x400", "inky") + speaker
    assert load_config(write(tmp_path, epaper), ENV).board.quiet == ()


def test_a_show_list_wins_over_the_older_clock_page(tmp_path):
    text = "clock_page = true\n" + BOARD_BASE + '[board]\nshow = ["rain"]\n'
    assert load_config(write(tmp_path, text), ENV).board.show == {"rain"}


@pytest.mark.parametrize("board, match", [
    ('show = ["platforms", "weather"]', r"\[board\] show: 'weather' is not something the board shows"),
    ('show = "platforms"', r"\[board\] show must be a list"),
    ("show = [1]", r"\[board\] show must be a list"),
    ('show = ["rain", "rain"]', "more than once"),
    ("rows = 3", r"\[board\] has unknown settings: rows"),
])
def test_a_board_show_list_is_checked(tmp_path, board, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, BOARD_BASE + f"[board]\n{board}\n"), ENV)


def test_board_must_be_a_table(tmp_path):
    with pytest.raises(ConfigError, match=r"\[board\] must be a table"):
        load_config(write(tmp_path, 'board = "everything"\n' + BOARD_BASE), ENV)


def test_panel_colour_is_separate_from_the_accent(tmp_path):
    base = 'display = "inky-phat-v1"\ntheme = "plain"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
    settings = load_config(write(tmp_path, 'panel_colour = "yellow"\n' + base), ENV)
    assert (settings.panel_colour, settings.accent) == ("yellow", "red")
    assert load_config(write(tmp_path, base), ENV).panel_colour == "red"  # defaults to the accent
    with pytest.raises(ConfigError, match="panel_colour"):
        load_config(write(tmp_path, 'panel_colour = "blue"\n' + base), ENV)


BODS_BUS = """
state_file = "{state}"
[bus]
source = "bods"
operators = ["BNML", "HIPK"]
windows = ["Mon-Sun 06:00-23:00"]
[bus.stops]
A = "1000DGHS0900"
"""


def bods_bus(tmp_path, env, extra=""):
    from twobells.sources.bodsbus import BodsBusSource

    path = write(tmp_path, BODS_BUS.format(state=tmp_path / "state" / "state.json") + extra)
    config = load_config(path, env).board.sources[0]
    assert isinstance(config.source, BodsBusSource)
    return config


def test_bods_bus_falls_back_to_transportapi_when_its_keys_are_set(tmp_path):
    config = bods_bus(tmp_path, {**ENV, "BODS_API_KEY": "bods"})
    assert config.source._fallback is not None
    assert config.source._folder == tmp_path / "state"
    assert config.source._operators == ["BNML", "HIPK"]
    # The timetable is checked every 2 minutes; TransportAPI, standing in, every 12.
    assert config.policy.interval == timedelta(minutes=2)
    assert config.fallback_policy.interval == timedelta(minutes=12)
    assert config.daily_budget == 28


def test_the_stand_in_keeps_to_transportapi_s_windows_whatever_the_bus_windows(tmp_path):
    """The bus windows are all day (the Board tab saves them so); TransportAPI,
    only a backup, keeps to the commute, or its 28 hits are gone by mid-morning."""
    from twobells.core.polling import parse_window
    from twobells.settings.schema import BOARD_DEFAULTS

    config = bods_bus(tmp_path, {**ENV, "BODS_API_KEY": "bods"})
    assert config.policy.windows == (parse_window("Mon-Sun 06:00-23:00"),)
    assert config.fallback_policy.windows == tuple(parse_window(w) for w in BOARD_DEFAULTS["bus"]["windows"])


def test_bods_bus_needs_no_transportapi_keys(tmp_path):
    config = bods_bus(tmp_path, {"BODS_API_KEY": "bods"})
    assert config.source._fallback is None and not config.source.falling_back


def test_bods_bus_needs_the_bods_key(tmp_path):
    with pytest.raises(ConfigError, match="BODS_API_KEY"):
        bods_bus(tmp_path, ENV)


def test_bods_bus_intervals_can_be_set(tmp_path):
    path = write(tmp_path, BODS_BUS.format(state=tmp_path / "s.json").replace(
        'source = "bods"', 'source = "bods"\ninterval_minutes = 1\nfallback_interval_minutes = 20'))
    config = load_config(path, {"BODS_API_KEY": "bods"}).board.sources[0]
    assert (config.policy.interval, config.fallback_policy.interval) == (timedelta(minutes=1), timedelta(minutes=20))


@pytest.mark.parametrize(
    "operators, message",
    [
        ("[]", "needs operators"),
        ('["BN ML"]', "not an operator code"),
        ('["BNML&api_key=x"]', "not an operator code"),
        ('"BNML"', "must be a list"),
    ],
)
def test_bods_bus_operators_are_checked(tmp_path, operators, message):
    path = write(tmp_path, BODS_BUS.format(state=tmp_path / "s.json").replace('["BNML", "HIPK"]', operators))
    with pytest.raises(ConfigError, match=message):
        load_config(path, {"BODS_API_KEY": "bods"})


def test_unknown_bus_source_is_reported(tmp_path):
    path = write(tmp_path, BODS_BUS.format(state=tmp_path / "s.json").replace('"bods"', '"nextbuses"'))
    with pytest.raises(ConfigError, match='source must be "transportapi" or "bods"'):
        load_config(path, {"BODS_API_KEY": "bods"})


def test_bus_source_defaults_to_bods(tmp_path):
    from twobells.sources.bodsbus import BodsBusSource

    path = write(tmp_path, f'state_file = "{tmp_path / "s.json"}"\n[bus]\noperators = ["BNML"]\n'
                           '[bus.stops]\nA = "1000DGHS0900"\n')
    config = load_config(path, {**ENV, "BODS_API_KEY": "bods"}).board.sources[0]
    assert isinstance(config.source, BodsBusSource)
    assert config.source._fallback is not None  # TransportAPI's keys are set: it stands in
    assert config.policy.interval == timedelta(minutes=2)


def test_transportapi_alone_is_asked_for_outright(tmp_path):
    path = write(tmp_path, '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:00"]\n'
                           '[bus.stops]\nA = "1000DGHS0900"\n')
    config = load_config(path, ENV).board.sources[0]
    assert isinstance(config.source, TransportApiBusSource)
    assert config.fallback_policy is None
    assert config.policy.interval == timedelta(minutes=12)


def test_buses_with_no_operators_say_how_to_use_transportapi_alone(tmp_path):
    path = write(tmp_path, '[bus]\n[bus.stops]\nA = "1000DGHS0900"\n')
    with pytest.raises(ConfigError, match=r'needs operators.*source = "transportapi"'):
        load_config(path, {**ENV, "BODS_API_KEY": "bods"})


def test_bods_bus_fallback_has_its_own_windows(tmp_path):
    from twobells.core.polling import parse_window

    path = write(tmp_path, BODS_BUS.format(state=tmp_path / "s.json").replace(
        'source = "bods"', 'source = "bods"\nfallback_windows = ["Mon-Fri 07:00-08:30", "Mon-Fri 17:00-18:00"]'))
    config = load_config(path, {"BODS_API_KEY": "bods"}).board.sources[0]
    assert config.policy.windows == (parse_window("Mon-Sun 06:00-23:00"),)
    assert config.fallback_policy.windows == (parse_window("Mon-Fri 07:00-08:30"), parse_window("Mon-Fri 17:00-18:00"))


def test_bods_bus_fallback_windows_are_checked(tmp_path):
    path = write(tmp_path, BODS_BUS.format(state=tmp_path / "s.json").replace(
        'source = "bods"', 'source = "bods"\nfallback_windows = ["evenings"]'))
    with pytest.raises(ConfigError, match="evenings"):
        load_config(path, {"BODS_API_KEY": "bods"})


def test_the_board_looks_for_a_reset_code_in_the_store(tmp_path):
    import time

    from twobells.settings.resetcodes import ResetCodes
    from twobells.settings.store import platform_folder

    elsewhere = load_config(write(tmp_path, TRAIN), ENV).board
    code = ResetCodes(platform_folder()).issue(time.time())  # no state_file: the platform's folder
    assert elsewhere.reset_code().code == f"{code[:3]} {code[3:]}"
    board = load_config(_with_state(tmp_path, TRAIN), ENV).board
    assert board.reset_code() is None
    code = ResetCodes(tmp_path / "state").issue(time.time())
    assert board.reset_code().code == f"{code[:3]} {code[3:]}"
    assert load_config(_with_state(tmp_path, TRAIN), ENV, only={"train"}).board.reset_code is None


# --- A first run: no config.toml, or the template as installed ---


def installed_template(tmp_path):
    """examples/config.example.toml as install.sh copies it, its state folder under tmp_path."""
    text = TEMPLATE.read_text().replace("/var/lib/twobells/state.json", str(tmp_path / "state" / "state.json"))
    return write(tmp_path, text)


def test_first_run_settings_use_the_templates_display_and_state_folder(tmp_path):
    import time

    from twobells.board.config import first_run_settings
    from twobells.settings.resetcodes import ResetCodes

    settings = first_run_settings(installed_template(tmp_path))
    assert settings.board.sources == [] and settings.board.weather is None
    assert settings.state_file == tmp_path / "state" / "state.json"
    assert (settings.display, settings.board.theme.name) == ("inky", "sign")
    assert settings.board.reset_code() is None
    code = ResetCodes(tmp_path / "state").issue(time.time())
    assert settings.board.reset_code().code == f"{code[:3]} {code[3:]}"  # to claim the settings site


# Which modes the board shows: [board] modes (settings/modes.py).

BOTH = '''
[train]
crs = "GLO"
[bus]
source = "transportapi"
[bus.stops]
A = "1000DGHS0900"
'''


def test_the_board_shows_trains_and_buses_while_both_are_set_up(tmp_path):
    settings = load_config(write(tmp_path, BOTH), ENV)
    assert settings.board.modes == ("train", "bus")
    assert [c.source.name for c in settings.board.sources] == ["train", "bus"]


def test_the_board_fetches_only_the_modes_it_shows(tmp_path):
    settings = load_config(write(tmp_path, BOTH + '[board]\nmodes = ["bus"]\n'), {"TAPI_APP_ID": "id",
                                                                                  "TAPI_APP_KEY": "key"})
    assert settings.board.modes == ("bus",)
    assert [c.source.name for c in settings.board.sources] == ["bus"]  # no rail key needed


def test_a_board_with_one_mode_set_up_shows_that_one_alone(tmp_path):
    settings = load_config(write(tmp_path, '[train]\ncrs = "GLO"\n'), ENV)
    assert settings.board.modes == ("train",)


def test_a_board_showing_more_modes_than_its_display_has_room_for_is_refused(tmp_path):
    text = BOTH + '[board]\nmodes = ["train", "bus", "metro"]\n'
    with pytest.raises(ConfigError, match=r"\[board\] modes: The Inky pHAT has room for 2 modes at most"):
        load_config(write(tmp_path, text), ENV)
    # A screen has room for three in the plain look up to a text size, and says what would give it room.
    plain = 'display = "hdmi-1920x440"\ntheme = "plain"\ntext_size = 14\n'
    with pytest.raises(ConfigError, match=r"\[board\] modes: The 11\.3-inch bar screen in the plain look at text "
                                          r"size 14 has room for 2 modes at most, each with its own section, not 3; "
                                          r"name fewer, or choose text size 13 or smaller, or another look"):
        load_config(write(tmp_path, plain + text), ENV)
    # The 10.85" e-paper has room for three, as a screen has in the station board look; trams aren't set up,
    # so trains and buses show.
    for display in ("waveshare-10in85g", "hdmi-1920x440"):
        settings = load_config(write(tmp_path, f'display = "{display}"\n' + text), ENV)
        assert settings.board.modes == ("train", "bus")
    settings = load_config(write(tmp_path, plain.replace("14", "13") + text), ENV)
    assert settings.board.modes == ("train", "bus")


def test_only_the_board_refuses_more_modes_than_its_display_has_room_for(tmp_path):
    # A probe or a timetable build loads its own source alone; the board's room is the board's to check.
    path = write(tmp_path, BOTH + '[board]\nmodes = ["train", "bus", "metro"]\n')
    settings = load_config(path, ENV, only={"train"})
    assert [config.source.name for config in settings.board.sources] == ["train"]


def test_the_display_override_sets_the_room_too(tmp_path):
    path = write(tmp_path, 'display = "waveshare-10in85g"\ntheme = "plain"\ntext_size = 13\n' + BOTH
                 + '[board]\nmodes = ["train", "bus", "metro"]\n')
    load_config(path, ENV)
    with pytest.raises(ConfigError, match=r"The 7\.9-inch screen in the plain look at text size 13 has room for 2"):
        load_config(path, ENV, display="hdmi-1280x400")


def test_a_board_modes_list_that_is_not_one_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match=r"\[board\] modes must be a list of modes"):
        load_config(write(tmp_path, BOTH + '[board]\nmodes = "bus"\n'), ENV)


def test_first_run_settings_keep_the_screen_and_its_turn(tmp_path):
    from twobells.board.config import first_run_settings

    text = TEMPLATE.read_text().replace('display = "inky"', 'display = "hdmi-1280x400"').replace("rotate = 0", "rotate = 90")
    settings = first_run_settings(write(tmp_path, text))
    assert (settings.display, settings.rotate) == ("hdmi-1280x400", 90)


def test_first_run_settings_fall_back_to_the_defaults(tmp_path):
    from twobells.board.config import first_run_settings
    from twobells.settings.store import Store

    missing = first_run_settings(tmp_path / "missing.toml")
    assert (missing.display, missing.rotate) == ("inky", 0) and missing.board.reset_code is not None
    given = first_run_settings(Store.open(tmp_path / "missing.toml", state_dir=tmp_path / "state"))
    assert given.state_file == tmp_path / "state" / "state.json" and given.board.reset_code is not None
    text = TEMPLATE.read_text().replace('display = "inky"', 'display = "hdmi-1280x400"\npanel_colour = "purple"')
    text = text.replace("/var/lib/twobells/state.json", str(tmp_path / "state" / "state.json"))
    broken = first_run_settings(write(tmp_path, text))
    assert broken.display == "inky" and broken.state_file == tmp_path / "state" / "state.json"


# Trams: [metro], from TfGM's timetable kept in the store (sources/trams.py).

TRAMS = '''
[metro]
directions = ["Bury"]
[metro.stops]
T = "9400ZZMAPIC"
[metro.walk_minutes]
T = 6
'''


def test_trams_load_from_their_stops_with_their_own_walks(tmp_path):
    from twobells.sources.trams import TramTimetableSource

    settings = load_config(write(tmp_path, TRAMS), ENV)
    [config] = settings.board.sources
    assert isinstance(config.source, TramTimetableSource) and config.source.stops == {"T": "9400ZZMAPIC"}
    assert settings.board.modes == ("metro",)
    assert settings.board.walk.metro == {"T": 6}
    assert config.policy.interval == timedelta(minutes=1)


@pytest.mark.parametrize(("stops", "says"), [
    ("", r"Add a \[metro.stops\] table"),
    ('T = "1800SB12341"', "set a Metrolink stop code"),
    ('T = 5', "set a Metrolink stop code"),
    ('"Stop T" = "9400ZZMAPIC"', "1 to 3 letters or digits"),
    ('"T\\n" = "9400ZZMAPIC"', "1 to 3 letters or digits"),  # a pattern's $ matches before a last newline
    ('T = "9400ZZMAPIC\\n"', "set a Metrolink stop code"),
    ("\n".join(f'{c} = "9400ZZMAPIC"' for c in "ABCDE"), "at most 4"),
])
def test_tram_stops_that_aren_t_metrolink_stops_are_refused(tmp_path, stops, says):
    with pytest.raises(ConfigError, match=says):
        load_config(write(tmp_path, f"[metro]\n[metro.stops]\n{stops}\n"), ENV)


# London's metro: [metro] with TfL's codes, live from TfL (sources/tfl.py).


def test_london_stops_come_live_from_tfl_with_their_lines_and_key(tmp_path):
    from twobells.sources.tfl import TflSource

    text = '[metro]\n[metro.stops]\nT = "940GZZLUBNK"\nU = "940GZZDLBNK"\n'
    settings = load_config(write(tmp_path, text), {**ENV, "TFL_APP_KEY": "abc123"})
    [config] = settings.board.sources
    source = config.source
    assert isinstance(source, TflSource) and source.stops == {"T": "940GZZLUBNK", "U": "940GZZDLBNK"}
    assert source.label == "Metro" and source.lines[:3] == ["central", "northern", "waterloo-city"]
    assert "dlr" in source.lines
    assert source._app_key == "abc123"
    assert load_config(write(tmp_path, text), ENV).board.sources[0].source._app_key == ""  # optional


@pytest.mark.parametrize(("stops", "env", "says"), [
    ('T = "940GZZLUBNK"\nU = "9400ZZMAPIC"', {}, "Metrolink stops and London ones"),
    ('T = "940GZZOVBNK"', {}, "set a Metrolink stop code"),  # Overground: Darwin's
    ('T = "930G"', {}, "set a Metrolink stop code"),
    ('T = "940GZZLUBNK"', {"TFL_APP_KEY": "abc\r\nX-Other: 1"}, "TFL_APP_KEY in secrets.env isn't a key"),
])
def test_london_stops_and_keys_that_aren_t_right_are_refused(tmp_path, stops, env, says):
    with pytest.raises(ConfigError, match=says):
        load_config(write(tmp_path, f"[metro]\n[metro.stops]\n{stops}\n"), {**ENV, **env})

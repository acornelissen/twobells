import pytest

from tests.paths import ROOT

TEMPLATE = ROOT / "examples/config.example.toml"  # the blank template, with CHANGE_ME placeholders
GLOSSOP = ROOT / "examples" / "glossop.toml"  # the worked example, filled in


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


def installed_template(tmp_path):
    """examples/config.example.toml as install.sh copies it, its state folder under tmp_path."""
    text = TEMPLATE.read_text().replace("/var/lib/twobells/state.json", str(tmp_path / "state" / "state.json"))
    return write(tmp_path, text)


def test_merge_replaces_lists_and_merges_tables():
    from twobells.settings.config import merge

    base = {"a": 1, "t": {"x": 1, "y": [1, 2]}, "l": [1]}
    assert merge(base, {"t": {"y": [3]}, "l": [2]}) == {"a": 1, "t": {"x": 1, "y": [3]}, "l": [2]}


def test_merge_replaces_tables_below_the_top_level():
    from twobells.settings.config import merge

    base = {"bus": {"source": "transportapi", "rows": 2, "stops": {"A": "x", "B": "y"}, "routes": {"341": {}}}}
    merged = merge(base, {"bus": {"source": "transportapi", "stops": {"A": "x"}, "walk_minutes": {}}})
    assert merged == {"bus": {"source": "transportapi", "rows": 2, "stops": {"A": "x"}, "walk_minutes": {}, "routes": {"341": {}}}}


def test_no_config_or_the_template_as_installed_needs_setting_up(tmp_path):
    from twobells.settings.config import needs_setup

    assert needs_setup(tmp_path / "missing.toml")
    assert needs_setup(installed_template(tmp_path))
    assert needs_setup(TEMPLATE)


@pytest.mark.parametrize("text", ["", "\n# Nothing yet\n", 'title = "Departures"\ndisplay = "inky"\n'])


def test_a_config_with_neither_trains_nor_buses_needs_setting_up(tmp_path, text):
    from twobells.settings.config import needs_setup

    assert needs_setup(write(tmp_path, text))


def test_settings_saved_without_a_config_count(tmp_path):
    from twobells.settings.config import needs_setup
    from twobells.settings.store import Store

    store = Store.open(tmp_path / "missing.toml", state_dir=tmp_path / "data")
    assert needs_setup(store)
    store.make()
    (tmp_path / "data" / "settings.toml").write_text('title = "Departures"\n')
    assert needs_setup(store)  # saved, but nothing to show
    (tmp_path / "data" / "settings.toml").write_text('[train]\ncrs = "GLO"\n')
    assert not needs_setup(store)


def test_trains_and_buses_both_switched_off_need_setting_up(tmp_path):
    from twobells.settings.config import needs_setup

    config = write(tmp_path, '[train]\ncrs = "GLO"\nenabled = false\n[bus]\nsource = "transportapi"\nenabled = false\n')
    assert needs_setup(config)
    config.write_text('[train]\ncrs = "GLO"\n[bus]\nsource = "transportapi"\nenabled = false\n')
    assert not needs_setup(config)


def test_a_filled_in_board_needs_no_setting_up(tmp_path, example):
    from twobells.settings.config import needs_setup

    assert not needs_setup(example)
    assert not needs_setup(filled_template(tmp_path))


def test_setting_up_from_the_page_counts_once_every_placeholder_is_covered(tmp_path):
    from twobells.settings.config import needs_setup

    config = installed_template(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "settings.toml").write_text('[train]\ncrs = "GLO"\n')
    assert needs_setup(config)  # the bus stop is still CHANGE_ME
    (state / "settings.toml").write_text('[train]\ncrs = "GLO"\n[bus.stops]\nA = "1800SB12341"\n')
    assert needs_setup(config)  # the operators are still CHANGE_ME
    (state / "settings.toml").write_text('[train]\ncrs = "GLO"\n[bus]\noperators = ["SBUS"]\n'
                                         '[bus.stops]\nA = "1800SB12341"\n')
    assert not needs_setup(config)


def test_placeholders_in_a_section_switched_off_need_no_setting_up(tmp_path):
    from twobells.settings.config import needs_setup

    config = installed_template(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "settings.toml").write_text('[train]\ncrs = "GLO"\n[bus]\nsource = "transportapi"\nenabled = false\n')
    assert not needs_setup(config)  # the bus stop is still CHANGE_ME, but buses are off
    (state / "settings.toml").write_text('[train]\nenabled = false\n[bus]\noperators = ["SBUS"]\n'
                                         '[bus.stops]\nA = "1800SB12341"\n')
    assert not needs_setup(config)
    (state / "settings.toml").write_text('[train]\ncrs = "GLO"\n[bus]\nsource = "transportapi"\nenabled = true\n')
    assert needs_setup(config)
    (state / "settings.toml").write_text('[train]\ncrs = "GLO"\n[bus]\nsource = "transportapi"\nenabled = "no"\n')
    assert not needs_setup(config)  # a fault, for load_config to report


@pytest.mark.parametrize("text", ["title = \n", "[train\n"])


def test_a_broken_config_is_a_fault_to_report_not_a_first_run(tmp_path, text):
    from twobells.settings.config import needs_setup

    assert not needs_setup(write(tmp_path, text))

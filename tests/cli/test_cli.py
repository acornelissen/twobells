import http.client
import json
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image

from departureboard.cli.main import main
from departureboard.settings.config import load_env_file
from departureboard.sources import holidays
from tests.paths import FIXTURES, ROOT


@pytest.fixture(autouse=True)
def gov_uk(monkeypatch):
    """No test reaches gov.uk: bank holiday downloads fail as if offline, and are counted."""
    calls = []

    def offline(url, max_bytes, timeout=15.0):
        calls.append(url)
        raise OSError("offline in tests")

    monkeypatch.setattr(holidays, "get_bytes", offline)
    return calls


def test_demo_writes_preview_without_any_keys(tmp_path):
    out = tmp_path / "demo.png"
    assert main(["demo", "--png", str(out)]) == 0
    assert Image.open(out).size == (250, 122)


@pytest.mark.parametrize("display, size", [("waveshare-10in85g", (1360, 480)), ("hdmi-1280x400", (1280, 400))])
def test_demo_previews_bigger_panels(tmp_path, display, size):
    out = tmp_path / "demo.png"
    assert main(["demo", "--png", str(out), "--display", display]) == 0
    assert Image.open(out).size == size


@pytest.mark.parametrize("display", ["inky", "waveshare-10in85g", "hdmi-1280x400"])
@pytest.mark.parametrize("theme", ["sign", "plain"])
def test_demo_can_show_a_notice_and_a_rain_line(tmp_path, display, theme):
    def demo(*flags):
        out = tmp_path / f"{'-'.join(flags) or 'plain'}.png"
        assert main(["demo", "--png", str(out), "--display", display, "--theme", theme, *flags]) == 0
        return Image.open(out).tobytes()

    plain, notice, rain, both = demo(), demo("--notice"), demo("--rain"), demo("--notice", "--rain")
    assert len({plain, notice, rain}) == 3
    assert both == notice  # a disruption notice takes the rain line's place


def test_demo_rain_line_follows_the_rain_rule(monkeypatch, tmp_path):
    from departureboard.cli import dev

    views = []
    real = dev.build_view
    monkeypatch.setattr(dev, "build_view", lambda *a, **k: views.append(real(*a, **k)) or views[-1])
    main(["demo", "--png", str(tmp_path / "a.png"), "--rain"])
    main(["demo", "--png", str(tmp_path / "b.png"), "--rain", "--display", "hdmi-1280x400"])
    main(["demo", "--png", str(tmp_path / "c.png"), "--notice"])
    assert [v.rain for v in views] == ["Rain from 07:00 (70%)", "Rain from 07:03 (70%)", None]
    assert views[2].notice.startswith("Disruption between Glossop")


def test_load_env_file_parses_assignments(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text('# comment\nRDM_API_KEY=abc\n\nTAPI_APP_ID = "id"\nTAPI_APP_KEY=\'k=1\'\n')
    assert load_env_file(path) == {"RDM_API_KEY": "abc", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "k=1"}


def test_unreadable_env_file_is_treated_as_empty(tmp_path):
    # systemd reads secrets.env as root; the service user may not be able to.
    assert load_env_file(tmp_path) == {}  # a directory cannot be read as a file


def test_run_reports_config_errors_without_traceback(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    code = main(["run", "--config", str(config), "--secrets", str(tmp_path / "missing.env")])
    assert code == 2
    assert "RDM_API_KEY" in capsys.readouterr().err


CONFIG = """
[bus]
source = "transportapi"
windows = ["Mon-Fri 07:00-08:00"]
[bus.stops]
A = "atco-a"
"""

PAYLOADS = {
    "atco-a": {"departures": {"all": [{
        "id": "https://transportapi.com/v3/uk/bus/route.json?app_id=id&app_key=key",
        "line_name": "341", "direction": "Hyde", "date": "2026-10-09",
        "aimed_departure_time": "07:20", "expected_departure_time": "07:24",
        "best_departure_estimate": "07:24",
    }]}},
    "atco-v": {"departures": {"all": []}},
}


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    from departureboard.sources.transportapi import TransportApiBusSource

    calls = []

    def fake_raw(self, atco):
        calls.append(atco)
        if atco == "broken":
            raise OSError("boom")
        return PAYLOADS[atco]

    monkeypatch.setattr(TransportApiBusSource, "fetch_raw", fake_raw)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    secrets = tmp_path / "secrets.env"
    secrets.write_text("TAPI_APP_ID=id\nTAPI_APP_KEY=key\n")
    return ["--config", str(config), "--secrets", str(secrets)], calls


def test_probe_bus_lists_each_configured_stop(probe_env, capsys):
    args, calls = probe_env
    assert main(["probe", "bus", *args]) == 0
    out = capsys.readouterr().out
    assert calls == ["atco-a"]
    assert "A  atco-a" in out
    assert "341" in out and "Hyde" in out and "07:24" in out


def test_probe_bus_accepts_stop_overrides(probe_env, capsys):
    args, calls = probe_env
    assert main(["probe", "bus", *args, "--stop", "A=atco-a", "--stop", "V=atco-v"]) == 0
    out = capsys.readouterr().out
    assert calls == ["atco-a", "atco-v"]
    assert "V  atco-v" in out
    assert "no departures" in out


def test_probe_bus_saves_raw_responses(probe_env, tmp_path):
    args, _ = probe_env
    out_dir = tmp_path / "raw"
    main(["probe", "bus", *args, "--save", str(out_dir)])
    saved = json.loads((out_dir / "atco-a.json").read_text())
    entry = saved["departures"]["all"][0]
    assert entry["line_name"] == "341"
    assert entry["id"].endswith("app_id=REDACTED&app_key=REDACTED")


def test_probe_bus_reports_failing_stop_and_carries_on(probe_env, capsys):
    args, calls = probe_env
    code = main(["probe", "bus", *args, "--stop", "X=broken", "--stop", "A=atco-a"])
    out = capsys.readouterr().out
    assert code == 1
    assert calls == ["broken", "atco-a"]
    assert "boom" in out and "341" in out


def test_probe_rejects_malformed_stop_override(probe_env, capsys):
    args, _ = probe_env
    assert main(["probe", "bus", *args, "--stop", "nolabel"]) == 2


def test_probe_bus_works_without_rail_key(probe_env, capsys):
    args, _ = probe_env
    config = args[1]
    with open(config, "a") as f:
        f.write('\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert main(["probe", "bus", *args]) == 0


def test_demo_png_can_use_either_theme(tmp_path):
    for theme in ("sign", "plain"):
        out = tmp_path / f"{theme}.png"
        assert main(["demo", "--theme", theme, "--png", str(out)]) == 0
        assert Image.open(out).size == (250, 122)


def test_probe_train_reports_http_errors_on_one_line(tmp_path, monkeypatch, capsys):
    from urllib.error import HTTPError

    from departureboard.sources.darwin import DarwinSource

    def refuse(self):
        raise HTTPError("https://example.test", 403, "Forbidden", None, None)

    monkeypatch.setattr(DarwinSource, "fetch", refuse)
    config = tmp_path / "config.toml"
    config.write_text('[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=k\n")

    code = main(["probe", "train", "--config", str(config), "--secrets", str(secrets)])

    out = capsys.readouterr().out
    assert code == 1
    assert "403" in out and "Forbidden" in out
    assert "Traceback" not in out


@pytest.fixture
def train_probe(tmp_path, monkeypatch):
    """A train config, and a fake Darwin that answers with the synthetic
    detailed Glossop board and records the URLs asked for."""
    from departureboard.sources import darwin

    board = json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())
    urls = []
    monkeypatch.setattr(darwin, "get_json", lambda url, headers: urls.append(url) or board)
    monkeypatch.setattr(darwin, "get_json_capped", lambda url, headers, max_bytes: urls.append(url) or board)
    config = tmp_path / "config.toml"
    config.write_text('[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=k\n")
    return ["--config", str(config), "--secrets", str(secrets)], urls


def test_probe_train_details_prints_coaches_and_calling_points(train_probe, capsys):
    args, urls = train_probe
    assert main(["probe", "train", "--details", *args]) == 0
    out = capsys.readouterr().out
    assert ["/GetDepBoardWithDetails/GLO" in url for url in urls] == [True]
    assert "22:25  Manchester Piccadilly" in out
    assert "Northern from Hadfield, 2 coaches" in out
    assert "    22:39  Newton for Hyde" in out and "On time" in out


def test_probe_train_without_details_is_unchanged(train_probe, capsys):
    args, urls = train_probe
    assert main(["probe", "train", *args]) == 0
    out = capsys.readouterr().out
    assert ["/GetDepartureBoard/GLO" in url for url in urls] == [True]
    assert "22:25  Northern   Manchester Piccadilly" in out
    assert "coaches" not in out and "Newton for Hyde" not in out


def test_probe_details_is_for_trains_only(probe_env, capsys):
    args, calls = probe_env
    assert main(["probe", "bus", *args, "--details"]) == 2
    assert "trains only" in capsys.readouterr().err
    assert calls == []


def test_emulate_primes_the_first_tick(monkeypatch):
    from departureboard.cli import dev

    seen = {}
    monkeypatch.setattr(dev, "run_loop", lambda *a, **kw: seen.update(kw))
    assert main(["emulate", "--demo", "--no-browser", "--port", "0"]) == 0
    assert seen.get("prime") is True


def _run_setup(tmp_path, monkeypatch):
    from datetime import datetime

    from departureboard.cli import run
    from departureboard.sources.darwin import DarwinSource

    calls = []
    monkeypatch.setattr(DarwinSource, "fetch", lambda self: calls.append(1) or [])
    monkeypatch.setattr(run, "local_now", lambda tz: datetime(2026, 10, 10, 12, 0))  # Saturday
    config = tmp_path / "config.toml"
    config.write_text(
        f'state_file = "{tmp_path / "state.json"}"\n'
        '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
    )
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=k\n")
    args = ["run", "--config", str(config), "--secrets", str(secrets), "--png", str(tmp_path / "out.png")]
    return args, calls


def test_first_ever_run_primes_outside_windows(tmp_path, monkeypatch):
    args, calls = _run_setup(tmp_path, monkeypatch)
    assert main(args) == 0
    assert calls == [1]


def test_later_runs_respect_windows(tmp_path, monkeypatch):
    args, calls = _run_setup(tmp_path, monkeypatch)
    main(args)
    main(args)
    assert calls == [1]


def test_run_png_draws_at_the_configured_display_size(tmp_path):
    config = tmp_path / "config.toml"
    state = tmp_path / "state.json"
    config.write_text(f'display = "waveshare-10in85g"\nstate_file = "{state}"\n')
    out = tmp_path / "board.png"
    assert main(["run", "--config", str(config), "--secrets", str(tmp_path / "none.env"), "--png", str(out)]) == 0
    assert Image.open(out).size == (1360, 480)


def test_run_loop_on_a_screen_draws_a_running_clock(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    state = tmp_path / "state.json"
    config.write_text(f'display = "hdmi-1280x400"\nrotate = 90\nstate_file = "{state}"\n')
    out = tmp_path / "board.png"
    args = ["run", "--loop", "--ticks", "2", "--tick", "0", "--png", str(out)]
    assert main([*args, "--config", str(config), "--secrets", str(tmp_path / "none.env")]) == 0
    assert Image.open(out).size == (1280, 400)
    assert state.exists()


def test_sleeping_to_the_tick_lands_on_a_whole_second(monkeypatch):
    from departureboard.cli import common

    slept = []
    monkeypatch.setattr(common.time, "time", lambda: 1000.25)
    monkeypatch.setattr(common.time, "sleep", slept.append)
    common.sleep_to_the_tick(1)
    common.sleep_to_the_tick(60)
    common.sleep_to_the_tick(0)
    assert slept == [0.75, 19.75]  # 1000.25 s is 40.25 s past a minute


def _route_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:30"]\nwalk_minutes = 2\n[bus.stops]\nA = "1000DGHS0900"\n')
    return config


def test_route_prints_a_config_block_from_a_timetable_file(tmp_path, capsys, monkeypatch):
    import tomllib

    from departureboard.timetable import routesetup

    fixtures = FIXTURES
    naptan = (fixtures / "naptan_glossop_high_street_west.csv").read_bytes()
    monkeypatch.setattr(routesetup, "naptan_stops", lambda atcos: routesetup.parse_naptan(naptan.decode(), set(atcos)))
    code = main(["route", "341", "--stop", "A", "--txc", str(fixtures / "txc_glossop_synthetic.xml"), "--config", str(_route_config(tmp_path))])
    assert code == 0
    printed = capsys.readouterr().out
    route = tomllib.loads(printed)["bus"]["routes"]["341"]
    assert [s["name"] for s in route["stops"]] == [
        "The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street",
    ]


def test_route_searches_each_download_before_fetching_the_next(tmp_path, capsys, monkeypatch):
    # Holding every unzipped timetable at once could run to gigabytes.
    import io
    import zipfile

    from departureboard.cli import dev
    from departureboard.timetable import routesetup

    txc = (FIXTURES / "txc_glossop_synthetic.xml").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.xml", txc)
        archive.writestr("b.xml", txc)
    events = []

    def fake_get(url, limit, timeout):
        events.append(f"download {url}")
        return buffer.getvalue()

    real_patterns = routesetup.patterns
    monkeypatch.setattr(dev, "get_bytes", fake_get)
    monkeypatch.setattr(routesetup, "dataset_urls", lambda key, noc, line: ["one", "two"])
    monkeypatch.setattr(routesetup, "patterns", lambda doc, line: events.append("search") or real_patterns(doc, line))
    monkeypatch.setattr(routesetup, "naptan_stops", lambda atcos: {a: (a, 53.4, -1.9) for a in atcos})
    monkeypatch.setenv("BODS_API_KEY", "k")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("")
    args = ["route", "341", "--stop", "A", "--noc", "BNML", "--secrets", str(secrets), "--config", str(_route_config(tmp_path))]
    assert main(args) == 0
    assert events == ["download one", "search", "search", "download two", "search", "search"]


def test_route_needs_a_timetable_source(tmp_path, capsys):
    assert main(["route", "341", "--stop", "A", "--config", str(_route_config(tmp_path))]) == 2
    assert "--noc" in capsys.readouterr().err


def test_route_needs_a_known_stop(tmp_path, capsys):
    assert main(["route", "341", "--stop", "Z", "--txc", "x.xml", "--config", str(_route_config(tmp_path))]) == 2
    assert "'Z'" in capsys.readouterr().err


def test_keys_saved_from_the_web_page_win_over_secrets_env(tmp_path):
    from departureboard.cli.dev import _env

    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{tmp_path / "state" / "state.json"}"\n')
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=old\nTAPI_APP_ID=id\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "secrets.env").write_text("RDM_API_KEY=new\n")
    env = _env(config, secrets)
    assert env["RDM_API_KEY"] == "new"
    assert env["TAPI_APP_ID"] == "id"


def _watched(tmp_path, extra=""):
    import argparse

    from departureboard.cli.common import load_settings
    from departureboard.cli.run import _Watcher

    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\ntitle = "Before"\n{extra}')
    args = argparse.Namespace(config=str(config), secrets=str(tmp_path / "none.env"))
    return _Watcher(args, load_settings(args)), state


def test_watcher_reloads_when_saved_settings_change(tmp_path):
    watcher, state = _watched(tmp_path)
    assert watcher() is None  # nothing changed
    (state / "settings.toml").write_text('title = "After"\n')
    board = watcher()
    assert board.title == "After"
    assert watcher() is None


def test_watcher_asks_for_a_restart_when_the_display_changes(tmp_path):
    from departureboard.cli.run import _Restart

    watcher, state = _watched(tmp_path, 'display = "hdmi-1280x400"\n')
    (state / "settings.toml").write_text("rotate = 90\n")
    with pytest.raises(_Restart):
        watcher()


def test_watcher_keeps_running_settings_when_new_ones_are_broken(tmp_path):
    watcher, state = _watched(tmp_path)
    (state / "settings.toml").write_text('theme = "neon"\n')
    assert watcher() is None


def test_run_saves_state_even_when_drawing_fails(tmp_path, monkeypatch):
    from departureboard.board.displays.display import PngDisplay

    args, calls = _run_setup(tmp_path, monkeypatch)

    def broken(self, image):
        raise OSError("GPIO busy")

    monkeypatch.setattr(PngDisplay, "show", broken)
    with pytest.raises(OSError):
        main(args)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["sources"]["train"]["last_attempt"]
    assert "last_render" not in state


def test_env_without_a_readable_config_is_just_the_environment(tmp_path):
    from departureboard.cli.dev import _env

    config = tmp_path / "config.toml"
    config.write_text("title = \n")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=old\n")
    assert _env(config, secrets)["RDM_API_KEY"] == "old"


@pytest.mark.parametrize("broken", ["title = \n", 'title = "Bad"\naccent = "blue"\n', 'title = "Bad"\nmin_redraw_minutes = "2"\n'])
def test_watcher_keeps_running_settings_through_a_broken_hand_edit(tmp_path, broken):
    watcher, state = _watched(tmp_path)
    config = tmp_path / "config.toml"
    good = config.read_text()
    config.write_text(f'state_file = "{state / "state.json"}"\n{broken}')
    assert watcher() is None
    config.write_text(good.replace("Before", "Mended"))
    assert watcher().title == "Mended"


def test_settings_page_starts_even_when_saved_settings_are_broken(tmp_path, monkeypatch):
    # The page is how the owner fixes a broken settings.toml, so it must still start.
    import getpass

    state = tmp_path / "state"
    state.mkdir()
    (state / "settings.toml").write_text("title = \n")
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\n')
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "a long enough password")
    assert main(["web", "--config", str(config), "--set-password"]) == 0
    assert (state / "web.json").exists()


def _web_config(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\n')
    return config, state


def test_reset_code_prints_a_code_the_page_will_take(tmp_path, capsys):
    import re
    import time

    from departureboard.web.auth import ResetCodes

    config, state = _web_config(tmp_path)
    assert main(["web", "--config", str(config), "--reset-code"]) == 0
    out = capsys.readouterr().out
    code = re.search(r"Settings reset code: (\d{3}) (\d{3})", out)
    assert code and "valid until" in out
    assert not (state / "reset-display.json").exists()  # printed here, not shown on the board
    assert ResetCodes(state).verify("".join(code.groups()), time.time())


def test_reset_code_is_not_held_back_by_the_pages_limits(tmp_path, capsys):
    import time

    from departureboard.web.auth import ResetCodes

    config, state = _web_config(tmp_path)
    ResetCodes(state).issue(time.time())  # someone just pressed the page's button
    assert main(["web", "--config", str(config), "--reset-code"]) == 0
    assert main(["web", "--config", str(config), "--reset-code"]) == 0
    assert capsys.readouterr().out.count("Settings reset code:") == 2


def test_reset_code_and_set_password_are_one_or_the_other(tmp_path):
    config, _ = _web_config(tmp_path)
    with pytest.raises(SystemExit):
        main(["web", "--config", str(config), "--reset-code", "--set-password"])


# Root, by hand on a Pi, on the store the services' user owns: anything it
# wrote there would be root's, and the services would then fail with
# "Permission denied". Every command that writes to the store refuses.


@pytest.fixture
def root_on_their_store(tmp_path, monkeypatch):
    """Root (faked) and a store that, like any in the tests, belongs to another user."""
    import pwd

    config, state = _web_config(tmp_path)
    state.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    return config, state, pwd.getpwuid(os.stat(state).st_uid).pw_name


@pytest.mark.parametrize("command", [
    ["run"],
    ["run", "--loop"],
    ["timetable"],
    ["timetable", "--force"],
    ["setup", "discover", "--stop", "A=1000DGHS0900"],
    ["emulate", "--no-browser"],
    ["serve", "--install", "pi"],
    ["serve", "--set-password"],
    ["serve", "--reset-code"],
    ["web"],
    ["web", "--reset-code"],
    ["dashboard", "--no-browser"],
    ["dashboard", "--set-password"],
    ["fetch"],
    ["setup", "serve"],
])
def test_root_is_refused_a_store_that_belongs_to_the_services_user(root_on_their_store, capsys, command):
    import shlex

    config, state, owner = root_on_their_store
    argv = [*command, "--config", str(config)]
    assert main(argv) == 2
    said = capsys.readouterr()
    assert said.err.splitlines() == [f"Run this as the service's user: sudo -u {owner} departureboard {shlex.join(argv)}"]
    assert said.out == "" and list(state.iterdir()) == []


@pytest.mark.parametrize("argv0, named", [
    # On a Pi it isn't on the PATH, so the line names it where it was run from.
    ("/opt/departureboard/venv/bin/departureboard", "/opt/departureboard/venv/bin/departureboard"),
    ("departureboard", "departureboard"),  # found on the PATH (or not): its name does
    ("/usr/bin/pytest", "departureboard"),  # run some other way, as in these tests
])
def test_the_line_for_root_names_the_program_as_it_was_run(root_on_their_store, capsys, monkeypatch, argv0, named):
    import shlex

    config, _, owner = root_on_their_store
    monkeypatch.setattr(sys, "argv", [argv0])
    argv = ["timetable", "--config", str(config)]
    assert main(argv) == 2
    assert capsys.readouterr().err.splitlines() == [
        f"Run this as the service's user: sudo -u {owner} {shlex.join([named, *argv])}"]


@pytest.mark.parametrize("command", [
    ["query", "views"],  # install.sh and update.sh ask these
    ["query", "needs-timetable"],
    ["probe", "train"],
    ["route", "341", "--stop", "A", "--noc", "BNML"],
])
def test_root_may_still_run_what_only_reads_the_store(root_on_their_store, capsys, command):
    config, state, _ = root_on_their_store
    main([*command, "--config", str(config)])
    assert "Run this as" not in capsys.readouterr().err and list(state.iterdir()) == []


def test_root_may_use_its_own_store_and_anyone_else_goes_ahead_as_today(root_on_their_store, monkeypatch, capsys):
    from departureboard.settings import store

    config, state, _ = root_on_their_store
    monkeypatch.setattr(store, "_owner", lambda folder: 0)
    assert main(["web", "--config", str(config), "--reset-code"]) == 0
    monkeypatch.setattr(store, "_owner", lambda folder: 999)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert main(["web", "--config", str(config), "--reset-code"]) == 0
    assert capsys.readouterr().out.count("Settings reset code:") == 2


def _holiday_run(tmp_path, monkeypatch, extra=""):
    """A board with a Mon-Fri train window, run at 07:00 on the August bank holiday Monday."""
    from datetime import datetime

    from departureboard.cli import run
    from departureboard.sources.darwin import DarwinSource

    calls = []
    monkeypatch.setattr(DarwinSource, "fetch", lambda self: calls.append(1) or [])
    monkeypatch.setattr(run, "local_now", lambda tz: datetime(2026, 8, 31, 7, 0))
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text('{"sources": {}}')  # not a first run, so nothing is primed
    fresh = datetime.now().isoformat()
    cache = {"fetched": fresh, "checked": fresh, "dates": ["2026-08-31"]}
    (state / holidays.CACHE_NAME).write_text(json.dumps(cache))
    config = tmp_path / "config.toml"
    config.write_text(
        f'state_file = "{state / "state.json"}"\n{extra}'
        '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
    )
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=k\n")
    return ["--config", str(config), "--secrets", str(secrets)], calls


def test_run_treats_a_bank_holiday_like_a_sunday(tmp_path, monkeypatch, gov_uk):
    args, calls = _holiday_run(tmp_path, monkeypatch)
    assert main(["run", *args, "--png", str(tmp_path / "out.png")]) == 0
    assert calls == []  # a Mon-Fri window is off on a bank holiday
    assert gov_uk == []  # the cache is fresh


def test_run_follows_the_configured_bank_holiday_division(tmp_path, monkeypatch, gov_uk):
    # 31 August is a bank holiday in England and Wales but not in Scotland.
    args, calls = _holiday_run(tmp_path, monkeypatch, 'bank_holiday_division = "scotland"\n')
    fresh = json.loads((tmp_path / "state" / holidays.CACHE_NAME).read_text())
    fresh["divisions"] = {"england-and-wales": ["2026-08-31"], "scotland": ["2026-08-03"]}
    (tmp_path / "state" / holidays.CACHE_NAME).write_text(json.dumps(fresh))
    assert main(["run", *args, "--png", str(tmp_path / "out.png")]) == 0
    assert calls == [1]  # an ordinary Monday in Scotland
    assert gov_uk == []  # the cache is fresh and has Scotland's dates


def test_run_can_ignore_bank_holidays_without_asking_gov_uk(tmp_path, monkeypatch, gov_uk):
    args, calls = _holiday_run(tmp_path, monkeypatch, "bank_holidays = false\n")
    (tmp_path / "state" / holidays.CACHE_NAME).unlink()
    assert main(["run", *args, "--png", str(tmp_path / "out.png")]) == 0
    assert calls == [1]
    assert gov_uk == []


def test_run_without_a_holiday_cache_asks_gov_uk_once_and_carries_on(tmp_path, monkeypatch, gov_uk):
    args, calls = _holiday_run(tmp_path, monkeypatch)
    (tmp_path / "state" / holidays.CACHE_NAME).unlink()
    assert main(["run", *args, "--png", str(tmp_path / "out.png")]) == 0
    assert main(["run", *args, "--png", str(tmp_path / "out.png")]) == 0
    assert gov_uk == [holidays.URL]
    assert calls == [1]  # offline with no cache: an ordinary Monday


def test_a_probe_never_asks_gov_uk(tmp_path, monkeypatch, gov_uk):
    args, calls = _holiday_run(tmp_path, monkeypatch)
    (tmp_path / "state" / holidays.CACHE_NAME).unlink()
    assert main(["probe", "train", *args]) == 0
    assert calls == [1]
    assert gov_uk == []


def test_reloaded_settings_keep_the_bank_holidays(tmp_path, monkeypatch):
    import argparse

    from departureboard.cli.common import load_settings
    from departureboard.cli.run import _Watcher

    args, _ = _holiday_run(tmp_path, monkeypatch)
    namespace = argparse.Namespace(config=args[1], secrets=args[3])
    watcher = _Watcher(namespace, load_settings(namespace))
    (tmp_path / "state" / "settings.toml").write_text('title = "After"\n')
    board = watcher()
    assert board.sources[0].policy.holidays == {date(2026, 8, 31)}


@pytest.fixture
def bods_env(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        f'state_file = "{tmp_path / "state" / "state.json"}"\n'
        '[bus]\nsource = "bods"\noperators = ["BNML"]\nwindows = ["Mon-Sun 06:00-23:00"]\n'
        '[bus.stops]\nA = "1000DGHS0900"\n'
    )
    secrets = tmp_path / "secrets.env"
    secrets.write_text("BODS_API_KEY=bods\n")
    gov_uk = (FIXTURES / "govuk_bank_holidays.json").read_bytes()
    monkeypatch.setattr(holidays, "get_bytes", lambda url, most: gov_uk)
    return ["--config", str(config), "--secrets", str(secrets)], tmp_path / "state"


def test_timetable_builds_and_reports_the_bus_timetable(bods_env, monkeypatch, capsys):
    from departureboard.timetable import build as timetable
    from departureboard.timetable.model import Calendar, Call, Journey, Schedule

    args, folder = bods_env
    seen = {}

    def fake_refresh(folder, stops, operators, now, api_key, *, force=False, **kwargs):
        seen.update(stops=stops, operators=operators, key=api_key, force=force)
        journey = Journey("341", "BNGN", "Hyde", "1", (Call("1000DGHS0900", 600), Call("x", 610)),
                          Calendar(frozenset(range(7))))
        to_stand = Journey("341", "BNGN", "Glossop", "2", (Call("y", 600), Call("1000DGHSW936", 610)),
                           Calendar(frozenset(range(7))), inbound_only=True)
        made = Schedule(now, None, dict(stops), (journey, to_stand), {})
        timetable.save(folder, made)
        return made

    monkeypatch.setattr(timetable, "refresh", fake_refresh)
    assert main(["timetable", *args, "--force"]) == 0
    assert seen == {"stops": {"A": "1000DGHS0900"}, "operators": ["BNML"], "key": "bods", "force": True}
    assert "Bus timetable: 1 journeys at your stops (and 1 ending near them)" in capsys.readouterr().out
    assert (folder / "bank-holidays.json").exists()  # its holiday names come from here


def test_timetable_fails_when_no_timetable_could_be_built(bods_env, monkeypatch, capsys):
    from departureboard.timetable import build as timetable

    args, _ = bods_env
    monkeypatch.setattr(timetable, "refresh", lambda *a, **k: None)
    assert main(["timetable", *args]) == 1
    assert "No usable bus timetable: there is no bus timetable yet" in capsys.readouterr().err


def test_timetable_needs_the_bods_bus_source(probe_env, capsys):
    args, _ = probe_env
    assert main(["timetable", *args]) == 2
    assert 'source = "transportapi"' in capsys.readouterr().err


def test_timetable_with_buses_switched_off_has_nothing_to_do(bods_env, monkeypatch, capsys):
    # The wizard switches buses off for a board of trains alone; the weekly timer must still succeed.
    from departureboard.timetable import build as timetable

    args, folder = bods_env
    folder.mkdir()
    (folder / "settings.toml").write_text("[bus]\nsource = \"transportapi\"\nenabled = false\n")
    monkeypatch.setattr(timetable, "refresh", lambda *a, **k: pytest.fail("built a timetable"))
    assert main(["timetable", *args]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Buses are switched off, so there's no bus timetable to build\n"
    assert "TransportAPI" not in captured.err


def test_timetable_builds_from_bods_set_only_in_the_saved_settings(bods_env, monkeypatch, capsys):
    # The settings site or the wizard saved source = "bods" and the operators; config.toml says neither.
    from departureboard.timetable import build as timetable

    args, folder = bods_env
    config = Path(args[1])
    config.write_text(
        f'state_file = "{folder / "state.json"}"\n'
        '[bus]\nsource = "transportapi"\nwindows = ["Mon-Sun 06:00-23:00"]\n[bus.stops]\nA = "1000DGHS0900"\n'
    )
    folder.mkdir()
    (folder / "settings.toml").write_text('[bus]\nsource = "bods"\noperators = ["HIPK"]\n')
    seen = []

    def fake_refresh(folder, stops, operators, now, api_key, *, force=False, **kwargs):
        seen.append((folder, operators))
        return None

    monkeypatch.setattr(timetable, "refresh", fake_refresh)
    main(["timetable", *args])
    assert seen == [(folder, ["HIPK"])]


def test_timetable_is_skipped_while_another_build_runs(bods_env, monkeypatch, capsys):
    # The weekly timer meeting the setup search: the other build saves it; the timer isn't a failure.
    import fcntl
    import os

    from departureboard.timetable import build as timetable

    args, folder = bods_env
    folder.mkdir()
    monkeypatch.setattr(timetable, "refresh", lambda *a, **k: pytest.fail("built beside another build"))
    fd = os.open(folder / timetable.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert main(["timetable", *args, "--force"]) == 0
    finally:
        os.close(fd)
    assert capsys.readouterr().out == "Another bus timetable build is running, so this one is skipped\n"


def test_timetable_fails_when_its_lock_cannot_be_opened(bods_env, capsys):
    from departureboard.timetable import build as timetable

    args, folder = bods_env
    folder.mkdir()
    (folder / timetable.LOCK_NAME).symlink_to(folder / "elsewhere")
    assert main(["timetable", *args]) == 1
    assert "Bus timetable not built: Can't open" in capsys.readouterr().err
    assert not (folder / "elsewhere").exists()


def test_timetable_with_no_buses_at_all_has_nothing_to_do(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{tmp_path / "state.json"}"\n[train]\ncrs = "GLO"\n')
    assert main(["timetable", "--config", str(config), "--secrets", str(tmp_path / "none.env")]) == 0
    assert "no bus timetable to build" in capsys.readouterr().out


def test_probe_bus_with_bods_says_why_it_cannot_use_the_timetable(bods_env, capsys):
    args, _ = bods_env
    assert main(["probe", "bus", *args]) == 1
    captured = capsys.readouterr()
    assert "there is no bus timetable yet" in captured.out
    assert "departureboard timetable" in captured.out


# The web dashboard


def test_dashboard_options_parse_with_their_defaults():
    from departureboard.cli.main import _parser

    args = _parser().parse_args(["dashboard"])
    assert (args.state_dir, args.host, args.port, args.lan, args.no_browser) == (None, "127.0.0.1", None, False, False)
    assert args.config == "/etc/departureboard/config.toml"
    args = _parser().parse_args([
        "dashboard", "--config", "c.toml", "--secrets", "/nonexistent", "--state-dir", "/data",
        "--host", "0.0.0.0", "--port", "8090", "--lan", "--no-browser",
    ])
    assert (args.config, args.secrets, args.state_dir, args.host, args.port, args.lan, args.no_browser) == (
        "c.toml", "/nonexistent", "/data", "0.0.0.0", 8090, True, True,
    )


@pytest.mark.parametrize("port", ["-1", "65536", "http", ""])
def test_dashboard_refuses_a_port_that_is_not_one(port, capsys):
    with pytest.raises(SystemExit):
        main(["dashboard", "--port", port])
    assert "port number" in capsys.readouterr().err


@pytest.fixture
def dashboard_config(tmp_path, monkeypatch):
    """A trains-only config whose key comes from the environment, as in Docker."""
    monkeypatch.setenv("RDM_API_KEY", "from-the-environment")
    config = tmp_path / "config.toml"
    config.write_text('title = "Glossop"\n[train]\ncrs = "GLO"\n')
    return config


def test_dashboard_never_moves_off_a_port_it_was_given(dashboard_config, tmp_path, capsys):
    import socket

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        code = main(["dashboard", "--config", str(dashboard_config), "--secrets", str(tmp_path / "nonexistent"),
                     "--state-dir", str(tmp_path / "dash"), "--port", str(port), "--no-browser"])
    assert code == 2
    err = capsys.readouterr().err
    assert f"127.0.0.1:{port}" in err and "--port" in err


def test_dashboard_runs_until_interrupted_and_lets_go_of_its_folder(dashboard_config, tmp_path, monkeypatch, capsys):
    from departureboard.core import net
    from departureboard.dashboard.config import load_dashboard
    from departureboard.web import server as dashserver

    def offline(request, timeout):
        raise OSError("offline in tests")

    def interrupted(self, poll_interval=0.5):
        raise KeyboardInterrupt

    opened = []
    monkeypatch.setattr(net, "urlopen", offline)
    monkeypatch.setattr(dashserver.DashboardServer, "serve_forever", interrupted)
    monkeypatch.setattr("webbrowser.open", opened.append)
    folder = tmp_path / "dash"
    code = main(["dashboard", "--config", str(dashboard_config), "--secrets", str(tmp_path / "nonexistent"),
                 "--state-dir", str(folder), "--port", "0", "--lan"])
    assert code == 0
    out = capsys.readouterr().out
    assert opened and opened[0].startswith("http://localhost:") and opened[0] in out
    assert "On your home network: http://" in out
    assert (folder / "history.sqlite3").exists()
    load_dashboard(dashboard_config, {"RDM_API_KEY": "k"}, folder).close()  # the lock was let go


def free_port():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def answering(port, deadline=10.0):
    """Whether the dashboard on `port` answers within `deadline` seconds."""

    stop = time.monotonic() + deadline
    while time.monotonic() < stop:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/healthz", headers={"Host": "localhost"})
            conn.getresponse().read()
            conn.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def test_dashboard_starts_again_when_its_saved_settings_change(dashboard_config, tmp_path, monkeypatch, caplog):
    from departureboard.cli import serve
    from departureboard.core import net
    from departureboard.dashboard.config import load_dashboard

    def offline(request, timeout):
        raise OSError("offline in tests")

    started_again = []
    monkeypatch.setattr(net, "urlopen", offline)
    monkeypatch.setattr(serve, "SETTINGS_CHECK_S", 0.05)
    monkeypatch.setattr(serve, "_start_again", started_again.append)
    caplog.set_level("INFO")
    folder, port = tmp_path / "dash", free_port()

    def owner():
        assert answering(port)
        (folder / "settings.toml").write_text("[train]\ncrs = 12\n")  # saved by hand, and wrong
        stop = time.monotonic() + 5
        while time.monotonic() < stop and "don't load" not in caplog.text:
            time.sleep(0.02)
        (folder / "settings.toml").write_text('title = "Our house"\n')

    helper = threading.Thread(target=owner, daemon=True)
    helper.start()
    code = main(["dashboard", "--config", str(dashboard_config), "--secrets", str(tmp_path / "nonexistent"),
                 "--state-dir", str(folder), "--port", str(port), "--no-browser"])
    helper.join(timeout=5)
    assert code == 0 and started_again == [port]
    assert "Settings changed but don't load" in caplog.text  # the broken one kept the old settings
    assert "starting the dashboard again" in caplog.text
    assert load_dashboard(dashboard_config, {"RDM_API_KEY": "k"}, folder).title == "Our house"  # lock let go


def test_starting_again_runs_the_same_command_on_the_same_port_without_the_browser(monkeypatch):
    from departureboard.cli import serve

    ran = []
    monkeypatch.setattr(sys, "orig_argv", ["python3", "-m", "departureboard", "dashboard", "--lan"])
    monkeypatch.setattr(os, "execv", lambda path, argv: ran.append((path, argv)))
    serve._start_again(8091)
    assert ran == [(sys.executable, [sys.executable, "-m", "departureboard", "dashboard", "--lan",
                                     "--no-browser", "--port", "8091"])]


class Stopped:
    """A server that stops as soon as it starts, as for Ctrl+C."""

    server_address = ("127.0.0.1", 8090)

    def serve_forever(self, poll_interval=0.5):
        raise KeyboardInterrupt

    def shutdown(self):
        pass

    def server_close(self):
        pass


@pytest.fixture
def served(monkeypatch):
    """What serve would serve, and where: make_server records it and the server stops at once."""
    from departureboard.core import net
    from departureboard.web import server as dashserver

    def offline(request, timeout):
        raise OSError("offline in tests")

    made = {}

    def make_server(app, host, port, attempts=1):
        made.update(app=app, host=host, port=port, attempts=attempts)
        return Stopped()

    monkeypatch.setattr(net, "urlopen", offline)
    monkeypatch.setattr(dashserver, "make_server", make_server)
    monkeypatch.setattr("webbrowser.open", lambda url: made.setdefault("opened", []).append(url))
    return made


def ask(app, path, fields=None):
    """A GET to `app`, or with `fields` a form's POST from its own page, served
    for real on a free port: (status, Location, body)."""
    from urllib.parse import urlencode

    from departureboard.web.server import DashboardServer

    server = DashboardServer(("127.0.0.1", 0), app)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        if fields is None:
            conn.request("GET", path, headers={"Host": "localhost"})
        else:
            conn.request("POST", path, urlencode(fields), headers={
                "Host": "localhost", "Origin": "http://localhost",
                "Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        found = response.status, response.getheader("Location"), response.read()
        conn.close()
        return found
    finally:
        server.shutdown()
        server.server_close()


def test_a_config_that_fails_to_load_still_serves_the_settings(tmp_path, served, capsys):
    # In Docker a crash would be started again and again; the settings are where it is fixed.
    config = tmp_path / "config.toml"
    config.write_text('[train]\ncrs = "GLO"\n[dashboard]\ntrain_rows = 50\nallowed_hosts = ["departures.example.com"]\n')
    code = main(["serve", "--install", "docker", "--config", str(config), "--secrets", str(tmp_path / "none.env"),
                 "--state-dir", str(tmp_path / "data")])
    assert code == 0
    err = capsys.readouterr().err
    assert "train_rows must be a whole number from 1 to 20" in err and "Traceback" not in err
    app = served["app"]
    assert app.hub is None and "train_rows must be a whole number from 1 to 20" in app.settings.problem
    assert app.allowed_hosts == {"departures.example.com"}  # so the owner can still reach the settings by it
    assert ask(app, "/") == (303, "/settings", b"")
    assert ask(app, "/settings/login")[0] == 200
    assert ask(app, "/healthz") == (200, None, b"ok")
    assert ask(app, "/api/snapshot")[0] == 404


def test_serve_tells_the_dashboard_and_the_settings_its_version(tmp_path, served):
    import tomllib

    wanted = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert main(["serve", "--install", "docker", "--config", str(tmp_path / "config.toml"),
                 "--secrets", str(tmp_path / "none.env"), "--state-dir", str(tmp_path / "data")]) == 0
    app = served["app"]
    assert app.settings.version == wanted
    page = app.static.get("index.html").body.decode()
    footer = page[page.index('<footer class="foot">'):page.index("</footer>")]
    assert f'<p class="version">departureboard {wanted}</p>' in footer


@pytest.mark.parametrize("argv, host, port, attempts, browser", [
    (["serve", "--install", "docker"], "0.0.0.0", 8090, 1, False),
    (["serve", "--install", "docker", "--port", "8091"], "0.0.0.0", 8091, 1, False),
    (["serve"], "127.0.0.1", 8090, 10, True),  # a desktop: the next free port, and the browser
    (["serve", "--install", "desktop", "--lan", "--no-browser"], "0.0.0.0", 8090, 10, False),
    (["serve", "--install", "desktop", "--port", "8095", "--host", "192.168.1.5"], "192.168.1.5", 8095, 1, True),
    (["dashboard"], "127.0.0.1", 8090, 10, True),
    (["dashboard", "--lan", "--no-browser", "--port", "8090"], "0.0.0.0", 8090, 1, False),
])
def test_serve_listens_and_opens_the_browser_as_its_install_does(tmp_path, served, dashboard_config, argv, host,
                                                                  port, attempts, browser):
    paths = ["--config", str(dashboard_config), "--secrets", str(tmp_path / "none.env"),
             "--state-dir", str(tmp_path / "data")]
    assert main([*argv, *paths]) == 0
    assert (served["host"], served["port"], served["attempts"]) == (host, port, attempts)
    assert bool(served.get("opened")) is browser
    assert served["app"].hub is not None  # its dashboard view is on, and set up


def test_serve_on_a_pi_not_set_up_shows_no_dashboard_and_hands_setup_to_the_fetch_service(tmp_path, served,
                                                                                         monkeypatch):
    from departureboard.setup.spool.runner import SpoolRunner

    run = tmp_path / "run"
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    config = installed_template(tmp_path)
    (tmp_path / "state").mkdir()
    assert main(["serve", "--install", "pi", "--config", str(config), "--secrets", str(tmp_path / "none.env")]) == 0
    assert (served["host"], served["port"], served["attempts"]) == ("0.0.0.0", 8090, 1)
    app = served["app"]
    assert not app.dashboard_on() and app.settings.install.name == "pi" and not served.get("opened")
    assert ask(app, "/") == (303, "/settings", b"")
    assert isinstance(app.settings.setup._runner, SpoolRunner)
    assert app.settings.store.folder == tmp_path / "state"


@pytest.fixture
def pi_with_the_dashboard(tmp_path, monkeypatch, dashboard_config):
    """A Pi set up, with its spool in memory and the dashboard view on; no pollers may run in serve."""
    from departureboard.dashboard import collector

    monkeypatch.setattr(collector, "Collector", lambda *args, **kwargs: pytest.fail("pollers in serve"))
    run = tmp_path / "run"
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "settings.toml").write_text('[app]\nviews = ["board", "dashboard"]\n')
    args = ["serve", "--install", "pi", "--config", str(dashboard_config), "--secrets", str(tmp_path / "none.env"),
            "--state-dir", str(folder)]
    return args, folder, run


def test_serve_on_a_pi_shows_the_dashboard_the_fetch_service_hands_over(tmp_path, served, pi_with_the_dashboard,
                                                                         capsys):
    import fcntl

    from departureboard.core.walk import WalkTimes
    from departureboard.dashboard.history import History
    from departureboard.dashboard.snapshot import Hub
    from departureboard.fetch import handoff
    from departureboard.setup.spool.folder import Spool

    args, folder, run = pi_with_the_dashboard
    assert main(args) == 0
    app = served["app"]
    assert isinstance(app.hub, handoff.SnapshotMirror) and app.dashboard_on()
    assert "Dashboard at" in capsys.readouterr().out
    assert ask(app, "/healthz") == (503, None, b"unavailable")  # nothing handed over yet
    with open(folder / "dashboard.lock", "a") as lock:  # the fetch service's to take, not serve's
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not (folder / "history.sqlite3").exists()  # serve makes none: only reads the fetch service's
    hub = Hub("Glossop", WalkTimes())
    hub.publish("trains", {"rows": [{"id": "9f1c000000000002"}]}, {"9f1c000000000002": {"history": {
        "mode": "train", "route": "GLO|Manchester", "scheduled": "07:12", "label": "07:12 to Manchester",
        "day_type": "weekday", "phrase": None}}})
    handoff.SnapshotWriter(hub, Spool(run), lambda: True).write()
    app.hub.check()
    assert ask(app, "/healthz") == (200, None, b"ok")
    assert json.loads(ask(app, "/api/snapshot")[2])["trains"]["rows"] == [{"id": "9f1c000000000002"}]
    assert json.loads(ask(app, "/api/history/9f1c000000000002")[2])["days"] == []  # no history yet
    History(folder).close()  # the fetch service starts one
    assert ask(app, "/api/history/9f1c000000000002")[0] == 200
    assert os.listdir(run / "requests")  # the browser was said to the fetch service


def test_a_pi_turns_its_dashboard_on_and_off_without_starting_again(tmp_path, served, pi_with_the_dashboard):
    args, folder, _ = pi_with_the_dashboard
    (folder / "settings.toml").write_text('[app]\nviews = ["board"]\n')
    assert main(args) == 0
    app = served["app"]
    assert ask(app, "/") == (303, "/settings", b"") and ask(app, "/healthz") == (200, None, b"ok")
    assert ask(app, "/api/snapshot")[0] == 404
    (folder / "settings.toml").write_text('[app]\nviews = ["board", "dashboard"]\n')  # saved on the General page
    assert ask(app, "/")[0] == 200 and ask(app, "/api/snapshot")[0] == 200
    assert ask(app, "/healthz")[0] == 503  # until the fetch service hands a snapshot over


def test_a_pi_with_broken_settings_serves_them_without_a_word_about_a_dashboard(tmp_path, served, monkeypatch,
                                                                                  capsys):
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "lib"))
    config = tmp_path / "config.toml"
    config.write_text("display = [\n")
    assert main(["web", "--config", str(config), "--secrets", str(tmp_path / "none.env")]) == 0
    assert "dashboard" not in capsys.readouterr().err.lower()
    assert served["app"].settings.problem == "" and not served["app"].dashboard_on()


def test_serve_starts_no_dashboard_until_there_is_something_to_show(tmp_path, served, capsys):
    # A new container: no config.toml, nothing saved. The settings open the wizard.
    folder = tmp_path / "data"
    assert main(["serve", "--install", "docker", "--config", str(tmp_path / "missing.toml"),
                 "--secrets", str(tmp_path / "none.env"), "--state-dir", str(folder)]) == 0
    err = capsys.readouterr().err
    assert "opens the setup wizard" in err and "Settings: no password yet" in err and "in this log" in err
    app = served["app"]
    assert app.hub is None and app.settings.problem == ""
    assert ask(app, "/") == (303, "/settings", b"")
    assert not (folder / "history.sqlite3").exists()


FIRST_RUNS = {
    "missing": None,
    "blank": "",
    "example": (ROOT / "examples/config.example.toml").read_text(),
}


@pytest.mark.parametrize("install", ["docker", "pi"])
@pytest.mark.parametrize("config", sorted(FIRST_RUNS))
def test_a_first_run_serves_the_settings_and_signing_in_opens_the_wizard(tmp_path, served, monkeypatch, capsys,
                                                                         install, config):
    # Docker's first start (no config.toml), a blank one, and the template as installed on a Pi.
    from departureboard.web.auth import PasswordStore

    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))  # the Pi's spool, in memory
    path, folder = tmp_path / "config.toml", tmp_path / "data"
    if FIRST_RUNS[config] is not None:
        path.write_text(FIRST_RUNS[config])
    assert main(["serve", "--install", install, "--config", str(path), "--secrets", str(tmp_path / "none.env"),
                 "--state-dir", str(folder)]) == 0
    assert "Not set up yet: sign in and the page opens the setup wizard" in capsys.readouterr().err
    app = served["app"]
    # Nothing is fetched until it is set up: no hub in-process, and a Pi's
    # snapshot mirror stays switched off.
    if install == "pi":
        assert not app.dashboard_on()
    else:
        assert app.hub is None
    assert ask(app, "/") == (303, "/settings", b"")
    assert ask(app, "/healthz") == (200, None, b"ok")
    PasswordStore(folder / "web.json").set(DASH_PASSWORD)
    status, where, _ = ask(app, "/settings/login", {"password": DASH_PASSWORD})
    assert (status, where) == (303, "/settings/setup/")


class Browser:
    """Just enough of a browser for the settings site: one host, its cookies
    by name, and same-origin requests."""

    def __init__(self, port):
        self.port, self.cookies = port, {}

    def __call__(self, method, path, form=None, json_body=None, headers=None):
        from urllib.parse import urlencode

        sent = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}", **(headers or {})}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = None
        if form is not None:
            body, sent["Content-Type"] = urlencode(form), "application/x-www-form-urlencoded"
        if json_body is not None:
            body, sent["Content-Type"] = json.dumps(json_body), "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body, sent)
        response = conn.getresponse()
        text = response.read().decode()
        conn.close()
        for header in response.headers.get_all("Set-Cookie") or []:
            name, _, value = header.split(";")[0].partition("=")
            self.cookies[name] = value
        return response, text


def test_dockers_first_run_claims_in_the_log_sets_up_and_starts_the_dashboard_again(tmp_path, monkeypatch, caplog):
    # A new container: no config.toml, nothing saved, the key from the environment.
    import re

    from departureboard.cli import serve
    from departureboard.core import net
    from departureboard.dashboard import collector
    from departureboard.web import server as dashserver

    def offline(request, timeout):
        raise OSError("offline in tests")

    monkeypatch.setattr(net, "urlopen", offline)
    monkeypatch.setattr(serve, "SETTINGS_CHECK_S", 0.05)
    started_again = []
    monkeypatch.setattr(serve, "_start_again", started_again.append)
    monkeypatch.setenv("RDM_API_KEY", "from-the-environment")
    caplog.set_level("INFO")
    folder, port = tmp_path / "data", free_port()
    argv = ["serve", "--install", "docker", "--config", str(tmp_path / "missing.toml"),
            "--secrets", str(tmp_path / "none.env"), "--state-dir", str(folder), "--port", str(port)]
    seen = {}

    def owner():
        try:
            setting_up()
        finally:
            if "finish" not in seen:  # something failed: stop the server, as Ctrl+C does, so the test ends
                import signal

                os.kill(os.getpid(), signal.SIGINT)

    def setting_up():
        assert answering(port)
        browser = Browser(port)
        response, _ = browser("GET", "/")
        seen["home"] = (response.status, response.getheader("Location"))
        # Claim the settings with a code from the container's log.
        _, page = browser("GET", "/settings/login")
        token = re.search(r'name="form" value="([^"]+)"', page).group(1)
        browser("POST", "/settings/forgot", {"form": token})
        code = "".join(re.search(r"Settings reset code: (\d{3}) (\d{3})", caplog.text).groups())
        _, page = browser("GET", "/settings/reset?asked")
        token = re.search(r'name="form" value="([^"]+)"', page).group(1)
        response, _ = browser("POST", "/settings/reset", {"form": token, "code": code, "password": DASH_PASSWORD,
                                                          "again": DASH_PASSWORD})
        seen["claimed"] = (response.status, response.getheader("Location"))
        # The wizard, as its page calls it.
        csrf = json.loads(browser("GET", "/settings/setup/api/session")[1])["csrf"]
        step = {"X-CSRF-Token": csrf}
        seen["begin"] = json.loads(browser("POST", "/settings/setup/api/begin", json_body={}, headers=step)[1])
        response, text = browser("POST", "/settings/setup/api/finish", headers=step, json_body={
            "home": {"lat": 53.443, "lon": -1.949}, "station_crs": "GLO", "stops": {}, "walk": {"GLO": 6},
            "views": ["dashboard"]})
        seen["finish"] = (response.status, json.loads(text))

    helper = threading.Thread(target=owner, daemon=True)
    helper.start()
    assert main(argv) == 0  # until the settings saved start it again
    helper.join(timeout=10)
    assert seen["home"] == (303, "/settings")
    assert seen["claimed"] == (303, "/settings/setup/")
    assert seen["begin"]["noun"] == "dashboard" and seen["begin"]["views"]["on"] == ["dashboard"]
    status, finished = seen["finish"]
    assert status == 200 and finished["message"] == "Saved. The dashboard starts again in a few seconds to use them."
    assert finished["links"][0] == {"label": "The dashboard", "href": "/"}
    assert started_again == [port] and "starting the dashboard again" in caplog.text

    # Started again, as _start_again would: now the Collector runs.
    started, made = [], {}
    start = collector.Collector.start
    monkeypatch.setattr(collector.Collector, "start", lambda self: started.append(self) or start(self))
    monkeypatch.setattr(dashserver, "make_server", lambda app, host, port, attempts=1: made.update(app=app) or Stopped())
    assert main(argv) == 0
    assert made["app"].hub is not None and len(started) == 1


def test_serve_with_the_dashboard_view_off_runs_only_the_settings(tmp_path, served, dashboard_config):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "settings.toml").write_text("[app]\nviews = []\n")
    assert main(["serve", "--install", "docker", "--config", str(dashboard_config), "--secrets",
                 str(tmp_path / "none.env"), "--state-dir", str(folder)]) == 0
    assert served["app"].hub is None
    assert served["app"].settings.tabs() == ("overview", "trains_buses", "keys", "general")


@pytest.mark.parametrize("install", ["docker", "desktop"])
def test_serve_sets_the_password_and_prints_a_code_in_the_store(tmp_path, typed, no_dashboard, capsys, install):
    import re

    from departureboard.web.auth import PasswordStore, ResetCodes

    typed[0].extend([DASH_PASSWORD] * 2)
    folder = tmp_path / "data"
    assert main(["serve", "--install", install, "--set-password", "--state-dir", str(folder)]) == 0
    assert typed[1] == ["New dashboard settings password: ", "Again: "]
    assert PasswordStore(folder / "web.json").check(DASH_PASSWORD)
    assert main(["serve", "--install", install, "--reset-code", "--state-dir", str(folder)]) == 0
    out = capsys.readouterr().out
    code = "".join(re.search(r"Settings reset code: (\d{3}) (\d{3})", out).groups())
    assert "/settings/reset" in out and not (folder / "reset-display.json").exists()
    assert ResetCodes(folder).verify(code, time.time())


def test_serve_password_flags_are_one_or_the_other(tmp_path):
    with pytest.raises(SystemExit):
        main(["serve", "--state-dir", str(tmp_path), "--set-password", "--reset-code"])


def test_serve_knows_only_the_installs_there_are(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["serve", "--install", "laptop", "--state-dir", str(tmp_path)])
    assert "invalid choice: 'laptop'" in capsys.readouterr().err


def test_the_old_dashboard_command_is_hidden_but_works(capsys):
    import re

    from departureboard.cli.main import _parser

    help_text = _parser().format_help()
    listed = re.search(r"\{([a-z,]+)\}", help_text).group(1).split(",")
    assert "serve" in listed and "dashboard" not in listed
    assert not re.search(r"^ +dashboard ", help_text, re.M)
    args = _parser().parse_args(["dashboard", "--state-dir", "/data", "--set-password"])
    assert (args.install, args.state_dir, args.set_password) == ("desktop", "/data", True)


def test_the_fetch_service_is_listed_and_its_old_name_hidden_but_works():
    import re

    from departureboard.cli.main import _parser
    from departureboard.cli.serve import fetch_command

    parser = _parser()
    listed = re.search(r"\{([a-z,]+)\}", parser.format_help()).group(1).split(",")
    assert "fetch" in listed
    setup = parser._subparsers._group_actions[0].choices["setup"]
    assert "serve" not in setup.format_help()
    for argv in (["fetch"], ["setup", "serve"]):
        args = parser.parse_args([*argv, "--config", "/etc/departureboard/config.toml"])
        assert (args.handler, args.install, args.state_dir) == (fetch_command, "pi", None)
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch", "--install", "docker"])  # Docker and a desktop fetch in-process


def _query(tmp_path, what, config="", saved=""):
    """`departureboard query` as the Pi's scripts ask it, with the board's store at tmp_path/state."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (tmp_path / "config.toml").write_text(f'state_file = "{state / "state.json"}"\n{config}')
    if saved:
        (state / "settings.toml").write_text(saved)
    return main(["query", what, "--install", "pi", "--config", str(tmp_path / "config.toml")])


@pytest.mark.parametrize("config, saved, out", [
    ("", "", "board\n"),  # a Pi's view unless [app] views says
    ("", '[app]\nviews = ["board", "dashboard"]\n', "board\ndashboard\n"),  # switched on in the settings
    ('[app]\nviews = ["dashboard"]\n', "", "dashboard\n"),
    ('[app]\nviews = ["board"]\n', "[app]\nviews = []\n", ""),  # none: the settings site only
])
def test_query_views_prints_each_view_on_one_line(tmp_path, capsys, config, saved, out):
    assert _query(tmp_path, "views", config, saved) == 0
    assert capsys.readouterr().out == out


@pytest.mark.parametrize("config, saved, out", [
    ("", "", "inky\n"),
    ('display = "hdmi-1280x400"\n', "", "hdmi-1280x400\n"),
    ('display = "waveshare-10in85g"\n', 'display = "hdmi-1280x400"\n', "waveshare-10in85g\n"),  # config.toml's alone
])
def test_query_display_prints_the_display_config_toml_sets(tmp_path, capsys, config, saved, out):
    assert _query(tmp_path, "display", config, saved) == 0
    assert capsys.readouterr().out == out


@pytest.mark.parametrize("config, saved, wanted", [
    ('[bus]\nsource = "bods"\n', "", True),
    ("[bus]\nsource = \"transportapi\"\n", '[bus]\nsource = "bods"\n', True),  # chosen on the settings site or in the wizard
    ('[bus]\nsource = "bods"\n', "[bus]\nsource = \"transportapi\"\nenabled = false\n", False),  # buses switched off since
    ("[bus]\nsource = \"transportapi\"\n", "", False),  # TransportAPI
    ('[train]\ncrs = "GLO"\n', "", False),  # no buses at all
])
def test_query_needs_timetable_says_yes_or_no_and_exits_so(tmp_path, capsys, config, saved, wanted):
    assert _query(tmp_path, "needs-timetable", config, saved) == (0 if wanted else 1)
    assert capsys.readouterr().out == ("yes\n" if wanted else "no\n")


@pytest.mark.parametrize("what, config, said", [
    ("views", '[app]\nviews = "board"\n', "[app] views must be a list of views"),
    ("display", 'display = "hdmi"\n', "Unknown display 'hdmi'"),
    ("needs-timetable", "[bus\n", "Expected ']' at the end of a table declaration"),
])
def test_query_says_why_when_the_settings_cant_be_read(tmp_path, capsys, what, config, said):
    (tmp_path / "config.toml").write_text(config)
    assert main(["query", what, "--install", "pi", "--config", str(tmp_path / "config.toml"),
                 "--state-dir", str(tmp_path / "state")]) == 2
    out = capsys.readouterr()
    assert said in out.err and out.out == ""


def test_query_needs_no_keys_and_is_hidden(tmp_path, monkeypatch, capsys):
    import re

    from departureboard.cli.main import _parser

    for name in ("RDM_API_KEY", "BODS_API_KEY", "TAPI_APP_ID", "TAPI_APP_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert _query(tmp_path, "views") == 0 and capsys.readouterr().out == "board\n"
    assert not (tmp_path / "state" / "settings.toml").exists()  # nothing written
    listed = re.search(r"\{([a-z,]+)\}", _parser().format_help()).group(1).split(",")
    assert "query" not in listed
    with pytest.raises(SystemExit):
        _parser().parse_args(["query", "keys"])


class FakeServer:
    def __init__(self):
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


@pytest.mark.parametrize("running, saved, restarts", [
    (True, 'title = "Our house"\n', True),  # new settings for the dashboard
    (True, "[app]\nviews = []\n", True),  # the dashboard turned off
    (False, 'title = "Our house"\n', True),  # it couldn't start, and now can
    (False, '[app]\nviews = []\ntitle = "Our house"\n', False),  # no dashboard before or after
    (True, "[train]\ncrs = 12\n", False),  # doesn't load: the running one stays
])
def test_serve_starts_again_only_when_the_dashboard_should_start_stop_or_change(
        dashboard_config, tmp_path, monkeypatch, running, saved, restarts):
    from departureboard.cli import serve
    from departureboard.settings.installs import install_named
    from departureboard.settings.store import Store

    monkeypatch.setattr(serve, "SETTINGS_CHECK_S", 0.02)
    folder = tmp_path / "data"
    folder.mkdir()
    store = Store.open(dashboard_config, tmp_path / "none.env", state_dir=folder)
    server = FakeServer()
    watch = serve._SettingsWatch(store, install_named("docker"), server, running)
    watch.start()
    try:
        time.sleep(0.05)
        (folder / "settings.toml").write_text(saved)
        assert server.stopped.wait(1.0 if restarts else 0.3) is restarts
        assert watch.restart is restarts
    finally:
        watch.stop()


# The dashboard's settings password and reset codes, from the command line

DASH_PASSWORD = "a long enough password"


@pytest.fixture
def typed(monkeypatch):
    """What getpass answers, in turn; every prompt it was asked with."""
    import getpass

    answers, prompts = [], []

    def ask(prompt=""):
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(getpass, "getpass", ask)
    return answers, prompts


@pytest.fixture
def no_dashboard(monkeypatch):
    """Fails the test if the dashboard itself would start."""
    from departureboard.dashboard import config as dashconfig
    from departureboard.web import server as dashserver

    def started(*args, **kwargs):
        raise AssertionError("the dashboard started")

    monkeypatch.setattr(dashconfig, "load_dashboard", started)
    monkeypatch.setattr(dashserver, "make_server", started)


def test_dashboard_set_password_needs_only_its_folder_as_in_docker(tmp_path, typed, no_dashboard, capsys):
    from departureboard.web.auth import PasswordStore

    answers, prompts = typed
    answers += [DASH_PASSWORD, DASH_PASSWORD]
    folder = tmp_path / "data"
    # As `docker exec … dashboard --set-password --state-dir /data`: no config.toml or secrets.env there.
    assert main(["dashboard", "--set-password", "--state-dir", str(folder)]) == 0
    assert prompts == ["New dashboard settings password: ", "Again: "]
    assert PasswordStore(folder / "web.json").check(DASH_PASSWORD)
    assert (folder.stat().st_mode & 0o777, (folder / "web.json").stat().st_mode & 0o777) == (0o700, 0o600)
    out = capsys.readouterr().out
    assert f"Password saved to {folder / 'web.json'}" in out and DASH_PASSWORD not in out
    assert sorted(p.name for p in folder.iterdir()) == ["web.json"]  # nothing else written


def test_dashboard_set_password_finds_the_folder_as_the_dashboard_does(tmp_path, typed, no_dashboard,
                                                                        monkeypatch):
    from departureboard.settings import store
    from departureboard.web.auth import PasswordStore

    answers, _ = typed
    config = tmp_path / "config.toml"
    config.write_text(f'[train]\ncrs = "GLO"\n[dashboard]\nstate_dir = "{tmp_path / "named"}"\n')
    answers += [DASH_PASSWORD] * 2
    assert main(["dashboard", "--config", str(config), "--set-password"]) == 0
    assert PasswordStore(tmp_path / "named" / "web.json").check(DASH_PASSWORD)
    # --state-dir wins over [dashboard] state_dir, as when running.
    answers += [DASH_PASSWORD] * 2
    assert main(["dashboard", "--config", str(config), "--set-password", "--state-dir", str(tmp_path / "given")]) == 0
    assert (tmp_path / "given" / "web.json").exists()
    # Neither: this computer's own folder.
    monkeypatch.setattr(store, "_home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    config.write_text('[train]\ncrs = "GLO"\n')
    answers += [DASH_PASSWORD] * 2
    assert main(["dashboard", "--config", str(config), "--set-password"]) == 0
    assert PasswordStore(store.platform_folder() / "web.json").check(DASH_PASSWORD)
    assert store.platform_folder().name == "departureboard"
    assert str(store.platform_folder()).startswith(str(tmp_path))


def test_dashboard_set_password_without_a_folder_or_config_uses_this_computer_s_folder(tmp_path, typed,
                                                                                      no_dashboard):
    from departureboard.settings.store import platform_folder
    from departureboard.web.auth import PasswordStore

    typed[0].extend([DASH_PASSWORD] * 2)
    assert main(["dashboard", "--config", str(tmp_path / "missing.toml"), "--set-password"]) == 0
    assert PasswordStore(platform_folder() / "web.json").check(DASH_PASSWORD)


def test_dashboard_set_password_uses_the_same_password_as_the_boards_page(tmp_path, typed, no_dashboard, capsys):
    import re

    from departureboard.web.auth import PasswordStore, ResetCodes, show_in_log

    config, state = _web_config(tmp_path)
    typed[0].extend([DASH_PASSWORD] * 2)
    assert main(["dashboard", "--config", str(config), "--set-password"]) == 0
    assert PasswordStore(state / "web.json").check(DASH_PASSWORD)  # what `web` checks
    assert main(["web", "--config", str(config), "--reset-code"]) == 0
    code = "".join(re.search(r"Settings reset code: (\d{3}) (\d{3})", capsys.readouterr().out).groups())
    assert ResetCodes(state, show=show_in_log).verify(code, time.time())  # and the dashboard's codes


@pytest.mark.parametrize("first, again, says", [
    ("too short", "too short", "Use at least 10 characters"),
    (DASH_PASSWORD, "something else entirely", "Those didn't match"),
])
def test_dashboard_set_password_refuses_a_bad_one(tmp_path, typed, capsys, first, again, says):
    from departureboard.web.auth import PasswordStore

    folder = tmp_path / "data"
    PasswordStore(folder / "web.json").set("the old password")
    typed[0].extend([first, again])
    assert main(["dashboard", "--set-password", "--state-dir", str(folder)]) == 1
    assert says in capsys.readouterr().err
    assert PasswordStore(folder / "web.json").check("the old password")


def test_dashboard_set_password_works_beside_a_running_dashboard(dashboard_config, tmp_path, typed):
    from departureboard.dashboard.config import load_dashboard
    from departureboard.web.auth import PasswordStore

    folder = tmp_path / "dash"
    running = load_dashboard(dashboard_config, {"RDM_API_KEY": "k"}, folder)  # holds the folder's lock
    try:
        typed[0].extend([DASH_PASSWORD] * 2)
        assert main(["dashboard", "--config", str(dashboard_config), "--set-password", "--state-dir", str(folder)]) == 0
    finally:
        running.close()
    assert PasswordStore(folder / "web.json").check(DASH_PASSWORD)


def test_dashboard_password_and_code_write_only_in_the_state_folder(tmp_path, typed, monkeypatch, capsys):
    # As in the container, where the root is read-only and only /data (the
    # working folder too) can be written: everything around it is read-only here.
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    monkeypatch.chdir(data)
    for name in ("HOME", "TMPDIR", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(root))
    root.chmod(0o500)
    try:
        typed[0].extend([DASH_PASSWORD] * 2)
        assert main(["dashboard", "--set-password", "--state-dir", str(data)]) == 0
        assert main(["dashboard", "--reset-code", "--state-dir", str(data)]) == 0
    finally:
        root.chmod(0o700)
    assert sorted(p.name for p in root.iterdir()) == ["data"]
    assert sorted(p.name for p in data.iterdir()) == ["reset.json", "reset.lock", "web.json"]


@pytest.mark.parametrize("flags", [["--set-password", "--reset-code"], ["--reset-code", "--set-password"]])
def test_dashboard_password_flags_are_one_or_the_other(tmp_path, flags):
    with pytest.raises(SystemExit):
        main(["dashboard", "--state-dir", str(tmp_path), *flags])


def test_dashboard_reset_code_prints_one_code_the_settings_will_take(tmp_path, no_dashboard, capsys, caplog):
    import re

    from departureboard.web.auth import ResetCodes, show_in_log

    caplog.set_level("DEBUG")
    folder = tmp_path / "data"
    ResetCodes(folder, show=show_in_log).issue(time.time())  # someone just asked for one on the page
    caplog.clear()
    assert main(["dashboard", "--reset-code", "--state-dir", str(folder)]) == 0  # no page limits here
    captured = capsys.readouterr()
    codes = re.findall(r"Settings reset code: (\d{3}) (\d{3})", captured.out)
    assert len(codes) == 1 and "valid until" in captured.out and "/settings/reset" in captured.out
    code = "".join(codes[0])
    assert code not in captured.err and code not in caplog.text  # printed once, never logged
    assert not (folder / "reset-display.json").exists()
    # The dashboard's settings check codes in the same folder.
    assert ResetCodes(folder, show=show_in_log).verify(code, time.time())


@pytest.mark.parametrize("install, noun, offered, on, links", [
    ("pi", "board", ["board", "dashboard"], ["board"], ["Settings"]),  # a Pi offers both, the board on at first
    ("docker", "dashboard", ["dashboard"], ["dashboard"], ["The dashboard", "Settings"]),
    ("desktop", "dashboard", ["dashboard"], ["dashboard"], ["The dashboard", "Settings"]),
])
def test_the_setup_wizard_is_one_page_on_every_install_in_its_own_words(dashboard_config, tmp_path, install, noun,
                                                                        offered, on, links):
    from departureboard.cli import serve
    from departureboard.settings.installs import install_named
    from departureboard.settings.schema import keys
    from departureboard.settings.store import Store

    folder = tmp_path / "dash"
    folder.mkdir()
    store = Store.open(dashboard_config, tmp_path / "nonexistent", state_dir=folder, install=install)
    site = serve._settings_site(store, install_named(install), install != "pi")
    status, body = site.setup.handle("begin", {})
    assert status == 200 and body["title"] == "Set up your departures" and body["noun"] == noun
    # The keys every view uses; TransportAPI's and the dashboard's own are on their tabs.
    assert [k["name"] for k in body["keys"]] == [name for name, _, _ in keys("general")]
    assert [view["name"] for view in body["views"]["offered"]] == offered and body["views"]["on"] == on
    assert [link["label"] for link in body["links"]] == links


# --- A first run: the board shows where to set it up, rather than failing ---

TEMPLATE = ROOT / "examples/config.example.toml"


def installed_template(tmp_path, display=None):
    text = TEMPLATE.read_text().replace("/var/lib/departureboard/state.json", str(tmp_path / "state" / "state.json"))
    if display:
        text = text.replace('display = "inky"', f'display = "{display}"')
    config = tmp_path / "config.toml"
    config.write_text(text)
    return config


def first_run_args(tmp_path, config, *extra):
    return ["run", "--config", str(config), "--secrets", str(tmp_path / "none.env"),
            "--png", str(tmp_path / "board.png"), *extra]


def saved_page(folder):
    return json.loads((folder / "state.json").read_text())["last_render"]["page"]


def test_a_template_config_shows_the_setup_page_instead_of_failing(tmp_path, monkeypatch, caplog):
    from departureboard.cli import common

    caplog.set_level("INFO")
    monkeypatch.setattr(common.socket, "gethostname", lambda: "kitchen-board")
    config = installed_template(tmp_path)
    for _ in range(3):  # the timer, minute after minute: never a failure
        assert main(first_run_args(tmp_path, config)) == 0
    assert Image.open(tmp_path / "board.png").size == (250, 122)
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["last_render"]["page"] == "setup" and "sources" not in state
    assert "http://kitchen-board.local:8090/settings" in caplog.text
    # Drawn once: e-paper wears, and the page doesn't change.
    rendered = [r.getMessage() for r in caplog.records if r.getMessage().startswith("rendered=")]
    assert rendered == ["rendered=True", "rendered=False", "rendered=False"]


@pytest.mark.parametrize("config", sorted(FIRST_RUNS))
def test_a_pi_not_set_up_shows_set_me_up_with_the_settings_address(tmp_path, monkeypatch, caplog, config):
    from departureboard.cli import common

    caplog.set_level("INFO")
    monkeypatch.setattr(common.socket, "gethostname", lambda: "kitchen-board")
    folder = tmp_path / "lib"
    folder.mkdir()
    monkeypatch.setenv("STATE_DIRECTORY", str(folder))  # as systemd sets it for the board's units
    path = tmp_path / "config.toml"
    if FIRST_RUNS[config] is not None:
        path.write_text(FIRST_RUNS[config])
    assert main(first_run_args(tmp_path, path)) == 0
    assert "Not set up yet: showing http://kitchen-board.local:8090/settings on the board" in caplog.text
    assert saved_page(folder) == "setup"


@pytest.mark.parametrize("display, size", [("waveshare-10in85g", (1360, 480)), ("hdmi-1280x400", (1280, 400))])
def test_the_setup_page_is_drawn_for_the_configured_display(tmp_path, display, size):
    config = installed_template(tmp_path, display)
    assert main(first_run_args(tmp_path, config)) == 0
    assert Image.open(tmp_path / "board.png").size == size


def test_without_a_config_the_setup_page_keeps_its_state_in_systemds_folder(tmp_path, monkeypatch):
    folder = tmp_path / "lib"
    folder.mkdir()
    monkeypatch.setenv("STATE_DIRECTORY", str(folder))
    assert main(first_run_args(tmp_path, tmp_path / "missing.toml")) == 0
    assert Image.open(tmp_path / "board.png").size == (250, 122)
    assert saved_page(folder) == "setup"


def test_a_code_asked_for_shows_on_a_board_not_yet_set_up(tmp_path):
    import time

    from departureboard.web.auth import ResetCodes

    config = installed_template(tmp_path)
    assert main(first_run_args(tmp_path, config)) == 0
    ResetCodes(tmp_path / "state").issue(time.time())
    assert main(first_run_args(tmp_path, config)) == 0
    assert saved_page(tmp_path / "state") == "code"


def test_a_screen_not_yet_set_up_keeps_running(tmp_path):
    config = installed_template(tmp_path, "hdmi-1280x400")
    assert main(first_run_args(tmp_path, config, "--loop", "--ticks", "3", "--tick", "0")) == 0
    assert Image.open(tmp_path / "board.png").size == (1280, 400)
    assert saved_page(tmp_path / "state") == "setup"


def test_a_screen_starts_again_once_set_up(tmp_path, monkeypatch):
    from departureboard.cli import run

    answers = iter([True, True, False])  # set up between the first tick and the second
    monkeypatch.setattr(run, "needs_setup", lambda config: next(answers))
    monkeypatch.setattr(run, "SETUP_CHECK_S", 0)
    config = installed_template(tmp_path, "hdmi-1280x400")
    # Once, for systemd's Restart=on-failure to start the board with its settings.
    assert main(first_run_args(tmp_path, config, "--loop", "--ticks", "10", "--tick", "0")) == run.RESTART
    assert saved_page(tmp_path / "state") == "setup"


def test_a_running_board_asks_whether_it_is_set_up_every_few_seconds(monkeypatch):
    from departureboard.cli import run

    asked = []
    monkeypatch.setattr(run, "needs_setup", lambda config: asked.append(config) or True)
    now = [100.0]
    check = run._SetUpYet("config.toml", clock=lambda: now[0])
    for step in (0, 1, 1, 1, 1, 1, 1):
        now[0] += step
        assert check() is None
    assert len(asked) == 2  # at 100 and 105
    monkeypatch.setattr(run, "needs_setup", lambda config: False)
    now[0] += run.SETUP_CHECK_S
    with pytest.raises(run._Restart):
        check()


# --- The board view switched off: the board says so and fetches nothing ---


def board_with_views(tmp_path, views, display=None):
    """A board set up with trains and TransportAPI buses, and [app] views saved in its store."""
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\n' + (f'display = "{display}"\n' if display else "")
                      + '[train]\ncrs = "GLO"\n[bus]\nsource = "transportapi"\n[bus.stops]\nA = "1000DGHS0900"\n')
    (state / "settings.toml").write_text(f"[app]\nviews = {json.dumps(views)}\n")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=rail\nTAPI_APP_ID=id\nTAPI_APP_KEY=tapi\n")
    return ["run", "--config", str(config), "--secrets", str(secrets), "--png", str(tmp_path / "board.png")]


@pytest.fixture
def fetched(monkeypatch):
    """Every fetch the board makes: trains, TransportAPI's buses and bank holidays."""
    from departureboard.cli import common
    from departureboard.sources.darwin import DarwinSource
    from departureboard.sources.transportapi import TransportApiBusSource

    calls = []
    monkeypatch.setattr(DarwinSource, "fetch", lambda self: calls.append("train") or [])
    monkeypatch.setattr(TransportApiBusSource, "fetch", lambda self: calls.append("bus") or [])
    monkeypatch.setattr(common, "bank_holidays", lambda *a, **kw: calls.append("holidays") or frozenset())
    return calls


def test_with_the_board_view_off_the_board_says_so_and_fetches_nothing(tmp_path, fetched, caplog):
    caplog.set_level("INFO")
    args = board_with_views(tmp_path, ["dashboard"])
    for _ in range(3):  # the timer, minute after minute
        assert main(args) == 0
    assert fetched == []
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    # No source polled, so no TransportAPI allowance spent.
    assert state["last_render"]["page"] == "setup" and "sources" not in state
    assert "board view is off" in caplog.text
    rendered = [r.getMessage() for r in caplog.records if r.getMessage().startswith("rendered=")]
    assert rendered == ["rendered=True", "rendered=False", "rendered=False"]  # drawn once: e-paper wears


def test_the_board_off_page_says_where_to_turn_it_on(tmp_path, fetched, monkeypatch):
    from departureboard.cli import common, run

    shown = []
    real = run.run_once
    monkeypatch.setattr(run, "run_once", lambda board, *a, **kw: shown.append(board) or real(board, *a, **kw))
    assert main(board_with_views(tmp_path, [])) == 0  # no views at all is off too
    (board,) = shown
    assert (board.setup.heading, board.setup.note) == ("Board off", "Turn it on in Settings:")
    assert board.setup.url == common.setup_url()
    assert board.sources == [] and board.weather is None and board.tracker is None


def test_with_the_board_view_on_the_board_fetches_as_ever(tmp_path, fetched):
    assert main(board_with_views(tmp_path, ["board", "dashboard"])) == 0
    assert "train" in fetched and "bus" in fetched
    assert json.loads((tmp_path / "state" / "state.json").read_text())["last_render"]["page"] != "setup"


def test_a_code_asked_for_still_shows_with_the_board_view_off(tmp_path, fetched):
    from departureboard.web.auth import ResetCodes

    args = board_with_views(tmp_path, ["dashboard"])
    assert main(args) == 0
    ResetCodes(tmp_path / "state").issue(time.time())
    assert main(args) == 0
    assert saved_page(tmp_path / "state") == "code" and fetched == []


def test_a_screen_with_the_board_view_off_starts_again_once_it_is_on(tmp_path, fetched, monkeypatch):
    from departureboard.cli import run

    answers = iter([False, False, False, True])  # turned on after a couple of ticks
    monkeypatch.setattr(run, "_board_on", lambda store: next(answers))
    monkeypatch.setattr(run, "SETUP_CHECK_S", 0)
    args = board_with_views(tmp_path, ["dashboard"], display="hdmi-1280x400")
    assert main([*args, "--loop", "--ticks", "10", "--tick", "0"]) == run.RESTART
    assert saved_page(tmp_path / "state") == "setup" and fetched == []


def test_a_screen_with_the_board_view_off_asks_every_few_seconds(tmp_path, monkeypatch):
    from departureboard.cli import run
    from departureboard.settings.store import Store

    board_with_views(tmp_path, ["dashboard"])
    store = Store.open(tmp_path / "config.toml")
    now = [100.0]
    check = run._ViewOn(store, clock=lambda: now[0])
    assert check() is None
    (tmp_path / "state" / "settings.toml").write_text('[app]\nviews = ["board"]\n')
    now[0] += 1
    assert check() is None  # not asked again yet
    now[0] += run.SETUP_CHECK_S
    with pytest.raises(run._Restart):
        check()


def test_a_running_board_starts_again_when_its_view_is_turned_off(tmp_path, fetched):
    import argparse

    from departureboard.cli.common import load_settings
    from departureboard.cli.run import _Restart, _Watcher

    run = board_with_views(tmp_path, ["board"])
    args = argparse.Namespace(config=run[2], secrets=run[4])
    watcher = _Watcher(args, load_settings(args))
    assert watcher() is None  # nothing changed
    (tmp_path / "state" / "settings.toml").write_text('[app]\nviews = ["dashboard"]\n')
    with pytest.raises(_Restart):
        watcher()  # to show the board view is off


def test_the_first_run_after_setting_up_fetches_everything(tmp_path, monkeypatch):
    args, calls = _run_setup(tmp_path, monkeypatch)
    # Only the set-me-up page was ever drawn: nothing fetched yet, so this is a fresh start.
    (tmp_path / "state.json").write_text(json.dumps({"last_render": {"signature": "x", "page": "setup"}}))
    assert main(args) == 0
    assert calls == [1]


def test_the_settings_open_the_wizard_until_the_board_is_set_up(tmp_path, monkeypatch, capsys, served):
    from departureboard.web.settingsite import PREFIX, SETUP

    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    config = installed_template(tmp_path)
    assert main(["web", "--config", str(config), "--secrets", str(tmp_path / "none.env")]) == 0
    assert "opens the setup wizard" in capsys.readouterr().err
    site = served["app"].settings
    assert "" in site._files and site._home() == SETUP
    config.write_text(config.read_text().replace('crs = "CHANGE_ME"', 'crs = "GLO"')
                      .replace('A = "CHANGE_ME"', 'A = "1800SB12341"')
                      .replace('operators = ["CHANGE_ME"]', 'operators = ["SBUS"]'))
    assert site._home() == PREFIX


@pytest.fixture
def no_runtime_folder(tmp_path, monkeypatch):
    """No systemd RuntimeDirectory, and none left in /run: no folder in memory for the setup spool."""

    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setattr("departureboard.setup.spool.folder.RUNTIME", tmp_path / "no-run")


@pytest.mark.parametrize("flag", [[], ["--setup-via-spool"]])
def test_the_pis_settings_hand_setup_to_the_fetch_service(tmp_path, monkeypatch, flag, no_runtime_folder, served):
    # Always, now: the flag the unit still passes is taken and changes nothing.
    from departureboard.setup.spool.runner import SpoolRunner

    config = installed_template(tmp_path)
    (tmp_path / "state").mkdir()  # as install.sh and systemd's StateDirectory make it
    run = tmp_path / "run"
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))  # as systemd gives it
    assert main(["web", "--config", str(config), "--secrets", str(tmp_path / "none.env"), *flag]) == 0
    assert (served["host"], served["port"], served["attempts"]) == ("0.0.0.0", 8090, 1)  # the old 8080 no more
    site = served["app"].settings
    assert site.install.name == "pi" and not served["app"].dashboard_on()
    runner = site.setup._runner
    assert isinstance(runner, SpoolRunner)
    assert runner.spool.folder == run and runner.spool.requests.is_dir()
    assert sorted(os.listdir(tmp_path / "state")) == []  # nothing of setup's on the SD card


@pytest.mark.parametrize("command", [["fetch"], ["setup", "serve"]])  # and by its old name, hidden
def test_the_fetch_service_searches_in_the_boards_state_folder(tmp_path, monkeypatch, no_runtime_folder, command):
    from departureboard.setup.spool.worker import Worker

    ran = []
    monkeypatch.setattr(Worker, "run", lambda self, tend=None: ran.append(self) or 0)
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    config = installed_template(tmp_path)
    secrets = tmp_path / "secrets.env"
    secrets.write_text("BODS_API_KEY=saved-bods-key\n")
    assert main([*command, "--config", str(config), "--secrets", str(secrets)]) == 0
    [worker] = ran
    assert worker.spool.folder == tmp_path / "run" and worker._job_folder == tmp_path / "state"
    assert worker._load_secrets()["BODS_API_KEY"] == "saved-bods-key"
    assert isinstance(worker._settings(), dict)


@pytest.mark.parametrize("flag", [[], ["--setup-via-spool"]])
def test_without_a_folder_in_memory_the_setup_spool_is_refused(tmp_path, monkeypatch, capsys, no_runtime_folder,
                                                               flag):
    # Never on the SD card, where flash can keep what was deleted: a request can hold home's position or a key.
    from departureboard.setup.spool.worker import Worker
    from departureboard.web import server as dashserver

    monkeypatch.setattr(dashserver, "make_server", lambda *args, **kwargs: pytest.fail("served"))
    monkeypatch.setattr(Worker, "run", lambda self: pytest.fail("ran"))
    config = installed_template(tmp_path)
    (tmp_path / "state").mkdir()
    none = str(tmp_path / "none.env")
    assert main(["web", "--config", str(config), "--secrets", none, *flag]) == 2
    assert "RUNTIME_DIRECTORY" in capsys.readouterr().err
    assert main(["serve", "--install", "pi", "--config", str(config), "--secrets", none]) == 2
    assert "RUNTIME_DIRECTORY" in capsys.readouterr().err
    assert main(["setup", "serve", "--config", str(config), "--secrets", none]) == 2
    assert "RUNTIME_DIRECTORY" in capsys.readouterr().err
    assert main(["fetch", "--config", str(config), "--secrets", none]) == 2
    assert "RUNTIME_DIRECTORY" in capsys.readouterr().err
    assert os.listdir(tmp_path / "state") == []


def test_the_page_and_the_fetch_service_share_systemds_folder_in_memory(tmp_path, monkeypatch, served):
    from departureboard.setup.spool.worker import Worker

    run = tmp_path / "run"
    run.mkdir(mode=0o700)  # as systemd makes RuntimeDirectory=
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    ran = []
    monkeypatch.setattr(Worker, "run", lambda self, tend=None: ran.append(self) or 0)
    config = installed_template(tmp_path)
    (tmp_path / "state").mkdir()
    none = str(tmp_path / "none.env")
    assert main(["web", "--config", str(config), "--secrets", none, "--setup-via-spool"]) == 0
    assert main(["fetch", "--config", str(config), "--secrets", none]) == 0
    assert served["app"].settings.setup._runner.spool.folder == run and ran[0].spool.folder == run
    assert (run / "requests").is_dir() and not (tmp_path / "state" / "setup-spool").exists()  # the spool's old home


def test_the_fetch_service_stopped_by_systemd_says_cancelled(tmp_path, monkeypatch):
    # systemctl stop, a timeout or update.sh: SIGTERM mid-search. The page is told at once, not after a silence.
    import signal

    from departureboard.setup.spool.runner import SpoolRunner
    from departureboard.setup.spool.worker import Worker

    class StuckJob:
        """A discover.SetupJob that runs until cancelled."""

        made: ClassVar[list] = []

        def __init__(self, folder, stops, api_key, *, routes=False, walk_minutes=None, nice=0):
            self.state = "running"
            StuckJob.made.append(self)

        def start(self):
            pass

        def status(self):
            return {"state": self.state, "step": "Finding bus companies", "done": 0, "total": 3, "message": "",
                    "result": None}

        def cancel(self):
            self.state = "failed"

    class Unhandled(Exception):
        pass

    def unhandled(signum, frame):
        raise Unhandled  # SIGTERM reached the test's own handler: the service didn't take it

    # The fetch service makes its Worker by the name it imported.
    monkeypatch.setattr("departureboard.fetch.service.Worker",
                        lambda *a, **kw: Worker(*a, make_job=StuckJob, **kw))
    run = tmp_path / "run"
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    config = installed_template(tmp_path)
    (tmp_path / "state").mkdir()
    secrets = tmp_path / "secrets.env"
    secrets.write_text("BODS_API_KEY=saved-bods-key\n")
    asked = {}

    def page():
        runner = SpoolRunner(run, poll=0.01)
        asked["job"] = job = runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
        job.start()
        deadline = time.monotonic() + 5
        while job.status()["state"] != "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, unhandled)
    thread = threading.Thread(target=page)
    try:
        thread.start()
        try:
            assert main(["fetch", "--config", str(config), "--secrets", str(secrets)]) == 0
        except Unhandled:
            pytest.fail("the fetch service didn't handle SIGTERM")
        thread.join(5)
        assert signal.getsignal(signal.SIGTERM) is unhandled  # put back as it was
    finally:
        signal.signal(signal.SIGTERM, previous)
    status = asked["job"].status()
    assert (status["state"], status["message"]) == ("failed", "Cancelled")
    assert [job.state for job in StuckJob.made] == ["failed"]


def test_the_fetch_service_without_a_config_searches_in_systemds_folder(tmp_path, monkeypatch, no_runtime_folder):
    from departureboard.setup.spool.worker import Worker

    ran = []
    monkeypatch.setattr(Worker, "run", lambda self, tend=None: ran.append(self) or 0)
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "lib"))
    assert main(["fetch", "--config", str(tmp_path / "missing.toml"),
                 "--secrets", str(tmp_path / "none.env")]) == 0
    [worker] = ran
    assert worker._job_folder == tmp_path / "lib"
    assert worker._settings() == {"state_file": str(tmp_path / "lib" / "state.json")}


@pytest.mark.parametrize("config", ["not toml", "a folder"])
def test_the_fetch_service_answers_with_why_when_it_cant_read_its_config(tmp_path, monkeypatch, capsys, config):
    from departureboard.setup.spool.worker import NO_SETTINGS, Worker

    run = tmp_path / "run"
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    ran = []
    monkeypatch.setattr(Worker, "run", lambda self: ran.append(self) or 0)
    path = tmp_path / "config.toml"
    if config == "not toml":
        path.write_text("display = [\n")
    else:
        path.mkdir()  # as Docker makes one for a file to mount that isn't there
    # A clean stop once each request has its answer, so systemd doesn't start it again and again.
    assert main(["setup", "serve", "--config", str(path), "--secrets", str(tmp_path / "none.env")]) == 0
    [worker] = ran
    assert worker.spool.folder == run and worker._unavailable == NO_SETTINGS
    assert "Traceback" not in capsys.readouterr().err


def test_without_a_config_the_settings_start_in_systemds_folder(tmp_path, monkeypatch, capsys, served):
    from departureboard.web.settingsite import SETUP

    folder = tmp_path / "lib"
    folder.mkdir()
    monkeypatch.setenv("STATE_DIRECTORY", str(folder))
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    assert main(["web", "--config", str(tmp_path / "missing.toml"), "--secrets", str(tmp_path / "none.env")]) == 0
    assert "opens the setup wizard" in capsys.readouterr().err
    site = served["app"].settings
    assert site._home() == SETUP and site.setup._job_folder == folder and site.store.folder == folder


@pytest.mark.parametrize("name, url", [
    ("kitchen-board", "http://kitchen-board.local:8090/settings"),
    ("Board.lan", "http://board.local:8090/settings"),
    ("bad name\n", "http://raspberrypi.local:8090/settings"),
    ("", "http://raspberrypi.local:8090/settings"),
])
def test_the_setup_address_is_this_pis_name(name, url):
    from departureboard.cli.common import setup_url

    assert setup_url(name) == url

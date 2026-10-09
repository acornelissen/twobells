import json

import pytest
from PIL import Image

from departureboard.cli import load_env_file, main


def test_demo_writes_preview_without_any_keys(tmp_path):
    out = tmp_path / "demo.png"
    assert main(["demo", "--png", str(out)]) == 0
    assert Image.open(out).size == (250, 122)


@pytest.mark.parametrize("display, size", [("waveshare-10in85g", (1360, 480)), ("hdmi-1280x400", (1280, 400))])
def test_demo_previews_bigger_panels(tmp_path, display, size):
    out = tmp_path / "demo.png"
    assert main(["demo", "--png", str(out), "--display", display]) == 0
    assert Image.open(out).size == size


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
    from departureboard.transportapi import TransportApiBusSource

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

    from departureboard.darwin import DarwinSource

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


def test_emulate_primes_the_first_tick(monkeypatch):
    import departureboard.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "run_loop", lambda *a, **kw: seen.update(kw))
    assert main(["emulate", "--demo", "--no-browser", "--port", "0"]) == 0
    assert seen.get("prime") is True


def _run_setup(tmp_path, monkeypatch):
    from datetime import datetime

    import departureboard.cli as cli
    from departureboard.darwin import DarwinSource

    calls = []
    monkeypatch.setattr(DarwinSource, "fetch", lambda self: calls.append(1) or [])
    monkeypatch.setattr(cli, "_local_now", lambda tz: datetime(2026, 10, 10, 12, 0))  # Saturday
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
    assert main(args + ["--config", str(config), "--secrets", str(tmp_path / "none.env")]) == 0
    assert Image.open(out).size == (1280, 400)
    assert state.exists()


def test_sleeping_to_the_tick_lands_on_a_whole_second(monkeypatch):
    from departureboard import cli

    slept = []
    monkeypatch.setattr(cli.time, "time", lambda: 1000.25)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    cli._sleep_to_the_tick(1)
    cli._sleep_to_the_tick(60)
    cli._sleep_to_the_tick(0)
    assert slept == [0.75, 19.75]  # 1000.25 s is 40.25 s past a minute


def _route_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[bus]\nwindows = ["Mon-Fri 07:00-08:30"]\nwalk_minutes = 2\n[bus.stops]\nA = "1000DGHS0900"\n')
    return config


def test_route_prints_a_config_block_from_a_timetable_file(tmp_path, capsys, monkeypatch):
    import tomllib
    from pathlib import Path

    from departureboard import routesetup

    fixtures = Path(__file__).parent / "fixtures"
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
    from pathlib import Path

    from departureboard import cli, routesetup

    txc = (Path(__file__).parent / "fixtures" / "txc_glossop_synthetic.xml").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.xml", txc)
        archive.writestr("b.xml", txc)
    events = []

    def fake_get(url, limit, timeout):
        events.append(f"download {url}")
        return buffer.getvalue()

    real_patterns = routesetup.patterns
    monkeypatch.setattr(cli, "get_bytes", fake_get)
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
    from departureboard.cli import _env

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

    from departureboard.cli import _settings, _Watcher

    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\ntitle = "Before"\n{extra}')
    args = argparse.Namespace(config=str(config), secrets=str(tmp_path / "none.env"))
    return _Watcher(args, _settings(args)), state


def test_watcher_reloads_when_saved_settings_change(tmp_path):
    watcher, state = _watched(tmp_path)
    assert watcher() is None  # nothing changed
    (state / "settings.toml").write_text('title = "After"\n')
    board = watcher()
    assert board.title == "After"
    assert watcher() is None


def test_watcher_asks_for_a_restart_when_the_display_changes(tmp_path):
    from departureboard.cli import _Restart

    watcher, state = _watched(tmp_path, 'display = "hdmi-1280x400"\n')
    (state / "settings.toml").write_text("rotate = 90\n")
    with pytest.raises(_Restart):
        watcher()


def test_watcher_keeps_running_settings_when_new_ones_are_broken(tmp_path):
    watcher, state = _watched(tmp_path)
    (state / "settings.toml").write_text('theme = "neon"\n')
    assert watcher() is None

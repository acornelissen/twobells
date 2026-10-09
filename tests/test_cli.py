import json

import pytest
from PIL import Image

from departureboard.cli import load_env_file, main


def test_demo_writes_preview_without_any_keys(tmp_path):
    out = tmp_path / "demo.png"
    assert main(["demo", "--png", str(out)]) == 0
    assert Image.open(out).size == (250, 122)


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

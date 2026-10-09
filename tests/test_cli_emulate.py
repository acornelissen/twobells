from datetime import datetime

from departureboard.cli import main
from departureboard.emulation import build_emulation, demo_outage
from departureboard.fake import FakeBusSource, FakeTrainSource
from departureboard.transportapi import TransportApiBusSource


def test_demo_uses_fake_sources_and_a_simulated_clock():
    em = build_emulation(demo=True)
    sources = [c.source for c in em.board.sources]
    assert [type(s) for s in sources] == [FakeTrainSource, FakeBusSource]
    assert hasattr(em.clock, "advance")
    assert em.store is None
    assert em.display.status()["mode"] == "demo"
    assert em.tick_seconds == 2


def test_live_mode_can_fake_trains_while_waiting_for_rail_key(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        f'state_file = "{tmp_path / "state.json"}"\n'
        '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
        '[bus]\nwindows = ["Mon-Fri 07:00-08:30"]\n[bus.stops]\nA = "1000DGHS0900"\n'
    )
    env = {"TAPI_APP_ID": "id", "TAPI_APP_KEY": "key"}

    em = build_emulation(config=config, env=env, fake_train=True)

    sources = {c.source.name: c.source for c in em.board.sources}
    assert isinstance(sources["train"], FakeTrainSource)
    assert isinstance(sources["bus"], TransportApiBusSource)
    assert not hasattr(em.clock, "advance")
    assert em.store is not None
    assert em.tick_seconds == 60
    assert em.display.status()["mode"] == "live"


def test_tick_can_be_overridden():
    assert build_emulation(demo=True, tick_seconds=0.5).tick_seconds == 0.5


def test_demo_outage_covers_a_morning_window():
    assert demo_outage(datetime(2026, 10, 8, 7, 35))
    assert not demo_outage(datetime(2026, 10, 8, 7, 0))
    assert not demo_outage(datetime(2026, 10, 8, 8, 0))


def test_demo_runs_headless_for_a_few_ticks():
    code = main(["emulate", "--demo", "--no-browser", "--port", "0", "--tick", "0", "--ticks", "3"])
    assert code == 0


def test_emulate_reports_busy_port_without_traceback(capsys):
    from departureboard.emulator import EmulatorDisplay, make_server

    blocker = make_server(EmulatorDisplay(), port=0)
    port = blocker.server_address[1]
    try:
        code = main(["emulate", "--demo", "--no-browser", "--port", str(port), "--port-attempts", "1"])
    finally:
        blocker.server_close()
    assert code == 2
    err = capsys.readouterr().err
    assert str(port) in err and "--port" in err


def test_demo_board_has_walking_times():
    walk = build_emulation(demo=True).board.walk
    assert walk.train is not None and walk.bus


def test_demo_uses_the_station_board_look_in_yellow():
    em = build_emulation(demo=True)
    assert em.board.theme.name == "sign"
    assert em.display.status()["accent"] == "yellow"


def test_demo_accent_can_be_overridden():
    assert build_emulation(demo=True, accent="red").display.status()["accent"] == "red"

from datetime import datetime

import pytest

from departureboard.cli.main import main
from departureboard.emulator.emulation import build_emulation, demo_outage
from departureboard.emulator.fake import FakeBusSource, FakeTrainSource
from departureboard.sources.transportapi import TransportApiBusSource


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
        '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:30"]\n[bus.stops]\nA = "1000DGHS0900"\n'
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
    code = main(["emulate", "--demo", "--no-browser", "--port", "0", "--tick", "0.001", "--ticks", "3"])
    assert code == 0


def test_emulate_reports_busy_port_without_traceback(capsys):
    from departureboard.emulator.server import EmulatorDisplay, make_server

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


@pytest.mark.parametrize("display", ["hdmi-1280x400", "hdmi-1920x440"])
def test_demo_can_emulate_another_display(display):
    em = build_emulation(demo=True, display=display)
    assert em.display.status()["panel"]["name"] == display


def _live_config(tmp_path, display):
    config = tmp_path / "config.toml"
    config.write_text(
        f'display = "{display}"\nstate_file = "{tmp_path / "state.json"}"\n'
        '[bus]\nsource = "transportapi"\nwindows = ["Mon-Fri 07:00-08:30"]\n[bus.stops]\nA = "1000DGHS0900"\n'
    )
    return config


def test_live_mode_emulates_the_configured_display(tmp_path):
    em = build_emulation(config=_live_config(tmp_path, "waveshare-10in85g"), env={"TAPI_APP_ID": "i", "TAPI_APP_KEY": "k"})
    assert em.display.status()["panel"]["name"] == "waveshare-10in85g"
    assert em.tick_seconds == 60


def test_live_screen_ticks_every_second_with_a_seconds_clock(tmp_path):
    em = build_emulation(config=_live_config(tmp_path, "hdmi-1280x400"), env={"TAPI_APP_ID": "i", "TAPI_APP_KEY": "k"})
    assert em.tick_seconds == 1
    assert em.display.panel.epaper is False


def test_display_flag_overrides_the_config(tmp_path):
    em = build_emulation(
        config=_live_config(tmp_path, "inky"), env={"TAPI_APP_ID": "i", "TAPI_APP_KEY": "k"}, display="hdmi-1280x400"
    )
    assert em.display.status()["panel"]["name"] == "hdmi-1280x400"


def test_demo_screen_runs_headless_for_a_few_ticks():
    code = main(["emulate", "--demo", "--display", "hdmi-1280x400", "--no-browser", "--port", "0", "--tick", "0.001", "--ticks", "3"])
    assert code == 0


def test_screen_demo_shows_the_bus_page_with_a_moving_bus():
    from departureboard.board.app import run_once

    em = build_emulation(demo=True, display="hdmi-1280x400")
    assert em.tick_seconds == 1
    state, pages, positions = {}, set(), []
    for _ in range(12 * 60 * 25):  # 25 simulated minutes at 5 s a tick
        now = em.clock()
        result = run_once(em.board, state, em.display, now, prime=not state)
        pages.add(result.page)
        if result.route and result.route.bus_at is not None:
            positions.append(result.route.bus_at)
        em.clock.advance()
        if len(positions) > 20:
            break
    assert pages == {"departures", "route"}
    assert positions and positions[-1] > positions[0]  # the bus moves toward your stop


def test_epaper_demo_has_no_bus_page():
    em = build_emulation(demo=True)
    assert em.board.tracker is None


def test_demo_shows_rain_then_a_disruption_then_neither():
    from departureboard.board.app import run_once

    em = build_emulation(demo=True)
    state, seen = {}, {}
    for _ in range(75):  # 06:58 to 08:12, a simulated minute a tick
        now = em.clock()
        view = run_once(em.board, state, em.display, now, prime=not state).view
        seen[now.strftime("%H:%M")] = (bool(view.notice), view.rain)
        em.clock.advance()
    assert seen["07:00"] == (False, "Rain from 07:00 (70%)")
    assert seen["07:15"][0]  # the disruption notice, which takes the rain line's place
    assert seen["07:30"] == (False, "Rain from 07:00 (70%)")
    assert seen["08:10"] == (False, None)


def test_demo_is_the_same_every_run():
    from departureboard.board.app import run_once

    def run():
        em = build_emulation(demo=True)
        state, views = {}, []
        for _ in range(40):
            views.append(run_once(em.board, state, em.display, em.clock(), prime=not state).view.signature())
            em.clock.advance()
        return views

    assert run() == run()


def test_screen_demo_says_where_the_trains_and_buses_are():
    from departureboard.board.app import run_once

    em = build_emulation(demo=True, display="hdmi-1280x400")
    state, trains, buses, words = {}, set(), set(), set()
    for _ in range(12 * 15):  # 15 simulated minutes at 5 s a tick
        view = run_once(em.board, state, em.display, em.clock(), prime=not state).view
        trains |= {r.strip.where for r in view.trains.rows if r.strip and r.strip.at is not None}
        buses |= {r.strip.where for r in view.buses.rows if r.strip and r.strip.at is not None}
        words |= {r.strip.where for r in (*view.trains.rows, *view.buses.rows) if r.strip}
        em.clock.advance()
    assert "Left Hadfield" in trains and "Not started" in words
    assert {"3 stops away", "1 stop away"} <= buses
    assert all(len(w) <= 18 for w in words)  # a row has room for 18 characters


def test_epaper_demo_asks_where_nothing_is():
    assert build_emulation(demo=True).board.positions is None

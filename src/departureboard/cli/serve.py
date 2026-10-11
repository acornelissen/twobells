"""The `serve` command: the settings site on every install and the dashboard
where its view is on, with --set-password and --reset-code; and `fetch`, a
Pi's internet work for its web server."""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from ..settings import migrate
from ..settings.config import ConfigError, needs_setup
from ..settings.installs import Install, install_named
from ..settings.schema import views
from ..settings.store import Store
from .common import as_owner

# How often serve looks for changed settings files, where it runs the dashboard itself.
SETTINGS_CHECK_S = 2.0


def _adopt(args, store: Store, install: str = "desktop") -> None:
    """Bring the old dashboard's folder into the store, once (see settings/migrate.py).
    A folder given with --state-dir was the old dashboard's own, so then
    there is nothing to bring."""
    if not getattr(args, "state_dir", None):
        migrate.adopt_legacy(store, install)


def fetch_command(args) -> int:
    """The fetch service on a Pi: the internet work of the web server, which
    can't reach the internet itself: the wizard's lookups and the dashboard's
    pollers (see fetch/service.py and setup/spool/)."""
    from ..fetch.service import FetchService
    from ..setup.spool.folder import SpoolError, spool_folder
    from ..setup.spool.worker import NO_SETTINGS, Worker

    install = install_named(args.install)
    store, problem = None, ""
    try:
        store = as_owner(args, Store.open(args.config, args.secrets, state_dir=args.state_dir, install=install.name))
    except ConfigError as error:
        problem = str(error)
    if problem:
        print(problem, file=sys.stderr)
    try:
        spool = spool_folder()
    except SpoolError as error:
        print(error, file=sys.stderr)
        return 2
    stop = threading.Event()
    if store is None:
        # Say so to the page, rather than stop with its requests waiting and be started again and again.
        service = Worker(spool, job_folder=None, load_secrets=dict, unavailable=NO_SETTINGS, stop=stop)
    else:
        service = FetchService(store, install, spool, stop=stop)
    # systemctl stop, a timeout or update.sh: end cleanly, telling the page a running search was cancelled.
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    try:
        return service.run()
    except (OSError, SpoolError) as error:
        print(f"Can't use the spool: {error}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


def _set_password(password, prompt: str) -> int:
    """Ask for a new settings password twice, without echoing it, and save it."""
    import getpass

    first = getpass.getpass(prompt)
    if first != getpass.getpass("Again: "):
        print("Those didn't match", file=sys.stderr)
        return 1
    try:
        password.set(first)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Can't save the password in {password.path}: {error.strerror or error}", file=sys.stderr)
        return 1
    print(f"Password saved to {password.path}")
    return 0


def _reset_code(folder: Path, where: str, zone: ZoneInfo) -> int:
    """Print a one-time code for setting a new settings password, and where it
    goes, with when it runs out in `zone` (the process's own may be UTC, as
    in Docker). Running this proves access, so no rate limit, and the code
    comes here rather than to the board or the log."""
    from ..web.auth import ResetCodes, spaced

    def show(code: str, expires: float) -> None:
        until = datetime.fromtimestamp(expires, zone).strftime("%H:%M")
        print(f"Settings reset code: {spaced(code)}  (valid until {until}, once)")
        print(where)

    try:
        ResetCodes(folder, show=show).issue(time.time(), limit=False)
    except OSError as error:
        print(f"Can't save a reset code in {folder}: {error.strerror or error}", file=sys.stderr)
        return 1
    return 0


def _dashboard_wanted(store: Store, install: Install) -> bool:
    """Whether serve should run the dashboard: the install offers it, its
    view is on, and there is something to show. ConfigError if [app] views
    is wrong, or the settings can't be read, where it is offered."""
    if "dashboard" not in install.offered:
        return False
    return "dashboard" in views(store.data(), install) and not needs_setup(store)


def _fetch_runs_dashboard(store: Store, install: Install) -> bool:
    """On a Pi: whether the fetch service would run the dashboard now, as it
    decides (fetch/service.py): wanted, and its settings load. While they
    don't, it stops when idle, so the mirror mustn't wake it for them every
    minute; a save that fixes them makes this true again."""
    from ..dashboard.config import load_dashboard

    if not _dashboard_wanted(store, install):
        return False
    try:
        load_dashboard(store, store.env(), lock=False)
    except ConfigError:
        return False
    return True


class _SettingsWatch(threading.Thread):
    """Looks every SETTINGS_CHECK_S for a change to any file the settings
    come from: those saved in the store, config.toml or the secrets. When
    the dashboard should start, stop or take new settings (it is wanted and
    its settings load, or it runs and is no longer wanted), it stops the
    server, so serve can start afresh; when they don't load, it says so and
    keeps the ones running."""

    def __init__(self, store: Store, install: Install, server, running: bool) -> None:
        from ..dashboard.config import FileWatch

        super().__init__(name="settings-watch", daemon=True)
        self._store = store
        self._install = install
        self._server = server
        self._running = running
        self._watch = FileWatch(store.watched())
        self._stopping = threading.Event()
        self.restart = False

    def run(self) -> None:
        from ..dashboard.config import load_dashboard

        while not self._stopping.wait(SETTINGS_CHECK_S):
            if not self._watch.changed():
                continue
            try:
                wanted = _dashboard_wanted(self._store, self._install)
                if wanted:
                    load_dashboard(self._store, self._store.env(), lock=False)
            except ConfigError as error:
                logging.warning("Settings changed but don't load (%s); keeping the current ones", error)
                continue
            except Exception as error:  # noqa: BLE001 - the running server carries on
                logging.warning("Settings changed but can't be checked (%s); keeping the current ones",
                                type(error).__name__)
                continue
            if not wanted and not self._running:
                continue  # no dashboard before or after: nothing to start again for
            logging.info("Settings changed; starting the dashboard again to use them")
            self.restart = True
            self._server.shutdown()
            return

    def stop(self) -> None:
        self._stopping.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=10)


def _start_again(port: int) -> None:
    """Replace this process with a fresh one, started the same way, on the
    same port and without opening the browser again. Nothing of the old one
    is left: every thread, the lock and the listening socket go with it.
    Inside Docker the process stays the container's main one throughout."""
    argv = [sys.executable, *sys.orig_argv[1:], "--no-browser", "--port", str(port)]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)  # noqa: S606 - starts this same program afresh, no shell involved


def _settings_site(store: Store, install: Install, running: bool, runner=None):
    """The password-protected settings site: its tabs and the setup wizard,
    both saving into the store, checked as each view that is on loads.
    `running`: whether serve runs the dashboard, for the links to it.
    `runner` does the wizard's internet work, if another service does it."""
    from ..setup import discover
    from ..setup.api import SetupApi
    from ..timetable.routesetup import document_limits
    from ..web.groupform import check_views
    from ..web.settingsite import PREFIX, SettingsSite

    target = store.target(lambda data, env: check_views(data, env, store.folder, install))
    # The dashboard, wherever it is offered, is served at / once its view is on and set up.
    view_links = {"dashboard": ("The dashboard", "/")} if "dashboard" in install.offered else {}
    setup = SetupApi(
        target, store.env, store.folder,
        make_job=partial(discover.SetupJob, limits=document_limits(install)),
        install=install,
        links=[("Settings", PREFIX)],
        view_links=view_links,
        runner=runner,
        back=PREFIX,  # the settings' Overview, for Back on a shortcut's first step
    )
    site = SettingsSite(store, install, setup, dashboard_on=lambda: running and "dashboard" in site.views())
    if not site.password.is_set():
        where = "on the board" if site.to_board() else "in this log"
        print(f"Settings: no password yet. Open {PREFIX} and claim it with a code shown {where}, "
              f"or set one with `{install.password_command}`.", file=sys.stderr)
    return site


def _allowed_hosts(store: Store) -> frozenset[str]:
    """[dashboard] allowed_hosts in config.toml, for the settings when the
    dashboard doesn't run; none if it can't be read (an address on the home
    network still reaches them)."""
    from ..dashboard.config import allowed_hosts

    try:
        return allowed_hosts(store.base())
    except (OSError, ConfigError) as error:
        logging.warning("[dashboard] allowed_hosts left out: %s", error)
        return frozenset()


def serve_command(args) -> int:
    """The settings site on every install, and the dashboard where its view
    is on (see web/settingsite.py and web/server.py)."""
    from ..dashboard.config import load_dashboard

    install = install_named(args.install)
    # Only config.toml is read to find the store: a broken settings.toml must
    # not stop the page that fixes it.
    store = Store.open(args.config, args.secrets, state_dir=args.state_dir, install=install.name)
    as_owner(args, store)  # --set-password and --reset-code write there too
    _adopt(args, store, install.name)
    if args.set_password or args.reset_code:
        return _password(args, store, install)
    store.make()
    runner = None
    if install.fetch == "files":
        # This service can't reach the internet; departureboard-fetch.service does that work.
        from ..setup.spool.folder import SpoolError, spool_folder
        from ..setup.spool.runner import SpoolRunner

        try:
            runner = SpoolRunner(spool_folder())
        except SpoolError as error:
            print(error, file=sys.stderr)
            return 2
    dash, problem = None, ""
    try:
        wanted = _dashboard_wanted(store, install)
    except ConfigError as error:
        # The views can't be read: said as the dashboard's problem only where it is on unless they say.
        wanted, problem = False, (str(error) if "dashboard" in install.default_views else "")
    if wanted:
        try:
            # Where another service runs its pollers (a Pi's fetch service), that one holds the store's lock.
            dash = load_dashboard(store, store.env(), lock=install.fetch == "process")
        except ConfigError as error:
            problem = str(error)
    if problem:
        # Start anyway, so the settings can fix it (and Docker doesn't start it again and again).
        print(f"The dashboard can't start: {problem}. Fix it in the settings.", file=sys.stderr)
    try:
        code, restart = _serve_with(args, store, install, runner, dash, problem)
    finally:
        if dash is not None:
            dash.close()
    if restart is not None:
        _start_again(restart)
    return code


def _mirror(store: Store, install: Install, runner, dash):
    """On a Pi: the dashboard's snapshot as the fetch service hands it over,
    through the spool `runner` uses. Until the first comes, an empty one
    with the title and walk times of `dash` (the dashboard's settings, if
    its view was on and they loaded at start), or the defaults."""
    from ..core.walk import WalkTimes
    from ..fetch.handoff import SnapshotMirror
    from ..settings.schema import BOARD_DEFAULTS
    from ..setup.spool.runner import Poster

    folder = runner.spool.folder
    return SnapshotMirror(
        folder,
        dash.title if dash is not None else BOARD_DEFAULTS["title"],
        dash.walk if dash is not None else WalkTimes(),
        Poster(folder),
        wanted=lambda: _fetch_runs_dashboard(store, install),
        timezone=dash.timezone if dash is not None else None,
        row_strips=dash.options.row_strips if dash is not None else True,
    )


def _serve_with(args, store: Store, install: Install, runner, dash, problem: str) -> tuple[int, int | None]:
    """Run the server until stopped, with the dashboard if `dash` (its
    settings) is given. The exit code, and the port to start again on if
    the settings changed."""
    import sqlite3

    from ..dashboard.collector import Collector
    from ..dashboard.history import History, HistoryReader
    from ..dashboard.snapshot import Hub
    from ..timetable.routesetup import document_limits
    from ..web import netguard
    from ..web import server as dashserver

    history = hub = collector = mirror = None
    if install.fetch == "files" and "dashboard" in install.offered:
        # No pollers here (this service can't reach the internet): the fetch
        # service runs them and hands over what they find.
        hub = mirror = _mirror(store, install, runner, dash)
        history = HistoryReader(store.folder)
    elif install.fetch == "process" and dash is not None:
        try:
            history = History(dash.state_dir)
        except sqlite3.Error as error:
            print(f"Can't open the punctuality history in {dash.state_dir}: {error}", file=sys.stderr)
            return 2, None
        hub = Hub(dash.title, dash.walk, tz=ZoneInfo(dash.timezone), row_strips=dash.options.row_strips)
        collector = Collector(dash, hub, history, timetable_limits=document_limits(install))

    def healthy() -> bool:
        if mirror is not None:
            return mirror.healthy()
        return collector.healthy() if collector is not None else True

    def dashboard_on() -> bool:
        # The mirror is always there, so the dashboard is on as soon as its view
        # is (a save on a Pi doesn't start serve again).
        return "dashboard" in site.views() and (mirror is None or not needs_setup(store))

    watch = None
    try:
        site = _settings_site(store, install, hub is not None, runner)
        site.problem = problem
        # The Overview reads the dashboard's health from the same snapshot, never connecting out itself.
        site.hub, site.healthy = hub, healthy
        app = dashserver.DashboardApp(
            hub,
            dashserver.StaticFiles.load(dashserver.default_static_dir(), version=site.version),
            healthy,
            site,
            dashboard_on=dashboard_on,
            recent=history.recent if history is not None else None,
            history_days=dash.options.history_days if dash is not None else 90,
            allowed_hosts=dash.options.allowed_hosts if dash is not None else _allowed_hosts(store),
        )
        host = "0.0.0.0" if args.lan else (args.host or install.host)  # noqa: S104 - --lan asks for every interface
        # A port asked for is that port or nothing (Docker's health check expects
        # it); without one, a desktop takes the next free port after the default.
        if args.port is None:
            port, attempts = dashserver.DEFAULT_PORT, (10 if install.browser else 1)
        else:
            port, attempts = args.port, 1
        try:
            server = dashserver.make_server(app, host, port, attempts)
        except OSError as error:
            hint = " Stop the other process or pass another --port." if args.port is not None else ""
            print(f"Can't listen on {host}:{port}: {error.strerror or error}.{hint}", file=sys.stderr)
            return 2, None
        port = server.server_address[1]
        if install.network_guard:
            # Whether the kernel really keeps this server off the internet; in the log and on the Overview,
            # which asks again as it is drawn.
            site.network_guard = netguard.Guard()
            site.network_guard.check()
        if collector is not None:
            collector.start()
        if mirror is not None:
            mirror.start()
        if needs_setup(store):
            print("Not set up yet: sign in and the page opens the setup wizard", file=sys.stderr)
        url = f"http://localhost:{port}/"
        if hub is not None and dashboard_on():
            print(f"Dashboard at {url}  (Ctrl+C to stop)")
        print(f"Settings at {url}settings  (home network only)")
        if args.lan:
            name = socket.gethostname().split(".")[0]
            print(f"On your home network: http://{name}.local:{port}/ or this computer's IP address and port {port}")
        if install.browser and not args.no_browser:
            webbrowser.open(url if hub is not None else f"{url}settings")
        # docker stop and systemctl stop send SIGTERM: shut down as for Ctrl+C (signals reach the main thread only).
        on_main = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGTERM, _interrupt) if on_main else None
        try:
            if install.fetch == "process":  # where serve runs the dashboard, a change starts it again
                watch = _SettingsWatch(store, install, server, hub is not None)
                watch.start()
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if watch is not None:
                watch.stop()
            if on_main:
                signal.signal(signal.SIGTERM, previous)
            if hub is not None:
                hub.close()
            if collector is not None and not collector.stop(timeout=5):
                logging.info("A dashboard poller was still fetching; it ends with the process")
            server.server_close()
    finally:
        if history is not None:
            history.close()
    return 0, (port if watch is not None and watch.restart else None)


def _password(args, store: Store, install: Install) -> int:
    """--set-password or --reset-code for the settings: in the store, beside
    a running server (whose sessions end when the password changes; see
    auth.Sessions). Nothing else is loaded, so broken settings can't keep
    their owner out."""
    from ..web.auth import PasswordStore
    from ..web.settingsite import PREFIX

    store.make()
    if args.set_password:
        return _set_password(PasswordStore(store.password_file), f"New {install.noun} settings password: ")
    return _reset_code(store.folder, f"Enter it on the settings site, {PREFIX}/reset, with a new password.",
                       store.zone())


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt

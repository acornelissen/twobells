"""The fetch service: everything a Pi asks of the internet for the web server.

On a Pi the web server (twobells-web.service) reaches only the home
network. This service, twobells-fetch.service, does its internet work
instead: it goes out, and listens on nothing. The two talk through the
spool folder in memory (setup/spool/):

- the setup wizard's lookups, key tests and search, asked for and answered
  in files (setup/spool/worker.py);
- while the dashboard view is on, the dashboard's pollers
  (collector.Collector, which never builds the bus timetable here: the
  timetable timer and the wizard's search do), their snapshot handed over as
  a file (handoff.SnapshotWriter). While a search runs, which builds a
  timetable in this process, they set the one they read aside
  (Collector.set_timetable_aside), so the two are never held at once;
- `active` requests from the web server, saying a browser is using the
  dashboard, so the pollers keep going through the quiet hours.

It starts at boot, and again whenever the web server leaves a request
(twobells-fetch.path). Each time round it looks for changed settings:
the dashboard starts when its view comes on, stops (its snapshot deleted)
when it goes off, and starts again with new settings when they change and
still load; settings that don't load leave it as it was. With the
dashboard off it stops after a minute with nothing to do, so a board-only
Pi Zero stays light (the web server, checking the settings as this does,
doesn't wake it again for a dashboard whose settings don't load); never with a request waiting, or a search running
or still finding the roads for the bus maps after it is done. Stopped
(SIGTERM) meanwhile, a done search keeps its result and only its roads
stop: the timetable is saved without them, and its next build finds them.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from ..board.config import settings_from
from ..core.errors import ConfigError
from ..dashboard.collector import Collector
from ..dashboard.config import DashboardSettings, FileWatch, load_dashboard
from ..dashboard.history import History
from ..dashboard.snapshot import Hub
from ..settings.config import needs_setup
from ..settings.installs import Install
from ..settings.schema import views
from ..settings.store import Store
from ..setup import discover
from ..setup.spool.folder import Spool
from ..setup.spool.worker import Worker
from ..timetable.routesetup import document_limits
from . import handoff

log = logging.getLogger(__name__)


def make_collector(dash: DashboardSettings, hub: Hub, history: History | None) -> Collector:
    """The dashboard's pollers, as this service runs them: the bus timetable
    is built elsewhere on a Pi, so never here."""
    return Collector(dash, hub, history, build_timetable=False)


def dashboard_wanted(store: Store, install: Install) -> bool:
    """Whether the dashboard should run: its view is on and there is
    something to show. ConfigError if the settings can't be read."""
    return "dashboard" in views(store.data(), install) and not needs_setup(store)


class FetchService:
    """The fetch service for the store `store`, with the spool in `folder`.
    run() until stopped (`stop` set, as SIGTERM does) or idle."""

    def __init__(self, store: Store, install: Install, folder: Path | str, *, stop: threading.Event | None = None,
                 collector: Callable[[DashboardSettings, Hub, History | None], Collector] = make_collector,
                 **worker_options) -> None:
        self._store = store
        self._install = install
        self._folder = Path(folder)
        self._make_collector = collector
        self._spool = Spool(folder)
        self.stop = stop or threading.Event()
        target = store.target(settings_from)

        def settings() -> dict:
            try:
                return target.current()
            except (OSError, ConfigError, ValueError, TypeError):
                return {}  # a key test then asks about Glossop

        # The bus company search reads as much of each timetable file as the
        # install can, with the dashboard's timetable set aside while it runs.
        worker_options.setdefault("make_job", partial(discover.SetupJob, limits=document_limits(install),
                                                      while_running=self._searching))
        self.worker = Worker(folder, job_folder=store.folder, load_secrets=store.env, settings=settings,
                             stop=self.stop, on_active=self._active, **worker_options)
        self._watch: FileWatch | None = None
        self._history: History | None = None
        self._dash: DashboardSettings | None = None
        self.hub: Hub | None = None
        self.collector: Collector | None = None
        self._writer: handoff.SnapshotWriter | None = None
        # Setup searches running (one finding its roads can overlap the next),
        # and the lock that keeps the count and the collector told in step.
        self._searches = 0
        self._searches_lock = threading.Lock()

    @contextmanager
    def _searching(self) -> Iterator[None]:
        """While a setup search runs in its thread: the dashboard's timetable
        is set aside, in the pollers running now and any started meanwhile."""
        self._count_search(1)
        try:
            yield
        finally:
            self._count_search(-1)

    def _count_search(self, change: int) -> None:
        with self._searches_lock:
            before = self._searches
            self._searches += change
            collector = self.collector
            if collector is not None and (before == 0) != (self._searches == 0):
                collector.set_timetable_aside(self._searches > 0)

    def run(self) -> int:
        try:
            return self.worker.run(tend=self._tend)
        finally:
            self._stop_dashboard()
            if self._history is not None:
                self._history.close()
                self._history = None

    def _active(self) -> None:
        """A browser is using the dashboard: its pollers keep going through the quiet hours."""
        if self.hub is not None:
            self.hub.touch()

    def _tend(self) -> bool:
        """Each time round: take on changed settings. Whether the dashboard runs, which keeps the service up."""
        first = self._watch is None
        if self._watch is None:
            self._watch = FileWatch(self._store.watched())
        if first or self._watch.changed():
            try:
                self._settings_changed()
            except Exception as error:  # noqa: BLE001 - the wizard's work carries on
                log.warning("Can't take on the settings: %s", type(error).__name__)
        return self.collector is not None

    def _settings_changed(self) -> None:
        try:
            env = self._store.env()
            wanted = dashboard_wanted(self._store, self._install)
            if not wanted:
                dash = None
            elif self._dash is None:
                dash = load_dashboard(self._store, env)  # with the store's dashboard lock, held while it runs
            else:
                dash = load_dashboard(self._store, env, lock=False)
        except ConfigError as error:
            log.warning("The settings don't load (%s); the dashboard is left as it was", error)
            return
        if dash is None:
            if self.collector is not None:
                log.info("The dashboard view is off: its pollers stop")
            self._stop_dashboard()
            handoff.remove(self._folder)  # none kept while it is off
            return
        if self._dash is not None:  # the lock passes to the new settings
            dash.lock, self._dash.lock = self._dash.lock, None
            log.info("The settings changed: the dashboard's pollers start again with them")
        self._stop_dashboard()
        self._start_dashboard(dash)

    def _start_dashboard(self, dash: DashboardSettings) -> None:
        if self._history is None:
            try:
                self._history = History(dash.state_dir, ZoneInfo(dash.timezone))
            except Exception as error:  # noqa: BLE001 - the departures matter more than the history
                log.warning("The punctuality history can't be opened: %s", type(error).__name__)
        try:
            hub = Hub(dash.title, dash.walk, tz=ZoneInfo(dash.timezone), row_strips=dash.options.row_strips,
                      show=dash.options.show, scrolling=dash.scrolling, leave=dash.options.leave_modes)
            collector = self._make_collector(dash, hub, self._history)
            writer = handoff.SnapshotWriter(hub, self._spool, collector.healthy)
        except BaseException:
            dash.close()  # the lock, so the next try can take it
            raise
        with self._searches_lock:
            if self._searches:
                collector.set_timetable_aside(True)  # before it starts, so it never reads it
            self._dash, self.hub, self.collector, self._writer = dash, hub, collector, writer
        collector.start()
        writer.start()
        log.info("The dashboard's pollers are running")

    def _stop_dashboard(self) -> None:
        """Stop the pollers and the writer, if they run, and let go of the lock."""
        # The four are set, and cleared, together.
        if self.collector is None or self.hub is None or self._writer is None or self._dash is None:
            return
        self.hub.close()
        if not self.collector.stop(timeout=5):
            log.info("A dashboard poller was still fetching; it ends with the process")
        self._writer.stop()
        self._dash.close()
        self._dash = self.hub = self.collector = self._writer = None

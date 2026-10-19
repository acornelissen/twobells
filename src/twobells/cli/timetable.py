"""The `timetable` command: build the BODS bus timetable for the board's
stops, and the Metrolink tram timetable for its tram stops, each if due."""

from __future__ import annotations

import sys
from datetime import datetime

from ..settings.config import is_enabled, metrolink_on, needs_timetable
from ..settings.installs import install_named
from ..settings.schema import bus_source
from ..sources.bodsbus import BodsBusSource
from ..sources.holidays import bank_holidays
from ..sources.trams import TramTimetableSource
from ..timetable.build import TimetableBusy
from ..timetable.routesetup import document_limits
from ..timetable.txc import TimetableError
from .common import as_owner, load_settings, open_store


def timetable_command(args) -> int:
    as_owner(args, open_store(args))
    data = open_store(args).data()
    install = install_named(args.install)
    # Not a fault to have nothing to build: a Pi's hourly timer runs whatever the settings.
    trams = needs_timetable(data, "metro", install)  # London's come live from TfL: nothing to build
    codes = [_trams(args)] if trams else []
    if not trams and metrolink_on(data):
        print("No view that is on shows trams, so there's no tram timetable to build")
    if needs_timetable(data, "bus", install):
        codes.append(_buses(args))
    elif not trams or "bus" in data:  # with trams alone, nothing need be said of buses
        print(f"{_why_no_buses(data)}, so there's no bus timetable to build")
    return max(codes, default=0)


def _why_no_buses(data: dict) -> str:
    """Why needs_timetable said there's no bus timetable to build."""
    if "bus" not in data:
        return "There are no buses on this board"
    if not is_enabled(data, "bus"):
        return "Buses are switched off"
    if bus_source(data["bus"]) != "bods":
        return "Bus times come from TransportAPI"
    return "No view that is on shows buses"


def _trams(args) -> int:
    """Build the tram timetable, if due: 0 built or not due, 1 failed."""
    settings = load_settings(args, only={"metro"})
    source = next((c.source for c in settings.board.sources if c.source.name == "metro"), None)
    if not isinstance(source, TramTimetableSource):
        return 0  # trams not on after all: nothing to build
    try:
        found = source.refresh_timetable(force=args.force)
    except TimetableBusy:
        print("Another tram timetable build is running, so this one is skipped")
        return 0
    except TimetableError as error:
        print(f"Tram timetable not built: {error}", file=sys.stderr)
        return 1
    reason = source.unusable()
    if found is None or reason:
        print(f"No usable tram timetable: {reason}", file=sys.stderr)
        return 1
    print(f"Tram timetable: {len(found.calls)} departures at your tram stops, built {found.built:%a %d %b %H:%M}")
    return 0


def _buses(args) -> int:
    settings = load_settings(args, only={"bus"})
    source = next((c.source for c in settings.board.sources if c.source.name == "bus"), None)
    if not isinstance(source, BodsBusSource):
        return 0  # needs_timetable said BODS: not after all
    # The timetable's bank holiday names come from this list, so keep it fresh
    # whatever bank_holidays says about checking times. Always England and
    # Wales: BODS timetables are England's.
    bank_holidays(settings.state_file.parent, datetime.now())
    try:
        schedule = source.refresh_timetable(force=args.force, limits=document_limits(install_named(args.install)))
    except TimetableBusy:
        # Not a fault: the timer and the setup search may meet. The other build saves it.
        print("Another bus timetable build is running, so this one is skipped")
        return 0
    except TimetableError as error:
        print(f"Bus timetable not built: {error}", file=sys.stderr)
        return 1
    reason = source.unusable()
    if schedule is None or reason:
        print(f"No usable bus timetable: {reason}", file=sys.stderr)
        return 1
    until = f", one service in it runs until {schedule.valid_until:%d %b %Y}" if schedule.valid_until else ""
    calling = len(schedule.departure_journeys)
    inbound = len(schedule.journeys) - calling
    near = f" (and {inbound} ending near them)" if inbound else ""
    print(f"Bus timetable: {calling} journeys at your stops{near}, built {schedule.built:%a %d %b %H:%M}{until}")
    return 0

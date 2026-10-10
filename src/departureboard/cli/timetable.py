"""The `timetable` command: build the BODS bus timetable for the board's stops, if due."""

from __future__ import annotations

import sys
from datetime import datetime

from ..settings.config import is_enabled
from ..sources.bodsbus import BodsBusSource
from ..sources.holidays import bank_holidays
from ..timetable.build import TimetableBusy, TimetableError
from .common import as_owner, load_settings, open_store


def timetable_command(args) -> int:
    as_owner(args, open_store(args))
    settings = load_settings(args, only={"bus"})
    source = next((c.source for c in settings.board.sources if c.source.name == "bus"), None)
    if not isinstance(source, BodsBusSource):
        # Not a fault with buses off (the wizard does that for trains alone): the weekly timer runs anyway.
        data = open_store(args).data()
        if "bus" not in data:
            print("There are no buses on this board, so there's no bus timetable to build")
            return 0
        if not is_enabled(data, "bus"):
            print("Buses are switched off, so there's no bus timetable to build")
            return 0
        print('Bus times come from TransportAPI; set source = "bods" under [bus] first', file=sys.stderr)
        return 2
    # The timetable's bank holiday names come from this list, so keep it fresh
    # whatever bank_holidays says about checking times. Always England and
    # Wales: BODS timetables are England's.
    bank_holidays(settings.state_file.parent, datetime.now())
    try:
        schedule = source.refresh_timetable(force=args.force)
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
    until = f", until {schedule.valid_until:%d %b %Y}" if schedule.valid_until else ""
    calling = len(schedule.departure_journeys)
    inbound = len(schedule.journeys) - calling
    near = f" (and {inbound} ending near them)" if inbound else ""
    print(f"Bus timetable: {calling} journeys at your stops{near}, built {schedule.built:%a %d %b %H:%M}{until}")
    return 0

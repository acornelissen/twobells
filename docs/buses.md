# Bus times

- [How bus times are worked out](#how-bus-times-are-worked-out)
- [TransportAPI as a stand-in](#transportapi-as-a-stand-in)
- [The bus budget](#the-bus-budget)

## How bus times are worked out

Bus times are worked out the way [bustimes.org](https://bustimes.org) does it, from the Bus Open Data Service (BODS): the timetable for your stops, plus where the buses are right now. BODS covers England only; elsewhere, `source = "transportapi"` under `[bus]` takes bus times from TransportAPI alone. The setup wizard sets this up for you; in `config.toml` it looks like this:

```toml
[bus]
source = "bods"
operators = ["BNML", "HIPK"]    # the bus companies serving your stops (these are Glossop's)
interval_minutes = 2
windows = ["Mon-Sun 06:00-23:00"]
```

- **The timetable** is built on the Pi by `twobells-timetable.timer`. It checks every hour and rebuilds weekly, when a timetable in it ends, or when your stops or operators change. A build that fails is tried again 6 hours later, or at once if your stops, operators or BODS key change (a setup search cancelled while it builds hasn't failed, so doesn't wait); until then the board keeps the timetable it has. It downloads the operators' timetables from BODS and keeps only the journeys calling at your stops, with the days each runs: school-term-only journeys run only in the terms the timetable gives, and bank holidays follow the operator's own rules. A timetable file too big to read in the memory there is (on a Pi, over 61 MB; in Docker, over 100 MB; on a desktop, over 200 MB) is skipped with a warning in the log naming it, and the rest are used. The largest seen is 48 MB (First Bus's, in October 2026). For the same reason a build holds at most 8,500 journeys calling at your stops on a Pi, 8,000 in Docker and 20,000 on a desktop, counted as it reads them, old versions of a timetable included, and fails with a message in the log if there are more (a busy stop's week has a few thousand at most); of the journeys ending near your stop (below), it keeps at most half that number and leaves out the rest. Where BODS still has an old version of a timetable beside its replacement, the old one ends the day before the new one starts, so no bus shows twice; a temporary timetable (for roadworks, say) takes over only for its own dates, and the usual one runs again after it. It also keeps journeys of the same lines that end at a stand within 150 m of your stop without calling at it, only to spot the bus coming in to work a departure from your stop; they never show as departures. On a Pi the dashboard's feeds read this timetable and never build it; while a bus company search runs there, the dashboard shows its buses as being built and lets go of the timetable, so the search's build has the memory, and shows them again once the search is done.
- **Each check**, the board asks BODS where those lines' buses are, matches each bus to its timetabled journey, and shows how late it is. A bus running early is shown on time, since buses wait at timing points. A bus without a live position shows its timetable time with `sch`.
- **The last bus** of each line and destination today comes from the same timetable, with its days of the week and bank holidays, and costs no calls. A day runs past midnight: a bus at 00:20 on a journey that started the evening before is that evening's last bus, and it stays the last bus until it has gone. The dashboard shows it (see [The dashboard](dashboard.md#whats-on-it)), and so does the HDMI screen in the last hour (see [Reading the board](reading-the-board.md#the-hdmi-screen)). Only the directions the board shows count. Trains have no last train: Darwin gives only the next two hours.
- **Building it now**, rather than waiting up to an hour: the service builds only when a build is due, and not within 6 hours of one that failed, so delete the old timetable, and the failed build's note, first. Use the service, which reads the keys in `/etc/twobells/secrets.env` and writes the timetable as the app's user:

  ```bash
  sudo rm -f /var/lib/twobells/bus-timetable.json /var/lib/twobells/bus-timetable-failed.json  # only to rebuild
  sudo systemctl start twobells-timetable.service      # a few minutes on a Zero W
  journalctl -u twobells-timetable -n 5
  ```

  Run by hand with your own config and keys (on a laptop, say), `twobells timetable --force` does both in one step.

## TransportAPI as a stand-in

Until there's a usable timetable (none yet, built for other stops, or not rebuilt for four weeks), TransportAPI stands in on the board if its keys are set, checked every `fallback_interval_minutes` (default 12), within the daily budget, and only inside `fallback_windows`. By default those are the commute, `["Mon-Fri 07:00-08:30", "Mon-Fri 17:00-18:00"]`, which fits 28 lookups with two stops, whatever the bus windows say. Both are set in `config.toml` only. Without TransportAPI keys the board says it isn't updating buses.

## The bus budget

This applies only to TransportAPI: with `source = "bods"`, only while it stands in for the timetable. Its free plan allows 30 lookups a day, and each check costs one per stop. With two stops, checking every 12 minutes from 07:00 to 08:30 and 17:00 to 18:00 uses 28 a day. The board keeps 2 spare for test lookups. It never goes over **Most TransportAPI lookups a day**: once the budget is spent, bus times stop updating until midnight, and after the "not updating" time passes the board says so.

Bus positions for the [screen's bus page](displays.md#the-bus-position-page) come from BODS, with no daily allowance to manage. They're checked every 30 seconds, only during the bus windows, and never written to the SD card.

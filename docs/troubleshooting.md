# Troubleshooting

Start with the settings' [Overview](settings.md#overview): it says what is working, what isn't, and what that means for you. The logs are listed in [Looking after a Pi](pi.md#logs), and sign-in problems in [If you can't sign in](password.md#if-you-cant-sign-in).

| What you see | Likely cause | What to do |
|---|---|---|
| Board says **Set me up** | Not set up yet | Open the address it shows and follow the [setup wizard](setup-wizard.md) |
| Board says **Board off** | The board view is off | Tick **The board** under **Views** on the General page of the settings |
| Setup can't find a postcode, stops or bus companies | The Pi can't reach the services setup asks, or one is down | Check the Pi's internet connection; `journalctl -u twobells-fetch -n 20` |
| Setup says **The fetch service didn't answer in time** | The fetch service isn't being started | `sudo systemctl enable --now twobells-fetch.path`; `systemctl status twobells-fetch.path twobells-fetch.service` |
| Setup keeps saying **didn't answer in time**, and `systemctl status twobells-fetch.path` says `failed` with `start-limit-hit` | The fetch service kept stopping at once, so systemd stopped starting it | Find why with `journalctl -u twobells-fetch -n 30` and fix that, then `sudo systemctl reset-failed twobells-fetch.service twobells-fetch.path` and `sudo systemctl start twobells-fetch.path` |
| Setup says **The board's settings can't be read** | The fetch service can't read `/etc/twobells/config.toml` | `journalctl -u twobells-fetch -n 20` says why; fix `config.toml`, or delete it to use the defaults |
| A command says `Run this as the service's user: sudo -u twobells …` | It was run as root, on the services' store | Run it as `sudo -u twobells /opt/twobells/venv/bin/twobells …` |
| E-paper blank after install | The board isn't started, or nothing fetched yet | `sudo systemctl enable --now twobells.timer`; wait a minute; `journalctl -u twobells -f` |
| `NOT UPDATING: TRAINS`, or `! NOT UPDATING` by TRAINS | Rail key wrong, or the feed is down | Test the key on the Keys page of the settings; `twobells probe train` (on a Pi, `sudo /opt/twobells/venv/bin/twobells probe train`: see [Checking the keys and codes](pi.md#checking-the-keys-and-codes)) |
| `NOT UPDATING: BUSES`, or `! NOT UPDATING` by BUSES | No bus timetable yet and no TransportAPI keys; or TransportAPI's daily budget spent, its key wrong, or its feed down | **On the board** in the settings' Overview; `twobells probe bus` (on a Pi, `sudo /opt/twobells/venv/bin/twobells probe bus`) |
| Every section says `NOT UPDATING` just after the Pi or the board started | The board takes nothing it saved for new until it has checked again since the start, as a Pi without a clock battery can start with the wrong time; outside the checking times it checks once, at the start | Usually nothing: it clears at the first check that works. If it doesn't, the network or the feeds are down: see the rows above. Buses from TransportAPI wait for their own checking times |
| Every bus says `sch` | No live positions for those buses right now, or live positions failing (the Overview says so) | Usually nothing; times are from the timetable |
| The Overview says `From TransportAPI: there is no bus timetable yet` | The timetable hasn't been built, or the build failed | `journalctl -u twobells-timetable -n 20`; start it as in [Bus times](buses.md) |
| The Overview says `From TransportAPI: the bus timetable is for other stops` | You changed the stops; on a Pi it rebuilds once they are saved, which takes a few minutes | Wait, or start the timetable service |
| The Overview says `From TransportAPI: the bus timetable is over four weeks old` | Its builds have been failing | `journalctl -u twobells-timetable -n 20` says why |
| The Overview's bus timetable says **Out of date**: *it was built by an older version* | You updated, and the new release keeps the timetable differently | Nothing: it is built again by itself within the hour, and the old one is used until then (see [After an update](buses.md#after-an-update)) |
| The board's Trams section says `No trams`, or the dashboard's says there is no tram timetable yet | The Metrolink timetable hasn't been built yet: on a Pi the build starts when the settings are saved and takes a minute or two; in Docker and on a desktop, the dashboard builds it | Wait; the Overview's **Tram timetable** line says when it was saved. To build it now, see [Tram times](trams.md#how-the-timetable-is-kept) |
| `NOT UPDATING: TRAMS`, or `! NOT UPDATING` by TRAMS, with Metrolink stops | No tram timetable, or one for other stops or over four weeks old: its builds are failing | `journalctl -u twobells-timetable -n 20` says why (`Tram timetable not built: …`); it tries again after 6 hours, or start it as in [Tram times](trams.md#how-the-timetable-is-kept) |
| London's section not updating, or the Overview says *The board's London times may be out of date* | TfL is down or refusing; with a key, the key may be wrong (TfL answers a wrong key as if its allowance were used up: `TfL: HTTP Error 429`) | Test the key on the Keys page of the settings, or remove it: London's times work without one (see [London](london.md#how-often-it-asks)) |
| The settings or the board say `TFL_APP_KEY in secrets.env isn't a key` | The key was pasted with something else, like a space inside it | Paste the Primary key again on the Keys page (see [Getting your keys](keys.md#transport-for-london)) |
| The board doesn't start: `Config error: [board] modes: … has room for 2 modes at most, each with its own section, not 3` | `[board] modes` in `config.toml` names more modes than the display has room for, in its look and text size | Name fewer, or choose a look or text size with room for them (see [Modes of transport](configuration.md#modes-of-transport)) |
| The Board page doesn't offer the plain look, or a bigger text size | The board shows three modes, and that look or size has no row for the third | Untick a mode under **Modes of transport**, or keep a look with room; the page says which sizes fit |
| Buses not updating while the Overview says `From TransportAPI` | Outside `fallback_windows`, or the daily budget is spent | Fix the timetable (see the row above); TransportAPI is only a stand-in |
| The board's log says `Can't save the board's state`, and TransportAPI's buses stop updating | The SD card is full, or has gone read-only (cards often do as they wear out) | `df -h /var/lib/twobells`; a read-only card needs replacing, after a [backup](backup.md). The board keeps showing what it fetches, but makes no TransportAPI lookup it can't record as spent. On e-paper each minute's run then fails (`systemctl status twobells` says `failed`), after drawing; it remembers its last redraw in memory, in `/run/twobells`, so the panel is redrawn only when what it shows changes, and what each check fetched, so each source is still checked only at its **Check every** time (a card that never saved, or whose state won't read, has everything checked once, at the first run), and a section saying "not updating" as its checking time ends goes on saying so. That memory goes when the Pi restarts |
| E-paper errors about chip select | SPI chip selects in use | Add `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` and reboot |
| Screen black | Screen service not running | `systemctl status twobells-screen.service` |
| Screen upside down or sideways | Rotation | Change **Rotation** on the Board page of the settings |
| Scrolling text on the screen jerks | Each second's board takes too long to make or show | Log the screen's timings, as in [Logging the screen's timings](#logging-the-screens-timings) below |
| No bus position page | Not a screen, **Bus page** unticked under **What to show** on the Board page, outside the bus windows, no `BODS_API_KEY`, or no route set up for the next bus | Check those in that order |
| Bus page always says `Not yet past …` | The strip is short (in town 8 stops is about 7 minutes) | Normal; the bus appears when it's close |
| No rain line on a rainy day | **Rain warning** unticked under **What to show** on the Board page, the chance is below the threshold, it's outside the checking windows, no location, or the forecast couldn't be fetched for over 3 hours | Check the Overview of the settings, and **Weather** on the Trains and buses page |
| Board quiet on a bank holiday | Weekday windows are off on bank holidays | Add a window including Sun, or choose **Ignore them** under **Bank holidays** on the General page |
| Something the board or dashboard showed has gone | It is unticked under **What to show** on the Board or Dashboard page, or a preset left it out | Tick it again, or fill in from **Everything** |
| No `Leave by` (or `Leave in`) for a mode on the board | That mode is unticked under **When to leave**, or its stop has no walk time (setup leaves a walk over an hour empty) | Tick it under **What to show** on the Board page; type the walk on the Trains and buses page |
| The dashboard shows the time and the weather where Next to catch was | No mode is ticked for Next to catch, or nothing it picks from can still be reached (late at night, or no walk times) | Tick the modes for **Next to catch** on the Dashboard page; set the walks on the Trains and buses page |
| Setup's tram step lists no stops | None is within 5 km of home | Type part of a stop's name in **Find a stop by name**, or type its code under **Tram stop codes** on the Trains and buses page (see [Tram times](trams.md#setting-it-up)) |
| No calling-at line or clock page on the screen | Neither is in The usual, the preset a new install starts with; e-paper never shows them | Tick **Calling at** or **Clock page** under **What to show** on the Board page |
| The screen says no station announcements | **Announcements** isn't ticked (no preset ticks it), it's outside the train checking times or in the dashboard's quiet hours, or there's no sound | Tick it on the Board page; `journalctl -u twobells-screen -n 20` says `espeak-ng isn't installed` (run `update.sh` again) or `is there a speaker?` |
| No code on the board when claiming the settings | The board isn't running | Start it, or print a code on the Pi with `--reset-code` (see [Password and reset codes](password.md)) |
| The dashboard's address opens the settings | The dashboard view is off, the app isn't set up, or the dashboard's settings don't load | The settings' Overview says why when it can't start; tick **The dashboard** under **Views** on the General page on a Pi |
| The dashboard on a Pi doesn't update | The fetch service isn't running | `systemctl status twobells-fetch.service`; `journalctl -u twobells-fetch -n 20` |
| The dashboard says there are no buses because they come from TransportAPI | It shows buses only from the BODS timetable | Run setup again to find your stops' bus companies |
| The Performance page says **Delay Repay needs to know where your journeys end** | No filter station | Set **Only trains calling at** on the Trains and buses page |
| The Performance page says **Delay Repay needs a Service Details key** | No `RDM_DETAILS_API_KEY` | Get one and add it on the Keys page (see [Getting your keys](keys.md#rail-data-marketplace-service-details)) |
| The Overview says **Network guard: Off** (a Pi) | The guard's rules didn't load, or something flushed every nftables rule since (`sudo nft flush ruleset`, a restart of `nftables`), so nothing stops the web server connecting out | `sudo systemctl restart twobells-guard twobells-web twobells-fetch`: loading the guard again empties its sets, so the web server and the fetch service restart after it to be put back in them. If it stays off, `journalctl -u twobells-guard -u twobells-web` says why (see [Checking the guard](security.md#checking-the-guard)) |
| `update.sh` says a service `stopped on the new release` and the update was undone | The new release crashed as it started, so the Pi went back to the release from before, which is running | Run the `journalctl` command it printed to see why; fix that (a setting, say) and run `update.sh` again, or stay on the release you have (see [Where the app is](install-pi.md#where-the-app-is)) |
| `update.sh` says `twobells.service, the e-paper board, failed on the new release` and the update was undone | The board's run on the new release failed or hung, so the Pi went back to the release from before and drew the board on it | `journalctl -u twobells -n 30` says why. If the release from before fails too (`update.sh` says so), it's likely the panel: check its cable and that SPI is on, then run `update.sh` again |
| `update.sh` or `install.sh` says `Another install.sh or update.sh is running` | One is running already, from another login or an `upgrade.sh` | Let it finish, then run yours again. `pgrep -af deploy/pi/` shows it |
| `update.sh` says the SD card is too full for the board to save its state | Less than 10 MB, or room for fewer than 1000 files, is free where the board saves its state, so the board couldn't save it | `df -h /var/lib/twobells` and `df -i /var/lib/twobells` say how much is free; make room (`sudo apt-get clean`, `sudo journalctl --vacuum-size=50M`), then run `update.sh` again |
| `update.sh` says the e-paper board `drew on the new release, but couldn't save its state` | The SD card filled up, ran out of files or went read-only during the update. The new release stays and is in use: the rest of the update goes on as far as the card lets it, and `update.sh` (and `upgrade.sh`) end with status `110`, saying which the card is. The first upgrade from 0.5.3 or earlier runs that release's `upgrade.sh`, which on a read-only card ends `1` after `rm` errors instead | Make room as in the row above. If the card has gone read-only (`touch /var/lib/twobells/x` as root says `Read-only file system`), it may be failing: [back it up](backup.md) and restart the Pi. What the card wouldn't let it do (turn the timetable units on, remove older releases, clear its work in `/tmp`) it names, and the next update or a restart does. If it says there's room now, the board's next run, within a minute, saves its state: `systemctl status twobells` says whether it did. If it says the update `stopped part way, after keeping the new release`, a step failed for another reason (its error is above): see to the card, then run `update.sh` again |
| Settings won't open | Server stopped, or you're not on the home network | `systemctl status twobells-web.service` on a Pi; the container's state in Docker |
| Settings say **Forbidden** | Opened from outside the home network, or through an unusual address | Use `http://<name>.local:8091/settings` from home Wi-Fi, or add the name to `allowed_hosts` (see [Addresses it answers to](security.md#addresses-it-answers-to)) |
| Opening the settings says **The settings are on port 8091** | That was port 8090, the dashboard's, from an old bookmark: the settings have a port of their own | Follow its link and change the bookmark. If that doesn't open: in Docker or on Unraid, publish port 8091 as well (see [Updating](docker.md#updating)); on a Pi, run `update.sh` again, which opens it |

## Logging the screen's timings

`twobells run --loop --timings` logs a line a second: how long that second's board took to make and to show, and how much of the screen changed. It also says when frames are skipped. To have the screen's service run it, give the service a drop-in file with that command. These commands write one with `tee`, so no editor is needed, and restart the screen onto it:

```bash
sudo mkdir -p /etc/systemd/system/twobells-screen.service.d
printf '%s\n' '[Service]' 'ExecStart=' \
  'ExecStart=/opt/twobells/venv/bin/twobells run --loop --timings --config /etc/twobells/config.toml' |
  sudo tee /etc/systemd/system/twobells-screen.service.d/timings.conf
sudo systemctl daemon-reload
sudo systemctl restart twobells-screen.service
journalctl -u twobells-screen -f
```

The empty `ExecStart=` clears the unit's own command, so the line after it replaces that command rather than adding a second one.

To write the same three lines in an editor instead, run `sudo systemctl edit twobells-screen.service`. It opens the editor `SYSTEMD_EDITOR` or `EDITOR` names, or else the system's default editor; to choose one, name it: `sudo env SYSTEMD_EDITOR=nano systemctl edit twobells-screen.service`. Type the lines between the two comment lines at the top, as the file says: anything below the second is thrown away. Save and close the editor, then `sudo systemctl restart twobells-screen.service`: the change takes effect only once the screen's service restarts.

Take it out when you're done, as the drop-in stays through updates and keeps the screen on this command whatever a new release's unit says:

```bash
sudo rm /etc/systemd/system/twobells-screen.service.d/timings.conf
sudo systemctl daemon-reload
sudo systemctl restart twobells-screen.service
```

`sudo systemctl revert twobells-screen.service` removes every drop-in for it, the one `systemctl edit` writes too; then restart it as above.

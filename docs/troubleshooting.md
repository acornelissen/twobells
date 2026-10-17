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
| `NOT UPDATING: TRAINS`, or `! NOT UPDATING` by TRAINS | Rail key wrong, or the feed is down | Test the key on the Keys page of the settings; `twobells probe train` |
| `NOT UPDATING: BUSES`, or `! NOT UPDATING` by BUSES | No bus timetable yet and no TransportAPI keys; or TransportAPI's daily budget spent, its key wrong, or its feed down | **On the board** in the settings' Overview; `twobells probe bus` |
| Every bus says `sch` | No live positions for those buses right now, or live positions failing (the Overview says so) | Usually nothing; times are from the timetable |
| The Overview says `From TransportAPI: there is no bus timetable yet` | The timetable hasn't been built, or the build failed | `journalctl -u twobells-timetable -n 20`; start it as in [Bus times](buses.md) |
| The Overview says `From TransportAPI: the bus timetable is for other stops` | You changed the stops; it rebuilds within the hour | Wait, or start the timetable service |
| The Overview says `From TransportAPI: the bus timetable is over four weeks old` | Its builds have been failing | `journalctl -u twobells-timetable -n 20` says why |
| The Overview's bus timetable says **Out of date**: *it was built by an older version* | You updated, and the new release keeps the timetable differently (0.2.0 and 0.2.2 do) | Nothing: it is built again by itself within the hour, and the old one is used until then |
| Buses not updating while the Overview says `From TransportAPI` | Outside `fallback_windows`, or the daily budget is spent | Fix the timetable (see the row above); TransportAPI is only a stand-in |
| E-paper errors about chip select | SPI chip selects in use | Add `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` and reboot |
| Screen black | Screen service not running | `systemctl status twobells-screen.service` |
| Screen upside down or sideways | Rotation | Change **Rotation** on the Board page of the settings |
| No bus position page | Not a screen, **Bus page** unticked under **What to show** on the Board page, outside the bus windows, no `BODS_API_KEY`, or no route set up for the next bus | Check those in that order |
| Bus page always says `Not yet past …` | The strip is short (in town 8 stops is about 7 minutes) | Normal; the bus appears when it's close |
| No rain line on a rainy day | **Rain warning** unticked under **What to show** on the Board page, the chance is below the threshold, it's outside the checking windows, no location, or the forecast couldn't be fetched for over 3 hours | Check the Overview of the settings, and **Weather** on the Trains and buses page |
| Board quiet on a bank holiday | Weekday windows are off on bank holidays | Add a window including Sun, or choose **Ignore them** under **Bank holidays** on the General page |
| Something the board or dashboard showed has gone | It is unticked under **What to show** on the Board or Dashboard page, or a preset left it out | Tick it again, or fill in from **Everything** |
| No calling-at line or clock page on the screen | Neither is in The usual, the preset a new install starts with; e-paper never shows them | Tick **Calling at** or **Clock page** under **What to show** on the Board page |
| The screen says no station announcements | **Announcements** isn't ticked (no preset ticks it), it's outside the train checking times or in the dashboard's quiet hours, or there's no sound | Tick it on the Board page; `journalctl -u twobells-screen -n 20` says `espeak-ng isn't installed` (run `update.sh` again) or `is there a speaker?` |

| No code on the board when claiming the settings | The board isn't running | Start it, or print a code on the Pi with `--reset-code` (see [Password and reset codes](password.md)) |
| The dashboard's address opens the settings | The dashboard view is off, the app isn't set up, or the dashboard's settings don't load | The settings' Overview says why when it can't start; tick **The dashboard** under **Views** on the General page on a Pi |
| The dashboard on a Pi doesn't update | The fetch service isn't running | `systemctl status twobells-fetch.service`; `journalctl -u twobells-fetch -n 20` |
| The dashboard says there are no buses because they come from TransportAPI | It shows buses only from the BODS timetable | Run setup again to find your stops' bus companies |
| The Performance page says **Delay Repay needs to know where your journeys end** | No filter station | Set **Only trains calling at** on the Trains and buses page |
| The Performance page says **Delay Repay needs a Service Details key** | No `RDM_DETAILS_API_KEY` | Get one and add it on the Keys page (see [Getting your keys](keys.md#rail-data-marketplace-service-details)) |
| The Overview says **Network guard: Off** (a Pi) | The guard's rules didn't load, so nothing stops the web server connecting out | `sudo systemctl restart twobells-web`; if it stays off, `sudo journalctl -u twobells-web` says why (see [How the web server is kept safe](security.md)) |
| Settings won't open | Server stopped, or you're not on the home network | `systemctl status twobells-web.service` on a Pi; the container's state in Docker |
| Settings say **Forbidden** | Opened from outside the home network, or through an unusual address | Use `http://<name>.local:8090/settings` from home Wi-Fi, or add the name to `allowed_hosts` |

# Departure board user guide

The board shows the next trains from Glossop to Manchester and the next buses from the Henry Street stops, and tells you when to set off so you catch them.

This guide has three parts:

1. [Reading the board](#1-reading-the-board): for everyone in the house.
2. [Changing settings](#2-changing-settings): using the settings page from a phone or laptop.
3. [Setting up and looking after it](#3-setting-up-and-looking-after-it): for whoever installs and maintains it.

Developer details (code layout, tests, the emulator) are in the [README](../README.md).

---

## 1. Reading the board

The board can run on three kinds of display. They all show the same information:

| Display | Size | How it updates |
|---|---|---|
| Inky pHAT e-paper | small, 2.13" | Redraws only when something changes, at most every 2 minutes. Each redraw takes about 20 seconds and flickers. That's normal. |
| Waveshare e-paper | wide, 10.85" | Same as the pHAT, just bigger. |
| HDMI screen | wide, 7.9" | Updates every second, with a running clock, countdowns and a second page showing where your bus is. |

The words always carry the meaning. Colour and highlighting only back them up, so the board reads the same in black and white or to someone who is colour-blind.

### The station board look

This is the usual look: amber dots on black, like a railway station sign.

![The station board look on the Inky pHAT](guide/phat-sign.png)

**Trains (top half)**

Each line reads: *which train*, *scheduled time*, *destination*, *status*.

| You see | It means |
|---|---|
| `1st 07:12 … On time` | The next train, running to time. |
| `Exp 07:49` | Running late; expected to leave at 07:49. |
| `Delayed` | Late, but no expected time yet. |
| `Cancelled`, on a highlighted bar | It isn't running. |
| `No info` | No information about this train yet. |
| `Leave by 07:05` | Set off at 07:05 to walk to the station and catch the train above it. It sits under the first train you can still reach, so if the 1st is too soon, it moves down to the 2nd. |

**Buses (bottom half)**

Each line reads: *route*, *stop*, *destination*, *when to leave*, *when it goes*.

| You see | It means |
|---|---|
| `341 A Hyde` | Route 341 from Henry Street Stop A, going to Hyde. |
| `leave 07:17` | Set off at 07:17 to walk to that stop in time. |
| `07:41` | Live tracking says it's on time. |
| `07:24 +4` | Live tracking says it's 4 minutes late and will leave at 07:24. |
| `07:35 sch` | Timetable time only ("scheduled"): there's no live tracking for this bus, so it could be early or late. |
| `Cancelled`, on a highlighted bar | It isn't running. |

**Bottom line**

| You see | It means |
|---|---|
| `UPDATED 07:03` | When the board last changed. The e-paper only redraws when something changes, so this can be a few minutes ago. |
| `NOT UPDATING: BUSES`, highlighted | No new bus information has arrived for a while. What's shown is the last known, and may be out of date. The same applies to trains. |

### The plain look

The same information in black on white, with red highlights. The words are the same as the station board look; only where some of them sit differs. Someone may have switched to it on the settings page.

![The plain look on the Inky pHAT](guide/phat-plain.png)

The differences:

| Plain look | Station board look |
|---|---|
| `Upd 07:03` in the top bar | `UPDATED 07:03` at the bottom |
| `Leave 07:05 (07:12)` next to TRAINS: leave at 07:05 for the 07:12 | `Leave by 07:05` under the train |
| `+7` in the accent colour: 7 minutes late | `Exp 07:49` |
| `Cancelled` on a black bar | `Cancelled` on a highlighted bar |
| `! NOT UPDATING` in the accent colour, next to TRAINS or BUSES | `NOT UPDATING: …` at the bottom |

The plain look shows 3 trains and 2 buses.

### The bigger e-paper display

The 10.85" display shows the same board with bigger dots and more room, so long destinations fit.

![The station board look on the 10.85 inch e-paper](guide/epaper-10in85.png)

### The HDMI screen

The screen can redraw every second, so it shows a bit more:

![Departures on the HDMI screen](guide/screen-departures.png)

- **A running clock** at the bottom, in place of "Updated".
- **Countdowns** in place of clock times: `Leave in 3 min`, then `Leave now` in the last minute. They round down, so `Leave in 3 min` means you have at least 3 minutes, never less.
- On the screen, a bus you can no longer reach on foot loses its leave time, rather than saying "leave now".

#### Where the next bus is

If it's set up, the screen switches every 25 seconds: 15 seconds of departures, then 10 seconds of this page.

![The bus position page on the HDMI screen](guide/screen-bus.png)

- **Top line:** the bus you can still catch, and when it goes (`341 to Hyde … 07:24 +4`).
- **Second line:** when to leave, and how many stops away the bus is, counting your stop.
- **The line of stops:** the stops the bus calls at before yours. Your stop is the highlighted one on the right; the bus is drawn on the road where it is now.

It also says:

| You see | It means |
|---|---|
| `Not yet past Bowland Road` | The bus is further back than the first stop shown. |
| `At your stop` | It's at your stop now. |
| `Position not updating` | Live positions have stopped arriving. Use the departure times instead. |

The page only appears when the board is checking buses (see *When it checks* below) and there's a tracked bus you can still catch.

### When it checks

To stay within free limits, the board only fetches new times during set windows, for example weekday mornings and early evenings. Outside them it keeps showing what it last knew, and doesn't say "not updating", because it isn't trying to. The windows are on the settings page.

---

## 2. Changing settings

The settings page works from any phone or laptop on the home network.

1. Open `http://<board name>.local:8080/` (for example `http://departureboard.local:8080/`).
2. Sign in with the settings password.
3. Change what you need, then press **Save settings** at the bottom.

![The settings page](guide/settings-page.png)

If something isn't right, nothing is saved. A box at the top lists the problems, each named by section and field (for example *Trains – Station code*); click one to jump to it, where the error is shown in words.

What you save replaces what's in `config.toml`, including emptying a box: clearing *Only trains calling at* really does show every train again.

### What you can change

**Board**

- **Title:** shown in the plain look's top bar.
- **Look:** station board or plain.
- **Accent colour:** the look's own (amber for the station board, red for plain), or another that stays readable on the look's background. A colour too faint on the background isn't offered.
- **Rotation:** if the display is mounted upside down (or, for the screen, on its side).
- **Least minutes between redraws:** e-paper only. Lower means fresher but more wear and flicker; 2 is a good balance.

**Trains**

- **Station code:** 3 letters, `GLO` for Glossop.
- **Only trains calling at:** for example `MAN`, so only trains to Manchester show. Empty shows every train.
- **Trains shown**, **Check every (minutes)**, and **Say "not updating" after (minutes)**.
- **When to check:** one window per line, written like `Mon-Fri 06:30-09:30`. Days can be `Mon-Sun`, `Sat,Sun`, `Mon-Fri`, or wrap round the week like `Fri-Mon`. A window can run past midnight: `Fri 22:00-01:00` runs from Friday night into Saturday morning.
- **Walk to the station (minutes):** used for the leave time. Empty means no leave time.

**Buses**

- **Stops:** one per line, written `LABEL = ATCO code`, for example `A = 1000DGHS0900`. The label is the letter shown on the board. Find stop codes on [bustimes.org](https://bustimes.org): the code is the last part of a stop page's address.
- **Only buses heading to:** optional, comma-separated, for example `Ashton, Hyde`. Empty shows every bus.
- **Buses shown**, **Check every (minutes)**, **Say "not updating" after (minutes)**.
- **Most hits a day:** a hard limit on bus lookups. The free TransportAPI plan allows 30 a day, and each check costs one per stop.
- **When to check:** as for trains.
- **Walk to each stop:** one per line (`A = 7`), or one number for every stop. Empty means no leave times.

**Keys**

Keys are never shown, only whether each one is set. To replace a key, paste the new one; leave a box empty to keep the current one. The key boxes are ordinary text boxes, so you can check what you pasted before saving.

### When changes take effect

- **E-paper:** within a few minutes (it never redraws more often than every 2 minutes). Changing the look, accent colour or rotation redraws it too.
- **HDMI screen:** within a second. If you change the rotation or accent colour, the screen restarts itself, which takes about 10 seconds.

Two things aren't on the page and are changed in `config.toml` on the Pi: the type of display, and the stop list for the bus position page.

### If you can't sign in

- **"That password isn't right":** after 5 wrong tries, that device is locked out for 15 minutes and sees "Too many attempts. Try again in 15 minutes."
- **"Busy, try again in a moment":** a few sign-ins were being checked at once; try again.
- **"Set a password first":** no password has been set yet; see [Setting up the settings page](#the-settings-page).
- **Forgotten password:** set a new one on the Pi (same command as when setting it up).
- **"Forbidden":** the page only answers devices on the home network, using its local name or address. It won't work over mobile data or a VPN to elsewhere.

---

## 3. Setting up and looking after it

Step-by-step install commands are in the [README](../README.md#2-install-on-the-pi). This part covers the choices and day-to-day care.

### Choosing a display

| | Inky pHAT | Waveshare 10.85" e-Paper (G) | 7.9" HDMI screen |
|---|---|---|---|
| Size | 250×122 | 1360×480 | 1280×400 (on its side) |
| Colours | black, white, red or yellow | black, white, red, yellow | full colour |
| Redraw | about 20 s, on change only | about 21 s, on change only | instant, every second |
| Clock, countdowns, bus page | no | no | yes |
| Light at night | none | none | the screen stays lit |
| Pi | Zero W or later | Zero 2 W or later | Zero 2 W or later (Zero needs a mini-HDMI adapter) |
| `display =` in `config.toml` | `"inky"` | `"waveshare-10in85g"` | `"hdmi-1280x400"`, plus `rotate = 90` or `270` |
| Runs as | `departureboard.timer` | `departureboard.timer` | `departureboard-screen.service` |

The Waveshare panel also needs `dtoverlay=spi0-0cs` in `/boot/firmware/config.txt`.

### Keys

All three are free. Put them in `/etc/departureboard/secrets.env`, or paste them into the settings page.

| Key | For | Where to get it |
|---|---|---|
| `RDM_API_KEY` | trains | Rail Data Marketplace, Live Departure Board product |
| `TAPI_APP_ID`, `TAPI_APP_KEY` | bus times | TransportAPI, free plan |
| `BODS_API_KEY` | bus positions (screen only) | Bus Open Data Service, account settings |

### The bus budget

TransportAPI's free plan allows 30 lookups a day, and each check costs one per stop. With two stops, checking every 12 minutes from 07:00 to 08:30 and 17:00 to 18:00 uses 28 a day. The board keeps 2 spare for test lookups. It never goes over **Most hits a day**: once the budget is spent, bus times stop updating until midnight, and after the "not updating" time passes the board says so.

Bus positions for the screen's second page come from a different service (BODS) with no daily allowance to manage. They're checked every 30 seconds, only during the bus windows.

### The bus position page

Screen only. It needs `BODS_API_KEY` and a stop list for each route you want to follow. Build the list on a laptop:

```bash
departureboard route 341 --stop A --noc BNML --config config.toml --secrets secrets.env
```

Paste the printed block into `config.toml`. It picks enough stops before yours to cover your walk plus 5 minutes of bus travel, at most 8. `BNML` is the operator code for Bee Network. For other operators, `probe bus --save DIR` writes TransportAPI's raw answers, which include an `operator` field.

Most 341s to Hyde start at Henry Street Stop A, so the page follows the 341 arriving into Glossop, which then turns round as your bus.

### The settings page

Set a password (10 characters or more), then start the page:

```bash
sudo -u departureboard /opt/departureboard/venv/bin/departureboard web --set-password
sudo systemctl enable --now departureboard-web.service
```

The page uses plain HTTP on your home network. Use a password you don't use anywhere else.

Settings saved on the page go to `/var/lib/departureboard/settings.toml` and override `config.toml`, which the page never changes. Keys pasted on the page go to `/var/lib/departureboard/secrets.env`. To undo everything saved on the page, delete those two files.

### Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| E-paper blank after install | No data fetched yet | Wait a minute; check `journalctl -u departureboard -f` |
| `NOT UPDATING: TRAINS` | Rail key wrong, or the feed is down | `departureboard probe train` |
| `NOT UPDATING: BUSES` | Daily bus budget spent, key wrong, or the feed is down | `departureboard probe bus` (uses one lookup per stop) |
| Every bus says `sch` | No live tracking for those buses right now | Nothing to fix; times are from the timetable |
| E-paper errors about chip select | SPI chip selects in use | Add `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` and reboot |
| Screen black | Screen service not running | `systemctl status departureboard-screen.service` |
| Screen upside down or sideways | Rotation | Change Rotation on the settings page |
| No bus position page | Not a screen, outside the bus windows, no `BODS_API_KEY`, or no route set up for the next bus | Check those in that order |
| Bus page always says `Not yet past …` | The strip is short (in town 8 stops is about 7 minutes) | Normal; the bus appears when it's close |
| Settings page won't open | Service stopped, or you're not on the home network | `systemctl status departureboard-web.service` |
| Settings page says Forbidden | Opened from outside the home network, or through an unusual address | Use `http://<board name>.local:8080/` from home Wi-Fi |

### Useful commands

On the Pi, run these as `sudo /opt/departureboard/venv/bin/departureboard …`; on a laptop with the project installed, `departureboard …` is enough.

```bash
journalctl -u departureboard -f                 # e-paper board log
journalctl -u departureboard-screen -f          # screen board log
journalctl -u departureboard-web -f             # settings page log
departureboard probe train                      # check the rail key and station
departureboard probe bus                        # check the bus keys and stops (costs lookups)
departureboard demo --png preview.png           # draw a sample board without any keys
departureboard emulate --demo --display hdmi-1280x400   # try the screen on a laptop
```

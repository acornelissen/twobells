# The settings

The settings are at `/settings` on the install's address, port 8090, from any phone or computer on the home network. Sign in with the settings password.

They are in pages by task, one tab each:

- **[Overview](#overview)** (`/settings`): whether everything is working, where your departures are for, the keys' state, and shortcuts. It only shows; nothing is changed here.
- **[Trains and buses](#trains-and-buses)** (`/settings/trains-and-buses`): the title, your station, stops and the weather, which both views use.
- **[Board](#board)** (`/settings/board`): the board's own settings. Only while the board view is on.
- **[Dashboard](#dashboard)** (`/settings/dashboard`): the dashboard's own. Only while the dashboard view is on.
- **[Keys](#keys)** (`/settings/keys`): the keys, each in a small form of its own, saved and tested on its own.
- **[General](#general)** (`/settings/general`): the views and bank holidays, and the time zone.

A setting is on one page only. Each page's settings are in sections; a page with more than three lists them at the top, to jump to. The settings most people never change wait under **Advanced** at the end of their section: open it to see them. A section that is switched off says **Off** by its heading, and keeps its settings for turning it back on; one that isn't set up yet says **Not set up**, with a link to the [setup wizard](setup-wizard.md). Numbers have their unit beside the box; something set in `config.toml` only, like the display, is shown with **Set in config.toml** and can't be changed here (see [Settings by hand](configuration.md)).

Change what you need on a page, then press **Save changes** in the bar at the foot of the window, which also says when the changes take effect. As you go, each field you have changed is marked **Changed**, and the bar says how many changes aren't saved yet, like *2 unsaved changes*; put a field back as it was and its mark goes. Leaving the page, by a tab or by closing it, asks first while there are changes not saved. On the Keys page each key's form counts its own. In a checking time or quiet hours row, **Weekdays**, **Weekends** and **Every day** tick its days at once. These need JavaScript; without it the pages work the same, without the marks. The page then says what you changed, for example *Saved 2 changes: Title; Trains shown, 3 to 2* (never a key). If something isn't right, nothing is saved. A box under the tabs lists the problems, each named by section and field (for example *Trains – Station code*); click one to jump to it, where the error is shown in words. Nothing is saved either unless the settings then load in every view that is on, so a change for one view can't break the other.

What you save replaces what's in `config.toml`, including emptying a box: clearing *Only trains calling at* really does show every train again. Settings a page doesn't show are kept, like the bus companies and route strips setup found, the weather's location, and the other pages'.

## Overview

- If the dashboard can't start, why, at the top.
- **Health:** one line, **All working** or how many problems there are, as of the time the page was drawn, with **Reload** to look again; on a bank holiday, that the board checks at its Sunday times. While all is working the feeds wait behind **Show each feed**, so the rest of the page comes up; with a problem they show at once. Each feed, under **On the board** and **On the dashboard** when both views are on: trains, where each train is (with its key), the bus times from the timetable and the live bus positions (or the board's buses with TransportAPI's lookups used today, when the board uses it), the weather, engineering works and bus disruptions (with their keys), and on a Pi the board's display and when it last redrew. For each: when it last updated, whether it is being checked now (or resting outside its checking times, or in the dashboard's quiet hours), the last error in words, and, when something is wrong, what that means for you, like *The dashboard's train times may be out of date.* A feed not heard from yet says **Waiting**, which isn't a problem. On a Pi, **Network guard** under **On this device** says whether the web server can connect out to the internet, and when it last checked (in the board's time zone): **On**, **Off** (a problem: nothing stops it, though it still refuses visitors from outside your home network itself) or **Can't tell**. It checks when the server starts, and again when the page is drawn, at most once a minute. See [How the web server is kept safe](security.md).
- **Your place**, in names: the station, its walk, and the one trains must call at, like *Glossop (GLO), 12 min walk; only trains calling at Manchester Piccadilly (MAN)*; each bus stop with its name from the bus timetable and its walk, like *A: Henry Street, 5 min walk* (its stop code until the timetable has it); the bus timetable, saying **Out of date** and why only when it is due to be built again (a week old, your stops changed, or a timetable in it ended), and how finding the bus maps' roads is getting on; whether home is set (to about 1 km, for the weather); and whether the weather is on. Names come from the station list in the app and the bus timetable, so this page never asks the internet.
- **Keys:** each key the views on use, in one line: **Set**, **Not set**, or **Needed for** what uses it (trains, buses, the board's buses). Keys are never shown. **Change or test keys** opens the Keys page.
- **Shortcuts:** **Change home, station or stops** runs the whole [setup wizard](setup-wizard.md); **Change stops only** and **Change station only** start it at that step. **Open the dashboard**, while it runs. **Wall tablet:** the address for a [tablet on the wall](dashboard.md#wall-tablet-kiosk), made from the address you opened the settings with, like `http://glossop.local:8090/?kiosk`. Opened on the computer itself (`localhost`), the page says to use its address on the home network instead, as a tablet can't reach `localhost`.

The version running is at the foot of every page.

## Trains and buses

- **Title:** the board shows all of it. The dashboard shows the part before `>` as its heading: Glossop, from `Glossop > Manchester`.
- **Trains**
  - **Show trains:** on or off. Off, no trains are shown or checked for; the rest of this section is kept, unchecked, for turning them back on.
  - **Station code:** 3 letters, like `GLO` for Glossop, with the station's name for the one saved. National Rail's station pages show it.
  - **Only trains calling at:** a station code, for example `MAN`, so only trains to Manchester show. Empty shows every train.
  - **Walk to the station:** in minutes, for the leave time. Empty means no leave time.
- **Buses**
  - **Show buses:** on or off, as for trains.
  - **Stops:** the stops setup found, by their label (the letter shown on the board), name and stop code, with **Change them with setup**, which starts the wizard at its stops.
  - **Walk to each stop:** a box for each stop, named by the stop and its label, like *Henry Street (A)*, in minutes. An empty box means no leave time for that stop.
  - **Only buses heading to:** optional, separated by commas, for example `Ashton, Hyde`. Empty shows every bus.
  - Advanced: **Stop codes**, one per line, written `LABEL = stop code`, for example `A = 1000DGHS0900`. Find stop codes on [bustimes.org](https://bustimes.org): the code is the last part of a stop page's address.
- **Weather** (once setup or `config.toml` gives a location; see [Rain warning set-up](configuration.md#rain-warning-set-up))
  - **Show the weather:** on or off. The rain warning on the board and the dashboard's forecast.
  - **Warn of rain when the chance is at least:** 50 % means warn when rain is more likely than not.
  - **Location:** shown, not changed here: setup sets it from home.

Trains and buses can't both be off, or there would be nothing to show.

## Board

How the board is doing is on the [Overview](#overview), under **On the board**: when trains, buses and bus positions were last fetched and whether it's checking now, the last error in words (keys are never shown), a note when bus times aren't coming from the timetable and live positions as they should (`From TransportAPI: …` or `Timetabled times only; live positions failed: …`), how many of today's TransportAPI lookups are used, and when the display last redrew. **See how the board is doing**, at the top of the Board page, opens it. It's the first place to look when the board seems wrong.

- **Display**
  - **Look:** station board or plain, and on an HDMI screen also TFT, white and yellow on black (see [Reading the board](reading-the-board.md#the-tft-look)), and split-flap, white letters on flipping tiles (see [Reading the board](reading-the-board.md#the-split-flap-look)). E-paper isn't offered either, and refuses them in `config.toml` (`theme = "tft"` or `"flap"`).
  - **Rotation:** if the display is mounted upside down (or, for the screen, on its side).
  - **Clock page:** on or off, off unless you switch it on. HDMI screens only: 10 seconds of a platform clock after the departures and the bus page (see [The clock page](reading-the-board.md#the-clock-page)). E-paper ignores it.
  - **Display:** which display `config.toml` sets. Changing it means switching services, so it's set there only.
  - Advanced: **Accent colour**, the look's own (amber for the station board, yellow for TFT and split-flap, red for plain) or another that stays readable on the look's background (a colour too faint on the background isn't offered); **Redraw at most every** so many minutes, e-paper only (lower means fresher but more wear and flicker; 2 is a good balance); **Text size**, the plain look only, 9 to 16 (11 is standard; bigger shows fewer departures on a small display).
- **Trains** and **Buses**, each:
  - **Trains shown** or **Buses shown**.
  - **When to check for trains** and **When to check for buses:** each checking time is a row of its own: tick its days, and give it a **From** and a **To** time, 24-hour like `06:30`. A To earlier than its From runs past midnight: Friday from 22:00 to 01:00 runs from Friday night into Saturday morning. Two empty rows follow the saved ones for new times; leave a row empty to remove it. Outside these times the board shows what it last knew, without saying "not updating". In `config.toml` the same times are written like `Mon-Fri 06:30-09:30`: days can be `Mon-Sun`, `Sat,Sun`, `Mon-Fri`, or wrap round the week like `Fri-Mon`, and `Fri 22:00-01:00` runs past midnight. A saved time the page can't show as days and times is shown as it is, and has to be removed before the page saves.
  - Advanced: **Check every** so many minutes, and **Say "not updating" after** so many minutes. For buses, while TransportAPI is used (its source, or its keys saved for it to stand in for the bus timetable), **Most TransportAPI lookups a day** too: a hard limit, used only while it stands in for the timetable. The free plan allows 30 a day, and each check costs one per stop.

## Dashboard

- **Departures**
  - **Most trains listed** and **Most buses listed:** the first 5 show; the rest are behind **Show later departures**. A wall tablet shows 4.
  - **Position strips in each row:** on or off. On, each departure row has a small drawing of its stops with the train or bus where it is; off, the rows keep their words without it. A wall tablet never shows them.
  - Advanced: **Check for trains every** and **Check for buses every** so many seconds; **Check the forecast every** so many minutes; **Count trains as late from** and **Count buses as late from** so many minutes, for the punctuality history; **Keep punctuality history for** so many days (1 to 3650).
- **Quiet hours**
  - **When to stop checking:** rows of days and times, as for the board's checking times; 01:00 to 05:00 every day unless you change it. The dashboard stops checking for departures and the weather then, unless a browser has it open: a wall tablet keeps it checking, and dims to a [platform clock](dashboard.md#wall-tablet-kiosk). Leave every row empty to check all the time.

The same settings can go under `[dashboard]` in `config.toml`: see [The dashboard's settings](configuration.md#the-dashboards-settings).

## Keys

Each key has a small form of its own, headed by what it unlocks, with a label saying how it stands: **Not set**, **Needed for trains**, **Saved, not tested**, **Works, tested 10 Oct 10:42** or **Refused, tested …**. Keys are never shown, not even here. Each form saves, and tests, its own key only.

[Getting your keys](keys.md) says where to get each key, and [Where keys go](keys.md#where-keys-go) how the forms and their tests work.

## General

- **Views** (a Pi only): the board, the dashboard, or both. At least one stays on.
- **Bank holidays:** trains and buses run Sunday times on bank holidays, so only checking times that include `Sun` apply then. Pick the part of the UK whose bank holidays to follow (England and Wales, Scotland, or Northern Ireland), or ignore them. Dates come from GOV.UK.
- Advanced: **Time zone**, like `Europe/London`, with every zone suggested as you type. Departures and checking times are in this zone.

## When changes take effect

- **E-paper:** within a few minutes (it never redraws more often than every 2 minutes). Changing the look, accent colour or rotation redraws it too.
- **HDMI screen:** within a second. If you change the rotation, accent colour or time zone, the screen restarts itself, which takes about 10 seconds.
- **The dashboard on a Pi:** within a few minutes.
- **The dashboard in Docker or on a desktop:** the server starts again by itself within a few seconds to use the new settings. You stay signed in.

The save bar says which applies before you save.

Turning a view off doesn't stop or disable any service. The board shows **Board off** and fetches nothing; the dashboard's address opens the settings.

A few things aren't on these pages, such as the type of display and `allowed_hosts`: see [Settings by hand](configuration.md).

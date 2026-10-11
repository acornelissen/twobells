# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

The first release.

### Added

- One app with two views of the same train and bus departures: the board, on an e-paper panel or an HDMI screen on a Raspberry Pi, and the dashboard, a web page for a phone or computer at home.
- Trains from National Rail Darwin. Buses from the BODS timetable and live positions, with TransportAPI standing in while there is no timetable.
- One settings site per install, at `/settings` on port 8090, with one password and a setup wizard that works from your postcode.
- Three installs: a Raspberry Pi, Docker, and a desktop (macOS or Linux).
- Signed releases: the package on PyPI, a signed container image on GHCR, and a signed source tarball for the Pi.
- A network guard for the Pi's web server: nftables rules for it alone (`deploy/pi/twobells-web.nft`), loaded at boot by `twobells-guard.service` and matching the server by the cgroup systemd puts in their set (`NFTSet=`, systemd 254 or later, so Raspberry Pi OS Trixie on a Pi Zero or Pi 1), that keep it from connecting out to the internet and keep port 8090 to the home network. They back up the unit's systemd IP firewall, which a Pi Zero's kernel can't apply ("Preparation of BPF allow maps failed"). `install.sh` and `update.sh` install `nftables`. The server checks at start whether it can connect out, warns in its log if it can, and the settings' Overview shows **Network guard**: On, Off (a problem) or Can't tell.
- A wall-tablet mode for the dashboard at `/?kiosk`: one screen with no controls, the station-board look by default, a shift of a pixel or two every few minutes against burn-in, dimmed in quiet hours, a large banner when the times stop updating, and a reload at 03:30.

### Changed

- The settings' Keys page has a small form for each key, headed by what it unlocks, with a label saying whether it is set, needed, works or was refused (and when it was tested). **Save key** saves that key alone; **Save and test** saves then tests it, so testing no longer empties the box and makes you paste the key again; **Test saved key** tests the key already saved. The last result is kept in `keytests.json` as works or refused and a time, never the key.
- The dashboard is redesigned so departures come first. Under the place name and clock sit **Next to catch** (the countdown, how sure it is, and the next train and bus if you miss it), one line for disruption, then trains and buses side by side.
  - Trains read like a station board (Time, Destination, Plat, Expected, Leave) and buses like a bus-stop countdown sign (line, destination, stop, due, leave).
  - A departure you can no longer walk to in time leaves the list. Each row opens one sheet with its details, map or journey, punctuality and **Remind me**.
  - Disruption is limited to your stops, nearby stops and your lines. Station facility notices, such as a closed ticket office, show as station information.
  - The weather shows now and the next four hours, with the rest under **More hours**.
  - One type (Inter, served by the install itself), one icon set and one colour scheme, with colour kept for what is late, cancelled or disrupted.
  - On a wide screen the weather sits beside Next to catch. A train or bus with a live position draws a small strip of its stops in its row.
  - **Position strips in each row**, on the Dashboard tab (`[dashboard] row_strips`, and `row_strips` in the snapshot), turns those strips off.
  - The **Board look** uses the e-paper sign's own dot-matrix lettering.
- The settings site and the setup wizard share the dashboard's type, colours and icons.
- Saving the settings, or restarting the server, no longer signs you out: the store keeps a hash of each session (`sessions.json`, readable by the app's user only). A new password still ends every session.
- The settings are in pages by task, each setting on one page: Overview, Trains and buses, Board, Dashboard, Keys and General. Short choices are radio buttons, numbers show their unit, rarely changed settings wait under **Advanced**, and a save bar says when changes take effect. A save says what it changed, and reloading the page after it can't post the form again.
  - Bank holidays are one choice: Sunday times for England and Wales, Scotland or Northern Ireland, or ignore them. `settings.toml` keeps `bank_holidays` and `bank_holiday_division` as before.
  - Checking times and quiet hours are rows of day checkboxes with From and To times, instead of lines like `Mon-Fri 06:30-09:30` to type. `settings.toml` keeps the same lines.
  - The keys and the engineering works address are on the Keys page, with their tests.
  - The Overview says whether everything is working (each feed's last update, checking times, last error and what it means), your place in names (*Glossop (GLO), 12 min walk*; *A: Henry Street, 5 min walk*) with the bus timetable flagged only when out of date, each key's state, and shortcuts: change stops only, change station only, the dashboard and the wall tablet's address.
  - The walks to the stops are a box for each stop, named by the stop, instead of `LABEL = minutes` lines.
  - Each field you change is marked **Changed**, the save bar counts the changes not saved yet, and leaving the page with any asks first. A checking time or quiet hours row has **Weekdays**, **Weekends** and **Every day** buttons. This is the settings pages' one script, `/settings/settings.js`; the pages work without it, and the sign-in pages still run none.
  - While all is working, the Overview folds its feeds behind **Show each feed**. The Board page's Status panel is gone: how the board is doing is on the Overview, which the Board page links to.
- The board and the dashboard use the same status words: "On time", "Exp 17:54", "Delayed", "Cancelled", "No report". The plain e-paper look said "+7" for a late train; it now says "Exp 07:49". Its bus section says "Leave by" once, over the column of times.
- The plain e-paper look draws in Inter, which ships with the package. The Pi no longer installs DejaVu fonts.
- The dashboard API (`/api/snapshot`) changed shape. Anything reading it needs updating; `docs/dashboard-api.md` has the details.
  - `buses.stops` maps each stop label to `{name, letter, towards, walk_min}` instead of a display string.
  - Every train and bus row gains `walk_min` and `status_spoken` (the status in full words). `status_text` uses the words above.
  - Disruption items gain `relevance` (`your_stop`, `nearby` or `line`) and come in order of it.
  - `trains.messages` holds `{text, category, severity, kind}` objects instead of strings, with `kind` `station` or `service`.
  - `trains.positions` and `buses.positions` say how asking where the trains and buses are is going, for the settings' Overview.
  - A top-level `quiet` says whether a quiet window is on.

- The package is organised by app area (`core`, `settings`, `sources`, `timetable`, `board`, `dashboard`, `web`, `setup`, `fetch`, `emulator`, `cli`), with the tests mirroring it.
- The repo keeps deploy files in `deploy/`, docs in `docs/` and example settings in `examples/`.
- The example settings lead with bus times from the BODS timetable.
- `[bus]` takes bus times from the BODS timetable unless it says `source = "transportapi"`; a config that relied on TransportAPI being the default needs that line. TransportAPI otherwise only stands in while there is no usable timetable, and then only in its own `fallback_windows` (the commute by default), never the all-day bus windows.

[Unreleased]: https://github.com/acornelissen/twobells/commits/main

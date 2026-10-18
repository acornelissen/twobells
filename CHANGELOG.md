# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The settings' Overview says each key's state from the same place as the Keys page, in its words, so the two pages can't disagree. Each line is headed by what the key unlocks, as its form on the Keys page is (TransportAPI's app ID and key are one line), and a key saved but not tested since says **Saved, not tested** rather than **Set**.
- The setup wizard lists bus lines in the order the board and dashboard do, by their number: X57 comes next to 57, not after every numbered line.

### Fixed

- Setting a new settings password with a reset code when the password can't be saved (a full or read-only disk, say) now says the new password couldn't be saved, that the old one still works and to ask for a new code, as that one is used up; the log says why. It sent nothing back, and logged nothing. Every session still stands.
- The web server logs a fault answering a request, such as a file it can't read, by the fault's type. It took every such fault for a browser going away and logged nothing, on the dashboard's event stream too.
- On the dashboard's area map, a bus whose position time comes without a UTC offset shows how old the position is, read as UTC as the rest of the dashboard reads it, rather than no age at all.
- On the dashboard, a bus row's **Disruption** link keeps the keyboard focus when the boards are drawn again with each update. The focus went back to the top of the page, so a keyboard user lost their place (WCAG 2.4.3).
- A reminder for a cancelled tram, river bus, cable car, ferry or replacement bus is headed by what was cancelled (**Tram cancelled**, **Ferry cancelled**), not **Train cancelled** for all of them.
- A number on the settings pages takes the digits 0 to 9 alone, up to 9 of them. "²" failed the page with an error rather than saying what to enter, and other scripts' digits, such as Arabic-Indic ones, were saved as numbers.
- A stop label, stop code, station code or key with a newline at its end is refused, as any other character out of place is. The checks let one last newline through, so a tram stop labelled "T" and a newline in the setup wizard saved a `settings.toml` that couldn't be read again.
- The same goes for the codes, times and dates read from elsewhere: a stop, area or operator code in the bus search, a TfL line or stop code, a Metrolink stop code saved for the tram timetable, a tram time or date, and a request's name in the fetch service's folder are each refused with a newline at its end.

### Security

- The setup wizard's last step shows a link the server sends only when it is a plain path on this site or a plain web address, as the server itself checks. It took anything starting with `/` or `http`, `/\evil.example.com` among them, which some browsers open as another site; the server never sends one, so this only backs its check up.
- The dashboard's responses carry `X-Frame-Options: DENY`, as the settings site's do, so no other page can frame it in a browser that ignores the policy's `frame-ancestors`.
- A client sending its request slowly, a line every few seconds, can't hold the web server's connections for long: a request's line, headers and body must all arrive within 15 seconds, and one address may have at most 16 connections open of the 48. Four such clients kept the health check and every other page from answering for as long as they kept at it. The dashboard's event stream is not cut short. In Docker Desktop and rootless Podman, where every device shows as one address, the 16 are shared ([Docker](docs/docker.md#keep-it-on-the-home-network)).
- The settings refuse a common password: one of the 5,000 most common of 10 characters or more, such as `password123` or `Manchester1`, whatever its case, saying "That's one of the most common passwords. Choose another." This holds when claiming the settings, setting a new password with a reset code (checked before the code is used up) and `--set-password`. A password already set keeps working. The list, `src/twobells/data/common_passwords.txt`, comes from SecLists under the MIT License.
- `upgrade.sh` needs cosign: without it, it stops before downloading anything and says how to install it, where it used to go on checking the tarball's SHA-256 only. `--no-signature-check` still does that, for a Pi Zero, Zero W or Pi 1, which can't run cosign, and says so. `upgrade.sh` runs from the release you have, so this starts with the release after this one.
- The Pi install guide and the release notes check `SHA256SUMS`'s signature with cosign first, in the same commands as the SHA-256, and the guide says how to install cosign on a Pi.
- A release signs the container image and tags it with its version and `latest` only once the release is approved, the same step that lets it reach PyPI. Before, the image was tagged and signed while the release still waited for approval.
- The container image sets `PYTHONSAFEPATH`, so Python no longer looks for code in its working folder, the `/data` volume. A `urllib.py` or `twobells/` written there would have run in place of the real one at every start, health check and `docker exec`.
- An error message is read no further than 640 characters before keys are taken out of it and it is cut to one short line. A server chooses what its errors say, and the patterns that find keys took time with the square of the message: a 64 KB reason phrase in an HTTP answer held the board or the fetch service for about 30 seconds on a Mac, and far longer on a Pi Zero. It now takes under a millisecond.
- An XML document is refused once it has more elements than any real one would: 500,000 for BODS's disruptions, 330,000 for National Rail's incidents, 80,000 for bus positions, and 100,000 in any one journey, service or other part of a bus timetable. An empty element is 4 bytes to send and about 100 to hold, so a disruptions feed under its 12 MB cap could take 300 MB, more than a Pi's services may use. Real documents take 35 to 60 bytes an element, so stay well under; the largest part of a timetable seen has 395.
- The tram timetable is refused if a part of it is packed other than stored or deflated, as the bus timetables already were. Python before 3.12.15 unpacks bzip2 and LZMA a whole read at a time, so a 384-byte file could take 290 MB at once, whatever the part's size cap. TfGM's file is deflated.
- A redirect's body is no longer read. Python's URL library read it whole before following the redirect, whatever cap the request had, so a 200 MB body on a redirect took 265 MB of memory; now the connection is closed and the redirect followed at once.
- A whole number too big for a float, such as one of 401 digits, is refused as a position or time in the setup wizard's requests and the fetch service's files, rather than crashing the check. On a Pi, one such request stopped the fetch service answering the wizard, and one such snapshot froze the dashboard, until a restart. The fetch service and the dashboard's reader of its snapshot now also log any other failure they didn't foresee by its kind alone and carry on.
- The settings site refuses a form token or its cookie with a newline at its end, and the board won't show a reset code with one. The checks let one last newline through, so a form value with a newline after its expiry time was taken; a cookie holding one is refused by Python's own cookie reader today, but the check no longer leans on that, as the value goes back into a header.

## [0.4.3] - 2026-10-18

Subtitles under a train on a screen, and the tram timetable built as soon as it is set up on a Pi.

### Changed

- In the station board look on an HDMI screen, the lines under a train's row read as its subtitles: the "Calling at" line, and on the 7.9" screen the leave line, are drawn in a new, smaller dot font (capitals 5 dots tall rather than 7, with lowercase and descenders) on a line 9 dots tall rather than 11. E-paper is unchanged.

### Fixed

- On a Pi, tram and bus stops saved in the setup wizard or on the settings pages have their timetable built straight away, not at the timetable timer's next hourly check. Until then the board said "there is no tram timetable yet" for up to an hour after setting up Metrolink stops. A new `twobells-timetable.path` starts the timetable check whenever the settings are saved; it builds only what is due. `install.sh` and `update.sh` turn it on.

## [0.4.2] - 2026-10-18

The HDMI screen shows the board on a Pi 4 with Raspberry Pi OS Trixie, and its scrolling text moves evenly.

### Fixed

- On a Pi 4 with Raspberry Pi OS Trixie, the HDMI screen stayed black while its service ran without errors. `twobells-screen.service` now has SDL draw the board through GLES (`SDL_FRAMEBUFFER_ACCELERATION=opengles2`); left to choose, SDL 2.32 drew nothing the screen showed. Upgrading with `upgrade.sh` refreshes the unit and restarts the screen.
- Scrolling text on an HDMI screen moves evenly. It moved 16 steps a second, which a 60 Hz screen shows for 3 and 4 refreshes in turn, so it juddered; it now moves 15 steps a second, each shown for 4. Still about 3 letters a second, and the split-flap look's tiles still turn over in about half a second. A frame woken on its boundary no longer reads as a whisker before it and ends the second's frames early.

## [0.4.1] - 2026-10-18

The HDMI screen opens on Raspberry Pi OS Trixie.

### Fixed

- On Raspberry Pi OS Trixie, the HDMI screen's service crashed every few seconds and the screen stayed on the boot console. `twobells-screen.service` now allows netlink sockets, which SDL 2.32 needs to open the screen, and `install.sh` and `update.sh` install the EGL and GLES libraries pygame draws through (`libegl1`, `libegl-mesa0`, `libgles2`), which Raspberry Pi OS Lite comes without. Upgrading a screen Pi with `upgrade.sh` installs them and refreshes the unit.

## [0.4.0] - 2026-10-17

Trams and leave times your way: tram stops found by name wherever home is, a choice of which modes get leave times, and the time and weather in Next to catch's place when it has nothing to show.

### Added

- Choose which modes get leave times. Under **What to show**, the Board page has a **When to leave** tickbox for each mode set up, for the board's "Leave by" and "Leave in", and the Dashboard page's **Next to catch** becomes a tickbox for each mode, for those it picks from; the dashboard's rows keep their leave times. They are saved as `[board] leave_modes` and `[dashboard] leave_modes`, which `config.toml` can set too: without a list, every mode, as before; `[]`, none. The dashboard API sends its list as `leave_modes`. See [What each view shows](docs/configuration.md#what-each-view-shows).
- Tram stops wherever home is. The setup wizard's tram step always shows, with **Find a stop by name** over the Metrolink and London stops that come with the app, so nothing is sent anywhere: 2 or more letters of a name list up to 10 stops with it in their names, however far away. The Trains and buses page's **Trams** section shows before trams are set up, with **Tram stop codes**: typing codes there sets them up.

### Changed

- With Next to catch left out, or nothing to point at (no departure you can still reach, or none with a walk time), the dashboard shows the time in its place, as large as the countdown, with the date, the weather now and the rain sentence; the header's clock and the weather section's now line and rain sentence make way for it. Before, the place said "Nothing to leave for", or was left empty.
- The README opens on busy places: made-up mornings at Manchester Piccadilly, Bank and Greenwich, with real stations, stops and lines and invented times, on the dashboard, the 11.3" bar screen and the 10.85" e-paper, each with three modes. Glossop's pictures still tell the live bus story further down. `tools/serve_doc_dashboard.py --scene` and `tools/build_doc_images.py` draw them (see [Pictures](CONTRIBUTING.md#pictures)).
- A walk the setup wizard finds to be over an hour, like one to a tram stop found by name in another town, is left empty and marked **Over an hour**, so no time to leave is shown for it. It was set to 60 minutes.

### Fixed

- The setup wizard's **Find a stop by name** box is as big as its other boxes. The browser drew it at its own size, half as tall as the others and under the 24 px a target needs.
- A wall tablet in landscape with trains, buses and trams no longer runs the three sections into each other. Side by side, each was too narrow for its columns; now a section that narrow folds each row onto two lines, as on a phone, and a stop's line no longer ends in a stray "·".
- A bus timetable download that isn't a zip is read a chunk at a time. It was read in one go with room for the most an install reads, 200 MB on a desktop, however small the file; a file of a few KB now takes under 1 MB.
- The setup wizard's stop search reads NaPTAN's stop list a line at a time, never more than 4,096 characters of a line at once, as the timetable build does, and decodes it as it reads. Before, one very long line was held whole, or refused the whole list; now that line is left out and the rest are read.
- An install set up with tram stops alone, its trains and buses switched off, can save the Trains and buses settings page again. It was refused with "Trains and buses can't both be off", whatever was changed; now the page is refused only when the trams are off too, and says so: "Trains, buses and trams can't all be off".
- With the network guard **Off**, the Overview and the troubleshooting guide said to restart the web server alone, which leaves the guard unloaded after its rules were flushed. They now say what the security page does: `sudo systemctl restart twobells-guard twobells-web twobells-fetch`.
- The station board look's `NOT UPDATING` badge names London's section by its heading, like `UNDERGROUND` or `DLR`; it said `TRAMS` whatever the stops.
- `twobells --help` says `timetable` builds the tram timetable as well as the bus timetable, and `twobells timetable --help` that `--force` builds each, even within 6 hours of a failed build; both spoke of buses alone.
- The tram timetable is no longer built for a board with no room to show the trams. Without `[board] modes`, a display with room for two shows trains and buses, but the timetable was built as if it showed the trams too; it is now built only while the board shows them, or the dashboard is on and lists them.
- In the station board look with trains, buses and trams, the trams' leave line no longer runs into the rule over the bottom line and the clock or "Updated" (on the 7.9" screen and the 10.85" e-paper). A section whose leave line wouldn't fit under its train or tram shows one departure fewer, or, down to one, the section with the most does, so every section keeps a row and its leave time.

## [0.3.0] - 2026-10-17

Trams and the Underground: Manchester's Metrolink from TfGM's timetable and London's metro live from TfL, with a choice of which modes each view shows.

### Added

- Choose which modes of transport each view shows: **Modes of transport** on the Board and Dashboard settings pages, and in the setup wizard's What to show step when you pick a station and stops, saved as `[board] modes` and `[dashboard] modes`. The board can show buses alone while the dashboard lists trains too, and a view checks only for the modes it shows; a bus or tram timetable is built only while a view that is on shows its mode. The board's display limits how many: each mode takes a section with its heading and at least a row, so the 10.85" e-paper has room for 3 and every other display for 2; the settings say so, and a `config.toml` naming more says how many fit and stops the board loading; the dashboard loads all the same. This makes room for trams and metro, a third mode whose sources come next. An install with trains and buses keeps both, in the same order and with the same rows.
- Manchester's Metrolink trams, the third mode of transport, in a **Trams** section on the board and the dashboard, where home is near a tram stop. TfGM's live tram times are closed to new users and the Bus Open Data Service has no trams, so they come from the timetable TfGM publishes each night (GTFS, no key, Open Database License): booked times, marked `sch` on the board and "sched." on the dashboard, with walk and leave times, Next to catch, reminders and a sheet, and no punctuality record. The setup wizard asks for up to 4 tram stops after the bus stops, from a list of Metrolink stops that comes with the app; `[metro]` sets them in `config.toml`. The timetable is built weekly with the bus timetable (`twobells timetable`, the Pi's hourly timer, or the dashboard's upkeep), keeping only your stops' trams as it reads the file a line at a time, never more than 4,096 characters of a line at once, and refusing a file of more than 100 parts before they are read. The settings have Trams sections while trams are set up, and the Overview their stops and timetable. See [Tram times](docs/trams.md).
- London's Underground, DLR, trams, cable car and river buses, live from TfL's Unified API, in the same section as Manchester's trams, named for the stops chosen (**Underground**, **DLR**, **Trams**, **Cable car**, **River buses**, or **Metro** for a mix). The setup wizard offers the 8 London stops nearest home from a list that comes with the app (NaPTAN's stations and piers, with the TfL lines at each); `[metro]` takes TfL's codes, like `940GZZLUBNK`. Each row is live, at the time TfL expects it: the board says `Live` on e-paper and counts down on a screen, and the dashboard counts down with the live mark, its line and its colour, its platform and where it is now. TfL gives no booked times, so there is no punctuality record. The status of the lines at your stops is the board's notice and joins the dashboard's disruption line; a lift or escalator out of order is the dashboard's station information. It needs no key: TfL allows 50 calls a minute without one, and the optional `TFL_APP_KEY` (Keys page and wizard, with a Test button) raises that. Overground and the Elizabeth line stay with the trains, London's buses with the bus data. See [London](docs/london.md).

### Changed

- An HDMI screen has room for three modes of transport where its look and text size keep a whole row for each: the station board, TFT and split-flap looks, and the plain look up to text size 12 on the 7.9" screen and 13 on the bar screen. The Board settings page offers only the looks and text sizes with room for the modes the board shows, and says why; a `config.toml` without room says what would give it.
- On a Pi, `install.sh` and `update.sh` turn `twobells-timetable.timer` on whatever the settings, not only when buses came from BODS at the time, so bus or tram stops set up later are built within the hour; it builds nothing until a timetable is due. `twobells timetable` with buses from TransportAPI now has nothing to do and says so, rather than failing.
- A board with only trains, or only buses, set up no longer draws an empty "No buses" or "No trains" section: the mode it shows takes the room. E-paper redraws once after updating.

### Fixed

- A bus timetable download listing too many files is refused before the list is read. Python's zip reader holds every file a zip lists before reading any, so a download of a few MB listing hundreds of thousands of empty files could fill a Pi's memory; one listing 20,000 took 11 MB, and now takes under 0.1 MB. A zip may now list up to 5,000 files of any kind, in up to 1 MB of names (it was 2,000 .xml files, counted only after the list was read); the most seen in a real BODS data set is 814. The setup search and `twobells route` read timetables the same way. NaPTAN's stop list, read when a bus timetable is built, is now read a line at a time, never more than 4,096 characters of a line at once. Before, one very long line was held whole and then lost every stop's NaPTAN name and position; now that line is left out and the rest are read. The tram timetable's reader makes the same checks in the same shared code, without relying on Python's private zip code.

## [0.2.3] - 2026-10-17

Quieter bus rows on the dashboard.

### Changed

- A bus known only from the timetable is quieter in the dashboard's rows. It used to say "No live position" under every one, with an empty strip, beside the "sched." that already said it. Now it shows only its time and "sched." until 10 minutes before you'd need to leave for it (before its time, with no walk set), then "Not tracked yet" on one quiet line, and once it is due the "May not be running: no tracker yet" warning, as before. Live buses, and buses whose bus is coming in, keep their strips and words; the bus's sheet still draws its stops, and screen readers still hear "timetable only" with its time. The snapshot's fields are unchanged.

## [0.2.2] - 2026-10-17

More buses shown live, and the Saturday 393 on the board. The bus timetable is built again once after updating.

### Changed

- The dashboard lists up to 15 buses by default, up from 10: about half an hour across five stops. An install with **Most buses listed** (`[dashboard] bus_rows`) saved keeps its number, and a wall tablet still shows 4.

### Fixed

- Saturday buses on High Peak's 393 now show. BODS has its timetable in two parts with the same revision number, Saturdays from September 2024 and weekdays from January 2026, and the app took the weekdays for a replacement, so it ended the Saturdays the day before they started: no Saturday 393 since. As bustimes.org does, a version of a timetable is now replaced only by one with a higher revision number; parts of the same revision all run. The bus timetable is built again once after updating (timetable format 6).
- A bus coming in to work a journey that starts at your stop now counts at that journey's later stops of yours too, not only the first: the 61 at stop I at 16:30 was "Bus on its way" and the same journey at stop A at 16:31 "No live position". Each later stop is expected the timetable's time from the first stop after the bus is expected to leave there, never before its time, says "Bus on its way, due in 9 min" (counting down to the bus getting to that stop) and then "Bus waiting to start" (`Waiting to start` on a screen's row), and goes when the first stop's does. The dashboard's line under it names where the bus arrives ("Bus arriving at Henry Street from Ashton, ..."). The history records each stop's row once, with no delay for a bus only waiting in.

## [0.2.1] - 2026-10-17

A fix for engineering works, and the user guide brought up to date.

### Fixed

- Engineering works and other rail incidents now show: the Rail Data Marketplace sends its Knowledgebase Incidents feed gzipped (`Content-Encoding: gzip`) even when asked not to, and the app read the compressed bytes as they came, so the feed never parsed and the Keys page's test said it "answered, but not with incidents". Every answer the app fetches is now read decoded from gzip or deflate as it arrives, with each size limit on the decoded size, so a small answer that would inflate to far more is refused without being inflated; an answer in any other encoding is refused with a plain message, and the key test says it couldn't read the answer. What the app asks for is unchanged.
- The user guide matches 0.2.0 throughout: where the plain, TFT and split-flap looks put the clock, notices and the bus page on a screen; that the bus timetable is built again once after updating; what the show lists stop fetching; Around here, the no-tracker warning and what a wall tablet leaves out; the Overview's timetable, keys and TransportAPI lines; and 0.2.0's failure modes in Troubleshooting. The phone-width and wall tablet night pictures are retaken.

## [0.2.0] - 2026-10-17

More of what a station board says, how the trains have run, more buses tracked live, new looks, station announcements, and a choice of what each view shows.

### Added

- **What to show**, on the Board and Dashboard tabs: a tickbox for each thing a view can show beside the departures' times, which always show, with presets that tick them for you: **Just the times**, **The usual** and **Everything**. The board can leave out platforms, where each train and bus is, the calling-at line, disruption notices, the rain warning, the last bus, the bus page and the clock page; the dashboard Next to catch, where each is, disruption, the weather, the last buses, Around here, Remind me, the calling-at line, coaches, punctuality, the Performance link, fleet numbers and announcements, on a wall tablet too. What is left out isn't drawn and leaves no gap. They are saved as `[board] show` and `[dashboard] show`, which `config.toml` can set too, and the dashboard API sends its list as `show`. Without a list a view shows everything, as before, so nothing changes until you save one. What no view shows isn't fetched: the forecast, the disruption checks, Service Details for where each train is and for Delay Repay, and the bus positions for the board's strips and bus page each stop when nothing they are for is ticked; the departures, and the punctuality history recorded from them, carry on.
- The last bus of each line and destination today, from the bus timetable: under the buses on the dashboard from 19:00 or once few are left ("Last 341 to Hyde 23:05"), in every bus's sheet, and on the HDMI screen's bottom line in the last hour ("Last bus 23:05"). It follows the timetable's days, bank holidays and buses after midnight, whether the timetable runs them on from the evening or gives them the next day (before 03:00, they are the night's).
- The setup wizard asks **What to show**, after the views: a preset for each view on, The usual at first on a new install, each saying what it adds. Running it again starts at the preset each view's list matches, or at **Keep what it shows now**.
- A Performance page on the dashboard (`/performance`, linked from its header): how the trains and buses ran over the last 7 and 28 days, all together and route by route (rail replacement buses and ferries in routes of their own, not counted with the trains), by the rail industry's measures (Right Time, T-3, T-15, cancellations; buses on time by the bus late threshold), as counts with percentages in plain tables. Each departure's sheet shows its route's figures too. The API is `GET /api/performance`, and `GET /api/history/<id>` has the route's figures as `route`.
- A train's sheet on the dashboard lists its coaches from the front, where the operator sends them to Darwin: number, First Class or Standard, toilets, how busy each is and where it is quietest ("Quietest at the back"), and "Front 2 coaches detach here" when Darwin says so. Without them the sheet keeps the coach count; the board is unchanged.
- A TFT look for the board on an HDMI screen (`theme = "tft"`): white and yellow on black, like the newer station screens, with times in bold yellow and each status in a coloured pill backed by its words. E-paper isn't offered it.
- The dashboard's TFT look, in the same colours.
- A split-flap look for the board on an HDMI screen (`theme = "flap"`): white letters on dark tiles split across the middle, like a Solari board. When a row's time, destination, platform or status changes, its tiles flip through to the new letters in half a second, drawing only what changed, even as the first train's calling-at line comes or goes and moves the rows under it. E-paper isn't offered it.
- The dashboard's Split-flap look: its boards' times, destinations and statuses on tiles that flip when they change, at once with reduced motion; screen readers hear the words as in any other look.
- Platform changes: a train whose platform isn't the one it was given earlier says `Plat 2 (changed)` on the bigger boards and screens (in the accent colour in the plain, TFT and split-flap looks), and "2 (changed)" on the dashboard, where it also counts when it isn't the platform the train usually leaves from, and screen readers hear it once. A rail replacement bus, or a train cancelled, is never a platform change.
- The dashboard's sheet says which platform a train usually leaves from ("Usually: Platform 1"), once its punctuality history has a few days to go on.
- National Rail's route words after a destination ("via Guide Bridge"): under the destination on the dashboard, and after it on the bigger boards and screens.
- Delay Repay on the Performance page: with a filter station (**Only trains calling at**) and a Service Details key (`RDM_DETAILS_API_KEY`), the trains that reached it 15 or more minutes late in the last 28 days, in Delay Repay's bands (15, 30, 60 and 120 minutes), with the date, train, booked and actual arrival, minutes late and operator, and a link to National Rail's compensation page. The dashboard follows each train that has left with Service Details until it gets there, at most one ask a minute per train, only while it is due and never in the quiet hours, waits for the arrival time Darwin reports rather than taking the train's estimate while Darwin still knows it, and stops asking about a train Darwin no longer knows. Without either, the section says why it is off. The punctuality history moves to version 3, keeping everything already in it, and keeps a copy of the version 1 file, `history.sqlite3.v1`, to put back if you go back to 0.1.0, which can't read version 3: the Pi's `upgrade.sh` says how when it goes to an older release, and the install guides do too; `GET /api/performance` has the list as `delay_repay`.
- A clock page for the board on an HDMI screen: a platform clock in the style of the classic British one (white face, black batons and hands, red seconds hand), with the time and date beside it in words, for 10 seconds after the departures and the bus page. Off unless ticked: **Clock page** under **What to show** on the Board tab, or `"clock_page"` in `[board] show`.
- A wall tablet (`/?kiosk`) shows the same platform clock large in place of the departures during the quiet hours, dimmed with the page and still moving against burn-in, with the time in figures. Its seconds hand sweeps, or ticks where the tablet asks for reduced motion.
- A live bus's sheet on the dashboard says which bus it is, from its tracker: the fleet number and registration, "3017 (AB25 CDE)", or either alone, with a link to look a registration up on bustimes.org. Anything else the tracker sends is left out.
- The station board's "Calling at" line under the first train: "Calling at: Dinting, Broadbottom and Manchester Piccadilly. This train is formed of 3 coaches.", with stops no longer called at named after it ("Does not call at Hattersley."), or alone when it calls at none, and "This is a replacement bus." for a bus. A train that divides names every part's stops, in time order. The HDMI screens scroll it in every look; to have the calling points, a screen asks for National Rail's board with details, the same request with more in its answer. E-paper doesn't show it. The dashboard's first train has it too (`calling_at` in the API), scrolling along one line in the Dot matrix, TFT and Split-flap looks, and two lines cut short with "…" with reduced motion.
- Station announcements on the dashboard, in a station's classic words after a chime of three tones: "The next train to depart from platform 1 will be the 08:12 Northern service to Manchester Piccadilly. Calling at …", cancellations and delays with National Rail's reason ("This is due to a shortage of train crew."), and platform alterations. A train's sheet has **Announce**, showing the words as they are said; a wall tablet at `/?kiosk&announce` announces the next train a few minutes before it goes, a train delayed with no expected time, cancellations and platform alterations by itself, once each and never in the quiet hours, after one tap to let it make sound. The voice is the device's own, British English where it has one. Each train row in the API has them as `announce`.
- Station announcements on the HDMI screen, through the Pi's speaker with espeak-ng, in the dashboard's words for the same train, stops, coaches and National Rail's reasons included: the next train to depart 4 minutes before it goes (as the dashboard decides it, never one already gone or delayed with no expected time), the delay of a train delayed with no expected time, cancellations and platform alterations, once each, by the wall tablet's rules, only in the train checking times and never in the quiet hours. Off unless chosen: **Announcements** under **What to show** on the Board tab (`"announce_speaker"` in `[board] show`), which no preset ticks. `install.sh` and `update.sh` install espeak-ng for a screen, and the screen's service joins the `audio` group for the sound devices. E-paper boards don't speak. Not yet tried on a real Pi.

- A departure that starts at your stop goes live before its bus turns round, where the operator's timetable gives blocks (TransXChange's `BlockNumber`): the bus on the journey before it in its block, ending at your stop, is followed in. The row is marked inbound ("Bus on its way, due in 6 min", `On its way, 6 min` on a screen's row) and a late inbound bus makes it late, leaving 2 minutes after it gets in, never before its time. A bus still in 30 minutes after the departure's time, still on the journey before, has laid over or gone out of service: it is let go, and the departure goes as one with no bus seen would. The punctuality history records no delay for a departure whose bus is only waiting in. The block is checked against the timetable before it is trusted, and a bus whose block's bus isn't seen stays timetable only. Without blocks the bus is guessed by line, as bustimes.org does: each bus coming in on a journey of the line and operator ending at your stop goes to the earliest departure it can make, up to 30 minutes after it is due in, and a departure with exactly one bus coming to it is live, marked inbound, in the same words. A bus whose block goes on to another journey is left to that one, and a departure first in its block, out of the depot, is never guessed at, so one bus is never given to two departures. Bee Network's and High Peak's timetables give no blocks, so Glossop's buses go live this way. The dashboard API's `inbound` says which it is with `followed`.
- **Scrolling text**, on the General tab (`scrolling` in `config.toml`): one switch for every view. Off, the dashboard's calling-at line is two lines cut short with `…` in every look and an HDMI screen board cuts long text short with `…`, as e-paper does, rather than scrolling it; the split-flap look's tiles still turn. The dashboard API sends it as `scrolling`.

### Changed

- The bus timetable keeps each journey's block number (timetable format 4), so it is built again once after updating. It takes 8 bytes more per journey held, and its block numbers, shared: the journey limits are unchanged.
- A bus coming in to turn round at your stop is tracked all the way in: for a line with departures starting at your stop, the timetable keeps where every stop is on its journeys ending there (or within 150 m), not just the 20 stops before, and BODS is asked for buses in a box around them all. In Glossop the box grows from about 6 by 13 km to 16 by 28 km. Those stops are marked apart (timetable format 5, so it is built again once after updating): they only follow the bus in, so roadworks or a closure out there aren't counted as near your stops. A box bigger than 500 km² is drawn round the rest only, the 20 stops before yours, as before: over a city, BODS's answer for it would be more than the board takes, and every check would lose its live positions.
- Live buses are placed along the road the timetable saved between two stops (the bus maps' roads), up to 300 m from it, so a bus on a road that bends away from the straight line between its stops is tracked. Between stops with no saved road the straight line and its 150 m stay. Where a bus is near more than one part of its journey, the nearest wins, and only parts about as near are settled by which fits the timetable best, so a bus on a parallel road further on isn't placed there.
- The dashboard's **Board look** button is now a **Look** list, on the Performance page too: Standard, Dot matrix (the old Board look), TFT and Split-flap. A browser that had the Board look keeps it.
- The dashboard's header keeps the place, a larger clock and the status; **Look**, **Performance** and **Settings** move to a footer, on the Performance page too. Next to catch's **Remind me** is a small bell saying **Remind** at the top right, beside its label, and the departure sheet's is smaller.
- The dashboard's status is quiet words while all is well (**Live**, or **Updating every 30 s** without a ticking age), and fills in amber or red only when something is wrong.
- Next to catch reads as one statement: the countdown, which departure in the rows' size, then one quiet line for how sure it is, what to catch instead and the rain, which a wide screen leaves to the weather beside it. On a wall tablet that line sits at the foot of the hero.
- On a wide screen the disruption line sits under Next to catch, beside the weather, rather than across the page under both, so the trains and buses start sooner. Screen readers and the Tab key meet it where they did, after Next to catch.
- The trains and buses start sooner: the walk to the station sits beside the Trains heading, and station information and the bus timetable changes coming up are quiet notes under their boards, no longer boxes that looked like buttons above them.
- The dashboard's rows are calmer: destinations a size larger, everything under them one quiet size, the first train's calling points two lines in every look (one scrolling line in the station looks while they scroll) and a bus coming in one line, each cut short with the rest in the sheet, and "Often late" and a bus's **Disruption** link as amber words rather than pills.
- The settings site and setup wizard are tidier: each section's intro stands apart from its first field, read-only lists line up in one column, the current tab leads the others, the display's row on the Board tab says where it is set once, a bus stop's distance sits under its name, and a done setup step shows its tick in place of its number, so the steps no longer wrap as you go.
- The user guide is a page per topic, indexed by `docs/user-guide.md`; the Docker pages are one, and the code layout is in `docs/architecture.md`.

### Fixed

- Rail replacement buses show. Darwin lists them apart from the trains (`busServices`), and they were left out, so on engineering works days the board and dashboard showed no trains. They are now in time order with the trains: `Bus` in the platform column (or `Bus:` before the destination where the board has no platform column), "Replacement bus" on the dashboard's row and sheet, and a punctuality record of their own.
- Ferries show, at stations with a connecting ferry. Darwin lists them apart from the trains (`ferryServices`), and they were left out. They are now in time order with the trains, as for replacement buses: `Ferry` in the platform column (or `Ferry:` before the destination), "ferry" in the dashboard's row and sheet, "This is a ferry." in the calling-at line, a punctuality record of their own, and never in Delay Repay.
- A Pi reads bus timetable files up to 61 MB, up from 50 MB: First Bus's largest is now 48 MB, and a bigger one was skipped.
- A Pi holds 8,500 bus journeys while it builds its timetable, up from 7,000, so two of a city's busiest stops no longer fail to build.
- On a Pi, the network guard also keeps connections off the fetch service, where systemd can't apply its `SocketBindDeny=` (a Pi Zero W).
- A bus whose tracker last reported up to 10 minutes ago still counts as live, up from 5, so a tracker that goes quiet for a few minutes no longer drops its bus.
- A bus whose line the live feed writes in another case from the timetable ("x1" for "X1") is no longer dropped before it can be matched.
- `update.sh` stops before changing anything where the app isn't installed.
- Delays across the clocks changing are the minutes that passed, not the difference on the clock: a train booked at 01:50 BST that leaves at 01:10 GMT is 20 minutes late, not 40 early and left out, and one booked at 00:50 GMT that leaves at 02:10 BST is 20 minutes late, not 80. That goes for the dashboard's rows and announcements, the arrival under each train and bus ("arr 01:10 · 20 min", which was an hour out or missing), the punctuality history and the Performance page, Delay Repay too, and the HDMI screen's spoken announcements, their delay and when they are said. In the hour the clocks go back, which comes twice, a departure at the same time in each pass is kept as two, a bus in the pass it was seen in; the second was lost, and a bus seen in the second pass was put in the first and decided before it went. A route's figures by day ("On time the last 4 Sundays", the days in its sheet) count that day once, by the later pass. The history keeps its times in UTC from version 3, which reads an older one's times in the board's time zone; a version 2 file is copied first, as `history.sqlite3.v2`.
- The docs say which cosign checks a release: 2.5 or later for `SHA256SUMS`, 3.0 or later for the image.
- The settings Overview no longer says a board's trains or buses have checking times that "don't read" when none are saved: it goes by the times the board checks at then, all day for buses from the timetable.
- On a Pi, the setup wizard's search no longer fails now and then with "The fetch service sent an answer that doesn't fit": the page read the search's progress just as the fetch service wrote it again, and took the old copy for a stranger's file. The dashboard's snapshot is read the same way.

## [0.1.0] - 2026-10-12

The first release.

### Added

- One app with two views of the same train and bus departures: the board, on an e-paper panel or an HDMI screen on a Raspberry Pi, and the dashboard, a web page for a phone or computer at home.
- Trains from National Rail Darwin. Buses from the BODS timetable and live positions, with TransportAPI standing in while there is no timetable.
- One settings site per install, at `/settings` on port 8090, with one password and a setup wizard that works from your postcode.
- Three installs: a Raspberry Pi, Docker, and a desktop (macOS or Linux).
- Signed releases: the package on PyPI, a signed container image on GHCR, and a signed source tarball for the Pi.
- A network guard for the Pi's web server: nftables rules for it alone (`deploy/pi/twobells-web.nft`), loaded at boot by `twobells-guard.service` and matching the server by the cgroup systemd puts in their set (`NFTSet=`, systemd 254 or later, so Raspberry Pi OS Trixie on a Pi Zero or Pi 1), that keep it from connecting out to the internet and keep port 8090 to the home network. They back up the unit's systemd IP firewall, which a Pi Zero's kernel can't apply ("Preparation of BPF allow maps failed"). `install.sh` and `update.sh` install `nftables`. The server checks at start whether it can connect out, warns in its log if it can, and the settings' Overview shows **Network guard**: On, Off (a problem) or Can't tell.
- A wall-tablet mode for the dashboard at `/?kiosk`: one screen with no controls, the station-board look by default, a shift of a pixel or two every few minutes against burn-in, dimmed in quiet hours, a large banner when the times stop updating, and a reload at 03:30.

[Unreleased]: https://github.com/acornelissen/twobells/compare/v0.4.3...main
[0.4.3]: https://github.com/acornelissen/twobells/releases/tag/v0.4.3
[0.4.2]: https://github.com/acornelissen/twobells/releases/tag/v0.4.2
[0.4.1]: https://github.com/acornelissen/twobells/releases/tag/v0.4.1
[0.4.0]: https://github.com/acornelissen/twobells/releases/tag/v0.4.0
[0.3.0]: https://github.com/acornelissen/twobells/releases/tag/v0.3.0
[0.2.3]: https://github.com/acornelissen/twobells/releases/tag/v0.2.3
[0.2.2]: https://github.com/acornelissen/twobells/releases/tag/v0.2.2
[0.2.1]: https://github.com/acornelissen/twobells/releases/tag/v0.2.1
[0.2.0]: https://github.com/acornelissen/twobells/releases/tag/v0.2.0
[0.1.0]: https://github.com/acornelissen/twobells/releases/tag/v0.1.0

# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The last bus of each line and destination today, from the bus timetable: under the buses on the dashboard from 19:00 or once few are left ("Last 341 to Hyde 23:05"), in every bus's sheet, and on the HDMI screen's bottom line in the last hour ("Last bus 23:05"). It follows the timetable's days, bank holidays and buses after midnight.
- A Performance page on the dashboard (`/performance`, linked from its header): how the trains and buses ran over the last 7 and 28 days, all together and route by route, by the rail industry's measures (Right Time, T-3, T-15, cancellations; buses on time by the bus late threshold), as counts with percentages in plain tables. Each departure's sheet shows its route's figures too. The API is `GET /api/performance`, and `GET /api/history/<id>` has the route's figures as `route`.
- A train's sheet on the dashboard lists its coaches from the front, where the operator sends them to Darwin: number, First Class or Standard, toilets, how busy each is and where it is quietest ("Quietest at the back"), and "Front 2 coaches detach here" when Darwin says so. Without them the sheet keeps the coach count; the board is unchanged.
- A TFT look for the board on an HDMI screen (`theme = "tft"`): white and yellow on black, like the newer station screens, with times in bold yellow and each status in a coloured pill backed by its words. E-paper isn't offered it.
- The dashboard's TFT look, in the same colours.
- Platform changes: a train whose platform isn't the one it was given earlier says `Plat 2 (changed)` on the bigger boards and screens (in the accent colour in the plain and TFT looks), and "2 (changed)" on the dashboard, where it also counts when it isn't the platform the train usually leaves from, and screen readers hear it once. A rail replacement bus is never a platform change.
- The dashboard's sheet says which platform a train usually leaves from ("Usually: Platform 1"), once its punctuality history has a few days to go on.
- National Rail's route words after a destination ("via Guide Bridge"): under the destination on the dashboard, and after it on the bigger boards and screens.
- Delay Repay on the Performance page: with a filter station (**Only trains calling at**), the trains that reached it 15 or more, or 30 or more, minutes late in the last 28 days, with the date, train, booked and actual arrival, minutes late and operator, and a link to National Rail's compensation page. The dashboard follows each train that has left until it gets there, only while it is due and never in the quiet hours: with Service Details if `RDM_DETAILS_API_KEY` is set, else one arrival board at the filter station per train check. The punctuality history moves to version 2, keeping everything already in it; `GET /api/performance` has the list as `delay_repay`.

### Changed

- The dashboard's **Board look** button is now a **Look** list, on the Performance page too: Standard, Dot matrix (the old Board look) and TFT. A browser that had the Board look keeps it.
- The user guide is a page per topic, indexed by `docs/user-guide.md`; the Docker pages are one, and the code layout is in `docs/architecture.md`.

### Fixed

- Rail replacement buses show. Darwin lists them apart from the trains (`busServices`), and they were left out, so on engineering works days the board and dashboard showed no trains. They are now in time order with the trains: `Bus` in the platform column (or `Bus:` before the destination where the board has no platform column), "Replacement bus" on the dashboard's row and sheet, and a punctuality record of their own.
- A Pi reads bus timetable files up to 61 MB, up from 50 MB: First Bus's largest is now 48 MB, and a bigger one was skipped.
- A Pi holds 8,500 bus journeys while it builds its timetable, up from 7,000, so two of a city's busiest stops no longer fail to build.
- On a Pi, the network guard also keeps connections off the fetch service, where systemd can't apply its `SocketBindDeny=` (a Pi Zero W).
- `update.sh` stops before changing anything where the app isn't installed.
- The docs say which cosign checks a release: 2.5 or later for `SHA256SUMS`, 3.0 or later for the image.

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

[Unreleased]: https://github.com/acornelissen/twobells/compare/v0.1.0...main
[0.1.0]: https://github.com/acornelissen/twobells/releases/tag/v0.1.0

# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

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

### Changed

- The package is organised by app area (`core`, `settings`, `sources`, `timetable`, `board`, `dashboard`, `web`, `setup`, `fetch`, `emulator`, `cli`), with the tests mirroring it.
- The repo keeps deploy files in `deploy/`, docs in `docs/` and example settings in `examples/`.
- The example settings lead with bus times from the BODS timetable.

[Unreleased]: https://github.com/acornelissen/inky-departures/commits/main

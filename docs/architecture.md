# Architecture

How the code is laid out. To set up and run the checks, see [CONTRIBUTING.md](../CONTRIBUTING.md).

One install is one app with two views of the same departures: the **board** (e-paper or an HDMI screen on a Pi) and the **dashboard** (a web page). `[app] views` says which are on. `settings/installs.py` says what each install (`pi`, `docker`, `desktop`) offers; every difference between installs belongs in its table.

## The processes

- **`twobells serve`:** the one web server, on port 8090 (`web/server.py`). The dashboard at `/` while its view is on, the settings site at `/settings`, and the setup wizard at `/settings/setup/`. On a Pi it is home-network only and never connects out.
- **`twobells fetch`:** on a Pi only, an outbound-only service doing the web server's internet work (the wizard's lookups, the dashboard's pollers), handed over through files in `/run/twobells-fetch`. In Docker and on a desktop that work runs in the server itself. See [The fetch service](security.md#the-fetch-service).
- **`twobells run`:** the board renderer: one tick a minute from a systemd timer on e-paper, or `--loop` on a screen.
- **`twobells timetable`:** builds the BODS bus timetable for your stops.

## The store

`settings/store.py`: one folder per install holding `settings.toml` (only the keys in `schema.saved_keys`), `secrets.env`, `web.json` (the one password), the timetable, history and locks. `config.toml` is optional, layered under it. `settings/schema.py` is the registry of every saved setting, by group (general, board, dashboard) and settings page. See [Where things are kept](configuration.md#where-things-are-kept).

## The package

One package, `src/twobells`, a folder per part of the app. Each folder's `__init__.py` says what it holds.

| Folder | Role |
|---|---|
| `cli/` | The commands, a module each (`run`, `serve`, `timetable`, `setup`, and `dev` for `demo`, `emulate`, `probe` and `route`), with `fetch` in `serve.py`, shared options in `common.py`, and the parser in `cli/main.py` (see [Commands](commands.md)). `__main__.py` makes it `python -m twobells` |
| `core/` | Shared building blocks: the provider-neutral `Departure` and lateness maths (`model.py`), when to poll (`polling.py`), the daily hit cap (`budget.py`), walk times (`walk.py`), the status and leave words every view uses (`words.py`), the HTTPS helper (`net.py`), files written whole or not at all, and lock files (`files.py`), XML that refuses a DOCTYPE (`safexml.py`), grid references (`osgb.py`), and short, key-free error messages (`errors.py`) |
| `settings/` | The store, the one folder an install saves in, layered over `config.toml` (`store.py`); every saved setting, by group and settings page, and the board's defaults (`schema.py`); loading them into the board's sources (`config.py`); the kinds of install and what differs between them (`installs.py`); saving a settings page's or the wizard's changes (`overlay.py`); each key's last test (`keyresults.py`) |
| `sources/` | Where departures and the rest come from, with HTTP injected so parsing is tested with fixtures: Darwin (`darwin.py`), the BODS bus source (`bodsbus.py`: timetable plus live positions, with TransportAPI standing in), its matching (`livebus.py`) and the day's last buses (`lastbus.py`), BODS positions (`bods.py`), TransportAPI (`transportapi.py`), disruptions (`disruptions.py`), weather (`weather.py`) and bank holidays (`holidays.py`) |
| `timetable/` | The BODS bus timetable: built from TransXChange for your stops (`build.py`), kept between builds (`model.py`), the roads between stops for the dashboard's maps and placing live buses (`roads.py`), and route strips for the screen (`routesetup.py`, the `route` command) |
| `board/` | The board view: one tick, redrawn only on change (`app.py`), departures to rows (`view.py`), the looks, each one entry in `themes.py` (`sign.py` and `dotfont.py`, `render.py` with Inter from `fonts/`, and `tft.py`, the plain look drawn light on black for screens), panel sizes (`panel.py`), route strips (`route.py`), where each train and bus is on a screen's rows (`positions.py`), scrolling long text on a screen (`scroll.py`), state between runs (`state.py`) |
| `board/displays/` | Inky or PNG output (`display.py`), the Waveshare 10.85" e-Paper (G) driver (`waveshare.py`), and the HDMI screen with pygame (`screen.py`) |
| `dashboard/` | The dashboard view: its settings (`config.py`), pollers (`collector.py`), the versioned snapshot the page shows (`snapshot.py`), row extras (`enrich.py`), the area map (`area.py`), punctuality history in SQLite (`history.py`), the Performance page's figures (`performance.py`), the Delay Repay watch of arrivals at the filter station (`repay.py`) |
| `web/` | The one web server on port 8090 (`server.py`): the dashboard's page, files and API, and the settings site under `/settings` (`settingsite.py`). The Overview (`overview.py`, with the names it shows from `places.py` and each key's state from `keystate.py`), a `groupform.py` form per page with `formpages.py`, the Keys page (`keyspage.py`), checking times as rows (`windowrows.py`), `web.css`, `icons.py`, and `settings.js`, which marks unsaved changes. Sign-in and the home-network checks (`auth.py`), whether the server can connect out (`netguard.py`), reading the board's state for the Overview (`status.py`) |
| `setup/` | First-run setup: the wizard's JSON API (`api.py`), finding home, stations, stops and walk times (`lookups.py`), finding the bus companies and building their timetable (`discover.py`), key tests (`keytest.py`) |
| `setup/spool/` | On a Pi, the wizard's internet work handed from the web server to the fetch service through files in memory: the web server's side (`runner.py`), the fetch service's (`worker.py`), the folder (`folder.py`) and the checks on what each side writes (`checks.py`) |
| `fetch/` | The Pi's outbound-only fetch service (`service.py`), and the dashboard's snapshot handed to the web server as a file (`handoff.py`) |
| `emulator/` | The browser panel emulator (`server.py`, `emulator.html`), its demo and live wiring (`emulation.py`), and the fake sources and clock (`fake.py`) |
| `dashboard_static/` | The dashboard's pages (`index.html`, `performance.html`): HTML, CSS, JS modules with no build step, vendored Leaflet 1.9.4 |
| `wizard_static/` | The setup wizard's page: HTML, CSS, a JS module per step. It sits beside `dashboard_static/` because it uses the dashboard's `dom.js`, `icons.js`, Leaflet and Inter by relative path, which must resolve the same on disk as when served |
| `data/` | Stations and NaPTAN areas for setup, built by `tools/build_setup_data.py` |
| `version.py` | The installed version, for `--version` |

To add a provider, implement `fetch() -> list[Departure]` with a `name` and add it to `settings/config.py`.

## Around the package

- **Tests** mirror the package: `tests/<folder>/test_<module>.py`. `tests/test_deploy.py` keeps the deploy files and the docs that describe them in step, and `tests/test_docs.py` checks every link in the docs.
- **`deploy/`:** the Pi's units and scripts (`pi/`), the published image's compose file and the image's pinned requirements (`docker/`), the desktop's login files (`desktop/`), the Unraid template (`unraid/`) and `secrets.env.example`. `Dockerfile` and `docker-compose.yml` stay at the top, where `docker compose` looks.
- **`examples/`:** `config.example.toml`, every setting with notes, and `glossop.toml`, a filled-in one.
- **`tools/`:** building the setup data, fonts, the dot font and the pictures in `docs/images/`.
- **`.github/workflows/`:** CI, and the tagged release (PyPI, the signed GHCR image, the signed Pi tarball).
- **The dashboard's API** between the server and its page is read-only JSON and SSE, specified in [Dashboard API](dashboard-api.md); the page runs under a strict CSP.

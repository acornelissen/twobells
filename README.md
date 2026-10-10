# inky-departures

A train and bus departure app for your station and stops in Great Britain. One install is one app with two views of the same departures:

- **The board:** a Raspberry Pi with a four-colour Inky pHAT (250x122), a Waveshare 10.85" e-Paper (G) (1360x480), or a Waveshare 7.9" HDMI screen (1280x400, on its side).
- **The dashboard:** a web page for a phone or computer at home, with the next 10 trains and buses, calling points, bus strips and maps, the weather, disruptions and punctuality history.

A Pi can run either view or both; Docker and a desktop run the dashboard. Each install has one settings site, at `/settings` on port 8090, with one password and a setup wizard that finds your station, stops, walk times and bus companies from your postcode. The examples use Glossop, with trains to Manchester and buses from two nearby stops (`examples/glossop.toml`).

![Board preview](docs/images/preview.png)

New here? The [user guide](docs/user-guide.md) explains what everything on the board and the dashboard means, how to install it, how to use the settings, and how to look after it. [Getting your keys](docs/keys.md) covers the free keys it needs.

- **Where it works:** trains anywhere in Great Britain (National Rail Darwin). Bus times and positions from BODS cover England only. The rain warning works anywhere. Bank holidays follow England and Wales, Scotland or Northern Ireland.
- **Trains:** National Rail Darwin, via the Rail Data Marketplace. Free.
- **Buses:** worked out the way [bustimes.org](https://bustimes.org) does, from the DfT's Bus Open Data Service (BODS) timetable for your stops plus live bus positions, free and checked all day. On the board, TransportAPI (free plan: 30 hits a day) can stand in while there is no usable timetable, or be the only source.
- **E-paper** takes about 20 s to refresh, so the board redraws only when what it shows changes, and never more than once every 2 minutes. An HDMI screen redraws every second, with a running clock, countdowns and a page showing where the next bus is.
- **Two looks** for the board: `sign`, an amber dot-matrix station board (its font drawn for this project at one pixel per LED), and `plain`, black on white with a red accent.
- **Meaning is always in text** (`Exp 07:49`, `Cancelled`, `No info`, `+4`, `sch`, `Not updating`). Colour only reinforces it, so both views work for colour-blind viewers, and the board works on a mono panel.

## Install

- **Raspberry Pi** (the board, and the dashboard if you like): download a release tarball and its `SHA256SUMS`, check it with `sha256sum --check --ignore-missing SHA256SUMS`, unpack it and run `sudo ./deploy/pi/install.sh`. Nothing is piped from the internet into `sudo bash`. Then open `http://<pi name>.local:8090/settings`. `sudo ./deploy/pi/upgrade.sh VERSION` moves it to another release, checked the same way. See [On a Raspberry Pi](docs/user-guide.md#on-a-raspberry-pi).
- **Docker** (a NAS, say; the dashboard): copy the folder, copy `deploy/secrets.env.example` to `secrets.env`, and `docker compose up -d --build`. The container is `departureboard`, keeping its data in the `dashboard-data` volume. See [Docker](docs/user-guide.md#9-docker). From the first release, `deploy/docker/docker-compose.ghcr.yml` runs the published, signed image instead; [Docker on other NASes and on Linux](docs/docker.md) covers QNAP, Unraid, TrueNAS SCALE, and Docker or Podman.
- **Desktop** (a Mac or Linux laptop; the dashboard): `uv tool install departureboard` or `pipx install departureboard` (from PyPI from the first release), then `departureboard serve`. `deploy/desktop` has a launchd agent and a systemd user service to start it at login. See [Desktop](docs/user-guide.md#10-desktop).

No `config.toml` is needed: the setup wizard saves your settings in the install's store. `examples/config.example.toml` lists everything that can be set by hand.

The settings password is claimed with a code shown on the board, or put in the log where there is no board. Or set it from the command line; in Docker:

```bash
docker exec -it departureboard python -m departureboard serve --set-password --state-dir /data
docker exec -it departureboard python -m departureboard serve --reset-code --state-dir /data
```

Upgrading from the separate board settings page (port 8080) or `departureboard dashboard`: see [Upgrading](docs/user-guide.md#13-upgrading-from-before-the-two-views).

## Releases

CI runs ruff, the tests on Python 3.11, 3.12 and 3.13, and builds the wheel and the container (amd64, arm64) on every push and pull request. A tag `vX.Y.Z` matching `pyproject.toml`'s version publishes a release: the package on PyPI, a signed container image on GHCR (`ghcr.io/acornelissen/inky-departures`), and a source tarball for the Pi with `SHA256SUMS`, signed with Sigstore. `departureboard --version` prints the installed version. See `.github/workflows/release.yml` for how to verify the signatures.

## Develop on your laptop

With [mise](https://mise.jdx.dev) (it pins python, ruff, node and uv in `mise.toml`):

```bash
mise install                 # the pinned tools
mise run setup               # .venv with the package and its dev tools, as uv.lock pins them
mise run test
mise run lint                # ruff
mise run typecheck           # pyright
mise run demo                # sample board to preview.png, no keys
mise run emulate             # the browser panel emulator on sample data
mise run serve               # the dashboard and settings, with ./config.toml and ./secrets.env
```

[CONTRIBUTING.md](CONTRIBUTING.md) lists the checks a change must pass.

Without mise: `uv sync && uv run pytest`.

Then, with the venv active (`. .venv/bin/activate`, or any shell where mise is activated):

```bash
departureboard demo --png preview.png            # sample data, no keys
departureboard demo --png big.png --display waveshare-10in85g   # or hdmi-1280x400
departureboard run --png preview.png --config config.toml --secrets secrets.env
departureboard serve --config config.toml --secrets secrets.env   # the dashboard and settings
```

## Emulator

A browser page that draws the panel at true proportions and logs every tick, so you can see when the board redraws and when it leaves the panel alone. E-paper panels show in e-paper colours and play the slow colour refresh; the HDMI screen swaps frames at once and ticks every second.

```bash
departureboard emulate --demo                       # fake data, 1 simulated minute every 2 s
departureboard emulate --fake-train \
  --config config.toml --secrets secrets.env        # fake trains, live buses
departureboard emulate --config config.toml --secrets secrets.env   # all live
departureboard emulate --demo --display hdmi-1280x400                # another panel
```

It opens <http://127.0.0.1:8780/>, or the next free port (`--port` picks another; `--no-browser` leaves the browser alone). Live modes emulate the `display` in your config; `--display` overrides it. Choose realistic (20 s), quick or instant refresh, and the scale (1× to 4× for the pHAT, ½× or 1× for the bigger panels); 1× is the panel's real pixel count. `--tick SECONDS` sets the tick (e-paper: 2 in the demo, 60 live; the screen: 1), and `--accent red` or `yellow` the demo's colour. The demo drops the bus feed from 07:30 to 07:52 simulated time, so the "Not updating" badge shows up.

On start the emulator fetches every source once, even outside its windows, so the board fills straight away; after that it polls only inside the windows. Bus times from the BODS timetable cost nothing. Where TransportAPI gives them (`source = "transportapi"`, or standing in while there is no timetable), a live start costs one hit per bus stop; the daily budget still applies, and when it is spent the first fetch is skipped too. `departureboard run` does the same on its very first run, when there is no saved state yet. Live modes keep state in the store, so the TransportAPI budget holds across restarts.

## The dashboard's API

The server and the dashboard page talk through a read-only JSON and SSE API, specified in [docs/dashboard-api.md](docs/dashboard-api.md); the page is vanilla JS modules with no build step, under a strict CSP.

## Layout

One package, `src/departureboard`, a folder per part of the app. Each folder's `__init__.py` says what it holds.

| Folder | Role |
|---|---|
| `cli/` | The commands, a module each (`run`, `serve`, `timetable`, `setup`, and `dev` for `demo`, `emulate`, `probe` and `route`), and the parser in `cli/main.py` (see the [user guide](docs/user-guide.md#11-commands)). `__main__.py` makes it `python -m departureboard` |
| `core/` | Shared building blocks: the provider-neutral `Departure` and lateness maths (`model.py`), when to poll (`polling.py`), the daily hit cap (`budget.py`), walk times (`walk.py`), the HTTPS helper (`net.py`), files written whole or not at all, and lock files (`files.py`), XML that refuses a DOCTYPE (`safexml.py`), grid references (`osgb.py`), and short, key-free error messages (`errors.py`) |
| `settings/` | The store, the one folder an install saves in, layered over `config.toml` (`store.py`); every saved setting, by group, and the board's defaults (`schema.py`); loading them into the board's sources (`config.py`); the kinds of install and what differs between them (`installs.py`); saving a tab's or the wizard's changes (`overlay.py`); bringing the old dashboard's folder in, once (`migrate.py`) |
| `sources/` | Where departures and the rest come from, with HTTP injected so parsing is tested with fixtures: Darwin (`darwin.py`), the BODS bus source (`bodsbus.py`: timetable plus live positions, with TransportAPI standing in) and its matching (`livebus.py`), BODS positions (`bods.py`), TransportAPI (`transportapi.py`), disruptions (`disruptions.py`), weather (`weather.py`) and bank holidays (`holidays.py`) |
| `timetable/` | The BODS bus timetable: built from TransXChange for your stops (`build.py`), kept between builds (`model.py`), the roads between stops for the dashboard's maps (`roads.py`), and route strips for the screen (`routesetup.py`, the `route` command) |
| `board/` | The board view: one tick, redrawn only on change (`app.py`), departures to rows (`view.py`), the two looks (`themes.py`, `sign.py` and `dotfont.py`, `render.py`), panel sizes (`panel.py`), route strips (`route.py`), state between runs (`state.py`) |
| `board/displays/` | Inky or PNG output (`display.py`), the Waveshare 10.85" e-Paper (G) driver (`waveshare.py`), and the HDMI screen with pygame (`screen.py`) |
| `dashboard/` | The dashboard view: its settings (`config.py`), pollers (`collector.py`), the versioned snapshot the page shows (`snapshot.py`), row extras (`enrich.py`), the area map (`area.py`), punctuality history in SQLite (`history.py`) |
| `web/` | The one web server on port 8090 (`server.py`): the dashboard's page, files and API, and the settings site under `/settings` (`settingsite.py`, a `groupform.py` form per tab, `formpages.py` and `web.css`), sign-in and the home-network checks (`auth.py`), the Board tab's status (`status.py`) |
| `setup/` | First-run setup: the wizard's JSON API (`api.py`), finding home, stations, stops and walk times (`lookups.py`), finding the bus companies and building their timetable (`discover.py`), key tests (`keytest.py`) |
| `setup/spool/` | On a Pi, the wizard's internet work handed from the web server to the fetch service through files in memory: the web server's side (`runner.py`), the fetch service's (`worker.py`), the folder (`folder.py`) and the checks on what each side writes (`checks.py`) |
| `fetch/` | The Pi's outbound-only fetch service (`service.py`, the `fetch` command), and the dashboard's snapshot handed to the web server as a file (`handoff.py`) |
| `emulator/` | The browser panel emulator (`server.py`, `emulator.html`), its demo and live wiring (`emulation.py`), and the fake sources and clock (`fake.py`) |
| `dashboard_static/` | The dashboard's page: HTML, CSS, JS modules, vendored Leaflet 1.9.4 |
| `wizard_static/` | The setup wizard's page: HTML, CSS, a JS module per step. It sits beside `dashboard_static/` because it uses the dashboard's `dom.js` and Leaflet by relative path, which must resolve the same on disk as when served |
| `data/` | Stations and NaPTAN areas for setup, built by `tools/build_setup_data.py` |
| `version.py` | The installed version, for `--version` |

Tests mirror the package: `tests/<folder>/test_<module>.py`, with `tests/test_deploy.py` keeping the deploy files and their docs in step. `deploy/` holds the Pi's units and scripts (`pi/`), the published image's compose file and the image's pinned requirements (`docker/`), the desktop's login files (`desktop/`), the Unraid template (`unraid/`) and `secrets.env.example`; `Dockerfile` and `docker-compose.yml` stay at the top, where `docker compose` looks.

To add a provider, implement `fetch() -> list[Departure]` with a `name` and add it to `settings/config.py`.

## Licence

MIT: see [LICENSE](LICENSE). The data the app shows comes under its providers' own terms.

## Data credits

See [NOTICE.md](NOTICE.md) for every data source and licence.

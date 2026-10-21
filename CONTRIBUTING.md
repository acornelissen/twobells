# Contributing

Thanks for helping. Bug reports and small, focused changes are the easiest to take. To report a vulnerability, see [SECURITY.md](SECURITY.md) instead.

- [Set up](#set-up)
- [Run it](#run-it)
- [The emulator](#the-emulator)
- [Checks](#checks)
- [Commits](#commits)
- [How the code is written](#how-the-code-is-written)
- [Pictures](#pictures)
- [Releases](#releases)

## Set up

With [mise](https://mise.jdx.dev), which pins python, ruff, node and uv in `mise.toml`:

```bash
mise install          # the pinned tools
mise run setup        # .venv with the package and its dev tools, as uv.lock pins them
```

`mise tasks` lists the rest. Without mise: `uv sync --locked`, then `uv run pytest`.

## Run it

```bash
mise run demo         # sample board to preview.png, no keys
mise run emulate      # the browser panel emulator on sample data
mise run serve        # the dashboard and settings, with ./config.toml and ./secrets.env
```

Or with the venv active (`. .venv/bin/activate`, or any shell where mise is activated):

```bash
twobells demo --png preview.png            # sample data, no keys
twobells demo --png big.png --display waveshare-10in85g   # or hdmi-1280x400, hdmi-1920x440
twobells run --png preview.png --config config.toml --secrets secrets.env
twobells serve --config config.toml --secrets secrets.env   # the dashboard and settings
```

On a Pi with an HDMI screen, `twobells run --loop --timings` logs how long each second's board took to make and show, and any frames skipped; [Troubleshooting](docs/troubleshooting.md) says how to run the screen's service that way. Every command and its options are in [Commands](docs/commands.md).

## The emulator

A browser page that draws the panel at true proportions and logs every tick, so you can see when the board redraws and when it leaves the panel alone. E-paper panels show in e-paper colours and play the slow colour refresh; the HDMI screen swaps frames at once and ticks every second.

```bash
twobells emulate --demo                       # fake data, 1 simulated minute every 2 s
twobells emulate --fake-train \
  --config config.toml --secrets secrets.env        # fake trains, live buses
twobells emulate --config config.toml --secrets secrets.env   # all live
twobells emulate --demo --display hdmi-1280x400                # another panel
```

- **Where:** it opens <http://127.0.0.1:8780/>, or the next free port (`--port` picks another; `--no-browser` leaves the browser alone).
- **Panel:** live modes emulate the `display` in your config; `--display` overrides it. Choose realistic (20 s), quick or instant refresh, and the scale (1× to 4× for the pHAT, ½× or 1× for the bigger panels); 1× is the panel's real pixel count.
- **Clock:** `--tick SECONDS` sets the tick (e-paper: 2 in the demo, 60 live; the screen: 1), and `--accent red` or `yellow` the demo's colour. The demo drops the bus feed from 07:30 to 07:52 simulated time, so the "Not updating" badge shows up.
- **What it fetches:** on start it fetches every source once, even outside its windows, so the board fills straight away; after that it polls only inside the windows. Bus times from the BODS timetable cost nothing. Where TransportAPI gives them, a live start costs one lookup per bus stop, within the daily budget; `twobells run` does the same on its very first run. Live modes keep state in the store, so the budget holds across restarts.

## Checks

Every change passes all three before it is ready:

```bash
mise run test         # pytest, in parallel (plain pytest runs serially)
mise run lint         # ruff
mise run typecheck    # pyright
```

- Tests mirror the package: the tests for `src/twobells/<folder>/<module>.py` are in `tests/<folder>/test_<module>.py`. `tests/test_deploy.py` keeps the deploy files and the docs that describe them in step, so change them together with the code.
- Some tests guard the layout: the folders import downwards only (`tests/test_layers.py`), no module uses another's leading-underscore names (`tests/test_private_names.py`), and a check uses `fullmatch`, never `match` or `search` (`tests/test_patterns.py`). [Architecture](docs/architecture.md) says why.
- After changing `src/twobells/board/dotfont.py`, run `.venv/bin/python tools/build_dotfont.py` and commit the dashboard's Dot matrix font it writes: `tests/tools/test_build_dotfont.py` fails until you do. `tools/build_fonts.py` (Inter) and `tools/build_setup_data.py` (the stations and stops in `src/twobells/data/`) are run now and then; each says how at its top.
- Fixing a bug starts with a test that fails because of it. Then the fix.
- When a test fails, fix the code, not the test. Change a test only when the intended behaviour has changed, and say so.
- The JS behaviour tests run when `node` is on PATH.
- Don't run a formatter over the codebase. ruff checks; it doesn't reformat.

## Commits

Conventional commits: `<type>(<scope>): <subject>`, in the imperative, like `fix(timetable): keep journeys that start at your stop`. The body says why. Keep each commit small enough to revert on its own.

## How the code is written

- **Security first.** Treat every input as hostile: from the network, the settings forms, files and other services. Check it where it comes in. Units and containers get the least privilege that works.
- **Accessible pages.** WCAG 2.2 AA is the floor for every page: it works from the keyboard, uses semantic markup, has enough contrast and a visible focus.
- **Plain British English** in code, UI text and docs. Short sentences, active voice, no emoji.
- **Simple code.** Match the style around it.

[Architecture](docs/architecture.md) shows where things are, and [Dashboard API](docs/dashboard-api.md) is the contract between the server and the dashboard page.

## Pictures

The pictures in `docs/images/` are drawn from the sample data. After a change to how the board looks, run `.venv/bin/python tools/build_doc_images.py` with no arguments: any argument, `--help` among them, stops it before it draws over them. The rest are taken by hand in a browser from two servers on this checkout, which listen on 127.0.0.1 only:

- **The dashboard:** `.venv/bin/python tools/serve_doc_dashboard.py` serves the page on the sample morning at <http://127.0.0.1:8931/>, its clock starting at 07:01:30. `?kiosk` gives the wall tablet's page, `/performance` the Performance page, and `--quiet` a quiet window, so the wall tablet dims to its night clock. `--trams` adds a made-up Trams section (Piccadilly's trams that morning), and `--london` a made-up London one instead (Bank's Underground and DLR, with a line's status and a lift out of order). `--leave bus` (modes separated by commas) has Next to catch pick from the buses alone, and `--leave none` leaves it out, so the time and the weather stand in its place.
- **A phone or a tablet:** a browser window can't always be made that size, so `--phone` also serves <http://127.0.0.1:8931/frame>, the page in a frame 390 by 844 CSS pixels, and `--frame 1280x800` a frame of another size. The frame is its own viewport, so the page lays itself out for that size; `/frame?kiosk` frames the wall tablet's page. The browser tools may not scroll the page, so `--offset 1200` with a tall frame (`--frame 1376x2400`) moves the frame up, to picture a part far down it, like the Trams section. Choose the look on `/` first, as the frame shares its storage. Only the tool can be framed: the app sends `frame-ancestors 'none'`.
- **The settings and the setup wizard:** `.venv/bin/python tools/serve_doc_settings.py` serves the real settings site at <http://127.0.0.1:8932/settings>, over a throwaway store with Glossop's settings and made-up keys, already signed in. The wizard's lookups answer with sample answers and never reach the internet; type any postcode. `--install desktop` (or `docker`) gives that install's tabs and words; the default, `pi`, has them all.
- **Light or dark:** both take `--scheme light` or `--scheme dark`, which serve that scheme's colours whatever the browser prefers.
- **Busy places:** the README's first pictures are made-up mornings at real places, in `tools/doc_scenes.py`, with real station, stop, line and operator names, invented times and nothing of anyone's home. `piccadilly` is Manchester Piccadilly's main-line trains (Avanti West Coast, TransPennine Express, Northern, CrossCountry, with a cancellation, a platform change and via words), Metrolink trams from Piccadilly and Bee Network buses from Piccadilly Gardens; `bank` the Underground and DLR at Bank and Monument, with a line's status and a lift out of service, and London buses; `greenwich` Southeastern and Thameslink trains, the DLR from Cutty Sark, river buses from Greenwich Pier with the cable car shut by the wind, and London buses. `build_doc_images.py` draws them on the board, named for the panel and the scene (`epaper-10in85-piccadilly.png`, `screen-bar-piccadilly.png`, `screen-bar-flap-greenwich.png`). `serve_doc_dashboard.py --scene piccadilly` (or `bank`, `greenwich`) serves one in Glossop's place, its clock starting at 07:31:30, without the area map.

Take them at 2x and save them at 0.75 scale in 256 colours, with a quantiser that keeps the line colours (Pillow's `FASTOCTREE`; median cut turns the Central line's red brown): the dashboard 1376 CSS pixels wide, as `dashboard-piccadilly-dark.png` is, that and `dashboard-piccadilly-light.png` the top 820 CSS pixels of `--scene piccadilly`, `dashboard-bank.png` (dark) and `dashboard-greenwich.png` (light) the top 1065, down to Bank's station information, from two frames (the lower part with `--offset 330`), `dashboard-looks.png` three phone frames side by side (Standard light, Standard dark, Dot matrix, 798 CSS pixels high, 24 apart), `dashboard-kiosk-night.png` a 1280x800 frame of `/frame?kiosk` with `--quiet`, after choosing Standard on `/` (with no look saved, a wall tablet takes the Dot matrix look), `dashboard-trams.png` and `dashboard-london.png` their section cropped as `dashboard-boards.png` is, `dashboard-tft.png` and `dashboard-flap.png` the Trains and Buses sections of the sample morning cropped as `dashboard-boards.png` is (1332 by 400 CSS pixels, from just above the Trains heading), about 07:05 on its clock, after choosing **TFT** or **Split-flap** under **Look** at the foot of `/` (both looks are on black, whatever the scheme), the Split-flap one with the first train's calling-at line part way through its scroll, `dashboard-now.png` (`--leave none`) the top of the page down to the disruption line, in the dark scheme, and the settings and wizard cropped to their column, all with `--install desktop` (`settings-dashboard.png` shows its five tabs; `setup-walk.png` after ticking stop B as well as A on the bus stops step, as only A is ticked at first; `setup-trams.png` after a search for Piccadilly). Look at each picture before you commit it.

## Releases

CI (`.github/workflows/ci.yml`) runs ruff, pyright, the tests on Python 3.11, 3.12 and 3.13, and builds the wheel and the container (amd64, arm64) on every push to main and every pull request. Once a month `data.yml` rebuilds the setup data from NaPTAN and opens a pull request if it changed.

To cut a release, set the version in `pyproject.toml`, add the release to [CHANGELOG.md](CHANGELOG.md), merge to main, then push a signed tag: `git tag -s vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`. The tag starts `.github/workflows/release.yml`, which runs in this order:

1. **verify:** the tag is `vX.Y.Z`, matches `pyproject.toml`'s version and is on main; lint, the type check and the tests pass.
2. **dist**, **image** and **tarball:** the sdist and wheel; the container image (amd64, arm64), pushed to GHCR (`ghcr.io/acornelissen/twobells`) by its digest alone, with no tag and no signature; the Pi's source tarball and `SHA256SUMS`, not signed yet.
3. **pypi:** waits for the owner's approval (the `pypi` environment), then publishes to PyPI. This is the release's one approval gate: nothing is tagged or signed before it.
4. **image-tags** and **sign-sums**, once approved: the image is signed by digest and tagged `X.Y.Z`, and `latest` too if it is the highest version; `SHA256SUMS` is signed with Sigstore.
5. **release:** the GitHub release, with the tarball, `SHA256SUMS`, its signature and the wheel.

How to check the signatures is in [Install on a Raspberry Pi](docs/install-pi.md#install-from-a-release) and [Docker](docs/docker.md#pick-a-release-and-check-it).

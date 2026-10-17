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

Every command and its options are in [Commands](docs/commands.md).

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
mise run test         # pytest
mise run lint         # ruff
mise run typecheck    # pyright
```

- Tests mirror the package: the tests for `src/twobells/<folder>/<module>.py` are in `tests/<folder>/test_<module>.py`. `tests/test_deploy.py` keeps the deploy files and the docs that describe them in step, so change them together with the code.
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

The pictures in `docs/images/` are drawn from the sample data. After a change to how the board looks, run `.venv/bin/python tools/build_doc_images.py`. The rest are taken by hand in a browser from two servers on this checkout, which listen on 127.0.0.1 only:

- **The dashboard:** `.venv/bin/python tools/serve_doc_dashboard.py` serves the page on the sample morning at <http://127.0.0.1:8931/>, its clock starting at 07:01:30.
- **The settings and the setup wizard:** `.venv/bin/python tools/serve_doc_settings.py` serves the real settings site at <http://127.0.0.1:8932/settings>, over a throwaway store with Glossop's settings and made-up keys, already signed in. The wizard's lookups answer with sample answers and never reach the internet; type any postcode. `--install desktop` (or `docker`) gives that install's tabs and words; the default, `pi`, has them all.
- **Light or dark:** both take `--scheme light` or `--scheme dark`, which serve that scheme's colours whatever the browser prefers.

Take them at 2x and save them at 0.75 scale in 256 colours: the dashboard 1376 CSS pixels wide, as `dashboard-dark.png` is, and the settings and wizard cropped to their column. Look at each picture before you commit it.

## Releases

CI runs ruff, pyright, the tests on Python 3.11, 3.12 and 3.13, and builds the wheel and the container (amd64, arm64) on every push and pull request.

A tag `vX.Y.Z` matching `pyproject.toml`'s version publishes a release: the package on PyPI, a signed container image on GHCR (`ghcr.io/acornelissen/twobells`), and a source tarball for the Pi with `SHA256SUMS`, signed with Sigstore. Add the release to [CHANGELOG.md](CHANGELOG.md) first. How to check the signatures is in [Install on a Raspberry Pi](docs/install-pi.md#install-from-a-release) and [Docker](docs/docker.md#pick-a-release-and-check-it).

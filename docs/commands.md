# Commands

`twobells <command> --help` lists every option, and `twobells --version` prints the version. On a Pi, the app is `/opt/twobells/venv/bin/twobells`; run it as the `twobells` user for commands that write to the store (`sudo -u twobells …`).

Every command but `demo` takes `--config FILE` and `--secrets FILE`, which default to `/etc/twobells/config.toml` and `/etc/twobells/secrets.env`. Neither has to be there.

| Command | What it does | Options |
|---|---|---|
| `serve` | The web server: the settings at `/settings`, and the dashboard at `/` where its view is on. `twobells-web.service` runs it on a Pi, and the container in Docker. | `--install pi`, `docker` or `desktop` (the default): what it runs on. `--state-dir DIR`, `--host` (default all addresses on a Pi and in Docker, this computer only on a desktop), `--port` (default 8090, or on a desktop the next free one), `--lan` for every address, `--no-browser`. `--set-password` or `--reset-code`: see [Password and reset codes](password.md). |
| `fetch` | A Pi's internet work for its web server. `twobells-fetch.service` runs it; there's no need to run it yourself. | `--install pi`, the only install with one, and `--state-dir DIR`. |
| `run` | One tick of the board: check what's due, redraw if anything changed. `twobells.timer` runs it every minute. | `--png FILE` draws to a picture instead of the display. `--loop` keeps running, as the screen's service does; `--tick SECONDS` sets how often it ticks then (1 on a screen, 60 on e-paper). |
| `timetable` | Builds the bus timetable for your stops from BODS, and the tram timetable for your tram stops from TfGM (see [Tram times](trams.md)), each if a build is due and a view that is on shows it (`--install` says which views are on when `[app] views` doesn't). `twobells-timetable.timer` runs it hourly. | `--force` builds it now. `--install pi`, `docker` or `desktop` (the default): what it runs on; a Pi reads less of each timetable file, to fit its memory. |
| `probe train` or `probe bus` | One live call, to check the keys, station and stops. | `--stop LABEL=ATCO` (once per stop) asks those stops instead of the settings'. `--save DIR` keeps TransportAPI's raw answers. `--details` (trains only) adds coaches and calling points. |
| `route LINE` | Prints a `[bus.routes]` block for the screen's bus page. | `--stop LABEL` (needed), and `--noc CODE` or `--txc FILE`; see [The bus position page](displays.md#the-bus-position-page). |
| `setup discover` | Finds the bus companies serving your stops, builds their timetable, and prints the settings to add to `config.toml`. Needs `BODS_API_KEY`. | `--stop LABEL=ATCO` (needed, once per stop). `--routes` also builds a route strip for each line. |
| `demo` | Draws a sample board (a Glossop morning) to a picture, with no keys. | `--png FILE` (default `preview.png`), `--display` (default `inky`), `--theme sign`, `plain`, `tft` or `flap` (the TFT and split-flap looks, on a screen only), `--accent red` or `yellow`, `--notice` for a sample disruption notice, `--rain` for a sample rain warning (both together show side by side on a screen). |
| `emulate` | Runs the board against a panel drawn in your browser (see [The emulator](../CONTRIBUTING.md#the-emulator)). | `--demo` for sample data on a fast clock, `--fake-train` for fake trains and live buses, `--display`, `--accent red` or `yellow` (demo only), `--tick SECONDS`, `--port` (default 8780), `--no-browser`. |

The Pi's logs, and the command that builds the bus timetable now, are in [Looking after a Pi](pi.md#logs).

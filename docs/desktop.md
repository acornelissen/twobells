# Desktop

On a Mac or a Linux laptop, the dashboard runs while you leave it running. It needs Python 3.11 or later, and its only dependency is Pillow: the Pi's extras (the Inky driver, pygame) aren't needed. Install it with [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/), which give it a virtual environment of its own and put the `twobells` command in `~/.local/bin`. From PyPI:

```bash
uv tool install twobells      # or: pipx install twobells
twobells serve
```

To update: `uv tool upgrade twobells` or `pipx upgrade twobells`. From a copy of the repo, `uv tool install .` or `pipx install .` instead.

`twobells serve` runs as a desktop install unless told otherwise. It opens in your browser, at `http://localhost:8090/` (or the next free port), and the settings on the first run. The first build of the bus timetable takes a minute or so. Ctrl-C stops it.

- `--lan` lets phones on the home Wi-Fi open it, at the address it prints. `--host` and `--port` pick others (a port you name is that port or nothing); `--no-browser` leaves the browser alone.
- `--config config.toml` and `--secrets secrets.env` use files of your own, if you keep them. When either changes, or a setting is saved, it starts again by itself within a few seconds.
- It keeps its files in its [store](configuration.md#where-things-are-kept): `~/Library/Application Support/twobells` on a Mac, and `~/.local/state/twobells` on Linux (unless `$XDG_STATE_HOME` says otherwise). `--state-dir` names another folder.
- To see the board without a Pi, `twobells emulate --demo` draws it in your browser on sample data (see [Commands](commands.md)).

## Start it when you log in

The release tarball and the repo have a file for each in `deploy/desktop`. Both run `twobells serve` from `~/.local/bin` on this computer only, at `http://localhost:8090/`, without opening the browser, and start it again if it fails. Run the commands from the tarball's or the repo's folder.

**On a Mac** (a launchd agent). The file names your home folder as `/Users/YOU`; `sed` puts yours in:

```bash
mkdir -p ~/Library/LaunchAgents
sed "s|/Users/YOU|$HOME|g" deploy/desktop/uk.twobells.serve.plist \
  > ~/Library/LaunchAgents/uk.twobells.serve.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/uk.twobells.serve.plist
```

It starts now and at each login. Its log, with any reset code, is `~/Library/Logs/twobells.log`. To stop it until the next login: `launchctl bootout "gui/$(id -u)/uk.twobells.serve"`. To stop it for good, run that and delete `~/Library/LaunchAgents/uk.twobells.serve.plist`.

**On Linux** (a systemd user service):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/desktop/twobells.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now twobells.service
```

It starts now and whenever you log in. Its log, with any reset code: `journalctl --user -u twobells -f`. To stop it: `systemctl --user stop twobells.service`; to stop it starting at login: `systemctl --user disable --now twobells.service`.

If `command -v twobells` doesn't print `~/.local/bin/twobells` (with your home folder), change that path in the file before copying it.

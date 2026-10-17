# Install on a Raspberry Pi

A Pi runs the board, the dashboard, or both. For the dashboard alone, [Docker](docker.md) or a [desktop](desktop.md) is also fine.

- [What you need](#what-you-need)
- [Install from a release](#install-from-a-release)
- [Start the board](#start-the-board)
- [Updating](#updating)

## What you need

- **Raspberry Pi OS Lite** (Bookworm or later), with Wi-Fi and SSH set up in Raspberry Pi Imager.
- **A Pi to suit the display:** a Zero W runs the Inky pHAT; the Waveshare panel and the HDMI screens want a Zero 2 W or better. See [Displays](displays.md).
- **On a Pi Zero, Zero W or Pi 1, Raspberry Pi OS Trixie.** There only the network guard keeps the web server off the internet, and it needs Trixie's systemd. See [How the web server is kept safe](security.md).

## Install from a release

Download a release's tarball and `SHA256SUMS` from the [releases page](https://github.com/acornelissen/twobells/releases), check the tarball against it, then run the installer from inside. On the Pi, with the version you want in the first line:

```bash
VERSION=0.2.3
base="https://github.com/acornelissen/twobells/releases/download/v$VERSION"
curl -fLO "$base/twobells-$VERSION.tar.gz"
curl -fLO "$base/SHA256SUMS"
curl -fLO "$base/SHA256SUMS.sigstore.json"
sha256sum --check --ignore-missing SHA256SUMS &&
  tar -xzf "twobells-$VERSION.tar.gz" &&
  cd "twobells-$VERSION" &&
  sudo ./deploy/pi/install.sh
```

`sha256sum` must say `OK`. If it doesn't, nothing after it runs: delete the download and don't run anything from it. Nothing is ever piped from the internet into `sudo bash`.

`SHA256SUMS` is signed too, so you can check who made it. With [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) 2.5 or later installed (older ones can't read the signature), run this after the downloads and before the `sha256sum` line. It must say `Verified OK`:

```bash
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-identity "https://github.com/acornelissen/twobells/.github/workflows/release.yml@refs/tags/v$VERSION" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

A copy of the repo works the same way: `sudo ./deploy/pi/install.sh` from its folder.

`install.sh` installs the app in `/opt/twobells` and starts the web server and the fetch service (see [Looking after a Pi](pi.md)). Then open `http://<pi name>.local:8090/settings` on a phone or computer on your home network, and carry on with [First run and the setup wizard](setup-wizard.md).

## Start the board

`install.sh` never starts the board itself; it prints the command that does:

```bash
sudo systemctl enable --now twobells.timer             # e-paper
sudo systemctl enable --now twobells-screen.service    # the HDMI screen
```

For an HDMI screen, first set `display = "hdmi-1280x400"` (or `"hdmi-1920x440"` for the 11.3" bar screen) in `/etc/twobells/config.toml` (`install.sh` puts one there) and run `install.sh` again: it installs pygame and espeak-ng (for [station announcements](displays.md#station-announcements-on-a-screen)) and names the screen's service. `install.sh --screen` only installs them up front; which service it names still follows `display`. See [Displays](displays.md).

## Updating

To move to another release, run `upgrade.sh` with its version, from the folder of the release you have (or a copy of the repo):

```bash
sudo ./deploy/pi/upgrade.sh 0.2.0
```

It downloads that release's tarball and `SHA256SUMS` over https into a temporary folder, and stops unless the tarball's SHA-256 matches. With `cosign` installed (2.5 or later) it checks the signature of `SHA256SUMS` first. Then it unpacks the release and runs its `deploy/pi/update.sh`, and removes the temporary folder. Options after the version go to `update.sh`.

Going back to an older release works the same way, but a newer release may have moved what it keeps on to a version the older one can't read. The punctuality history is one: 0.1.0 reads only its version 1, so after going back to it the dashboard has no punctuality history. Before moving the history on, the newer release kept a copy of it as it was, `history.sqlite3.v1` in `/var/lib/twobells`, and `upgrade.sh` says how to put it back when it goes to an older version. What was recorded since is lost:

```bash
sudo systemctl stop twobells-web twobells-fetch
sudo -u twobells cp /var/lib/twobells/history.sqlite3.v1 /var/lib/twobells/history.sqlite3
sudo -u twobells rm -f /var/lib/twobells/history.sqlite3-wal /var/lib/twobells/history.sqlite3-shm
sudo systemctl start twobells-fetch twobells-web
```

Or get the new release onto the Pi as for installing, check it, then from its folder:

```bash
sudo ./deploy/pi/update.sh
```

What `update.sh` does:

- It stops before changing anything if the app isn't installed in `/opt/twobells`, and says to run `install.sh`.
- It holds the board's timer while it works, so no tick runs half-installed code, and starts it again at the end (also if the update stops part way).
- It first installs what the new release needs: `nftables` if it's missing, pygame and espeak-ng if `display` says `hdmi-…` and they're missing, and the Inky driver from PyPI at the versions and hashes the release pins (`deploy/pi/requirements-pi.txt`). It checks that the Inky driver and its libraries import, as `install.sh` does, and stops before touching the code if one doesn't, so the old code still runs.
- Then it installs the new code, checks that imports too, refreshes the systemd units, loads the network guard afresh, restarts the fetch service and the web server (if it is running), starts a timetable build if buses come from BODS or trams are set up (`journalctl -u twobells-timetable -f` follows it), restarts the screen's service if it is running, and runs the e-paper board once if its timer is on.
- `--config FILE` and `--secrets FILE` replace `/etc/twobells/config.toml` and `secrets.env` too (the secrets copy is shredded).

After updating, the bus timetable is built again once if the new release keeps it differently, as 0.2.0 and 0.2.2 do (see [Bus times](buses.md#how-bus-times-are-worked-out)).

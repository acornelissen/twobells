# Install on a Raspberry Pi

A Pi runs the board, the dashboard, or both. For the dashboard alone, [Docker](docker.md) or a [desktop](desktop.md) is also fine.

- [What you need](#what-you-need)
- [Install cosign](#install-cosign)
- [Install from a release](#install-from-a-release)
- [Start the board](#start-the-board)
- [Updating](#updating)

## What you need

- **Raspberry Pi OS Lite** (Bookworm or later), with Wi-Fi and SSH set up in Raspberry Pi Imager.
- **A Pi to suit the display:** a Zero W runs the Inky pHAT; the Waveshare panel and the HDMI screens want a Zero 2 W or better. See [Displays](displays.md).
- **cosign 2.5 or later**, to check a release: see [Install cosign](#install-cosign).
- **On a Pi Zero, Zero W or Pi 1, Raspberry Pi OS Trixie.** There only the network guard keeps the web server off the internet, and it needs Trixie's systemd. See [How the web server is kept safe](security.md).

## Install cosign

Each release's `SHA256SUMS` is signed by the project's release workflow, and cosign checks that signature: it tells you the files came from this project's release, not only that they arrived whole. Installing and `upgrade.sh` both use it. It must be cosign 2.5 or later: the signature comes as a Sigstore bundle, `SHA256SUMS.sigstore.json`, which older ones can't read. (Checking the Docker image's signature needs 3.0 or later, as [Docker](docker.md#pick-a-release-and-check-it) says; a Pi checks only `SHA256SUMS`.)

On a Pi 2 or later, or a Zero 2 W, install sigstore's package for the Pi, checked against sigstore's list of sums:

```bash
COSIGN=3.1.3
deb="cosign_${COSIGN}_$(dpkg --print-architecture).deb"
curl -fLO "https://github.com/sigstore/cosign/releases/download/v$COSIGN/$deb"
curl -fLO "https://github.com/sigstore/cosign/releases/download/v$COSIGN/cosign_checksums.txt"
sha256sum --check --ignore-missing cosign_checksums.txt &&
  sudo apt-get install -y "./$deb"
cosign version
```

[Sigstore's install guide](https://docs.sigstore.dev/cosign/system_config/installation/) has other ways to install it, and how to check cosign's own signature.

A Pi Zero, Zero W or Pi 1 can't run cosign: sigstore builds it for ARMv7 and later only, and these are ARMv6 (`uname -m` says `armv6l`). On one of those, check the release's files with cosign on another computer and copy them to the Pi, as below, and give `upgrade.sh` `--no-signature-check` (see [Updating](#updating)).

## Install from a release

Download a release's tarball, `SHA256SUMS` and its signature from the [releases page](https://github.com/acornelissen/twobells/releases), check them, then run the installer from inside. On the Pi, with [cosign](#install-cosign) installed and the version you want in the first line:

```bash
VERSION=0.5.2
base="https://github.com/acornelissen/twobells/releases/download/v$VERSION"
curl -fLO "$base/twobells-$VERSION.tar.gz"
curl -fLO "$base/SHA256SUMS"
curl -fLO "$base/SHA256SUMS.sigstore.json"
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-identity "https://github.com/acornelissen/twobells/.github/workflows/release.yml@refs/tags/v$VERSION" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com &&
  sha256sum --check --ignore-missing SHA256SUMS &&
  tar -xzf "twobells-$VERSION.tar.gz" &&
  cd "twobells-$VERSION" &&
  sudo ./deploy/pi/install.sh
```

`cosign` must say `Verified OK`: this project's release workflow made that `SHA256SUMS` for that version. Then `sha256sum` must say `OK`: the tarball is the one `SHA256SUMS` names. If either doesn't, nothing after it runs: delete the download and don't run anything from it. Nothing is ever piped from the internet into `sudo bash`.

On a Pi that can't run cosign, download the three files on another computer and run the `cosign verify-blob` command there. Copy the files you checked to the Pi, and run the block there from the `sha256sum` line.

A copy of the repo works the same way: `sudo ./deploy/pi/install.sh` from its folder.

`install.sh` installs the app in `/opt/twobells`, starts the web server behind its network guard and the fetch service, and turns on the timetable timer, which builds nothing until a timetable is due (see [Looking after a Pi](pi.md)). Then open `http://<pi name>.local:8090/settings` on a phone or computer on your home network, and carry on with [First run and the setup wizard](setup-wizard.md).

## Start the board

`install.sh` never starts the board itself; it prints the command that does:

```bash
sudo systemctl enable --now twobells.timer             # e-paper
sudo systemctl enable --now twobells-screen.service    # the HDMI screen
```

For an HDMI screen, first set `display = "hdmi-1280x400"` (or `"hdmi-1920x440"` for the 11.3" bar screen) in `/etc/twobells/config.toml` (`install.sh` puts one there) and run `install.sh` again: it installs pygame, the EGL and GLES libraries it draws through (Raspberry Pi OS Lite comes without them), and espeak-ng (for [station announcements](displays.md#station-announcements-on-a-screen)) and names the screen's service. `install.sh --screen` only installs them up front; which service it names still follows `display`. See [Displays](displays.md).

## Updating

To move to another release, run `upgrade.sh` with its version, from the folder of the release you have (or a copy of the repo):

```bash
sudo ./deploy/pi/upgrade.sh 0.2.0
```

It needs [cosign](#install-cosign). It downloads that release's tarball, `SHA256SUMS` and its signature over https into a temporary folder, and stops unless cosign says the release workflow signed `SHA256SUMS` for that version and the tarball's SHA-256 matches it. Then it unpacks the release and runs its `deploy/pi/update.sh`, and removes the temporary folder. Options after the version go to `update.sh`, except `--no-signature-check`.

Without cosign, `upgrade.sh` stops before downloading anything and says how to install it. On a Pi that can't run cosign, `--no-signature-check` has it check only the tarball's SHA-256, and it says so. That catches a damaged download but not a forged one, as `SHA256SUMS` comes from the same place as the tarball:

```bash
sudo ./deploy/pi/upgrade.sh 0.2.0 --no-signature-check
```

`upgrade.sh` runs from the release you have, and the one in 0.4.3 and earlier goes on without cosign, checking the SHA-256 only. Install cosign first and it checks the signature too.

Going back to an older release works the same way. Going back to 0.1.0, which can't read the punctuality history a newer release keeps, put back the copy kept from before (see [Going back to an older release](configuration.md#going-back-to-an-older-release)); `upgrade.sh` prints these commands when it goes to an older version:

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
- It holds the board's timer while it works, so no tick runs half-installed code, and starts it again at the end if it was running (also if the update stops part way).
- It first installs what the new release needs: `nftables` if it's missing, pygame, the EGL and GLES libraries and espeak-ng if `display` says `hdmi-…` and they're missing, and the Inky driver from PyPI at the versions and hashes the release pins (`deploy/pi/requirements-pi.txt`). It checks that the Inky driver and its libraries import, as `install.sh` does, and stops before touching the code if one doesn't, so the old code still runs.
- Then it installs the new code, checks that imports too, refreshes the systemd units, loads the network guard afresh, restarts the fetch service, and the web server if it is running, turns the timetable timer on (an older install turned it on only if buses came from BODS when it was installed), starts a timetable build if a view that is on shows buses from BODS or Metrolink trams (`journalctl -u twobells-timetable -f` follows it), restarts the screen's service if it is running, and runs the e-paper board once if its timer is enabled.
- `--config FILE` and `--secrets FILE` replace `/etc/twobells/config.toml` and `secrets.env` too. The secrets file you named is then deleted with `shred -u`, which writes over it first. On an SD card or USB stick that isn't sure to reach the old contents, as the card puts each write where it chooses, so copies of the keys may stay on the card.

After updating, the bus timetable may be built again once (see [After an update](buses.md#after-an-update)).

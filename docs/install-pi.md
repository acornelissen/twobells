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
VERSION=0.5.3
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

- It runs alone. If another `update.sh` or `install.sh` is running (from another login, say), it stops at once and says so, before changing anything: let that one finish, then run it again. `install.sh` does the same. The lock goes when the run ends, however it ends, so there's never one to clear by hand.
- It stops before changing anything if the app isn't installed in `/opt/twobells`, and says to run `install.sh`.
- It first installs the Debian packages the new release needs: `nftables` if it's missing, and pygame, the EGL and GLES libraries and espeak-ng if `display` says `hdmi-…` and they're missing.
- It builds the new release in a folder of its own (see [Where the app is](#where-the-app-is)), beside the one running, which it doesn't touch: the board's timer and the services carry on meanwhile. The Inky driver comes from PyPI at the versions and hashes the release pins (`deploy/pi/requirements-pi.txt`). Then it checks that the new code, the Inky driver and their libraries all import. If anything fails up to here, it removes the new folder and stops, and the old code runs on as it was.
- Then it switches to the new release in one step, refreshes the systemd units, loads the network guard afresh, and restarts the fetch service, the web server if it is running, and the screen's service if it is running. It watches them for 30 seconds: if one crashes, it goes back to the release from before, with the units and network guard from before, restarts the services onto it, says so and which log says why, and stops.
- Once they stay up, it turns the timetable timer on (an older install turned it on only if buses came from BODS when it was installed), starts a timetable build if a view that is on shows buses from BODS or Metrolink trams (`journalctl -u twobells-timetable -f` follows it), runs the e-paper board once if its timer is enabled, and removes any release older than the one before.
- `--config FILE` and `--secrets FILE` replace `/etc/twobells/config.toml` and `secrets.env` too, before anything else, and stay if it goes back. The secrets file you named is then deleted with `shred -u`, which writes over it first. On an SD card or USB stick that isn't sure to reach the old contents, as the card puts each write where it chooses, so copies of the keys may stay on the card.

After updating, the bus timetable may be built again once (see [After an update](buses.md#after-an-update)).

### Where the app is

Each release is installed in a folder of its own, and two links say which is in use:

| Path | What it is |
|---|---|
| `/opt/twobells/releases/<version>-<6 letters>/venv` | One release: its Python environment, with the app and the Inky driver. The letters are random, so the same version installed again gets a folder of its own. |
| `/opt/twobells/venv` | A link to the release in use. The services and every command in these guides run the app through it. |
| `/opt/twobells/previous` | A link to the release before, kept to go back to. |
| `/opt/twobells/twobells-web.nft` | The network guard's rules. |

`install.sh` lays a new install out this way, and each time it is run again it makes a new release folder and keeps the one before, as `update.sh` does. An install from before releases had folders of their own, with the app in `/opt/twobells/venv` itself, is moved to `/opt/twobells/releases/<version>-inplace/venv` on its first update and kept as the one before. If the power goes during an update, the next run of `update.sh` or `install.sh` puts things right first, then updates.

Each release takes about 30 MB: two of them, the one in use and the one before, about 60 MB, and a third while an update builds the next. `ls -l /opt/twobells` shows which version each link points to.

Going back to a release from before releases had folders of their own (with `upgrade.sh`, say) runs that release's `update.sh`, which installs it over the release in use, in its folder, as it always did. The next update makes a folder of its own again.

### Going back to the release before

To go back, run `upgrade.sh` with the earlier version, as in [Updating](#updating): it checks and installs that release whole, units included.

Without the internet, switch the link back to the release before and restart the services onto it:

```bash
cd /opt/twobells
sudo ln -sfnT "$(readlink previous)" venv.new && sudo mv -T venv.new venv
sudo systemctl restart twobells-fetch.service
sudo systemctl try-restart twobells-web.service twobells-screen.service
```

That puts back the code only: the systemd units stay the newer release's. Both links then point to the same release, and the next update keeps it as the one before.

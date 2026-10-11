#!/usr/bin/env bash
# Updates an installed board from an unpacked release tarball (or a copy of
# the repo), then runs it once. Put the new release (and optionally a new
# config.toml / secrets.env) on the Pi, then from its folder:
#   sudo ./deploy/pi/update.sh [--config FILE] [--secrets FILE]
# A --secrets file is moved into place and the copy shredded.
# deploy/pi/upgrade.sh VERSION downloads and checks a release, then runs this.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX=/opt/departureboard
CONF=/etc/departureboard
config="" secrets=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config="$2"; shift 2 ;;
    --secrets) secrets="$2"; shift 2 ;;
    *) echo "Unknown option $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$config" ]]; then
  install -m 644 "$config" "$CONF/config.toml"
fi
if [[ -n "$secrets" ]]; then
  install -m 600 -o root -g root "$secrets" "$CONF/secrets.env"
  shred -u "$secrets"
fi

# The web server's network guard needs nft, where its unit runs it (a package
# removed but not purged still passes dpkg -s). The package lists are
# refreshed first, as an installed Pi's may name versions long gone from the
# mirror. Installed before the new code goes in, so if it fails the old code
# still runs. An if, not || { a && b; }: set -e doesn't stop on a failing
# apt-get update inside an && list.
if [[ ! -x /usr/sbin/nft ]]; then
  apt-get update
  apt-get install -y nftables
fi

# Ask the code installed, as the services' user, about the settings in force:
# config.toml with those saved from the settings site over it. No keys are
# read. Until the new code goes in below, that is the old code.
query() {
  runuser -u departureboard -- "$PREFIX/venv/bin/departureboard" query "$1" --install pi --config "$CONF/config.toml"
}
# Switched to the HDMI screen since installing? It needs pygame, looked for
# where Debian's python3-pygame puts it, and installed as nftables is above.
# (Settings that can't be read say why here; the board says so too.)
display="$(query display)" || display=""
if [[ "$display" == hdmi-* && ! -f /usr/lib/python3/dist-packages/pygame/__init__.py ]]; then
  apt-get update
  apt-get install -y python3-pygame
fi

# pip takes the departureboard command away for a moment while it puts new
# code in, so a tick the timer starts then fails: the board's timer is held
# (and a tick under way stopped; it saves what it has) until the new code and
# units are in, and started again however this ends, if it was running.
timer_was_running=false
if systemctl is-active --quiet departureboard.timer; then
  timer_was_running=true
  systemctl stop departureboard.timer departureboard.service
fi
build=""
trap 'if [[ -n "$build" ]]; then rm -rf "$build"; fi; if $timer_was_running; then systemctl start departureboard.timer; fi' EXIT

# inky and its helper, at the versions and hashes this release names (a new
# release may pin new ones), then the same check as install.sh that they and
# their Debian dependencies import, all before the new code goes in.
"$PREFIX/venv/bin/pip" install --quiet --require-hashes --no-deps -r "$REPO/deploy/pi/requirements-pi.txt"
"$PREFIX/venv/bin/python" -c "import inky, PIL, numpy, smbus2, spidev, gpiod" \
  || { echo "A dependency is missing; see the error above" >&2; exit 1; }

# Build from a clean copy: pip builds in the source tree, and as root that
# leaves root-owned build/ and *.egg-info behind (and a stale build/ can leak
# old files into the next install).
build="$(mktemp -d)"
cp -a "$REPO/." "$build/"
rm -rf "$build/build" "$build"/src/*.egg-info

# The code only: its dependencies are in the venv now. It is built with the
# hashed setuptools, not one fetched for the build. Then the rest of
# install.sh's check: the new code imports.
"$PREFIX/venv/bin/pip" install --quiet --require-hashes --no-deps -r "$REPO/deploy/pi/requirements-build.txt"
"$PREFIX/venv/bin/pip" install --quiet --no-deps --no-build-isolation --force-reinstall "$build"
"$PREFIX/venv/bin/python" -c "import departureboard.cli.main" \
  || { echo "A dependency is missing; see the error above" >&2; exit 1; }

# The setup service is now the fetch service, which also runs the
# dashboard's feeds: the old units go.
systemctl disable --now departureboard-setup.path departureboard-setup.service 2>/dev/null || true
rm -f /etc/systemd/system/departureboard-setup.service /etc/systemd/system/departureboard-setup.path
for unit in departureboard.service departureboard.timer departureboard-screen.service departureboard-web.service \
    departureboard-timetable.service departureboard-timetable.timer \
    departureboard-fetch.service departureboard-fetch.path; do
  install -m 644 "$REPO/deploy/pi/$unit" /etc/systemd/system/
done
# The web server's network guard, which its unit loads as root: root's alone to change.
install -m 644 -o root -g root "$REPO/deploy/pi/departureboard-web.nft" "$PREFIX/departureboard-web.nft"
systemctl daemon-reload
# The web server hands its internet work to the fetch service, started at
# boot and by the .path unit when asked. Both restart onto the new code and
# the spool folder in memory; then the old spool folders, in memory and on
# the SD card, go.
systemctl enable departureboard-fetch.path departureboard-fetch.service
systemctl restart departureboard-fetch.path departureboard-fetch.service
systemctl try-restart departureboard-web.service
rm -rf /run/departureboard-setup /var/lib/departureboard/setup-spool
if systemctl is-active --quiet departureboard-web.service && ! nft list table inet departureboard_web &>/dev/null; then
  echo "Warning: the web server's network guard didn't load, so nothing stops it connecting out." >&2
  echo "It still turns away visitors outside your home network. Why: journalctl -u departureboard-web" >&2
fi

# Bus times from the BODS timetable need it built and kept fresh. The first
# build takes minutes, so it carries on in the background.
if query needs-timetable >/dev/null; then
  systemctl enable --now departureboard-timetable.timer
  systemctl start --no-block departureboard-timetable.service
  echo "Building the bus timetable; follow it with: journalctl -u departureboard-timetable -f"
fi

# A screen's service runs for good, so it needs restarting onto the new code;
# try-restart leaves a stopped one stopped. Then one e-paper run now, rather
# than waiting for the timer (a oneshot: this waits for it). Whether the
# board runs at all is yours: this never enables or disables its timer or
# the screen's service.
systemctl try-restart departureboard-screen.service
if systemctl is-enabled --quiet departureboard.timer; then
  systemctl start departureboard.service || true
  journalctl -u departureboard.service -n 12 --no-pager -o cat
fi

# The web server moved from 8080 in this version, and nothing listens there
# now: say so, last, where it is seen.
echo
echo "The settings site moved to :8090/settings: http://$(hostname).local:8090/settings"

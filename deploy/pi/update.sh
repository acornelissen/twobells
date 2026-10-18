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
PREFIX=/opt/twobells
CONF=/etc/twobells
config="" secrets=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config="$2"; shift 2 ;;
    --secrets) secrets="$2"; shift 2 ;;
    *) echo "Unknown option $1" >&2; exit 2 ;;
  esac
done

# Only an installed board is updated: on a Pi without the app, stop before
# writing anything or installing packages.
if [[ ! -x "$PREFIX/venv/bin/twobells" ]]; then
  echo "There's no install in $PREFIX to update. To install, run sudo ./deploy/pi/install.sh" >&2
  exit 1
fi

if [[ -n "$config" ]]; then
  install -m 644 "$config" "$CONF/config.toml"
fi
if [[ -n "$secrets" ]]; then
  install -m 600 -o root -g root "$secrets" "$CONF/secrets.env"
  shred -u "$secrets"
fi

# The web server's network guard needs nft, where twobells-guard.service
# runs it (a package removed but not purged still passes dpkg -s). The
# package lists are refreshed first, as an installed Pi's may name versions
# long gone from the mirror. Installed before the new code goes in, so if it fails the old code
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
  runuser -u twobells -- "$PREFIX/venv/bin/twobells" query "$1" --install pi --config "$CONF/config.toml"
}
# Switched to the HDMI screen since installing? It needs pygame, looked for
# where Debian's python3-pygame puts it, and installed as nftables is above.
# (Settings that can't be read say why here; the board says so too.)
display="$(query display)" || display=""
if [[ "$display" == hdmi-* && ! -f /usr/lib/python3/dist-packages/pygame/__init__.py ]]; then
  apt-get update
  apt-get install -y python3-pygame
fi
# And espeak-ng, for the screen's station announcements: a screen installed
# before them hasn't got it.
if [[ "$display" == hdmi-* && ! -x /usr/bin/espeak-ng ]]; then
  apt-get update
  apt-get install -y espeak-ng
fi
# And EGL and GLES, which SDL opens the screen through: python3-pygame doesn't
# pull them in, and Raspberry Pi OS Lite on Trixie comes without them. Looked
# for in the linker's cache, not asked of dpkg.
libraries="$(ldconfig -p)"
if [[ "$display" == hdmi-* && ( "$libraries" != *libEGL.so.1* || "$libraries" != *libEGL_mesa.so.0* \
    || "$libraries" != *libGLESv2.so.2* ) ]]; then
  apt-get update
  apt-get install -y libegl1 libegl-mesa0 libgles2
fi

# pip takes the twobells command away for a moment while it puts new
# code in, so a tick the timer starts then fails: the board's timer is held
# (and a tick under way stopped; it saves what it has) until the new code and
# units are in, and started again however this ends, if it was running.
timer_was_running=false
if systemctl is-active --quiet twobells.timer; then
  timer_was_running=true
  systemctl stop twobells.timer twobells.service
fi
build=""
trap 'if [[ -n "$build" ]]; then rm -rf "$build"; fi; if $timer_was_running; then systemctl start twobells.timer; fi' EXIT

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
"$PREFIX/venv/bin/python" -c "import twobells.cli.main" \
  || { echo "A dependency is missing; see the error above" >&2; exit 1; }

for unit in twobells.service twobells.timer twobells-screen.service twobells-web.service \
    twobells-guard.service twobells-timetable.service twobells-timetable.timer \
    twobells-fetch.service twobells-fetch.path; do
  install -m 644 "$REPO/deploy/pi/$unit" /etc/systemd/system/
done
# The web server's network guard, which twobells-guard.service loads as
# root: root's alone to change.
install -m 644 -o root -g root "$REPO/deploy/pi/twobells-web.nft" "$PREFIX/twobells-web.nft"
systemctl daemon-reload
# The guard loads at boot, before the web server. Loaded afresh now, which
# empties its set, so a running web server restarts after it, below, and
# systemd puts it in the set again. If it fails, the rest still goes in and
# the end says so.
systemctl enable twobells-guard.service
systemctl restart twobells-guard.service || true
# The web server hands its internet work to the fetch service, started at
# boot and by the .path unit when asked. Both restart onto the new code.
systemctl enable twobells-fetch.path twobells-fetch.service
systemctl restart twobells-fetch.path twobells-fetch.service
systemctl try-restart twobells-web.service
# Says if the network guard isn't holding the web server: that needs its
# table in and the server's cgroup in its set, which systemd fills as the
# server starts (NFTSet=, systemd 254 or later). A warning, never a stop:
# the board and the dashboard work without it.
say_if_unguarded() {
  local listed version
  systemctl is-active --quiet twobells-web.service || return 0
  listed="$(nft list set inet twobells_web web 2>/dev/null)" || listed=""
  if [[ "$listed" == *"elements = "* ]]; then
    return 0
  fi
  echo "Warning: the web server's network guard isn't holding it. Only systemd's own firewall, which" >&2
  echo "a Pi Zero or Pi 1 can't apply, stops it connecting out; the settings' Overview says whether" >&2
  echo "anything does. It still turns away visitors outside your home network." >&2
  version="$(systemctl --version | awk 'NR == 1 { print $2 }')" || version=""
  if [[ "$version" =~ ^[0-9]+$ ]] && (( version < 254 )); then
    echo "This systemd ($version) can't put the web server in the guard: that needs 254 or later, as in" >&2
    echo "Raspberry Pi OS Trixie. The board and the dashboard work all the same." >&2
  else
    echo "Why: journalctl -u twobells-guard -u twobells-web" >&2
  fi
}
say_if_unguarded

# Bus times from the BODS timetable, and trams from TfGM's, need it built and
# kept fresh. The timer is on whatever the settings, so stops set up later are
# built within the hour (an older install turned it on only for those set up
# then). When they want one, a build starts now; the first takes minutes, so
# it carries on in the background.
systemctl enable --now twobells-timetable.timer
if query needs-timetable >/dev/null; then
  systemctl start --no-block twobells-timetable.service
  echo "Building the timetables; follow it with: journalctl -u twobells-timetable -f"
fi

# A screen's service runs for good, so it needs restarting onto the new code;
# try-restart leaves a stopped one stopped. Then one e-paper run now, rather
# than waiting for the timer (a oneshot: this waits for it). Whether the
# board runs at all is yours: this never enables or disables its timer or
# the screen's service.
systemctl try-restart twobells-screen.service
if systemctl is-enabled --quiet twobells.timer; then
  systemctl start twobells.service || true
  journalctl -u twobells.service -n 12 --no-pager -o cat
fi

echo
echo "The settings site: http://$(hostname).local:8090/settings"

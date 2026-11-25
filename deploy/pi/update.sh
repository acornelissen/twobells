#!/usr/bin/env bash
# Updates an installed board from an unpacked release tarball (or a copy of
# the repo), then runs it once. Put the new release (and optionally a new
# config.toml / secrets.env) on the Pi, then from its folder:
#   sudo ./deploy/pi/update.sh [--config FILE] [--secrets FILE]
# A --secrets file is copied into place and the given file deleted with shred
# -u (on an SD card that may not reach its old contents: see install-pi.md).
# deploy/pi/upgrade.sh VERSION downloads and checks a release, then runs this.
# The release gets a venv of its own (releases.sh), built while the old one
# runs. The services move to it once it imports, and back to the old one if
# they don't stay up on it.
set -euo pipefail
# Whatever umask sudo passed on: the services' user must read the new venv.
umask 022

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX=/opt/twobells
CONF=/etc/twobells
# The units this installs, and puts back if the services don't stay up.
UNITS=(twobells.service twobells.timer twobells-screen.service twobells-web.service
  twobells-guard.service twobells-timetable.service twobells-timetable.timer twobells-timetable.path
  twobells-fetch.service twobells-fetch.path)
# How long the services must stay up on the new release, in seconds: a Pi
# Zero takes a while to start the app, and a crash at start comes inside it.
SETTLE=30
config="" secrets=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config="$2"; shift 2 ;;
    --secrets) secrets="$2"; shift 2 ;;
    *) echo "Unknown option $1" >&2; exit 2 ;;
  esac
done

# The release folders; and a power cut part way through an update may have
# left the venv link out, so that goes back first.
# shellcheck source=deploy/pi/releases.sh
source "$REPO/deploy/pi/releases.sh"
recover

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

# The new release, in a venv of its own beside the one in use, which nothing
# here changes: the board's timer and the services carry on meanwhile. If
# this stops before switching to it (a check fails, say), it is removed, and
# the old code runs on as it was.
release="" build="" saved=""
finish() {
  if [[ -n "$build" ]]; then rm -rf "$build"; fi
  if [[ -n "$saved" ]]; then rm -rf "$saved"; fi
  discard "$release"
}
trap finish EXIT
read_version
prune
new_release
build_release "$release"

# What the services run with now, to put back if they don't stay up on the
# new release: the units and the guard's rules, the release before (switch_to
# moves previous on), and which of the long-running services are up.
saved="$(mktemp -d)"
for unit in "${UNITS[@]}"; do
  if [[ -f "/etc/systemd/system/$unit" ]]; then
    cp -p "/etc/systemd/system/$unit" "$saved/"
  fi
done
if [[ -f "$PREFIX/twobells-web.nft" ]]; then
  cp -p "$PREFIX/twobells-web.nft" "$saved/"
fi
before="$(linked previous)"
# The fetch service is started below whatever it was; the others only if running.
watched=(twobells-fetch.service)
for unit in twobells-web.service twobells-screen.service; do
  if systemctl is-active --quiet "$unit"; then
    watched+=("$unit")
  fi
done

# The new code goes in: the venv link moves to it in one step, so each tick
# of the board runs one release or the other, whole.
switch_to "$release"

for unit in "${UNITS[@]}"; do
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
# A screen's service runs for good, so it needs restarting onto the new code;
# try-restart leaves a stopped one stopped.
systemctl try-restart twobells-screen.service

# Whether each service named stays up for SETTLE seconds: none of them
# fails, or waits to start again after a crash. Looked at every second.
stay_up() {
  local second unit active sub
  for (( second = 0; second < SETTLE; second++ )); do
    sleep 1
    for unit in "$@"; do
      active="$(systemctl show --property=ActiveState --value "$unit")"
      sub="$(systemctl show --property=SubState --value "$unit")"
      if [[ "$active" == failed || "$sub" == auto-restart* ]]; then
        echo "$unit stopped on the new release; why: journalctl -u ${unit%.service} -n 30" >&2
        return 1
      fi
    done
  done
}
# Back to the release from before, with the units and guard's rules from
# before, and the services restarted onto them as they were.
go_back() {
  local unit
  if ! switch_back "$release" "$before"; then
    echo "There's no release before it to go back to, so the new one stays." >&2
    return 0
  fi
  for unit in "${UNITS[@]}"; do
    if [[ -f "$saved/$unit" ]]; then
      install -m 644 "$saved/$unit" /etc/systemd/system/
    fi
  done
  if [[ -f "$saved/twobells-web.nft" ]]; then
    install -m 644 -o root -g root "$saved/twobells-web.nft" "$PREFIX/twobells-web.nft"
  fi
  systemctl daemon-reload
  systemctl restart twobells-guard.service || true
  systemctl restart twobells-fetch.path "${watched[@]}"
  echo "So the update was undone: it is back on the release from before ($(linked venv))." >&2
}
echo "Checking the services stay up on the new release ($SETTLE seconds)"
if ! stay_up "${watched[@]}"; then
  go_back
  exit 1
fi

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
# built (an older install turned it on only for those set up then): it checks
# hourly, and the .path unit as soon as the settings are saved. When they want
# one, a build starts now; the first takes minutes, so it carries on in the
# background.
systemctl enable --now twobells-timetable.timer
systemctl enable --now twobells-timetable.path
if query needs-timetable >/dev/null; then
  systemctl start --no-block twobells-timetable.service
  echo "Building the timetables; follow it with: journalctl -u twobells-timetable -f"
fi

# One e-paper run now, rather than waiting for the timer (a oneshot: this
# waits for it). Whether the board runs at all is yours: this never enables
# or disables its timer or the screen's service.
if systemctl is-enabled --quiet twobells.timer; then
  systemctl start twobells.service || true
  journalctl -u twobells.service -n 12 --no-pager -o cat
fi

# The release before this one is kept, to go back to; any older are removed.
prune
echo
echo "Updated to $version; the release before it is kept in $PREFIX/previous (see install-pi.md)."
echo "The settings site: http://$(hostname).local:8090/settings"

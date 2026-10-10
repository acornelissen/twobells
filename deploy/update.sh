#!/usr/bin/env bash
# Updates an installed board from a copy of the repo, then runs it once.
# Copy the repo (and optionally a new config.toml / secrets.env) next to it,
# then from the repo root:
#   sudo ./deploy/update.sh [--config FILE] [--secrets FILE]
# A --secrets file is moved into place and the copy shredded.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
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

# Build from a clean copy: pip builds in the source tree, and as root that
# leaves root-owned build/ and *.egg-info behind (and a stale build/ can leak
# old files into the next install).
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
cp -a "$REPO/." "$build/"
rm -rf "$build/build" "$build"/src/*.egg-info
# Switched to the HDMI screen since installing? It needs pygame.
if grep -Eq '^[[:space:]]*display[[:space:]]*=[[:space:]]*"hdmi-' "$CONF/config.toml" \
    && ! dpkg -s python3-pygame &>/dev/null; then
  apt-get install -y python3-pygame
fi

# The code only: its dependencies are already in the venv.
"$PREFIX/venv/bin/pip" install --quiet --no-deps --force-reinstall "$build"

# The setup service is now the fetch service, which also runs the
# dashboard's feeds: the old units go.
systemctl disable --now departureboard-setup.path departureboard-setup.service 2>/dev/null || true
rm -f /etc/systemd/system/departureboard-setup.service /etc/systemd/system/departureboard-setup.path
for unit in departureboard.service departureboard.timer departureboard-screen.service departureboard-web.service \
    departureboard-timetable.service departureboard-timetable.timer \
    departureboard-fetch.service departureboard-fetch.path; do
  install -m 644 "$REPO/deploy/$unit" /etc/systemd/system/
done
systemctl daemon-reload
# The settings page hands its internet work to the fetch service, started at
# boot and by the .path unit when asked. Both restart onto the new code and
# the spool folder in memory (a running page signs browsers out); then the
# old spool folders, in memory and on the SD card, go.
systemctl enable departureboard-fetch.path departureboard-fetch.service
systemctl restart departureboard-fetch.path departureboard-fetch.service
systemctl try-restart departureboard-web.service
rm -rf /run/departureboard-setup /var/lib/departureboard/setup-spool

# Bus times from the BODS timetable need it built and kept fresh. The board
# says whether its buses are on and use it, from config.toml with the
# settings saved from the page over it. The first build takes minutes, so it
# carries on in the background.
if runuser -u departureboard -- "$PREFIX/venv/bin/departureboard" timetable --needed --config "$CONF/config.toml"; then
  systemctl enable --now departureboard-timetable.timer
  systemctl start --no-block departureboard-timetable.service
  echo "Building the bus timetable; follow it with: journalctl -u departureboard-timetable -f"
fi

# One run now, rather than waiting for the timer (a oneshot: this waits for it).
if systemctl is-enabled --quiet departureboard.timer; then
  systemctl start departureboard.service || true
  journalctl -u departureboard.service -n 12 --no-pager -o cat
fi

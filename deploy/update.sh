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

for unit in departureboard.service departureboard.timer departureboard-screen.service departureboard-web.service; do
  install -m 644 "$REPO/deploy/$unit" /etc/systemd/system/
done
systemctl daemon-reload

# One run now, rather than waiting for the timer (a oneshot: this waits for it).
if systemctl is-enabled --quiet departureboard.timer; then
  systemctl start departureboard.service || true
  journalctl -u departureboard.service -n 12 --no-pager -o cat
fi

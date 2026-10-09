#!/usr/bin/env bash
# Installs the board on Raspberry Pi OS (Bookworm or later). Run from the repo root:
#   sudo ./deploy/install.sh [--screen]
# --screen also installs pygame for the HDMI screen; that happens anyway when
# /etc/departureboard/config.toml already says display = "hdmi-...".
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/departureboard
CONF=/etc/departureboard

screen=false
[[ "${1:-}" == "--screen" ]] && screen=true
grep -Eq '^[[:space:]]*display[[:space:]]*=[[:space:]]*"hdmi-' "$CONF/config.toml" 2>/dev/null && screen=true

apt-get update
# spidev, gpiozero and lgpio drive the Waveshare panel; the venv sees them via system site packages.
# Pillow, numpy, smbus2 and gpiod (for inky) come from Debian rather than pip: built for the
# Pi, patched through apt, and seen by the venv through system site packages.
apt-get install -y python3-venv python3-dev fonts-dejavu-core python3-spidev python3-gpiozero python3-lgpio python3-libgpiod python3-numpy python3-smbus2 python3-pil
# pygame, only for the HDMI screen: it pulls in graphics libraries (LLVM, Mesa) and a
# MIDI soundfont, slow to install on a Pi Zero and unused by e-paper.
if $screen; then
  apt-get install -y python3-pygame
fi

# The panels need SPI; the Inky also needs I2C (the I2C bus holds the board's EEPROM for auto-detect).
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0

id departureboard &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin departureboard
# State, saved settings and the settings page password live here.
install -d -o departureboard -g departureboard -m 750 /var/lib/departureboard

python3 -m venv --system-site-packages "$PREFIX/venv"
# Pinned versions, not the newest of each that day. Debian's own pip is used as
# is: upgrading it would fetch and run an unpinned package as root.
# Build from a clean copy: pip builds in the source tree, and as root that
# leaves root-owned build/ and *.egg-info behind (and a stale build/ can leak
# old files into the next install).
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
cp -a "$REPO/." "$build/"
rm -rf "$build/build" "$build"/src/*.egg-info
# From PyPI only inky and its helper, at exact versions with their hashes; their
# dependencies are the Debian packages above. (Not pip check: with system site
# packages it also judges Debian's own, some of which it reports as broken.)
"$PREFIX/venv/bin/pip" install --require-hashes --no-deps -r "$REPO/deploy/requirements-pi.txt"
"$PREFIX/venv/bin/pip" install --no-deps "$build"
"$PREFIX/venv/bin/python" -c "import departureboard.cli, inky, PIL, numpy, smbus2, spidev, gpiod" \
  || { echo "A dependency is missing; see the error above" >&2; exit 1; }

install -d -m 755 "$CONF"
[[ -f "$CONF/config.toml" ]] || install -m 644 "$REPO/config.example.toml" "$CONF/config.toml"
[[ -f "$CONF/secrets.env" ]] || install -m 600 "$REPO/deploy/secrets.env.example" "$CONF/secrets.env"

install -m 644 "$REPO/deploy/departureboard.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard.timer" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard-screen.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard-web.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard-timetable.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard-timetable.timer" /etc/systemd/system/
systemctl daemon-reload

cat <<EOF

Installed. Next:
  1. Edit $CONF/secrets.env (keys) and $CONF/config.toml (replace each CHANGE_ME:
     your station code and bus stop codes; $REPO/examples/glossop.toml is a filled-in one).
  2. Check the keys:   sudo $PREFIX/venv/bin/departureboard probe train
                       sudo $PREFIX/venv/bin/departureboard probe bus
  3. Start the board:  sudo systemctl enable --now departureboard.timer          (e-paper)
                  or:  sudo systemctl enable --now departureboard-screen.service (HDMI screen)
  4. Watch it:         journalctl -u departureboard -f
     With source = "bods" under [bus], build the bus timetable and keep it fresh:
       sudo systemctl enable --now departureboard-timetable.timer
       sudo systemctl start departureboard-timetable.service   (the first build, now)
  5. Settings page (optional, home network only):
       sudo -u departureboard $PREFIX/venv/bin/departureboard web --set-password
       sudo systemctl enable --now departureboard-web.service
     then open http://$(hostname).local:8080/

If the display errors about chip select, add  dtoverlay=spi0-0cs
to /boot/firmware/config.txt and reboot. The Waveshare 10.85" panel always
needs it: the board drives its two chip selects itself.
EOF

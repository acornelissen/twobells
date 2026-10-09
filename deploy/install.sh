#!/usr/bin/env bash
# Installs the board on Raspberry Pi OS (Bookworm or later). Run from the repo root:
#   sudo ./deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/departureboard
CONF=/etc/departureboard

apt-get update
# spidev, gpiozero and lgpio drive the Waveshare panel; the venv sees them via system site packages.
# pygame draws on an HDMI screen.
apt-get install -y python3-venv python3-dev fonts-dejavu-core python3-spidev python3-gpiozero python3-lgpio python3-pygame

# The panels need SPI; the Inky also needs I2C (the I2C bus holds the board's EEPROM for auto-detect).
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0

id departureboard &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin departureboard

python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install "$REPO[pi]"

install -d -m 755 "$CONF"
[[ -f "$CONF/config.toml" ]] || install -m 644 "$REPO/config.example.toml" "$CONF/config.toml"
[[ -f "$CONF/secrets.env" ]] || install -m 600 "$REPO/deploy/secrets.env.example" "$CONF/secrets.env"

install -m 644 "$REPO/deploy/departureboard.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard.timer" /etc/systemd/system/
install -m 644 "$REPO/deploy/departureboard-screen.service" /etc/systemd/system/
systemctl daemon-reload

cat <<EOF

Installed. Next:
  1. Edit $CONF/secrets.env (keys) and $CONF/config.toml (bus stop ATCO code).
  2. Check the keys:   sudo $PREFIX/venv/bin/departureboard probe train
                       sudo $PREFIX/venv/bin/departureboard probe bus
  3. Start the board:  sudo systemctl enable --now departureboard.timer          (e-paper)
                  or:  sudo systemctl enable --now departureboard-screen.service (HDMI screen)
  4. Watch it:         journalctl -u departureboard -f

If the display errors about chip select, add  dtoverlay=spi0-0cs
to /boot/firmware/config.txt and reboot. The Waveshare 10.85" panel always
needs it: the board drives its two chip selects itself.
EOF

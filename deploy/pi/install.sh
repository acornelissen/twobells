#!/usr/bin/env bash
# Installs the board on Raspberry Pi OS (Bookworm or later). Run it from the
# unpacked release tarball (or a copy of the repo):
#   sudo ./deploy/pi/install.sh [--screen]
# --screen also installs pygame for the HDMI screen; that happens anyway when
# /etc/departureboard/config.toml already says display = "hdmi-...".
# It starts the settings page and the fetch service, and says how to start
# the board itself.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX=/opt/departureboard
CONF=/etc/departureboard

screen=false
[[ "${1:-}" == "--screen" ]] && screen=true

apt-get update
# spidev, gpiozero and lgpio drive the Waveshare panel; the venv sees them via system site packages.
# Pillow, numpy, smbus2 and gpiod (for inky) come from Debian rather than pip: built for the
# Pi, patched through apt, and seen by the venv through system site packages.
apt-get install -y python3-venv python3-dev fonts-dejavu-core python3-spidev python3-gpiozero python3-lgpio python3-libgpiod python3-numpy python3-smbus2 python3-pil

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
"$PREFIX/venv/bin/pip" install --require-hashes --no-deps -r "$REPO/deploy/pi/requirements-pi.txt"
# The code is built with the hashed setuptools, not one fetched for the build.
"$PREFIX/venv/bin/pip" install --require-hashes --no-deps -r "$REPO/deploy/pi/requirements-build.txt"
"$PREFIX/venv/bin/pip" install --no-deps --no-build-isolation "$build"
"$PREFIX/venv/bin/python" -c "import departureboard.cli.main, inky, PIL, numpy, smbus2, spidev, gpiod" \
  || { echo "A dependency is missing; see the error above" >&2; exit 1; }

install -d -m 755 "$CONF"
[[ -f "$CONF/config.toml" ]] || install -m 644 "$REPO/examples/config.example.toml" "$CONF/config.toml"
[[ -f "$CONF/secrets.env" ]] || install -m 600 "$REPO/deploy/secrets.env.example" "$CONF/secrets.env"

# Ask the app, as the services' user, about the settings in force: config.toml
# with those saved from the settings page over it. No keys are read.
query() {
  runuser -u departureboard -- "$PREFIX/venv/bin/departureboard" query "$1" --install pi --config "$CONF/config.toml"
}
if ! display="$(query display)" || ! views="$(query views)"; then
  echo "Fix the settings named above, then run this again" >&2
  exit 1
fi
[[ "$display" == hdmi-* ]] && screen=true
# pygame, only for the HDMI screen: it pulls in graphics libraries (LLVM, Mesa) and a
# MIDI soundfont, slow to install on a Pi Zero and unused by e-paper.
if $screen; then
  apt-get install -y python3-pygame
fi

for unit in departureboard.service departureboard.timer departureboard-screen.service departureboard-web.service \
    departureboard-timetable.service departureboard-timetable.timer \
    departureboard-fetch.service departureboard-fetch.path; do
  install -m 644 "$REPO/deploy/pi/$unit" /etc/systemd/system/
done
systemctl daemon-reload
# The settings page can't reach the internet; the fetch service does its
# internet work (the wizard's lookups, the dashboard's feeds). It starts at
# boot, and the .path unit starts it whenever the page leaves it a request.
systemctl enable --now departureboard-fetch.path departureboard-fetch.service
systemctl enable --now departureboard-web.service
# Bus times from the BODS timetable need it built and kept fresh.
timetable=false
if query needs-timetable >/dev/null; then
  systemctl enable --now departureboard-timetable.timer
  timetable=true
fi

# The board itself is yours to start, so it is only named here: the e-paper
# timer, or the screen's service, by the display config.toml sets.
if [[ "$display" == hdmi-* ]]; then
  renderer=departureboard-screen.service
else
  renderer=departureboard.timer
fi

echo
echo "Installed. The settings page and the fetch service are running. Next:"
if grep -qx board <<<"$views"; then
  cat <<EOF
  1. Start the board:
       sudo systemctl enable --now $renderer
     Until it is set up, it shows "Set me up" and the settings page's address.
EOF
else
  cat <<EOF
  1. The board view is off, so the board has nothing to show. Once it is on
     in the settings, start it with:
       sudo systemctl enable --now $renderer
EOF
fi
cat <<EOF
  2. Open the settings page: http://$(hostname).local:8090/settings on a phone or
     computer on your home network. Claim it with the code the board shows (or
     set the password here first:
       sudo -u departureboard $PREFIX/venv/bin/departureboard serve --install pi --set-password )
     and it opens the setup wizard: home, station, bus stops, walk times and
     keys, saved for you. No need to edit config.toml by hand.
EOF
if $timetable; then
  echo "  3. The bus timetable is kept fresh by departureboard-timetable.timer."
else
  cat <<EOF
  3. If your buses come from the BODS timetable, keep it fresh (the wizard
     builds the first one):
       sudo systemctl enable --now departureboard-timetable.timer
EOF
fi
cat <<EOF
  4. Watch it:         journalctl -u ${renderer%.timer} -f
     Check the keys:   sudo $PREFIX/venv/bin/departureboard probe train
                       sudo $PREFIX/venv/bin/departureboard probe bus
EOF
if [[ "$renderer" == departureboard.timer ]]; then
  cat <<EOF

For the HDMI screen, set display = "hdmi-1280x400" in $CONF/config.toml and
run this again: it installs pygame and names the screen's service instead.
EOF
fi
cat <<EOF

To set it up by hand instead, edit $CONF/secrets.env (keys) and
$CONF/config.toml (replace each CHANGE_ME; $REPO/examples/glossop.toml is a
filled-in one).

If the display errors about chip select, add  dtoverlay=spi0-0cs
to /boot/firmware/config.txt and reboot. The Waveshare 10.85" panel always
needs it: the board drives its two chip selects itself.
EOF

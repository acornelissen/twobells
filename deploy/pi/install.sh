#!/usr/bin/env bash
# Installs the board on Raspberry Pi OS (Bookworm or later; Trixie on a Pi
# Zero or Pi 1, whose web server's network guard needs its newer systemd).
# Run it from the unpacked release tarball (or a copy of the repo):
#   sudo ./deploy/pi/install.sh [--screen]
# --screen also installs pygame for the HDMI screen; that happens anyway when
# /etc/twobells/config.toml already says display = "hdmi-...".
# It starts the web server (the settings site, and the dashboard when that
# view is on) and the fetch service, and says how to start the board itself.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX=/opt/twobells
CONF=/etc/twobells

screen=false
[[ "${1:-}" == "--screen" ]] && screen=true

apt-get update
# spidev, gpiozero and lgpio drive the Waveshare panel; the venv sees them via system site packages.
# Pillow, numpy, smbus2 and gpiod (for inky) come from Debian rather than pip: built for the
# Pi, patched through apt, and seen by the venv through system site packages.
# nftables loads the web server's network guard (twobells-web.nft).
apt-get install -y python3-venv python3-dev python3-spidev python3-gpiozero python3-lgpio python3-libgpiod python3-numpy python3-smbus2 python3-pil nftables

# The panels need SPI; the Inky also needs I2C (the I2C bus holds the board's EEPROM for auto-detect).
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0

id twobells &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin twobells
# State, saved settings and the settings password live here.
install -d -o twobells -g twobells -m 750 /var/lib/twobells

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
"$PREFIX/venv/bin/python" -c "import twobells.cli.main, inky, PIL, numpy, smbus2, spidev, gpiod" \
  || { echo "A dependency is missing; see the error above" >&2; exit 1; }

install -d -m 755 "$CONF"
[[ -f "$CONF/config.toml" ]] || install -m 644 "$REPO/examples/config.example.toml" "$CONF/config.toml"
[[ -f "$CONF/secrets.env" ]] || install -m 600 "$REPO/deploy/secrets.env.example" "$CONF/secrets.env"

# Ask the app, as the services' user, about the settings in force: config.toml
# with those saved from the settings site over it. No keys are read.
query() {
  runuser -u twobells -- "$PREFIX/venv/bin/twobells" query "$1" --install pi --config "$CONF/config.toml"
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

# Run again, the web server may be running already on the old unit, and
# enable --now leaves it so: it restarts below, onto the new unit, after its
# network guard loads afresh. The fetch service restarts too if its unit
# changed. Each as it was before this run; try-restart never starts one.
restart_web=false
systemctl is-active --quiet twobells-web.service && restart_web=true
restart_fetch=false
if systemctl is-active --quiet twobells-fetch.service \
    && ! cmp -s "$REPO/deploy/pi/twobells-fetch.service" /etc/systemd/system/twobells-fetch.service; then
  restart_fetch=true
fi
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
# The web server can't reach the internet; the fetch service does its
# internet work (the wizard's lookups, the dashboard's feeds). It starts at
# boot, and the .path unit starts it whenever the server leaves it a request.
systemctl enable --now twobells-fetch.path twobells-fetch.service
if $restart_fetch; then
  systemctl try-restart twobells-fetch.service
fi
systemctl enable --now twobells-web.service
if $restart_web; then
  systemctl try-restart twobells-web.service
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
# Bus times from the BODS timetable need it built and kept fresh.
timetable=false
if query needs-timetable >/dev/null; then
  systemctl enable --now twobells-timetable.timer
  timetable=true
fi

# The board itself is yours to start, so it is only named here: the e-paper
# timer, or the screen's service, by the display config.toml sets.
if [[ "$display" == hdmi-* ]]; then
  renderer=twobells-screen.service
else
  renderer=twobells.timer
fi

echo
echo "Installed. The web server and the fetch service are running. Next:"
if grep -qx board <<<"$views"; then
  cat <<EOF
  1. Start the board:
       sudo systemctl enable --now $renderer
     Until it is set up, it shows "Set me up" and the settings site's address.
EOF
else
  cat <<EOF
  1. The board view is off, so the board has nothing to show. Once it is on
     in the settings, start it with:
       sudo systemctl enable --now $renderer
EOF
fi
cat <<EOF
  2. Open the settings site: http://$(hostname).local:8090/settings on a phone or
     computer on your home network. Claim it with the code the board shows (or
     set the password here first:
       sudo -u twobells $PREFIX/venv/bin/twobells serve --install pi --set-password )
     and it opens the setup wizard: home, station, bus stops, walk times and
     keys, saved for you. No need to edit config.toml by hand.
EOF
if $timetable; then
  echo "  3. The bus timetable is kept fresh by twobells-timetable.timer."
else
  cat <<EOF
  3. If your buses come from the BODS timetable, keep it fresh (the wizard
     builds the first one):
       sudo systemctl enable --now twobells-timetable.timer
EOF
fi
cat <<EOF
  4. Watch it:         journalctl -u ${renderer%.timer} -f
     Check the keys:   sudo $PREFIX/venv/bin/twobells probe train
                       sudo $PREFIX/venv/bin/twobells probe bus
EOF
if [[ "$renderer" == twobells.timer ]]; then
  cat <<EOF

For an HDMI screen, set display = "hdmi-1280x400" or "hdmi-1920x440" in
$CONF/config.toml and run this again: it installs pygame and names the
screen's service instead.
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

# Removing Two Bells

How to take Two Bells off a Pi, out of Docker, or off a desktop. To keep your settings, keys and history for later, [back them up](backup.md) first. [Install on a Raspberry Pi](install-pi.md), [Docker](docker.md) and [Desktop](desktop.md) say what was put there.

- [Before you start](#before-you-start)
- [On a Pi](#on-a-pi)
- [In Docker](#in-docker)
- [On a desktop](#on-a-desktop)

## Before you start

Each install keeps everything it saves in one folder, its [store](configuration.md#where-things-are-kept). The store holds your keys (`secrets.env`) and the settings password's hash (`web.json`), so delete it unless you are [moving it](backup.md) somewhere else. Deleting a file doesn't wipe it from an SD card or a disk, so if you are giving the Pi, the card or the NAS away, [replace your keys](keys.md#replacing-a-key) at each service too.

## On a Pi

1. Stop the services and turn them off. Stopping `twobells-guard.service` also takes the web server's network guard out of nftables.

   ```bash
   sudo systemctl disable --now twobells.timer twobells-screen.service twobells-web.service \
     twobells-fetch.path twobells-fetch.service twobells-timetable.timer twobells-timetable.path \
     twobells-guard.service
   sudo systemctl stop twobells.service twobells-timetable.service
   ```

2. Remove the units:

   ```bash
   cd /etc/systemd/system
   sudo rm -f twobells.service twobells.timer twobells-screen.service twobells-web.service \
     twobells-guard.service twobells-fetch.service twobells-fetch.path \
     twobells-timetable.service twobells-timetable.timer twobells-timetable.path
   cd
   sudo systemctl daemon-reload
   sudo systemctl reset-failed
   ```

3. Check the network guard has gone. If this lists a table, delete it with `sudo nft delete table inet twobells_web`:

   ```bash
   sudo nft list table inet twobells_web
   ```

4. Remove the app, its settings, its store, the screen's cache and the folder it shares with the fetch service:

   ```bash
   sudo rm -rf /opt/twobells /etc/twobells /var/lib/twobells /var/cache/twobells-screen /run/twobells-fetch
   ```

   - `/opt/twobells`: the app and the network guard's rules.
   - `/etc/twobells`: `config.toml` and `secrets.env`, your keys if you kept them there.
   - `/var/lib/twobells`: the store.
   - `/var/cache/twobells-screen`: the HDMI screen's graphics cache, if it ran.
   - `/run/twobells-fetch`: in memory, and gone at the next restart anyway.

5. Remove the user the services ran as:

   ```bash
   sudo userdel twobells
   ```

   `getent group twobells` should then print nothing. If it prints a line, `sudo groupdel twobells`.

`install.sh` also installed some Debian packages (`python3-venv`, `nftables`, Pillow, numpy and the Pi's GPIO, SPI and I2C libraries, and for a screen `python3-pygame`, `espeak-ng` and the EGL and GLES libraries) and turned SPI and I2C on. Other software may use them, so leave them unless you know it doesn't. To turn SPI and I2C off again: `sudo raspi-config nonint do_spi 1` and `sudo raspi-config nonint do_i2c 1`. If you added `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` for the display, take that line out. If you installed cosign only for Two Bells, `sudo apt-get remove cosign`.

The services' logs stay in the system journal until it clears them out with age. They never hold a key.

Last, delete the release folder you installed from.

## In Docker

Your data is in the `twobells-data` volume, or the folder you gave the container at `/data`.

**With compose**, in the folder with the compose file (add `-f docker-compose.ghcr.yml` if that is its name):

```bash
docker compose down --volumes --rmi all
```

That removes the container, the `twobells-data` volume and the image. Then delete the folder: it may hold `secrets.env` and a `config.toml`.

**With `docker run`:**

```bash
docker rm -f twobells
docker volume rm twobells-data
docker image ls ghcr.io/acornelissen/twobells
```

Then `docker image rm` with each IMAGE ID it lists.

**A folder in place of the volume:** delete it as root, as it belongs to the container's user (uid 10001). On Unraid, after removing the container in the **Docker** tab:

```bash
rm -rf /mnt/user/appdata/twobells
rm /boot/config/plugins/dockerMan/templates-user/my-twobells.xml
```

On TrueNAS, delete the app in **Apps**, then the dataset if you made one.

**Podman** takes the same commands, with `podman` in place of `docker`. A folder you gave the container with `podman unshare chown` is deleted the same way: `podman unshare rm -rf ~/twobells-data`.

## On a desktop

**On a Mac:**

```bash
launchctl bootout "gui/$(id -u)/uk.twobells.serve"
rm -f ~/Library/LaunchAgents/uk.twobells.serve.plist ~/Library/Logs/twobells.log
uv tool uninstall twobells      # or: pipx uninstall twobells
rm -rf ~/Library/"Application Support"/twobells
```

If `launchctl` says it can't find the service, it wasn't started at login: carry on.

**On Linux:**

```bash
systemctl --user disable --now twobells.service
rm -f ~/.config/systemd/user/twobells.service
systemctl --user daemon-reload
uv tool uninstall twobells      # or: pipx uninstall twobells
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/twobells"
```

If you ran it with `--state-dir`, or `config.toml` names another store, delete that folder instead. Delete any `config.toml` and `secrets.env` of your own too.

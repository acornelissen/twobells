# Backing up and moving

How to back up the settings, keys and history, put them back, and move them to another machine. [Where things are kept](configuration.md#where-things-are-kept) says what the store is, and [Removing Two Bells](uninstall.md) how to take it off afterwards.

- [What to keep](#what-to-keep)
- [On a Pi](#on-a-pi)
- [In Docker](#in-docker)
- [On a desktop](#on-a-desktop)
- [Moving to another kind of install](#moving-to-another-kind-of-install)

## What to keep

Each install keeps everything it saves in one folder, its store: `/var/lib/twobells` on a Pi, `/data` (the `twobells-data` volume) in Docker, and on a desktop `~/Library/Application Support/twobells` on a Mac or `~/.local/state/twobells` on Linux. Back up the whole folder; the commands below do. In it:

| File | What it is | Mode |
|---|---|---|
| `settings.toml` | What the settings pages and the setup wizard saved | 0640 |
| `secrets.env` | The keys pasted in the settings. **Secret.** | 0600 |
| `web.json` | The settings password, as a hash. **Secret.** | 0600 |
| `sessions.json`, `form.key` | Who is signed in to the settings, and the key their forms are signed with | 0600 |
| `keytests.json` | How each key's last test went; never the key | 0600 |
| `history.sqlite3` | The punctuality history, with `history.sqlite3-wal` and `-shm` beside it, and any `.v1` or `.v2` copy | 0640 |
| `state.json` | The board's state between runs | 0640 |
| `bus-timetable.json`, `tram-timetable.json`, `bank-holidays.json` | Built or downloaded again by themselves if they are missing | 0640 |

The rest (lock files, reset codes, a build's progress) needn't be kept. Only `settings.toml`, `secrets.env`, `web.json` and the history can't be made again.

A backup holds your keys and the password's hash, so keep it where only you can read it. The commands below make it readable by you alone (0600).

Stop Two Bells before you back up or put back, so nothing writes to the store part way through. Put a backup back on the same release or a newer one: an older release may not read the history a newer one kept (see [Going back to an older release](configuration.md#going-back-to-an-older-release)).

## On a Pi

The store belongs to the services' user, `twobells`, with the folder 0750. `config.toml` and `secrets.env` in `/etc/twobells` belong to root, and `secrets.env` is 0600. One archive takes both folders, with their owners and modes.

**Back up:**

```bash
sudo systemctl stop twobells.timer twobells.service twobells-screen.service twobells-web.service \
  twobells-fetch.path twobells-fetch.service twobells-timetable.timer twobells-timetable.path \
  twobells-timetable.service
sudo tar -czf ~/twobells-backup.tar.gz -C / var/lib/twobells etc/twobells
sudo chown "$USER": ~/twobells-backup.tar.gz
chmod 600 ~/twobells-backup.tar.gz
sudo reboot
```

The restart starts every service that was on. Copy `twobells-backup.tar.gz` off the Pi, to a computer of your own.

**Put it back**, on this Pi or a new one: first [install](install-pi.md) the same release or a newer one, which makes the `twobells` user. Then, with the backup in your home folder:

```bash
sudo systemctl stop twobells.timer twobells.service twobells-screen.service twobells-web.service \
  twobells-fetch.path twobells-fetch.service twobells-timetable.timer twobells-timetable.path \
  twobells-timetable.service
sudo tar -xzf ~/twobells-backup.tar.gz -C /
sudo reboot
```

`tar`, run as root, puts back each file's owner and mode. Start the board as [Start the board](install-pi.md#start-the-board) says if it isn't on yet. On a new Pi with another name, the settings site's address changes, and you sign in again.

## In Docker

The store is the `twobells-data` volume, owned by the container's user, uid 10001, with the folder 0700. The commands below run `tar` in a throwaway container from the image you run, as that user, with no network.

**Back up**, in a folder of your own:

```bash
IMAGE="$(docker inspect --format '{{.Config.Image}}' twobells)"
docker stop twobells
(umask 077; docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --user 10001:10001 -v twobells-data:/data:ro --entrypoint tar "$IMAGE" -czf - -C /data . \
  > twobells-backup.tar.gz)
docker start twobells
```

Keep your compose file, and `secrets.env` and `config.toml` if you have them, with it.

**Put it back**, into a new volume or over the one there, with the container stopped (`docker stop twobells`) and `IMAGE` the image you run, the same release or a newer one:

```bash
docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --user 10001:10001 -v twobells-data:/data --entrypoint tar IMAGE -xzf - -C /data \
  < twobells-backup.tar.gz
docker start twobells
```

A new volume takes the image's owner for `/data` by itself, so this works before the container has ever run; then start it as [Docker](docker.md) says for your platform. Compose may warn that it didn't create the volume: it uses it all the same.

**A folder in place of the volume** (on Unraid, say): back up the folder itself, as root, so its owner, 10001, is kept: `tar -czf twobells-backup.tar.gz -C /mnt/user/appdata twobells`, then `chmod 600 twobells-backup.tar.gz`. Put it back with `tar -xzf twobells-backup.tar.gz -C /mnt/user/appdata`, as root, with the container stopped.

## On a desktop

The store is yours, with the folder 0700. Stop Two Bells first: Ctrl-C in its Terminal window, or if it starts at login, `launchctl bootout "gui/$(id -u)/uk.twobells.serve"` on a Mac or `systemctl --user stop twobells.service` on Linux.

**Back up**, on a Mac:

```bash
(umask 077; tar -czf ~/twobells-backup.tar.gz -C ~/Library/"Application Support" twobells)
```

On Linux:

```bash
(umask 077; tar -czf ~/twobells-backup.tar.gz -C "${XDG_STATE_HOME:-$HOME/.local/state}" twobells)
```

**Put it back** the same way, with `tar -xzf ~/twobells-backup.tar.gz -C` and the same folder. Keep any `config.toml` and `secrets.env` of your own with it, and the `--state-dir` folder in place of these if you use one.

Then start it again: `twobells serve`, or `launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/uk.twobells.serve.plist` on a Mac, or `systemctl --user start twobells.service` on Linux.

## Moving to another kind of install

The store is the same on every install, so the same files move from a desktop to a Pi, say, or from a Pi to Docker. Take only what can't be made again: `settings.toml`, `secrets.env`, `web.json` and every `history.sqlite3` file. Leave `sessions.json` and `form.key` behind: the new install has another address, so everyone signs in again anyway. The timetables are built again on the new install.

Copy them into the new store with the new install stopped, owned by its user, with the modes in [What to keep](#what-to-keep). On a Pi, with the files in the current folder and the services stopped as above:

```bash
sudo install -o twobells -g twobells -m 600 secrets.env web.json /var/lib/twobells/
sudo install -o twobells -g twobells -m 640 settings.toml history.sqlite3* /var/lib/twobells/
sudo reboot
```

Into Docker, put them in an archive of their own, then put it back with the commands in [In Docker](#in-docker):

```bash
(umask 077; tar -czf twobells-backup.tar.gz settings.toml secrets.env web.json history.sqlite3*)
```

Each view's settings come with them. An install leaves off a view it can't run (Docker and a desktop run only the dashboard), and on a Pi **Views** on the [General page](settings.md#general) turns the board on.

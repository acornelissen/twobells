# Looking after a Pi

What runs on a Pi, where its logs are, and how to check its keys. To install or update, see [Install on a Raspberry Pi](install-pi.md); to change what it shows, use the [settings](settings.md).

- [The services](#the-services)
- [Logs](#logs)
- [Checking the keys and codes](#checking-the-keys-and-codes)

## The services

| Unit | What it does |
|---|---|
| `twobells-web.service` | The web server, `twobells serve --install pi` on port 8090: the settings at `/settings` and, while its view is on, the dashboard at `/`. Home network only, and it can't reach the internet (see [How the web server is kept safe](security.md)). |
| `twobells-guard.service` | The web server's network guard: loads its nftables rules at boot, before the web server, and removes them as it stops (see [How the web server is kept safe](security.md)). |
| `twobells-fetch.service`, `twobells-fetch.path` | `twobells fetch`: the web server's internet work. The wizard's lookups, key tests and bus company search, and, while the dashboard view is on, the dashboard's feeds. It goes out to the internet and listens on nothing, and hands what it finds to the web server through files in `/run/twobells-fetch`, in memory (see [The fetch service](security.md#the-fetch-service)). It starts at boot, and the `.path` unit starts it whenever the web server leaves a request. With the dashboard view off, it stops a minute after the last one. |
| `twobells-timetable.timer` | Builds the BODS bus timetable when one is due (see [Bus times](buses.md)). |
| `twobells.timer` | The e-paper board: one `twobells run` a minute. |
| `twobells-screen.service` | The HDMI screen: `twobells run --loop`. |

`install.sh` enables the web server, its network guard and the fetch service, and the timetable timer when buses come from BODS. It loads the guard afresh, and run again it restarts the web server if it was running, so its new unit loads and the guard holds it again, and the fetch service if it was running and its unit changed. The board's timer or screen service is yours to enable: `install.sh` prints the command and never enables it, and `update.sh` never enables or disables it either.

All of them run as the `twobells` user, with the store in `/var/lib/twobells`. Commands that write to the store refuse to run as root and say how to run them as that user instead.

## Logs

```bash
journalctl -u twobells -f                 # e-paper board log
journalctl -u twobells-screen -f          # screen board log
journalctl -u twobells-web -f             # web server log: settings, dashboard, reset codes
journalctl -u twobells-fetch -f           # the wizard's lookups and the dashboard's feeds
journalctl -u twobells-timetable -n 20    # bus timetable builds
sudo systemctl start twobells-timetable.service   # build the bus timetable now, if due
```

Every command and its options are in [Commands](commands.md). When something looks wrong, start with the settings' [Overview](settings.md#overview), then [Troubleshooting](troubleshooting.md).

## Checking the keys and codes

One live call each, as root so they can read `/etc/twobells/secrets.env`:

```bash
sudo /opt/twobells/venv/bin/twobells probe train
sudo /opt/twobells/venv/bin/twobells probe bus
```

With TransportAPI, the bus probe uses one lookup per stop from your 30 daily lookups. With BODS, build the timetable first (see [Bus times](buses.md)). The bus probe tells you whether you're getting live estimates (an expected time and status `late`/`on_time`) or only timetabled times (`unknown`, shown as `sch` on the board).

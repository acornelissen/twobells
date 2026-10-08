# inky-departures

Glossop train and bus departure board for a Raspberry Pi Zero W with a four-colour Inky pHAT (250x122).

![Board preview](docs/preview.png)

- **Trains:** National Rail Darwin, via the Rail Data Marketplace. Free, polled every 2 minutes.
- **Buses:** TransportAPI. Free plan is 30 hits/day, so polling is limited to set windows, with a hard daily budget.
- The panel takes about 20 s to refresh, so it redraws only when the content changes, plus at least every 10 minutes while active, to keep `Upd HH:MM` honest.
- Meaning is always in text (`+7`, `CANC`, `Delayed`, `sch`, `! STALE`). Red or yellow only reinforces it, so the board works for colour-blind viewers and on a mono panel.

## 1. Get access

### Rail Data Marketplace (trains)

1. Register at <https://raildata.org.uk> and confirm your email.
2. Search the product catalogue for **Live Departure Board**. Pick the LDBWS product, not "Live Fastest Departure Boards" or the staff versions.
3. Subscribe. It's free; accept the licence.
4. Open the subscription and copy the **consumer key** into `RDM_API_KEY`.
5. Check the endpoint URL shown on the product page. If its base differs from the default in `config.example.toml`, set `[train] base_url`.

Station codes: Glossop is `GLO`, Manchester Piccadilly is `MAN`.

### TransportAPI (buses)

1. Sign up at <https://developer.transportapi.com/signup> and choose the **Free** plan.
2. Your account starts with one app. Copy its `app_id` and `app_key` into `TAPI_APP_ID` and `TAPI_APP_KEY`.
3. Find your stops' ATCO codes, either way works:
   - <https://bustimes.org>: search for the stop. The code is the last part of the stop page URL.
   - <https://www.openstreetmap.org>: zoom in, click the bus stop and read the `naptan:AtcoCode` tag.
4. Add them to `[bus.stops]` as `LABEL = "ATCO"`. The label is shown on the board (`A: Hyde`). Each stop costs one hit per poll, so two stops halve the polls you can make. Add `directions = ["Ashton"]` to filter.

## 2. Install on the Pi

Raspberry Pi OS Lite (Bookworm or later) on the Zero W, with Wi-Fi and SSH set up in Raspberry Pi Imager.

```bash
git clone <this repo> inky-departures && cd inky-departures
sudo ./deploy/install.sh
sudo nano /etc/departureboard/secrets.env   # keys
sudo nano /etc/departureboard/config.toml   # bus stop, windows
```

Check the keys and codes with one live call each. The bus probe uses one hit per stop from your 30 daily hits; the default schedule uses 28.

```bash
sudo /opt/departureboard/venv/bin/departureboard probe train
sudo /opt/departureboard/venv/bin/departureboard probe bus
```

To try stops that aren't in the config, and keep the raw responses:

```bash
departureboard probe bus --stop V=1000DGVS3332 --stop W=1000DGVS3323 --save probe-raw
```

The bus probe output tells you whether you're getting live estimates (an expected time and status `late`/`on_time`) or only timetabled times (`unknown`, shown as `sch` on the board).

Start it:

```bash
sudo systemctl enable --now departureboard.timer
journalctl -u departureboard -f
```

If the display errors about chip select, add `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` and reboot.

## 3. Develop on your laptop

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
departureboard demo --png preview.png            # sample data, no keys
departureboard run --png preview.png --config config.toml --secrets secrets.env
```

## Emulator

A browser page that draws the pHAT at true proportions in e-paper colours, plays the slow colour refresh, and logs every tick, so you can see when the board redraws and when it leaves the panel alone.

```bash
departureboard emulate --demo                       # fake data, 1 simulated minute every 2 s
departureboard emulate --fake-train \
  --config config.toml --secrets secrets.env        # fake trains, live buses
departureboard emulate --config config.toml --secrets secrets.env   # all live
```

It opens <http://127.0.0.1:8765/>. Choose realistic (20 s), quick or instant refresh, and 1× to 4× scale; 1× is the panel's real pixel count. The demo drops the bus feed from 07:30 to 07:52 simulated time, so the `! STALE` flag shows up.

Live modes keep state in `state_file`, so the TransportAPI budget holds across restarts, and buses are only polled inside their windows.

## Layout

| Module | Role |
|---|---|
| `model.py` | Provider-neutral `Departure` and lateness maths |
| `darwin.py`, `transportapi.py` | Sources; HTTP injected, so parsing is tested with fixtures |
| `schedule.py`, `budget.py` | When to poll, and the daily hit cap |
| `view.py` | Departures to text rows plus a change signature |
| `render.py` | Rows to a 250x122 paletted image |
| `display.py` | Inky pHAT or PNG output |
| `emulator.py`, `emulator.html`, `emulation.py` | Browser panel emulator and its demo/live wiring |
| `fake.py` | Deterministic fake sources and simulated clock |
| `app.py` | One tick: poll what's due, redraw only on change |
| `cli.py`, `config.py` | `run` / `probe` / `demo`; TOML settings, env secrets |

To add a page or provider, implement `fetch() -> list[Departure]` with a `name` and add it to `config.py`.

# Displays

What each display can do, and how to set it up on a Pi. What the board shows on each is in [Reading the board](reading-the-board.md). The Inky pHAT has been run on the real panel; the others are tested in software, as the app draws them, and not yet on the hardware.

- [Choosing a display](#choosing-a-display)
- [Setting one up](#setting-one-up)
- [The bus position page](#the-bus-position-page)

## Choosing a display

| | Inky pHAT | Waveshare 10.85" e-Paper (G) | 7.9" HDMI screen | 11.3" HDMI bar screen |
|---|---|---|---|---|
| Size | 250×122 | 1360×480 | 1280×400 (on its side) | 1920×440 |
| Colours | black, white, red or yellow | black, white, red, yellow | full colour | full colour |
| Redraw | about 20 s, on change only | about 21 s, on change only | instant, every second | instant, every second |
| Clock, countdowns, bus page | no | no | yes | yes |
| Where each train or bus is, on its row | no | no | yes | yes |
| Light at night | none | none | the screen stays lit | the screen stays lit |
| Pi | Zero W or later | Zero 2 W or later | Zero 2 W or later (Zero needs a mini-HDMI adapter) | Zero 2 W or later (Zero needs a mini-HDMI adapter) |
| `display =` in `config.toml` | `"inky"` | `"waveshare-10in85g"` | `"hdmi-1280x400"`, plus `rotate = 90` or `270` | `"hdmi-1920x440"` (and `rotate = 90` or `270` only if it comes up on its side) |
| Runs as | `twobells.timer` | `twobells.timer` | `twobells-screen.service` | `twobells-screen.service` |

## Setting one up

The display is set in `/etc/twobells/config.toml` only. It starts as `"inky"`.

- **The Waveshare panel** needs `dtoverlay=spi0-0cs` in `/boot/firmware/config.txt`: it is two controllers side by side, and the board drives both chip selects itself. The driver follows Waveshare's `epd10in85g.py` command sequence over `spidev` and `gpiozero`. Each sign dot is 4×4 panel pixels.
- **An original Inky pHAT** (before 2019) is smaller, 212×104, and has no chip the board can detect it by: set `display = "inky-phat-v1"` and `panel_colour = "red"`, `"yellow"` or `"black"`. It also needs `dtoverlay=spi0-0cs`. On a display that small the plain look, black on white, is the easiest to read; text size 12 makes it a little bigger and still shows 2 trains and 1 bus.
- **The HDMI screen** is 400×1280 portrait out of the box; the board runs it on its side. It draws full screen with pygame, no desktop needed. pygame is installed only for the screen: `install.sh --screen`, or `install.sh` and `update.sh` when `config.toml` already says `hdmi-…`. To switch from e-paper:

  ```bash
  sudo systemctl disable --now twobells.timer
  sudo systemctl enable --now twobells-screen.service
  ```

  State is written only when new data arrives, not on every redraw, to spare the SD card. Touch isn't used yet, and the backlight stays on. Feeds are polled on the same schedule as on e-paper, so the TransportAPI budget is unchanged.
- **The 11.3" bar screen** is set up the same way, with `display = "hdmi-1920x440"`. The board draws four pixels a dot there, one more than on the 7.9" screen, so the text is a third bigger, with one line for each train or bus (see [The 11.3" bar screen](reading-the-board.md#the-113-bar-screen)). Bar screens differ in how they come up: if the log (`journalctl -u twobells-screen`) says the screen is 440x1920, set `rotate = 90` or `270`; if it says another size, the screen isn't telling the Pi its own, so add `video=HDMI-A-1:1920x440@60` to the end of the line in `/boot/firmware/cmdline.txt` and restart.

If the display errors about chip select, add `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` and reboot.

## The bus position page

Screen only. It needs `BODS_API_KEY` and a stop list for each route you want to follow. The wizard can build them (tick the route option before saving). Or build one on a laptop:

```bash
twobells route 341 --stop A --noc BNML --config config.toml --secrets secrets.env
```

Paste the printed `[bus.routes]` block into `config.toml`. It picks enough stops before yours to cover your walk plus 5 minutes of bus travel, at most 8. `BNML` is the operator code for Bee Network. The setup wizard and `twobells setup discover` find your stops' operator codes, and save or print them as `[bus] operators`. Timetable downloads for a large operator can be hundreds of MB; `--txc FILE` uses one you've already downloaded instead.

A bus that starts its journey at your stop is followed on its way in. If, say, most 341s to Hyde start at your stop, the page follows the 341 arriving from the other direction, which then turns round as your bus.

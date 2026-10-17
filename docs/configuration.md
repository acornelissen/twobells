# Settings by hand

The [setup wizard](setup-wizard.md) and the [settings](settings.md) cover nearly everything, and no `config.toml` is needed. This page is for what they don't cover, and for setting things up by hand. `examples/config.example.toml` in the repo lists every setting, with notes.

- [What only config.toml sets](#what-only-configtoml-sets)
- [Without the wizard](#without-the-wizard)
- [Where things are kept](#where-things-are-kept)
- [The dashboard's settings](#the-dashboards-settings)
- [What each view shows](#what-each-view-shows)
- [Rain warning set-up](#rain-warning-set-up)
- [Bank holidays](#bank-holidays)
- [Scrolling text](#scrolling-text)

## What only config.toml sets

- **The type of display** (`display`), and the original Inky pHAT's colour (`panel_colour`): see [Displays](displays.md).
- **Addresses:** `[train] base_url` and `[bus] base_url`, and `[dashboard] allowed_hosts`, the extra names the dashboard and settings answer to.
- **Where the store is:** `state_file`, or `[dashboard] state_dir` (see [below](#where-things-are-kept)).
- **Where bus times come from:** `[bus] source` and `operators`, and the stop list for the bus position page. The setup wizard sets these for you; see [Bus times](buses.md) and [The bus position page](displays.md#the-bus-position-page).
- **How TransportAPI stands in, and the bus page's checks:** `[bus] fallback_windows`, `fallback_interval_minutes` and `position_interval_seconds` (see [Bus times](buses.md#transportapi-as-a-stand-in)).

The settings pages can't change where keys are sent: the one address they take, for engineering works, must be at `raildata.org.uk`.

Where `config.toml` is: `/etc/twobells/config.toml` on a Pi; in Docker, a file you mount (see [A config.toml of your own](docker.md#a-configtoml-of-your-own)); on a desktop, any file given with `--config`.

## Without the wizard

Put the keys in `secrets.env` (see [Getting your keys](keys.md#where-keys-go)) and replace each `CHANGE_ME` in `config.toml` (on a Pi, both are in `/etc/twobells/`). If you don't want trains or buses, put `enabled = false` in `[train]` or `[bus]` (or remove the section); its `CHANGE_ME`s can stay. `examples/glossop.toml` in the repo is a filled-in one to compare against.

## Where things are kept

Each install keeps everything it saves in one folder, its store:

| Install | Folder |
|---|---|
| Pi | `/var/lib/twobells` |
| Docker | `/data`, in the `twobells-data` volume |
| Desktop | `~/Library/Application Support/twobells` on a Mac; on Linux, `twobells` in `$XDG_STATE_HOME`, else in `~/.local/state` |

`--state-dir` names another. Without it, `state_file` or `[dashboard] state_dir` in `config.toml` can, except on a Pi, where systemd gives every service `/var/lib/twobells` (as `$STATE_DIRECTORY`) and that wins. In Docker the container passes `--state-dir /data`. In it:

- `settings.toml`: what the settings pages and the wizard save, layered over `config.toml`, which is never rewritten. It holds only the settings they manage; anything else put in it is ignored, and the log names it. Addresses (`[train]` and `[bus]` `base_url`), `state_file`, `display` and the other hand-edited settings belong in `config.toml`. A `settings.toml` that can't be read is replaced on the next save, with a copy kept as `settings.toml.broken`.
- `secrets.env`: keys pasted in the settings or the wizard, readable by the app's user only. They win over the keys you give it in a `secrets.env` of your own.
- `web.json`: the settings password, as a hash.
- `keytests.json`: whether each key's last test on the Keys page worked or was refused, and when; never the key. Readable by the app's user only.
- `sessions.json`: who is signed in to the settings, kept as a hash of each browser's session so a restart doesn't sign anyone out, and `form.key`, which the sign-in forms are signed with. Like `web.json` and `secrets.env`, readable by the app's user only. A new password ends every session.
- `history.sqlite3`: the punctuality history. Moving it to a newer version keeps a copy of the old one beside it, once, as `history.sqlite3.v1` (or `.v2`), to put back if you go back to an older release (see [Updating](install-pi.md#updating) for a Pi, [Docker](docker.md) and [On a desktop](desktop.md)).
- The bus timetable, the tram timetable (`tram-timetable.json`, where trams are set up), bank holidays, the board's state, and lock files.

`config.toml` is optional. Without one, the app uses its defaults and what you save. To undo everything saved in the settings, delete `settings.toml` and `secrets.env` from the store.

## The dashboard's settings

The Dashboard page's settings can also go under `[dashboard]` in `config.toml`. All are optional:

| Setting | Default | What it does |
|---|---|---|
| `train_rows`, `bus_rows` | 10, 15 | How many of each to list (1 to 20) |
| `train_interval_seconds` | 60 | How often to check trains (at least 30) |
| `bus_interval_seconds` | 30 | How often to check buses (at least 15) |
| `weather_interval_minutes` | 30 | How often to check the forecast (at least 10) |
| `quiet_windows` | `["Mon-Sun 01:00-05:00"]` | Quiet hours: when not to check, unless the page is open. An HDMI screen's [announcements](displays.md#station-announcements-on-a-screen) keep quiet then too. An empty list checks all the time |
| `late_minutes_train`, `late_minutes_bus` | 3, 2 | How late counts as late in the history (1 to 60) |
| `history_days` | 90 | How many days of history to keep and show (1 to 3650) |
| `row_strips` | `true` | Each row's small drawing of its stops, with the train or bus on it (never on a wall tablet) |
| `show` | everything | What the dashboard shows beside the departures: see [What each view shows](#what-each-view-shows) |
| `modes` | every mode set up | Which modes of transport the dashboard lists: see [Modes of transport](#modes-of-transport) |
| `kb_incidents_url` | a placeholder | The engineering works address from the RDM Knowledgebase Incidents product page |
| `allowed_hosts` | `[]` | Extra names the dashboard and settings answer to (`config.toml` only) |
| `state_dir` | see [Where things are kept](#where-things-are-kept) | Where the app keeps its files (`config.toml` only) |

## What each view shows

**What to show** on the [Board](settings.md#board) and [Dashboard](settings.md#dashboard) pages saves a list for each view, `[board] show` and `[dashboard] show`, which can go in `config.toml` too. Each names what that view shows beside the departures' times, which always show; anything left out isn't drawn. A list naming anything else isn't loaded, and the page says why.

```toml
[board]
show = ["platforms", "where", "disruption", "rain", "last_bus", "bus_page"]

[dashboard]
show = ["next_to_catch", "where", "disruptions", "weather", "last_buses", "area_map", "remind"]
```

| View | Items, in the order the settings list them |
|---|---|
| Board | `platforms`, `where`, `disruption`, `rain`, `last_bus`, `bus_page`, `calling_at`, `clock_page`, `announce_speaker` |
| Dashboard | `next_to_catch`, `where`, `disruptions`, `weather`, `last_buses`, `area_map`, `remind`, `calling_at`, `coaches`, `punctuality`, `performance`, `fleet_numbers`, `announce` |

The example above is The usual preset; `show = []` is Just the times (the dashboard's adds `next_to_catch`), and every item but `announce_speaker` is Everything. The [setup wizard](setup-wizard.md) saves one of the presets for each view, The usual unless you pick another. `announce_speaker` is the HDMI screen's [station announcements](displays.md#station-announcements-on-a-screen) through a speaker: no preset has it, so it is on only when a list names it. Without a list, a view shows everything, except the board's clock page, which shows only with the older `clock_page = true`, and its announcements. Once the board has a list, `clock_page` is ignored: the list says.

What no view shows isn't fetched either, so leaving things out saves calls to the feeds and their daily limits. Each view fetches for itself, and a change takes effect on its next check, without a restart:

| Fetched | For |
|---|---|
| The departures | Always, whatever the lists say. The punctuality history keeps recording from them, so it is there if you tick Punctuality or Performance again |
| The forecast (Open-Meteo) | The board's `rain`; the dashboard's `weather` |
| Disruption checks (BODS situations, engineering works) | The dashboard's `disruptions`. The board's `disruption` notices come with its train times, at no extra cost |
| Where each train is (Service Details) | The board's `where`; the dashboard's `where` or `area_map` |
| Where trains that have left are (Service Details) | The dashboard's `performance`, for the Delay Repay list, with a filter station and a Service Details key |
| The bus positions between departure checks | The board's `where`, and its `bus_page` for the bus position page. The dashboard's come with its bus times |

The rest, such as the calling points, coaches and the last buses, comes with the departures or from the bus timetable, so leaving it out saves no calls. `enabled = false` in `[weather]` still switches the forecast off altogether, whatever the lists say.

## Modes of transport

**Modes of transport** on the [Board](settings.md#board) and [Dashboard](settings.md#dashboard) pages saves which of trains and buses each view shows, `[board] modes` and `[dashboard] modes`, which can go in `config.toml` too:

```toml
[board]
modes = ["bus"]           # the board shows buses alone

[dashboard]
modes = ["train", "bus"]  # the dashboard lists both
```

The modes are `train`, `bus` and `metro` (trams, metro and underground: Manchester's Metrolink trams, or London's Underground, DLR, trams, cable car and river buses live from TfL, set up by `[metro]`, and called Trams; see [Tram times](trams.md) and [London](london.md)). Each view shows them in that order, a section each, and checks only for those it shows. Without a list, a view shows every mode set up: one whose table is there and not switched off with `enabled = false`. A mode a list names that isn't set up is left out, and a list that names none of those set up is as if there were none, so switching buses off never leaves a board showing buses alone with nothing. A list that isn't one (`modes = "bus"`, a mode named twice, `modes = []`) stops the view loading, and says why.

The dashboard lists every mode it shows. The board has the room its display gives it, in its look and text size, and a `[board] modes` naming more modes than that stops it loading, saying how many fit: `[board] modes: The Inky pHAT has room for 2 modes at most, each with its own section, not 3`. Without a list it shows the first of those set up that fit. Each mode needs a section with its heading (in the station board look, a dotted rule) and at least one whole row; the board's text is sized to the display's height, so it is height that counts, not inches. E-paper has the same room in every look and at every text size:

| Display | Room for | Why |
|---|---|---|
| Inky pHAT, original Inky pHAT | 2 | With 3, the plain look has room for a row each at the standard text size and none for the third from size 13; the original pHAT has none for it even then |
| Waveshare 10.85" e-paper | 3 | The plain look has 3 trains, 2 buses and a tram at the standard size, a row each at the biggest |

An HDMI screen has room for 3 in the looks that keep a whole row for each, and 2 otherwise. A screen's "Calling at" line takes a row, and the plain look's text size (`text_size`) sets how tall each row is; the TFT and split-flap looks always draw at the standard size, 11:

| Display | Room for 3 | Room for 2 |
|---|---|---|
| 7.9" HDMI screen | The station board, TFT and split-flap looks, and the plain look at text size 12 or smaller | The plain look from text size 13: 133 dots tall, little more than the pHAT, so the third mode has no row |
| 11.3" HDMI bar screen | The station board, TFT and split-flap looks, and the plain look at text size 13 or smaller | The plain look from text size 14 |

So a screen showing three modes refuses a look and text size without room for them: `[board] modes: The 7.9-inch screen in the plain look at text size 14 has room for 2 modes at most, each with its own section, not 3; name fewer, or choose text size 12 or smaller, or another look`. While `[board] modes` names three, the [Board](settings.md#board) page offers only the text sizes with room for them, and the plain look only at one of those; changing the look or text size later is checked again before it is saved.

Trains and buses have always shared every display, so each has room for at least those two. Sharing the board, each mode has a row, then the first modes take theirs first, up to 3 trains and 2 buses on e-paper (3 on a screen), as before; a mode alone takes all the rows there are.

## Rain warning set-up

The forecast comes from [Open-Meteo](https://open-meteo.com) (free, no key), and works anywhere. The setup wizard sets the location from home. To set it by hand, add it to `config.toml`, rounded to two decimal places (about 1 km); the app rounds it again before sending it:

```toml
[weather]
lat = 53.44    # Glossop's; use your own
lon = -1.95
threshold_percent = 50
```

`enabled = false` in `[weather]` switches the warning and the dashboard's forecast off and keeps the location, as **Show the weather: Off** on the Trains and buses page does. The board checks the forecast every 30 minutes, only while it is checking trains or buses, and only while it shows the rain warning (see [What each view shows](#what-each-view-shows)). If it can't be fetched, the last forecast is used for up to 3 hours.

## Bank holidays

Dates come from GOV.UK, which lists England and Wales, Scotland, and Northern Ireland separately. The app uses England and Wales unless told otherwise: choose another under **Bank holidays** on the General page of the settings, or set `bank_holiday_division = "scotland"` or `"northern-ireland"` in `config.toml`. All three lists are kept for a week in the store. If GOV.UK can't be reached, the app keeps the dates it has and tries again in 6 hours. To ignore bank holidays for the checking windows, choose **Ignore them** there, or set `bank_holidays = false`. Bus times from the timetable always follow the operators' own bank holiday rules.

## Scrolling text

`scrolling = true`, the default, scrolls text too long for its line: the dashboard's calling-at line in the Dot matrix, TFT and Split-flap looks, and long text on a board's HDMI screen. `scrolling = false` cuts the dashboard's line to two lines with `…` and has the screen cut long text short with `…`, as e-paper always does. It is **Scrolling text** on the General page of the settings. A device set to reduce motion never scrolls the dashboard's line.

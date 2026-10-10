# Dashboard API

The contract between the dashboard's server (`departureboard dashboard`) and its browser front end. Everything is read-only: GET and HEAD only.

The password-protected settings area under `/settings` is not part of this API: it has its own pages, cookies (`dash_session`, `dash_form`, both `Path=/settings`) and headers (`form-action 'self'` and no scripts, except `script-src 'self'` for the setup wizard under `/settings/setup/`). Nothing here changes because of it.

Rules for every response:
- **Times:** `*_at` fields are ISO 8601 with the board's UTC offset (`2026-10-10T07:12:00+01:00`). `HH:MM` fields are for display only. The browser counts down from `*_at` against the server clock (`generated_at` plus its own offset), so the server never pushes every second.
- **Text** is plain text from the feeds; the browser must set it with `textContent`, never as HTML.
- **`id`** values are 16 lowercase hex characters, opaque, and stable for the same departure across snapshots. Raw Darwin service IDs and bus keys never reach the browser.

## `GET /api/snapshot`

Answers with `ETag: "v<version>"`, and 304 if `If-None-Match` matches.

```json
{
  "version": 42,
  "generated_at": "2026-10-10T07:01:30+01:00",
  "title": "Glossop > Man Picc",
  "walk": {"train": 10, "bus": {"A": 10, "B": 10}},
  "trains": {
    "station": "Glossop",
    "filter": "MAN",
    "status": {"last_success": "…", "stale": false, "error": null, "note": null},
    "messages": ["Disruption between Hadfield and Glossop until 10:00."],
    "rows": [{
      "id": "9f1c2a7b5e3d4c10",
      "service": "Northern",
      "destination": "Manchester Piccadilly",
      "origin": "Hadfield",
      "scheduled": "07:12", "scheduled_at": "…",
      "expected": "07:19", "expected_at": "…",
      "status": "late",
      "status_text": "Expected 07:19",
      "minutes_late": 7,
      "platform": "1",
      "length": 3,
      "cancel_reason": null,
      "delay_reason": "This train has been delayed by a broken down train",
      "leave_at": "…",
      "calling_points": [
        {"name": "Dinting", "crs": "DGT", "scheduled": "07:15", "expected": "07:22", "actual": null, "cancelled": false}
      ],
      "previous_points": [
        {"name": "Hadfield", "crs": "HDF", "scheduled": "07:04", "expected": null, "actual": "07:11", "cancelled": false}
      ],
      "position": {
        "where": "Left Hadfield, 7 min late",
        "strip": {"stops": ["Hadfield", "Glossop", "Dinting"], "yours": 1, "train_at": 0.4}
      },
      "history": {"phrase": "Late 4 of the last 10 weekdays (avg 6 min)"},
      "arrives": {"name": "Manchester Piccadilly", "at": "07:55", "at_iso": "…", "minutes": 36}
    }]
  },
  "buses": {
    "status": {"last_success": "…", "stale": false, "error": null, "note": null},
    "timetable": {"state": "ready", "built": "…", "valid_until": "2031-02-23", "journeys": 445},
    "stops": {"A": "Church Street (Stop A)", "B": "Church Street (Stop B)"},
    "rows": [{
      "id": "4b7e…",
      "line": "341",
      "destination": "Hyde Bus Station",
      "stop": "A",
      "stop_name": "Church Street (Stop A)",
      "scheduled": "07:20", "scheduled_at": "…",
      "expected": "07:24", "expected_at": "…",
      "status": "late",
      "status_text": "Expected 07:24",
      "minutes_late": 4,
      "live": true,
      "leave_at": "…",
      "vehicle": {"age_s": 40, "recorded_at": "…", "bearing": 95.0},
      "strip": {"stops": ["Mill Lane", "Park Road", "Church Street", "Market Place"], "yours": 2, "bus_at": 0.6, "where": "2 stops away"},
      "history": {"phrase": null, "no_show_phrase": null},
      "arrives": {"name": "Hyde Bus Station", "at": "08:02", "at_iso": "…", "minutes": 38},
      "inbound": null,
      "ghost": false
    }, {
      "id": "c03d…",
      "line": "237",
      "destination": "Ashton",
      "stop": "A",
      "scheduled": "07:25", "scheduled_at": "…",
      "expected": null, "expected_at": null,
      "status": "unknown",
      "status_text": "Timetable only",
      "minutes_late": null,
      "live": false,
      "leave_at": "…",
      "vehicle": null,
      "strip": {"stops": ["Church Street", "Market Place"], "yours": 0, "bus_at": null, "where": "No live position"},
      "history": {"phrase": null, "no_show_phrase": "No tracker on 3 of the last 10 weekdays"},
      "arrives": {"name": "Ashton", "at": "08:10", "at_iso": "…", "minutes": 45},
      "inbound": {"from": "The Junction", "due": "07:29", "late_min": 4, "where": "4 stops away",
                  "text": "Bus arriving from The Junction, 4 min late (due 07:29)"},
      "ghost": false
    }]
  },
  "weather": {
    "status": {"last_success": "…", "stale": false, "error": null, "note": null},
    "rain": "Rain from 08:00 (70%)",
    "now": {"temperature": 11.2, "feels_like": 9.0, "code": 61, "text": "Light rain", "wind": 12, "gust": 25, "direction": 250, "is_day": true},
    "hours": [{"start": "…", "temperature": 11, "probability": 70, "precipitation": 0.4, "code": 61, "text": "Light rain", "wind": 12, "is_day": true}],
    "sun": {"sunrise": "…", "sunset": "…"}
  }
}
```

### Fields

- **Status:**
  - `status` is one of `on_time`, `late`, `cancelled`, `unknown`.
  - `status_text` is always words, for example "On time", "Expected 07:19", "Cancelled", "Delayed", "Timetable only" (a bus with no live position), "No report". Colour only reinforces it.
  - `minutes_late` is null unless the status is late with a known time.
- **`leave_at`:** when to set off on foot (`expected_at` or else `scheduled_at`, minus the walk minutes for that mode or stop). It is null when cancelled or no walk time is set.
- **Missing data:**
  - A section can be `null` when it isn't configured (`trains`, `buses`, `weather`).
  - `now`, `sun`, `rain`, `strip`, `vehicle` and `position` can each be null.
  - `calling_points` and `previous_points` can be `[]`.
- **Calling points:** `calling_points` are the stops after this station, `previous_points` the ones before it, both in journey order. Times are `HH:MM` or null; `actual` is set once the train has been there. A cancelled stop has `cancelled: true` and null times.
- **Train `position`** (from Darwin's Service Details, which needs its own key, `RDM_DETAILS_API_KEY`):
  - It is known for the first 3 trains on the board that aren't cancelled, refreshed at most once a minute. Other rows have `position: null` and `previous_points: []`, unless the server still holds their details from earlier.
  - Without the key, or while the key is refused (the server stops asking for 6 hours), every row has `position: null` and `previous_points: []`.
  - `where` is a sentence for the row and screen readers, like "Left Hadfield 22:44, on time", "Left Guide Bridge, 3 min late", "At Glossop now", "Departed Glossop" or "Not started yet: leaves Hadfield at 22:44".
  - `strip.stops` are up to 5 stops before this station, this station, then the next stop if there is one; `strip.yours` is this station's index.
  - `strip.train_at` is the train's place in stop units along `stops` (0 is the first), worked out from the stops it has been to and the times between them. It is null when the train hasn't started or is further back than the strip shows.
- **`timetable.state`** is one of:
  - `ready`;
  - `building` (first build in progress);
  - `missing` (no timetable, with `status.note` saying why).
- **`timetable.journeys`** counts the timetabled journeys calling at your stops. The journeys kept only to find an inbound bus (ending at a stand near your stop without calling at it; see `inbound`) aren't counted.
- **`stop_name`:** the stop's name from the stop data with the letter on its sign when known ("Church Street (Stop A)"); "Stop A" from the board's label when the stop has no data. Show this rather than the label.
- **`strip`:**
  - `stops` are the stop names before yours, then yours, then possibly the next one.
  - `yours` is the index of your stop.
  - `bus_at` is the bus's position in stop units along `stops` (0 is the first). It is null when the bus isn't on the strip yet, and when there's no live bus.
  - `where` is a sentence for screen readers and the row.
- **`status.stale`** is true when nothing new has arrived for longer than expected. `error` is a short message with any keys removed.

### Row extras

Best guesses added on top of a row (worked out in `enrich.py`). They never change the row's own times, `status` or `status_text`.

- **`arrives`** (trains and buses; null when not known): where and when it gets there.
  - `name` is the place, `at` the time (`HH:MM`), `at_iso` the same with the board's offset, and `minutes` the journey time from this station or stop, counted from the expected departure (else the scheduled one). It crosses midnight correctly.
  - **Trains:** the arrival at the filter station (`trains.filter`) if the train calls there, else at its last calling point. Darwin's expected time is used when it is a time, otherwise the booked time ("On time", "No report"). It is null when the train or that stop is cancelled, when the stop says "Delayed", or when the train shows "Delayed" here: the booked time is known to be wrong and the right one isn't known.
  - **Buses:** the journey's last call (`name` is the bus's destination), at the timetabled time plus the delay the bus has now (`expected_at - scheduled_at`, 0 without a live bus).
- **`inbound`** (buses; otherwise null): the bus probably coming to work a departure that starts at your stop. BODS sends no block numbers, so it is inferred the way bustimes.org does:
  - only for a journey that starts at your stop;
  - from a journey of the same line that ends at your stop, or at a stop within 150 m of it, arriving 0–30 min before this one leaves. The timetable keeps journeys that end at such a stop without calling at yours for this alone;
  - only when exactly one such journey has a live bus placed on it (two or more is too much of a guess), and not when that bus would sooner work an earlier departure of the same line from the same stop.
  - `from` is where that journey started (null if no name is known), `due` its expected arrival at your stop (`HH:MM`), `late_min` how late it is now (0 if on time or early), `where` where it is ("4 stops away", "At your stop"), and `text` all of it as one line for the row, like "Bus arriving from Ashton, 4 min late (due 23:29)".
  - When the inbound bus is due after the departure's time, the departure still shows its timetabled time and its own status words. The pairing is a guess and buses often make up time on a layover, so the page states the inbound bus's facts and leaves the conclusion to the reader. It never shows a later departure time it can't back up.
- **`ghost`** (buses; always a boolean): true when the bus is due within 5 minutes (or just past its time) with no live bus placed on its journey (`live` false). `status_text` stays "Timetable only". It is false while live positions are failing (`status.note` says so) and when `inbound` is set, since a tracked bus is on its way to work it.
- **`history.no_show_phrase`** (buses only; null when there's nothing to say): how often this departure had no tracker before it left, like "No tracker on 3 of the last 10 weekdays", over the same days as `phrase`. It is null with fewer than 3 days recorded or when every day had a tracker. Train rows' `history` has `phrase` only.

## `GET /api/events`

Server-sent events (`text/event-stream`):
- On connect: `retry: 5000`, then the current snapshot as `id: <version>`, `event: snapshot`, `data: <the snapshot JSON on one line>`.
- On each change: the same event with the new version.
- Every 15 s: a comment line `: ping`.
- Resuming: if `Last-Event-ID` equals the current version, it sends no snapshot until the next change.
- The server closes a stream after 30 min, and the browser reconnects.
- With too many streams open (12) the server answers 503 with `Retry-After: 30`; the browser falls back to polling `/api/snapshot` every 30 s.

## `GET /api/bus/<id>`

The map for one bus row; 404 if the id is unknown (as when it's gone from the board).

```json
{
  "id": "4b7e…",
  "line": "341", "destination": "Hyde Bus Station", "stop": "A", "stop_name": "Church Street (Stop A)",
  "stops": [
    {"name": "Park Road", "lat": 53.4433, "lon": -1.9625, "time": "07:16", "yours": false},
    {"name": "Church Street", "lat": 53.4435, "lon": -1.9590, "time": "07:20", "yours": true}
  ],
  "path": [[53.4433, -1.9625], [53.44345, -1.9610], [53.4436, -1.9598], [53.4435, -1.9590]],
  "vehicle": {"lat": 53.4438, "lon": -1.9648, "bearing": 95.0, "recorded_at": "…", "age_s": 40},
  "where": "2 stops away"
}
```

- `stops` are the journey's positioned calls: up to 20 before yours, yours, and one after, in order. `time` is the timetabled time.
- `path` is the road through those stops, first to last, as `[lat, lon]` pairs: each stop, then the road to the next. The roads come from the OpenStreetMap car router when the timetable is built (`roadshape.py`), never when the map is asked for; where it had none, the path goes straight from one stop to the next. It has as many points as `stops` at least, so `[]` when `stops` is empty.
- `vehicle` is null when the bus has no live position.

## `GET /api/history/<id>`

The punctuality history of a train or bus row (same ids as the snapshot); 404 if the id is unknown.

```json
{
  "id": "9f1c…", "mode": "train",
  "label": "07:12 to Manchester Piccadilly",
  "day_type": "weekday",
  "phrase": "Late 4 of the last 10 weekdays (avg 6 min)",
  "days": [{"day": "2026-10-09", "status": "late", "delay_min": 7, "live": true, "reason": null}]
}
```

`days` are newest first, at most `[dashboard] history_days` of them (1 to 3650, default 90).

## `GET /healthz`

200 `ok` when the pollers are alive and the snapshot was rebuilt in the last 3 minutes; otherwise 503.

## Static files

- `/`: the page.
- `/static/<path>`: files from `src/departureboard/dashboard_static/`, from an allowlist built at start.
- `/manifest.webmanifest`.

## Access rules (every request)

Checked in this order, before anything else. Each refusal has a plain-text body and the security headers below.

- **403** unless the client's address is private, loopback or link-local, the request has exactly one `Host` header, and that host is local or in `[dashboard] allowed_hosts`. Local means a private IP address, a name without dots, or a name ending `.local`, `.lan` or `.home.arpa`. This stops DNS rebinding.
- Then everything under `/settings` goes to the settings area, which has its own rules (see the top of this page).
- **405** with `Allow: GET, HEAD` for any other method.
- **404** when the request target is over 512 characters, doesn't start with `/`, or holds `..`, a backslash, or an encoded dot, slash, backslash or NUL (`%2e`, `%2f`, `%5c`, `%00`, in any case).
- **403** for `/api/` when `Sec-Fetch-Site` is `cross-site`: another site's page can't read the API through the browser.

## Security headers (every response)

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
```

`img-src data:` is there for Leaflet's inline marker images. No inline scripts or styles: style changes are made from JS through the CSSOM (`el.style.x = …`), which CSP allows.

## Snapshot: `disruptions`

A top-level key of `/api/snapshot`, after `weather`. It is `null` when neither source is set up (no buses, and no `RDM_KB_API_KEY`).

```json
"disruptions": {
  "status": {
    "bus": {"last_success": "…", "stale": false, "error": null, "note": null},
    "train": null
  },
  "items": [{
    "id": "5d0e…",
    "mode": "bus",
    "title": "Station Road, Hadfield - Roadworks",
    "summary": "Due to roadworks on Station Road, service 237 will divert…",
    "reason": "Roadworks",
    "from_at": "2026-10-12T08:00:00+01:00", "to_at": "2026-10-30T16:00:00+00:00",
    "periods": [{"from_at": "2026-10-12T08:00:00+01:00", "to_at": "2026-10-30T16:00:00+00:00"}],
    "planned": true,
    "lines": ["237"],
    "stops": [],
    "places": ["Station Road"],
    "severity": "normal",
    "link": null
  }]
}
```

- **Sources:**
  - `bus`: BODS SIRI-SX (the buses' `BODS_API_KEY`), polled every 10 minutes. `null` without buses. `note` is "Waiting for the bus timetable" until the timetable says which lines and stops are ours.
  - `train`: the National Rail Knowledgebase's incidents through the Rail Data Marketplace (`RDM_KB_API_KEY`, at `[dashboard] kb_incidents_url`), polled every 30 minutes. `null` without the key. With the key but no address, `note` says so and nothing is fetched. `note` is "Waiting for the train board" until a board names our operators.
- **Which are kept** (see `disruptions.py`): valid now or in the next 7 days, and:
  - buses: on our line *with* the operator our timetable has for it; at one of our own stops; at a positioned stop our journeys use, if that part of the notice names no lines; or "all lines" of one of our operators with an affected stop inside our area (about 1 km around the positioned stops). Nothing else.
  - trains: names one of the operators running our trains, and mentions one of our stations (ours, the filter station, or any our trains call at) by name or three-letter code.
- **Items:** buses first, then trains, each soonest first; at most 30 of each.
  - `id` is 16 hex characters, stable for the same notice; the feed's own number stays on the server.
  - `title`, `summary` (at most 500 characters) and `reason` are plain text, with any HTML in the feed dropped.
  - `periods` are the notice's validity periods that touch now to 7 days ahead, in order (at most 14). `from_at` and `to_at` repeat the first. `to_at` is `null` when open-ended.
  - `planned` is the feed's own word: engineering works and booked roadworks are planned.
  - `lines` are our lines it affects (bus only); `stops` our stop labels it names, as in `buses.stops` (bus only); `places` stop or station names, in words.
  - `severity` is `slight`, `normal`, `severe` or `unknown`; always shown in words.
  - `link` is `null` unless the feed gave an https link on a known operator's site (`disruptions.LINK_HOSTS`).
- **On the page:** a panel lists them, in force first, then coming up, in words. A bus row whose line or stop an item names, during one of its periods, gets a "Disruption" link to it. Rail `messages` stay in their own panel.

## Snapshot: `timetable_changes`

A top-level key of `/api/snapshot`, after `disruptions`: the bus lines whose timetable changes in the next 28 days. `null` without buses or a usable timetable; `[]` when nothing changes.

```json
"timetable_changes": [{"line": "341", "operator": "BNML", "from": "2026-10-25"}]
```

- **The rule** (`area.timetable_changes`): the bus timetable keeps journeys whose calendar starts in the future (they never run before their start). A day D in the next 28 days is looked at if a journey starts on D or ended the day before. A line (with its operator) changes on D when the set of its journeys running on D, by their stops and times, differs from the set that ran on the same weekday a week before: new journeys, ended ones, or new times. Each line is listed once, at its first change, soonest first; at most 20. Bank holidays aren't counted. Journeys kept only to find an inbound bus give no departures, so they don't count either.
- It is worked out again only when the timetable is read again or the day changes.
- `from` is `YYYY-MM-DD`, the first day of the new timetable.
- **On the page:** a notice above the bus table, such as "New 341 timetable from Sun 25 Oct."

## `GET /api/area`

The area map: every live bus on our lines around our stops, from the bus poller's last BODS fetch (no extra calls). 404 until the buses have been checked with a usable timetable, and again while there is none. Not part of the snapshot and not versioned: the page asks again with each snapshot while the map is open, and every 25 s or so for its summary. Same [access rules](#access-rules-every-request) and headers as the rest of the API.

```json
{
  "fetched_at": "2026-10-09T10:14:30+01:00",
  "box": [-1.975966, 53.433239, -1.938157, 53.454467],
  "vehicles": [
    {"line": "341", "destination": "Hadfield", "lat": 53.4438, "lon": -1.9648, "bearing": 90.0,
     "recorded_at": "2026-10-09T10:14:00+01:00", "age_s": 30, "next_stop": "Park Road"}
  ],
  "stops": [{"label": "A", "name": "Church Street", "lat": 53.4435, "lon": -1.9590}],
  "routes": [{"line": "341", "destination": "Hadfield", "path": [[53.444467, -1.965966], [53.44438, -1.96431], [53.444223, -1.962774]]}]
}
```

- `fetched_at` is when the buses were last checked; `age_s` is a report's age then, so the page works ages out from `recorded_at`.
- `box` is west, south, east, north around the timetable's positioned stops (about 1 km wider), or `null`. Only buses inside it, on lines our timetable has, reported in the last 5 minutes, are sent; at most 80.
- `destination` is the timetabled journey's when the bus was matched to one, else the bus's own. `next_stop` is the next positioned stop on that journey, or `null` when it wasn't matched or the stop isn't positioned. `bearing` and `recorded_at` can be `null`.
- `stops` are our stops that have a position, by label.
- `routes`: for each line and destination, `path`: the road through the positioned stops of its longest journey, in order, as `[lat, lon]` pairs (at least two, every stop among them), as for `/api/bus/<id>`: straight between stops the timetable has no road for. At most 40. Only journeys calling at your stops are drawn, not those kept only to find an inbound bus.
- **On the page:** "Around here" has an **Area map** button opening a dialog with the map (line numbers on the bus markers, words in the popups), real zoom and "Show the whole area" buttons, the OpenStreetMap credit, and the same buses listed in words, each with a "Show on map" button. It also lists the stations on the train line in order, from the rows' calling points (and `previous_points`), with each train that has a `position` placed at or between stations, in words.

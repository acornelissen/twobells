# Dashboard API

The contract between the dashboard's server (`departureboard dashboard`) and its browser front end. Everything is read-only: GET and HEAD only.

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
  "walk": {"train": 12, "bus": {"A": 5, "B": 10}},
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
      "history": {"phrase": "Late 4 of the last 10 weekdays (avg 6 min)"}
    }]
  },
  "buses": {
    "status": {"last_success": "…", "stale": false, "error": null, "note": null},
    "timetable": {"state": "ready", "built": "…", "valid_until": "2031-02-23", "journeys": 445},
    "stops": {"A": "Henry Street (Stop A)", "B": "Henry Street (Stop B)"},
    "rows": [{
      "id": "4b7e…",
      "line": "341",
      "destination": "Hyde Bus Station",
      "stop": "A",
      "scheduled": "07:20", "scheduled_at": "…",
      "expected": "07:24", "expected_at": "…",
      "status": "late",
      "status_text": "Expected 07:24",
      "minutes_late": 4,
      "live": true,
      "leave_at": "…",
      "vehicle": {"age_s": 40, "recorded_at": "…", "bearing": 95.0},
      "strip": {"stops": ["Sunlaws Street", "St Marys Road", "Henry Street", "Howard Arms"], "yours": 2, "bus_at": 0.6, "where": "2 stops away"},
      "history": {"phrase": null}
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
  - `now`, `sun`, `rain`, `strip` and `vehicle` can each be null.
  - `calling_points` can be `[]`.
- **`timetable.state`** is one of:
  - `ready`;
  - `building` (first build in progress);
  - `missing` (no timetable, with `status.note` saying why).
- **`strip`:**
  - `stops` are the stop names before yours, then yours, then possibly the next one.
  - `yours` is the index of your stop.
  - `bus_at` is the bus's position in stop units along `stops` (0 is the first). It is null when the bus isn't on the strip yet, and when there's no live bus.
  - `where` is a sentence for screen readers and the row.
- **`status.stale`** is true when nothing new has arrived for longer than expected. `error` is a short message with any keys removed.

## `GET /api/events`

Server-sent events (`text/event-stream`):
- On connect: `retry: 5000`, then the current snapshot as `id: <version>`, `event: snapshot`, `data: <the snapshot JSON on one line>`.
- On each change: the same event with the new version.
- Every 15 s: a comment line `: ping`.
- Resuming: if `Last-Event-ID` equals the current version, it sends no snapshot until the next change.
- The server closes a stream after 30 min, and the browser reconnects.
- With too many streams open the server answers 503; the browser falls back to polling `/api/snapshot` every 30 s.

## `GET /api/bus/<id>`

The map for one bus row; 404 if the id is unknown (as when it's gone from the board).

```json
{
  "id": "4b7e…",
  "line": "341", "destination": "Hyde Bus Station", "stop": "A",
  "stops": [
    {"name": "Sunlaws Street", "lat": 53.443494, "lon": -1.959026, "time": "07:16", "yours": false},
    {"name": "Henry Street", "lat": 53.44384, "lon": -1.948999, "time": "07:20", "yours": true}
  ],
  "vehicle": {"lat": 53.4435, "lon": -1.9573, "bearing": 95.0, "recorded_at": "…", "age_s": 40},
  "where": "2 stops away"
}
```

- `stops` are the journey's positioned calls: up to 20 before yours, yours, and one after, in order. `time` is the timetabled time.
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

`days` are newest first, at most 90.

## `GET /healthz`

200 `ok` when the pollers are alive and the snapshot was rebuilt in the last 3 minutes; otherwise 503.

## Static files

- `/`: the page.
- `/static/<path>`: files from `src/departureboard/dashboard_static/`, from an allowlist built at start.
- `/manifest.webmanifest`.

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

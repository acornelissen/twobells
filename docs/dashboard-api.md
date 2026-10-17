# Dashboard API

The contract between the dashboard's server (`twobells serve`, on every install) and its browser front end. Everything is read-only: GET and HEAD only.

Everything under `/settings` is the settings site, not part of this API: it has its own pages, cookies (`settings_session`, `settings_form`, both `Path=/settings`) and headers (`form-action 'self'`, and `script-src 'self'` only for the setup wizard under `/settings/setup/` and for `/settings/settings.js` on a signed-in visitor's settings pages). Nothing here changes because of it.

The API is there only while the dashboard is on: its view is on (`[app] views`), the app is set up, and its settings load. While it is off, `/` redirects (303) to `/settings`, `/healthz` answers 200 `ok` so a container's health check still hears the server, and everything else here (`/performance`, `/api/…`, `/static/…`, `/manifest.webmanifest`) is 404. Responses then carry the settings site's security headers rather than those below.

Rules for every response:
- **Times:** `*_at` fields are ISO 8601 with the board's UTC offset (`2026-10-10T07:12:00+01:00`). `HH:MM` fields are for display only. The browser counts down from `*_at` against the server clock (`generated_at` plus its own offset), so the server never pushes every second.
- **Text** is plain text from the feeds; the browser must set it with `textContent`, never as HTML.
- **`id`** values are 16 lowercase hex characters, opaque, and stable for the same departure across snapshots. Raw Darwin service IDs and bus keys never reach the browser.

Contents:

- The snapshot: [`GET /api/snapshot`](#get-apisnapshot) ([Fields](#fields), [Row extras](#row-extras)), [`disruptions`](#snapshot-disruptions), [`timetable_changes`](#snapshot-timetable_changes)
- Other endpoints: [`GET /api/events`](#get-apievents), [`GET /api/bus/<id>`](#get-apibusid), [`GET /api/history/<id>`](#get-apihistoryid), [`GET /api/performance`](#get-apiperformance), [`GET /api/area`](#get-apiarea), [`GET /healthz`](#get-healthz), [Static files](#static-files)
- Rules: [Access rules](#access-rules-every-request), [Security headers](#security-headers-every-response-while-the-dashboard-is-on)

## `GET /api/snapshot`

Answers with `ETag: "v<version>"`, and 304 if `If-None-Match` matches.

```json
{
  "version": 42,
  "generated_at": "2026-10-10T07:01:30+01:00",
  "title": "Glossop > Man Picc",
  "walk": {"train": 10, "bus": {"A": 10, "B": 10}},
  "quiet": false,
  "row_strips": true,
  "show": ["next_to_catch", "where", "disruptions", "weather", "last_buses", "area_map", "remind", "calling_at",
           "coaches", "punctuality", "performance", "fleet_numbers", "announce"],
  "trains": {
    "station": "Glossop",
    "filter": "MAN",
    "status": {"last_success": "…", "stale": false, "error": null, "note": null},
    "positions": {"last_success": "…", "stale": false, "error": null, "note": null},
    "messages": [
      {"text": "Disruption between Hadfield and Glossop until 10:00.", "category": "train", "severity": "minor", "kind": "service"},
      {"text": "The ticket office is currently closed at this station.", "category": null, "severity": null, "kind": "station"}
    ],
    "rows": [{
      "id": "9f1c2a7b5e3d4c10",
      "service": "Northern",
      "destination": "Manchester Piccadilly",
      "origin": "Hadfield",
      "scheduled": "07:12", "scheduled_at": "…",
      "expected": "07:19", "expected_at": "…",
      "status": "late",
      "status_text": "Exp 07:19",
      "status_spoken": "Expected 07:19",
      "minutes_late": 7,
      "platform": "1",
      "platform_changed": false,
      "usual_platform": "1",
      "via": "via Guide Bridge",
      "replacement_bus": false,
      "ferry": false,
      "length": 3,
      "formation": {
        "coaches": [
          {"number": "1", "class": "first", "toilet": null, "toilet_out_of_order": false, "loading": 45, "busy": "Busy"},
          {"number": "2", "class": "standard", "toilet": "accessible", "toilet_out_of_order": false, "loading": 62, "busy": "Busy"},
          {"number": "3", "class": "standard", "toilet": "standard", "toilet_out_of_order": false, "loading": 18, "busy": "Quiet"}
        ],
        "loading_text": "Quietest at the back",
        "detach_text": null,
        "reverse": false
      },
      "cancel_reason": null,
      "delay_reason": "This train has been delayed by a broken down train",
      "walk_min": 10,
      "leave_at": "…",
      "calling_at": "Calling at: Dinting, Broadbottom, Hattersley, Godley, Newton for Hyde, Flowery Field, Guide Bridge, Gorton, Ashburys and Manchester Piccadilly. This train is formed of 3 coaches.",
      "announce": [
        {"kind": "delayed", "text": "We are sorry to announce that the 07:12 Northern service to Manchester Piccadilly via Guide Bridge is delayed by approximately 7 minutes. This is due to a broken down train."},
        {"kind": "next", "text": "The next train to depart from platform 1 will be the delayed 07:12 Northern service to Manchester Piccadilly via Guide Bridge. Calling at Dinting, … and Manchester Piccadilly. This train is formed of 3 coaches."}
      ],
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
    "positions": {"last_success": "…", "stale": false, "error": null, "note": null},
    "timetable": {"state": "ready", "built": "…", "valid_until": "2031-02-23", "journeys": 445},
    "stops": {
      "A": {"name": "Church Street", "letter": "A", "towards": "Market Place", "walk_min": 10},
      "B": {"name": "Church Street", "letter": "B", "towards": "Park Road", "walk_min": 10}
    },
    "rows": [{
      "id": "4b7e…",
      "line": "341",
      "destination": "Hyde Bus Station",
      "stop": "A",
      "stop_name": "Church Street (Stop A)",
      "scheduled": "07:20", "scheduled_at": "…",
      "expected": "07:24", "expected_at": "…",
      "status": "late",
      "status_text": "Exp 07:24",
      "status_spoken": "Expected 07:24",
      "minutes_late": 4,
      "live": true,
      "walk_min": 10,
      "leave_at": "…",
      "vehicle": {"age_s": 40, "recorded_at": "…", "bearing": 95.0, "fleet_number": "3017", "registration": "AB25 CDE"},
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
      "status_spoken": "Timetable only",
      "minutes_late": null,
      "live": false,
      "walk_min": 10,
      "leave_at": "…",
      "vehicle": null,
      "strip": {"stops": ["Church Street", "Market Place"], "yours": 0, "bus_at": null, "where": "No live position"},
      "history": {"phrase": null, "no_show_phrase": "No tracker on 3 of the last 10 weekdays"},
      "arrives": {"name": "Ashton", "at": "08:10", "at_iso": "…", "minutes": 45},
      "inbound": {"from": "The Junction", "due": "07:29", "late_min": 4, "where": "4 stops away",
                  "text": "Bus arriving from The Junction, 4 min late (due 07:29)", "followed": false},
      "ghost": false
    }],
    "last_buses": [{
      "line": "341",
      "destination": "Hyde Bus Station",
      "stop": "A",
      "scheduled": "23:05", "scheduled_at": "…",
      "remaining": 34,
      "show": false
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
  - `status_text` is always words, the same words the board uses: "On time", "Exp 07:19" (late, expected at 07:19), "Delayed" (late, no expected time yet), "Cancelled", "No report" (a train with no live report), "Timetable only" (a bus with no live position). Colour only reinforces it.
  - `status_spoken` is `status_text` with nothing shortened ("Expected 07:19"), for screen readers. It is the same as `status_text` unless that is abbreviated.
  - `minutes_late` is null unless the status is late with a known time.
- **`walk_min`:** the walk to the station or to the row's stop, in whole minutes, as set for the board (`walk.train`, or `walk.bus` for the stop, else its `*`). It is null when no walk time is set. A cancelled row still has it.
- **`leave_at`:** when to set off on foot (`expected_at` or else `scheduled_at`, minus `walk_min`). It is null when cancelled or no walk time is set.
- **Missing data:**
  - A section can be `null` when it isn't configured (`trains`, `buses`, `weather`).
  - `now`, `sun`, `rain`, `strip`, `vehicle`, `position` and `formation` can each be null.
  - `calling_points` and `previous_points` can be `[]`, and `calling_at` null.
- **Train `messages`** (National Rail's own messages for the station, at most 3, plain text):
  - `category` is Darwin's, when it sends one: `train`, `station`, `connections`, `system`, `misc`, `prior_trains` or `prior_other`; else null. `severity` likewise: `normal`, `minor`, `major` or `severe`, or null.
  - `kind` is `station` for a notice about the station's facilities (ticket office, ticket machines, lifts, toilets, waiting rooms, car park, help points, step-free access), else `service`. Darwin's category decides when it is sent (`station` is a station notice); without one, the words do (`darwin.message_kind`). Words about the trains (delays, cancellations, no trains, replacement buses, a closed station) always make it `service`, so a station notice never hides disruption.
  - **On the page:** `service` messages are the "Disruption" panel and are announced once in the live region; `station` ones are a quiet "Station information" line in the trains section and are never announced. An older server's plain strings are taken as `service`.
- **`replacement_bus`:** true for a rail replacement bus, which Darwin lists apart from the trains (`busServices`, `serviceType` "bus") and the server merges in with them by time; false for a train. A replacement bus has `platform: null` and `length: null`, and its punctuality is kept apart from the train it stands in for. The page says "Bus" in its Platform column and "Replacement bus" under its destination and in its sheet.
- **`ferry`:** true for a ferry, which Darwin lists apart from the trains where one connects (`ferryServices`, `serviceType` "ferry") and the server merges in with them by time; false for a train or a replacement bus (never both). Like a replacement bus it has `platform: null`, `length: null`, `formation: null`, no `usual_platform` and never a `platform_changed`, and its punctuality is kept apart. The page says "Ferry" in its Platform column and "ferry" in its name ("the 07:15 ferry to Ryde Pier Head").
- **`formation`:** the train's coaches, for its sheet, where the operator sends them to Darwin; many don't, and then it is null and the sheet has the coach count (`length`) alone. A replacement bus or a ferry always has null. The board's rows and the e-paper board use `length` only.
  - `coaches` are in Darwin's order, from the front of the train; it can be `[]` when Darwin sends only its flags. Each has its `number` as on the coach ("A", "12"; at most 2 letters or digits, else null), `class` (`first`, `standard`, `mixed` or null), `toilet` (`standard`, `accessible`, or null for none or not known), `toilet_out_of_order`, `loading` (how full, 0 to 100, or null) and `busy`, that loading in words: "Quiet" under 35, "Busy", or "Very busy" from 70.
  - `loading_text` says where to sit: "Quietest at the front", "Quietest in the middle", "Quietest at the back" (each end is a third of the train, rounded up, read from the coaches' loadings when at least half have one), "Much the same all along the train" (the ends within 15 of each other), or, from Darwin's average alone, "Quiet overall" and the like. It is null when Darwin gives no loading.
  - `detach_text` is "Front 2 coaches detach here" when Darwin says the front of the train detaches at this station (`detachFront`), with the count only when the train is shorter at its next stop; else null. `reverse` is Darwin's `isReverseFormation`: the train runs the other way round from usual.
  - The server reads Darwin's `formation` as hostile like the rest: a coach that isn't an object, or more than 30 coaches, leaves the whole formation out, and a field of the wrong type or out of range is null.
- **Platforms:**
  - `platform` is Darwin's, like "1" or "2A", or null until it is known.
  - `usual_platform` is the platform the train left from on most of its last 10 days in the punctuality history with a platform known: one used on more than half of them, and at least 3. It is null with less history than that, with no one platform used most, for a replacement bus or a ferry, and without a history.
  - `platform_changed` is true when `platform` isn't the one the server's last fetch gave the same train (it stays true while the train keeps its new platform), or isn't `usual_platform`. A platform appearing or going is no change, and a replacement bus or a ferry is never one. The page says "2 (changed)" in the Plat column, on the late colours, says "Usually: Platform 1" in the sheet, and announces each change once in the live region.
- **`via`:** Darwin's words on the train's route, after its destination, like "via Guide Bridge", or null. The page shows it under the destination and in the sheet's title.
- **Calling points:** `calling_points` are the stops after this station, `previous_points` the ones before it, both in journey order. Times are `HH:MM` or null; `actual` is set once the train has been there. A cancelled stop has `cancelled: true` and null times.
- **`calling_at`:** the station board's "Calling at" line, in words, from `calling_points` and `length` (or else the coaches in `formation`): "Calling at: Dinting and Manchester Piccadilly." ("Calling at: Manchester Piccadilly only." for one stop, from `calling_points` less those cancelled), then "Does not call at Hattersley or Godley." for stops cancelled (alone when every stop is), then "This train is formed of 3 coaches." when the length is known, or "This is a replacement bus." or "This is a ferry." It is null for a cancelled train and when Darwin gives no calling points. The board's screens scroll the same line under their first train. **On the page:** the first row shows it under the destination; the Dot matrix, TFT and Split-flap looks scroll it along one line, in from the right, with a **Pause scrolling** button above the trains table (`aria-pressed`, remembered in the browser) that stops it and wraps it. With reduced motion it never scrolls, and the button isn't shown; a wall tablet leaves the line out.
- **`announce`:** what a station's public address would say about the train, as `{"kind", "text"}` in the order to say them, worded by `core/announce.py`. A cancelled train has `cancelled` alone: "We are sorry to announce that the 07:42 Northern service to Manchester Piccadilly has been cancelled. This is due to a shortage of train crew." Any other train has, in this order:
  - `platform` when `platform_changed` is true and the platform is known: "Platform alteration: the 07:12 … will now depart from platform 2."
  - `delayed` when it is late: "… is delayed by approximately 7 minutes.", or "… is delayed." with no expected time, then the reason.
  - `next` for the next train to depart (the first row not cancelled, not gone by its expected or booked time, and not late with no expected time; one row at most): "The next train to depart from platform 1 will be the 07:12 Northern service to Manchester Piccadilly.", with "the delayed 07:12" when late, and "from platform 1" left out until it is known. Then "Calling at Dinting and Manchester Piccadilly." ("… only." for one stop), "Does not call at Hattersley." for stops cancelled (alone when every stop is), and "This train is formed of 3 coaches." when the length is known.
  - `departs` for any later train: "The 08:12 Northern service to Manchester Piccadilly will depart from platform 1.", or "… is due to depart." without a platform, then the same stops and length.
  - A rail replacement bus or a ferry has no platform, alteration or coaches. A replacement bus is "The next service to depart will be the 07:12 Northern service to Glossop.", ending "This is a replacement bus."; a ferry is "The next ferry to depart will be the 07:12 Wightlink service to Ryde Pier Head.", and a later one ends "This is a ferry.". Their apologies name them: "the 07:12 Northern replacement bus service", "the 07:12 Wightlink ferry service".
  - Darwin's reasons are whole sentences, "This train has been cancelled because of a shortage of train crew"; they are said as a station says them, "This is due to a shortage of train crew." A reason in any other shape is said as Darwin gives it.
  - **On the page:** a train's sheet has **Announce**, which plays a chime and speaks them all, showing the words as it does. A wall tablet at `/?kiosk&announce` speaks the `cancelled` and `platform` ones of trains due within the hour, the `delayed` and `next` of the row with a `next` 4 minutes before it goes, and the `delayed` of a train late with no `expected_at` 4 minutes before its `scheduled_at`, each once and never while `quiet` is true. See [Station announcements](dashboard.md#station-announcements).
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
- **`timetable.valid_until`** is the last day of the first service in the timetable to end (`YYYY-MM-DD`), or null when none has an end. Only that service stops then; the timetable is built again the day after.
- **`buses.stops`:** each of your stops by its board label, with what tells it from another stop of the same name (two stops across a road from each other usually share one). Nothing is made up: a stop the stop data doesn't have has every field null but `walk_min`.
  - `name`: NaPTAN's name for the stop, like "Church Street", or null.
  - `letter`: the letter or number on its sign from NaPTAN's indicator ("Stop A" is `A`, "Stand 12" is `12`), or null where the indicator is a word like "opp" or there is none.
  - `towards`: the name of the stop buses call at next, the one most timetabled journeys from this stop go on to (a next stop with this stop's own name doesn't count), or null.
  - `walk_min`: the walk to it in whole minutes, as on its rows, or null.
  - The page names each row's stop in its Stop column with its walk, telling stops of the same name apart by `letter`, else `towards`, else the label: "Church Street A · 10 min walk", "Victoria Street towards Market Hall".
- **`buses.last_buses`:** each line and destination's last bus today from your stops, from the timetable (`sources/lastbus.py`), soonest first; `[]` while there is no usable timetable. Line and destination are as the rows say them, and only the board's `directions` count.
  - A day is the timetable's service day: a bus at 00:20 on a journey that started the evening before is that evening's last bus, and so is one before 03:00 that the timetable gives the next day. After midnight a line shows the evening's last bus while it is still to come, then today's. Bank holidays and days of the week follow the timetable's own rules.
  - `stop` is the label of your stop the last bus leaves from; `scheduled` and `scheduled_at` its timetabled time.
  - `remaining` counts that line and destination's buses still to come that service day, the last included (a bus counts until a minute after its time, as rows do); 0 once the last has gone.
  - `show` is true from 19:00, or once `remaining` is 3 or fewer.
  - **On the page:** those with `show` true are listed under the bus table, "Last 341 to Hyde Bus Station 23:05", with ", gone" when `remaining` is 0. A bus's sheet always says when its line and destination's last bus goes, under **Last bus**: its time, the stop when it leaves from another of yours, or "This is the last today".
- **`walk`:** the walk times as set, by mode and stop label (`*` for any stop). The page reads `walk.train` for the trains' walk line; `walk_min` on rows and stops supersedes the rest.
- **`quiet`:** true while one of the dashboard's quiet windows (`quiet_windows`, bank holidays counted as Sundays) is on, else false. It is worked out once a minute, so it can be up to a minute behind the window. The wall-tablet page (`/?kiosk`) dims while it is true.
- **`row_strips`:** whether the page draws each row's position strip (`[dashboard] row_strips`, **Position strips in each row** in the settings). When false, rows keep their words without the drawing, and only the first train row says where its train is. A snapshot without it, from an older server, counts as true. The wall-tablet page never draws them.
- **`show`:** what the page shows beside the departures' times (`[dashboard] show`, **What to show** in the settings; see [What each view shows](configuration.md#what-each-view-shows)), each named once, in this order: `next_to_catch` (the hero), `where` (each row's where-words and strip), `disruptions` (the summary, the bus rows' links and their announcements), `weather` (the panel and the hero's rain sentence), `last_buses` (the list under the buses and a bus sheet's **Last bus**), `area_map` (Around here: the line and the buses nearby, which isn't fetched then), `remind` (Remind me, a row's bell and the reminders), `calling_at` (the first train's line), `coaches` (a train sheet's coaches and count), `punctuality` (a row's "Often late", the sheet's punctuality, which isn't fetched then, and a bus sheet's **Tracker**), `performance` (the header's link; `/performance` says it is left out), `fleet_numbers` (a bus sheet's **Bus**), `announce` (a train sheet's **Announce** and the wall tablet's announcements). The page leaves out whatever isn't named, on the wall tablet too, and leaves no gap. A snapshot without it, from an older server, shows everything. The rest of the snapshot is the same whatever it says.
- **`stop_name`:** the stop's name from the stop data with the letter on its sign when known ("Church Street (Stop A)"); "Stop A" from the board's label when the stop has no data. Show this rather than the label.
- **`strip`:**
  - `stops` are the stop names before yours, then yours, then possibly the next one.
  - `yours` is the index of your stop.
  - `bus_at` is the bus's position in stop units along `stops` (0 is the first). It is null when the bus isn't on the strip yet (as a bus followed in on the journey before isn't: see `inbound`), and when there's no live bus.
  - `where` is a sentence for screen readers and the row.
- **Bus `vehicle`** (null without a live position): `age_s` is how old its last report was, `recorded_at` when it was made, and `bearing` the way it faces in degrees, or null.
  - `fleet_number` and `registration` say which bus it is, read from BODS's `VehicleRef`: a fleet number of 1 to 6 digits ("3017"), and a registration spaced as on the plate ("AB25 CDE"). Each is null when the ref doesn't give one. Around Glossop it comes as `3017_-_AB25_CDE`, `AB25CDE`, `AB25_CDE` or `3017`. An older plate is read only as written, with its space or an underscore for it: one to four letters and digits either side, with both letters and digits in it (`R123_ABC`, `ABC 123D`, `X 1`). Anything else, anything with a character outside plain ASCII, or a ref longer than 32 characters gives neither, and the ref itself is never sent.
  - **On the page:** the sheet's **Bus** fact says "3017 (AB25 CDE)", or either alone. With a registration it links to `https://bustimes.org/search?q=AB25CDE` (bustimes.org's own search finds a vehicle by its registration; there is no stable address for one operator's fleet number), with `rel="noopener noreferrer"` and words saying it leaves the app. Rows and the board don't show it.
- **`status.stale`** is true when nothing new has arrived for longer than expected. `error` is a short message with any keys removed.
- **`positions`** says how asking where the vehicles are is going, in the same shape as `status`; the settings site's Overview shows it, and the page doesn't use it.
  - `trains.positions` is for Darwin's Service Details (`RDM_DETAILS_API_KEY`): `last_success` is when a train's details last came, `error` the last failure, and `note` is "Not asked again for 6 hours" while a refused key has turned them off. It is null without the key.
  - `buses.positions` is for the live bus positions: `last_success` is when they last came, and `error` says why the last try failed (the rows then show timetabled times, as `status.note` says). It is null when the buses come from TransportAPI, which the dashboard doesn't show.

### Row extras

Best guesses added on top of a row (worked out in `enrich.py`). They never change the row's own times, `status` or `status_text`; only the bus coming in to work it does (`inbound`, worked out with the row).

- **`arrives`** (trains and buses; null when not known): where and when it gets there.
  - `name` is the place, `at` the time (`HH:MM`), `at_iso` the same with the board's offset, and `minutes` the journey time from this station or stop, counted from the expected departure (else the scheduled one). It crosses midnight correctly.
  - **Trains:** the arrival at the filter station (`trains.filter`) if the train calls there, else at its last calling point. Darwin's expected time is used when it is a time, otherwise the booked time ("On time", "No report"). It is null when the train or that stop is cancelled, when the stop says "Delayed", or when the train shows "Delayed" here: the booked time is known to be wrong and the right one isn't known.
  - **Buses:** the journey's last call (`name` is the bus's destination), at the timetabled time plus the delay the bus has now (`expected_at - scheduled_at`, 0 without a live bus).
- **`inbound`** (buses; otherwise null): the bus coming in to work a departure that starts at your stop, on the journey before, while the departure's own journey has no live bus placed on it. It is found in `livebus.py`, one of two ways:
  - **Followed** (`followed` true): where the timetable gives blocks (TransXChange's BlockNumber; many operators give none), the journey just before this one in its block, by the same operator on the same service day, ending at your stop or within 150 m of it, 0–30 min before this one leaves. Its bus is the one placed on that journey, or else the one bus matched to no journey that sends that block as its SIRI-VM BlockRef. If the block's bus isn't seen, there is no `inbound` and the departure is timetable only.
  - **Guessed** (`followed` false), without a block to follow, the way bustimes.org does without blocks: each bus placed on a journey goes to the earliest departure it can make of the same line and operator, from your stop where that journey ends (or within 150 m of it), on the same service day, leaving 0–30 min after the journey is due in, with no live bus of its own and no block to follow. A bus whose block goes on to another journey is left to that journey, and a departure first in its block, whose bus comes out of the depot, gets no guess. A departure gets one only when exactly one bus goes to it (two is too much of a guess), and not when another departure of that line leaves at the same time. The timetable keeps journeys that end near your stop without calling at it, and every stop of the journeys of lines that turn round there, for both ways.
  - Either way the departure is then live (`live` true, `vehicle` is that bus), the same to the rider: expected 2 minutes after that bus gets in, never before its time, so a late inbound bus makes it late. A bus already in leaves on time, or, its time gone, any minute, for up to 30 minutes after its time; still in after that, still on the journey before, it has laid over or gone out of service, so it is let go and the departure goes, as one with no bus seen would. The punctuality history records no delay for a departure whose bus is only waiting in: it says nothing of when it goes. `strip.where` says "Bus on its way, due in 6 min", then "Bus in, waiting to leave", and `strip.bus_at` is null. Only `followed` tells them apart.
  - `from` is where that journey started (null if no name is known), `due` its expected arrival at your stop (`HH:MM`), `late_min` how late it is now (0 if on time or early), `where` where it is ("4 stops away", "At your stop"), and `text` all of it as one line for the row, like "Bus arriving from Ashton, 4 min late (due 23:29)".
- **`ghost`** (buses; always a boolean): true when the bus is due within 5 minutes (or just past its time) with no live bus placed on its journey (`live` false). `status_text` stays "Timetable only". It is false while live positions are failing (`status.note` says so) and when `inbound` is set, since a tracked bus is on its way to work it.
- **`history.phrase`** (null when there's nothing to say): how this departure ran on recent days of the same kind, like "Late 4 of the last 10 weekdays (avg 6 min)", the same words as `/api/history/<id>`. The page shows it in the row only when it is notable, as "Often late: 4 of 10" (at least 3 days, and at least a third of them, late or cancelled). It reads those counts from the words, so a change to their wording in `history.py` needs `recordNote` in `board.js` to follow; `tests/dashboard/test_static.py` checks the two agree.
- **`history.no_show_phrase`** (buses only; null when there's nothing to say): how often this departure had no tracker before it left, like "No tracker on 3 of the last 10 weekdays", over the same days as `phrase`. It is null with fewer than 3 days recorded or when every day had a tracker. Train rows' `history` has `phrase` only.

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
    "relevance": "line",
    "link": null
  }]
}
```

- **Sources:**
  - `bus`: BODS SIRI-SX (the buses' `BODS_API_KEY`), polled every 10 minutes. `null` without buses. `note` is "Waiting for the bus timetable" until the timetable says which lines and stops are ours.
  - `train`: the National Rail Knowledgebase's incidents through the Rail Data Marketplace (`RDM_KB_API_KEY`, at `[dashboard] kb_incidents_url`), polled every 30 minutes. `null` without the key or without the address (the settings site and the log say which). `note` is "Waiting for the train board" until a board names our operators.
- **Which are kept** (see `disruptions.py`): valid now or in the next 7 days, and:
  - buses, by `relevance`:
    - `your_stop`: at one of our own stops, whatever its lines;
    - `nearby`: at a positioned stop our journeys use near our stops, if that part of the notice names one of our lines or no lines at all;
    - `line`: on our line *with* the operator our timetable has for it, or "all lines" of one of our operators, with an affected stop inside our area (about 1 km around the positioned stops); or on our line with no stops named at all (a withdrawn or curtailed service).
    - Nothing else: a notice on our line whose stops are all outside our area (roadworks at the far end of the route), and "all lines" with no affected stop in our area, are left out. Only the parts of a notice in our area give its `lines` and `places`.
  - trains: names one of the operators running our trains, and mentions one of our stations (ours, the filter station, or any our trains call at) by name or three-letter code. `relevance` is `your_stop` when it mentions our own station, else `line`.
- **Items:** buses first, then trains, each most relevant first, then those in force now, then the most severe (`unknown` counts as `normal`), then the soonest; at most 30 of each.
  - `id` is 16 hex characters, stable for the same notice; the feed's own number stays on the server.
  - `title`, `summary` (at most 500 characters) and `reason` are plain text, with any HTML in the feed dropped.
  - `periods` are the notice's validity periods that touch now to 7 days ahead, in order (at most 14). `from_at` and `to_at` repeat the first. `to_at` is `null` when open-ended.
  - `planned` is the feed's own word: engineering works and booked roadworks are planned.
  - `lines` are our lines it affects (bus only); `stops` our stop labels it names, as in `buses.stops` (bus only); `places` stop or station names, in words.
  - `severity` is `slight`, `normal`, `severe` or `unknown`; always shown in words.
  - `relevance` is `your_stop`, `nearby` or `line`, most relevant first (see above).
  - `link` is `null` unless the feed gave an https link on a known operator's site (`disruptions.LINK_HOSTS`).
- **On the page:** one line under the hero names the first item in force at or near our stops (`your_stop` or `nearby`), or a rail service message, and how many more there are; opening it lists them all, in force first, then coming up (folded), each most relevant first, in words. Rail service `messages` join it. A bus row whose line or stop an item names, during one of its periods, gets a "Disruption" link that opens the line at it.

## Snapshot: `timetable_changes`

A top-level key of `/api/snapshot`, after `disruptions`: the bus lines whose timetable changes in the next 28 days. `null` without buses or a usable timetable; `[]` when nothing changes.

```json
"timetable_changes": [{"line": "341", "operator": "BNML", "from": "2026-10-25"}]
```

- **The rule** (`area.timetable_changes`): the bus timetable keeps journeys whose calendar starts in the future (they never run before their start). A day D in the next 28 days is looked at if a journey starts on D or ended the day before. A line (with its operator) changes on D when the set of its journeys running on D, by their stops and times, differs from the set that ran on the same weekday a week before: new journeys, ended ones, or new times. Each line is listed once, at its first change, soonest first; at most 20. Bank holidays aren't counted. Journeys kept only to find an inbound bus give no departures, so they don't count either.
- It is worked out again only when the timetable is read again or the day changes.
- `from` is `YYYY-MM-DD`, the first day of the new timetable.
- **On the page:** a notice above the bus table, such as "New 341 timetable from Sun 25 Oct."

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
  "vehicle": {"lat": 53.4438, "lon": -1.9648, "bearing": 95.0, "recorded_at": "…", "age_s": 40,
              "fleet_number": "3017", "registration": "AB25 CDE"},
  "where": "2 stops away"
}
```

- `stops` are the journey's positioned calls: up to 20 before yours, yours, and one after, in order. `time` is the timetabled time.
- `path` is the road through those stops, first to last, as `[lat, lon]` pairs: each stop, then the road to the next. The roads come from the OpenStreetMap car router when the timetable is built (`timetable/roads.py`), never when the map is asked for; where it had none, the path goes straight from one stop to the next. It has as many points as `stops` at least, so `[]` when `stops` is empty.
- `vehicle` is null when the bus has no live position. Its `fleet_number` and `registration` are as in the snapshot's rows. For a bus followed in on the journey before (see `inbound`), it is that bus, where it is now, off the drawn stops until it turns round.

## `GET /api/history/<id>`

The punctuality history of a train or bus row (same ids as the snapshot); 404 if the id is unknown.

```json
{
  "id": "9f1c…", "mode": "train",
  "label": "07:12 to Manchester Piccadilly",
  "day_type": "weekday",
  "phrase": "Late 4 of the last 10 weekdays (avg 6 min)",
  "days": [{"day": "2026-10-09", "status": "late", "delay_min": 7, "live": true, "reason": null}],
  "route": {
    "label": "Manchester Piccadilly", "stop": null, "late_minutes": 3,
    "periods": [
      {"span": 7, "since": "2026-10-04", "enough": true, "days": 6, "counted": 100, "right_time": 51,
       "within_3": 75, "within_15": 88, "on_time": 75, "cancelled": 3, "unknown": 2},
      {"span": 28, "since": "2026-09-13", "enough": true, "days": 27, "counted": 442, "…": "…"}
    ]
  }
}
```

- `days` are newest first, at most `[dashboard] history_days` of them (1 to 3650, default 90).
- `route` is the figures of every departure on the row's route, whatever its time (every train to Manchester Piccadilly; every 341 to Hyde Bus Station from stop A), as one route in [`GET /api/performance`](#get-apiperformance), with the mode's `late_minutes`. `null` when the server has no history to read.

## `GET /api/performance`

The Performance page's figures: how each regular route, and all the trains or all the buses together, ran over the last 7 and 28 days, today included, and the trains that reached the filter station late enough for Delay Repay. Worked out from the punctuality history each time it is asked for; 404 when the server has no history to read, 503 when it can't read it. Same [access rules](#access-rules-every-request) and headers as the rest of the API.

```json
{
  "generated_at": "2026-10-10T07:01:30+01:00",
  "trains": {
    "late_minutes": 3,
    "all": {"periods": [{"span": 7, "since": "2026-10-04", "enough": true, "days": 6, "counted": 102,
                          "right_time": 52, "within_3": 76, "within_15": 89, "on_time": 76, "cancelled": 3,
                          "unknown": 2}, {"span": 28, "…": "…"}]},
    "routes": [
      {"label": "Hadfield", "stop": null, "periods": [{"span": 7, "enough": false, "days": 2, "…": "…"}, {"span": 28, "…": "…"}]},
      {"label": "Manchester Piccadilly", "stop": null, "periods": ["…"]}
    ]
  },
  "buses": {
    "late_minutes": 2,
    "all": {"periods": ["…"]},
    "routes": [{"label": "341 to Hyde Bus Station", "stop": "A", "periods": ["…"]}]
  },
  "delay_repay": {
    "station": "MAN", "station_name": "Manchester Piccadilly", "since": "2026-09-13", "least_minutes": 15,
    "off": null,
    "trains": [
      {"day": "2026-10-08", "scheduled": "07:42", "destination": "Manchester Piccadilly", "operator": "Northern",
       "booked": "08:05", "arrived": "08:38", "minutes_late": 33, "band": 30, "reported": true}
    ]
  }
}
```

- **Routes** are the history's regular departures without their times: a train's destination (`stop` null; rail replacement buses and ferries apart, as "Manchester Piccadilly (replacement bus)" and "Ryde Pier Head (ferry)"), or a bus's line and destination from one of your stops (`stop` is its label, as in the snapshot's `buses.stops`). At most 50 per mode, the busiest, listed by name. `all` counts every departure, listed or not.
- **`periods`**: one per span, in order: `span` days (7, then 28) from `since` to today. Only departures whose outcome is final count (see `dashboard/history.py`).
- **The counts:** `days` with any departure recorded; `unknown`, gone with no time to tell how late (a train shown only as "Delayed", a bus with no tracker), left out of the rest; `counted`, the others, cancelled ones included, which every measure is out of. `right_time` left at or before its booked time; `within_3` (T-3) and `within_15` (T-15) under 3 and under 15 minutes late (whole minutes: 2 and 14 or less); `on_time` under `late_minutes` late (`[dashboard] late_minutes_train` or `late_minutes_bus`); `cancelled`. Early counts as right time.
- **`delay_repay`**: the trains that reached the filter station (`[train] filter_crs`, `station`; `station_name` as Darwin names it, null until one is recorded) at least `least_minutes` (15) late over the 28 days from `since`, newest first, at most 100. `day` and `scheduled` are when it was booked to leave your station; `booked` and `arrived` are HH:MM at the filter station (the next day for one there after midnight); `minutes_late` is whole minutes as they passed, across the clocks changing too (booked 01:50 BST, there 01:10 GMT, is 20); `band` is 30 for 30 minutes or more, else 15, the thresholds train companies pay Delay Repay from. `reported` is true when Darwin reported the arrival time, false when it is the last estimate before the train got there. Cancelled trains, rail replacement buses and ferries aren't listed. `off` says why there is no list: `"no_trains"`, `"no_filter"` (trains, but no filter station) or `"no_details_key"` (no `RDM_DETAILS_API_KEY`, which it needs to follow a train once it has left); null when it is on. How the arrivals are found is in `dashboard/repay.py`: from the board's calling point while the train is on it, then, only while it is due there, from Service Details, one ask per train per train poll.
- **`enough`** is false with fewer than 3 `days`: the page says "Not enough days yet" rather than a percentage.
- **On the page** (`/performance`, linked from the dashboard's header, not in the kiosk): for each mode a table per span, the total first, then each route. Each cell is the share of `counted` with the count, "62% (31 of 50)", rounded, but never to 100% or 0% unless all or none. Trains show Right Time, T-3, T-15, Cancelled and Not known; buses show On time, T-15 and No tracker, as bus data never says a bus was cancelled. Each departure's sheet shows its `route` the same way. Then **Delay Repay**: a table of `delay_repay.trains` (date, train, booked and arrival times, minutes late, band, operator) with a link to National Rail's compensation page, or why it is off (for `no_details_key`, with a link to the key's guide in `docs/keys.md`).

## `GET /healthz`

While the dashboard is on: 200 `ok` when the pollers are alive and the snapshot was rebuilt in the last 3 minutes; otherwise 503. On a Pi the pollers run in the fetch service, which hands the snapshot to the web server with its own health; it counts only while that snapshot is under 3 minutes old. While the dashboard is off: always 200 `ok`.

## Static files

- `/`: the page (`index.html`).
- `/performance`: the Performance page (`performance.html`), with the same headers and caching.
- `/static/<path>`: files from `src/twobells/dashboard_static/`, from an allowlist built at start.
- `/manifest.webmanifest`.

## Access rules (every request)

Checked in this order, before anything else. Each refusal has a plain-text body and the security headers below.

- **403** unless the client's address is private, loopback or link-local, the request has exactly one `Host` header, and that host is local or in `[dashboard] allowed_hosts`. Local means a private IP address, a name without dots, or a name ending `.local`, `.lan` or `.home.arpa`. This stops DNS rebinding.
- Then everything under `/settings` goes to the settings site, which has its own rules (see the top of this page).
- **405** with `Allow: GET, HEAD` for any other method.
- **404** when the request target is over 512 characters, doesn't start with `/`, or holds `..`, a backslash, or an encoded dot, slash, backslash or NUL (`%2e`, `%2f`, `%5c`, `%00`, in any case).
- While the dashboard is off, the rest is answered as at the top of this page.
- **403** for `/api/` when `Sec-Fetch-Site` is `cross-site`: another site's page can't read the API through the browser.

## Security headers (every response while the dashboard is on)

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
```

`img-src data:` is there for Leaflet's inline marker images. No inline scripts or styles: style changes are made from JS through the CSSOM (`el.style.x = …`), which CSP allows.

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
- `box` is west, south, east, north around the timetable's positioned stops (about 1 km wider), or `null`. Only buses inside it, on lines our timetable has, reported in the last 10 minutes, are sent; at most 80.
- `destination` is the timetabled journey's when the bus was matched to one, else the bus's own. `next_stop` is the next positioned stop on that journey, or `null` when it wasn't matched or the stop isn't positioned. `bearing` and `recorded_at` can be `null`.
- `stops` are our stops that have a position, by label.
- `routes`: for each line and destination, `path`: the road through the positioned stops of its longest journey, in order, as `[lat, lon]` pairs (at least two, every stop among them), as for `/api/bus/<id>`: straight between stops the timetable has no road for. At most 40. Only journeys calling at your stops are drawn, not those kept only to find an inbound bus.
- **On the page:** "Around here" has an **Area map** button opening a dialog with the map (line numbers on the bus markers, words in the popups), real zoom and "Show the whole area" buttons, the OpenStreetMap credit, and the same buses listed in words, each with a "Show on map" button. It also lists the stations on the train line in order, from the rows' calling points (and `previous_points`), with each train that has a `position` placed at or between stations, in words.

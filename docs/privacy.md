# What leaves your home

Every outside service Two Bells talks to, what each is sent, and when. [Getting your keys](keys.md) says how to get the keys, [How the web server is kept safe](security.md) how a Pi's web server is kept off the internet, and [Backing up and moving](backup.md) what is kept at home.

- [In short](#in-short)
- [Who asks, on each install](#who-asks-on-each-install)
- [Setting up](#setting-up)
- [Departures and the rest](#departures-and-the-rest)
- [Timetables](#timetables)
- [Maps](#maps)

## In short

- It talks to the services below and nothing else: no analytics, no adverts, no update checks. It asks only those your settings use.
- Your home's exact position leaves only while you set up, to find stops and walk times. The app keeps it only to two decimal places (about 1 km), which is all the weather service is sent, and keeps no postcode or address.
- Each key goes only to the service it is for.
- Every service is asked over HTTPS. Each sees the internet address of the machine asking, as any website does, and a user agent naming Two Bells.
- Testing a key on the Keys page, or in the wizard, asks its service once, for as little as it can.

## Who asks, on each install

**On a Pi** the work is split between services, and the web server never connects out:

- **The board** (`twobells.service` for e-paper, `twobells-screen.service` for a screen): the trains, buses, London's metro, the rain warning and the bank holidays.
- **The fetch service** (`twobells-fetch.service`): the setup wizard's lookups, key tests and bus company search, and the dashboard's feeds while its view is on.
- **The timetable service** (`twobells-timetable.service`): the bus and tram timetables, about once a week, and the bank holidays.
- **The web server** (`twobells-web.service`): nothing outside your home network. Its network guard stops it (see [How the web server is kept safe](security.md)).

**In Docker and on a desktop** the one `twobells serve` process does all of it: the wizard's lookups and key tests, the dashboard's feeds, and building the timetables.

**On every install** your browser fetches the map pictures itself, only when you open a map (see [Maps](#maps)).

The commands you run by hand (`probe`, `route`, `emulate`, `timetable`, `setup discover`) ask the same services, from the computer you run them on. A Pi's `upgrade.sh` downloads the release you name from GitHub, only when you run it.

## Setting up

Only the setup wizard asks these, and only while you use it:

| Service | What for | What it is sent |
|---|---|---|
| [postcodes.io](https://postcodes.io) | Finding home from a postcode | The postcode you type. |
| OpenStreetMap's [Nominatim](https://nominatim.org) | Finding home from an address | The address you type. |
| The OpenStreetMap foot router ([FOSSGIS](https://routing.openstreetmap.de/about.html)) | Walk times | Home's position, with your station's and stops' positions, in one request. |
| NaPTAN (the Department for Transport) | Bus stops near home | The codes of the council areas around home (the first three characters of a stop's code), never a position. |
| The Bus Open Data Service (BODS) | Which bus companies serve your stops | The codes of your stops' council areas, and `BODS_API_KEY`, in the address. It downloads their timetable files and reads them at home. |

The stations, the Metrolink tram stops and London's stops come from lists that come with the app, so finding them sends nothing. The wizard saves home's position to two decimal places, for the weather, and doesn't save the postcode or address.

## Departures and the rest

These are asked again and again, as often as the settings say: the board inside its checking times, and the dashboard all day but its quiet hours (01:00 to 05:00 unless you change them), when it asks only while someone has the page open.

| Service | What for | What it is sent |
|---|---|---|
| Rail Data Marketplace: Live Departure Board (National Rail's Darwin) | Trains | Your station's code, the filter station's if you set one, and how many trains to list. `RDM_API_KEY`, in a header. |
| Rail Data Marketplace: Service Details | Where each train is, on the dashboard and a screen, and Delay Repay | The ID Darwin gave a train: the first few on the board, and, for Delay Repay, those that have left until they reach the filter station. `RDM_DETAILS_API_KEY`, in a header. Only with that key. |
| Rail Data Marketplace: Knowledgebase Incidents | Engineering works, on the dashboard | Nothing but `RDM_KB_API_KEY`, in a header. It reads the whole list every 30 minutes and keeps what touches your trains. Only with the key and its address. |
| BODS: live positions | Live bus times, and the bus maps | A box around your stops and the stops your buses call at on their way. `BODS_API_KEY`, in the address. |
| BODS: disruptions | Bus disruptions, on the dashboard | Nothing but `BODS_API_KEY`, in the address. It reads England's whole list every 10 minutes and keeps what touches your stops and lines. |
| TransportAPI | Bus times, where you have its keys: standing in until the BODS timetable is built, or alone with `source = "transportapi"` | Your stops' codes, one lookup each. `TAPI_APP_ID` and `TAPI_APP_KEY`, in the address. Within its daily budget, 28 of the free plan's 30 lookups unless you change it. |
| TfL's Unified API | London's Underground, DLR, trams, cable car and river buses | The codes of your London stops and the lines calling there. `TFL_APP_KEY`, in a header, if you have one. |
| [Open-Meteo](https://open-meteo.com) | The rain warning, and the dashboard's weather | The position in `[weather]` to two decimal places (about 1 km), and your time zone. About every 30 minutes, only while a view shows the weather. |
| GOV.UK | Bank holidays | Nothing: it downloads the list, at most once a week. |

The dashboard doesn't ask for what its **What to show** leaves out: the weather, the disruptions, where the trains are and Delay Repay.

## Timetables

Built about once a week, and again when your stops change. On a Pi the timetable service builds them, and the wizard's bus company search builds the bus one too.

| Service | What for | What it is sent |
|---|---|---|
| BODS: timetables | The bus timetable for your stops | The codes of the bus companies serving them, and `BODS_API_KEY`, in the address. It downloads their whole timetable files and keeps your stops' part. |
| NaPTAN | Stop names and positions | The codes of your stops' council areas. |
| The OpenStreetMap car router (FOSSGIS) | The roads between stops, for the bus maps | The positions of two stops your buses call at one after the other, a pair at a time, each pair once. Never home. A pair whose road the last timetable has isn't sent again. |
| Transport for Greater Manchester | Metrolink's tram timetable, where you have tram stops | Nothing: it downloads the whole file, about 45 MB, and keeps your tram stops' part. |

## Maps

The maps load their pictures from OpenStreetMap's tile server, `tile.openstreetmap.org`. Your browser asks, not Two Bells, and only when you open a map: a bus's map or the area map on the dashboard, or **Show the map** in the setup wizard. OpenStreetMap sees the squares of map on screen, so roughly where you are looking (in the wizard, around home), and the address you opened Two Bells at, such as `http://raspberrypi.local:8090/`, as its tile policy asks. Nothing else on the pages is loaded from outside.

The dashboard links to bustimes.org (a bus's registration) and National Rail (Delay Repay). Nothing goes to them unless you follow the link.

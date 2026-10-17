# London's Underground, DLR, trams and river buses

Where home is in London, the board and the dashboard can show the Underground, the DLR, London Trams, the IFS Cloud Cable Car and the river buses, live from Transport for London (TfL), in the same section as Manchester's trams (see [Tram times](trams.md)). The section is named for the stops you choose: **Underground**, **DLR**, **Trams**, **Cable car** or **River buses** when they are all of one kind, and **Metro** for a mix.

- [Where the times come from](#where-the-times-come-from)
- [Setting it up](#setting-it-up)
- [What it shows](#what-it-shows)
- [How often it asks](#how-often-it-asks)

## Where the times come from

TfL's [Unified API](https://api.tfl.gov.uk) gives the predictions its own station screens show. It is free, and works without a key; an optional key, `TFL_APP_KEY`, lets it ask more often (see [Getting your keys](keys.md#transport-for-london)). The dashboard says "London times powered by TfL Open Data" at its foot while it shows them, as TfL's terms ask.

- **Live, with no timetable.** TfL gives when each train is expected, never a booked time, so a row is live or it isn't there. There is nothing to be late against, so London's rows have no "Exp", no "late" and no punctuality record, and nothing has to be built or downloaded first.
- **Only these modes.** London Overground and the Elizabeth line are trains, from National Rail's Darwin, so set them up as your station; London's buses come from the Bus Open Data Service, as your bus stops. Their predictions are left out of this section.
- **The cable car** runs every few minutes with no times at all, so a cable car station shows only its line's status.
- **Checked:** every field TfL sends is checked as it is read, for its type, shape, length and range. A prediction with a wrong field, for another mode, ending at your stop, or hours off is left out. A train listed at both platforms it opens on (as the DLR does) is one row.

## Setting it up

The [setup wizard](setup-wizard.md#the-steps) asks for your stops after the bus stops: where home is within 5 km of one, it lists the 8 nearest Underground and DLR stations, tram stops, cable car stations and piers, and **Find a stop by name** finds any of them from anywhere, from a list that comes with the app, so nothing is sent anywhere to find them. Tick up to 4. Each gets a label, T, U, V and W, and a walk time. Once you have ticked some, the **Keys** step offers TfL's key, which you can leave empty.

In `config.toml` it looks like this:

```toml
[metro]
directions = ["Epping", "Lewisham"]   # optional: only those heading to these
[metro.stops]
T = "940GZZLUBNK"                      # Bank, Underground
U = "940GZZDLBNK"                      # Bank, DLR
[metro.walk_minutes]
T = 4
U = 5
```

A stop's code is TfL's: `940GZZ`, then `LU` (Underground), `DL` (DLR), `CR` (London Trams) or `AL` (cable car) and three letters; or `930G` and three or four letters for a pier. `src/twobells/data/london_stops.csv` lists every one, with the TfL lines that call there. Up to 4 stops, all London's: Manchester's and London's can't share a board. The settings' **Trams** sections work as they do for Metrolink (see [Tram times](trams.md#setting-it-up)).

## What it shows

- **On the board:** each train's expected time, where it goes, and its platform on a board with room for one. On e-paper the status column says `Live`, as the panel never redraws just to count down; an HDMI screen counts down instead (`3 min`, `Due`). With more than one line among the rows, each names its line first ("Central: Epping"); with one line at two stops, its stop's label ("T: Epping"). The section says when to leave for the next you can reach, by its stop's walk. A line at your stops not running well (TfL's status, like "Minor Delays") is the board's notice, as National Rail's messages are for trains.
- **On the dashboard:** a countdown with the live mark, as a live bus has; where it goes, with "via Bank" where TfL says so; its line with the line's colour beside the name, which way and the platform ("Central line · Eastbound, platform 6"); and where it is now ("Between Chancery Lane and St. Paul's", while **Where it is** is shown). Next to catch takes them as live. A line not running well joins the disruption line under it, under the section's heading ("Underground: Minor delays…"); a lift or escalator out of order at one of your stations is a quiet **Station information** line in the section. A row's sheet has its line, platform, stop, expected time, where it is and when to leave, and says why there is no punctuality.

## How often it asks

Without a key TfL allows 50 calls a minute from one internet address, which is plenty:

- **The board** asks once a minute through its checking times (05:00 to midnight by default): one call for each stop, and one for the lines' status. Four stops are five calls a minute.
- **The dashboard** asks every 30 seconds for each stop, and every 5 minutes for the lines' status and the stations' lifts and escalators. On a Pi it is the fetch service that asks, as for every feed; in Docker and on a desktop, the dashboard itself.

A board and a dashboard on four stops make about 13 calls a minute. A key raises TfL's allowance to 500 a minute, should anything else on your network use TfL too. The key goes in a header, never in an address, so no log or message can show it.

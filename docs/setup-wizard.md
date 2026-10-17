# First run and the setup wizard

Until it is set up, the board shows **Set me up** and the settings address, and the dashboard's address opens the settings. No `config.toml` is needed: the wizard saves everything for you.

## Claim the settings

1. Open the settings: `http://<pi name>.local:8090/settings` on a Pi, `http://<NAS name>:8090/settings` in Docker; a desktop opens it for you.
2. Claim them with a 6-digit code and a new password (see [Password and reset codes](password.md)). On a Pi with the board running, the code shows on the board; otherwise it goes to the log.
3. The setup wizard opens.

## The steps

1. **Home:** type your postcode, or search for an address, and check the place it finds. A map is there if you want it, but every step works without one.
2. **Station:** pick from the stations nearest home, type another station's code, or choose no trains. Optionally, say where you usually go (like `MAN`) to show only trains calling there.
3. **Bus stops:** tick the stops you use, up to 10. Each gets a short label for the board; the letter on the stop's sign is suggested.
   - **Tram stops:** those within 5 km of home, Manchester Metrolink stops or London Underground, DLR, tram and cable car stations and river bus piers, and any other found with **Find a stop by name**, however far away. Tick up to 4, or none. They come from lists that come with the app, so finding them sends home nowhere. See [Tram times](trams.md) and [London](london.md).
4. **Walk times:** worked out from the walking route to each; change any that are wrong. A place over an hour's walk away is left empty, marked **Over an hour**, so no time to leave is shown for it; type one if you like.
5. **Keys:** paste the keys every view uses: the Rail Data Marketplace key for trains and the BODS key for buses, and, with London stops ticked, TfL's optional key. Each has **How to get this key** and a **Test this key** button. See [Keys](keys.md). Keys only one view uses are on the settings' Keys page.
   - **Tram stops** (only where home is within 5 km of a Manchester Metrolink stop, or of a London Underground, DLR, tram or cable car station or river bus pier, or where tram stops are saved already, so the steps after it are numbered one more): it lists the 5 nearest Metrolink stops and the 8 nearest London ones; tick up to 4, all Metrolink or all London, or none. They come from lists that come with the app, so finding them sends home nowhere. See [Tram times](trams.md) and [London](london.md).
4. **Walk times:** worked out from the walking route to each; change any that are wrong.
5. **Keys:** paste the keys every view uses: the Rail Data Marketplace key for trains and the BODS key for buses, and, with London stops ticked or a TfL key already saved, TfL's optional key. Each has **How to get this key** and a **Test this key** button. See [Keys](keys.md). Keys only one view uses are on the settings' Keys page.
6. **Views** (a Pi only): the board, the dashboard, or both. The board is ticked at first. Docker and a desktop have only the dashboard, so they skip this step.
7. **What to show:** with two or more of a station, bus stops and tram stops, first which of trains, buses and trams each view you turned on shows, all ticked at first that fit; the board says how many its display has room for in its look and text size (see [Modes of transport](configuration.md#modes-of-transport)). Then, for each view, how much it shows beside the departures' times, which always show: **Just the times**, **The usual** (picked at first) or **Everything**. Each says what it adds. To choose item by item, use **What to show** on the settings' [Board](settings.md#board) and [Dashboard](settings.md#dashboard) pages, now or later.
8. **Check and save:** with bus stops, saving first finds the bus companies serving them and builds their timetable, which can take a few minutes; leave the page open, or cancel. You can also tick **Also draw each bus line's route on the screen's map**, the route strips for the screen's bus page (slower to set up). Once the timetable is built, the roads for the dashboard's bus maps are found, for up to 5 minutes more, with a progress bar: press **Wait for the roads** to save once they are found, or **Carry on** to save now and let them finish on their own. Until then the maps draw straight lines between stops, and **Your place** on the settings' Overview says how far they have got. If they are stopped part way (the dashboard starting again with the new settings, say), the next timetable build, within the hour, finds the rest. Metrolink's timetable isn't built here: it is built within the hour after saving (see [Tram times](trams.md#how-the-timetable-is-kept)).

## Running it again

Saving keeps your other settings: the look, rotation, text size and so on. **What to show** starts at the preset that matches what each view shows; where none does (you ticked items one by one, or it was set up before there were presets), **Keep what it shows now** is picked first, and keeps it as it is. Without a choice from this step (an older page still open, say), a new install gets **The usual**. Change anything else on the [settings pages](settings.md) afterwards. Choosing no trains, no bus stops or no tram stops switches them off (shown as **Off** on the Trains and buses page); running setup again and picking a station or stops switches them back on. **Change home, station or stops**, under **Shortcuts** on the Overview, opens the wizard again at any time.

**Changing only the stops, or only the station:** **Change stops only** and **Change station only** on the Overview (`/settings/setup/?step=stops` and `/settings/setup/?step=station`) open the wizard at that step, with today's choices ticked. The stops are the bus stops: the tram stops stay as they are, and are changed by running the whole wizard or under **Tram stop codes** on the Trains and buses page. Then come the walk times, starting from the ones you have, the keys only if the one it needs isn't set, and **Check and save**, which saves only that part: home, the views, what they show and everything else stay as they are. Stops are found near roughly where home is, the position kept for the weather (to about 1 km), and your stops are listed even if they aren't near it; for a search from home exactly, run the whole wizard. Keeping the same stops with the same labels needs no new bus company search. **Back to settings**, on the first step, returns to the settings.

## What leaves your home

Where home goes: your postcode to postcodes.io (or the address you type to OpenStreetMap's Nominatim), then its position once to the OpenStreetMap foot router for walk times. The maps, only if you open them, load pictures from OpenStreetMap, which then sees roughly where home is. The app keeps only a rough position, to about 1 km, for the weather.

The wizard is the one part of the settings that needs JavaScript. The rest works without it; with it, the settings pages also mark what you have changed and ask before you leave with changes not saved.

On a Pi, the wizard's lookups, key tests and bus company search run in a separate service, `twobells-fetch.service`, as the web server never connects out; nothing from setup is written to the SD card. See [How the web server is kept safe](security.md#the-fetch-service). In Docker and on a desktop, the lookups run in the server itself.

## Until it is set up

Until it is set up, the board never fails and restarts: the e-paper timer draws the page once and leaves it, and the screen keeps it up, checking every few seconds whether setup has finished. Either way, the board shows departures within a minute of being set up (a screen restarts itself first, which takes about 10 seconds).

To set it up without the wizard, see [Settings by hand](configuration.md#without-the-wizard).

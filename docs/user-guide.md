# Two Bells user guide

The app shows the next trains from your station and the next buses from your stops, and tells you when to set off so you catch them. The examples in this guide come from Glossop, with trains to Manchester and buses from two stops in the town centre.

One install is one app with two views of the same departures:

- **The board:** an e-paper display or a small HDMI screen on a Raspberry Pi.
- **The dashboard:** a web page with more on it, for a phone or a computer at home.

A Pi can run either view or both. Docker (on a NAS, say) and a desktop computer run the dashboard. Each install has one settings site, at `/settings`, with one password.

## Where it works

- **Trains:** anywhere in Great Britain. Times come from National Rail (Darwin).
- **Buses:** England only. The timetable and live positions come from the Bus Open Data Service (BODS), which covers England's buses.
- **Rain warning:** anywhere. The forecast comes from Open-Meteo.
- **Bank holidays:** England and Wales, Scotland, or Northern Ireland, from GOV.UK.

## Use it

- [Reading the board](reading-the-board.md): what each line means in every look, the screen's bus and clock pages, for everyone in the house.
- [The dashboard](dashboard.md): what's on the web page, the Performance page and Delay Repay, disruptions, a wall tablet, and station announcements.

## Install it

Pick one:

- [On a Raspberry Pi](install-pi.md), for the board, and the dashboard too if you like.
- [In Docker](docker.md), on a NAS or any always-on computer, for the dashboard.
- [On a desktop](desktop.md) (a Mac or a Linux laptop), for the dashboard while it's running.

Then:

- [First run and the setup wizard](setup-wizard.md): claim the settings and find your station, stops and walk times. No `config.toml` is needed.
- [Getting your keys](keys.md): the free keys for trains and buses, step by step.

## Set it up

- [The settings](settings.md): the Overview, Trains and buses, Board, Dashboard, Keys and General pages, with What to show and Scrolling text.
- [Password and reset codes](password.md): claiming the settings, a forgotten password, and why sign-in can refuse.
- [Bus times](buses.md): the BODS timetable, live positions, and TransportAPI as a stand-in.
- [Displays](displays.md): choosing and setting up a display, the screen's bus position page, and its station announcements.
- [Settings by hand](configuration.md): `config.toml`, where things are kept, what each view shows, the rain warning, bank holidays and scrolling text.

## Look after it

- [Looking after a Pi](pi.md): its services, logs, and checking the keys.
- [How the web server is kept safe](security.md): the home-network checks, the Pi's network guard and the fetch service.
- [Commands](commands.md): every `twobells` command and its options.
- [Troubleshooting](troubleshooting.md): what you see, the likely cause, and what to do.

## For developers

- [Contributing](../CONTRIBUTING.md): setting up, the checks, the emulator and releases.
- [Architecture](architecture.md): how the code is laid out.
- [Dashboard API](dashboard-api.md): the JSON and SSE contract between the server and the dashboard page.

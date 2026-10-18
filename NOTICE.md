# Notices

## Data

The board and dashboard use, and the tests include recorded samples of, these open data sources:

| Source | Licence | Used for | Recorded in |
|---|---|---|---|
| [Bus Open Data Service](https://data.bus-data.dft.gov.uk/) (Department for Transport) | [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) | Bus timetables, live positions, disruptions | `tests/fixtures/bods_glossop_live.xml`, `tests/fixtures/bods_siri_sx_glossop.xml`, `tests/fixtures/txc_bnml_*.xml`, `tests/fixtures/txc_hipk_61.xml` |
| [NaPTAN](https://beta-naptan.dft.gov.uk/) (Department for Transport) | Open Government Licence v3.0 | Stop names and positions | `tests/fixtures/naptan_*.csv` |
| [GOV.UK bank holidays](https://www.gov.uk/bank-holidays.json) | Open Government Licence v3.0 | Bank holiday dates | `tests/fixtures/govuk_bank_holidays.json` |
| [Open-Meteo](https://open-meteo.com/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Weather forecasts | `tests/fixtures/openmeteo_*.json` |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors | [ODbL](https://opendatacommons.org/licenses/odbl/); tiles under their [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) | Map tiles on the dashboard | none recorded; `tests/fixtures/osrm_route_*.json` are car router answers made by hand, along a real street in Glossop |

Contains public sector information licensed under the Open Government Licence v3.0. Weather data by Open-Meteo.com. Map data © OpenStreetMap contributors.

Train times come from National Rail's Darwin via the [Rail Data Marketplace](https://raildata.org.uk), under that product's licence. London's Underground, DLR, tram, cable car and river bus times and line status come from [Transport for London's Unified API](https://api.tfl.gov.uk): Powered by TfL Open Data. Contains OS data © Crown copyright and database rights 2016 and Geomni UK Map data © and database rights 2019. Bus times can also come from [TransportAPI](https://www.transportapi.com) under its terms. The tests' Darwin and TransportAPI answers (`tests/fixtures/ldbws_*.json`, `tests/fixtures/tapi_*.json`) are synthetic: made up in the shape those services send, with real station and stop names. So are these:

- `tests/fixtures/kb_incidents_synthetic.xml`: National Rail Knowledgebase incidents, written by hand; the names, numbers and times are made up.
- `tests/fixtures/bods_datasets_synthetic.json`: pages of BODS's data set list, trimmed and made up.
- `tests/fixtures/bods_glossop_synthetic.xml` and `tests/fixtures/txc_glossop_synthetic.xml`: BODS live positions and a TransXChange timetable built for the tests, with real NaPTAN stop codes and positions in Glossop and invented vehicles, journeys and times.
- `tests/fixtures/tfl_*_synthetic.json`: TfL's arrivals, line status and station notices, made up in the shape the Unified API sends, with real station and line names and invented trains and times.
- `tests/fixtures/dashboard_*.json`: the dashboard API's answers ([docs/dashboard-api.md](docs/dashboard-api.md)), made up for both sides of it to test against, with real place names.

## Setup data and lookups

- `src/twobells/data/gb_stations.csv` (stations and CRS codes) and `naptan_area_grid.csv` (which NaPTAN areas have bus stops where) are built from the Department for Transport's [NaPTAN](https://naptan.api.dft.gov.uk/) by `tools/build_setup_data.py`, under the Open Government Licence v3.0. Rebuild them now and then: stations open and stops move.
- `src/twobells/data/london_stops.csv` (London's Underground, DLR, tram, cable car and river bus stops) is built by `tools/build_setup_data.py` from NaPTAN's names and positions, under the Open Government Licence v3.0, and the TfL lines calling at each from TfL's Unified API: Powered by TfL Open Data.
- Setup looks up postcodes with [postcodes.io](https://postcodes.io): Office for National Statistics data under the Open Government Licence; contains OS data © Crown copyright and database right, and Royal Mail data © Royal Mail copyright and database right.
- Addresses come from OpenStreetMap's [Nominatim](https://nominatim.openstreetmap.org) and walk times from the OpenStreetMap [foot router](https://routing.openstreetmap.de). OpenStreetMap data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
- The roads drawn on the dashboard's bus maps come from the OpenStreetMap [car router](https://routing.openstreetmap.de) when the bus timetable is built, and are kept in `bus-timetable.json`. OpenStreetMap data © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).

- `src/twobells/data/common_passwords.txt`, the common passwords the settings refuse, is the first 5,000 of 10 characters or more from [SecLists](https://github.com/danielmiessler/SecLists)' `100k-most-used-passwords-NCSC.txt`, under the MIT License, Copyright (c) 2018 Daniel Miessler; the file's header holds the licence.

## Code

- [Leaflet](https://leafletjs.com) 1.9.4, BSD 2-Clause: `src/twobells/dashboard_static/vendor/leaflet-1.9.4/LICENSE`.
- [Inter](https://rsms.me/inter/) 4.1, SIL Open Font License 1.1, subset by `tools/build_fonts.py` for the dashboard and the board's plain look: `src/twobells/dashboard_static/vendor/inter-4.1/LICENSE` and `src/twobells/board/fonts/LICENSE`.
- The Waveshare 10.85" e-Paper (G) start-up sequence in `src/twobells/board/displays/waveshare.py` follows Waveshare's MIT-licensed `epd10in85g.py` driver.

# Notices

## Data

The board and dashboard use, and the tests include recorded samples of, these open data sources:

| Source | Licence | Used for | Recorded in |
|---|---|---|---|
| [Bus Open Data Service](https://data.bus-data.dft.gov.uk/) (Department for Transport) | [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) | Bus timetables, live positions, disruptions | `tests/fixtures/bods_*.xml`, `tests/fixtures/txc_*.xml` |
| [NaPTAN](https://beta-naptan.dft.gov.uk/) (Department for Transport) | Open Government Licence v3.0 | Stop names and positions | `tests/fixtures/naptan_*.csv` |
| [GOV.UK bank holidays](https://www.gov.uk/bank-holidays.json) | Open Government Licence v3.0 | Bank holiday dates | `tests/fixtures/govuk_bank_holidays.json` |
| [Open-Meteo](https://open-meteo.com/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Weather forecasts | `tests/fixtures/openmeteo_*.json` |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors | [ODbL](https://opendatacommons.org/licenses/odbl/); tiles under their [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) | Map tiles on the dashboard | none |

Contains public sector information licensed under the Open Government Licence v3.0. Weather data by Open-Meteo.com. Map data © OpenStreetMap contributors.

Train times come from National Rail's Darwin via the [Rail Data Marketplace](https://raildata.org.uk), under that product's licence. Bus times can also come from [TransportAPI](https://www.transportapi.com) under its terms.

## Code

- [Leaflet](https://leafletjs.com) 1.9.4, BSD 2-Clause: `src/departureboard/dashboard_static/vendor/leaflet-1.9.4/LICENSE`.
- The Waveshare 10.85" e-Paper (G) start-up sequence in `src/departureboard/waveshare.py` follows Waveshare's MIT-licensed `epd10in85g.py` driver.

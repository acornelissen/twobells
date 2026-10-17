# Getting your keys

The board and the dashboard get train and bus times from a few free online services. Some of them want to know who's asking, so they give you a key: a long string of letters and numbers that you paste into the settings. This guide covers every key, step by step.

Checked: October 2026. The services change their websites from time to time, so a button may have moved or been renamed; the steps should still be close.

- [Which keys you need](#which-keys-you-need)
- [Where keys go](#where-keys-go)
- [Rail Data Marketplace: Live Departure Board](#rail-data-marketplace-live-departure-board) (trains)
- [Rail Data Marketplace: Service Details](#rail-data-marketplace-service-details) (where each train is)
- [Rail Data Marketplace: Knowledgebase Incidents](#rail-data-marketplace-knowledgebase-incidents) (engineering works)
- [Bus Open Data Service](#bus-open-data-service) (buses)
- [TransportAPI](#transportapi) (a stand-in for bus times)
- [Services that need no key](#services-that-need-no-key)
- [Keeping keys safe](#keeping-keys-safe)
- [Replacing a key](#replacing-a-key)

---

## Which keys you need

All of them are free.

| Key | Board, trains only | Board, trains and buses | Dashboard | Everything |
|---|---|---|---|---|
| `RDM_API_KEY` (Live Departure Board) | needed | needed | needed for trains | needed |
| `BODS_API_KEY` (Bus Open Data Service) | – | needed | needed for buses | needed |
| `RDM_DETAILS_API_KEY` (Service Details) | optional on a screen | optional on a screen | optional | needed |
| `RDM_KB_API_KEY` (Knowledgebase Incidents) | – | – | optional | needed |
| `TAPI_APP_ID`, `TAPI_APP_KEY` (TransportAPI) | – | optional | not used | optional |

- **Trains only:** one key. Start here; you can add the rest later.
- **Buses on the board:** BODS gives bus times all day at no cost. TransportAPI is only a stand-in while there's no bus timetable; see [TransportAPI](#transportapi).
- **The dashboard** never uses TransportAPI, even if its keys are set.
- **Everything** means every feature on both: where each train is and engineering works on the dashboard, and the TransportAPI stand-in on the board.

## Where keys go

The easiest way is to paste them in the settings, at `/settings` on your install's address (see [The settings](settings.md#keys)). Each key has a box in one place:

| Key | Pasted on |
|---|---|
| `RDM_API_KEY`, `BODS_API_KEY` | the setup wizard's **Keys** step, or the settings' **Keys** page |
| `TAPI_APP_ID`, `TAPI_APP_KEY` | the **Keys** page (only while the board view is on) |
| `RDM_DETAILS_API_KEY` | the **Keys** page (only while the dashboard view, or the board view on an HDMI screen, is on) |
| `RDM_KB_API_KEY` | the **Keys** page (only while the dashboard view is on) |

Keys pasted there are saved in `secrets.env` in the install's store (`/var/lib/twobells` on a Pi, the `/data` volume in Docker; see [Where things are kept](configuration.md#where-things-are-kept)), readable only by the app. They win over keys in a file of your own, and need no restart.

**The Keys page** has a small form for each key, headed by what it unlocks (**Trains**, **Buses**, **Where each train is** and so on), with a label saying how the key stands: **Not set**, **Needed for trains** (or whatever is switched on without it), **Saved, not tested**, **Works, tested 10 Oct 10:42**, or **Refused, tested …**. Each form has its own buttons, and saves its own key only:

- **Save key** saves the key you pasted. Pressing Enter in the box does the same. The page then names the key it saved, never the key itself. If the key isn't pasted right, nothing is saved and a box at the top says so: paste it again.
- **Save and test** saves it, then tests it straight away, so you don't paste it twice.
- **Test saved key**, while the box is empty, tests the key already saved.

**Testing a key** makes one small request to the key's service and says what it found, by the key's box: it works, it's the wrong key, it's for another Rail Data Marketplace product, the service isn't answering, or it can't tell. The wizard has **How to get this key** (the short version of this guide) and a **Test this key** button by each key, which tests the key in the box without saving it. Each key can be tested once every 10 seconds. The settings remember whether a key's last test worked or was refused, and when, but never the key itself; pasting a new key forgets it.

**Or in a file of your own.** Each key goes on its own line, as `NAME=key`, with no spaces and no quotes:

```
RDM_API_KEY=0a1b2c3d4e5f...
```

`deploy/secrets.env.example` has every key, grouped General, Board and Dashboard. Where the file goes:

| Install | File | Notes |
|---|---|---|
| A Pi | `/etc/twobells/secrets.env` | Edit with `sudo nano /etc/twobells/secrets.env`, then restart the services (see [Replacing a key](#replacing-a-key)). |
| Docker (a Synology NAS, say) | `secrets.env`, next to `docker-compose.yml` | Copied from `deploy/secrets.env.example`. |
| A desktop | any file, given with `--secrets` | For example `twobells serve --secrets secrets.env`. |

Never put a key in `config.toml`. That file holds settings, gets copied about, and anyone on the Pi can read it.

---

## Rail Data Marketplace: Live Departure Board

**What it unlocks:** every train on the board and the dashboard: times, platforms, expected times, cancellations, the reason for a delay, and National Rail's disruption notices. On the dashboard, it also gives each train's coaches and the stops after your station.

**Needed?** Yes, for trains.

**Cost:** free, with no usage limit.

The Rail Data Marketplace (RDM) is run by the Rail Delivery Group, the people behind National Rail. You make one account, then subscribe to each product you want. **Each product has its own key**, so the three RDM keys in this guide are all different.

### Make an RDM account

You only do this once, for all three RDM products.

1. Go to <https://raildata.org.uk/registerPartner>.
2. Under **Type of organisation**, choose **Individual not affiliated with a company**.
3. Fill in the other boxes, accept the platform agreement for data consumers if asked, and press **Register**.
4. Wait for your account to be approved. It's usually the same working day, but can take up to 3 working days.
5. You'll get an email, *Welcome to Rail Data Marketplace*, with your username and a link to set your password. Follow it.
6. Sign in. You'll be asked to set up multi-factor authentication (a code from an app on your phone, for example) and to accept the terms of use and privacy policy.

### Subscribe to Live Departure Board

1. Signed in, open the **Data product catalogue** and search for **Live Departure Board**.
2. Pick the one called just **Live Departure Board**, from Rail Delivery Group. Don't pick **Live Fastest Departure Boards**, or anything with **Staff** in its name: their keys won't work here.
3. If there's a **Versions** list, leave it on the one marked **Active**.
4. Press **Subscribe**. Check the charging model says **Free**, tick **I have read and accept the licence terms and conditions**, and press **Subscribe for free**.

### Find the key

1. Press **View subscriptions** (or open **Subscriptions** from the menu at the top), then **Live Departure Board**.
2. Open the **Specification** tab and scroll down to **API access credentials**.
3. Copy the **Consumer key**. Not the **Consumer secret**: the board doesn't need that, so leave it where it is.

Also on the Specification tab, check the **URL**. It should start `https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/`. If it doesn't, copy the part before `/GetDepBoardWithDetails` into `config.toml`:

```toml
[train]
base_url = "https://api1.raildata.org.uk/..."
```

### Put it in

The **Rail Data Marketplace key** box in the setup wizard or on the settings' **Keys** page. Or `RDM_API_KEY` in a `secrets.env` of your own.

### Check it works

In the wizard, press **Test this key**; on the **Keys** page, **Save and test** as you paste it, or **Test saved key** in the **Trains** form. "Not subscribed" means the key is for another RDM product: subscribe to Live Departure Board and copy its own Consumer key.

Or on the Pi:

```bash
sudo /opt/twobells/venv/bin/twobells probe train
```

On a desktop: `twobells probe train`, with the same `--config` and `--secrets` as you start it with, if any.

It lists the next trains from your station. An error mentioning 401 or 403 means the key is wrong, or isn't for Live Departure Board. If you've only just subscribed, give it a few minutes and try again.

---

## Rail Data Marketplace: Service Details

**What it unlocks:** on the dashboard, where each of the next three trains is ("Left Hadfield 22:44, on time", "At Glossop now"), and the stops before your station in each train's sheet. On an HDMI screen, the board's rows say the same about the next three trains: the board asks for itself, during the train checking times, each train at most once a minute. It doesn't change an e-paper board, which never asks. With a filter station, the dashboard's [Delay Repay](dashboard.md#delay-repay) list also uses it for the time each train reached the filter station, as Darwin reports it.

**Needed?** No. Without it, the dashboard and the board work as before, just without train positions, and the Performance page's Delay Repay list is off.

**Cost:** free, with no usage limit.

### Subscribe

You need the [RDM account](#make-an-rdm-account) from above.

1. In the **Data product catalogue**, search for **Service Details**.
2. Pick **Service Details** from Rail Delivery Group. Its description says it's for use with Live Departure Board.
3. Leave the version on the **Active** one, press **Subscribe**, tick **I have read and accept the licence terms and conditions**, and press **Subscribe for free**.

### Find the key

1. **View subscriptions**, then **Service Details**.
2. **Specification** tab, then **API access credentials**.
3. Copy the **Consumer key**. This is a different key from Live Departure Board's, even though it's the same account.

### Put it in

The **Rail Data Marketplace key for where each train is** box on the settings' **Keys** page. Or `RDM_DETAILS_API_KEY` in a `secrets.env` of your own.

### Check it works

On the **Keys** page, press **Save and test** as you paste it, or **Test saved key** in the **Where each train is** form. Then open the dashboard: within a minute or two, the first train's row says where it is, and the next three trains' sheets draw them on their journeys. A running screen picks the saved key up too, without a restart.

If they don't, look at the log (on a Pi `journalctl -u twobells-fetch`, or `journalctl -u twobells-screen` for the screen's rows; in Docker `docker logs twobells`; on a desktop, the Terminal window running it) for "train positions off for 6 hours". That means the key was refused: check you copied the Service Details key, not the Live Departure Board one. Saving the right key on the Keys page starts the dashboard's feeds again; a key changed in a file of your own needs a restart (see [Replacing a key](#replacing-a-key)), or the dashboard waits the full 6 hours before trying again.

---

## Rail Data Marketplace: Knowledgebase Incidents

**What it unlocks:** on the dashboard, planned engineering works and other National Rail incidents that affect your trains and stations. It doesn't change the board.

**Needed?** No.

**Cost:** free to registered users.

This one needs two things: a key, and the web address to fetch the incidents from.

### Subscribe

You need the [RDM account](#make-an-rdm-account) from above.

1. In the **Data product catalogue**, search for **Knowledgebase Incidents**. National Rail's [list of Knowledgebase feeds](https://www.nationalrail.co.uk/developers/knowledgebase-data-feeds/) links straight to it, under the name **Incidents**.
2. Pick the incidents product from Rail Delivery Group. There are other Knowledgebase products (stations, promotions, ticket types); you want the one for incidents.
3. Press **Subscribe**, accept the licence terms, and press **Subscribe for free**.

### Find the key and the address

1. **View subscriptions**, then the incidents product.
2. **Specification** tab.
3. Copy the endpoint URL from the product page: the **URL** at the top of the Specification tab. It starts `https://`.
4. Scroll down to **API access credentials** and copy the **Consumer key**. Again, a different key from the other two.

### Put them in

- The key: the **Rail Data Marketplace key for engineering works** box on the settings' **Keys** page. Or `RDM_KB_API_KEY` in a `secrets.env` of your own.
- The address: the **Engineering works address** box just below it. It must be at `raildata.org.uk`; any other address goes in `config.toml`, under `[dashboard]`:

  ```toml
  [dashboard]
  kb_incidents_url = "https://..."
  ```

### Check it works

The key and the address share the **Engineering works** form on the **Keys** page: press **Save and test** there, which saves both, then tests the key against the saved address. Changing the address forgets the key's last test. If the address is missing, the dashboard says engineering works are off and asks you to set `kb_incidents_url`. Otherwise, any works affecting your trains appear with the other disruption notices. There often aren't any, so an empty list doesn't mean the key failed; look in the dashboard's log for an error about incidents if you're unsure.

---

## Bus Open Data Service

**What it unlocks:**

- Bus times on the board and the dashboard, worked out from each bus company's timetable plus where the buses are right now (`source = "bods"` under `[bus]`, which the setup wizard sets; see [Bus times](buses.md)).
- The bus position page on the HDMI screen.
- On the dashboard: where each bus is, its strip of stops and map, and bus disruption notices.

**Needed?** Yes, for buses on the dashboard, and for buses on the board unless you use TransportAPI instead.

**Cost:** free. There's no daily allowance to manage, so the board can check all day.

**Covers:** local buses in England only. Scotland and Wales aren't included.

The Bus Open Data Service (BODS) is run by the Department for Transport.

### Make an account

1. Go to <https://data.bus-data.dft.gov.uk/> and press **Sign in**, then **Create account** (or go straight to <https://data.bus-data.dft.gov.uk/account/signup/>).
2. Fill in the form:
   - **Organisation:** type `N/A` if you don't belong to one.
   - **What best describes your intended use?** **Personal interests or other** fits a home board. Add a line such as "A departure board at home for my local bus stops".
   - **Which areas of data are you interested in?** **Regional** is fine; add your area.
   - The two questions about being contacted are up to you.
3. Choose a password (at least 8 characters) and press **Create account**.
4. If BODS sends you an email to confirm your address, follow its link.

### Find the key

1. Sign in at <https://data.bus-data.dft.gov.uk/>.
2. Open **Account settings** from your account menu.
3. Copy the **API key**.

### Put it in

The **Bus data key** box in the setup wizard or on the settings' **Keys** page. Or `BODS_API_KEY` in a `secrets.env` of your own.

Buses also need your stops and their bus companies, which the setup wizard finds; see [Bus times](buses.md).

### Check it works

In the wizard, press **Test this key**; on the **Keys** page, **Save and test** as you paste it, or **Test saved key** in the **Buses** form. It asks BODS for the buses in a small patch of map, so it works whether or not any are running.

Or build the bus timetable. On the Pi:

```bash
sudo systemctl start twobells-timetable.service
journalctl -u twobells-timetable -n 5
```

On a desktop: `twobells timetable`, with the same `--config` and `--secrets` as you start it with, if any.

Either way, it ends with `Bus timetable: … journeys at your stops`. If it builds a timetable, the key works. Then `twobells probe bus` (with `sudo /opt/twobells/venv/bin/` in front on the Pi) lists the next buses. If every bus shows `sch` for a long time, **On the board** on the settings' **Overview** says whether live positions are failing.

---

## TransportAPI

**What it unlocks:** bus times on the board, as a stand-in while there's no usable bus timetable from BODS (none built yet, or built for other stops). You can also use it on its own instead of BODS, but the free plan doesn't stretch across a whole day.

**Needed?** No. Most people can skip it. The dashboard never uses it.

**Cost:** the **Free** plan gives 30 lookups ("hits") a day, for free. Each check costs one lookup per bus stop, so the board keeps to a daily budget; see [The bus budget](buses.md#the-bus-budget). Paid plans allow more.

### Make an account

1. Go to <https://developer.transportapi.com/signup> and sign up.
2. Choose the **Free** plan.

### Find the keys

Your account starts with one app. Open it to see its credentials: an **app_id** and an **app_key**. You need both.

### Put them in

The **TransportAPI app ID** and **TransportAPI app key** boxes on the settings' **Keys** page, while the board view is on. Or `TAPI_APP_ID` and `TAPI_APP_KEY` in `/etc/twobells/secrets.env`.

### Check it works

The app ID and app key share the **Buses from TransportAPI** form on the **Keys** page. Paste both, then press **Save and test** (or, with both boxes empty, **Test saved key**). It tests the two together, and uses one of today's 30 lookups, as the note by the buttons says. The board doesn't count that lookup in its own daily budget, which leaves 2 spare by default.

With `source = "transportapi"` under `[bus]`, **On the board** on the settings' **Overview** shows how many of today's TransportAPI lookups are used, and the last error if a lookup failed. Standing in for the BODS timetable, it says `From TransportAPI: …` and why on the buses' line; while the timetable is working, TransportAPI isn't used at all, so there's nothing to see.

With `source = "transportapi"` under `[bus]`, `twobells probe bus` checks it directly. That uses one lookup per stop from today's 30.

---

## Services that need no key

These are free and need no sign-up, so there's nothing to look for. Each one is sent only what it needs, from your home internet address:

- **Open-Meteo** (the weather and rain warning): your location from `[weather]`, rounded to about 1 km.
- **GOV.UK bank holidays:** nothing about you; the board just downloads the list of dates, about once a week.
- **postcodes.io** and **OpenStreetMap Nominatim** (searching for your home or stops during setup): the postcode or place name you type.
- **The OpenStreetMap foot router** (walk times): the positions of your home and your stop or station.
- **The OpenStreetMap car router** (the roads on the dashboard's bus maps, also used to place live buses on them): the positions of the bus stops on your routes, two at a time, each pair once when the bus timetable is built: about once a week, and on every bus search in the setup wizard. A pair whose road it already found last time isn't sent again. The dashboard in Docker (on a NAS, say) or on a desktop builds its own timetable, so it asks from that machine's internet address. Never your home.
- **OpenStreetMap map tiles** (the dashboard's bus maps): your browser fetches the squares of map for the area on screen, so OpenStreetMap sees roughly where you're looking.
- **NaPTAN** (bus stop names and positions, for the bus position page): the codes of the stops on your route.

---

## Keeping keys safe

A key is like a password for your account with that service. Anyone who has it can use your allowance, and the service can block the account if it's misused.

- **Never paste a key into a chat, an email, a forum post, a GitHub issue, or a screenshot.** If someone is helping you, describe the error instead; the board's own messages never include keys.
- **Keep the key files private.**
  - On the Pi, the installer makes `/etc/twobells/secrets.env` readable only by the system. If you made the file yourself, run `sudo chmod 600 /etc/twobells/secrets.env`.
  - On a desktop: `chmod 600 secrets.env`.
  - On a NAS: in File Station, give only your own account access to `secrets.env`.
- **Keys stay off GitHub.** `secrets.env` is already left out of the project's git repository and its Docker image. Don't rename them to something that isn't.
- **The settings never show a key.** Each key's form only says whether it is set and how its last test went. You can paste a new key in, but nobody can read one out, even with the settings password.
- **The settings use plain HTTP**, so paste keys into them only from your home network.
- **You only need the Consumer key** from the Rail Data Marketplace. Leave the **Consumer secret** where it is.

## Replacing a key

Replace a key when it stops working, when you've made a new one, or if it might have leaked.

1. **Get the new key** from the service, the same way as the first time. If an old key might have leaked:
   - TransportAPI: make a new app, use its keys, and delete the old app.
   - Rail Data Marketplace and BODS: look in your account for a way to make a new key, or contact them through the site's help or contact page.
2. **Put the new key in** where the old one was:
   - **The settings:** paste the new key into its box on the settings' **Keys** page and press **Save key** (or **Save and test**) in that key's form, or paste it on the wizard's **Keys** step. Each form saves its own key only, so the others stay as they are. A key saved in the settings wins over one in a file of your own, so if you've ever pasted that key there, replace it there. Nothing needs restarting.
   - **A `secrets.env` of your own:** change the text after `NAME=` and save the file. Keep it to one line, with no spaces or quotes.
3. **Restart what uses it**, only after changing a file of your own:
   - On a Pi: the e-paper board and the timetable timer pick it up on their next run. Restart the rest: `sudo systemctl restart twobells-web.service twobells-fetch.service twobells-screen.service` (leave out the screen if you don't use it).
   - On a desktop: it notices the changed file and starts again by itself within a few seconds.
   - In Docker, built from source: in **Container Manager**, choose the project, then **Action**, then **Build** (or `docker compose up -d --build`). Just restarting the container doesn't read `secrets.env` again.
4. **Check it works**, with the check from that key's section above.

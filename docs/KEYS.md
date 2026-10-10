# Getting your keys

The board and the dashboard get train and bus times from a few free online services. Some of them want to know who's asking, so they give you a key: a long string of letters and numbers that you copy into a file. This guide covers every key, step by step.

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

| Key | Pi board, trains only | Pi board, trains and buses | Dashboard | Everything |
|---|---|---|---|---|
| `RDM_API_KEY` (Live Departure Board) | needed | needed | needed for trains | needed |
| `BODS_API_KEY` (Bus Open Data Service) | – | needed | needed for buses | needed |
| `RDM_DETAILS_API_KEY` (Service Details) | – | – | optional | needed |
| `RDM_KB_API_KEY` (Knowledgebase Incidents) | – | – | optional | needed |
| `TAPI_APP_ID`, `TAPI_APP_KEY` (TransportAPI) | – | optional | not used | optional |

- **Trains only:** one key. Start here; you can add the rest later.
- **Buses on the board:** BODS gives bus times all day at no cost. TransportAPI is only a stand-in while there's no bus timetable; see [TransportAPI](#transportapi).
- **The dashboard** never uses TransportAPI, even if its keys are set.
- **Everything** means every feature on both: where each train is and engineering works on the dashboard, and the TransportAPI stand-in on the board.

## Where keys go

Each key goes on its own line, as `NAME=key`, with no spaces and no quotes:

```
RDM_API_KEY=0a1b2c3d4e5f...
```

Where that line goes depends on what you're setting up:

| Setting up | File | Notes |
|---|---|---|
| The board on a Pi | `/etc/departureboard/secrets.env` | Edit with `sudo nano /etc/departureboard/secrets.env`. Or paste keys on the settings page (see below). |
| The dashboard on a Mac | `secrets.env` in the project folder | Start the dashboard with `--secrets secrets.env`. |
| The dashboard in Docker (a Synology NAS) | `dashboard.env`, next to `docker-compose.yml` | Copy it from `deploy/dashboard.env.example`. |

**The settings page** on the Pi has a box for `RDM_API_KEY`, `BODS_API_KEY`, `TAPI_APP_ID` and `TAPI_APP_KEY`. The two dashboard-only keys (`RDM_DETAILS_API_KEY` and `RDM_KB_API_KEY`) aren't on it, because the dashboard has no settings page.

**The setup page** (the first-run setup wizard) has the same four boxes, each with **How to get this key** (the short version of this guide) and a **Test this key** button. Testing makes one small request to the key's service and says what it found: it works, it's the wrong key, it's for another Rail Data Marketplace product, the service isn't answering, or it can't tell. It tests the key in the box if you've pasted one, without saving it, or else the key already saved. Each key can be tested once every 10 seconds.

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

`RDM_API_KEY` in `secrets.env` (Pi or Mac) or `dashboard.env` (Docker), or the **Rail Data Marketplace key** box on the settings page.

### Check it works

On the setup page, press **Test this key**. "Not subscribed" means the key is for another RDM product: subscribe to Live Departure Board and copy its own Consumer key.

Or on the Pi:

```bash
sudo /opt/departureboard/venv/bin/departureboard probe train
```

On a Mac, from the project folder: `departureboard probe train --config config.toml --secrets secrets.env`.

It lists the next trains from your station. An error mentioning 401 or 403 means the key is wrong, or isn't for Live Departure Board. If you've only just subscribed, give it a few minutes and try again.

---

## Rail Data Marketplace: Service Details

**What it unlocks:** on the dashboard, where each of the next three trains is ("Left Hadfield 22:44, on time", "At Glossop now"), and the stops before your station in **Details**. It doesn't change the Pi board.

**Needed?** No. Without it, the dashboard works as before, just without train positions.

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

`RDM_DETAILS_API_KEY` in `secrets.env` (Mac) or `dashboard.env` (Docker). It isn't on the Pi's settings page.

### Check it works

Start (or restart) the dashboard and open it. Within a minute or two, the next three trains get a strip showing where they are.

If they don't, look at the dashboard's log (the terminal window on a Mac; **Container Manager**, then the container's **Log**, on a NAS) for "train positions off for 6 hours". That means the key was refused: check you copied the Service Details key, not the Live Departure Board one. After fixing it, restart the dashboard, or it waits the full 6 hours before trying again.

---

## Rail Data Marketplace: Knowledgebase Incidents

**What it unlocks:** on the dashboard, planned engineering works and other National Rail incidents that affect your trains and stations. It doesn't change the Pi board.

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

- The key: `RDM_KB_API_KEY` in `secrets.env` (Mac) or `dashboard.env` (Docker). It isn't on the Pi's settings page.
- The address: in `config.toml`, under `[dashboard]`:

  ```toml
  [dashboard]
  kb_incidents_url = "https://..."
  ```

### Check it works

Restart the dashboard. If the address is missing, the dashboard says engineering works are off and asks you to set `kb_incidents_url`. Otherwise, any works affecting your trains appear with the other disruption notices. There often aren't any, so an empty list doesn't mean the key failed; look in the dashboard's log for an error about incidents if you're unsure.

---

## Bus Open Data Service

**What it unlocks:**

- Bus times on the board and the dashboard, worked out from each bus company's timetable plus where the buses are right now (with `source = "bods"` under `[bus]` in `config.toml`; see [Bus times](USER_GUIDE.md#bus-times)).
- The bus position page on the HDMI screen.
- On the dashboard: the strip of stops showing where each bus is, the bus map, and bus disruption notices.

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

`BODS_API_KEY` in `secrets.env` (Pi or Mac) or `dashboard.env` (Docker), or the **Bus Open Data Service key** box on the settings page.

Buses also need your stops and bus companies in `config.toml`; see [Bus times](USER_GUIDE.md#bus-times).

### Check it works

On the setup page, press **Test this key**. It asks BODS for the buses in a small patch of map, so it works whether or not any are running.

Or build the bus timetable. On the Pi:

```bash
sudo systemctl start departureboard-timetable.service
journalctl -u departureboard-timetable -n 5
```

On a Mac: `departureboard timetable --config config.toml --secrets secrets.env`.

Either way, it ends with `Bus timetable: … journeys at your stops`. If it builds a timetable, the key works. Then `departureboard probe bus` (with `sudo /opt/departureboard/venv/bin/` in front on the Pi) lists the next buses. If every bus shows `sch` for a long time, the settings page's **Status** section says whether live positions are failing.

---

## TransportAPI

**What it unlocks:** bus times on the Pi board, as a stand-in while there's no usable bus timetable from BODS (none built yet, or built for other stops). You can also use it on its own instead of BODS, but the free plan doesn't stretch across a whole day.

**Needed?** No. Most people can skip it. The dashboard never uses it.

**Cost:** the **Free** plan gives 30 lookups ("hits") a day, for free. Each check costs one lookup per bus stop, so the board keeps to a daily budget; see [The bus budget](USER_GUIDE.md#the-bus-budget). Paid plans allow more.

### Make an account

1. Go to <https://developer.transportapi.com/signup> and sign up.
2. Choose the **Free** plan.

### Find the keys

Your account starts with one app. Open it to see its credentials: an **app_id** and an **app_key**. You need both.

### Put them in

`TAPI_APP_ID` and `TAPI_APP_KEY` in `/etc/departureboard/secrets.env`, or the **TransportAPI app ID** and **TransportAPI app key** boxes on the settings page.

### Check it works

On the setup page, paste both, then press **Test this key** under the app key. It tests the two together, and uses one of today's 30 lookups, so it asks first. The board doesn't count that lookup in its own daily budget, which leaves 2 spare by default.

The settings page's **Status** section shows how many of today's TransportAPI lookups are used, and a "Problem: …" line if a lookup failed. While the BODS timetable is working, TransportAPI isn't used at all, so there's nothing to see.

With `source = "transportapi"` under `[bus]`, `departureboard probe bus` checks it directly. That uses one lookup per stop from today's 30.

---

## Services that need no key

These are free and need no sign-up, so there's nothing to look for. Each one is sent only what it needs, from your home internet address:

- **Open-Meteo** (the weather and rain warning): your location from `[weather]`, rounded to about 1 km.
- **GOV.UK bank holidays:** nothing about you; the board just downloads the list of dates, about once a week.
- **postcodes.io** and **OpenStreetMap Nominatim** (searching for your home or stops during setup): the postcode or place name you type.
- **The OpenStreetMap foot router** (walk times): the positions of your home and your stop or station.
- **The OpenStreetMap car router** (the roads on the dashboard's bus maps): the positions of the bus stops on your routes, two at a time, each pair once when the bus timetable is built, about once a week. Never your home.
- **OpenStreetMap map tiles** (the dashboard's bus maps): your browser fetches the squares of map for the area on screen, so OpenStreetMap sees roughly where you're looking.
- **NaPTAN** (bus stop names and positions, for the bus position page): the codes of the stops on your route.

---

## Keeping keys safe

A key is like a password for your account with that service. Anyone who has it can use your allowance, and the service can block the account if it's misused.

- **Never paste a key into a chat, an email, a forum post, a GitHub issue, or a screenshot.** If someone is helping you, describe the error instead; the board's own messages never include keys.
- **Keep the key files private.**
  - On the Pi, the installer makes `/etc/departureboard/secrets.env` readable only by the system. If you made the file yourself, run `sudo chmod 600 /etc/departureboard/secrets.env`.
  - On a Mac: `chmod 600 secrets.env`.
  - On a NAS: in File Station, give only your own account access to `dashboard.env`.
- **Keys stay off GitHub.** `secrets.env` and `dashboard.env` are already left out of the project's git repository and its Docker image. Don't rename them to something that isn't.
- **The settings page never shows a key.** Its key boxes only say whether each key is set. You can paste a new key in, but nobody can read one out, even with the settings password.
- **The settings page uses plain HTTP**, so paste keys into it only from your home network.
- **You only need the Consumer key** from the Rail Data Marketplace. Leave the **Consumer secret** where it is.

## Replacing a key

Replace a key when it stops working, when you've made a new one, or if it might have leaked.

1. **Get the new key** from the service, the same way as the first time. If an old key might have leaked:
   - TransportAPI: make a new app, use its keys, and delete the old app.
   - Rail Data Marketplace and BODS: look in your account for a way to make a new key, or contact them through the site's help or contact page.
2. **Put the new key in** where the old one was:
   - **The Pi's settings page:** paste the new key into its box and press **Save settings**. Leaving a box empty keeps the key you have. A key saved on the page wins over `/etc/departureboard/secrets.env`, so if you've ever pasted that key on the page, replace it on the page.
   - **`secrets.env` or `dashboard.env`:** change the text after `NAME=` and save the file. Keep it to one line, with no spaces or quotes.
3. **Restart what uses it:**
   - The e-paper board picks it up at its next check.
   - The HDMI screen and the settings page: `sudo systemctl restart departureboard-screen.service departureboard-web.service` (leave out whichever you don't use).
   - The dashboard on a Mac: press Ctrl-C and start it again.
   - The dashboard on a NAS: in **Container Manager**, choose the project, then **Action**, then **Build**. Just restarting the container doesn't read `dashboard.env` again.
4. **Check it works**, with the check from that key's section above.

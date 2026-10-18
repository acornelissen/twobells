// Step 1: home.

import { $, h, fill, str, num, list, obj } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { addHome, fit, isOpen, mapFor, maps, setUpMapToggle } from "./maps.js";
import { refuse, say, setError, shout } from "./say.js";
import { state } from "./state.js";
import { enterStation } from "./station.js";
import { once } from "./steps.js";

const POSTCODE = /^[A-Za-z]{1,2}[0-9][A-Za-z0-9]?\s*[0-9][A-Za-z]{2}$/;
const UK = { lat: [49.0, 61.0], lon: [-8.7, 2.0] }; // as core/places.py's LAT_RANGE and LON_RANGE

const inUK = (lat, lon) => lat >= UK.lat[0] && lat <= UK.lat[1] && lon >= UK.lon[0] && lon <= UK.lon[1];

function setHome(place, label) {
  const lat = num(place && place.lat);
  const lon = num(place && place.lon);
  if (lat === null || lon === null || !inUK(lat, lon)) {
    shout("That place is outside the UK. Pick somewhere in the UK.");
    return;
  }
  const country = str(place.country);
  state.home = { lat, lon, label: label || str(place.label) || "the place you picked", country };
  fill($("home-chosen"), "Home: ", h("strong", {}, state.home.label));
  $("home-chosen").hidden = false;
  setError("home", "");
  say(`Home set to ${state.home.label}.`);
  if (isOpen("home")) drawHomeMap(false);
}

async function findPostcode(event) {
  event.preventDefault();
  const input = $("postcode");
  const value = input.value.trim();
  if (!POSTCODE.test(value)) return refuse("postcode", "Enter a UK postcode, like SW1A 1AA.");
  setError("postcode", "");
  await once(event.submitter || $("postcode-form").querySelector("button"), async () => {
    say("Looking up the postcode…");
    try {
      const data = await call("postcode", { postcode: value });
      setHome(obj(data.place));
    } catch (error) {
      setError("postcode", error.message);
      shout(error.message);
    }
  });
}

async function findAddress(event) {
  event.preventDefault();
  const input = $("address");
  const value = input.value.trim();
  if (value.length < 3 || value.length > 120) {
    return refuse("address", "Type 3 to 120 characters, like 1 High Street, Glossop.");
  }
  setError("address", "");
  await once(event.submitter || $("address-form").querySelector("button"), async () => {
    say("Searching…");
    let places = [];
    try {
      const data = await call("address", { q: value });
      places = list(data.places).map(obj).filter((p) => p && num(p.lat) !== null && num(p.lon) !== null);
    } catch (error) {
      setError("address", error.message);
      shout(error.message);
      return;
    }
    const results = $("address-results");
    fill(results, places.map((place) => h("li", {}, h("button", {
      type: "button",
      class: "choice",
      "aria-pressed": "false",
      on: {
        click: (event) => {
          for (const button of results.querySelectorAll("button")) button.setAttribute("aria-pressed", "false");
          event.currentTarget.setAttribute("aria-pressed", "true");
          setHome(place);
        },
      },
    }, str(place.label)))));
    $("address-results-box").hidden = places.length === 0;
    if (!places.length) {
      setError("address", "Nothing found. Add the town, or use the postcode.");
      shout("Nothing found. Add the town, or use the postcode.");
    } else {
      say(`${places.length} ${places.length === 1 ? "place" : "places"} found. Pick yours from the list below.`);
    }
  });
}

function drawHomeMap(refit = true) {
  const { map, layer } = mapFor("home");
  layer.clearLayers();
  addHome(layer);
  if (refit) fit("home", state.home ? [[state.home.lat, state.home.lon]] : [], 16);
  if (!map.wizardClicks) {
    map.wizardClicks = true;
    map.on("click", (event) => setHome({ lat: event.latlng.lat, lon: event.latlng.lng }, "the point you picked on the map"));
  }
}

export function setUpHome() {
  $("postcode-form").addEventListener("submit", findPostcode);
  $("address-form").addEventListener("submit", findAddress);
  setUpMapToggle("home", () => drawHomeMap(true));
  $("home-map-centre").addEventListener("click", () => {
    if (!maps.home) return;
    const centre = maps.home.map.getCenter();
    setHome({ lat: centre.lat, lon: centre.lng }, "the point you picked on the map");
  });
  $("home-next").addEventListener("click", () => {
    if (!state.home) {
      setError("home", "Find your postcode or address, or pick a point on the map, first.");
      $("postcode").focus();
      return;
    }
    enterStation();
  });
}

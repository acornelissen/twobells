// The first-run setup wizard: six steps from home to a saved board.
//
// It talks only to its host's setup API (setupapi.py), by relative URLs, so
// the page works under whatever prefix the host mounts it at. Every step
// works without a map: maps are opt-in (their pictures come from
// OpenStreetMap, which then sees roughly where home is), load Leaflet only
// when first opened, and show what the lists already say. Text from the
// server goes in as text through h() and fill(), never as HTML, and Leaflet
// gets DOM nodes, never strings.

import { $, h, fill, str, num, list, obj } from "../dashboard_static/dom.js";

const LEAFLET = "../dashboard_static/vendor/leaflet-1.9.4/";
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const POLL_MS = 2000;
const POLL_FAILURES = 5; // network blips while the search runs, in a row, before giving up
const STEPS = ["home", "station", "stops", "walk", "keys", "finish", "done"];
const TITLES = {
  home: "Home", station: "Station", stops: "Bus stops", walk: "Walk times",
  keys: "Keys", finish: "Check and save", done: "Done",
};
const POSTCODE = /^[A-Za-z]{1,2}[0-9][A-Za-z0-9]?\s*[0-9][A-Za-z]{2}$/;
const CRS = /^[A-Za-z]{3}$/;
const LABEL = /^[A-Za-z0-9]{1,3}$/;
const KEY = /^[A-Za-z0-9._~+/=-]{1,200}$/;
const LINK = /^(\/(?!\/)|https?:\/\/)/;
const NOUN = /^[a-z]{1,20}$/;
const MAX_STOPS = 10;
const MAX_WALK = 60;
const UK = { lat: [49.0, 61.0], lon: [-8.7, 2.0] }; // as setup.py's LAT_RANGE and LON_RANGE
const UK_CENTRE = [54.5, -3.0];
const BEARINGS = {
  N: "north", NE: "north-east", E: "east", SE: "south-east",
  S: "south", SW: "south-west", W: "west", NW: "north-west",
};
const NEEDED = {
  RDM_API_KEY: () => state.station !== null ? "Needed for trains." : "",
  BODS_API_KEY: () => state.chosen.length ? "Needed for buses." : "",
};

const state = {
  csrf: "",
  title: "Setup", // the host's, like "Set up your board", from begin
  noun: "board", // what the text calls the host, like "dashboard", from begin
  keys: [], // [{name, label, hint, set, doc}]: the host's own, from begin
  links: [],
  home: null, // {lat, lon, label, country}; exact, so it stays in this page and the server's memory
  stations: [],
  stationsFor: "",
  stationChoice: "", // the radio picked: a code, "other" or "none"
  station: null, // {crs, name, lat?, lon?} or null for no trains
  filter: null, // {crs, name} or null
  stops: [], // found near home
  stopsFor: "",
  labels: new Map(), // atco -> label typed, for stops ticked
  chosen: [], // [{label, atco, name, indicator}], when leaving the stops step
  walk: [], // [{key, what, minutes, source}]
  walkFor: "",
  walkMinutes: {}, // key -> minutes, when leaving the walk step
  keysDrawn: false,
  saving: false,
  step: "",
};

// Talking to the server.

class ApiError extends Error {
  constructor(message, status, field) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

async function readJSON(res) {
  if (!(res.headers.get("Content-Type") || "").includes("application/json")) return null;
  try { return obj(await res.json()); } catch { return null; }
}

function failure(res, data) {
  if (data && str(data.error)) return new ApiError(str(data.error), res.status, str(data.field) || null);
  if (res.status === 401 || res.status === 403) {
    return new ApiError("You've been signed out. Reload the page and sign in again.", res.status, null);
  }
  return new ApiError(`The ${state.noun} couldn't do that (error ${res.status}). Try again.`, res.status, null);
}

async function call(name, body = {}) {
  let res;
  try {
    res = await fetch(`api/${name}`, {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(`The ${state.noun} didn't answer. Check it is on and connected, then try again.`, 0, null);
  }
  const data = await readJSON(res);
  if (!res.ok) throw failure(res, data);
  return data || {};
}

async function signIn() {
  let res;
  try {
    res = await fetch("api/session", { cache: "no-store", credentials: "same-origin" });
  } catch {
    // Before begin, so the host's noun isn't known yet.
    throw new ApiError("Nothing answered. Check you're connected, then reload.", 0, null);
  }
  const data = await readJSON(res);
  if (!res.ok || !data || !str(data.csrf)) throw failure(res, data);
  state.csrf = str(data.csrf);
}

// Saying things: polite news, and errors at once.

function announce(id, text) {
  const region = $(id);
  region.textContent = "";
  // Emptied first, so the same words twice are still read out.
  setTimeout(() => { region.textContent = text; }, 50);
}
const say = (text) => announce("live", text);
const shout = (text) => announce("alert", text);

function note(id, text) {
  const el = $(id);
  el.textContent = text;
  el.hidden = !text;
}

// An error beside a control: `name`-error holds it, and the control (if an
// input) is marked invalid. The error's id is already in its aria-describedby.
function setError(name, text, control = $(name)) {
  const error = $(`${name}-error`);
  if (!error) return;
  fill(error, text ? [h("span", { class: "tag" }, "Error:"), ` ${text}`] : null);
  error.hidden = !text;
  if (control && control.tagName === "INPUT") {
    if (text) control.setAttribute("aria-invalid", "true");
    else control.removeAttribute("aria-invalid");
  }
}

// A problem with what was typed: shown by the control, which takes focus so
// its label and the error are read together.
function refuse(name, text, control = $(name)) {
  setError(name, text, control);
  if (control) control.focus();
  return false;
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
const inUK = (lat, lon) => lat >= UK.lat[0] && lat <= UK.lat[1] && lon >= UK.lon[0] && lon <= UK.lon[1];
const homeKey = () => (state.home ? `${state.home.lat},${state.home.lon}` : "");

function distance(metres) {
  const m = num(metres);
  if (m === null) return "";
  return m < 1000 ? `${Math.max(10, Math.round(m / 10) * 10)} m` : `${(m / 1000).toFixed(1)} km`;
}

// Moving between steps. The new step's heading takes focus, so a screen
// reader starts there and keyboard users carry on from the top.

function go(step, { focus = true } = {}) {
  for (const name of STEPS) $(`step-${name}`).hidden = name !== step;
  const at = STEPS.indexOf(step);
  for (const item of $("progress").children) {
    const index = STEPS.indexOf(item.dataset.step);
    if (index === at) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    item.classList.toggle("done", index < at);
  }
  document.title = `${TITLES[step]}: ${state.title}`;
  state.step = step;
  if (focus) $(`${step}-heading`).focus();
}

// One action at a time per button: a second press while the first is still
// going does nothing. aria-disabled, not disabled, so focus stays put.
async function once(button, work) {
  if (button.getAttribute("aria-disabled") === "true") return;
  button.setAttribute("aria-disabled", "true");
  try { await work(); } finally { button.removeAttribute("aria-disabled"); }
}

// Maps: Leaflet, loaded once, the first time any map opens.

let L = null;
let leafletLoading = null;
const maps = {};
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function loadLeaflet() {
  if (!leafletLoading) {
    const css = new Promise((resolve, reject) => {
      const link = h("link", { rel: "stylesheet", href: new URL(`${LEAFLET}leaflet.css`, import.meta.url).href });
      link.addEventListener("load", resolve);
      link.addEventListener("error", reject);
      document.head.append(link);
    });
    leafletLoading = Promise.all([import("../dashboard_static/vendor/leaflet-1.9.4/leaflet-src.esm.js"), css])
      .then(([module]) => { L = module; return module; })
      .catch((error) => { leafletLoading = null; throw error; });
  }
  return leafletLoading;
}

function mapFor(name) {
  if (!maps[name]) {
    const animate = !reduceMotion();
    const map = L.map($(`${name}-map`), {
      attributionControl: false, // the credit is our own text under the map
      zoomAnimation: animate, fadeAnimation: animate, markerZoomAnimation: animate,
    });
    L.tileLayer(TILES, {
      maxZoom: 19,
      referrerPolicy: "strict-origin-when-cross-origin", // OSM's tile policy wants a referrer
    }).addTo(map);
    maps[name] = { map, layer: L.layerGroup().addTo(map) };
  }
  return maps[name];
}

const isOpen = (name) => Boolean(L && maps[name] && !$(`${name}-map-box`).hidden);

function pin(text, extra = "") {
  return L.divIcon({
    html: h("span", { class: ["pin", extra] }, text),
    className: "pin-marker",
    iconSize: [32, 32],
    iconAnchor: [16, 16],
  });
}

function homePin() {
  return L.divIcon({
    html: h("span", { class: "pin pin-home" }, h("span", { "aria-hidden": "true" }, "⌂")),
    className: "pin-marker",
    iconSize: [32, 32],
    iconAnchor: [16, 16],
  });
}

function addHome(layer) {
  if (!state.home) return null;
  return L.marker([state.home.lat, state.home.lon], { icon: homePin(), keyboard: false, zIndexOffset: 1000 })
    .bindTooltip(h("span", {}, "Home"), { direction: "top", offset: [0, -14] })
    .addTo(layer);
}

// A "Show the map" button: opens the map below it, loading Leaflet the first
// time, then draws it.
function setUpMapToggle(name, draw) {
  const toggle = $(`${name}-map-toggle`);
  const box = $(`${name}-map-box`);
  const status = `${name}-map-status`;
  toggle.addEventListener("click", async () => {
    const opening = box.hidden;
    box.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    if (!opening) return;
    note(status, "Loading the map…");
    try {
      await loadLeaflet();
    } catch {
      note(status, "The map couldn't load. The list has everything the map shows.");
      return;
    }
    note(status, "");
    mapFor(name).map.invalidateSize();
    draw();
  });
}

function fit(name, points, zoom) {
  const { map } = maps[name];
  if (points.length > 1) map.fitBounds(points, { padding: [32, 32], maxZoom: 17, animate: false });
  else if (points.length === 1) map.setView(points[0], zoom, { animate: false });
  else map.setView(UK_CENTRE, 5, { animate: false });
}

// Step 1: home.

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

function setUpHome() {
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

// Step 2: the station.

function checkedStation() {
  const checked = document.querySelector('input[name="station"]:checked');
  return checked ? checked.value : "";
}

function drawStations() {
  // The choice made before, if it still applies; else the nearest station.
  let current = state.stationChoice;
  if (!["other", "none"].includes(current) && !state.stations.some((s) => s.crs === current)) {
    current = state.stations.length ? state.stations[0].crs : "";
  }
  fill($("station-list"), state.stations.map((station, i) => {
    const id = `station-${station.crs}`;
    return h("div", { class: "option" },
      h("input", { type: "radio", name: "station", id, value: station.crs, checked: station.crs === current }),
      h("label", { for: id }, h("span", { class: "num" }, `${i + 1}.`), ` ${station.name} (${station.crs}), `,
        `${distance(station.distance_m)} away`));
  }));
  $("station-other").checked = current === "other";
  $("station-none").checked = current === "none";
  showStationCode();
  if (isOpen("station")) drawStationMap();
}

function showStationCode() {
  $("station-code-box").hidden = checkedStation() !== "other";
}

function drawStationMap() {
  const { layer } = mapFor("station");
  layer.clearLayers();
  addHome(layer);
  const points = state.home ? [[state.home.lat, state.home.lon]] : [];
  state.stations.forEach((station, i) => {
    points.push([station.lat, station.lon]);
    L.marker([station.lat, station.lon], { icon: pin(String(i + 1)), keyboard: false })
      .bindTooltip(h("span", {}, `${i + 1}. ${station.name}`), { direction: "top", offset: [0, -14] })
      .on("click", () => {
        const radio = $(`station-${station.crs}`);
        if (radio) { radio.checked = true; showStationCode(); say(`${station.name} picked.`); }
      })
      .addTo(layer);
  });
  fit("station", points, 14);
}

async function enterStation() {
  go("station");
  if (state.stationsFor === homeKey()) return;
  state.stations = [];
  fill($("station-list"));
  note("station-status", "Finding the stations near home…");
  try {
    const data = await call("stations", { lat: state.home.lat, lon: state.home.lon });
    state.stations = list(data.stations).map(obj)
      .filter((s) => s && CRS.test(str(s.crs)) && num(s.lat) !== null && num(s.lon) !== null)
      .map((s) => ({ crs: s.crs, name: str(s.name), lat: s.lat, lon: s.lon, distance_m: num(s.distance_m) }));
    state.stationsFor = homeKey();
    note("station-status", state.stations.length ? ""
      : "No stations within 40 km. Type a station's code, or pick no trains.");
    say(state.stations.length ? `${state.stations.length} stations found; the nearest is picked.` : "No stations found nearby.");
  } catch (error) {
    note("station-status", `${error.message} You can type a station's code instead.`);
    shout(error.message);
  }
  drawStations();
}

async function knownStation(field, value) {
  try {
    const data = await call("station", { crs: value });
    const station = obj(data.station);
    return station ? { crs: str(station.crs), name: str(station.name), lat: num(station.lat), lon: num(station.lon) } : null;
  } catch (error) {
    refuse(field, error.message);
    return null;
  }
}

async function leaveStation(button) {
  const choice = checkedStation();
  for (const name of ["station", "station-code", "filter"]) setError(name, "");
  if (!choice) {
    return refuse("station", "Pick a station, or no trains.", document.querySelector('input[name="station"]'));
  }
  const code = $("station-code").value.trim();
  const filter = $("filter").value.trim();
  if (choice === "other" && !CRS.test(code)) return refuse("station-code", "Type a 3-letter code, like GLO.");
  if (filter && !CRS.test(filter)) return refuse("filter", "Type a 3-letter code, like MAN, or leave it empty.");
  if (filter && choice === "none") return refuse("filter", "Pick a station first, or leave this empty.");
  await once(button, async () => {
    let station = null;
    if (choice === "other") {
      station = await knownStation("station-code", code);
      if (!station) return;
    } else if (choice !== "none") {
      station = state.stations.find((s) => s.crs === choice) || null;
    }
    let destination = null;
    if (filter) {
      destination = await knownStation("filter", filter);
      if (!destination) return;
    }
    state.stationChoice = choice;
    state.station = station;
    state.filter = destination;
    enterStops();
  });
}

function setUpStation() {
  $("station-set").addEventListener("change", showStationCode);
  setUpMapToggle("station", drawStationMap);
  $("station-next").addEventListener("click", (event) => leaveStation(event.currentTarget));
}

// Step 3: bus stops.

function stopName(stop) {
  return stop.indicator ? `${stop.name}, ${stop.indicator}` : stop.name;
}

function tickedCount() {
  return $("stops-list").querySelectorAll('input[type="checkbox"]:checked').length;
}

function toggleStop(stop, checkbox) {
  if (checkbox.checked && tickedCount() > MAX_STOPS) {
    checkbox.checked = false;
    setError("stops", `Pick at most ${MAX_STOPS} stops.`);
    shout(`Pick at most ${MAX_STOPS} stops.`);
    return;
  }
  setError("stops", "");
  if (checkbox.checked) state.labels.set(stop.atco, state.labels.get(stop.atco) || stop.label);
  else state.labels.delete(stop.atco);
  $(`${checkbox.id}-label-box`).hidden = !checkbox.checked;
  if (isOpen("stops")) drawStopsMap();
}

function drawStops() {
  fill($("stops-list"), state.stops.map((stop, i) => {
    const id = `stop-${i}`;
    const ticked = state.labels.has(stop.atco);
    const about = [`${distance(stop.distance_m)} away`];
    if (BEARINGS[stop.bearing]) about.push(`buses head ${BEARINGS[stop.bearing]}`);
    const checkbox = h("input", { type: "checkbox", id, value: stop.atco, checked: ticked, "aria-describedby": `${id}-about` });
    const label = h("input", {
      id: `${id}-label`, type: "text", class: "short", maxlength: 3, autocomplete: "off", spellcheck: "false",
      value: state.labels.get(stop.atco) || stop.label, "aria-describedby": `${id}-label-hint ${id}-label-error`,
      on: {
        input: (event) => {
          state.labels.set(stop.atco, event.currentTarget.value.trim());
          setError(`${id}-label`, ""); // checked again on Next
        },
      },
    });
    checkbox.addEventListener("change", () => toggleStop(stop, checkbox));
    return h("li", { class: "stop" },
      h("div", { class: "option" }, checkbox,
        h("label", { for: id }, h("span", { class: "num" }, `${i + 1}.`), ` ${stopName(stop)}`)),
      h("p", { class: "hint about", id: `${id}-about` }, `${about.join(", ")}.`),
      h("div", { class: "field nested", id: `${id}-label-box`, hidden: !ticked },
        h("label", { for: `${id}-label` }, `Label on the ${state.noun}`),
        h("p", { class: "hint", id: `${id}-label-hint` }, "1 to 3 letters or digits."),
        h("p", { class: "error", id: `${id}-label-error`, hidden: true }),
        label));
  }));
  if (isOpen("stops")) drawStopsMap();
}

function drawStopsMap() {
  const { layer } = mapFor("stops");
  layer.clearLayers();
  addHome(layer);
  const points = state.home ? [[state.home.lat, state.home.lon]] : [];
  state.stops.forEach((stop, i) => {
    points.push([stop.lat, stop.lon]);
    const ticked = state.labels.has(stop.atco);
    L.marker([stop.lat, stop.lon], { icon: pin(String(i + 1), ticked ? "pin-chosen" : ""), keyboard: false })
      .bindTooltip(h("span", {}, `${i + 1}. ${stopName(stop)}${ticked ? " (ticked)" : ""}`), { direction: "top", offset: [0, -14] })
      .on("click", () => {
        const checkbox = $(`stop-${i}`);
        if (!checkbox) return;
        checkbox.checked = !checkbox.checked;
        toggleStop(stop, checkbox);
        say(`${stopName(stop)} ${checkbox.checked ? "ticked" : "unticked"}.`);
      })
      .addTo(layer);
  });
  fit("stops", points, 16);
}

async function enterStops() {
  go("stops");
  if (state.stopsFor === homeKey()) return;
  state.stops = [];
  state.labels = new Map();
  fill($("stops-list"));
  note("stops-status", "Finding the bus stops near home. This downloads your area's stop list and can take a minute…");
  say("Finding the bus stops near home. This can take a minute.");
  try {
    const data = await call("stops", { lat: state.home.lat, lon: state.home.lon });
    state.stops = list(data.stops).map(obj)
      .filter((s) => s && str(s.atco) && num(s.lat) !== null && num(s.lon) !== null)
      .map((s) => ({
        atco: s.atco, name: str(s.name), indicator: str(s.indicator), bearing: str(s.bearing),
        lat: s.lat, lon: s.lon, distance_m: num(s.distance_m), label: LABEL.test(str(s.label)) ? s.label : "",
      }));
    state.stopsFor = homeKey();
    note("stops-status", state.stops.length ? "" : `No bus stops within a kilometre. Carry on for a ${state.noun} with trains only.`);
    say(state.stops.length ? `${state.stops.length} bus stops found.` : "No bus stops found nearby.");
  } catch (error) {
    note("stops-status", `${error.message} Go back and forward to try again, or carry on without buses.`);
    shout(error.message);
  }
  drawStops();
}

function leaveStops() {
  setError("stops", "");
  const chosen = [];
  const seen = new Set();
  for (const [i, stop] of state.stops.entries()) {
    if (!state.labels.has(stop.atco)) continue;
    const field = `stop-${i}-label`;
    const label = $(field).value.trim();
    setError(field, "");
    if (!LABEL.test(label)) return refuse(field, "Use 1 to 3 letters or digits, like A.");
    if (seen.has(label.toUpperCase())) return refuse(field, "Another stop has this label; give each its own.");
    seen.add(label.toUpperCase());
    chosen.push({ label, atco: stop.atco, name: stop.name, indicator: stop.indicator });
  }
  if (chosen.length > MAX_STOPS) return refuse("stops", `Pick at most ${MAX_STOPS} stops.`, $("stops-list").querySelector("input"));
  if (!chosen.length && !state.station) {
    const first = $("stops-list").querySelector("input");
    return refuse("stops", `Pick at least one stop: with no station and no stops, the ${state.noun} has nothing to show.`,
      first || $("stops-heading"));
  }
  state.chosen = chosen;
  enterWalk();
  return true;
}

function setUpStops() {
  setUpMapToggle("stops", drawStopsMap);
  $("stops-next").addEventListener("click", leaveStops);
}

// Step 4: walk times.

const SOURCES = {
  route: ["Route", "Worked out along streets and paths."],
  estimate: ["Estimate", "Guessed from the distance; check it."],
  none: ["Not known", "Type it in, or leave it empty."],
  edited: ["Yours", "Changed by you."],
};

function sourceNote(source) {
  const [tag, words] = SOURCES[source] || SOURCES.none;
  return [h("span", { class: `tag tag-${source}` }, tag), ` ${words}`];
}

function drawWalk() {
  fill($("walk-list"), state.walk.map((walk, i) => {
    const id = `walk-${i}`;
    const input = h("input", {
      id, type: "number", inputmode: "numeric", min: 0, max: MAX_WALK, step: 1, class: "short",
      value: walk.minutes === null ? "" : walk.minutes, "aria-describedby": `${id}-hint ${id}-error`,
    });
    const hint = h("p", { class: "hint", id: `${id}-hint` }, sourceNote(walk.source));
    input.addEventListener("input", () => {
      if (walk.source !== "edited") { walk.source = "edited"; fill(hint, sourceNote("edited")); }
    });
    return h("div", { class: "field" },
      h("label", { for: id }, `Minutes to walk to ${walk.what}`),
      hint,
      h("p", { class: "error", id: `${id}-error`, hidden: true }),
      input);
  }));
}

async function enterWalk() {
  go("walk");
  const key = JSON.stringify([homeKey(), state.station && state.station.crs, state.chosen.map((s) => s.atco)]);
  if (state.walkFor === key) return; // keep what was typed
  state.walk = [];
  if (state.station) state.walk.push({ key: state.station.crs, what: `${state.station.name} station` });
  for (const stop of state.chosen) state.walk.push({ key: stop.atco, what: `stop ${stop.label}, ${stopName(stop)}` });
  for (const walk of state.walk) Object.assign(walk, { minutes: null, source: "none" });
  fill($("walk-list"));
  note("walk-status", "Working out the walks…");
  try {
    const data = await call("walk", {
      home: { lat: state.home.lat, lon: state.home.lon },
      station_crs: state.station ? state.station.crs : null,
      stop_atcos: state.chosen.map((s) => s.atco),
    });
    const found = obj(data.walk) || {};
    for (const walk of state.walk) {
      const answer = obj(found[walk.key]);
      const minutes = answer ? num(answer.minutes) : null;
      if (minutes !== null && Number.isInteger(minutes) && minutes >= 0) {
        walk.minutes = Math.min(minutes, MAX_WALK);
        walk.source = answer.source === "route" ? "route" : "estimate";
      }
    }
    note("walk-status", "");
    say("Walk times worked out. Check them below.");
  } catch (error) {
    note("walk-status", `${error.message} Type the walks in yourself.`);
    shout(error.message);
  }
  state.walkFor = key;
  drawWalk();
}

function leaveWalk() {
  const minutes = {};
  for (const [i, walk] of state.walk.entries()) {
    const id = `walk-${i}`;
    const input = $(id);
    const raw = input.value.trim();
    setError(id, "");
    // A number box reads as empty when what's typed isn't a number.
    if (input.validity.badInput) return refuse(id, `Enter whole minutes from 0 to ${MAX_WALK}, or leave it empty.`);
    if (raw === "") continue;
    const value = Number(raw);
    if (!/^\d{1,2}$/.test(raw) || value > MAX_WALK) return refuse(id, `Enter whole minutes from 0 to ${MAX_WALK}, or leave it empty.`);
    minutes[walk.key] = value;
  }
  state.walkMinutes = minutes;
  enterKeys();
  return true;
}

// Step 5: keys. Never shown, only whether each is set; a pasted one is sent
// when saving (and the bus key for the search, if pasted), and to test it.

// The short way to get each key, from docs/KEYS.md; its link has the full guide.
const SIGN_UP = {
  RDM_API_KEY: [
    "Register at raildata.org.uk as an individual not affiliated with a company. Approval can take up to 3 working days.",
    "In the Data product catalogue, subscribe (free) to Live Departure Board. Not Live Fastest Departure Boards, or anything with Staff in its name.",
    "Open Subscriptions, then Live Departure Board, then the Specification tab, and copy the Consumer key. Not the Consumer secret.",
  ],
  BODS_API_KEY: [
    "Create an account at data.bus-data.dft.gov.uk. For intended use, pick Personal interests or other.",
    "If BODS emails you to confirm your address, follow its link.",
    "Sign in, open Account settings and copy the API key.",
  ],
  RDM_DETAILS_API_KEY: [
    "Optional. With the Rail Data Marketplace account above, find Service Details in the Data product catalogue and subscribe (free).",
    "Open Subscriptions, then Service Details, then the Specification tab, and copy its own Consumer key. It isn't the Live Departure Board one.",
  ],
  RDM_KB_API_KEY: [
    "Optional. With the Rail Data Marketplace account above, find Knowledgebase Incidents in the Data product catalogue and subscribe (free).",
    "Open Subscriptions, then the incidents product, then the Specification tab, and copy its own Consumer key.",
    "Copy the URL at the top of that tab too, into Knowledgebase Incidents address on the settings page, under Trains.",
  ],
  TAPI_APP_ID: [
    "Optional: most setups don't need TransportAPI. It stands in for bus times while there's no bus timetable.",
    "Sign up at developer.transportapi.com and choose the Free plan, 30 lookups a day.",
    "Open the app your account starts with, and copy its app_id here and its app_key into the next box.",
  ],
  TAPI_APP_KEY: [
    "Optional, with the app ID above: both come from the same app on developer.transportapi.com.",
    "Open the app and copy its app_key here.",
  ],
};

// "Test this key": the host makes one harmless call to the key's service.
// TransportAPI's app ID and key are tested together, from the key's box.
// Only the keys the host offers are drawn, so each host gets its own buttons.
const TESTED = ["RDM_API_KEY", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY", "BODS_API_KEY", "TAPI_APP_KEY"];
const RESULTS = { // state: [glyph, words]; the colour only repeats them
  testing: ["…", "Testing"],
  works: ["✓", "Works"],
  wrong_key: ["✗", "Wrong key"],
  not_subscribed: ["✗", "Not subscribed"],
  service_down: ["!", "Service not answering"],
  missing: ["!", "No key"],
  unknown: ["?", "Can't tell"],
  problem: ["!", "Not tested"],
};
const LOOKUP_WARNING = "Testing TransportAPI uses 1 of today's 30 free lookups, the same ones bus times use. Test now?";

function showResult(region, result, text) {
  const [glyph, words] = RESULTS[result] || RESULTS.unknown;
  region.className = `key-result key-result-${RESULTS[result] ? result : "unknown"}`;
  fill(region, h("span", { class: "key-glyph", "aria-hidden": "true" }, glyph), h("strong", {}, `${words}:`), ` ${text}`);
}

function keySet(name) {
  const key = state.keys.find((k) => k.name === name);
  return Boolean(key && key.set);
}

async function testKey(name, input, button, region) {
  const id = `key-${name}`;
  const body = { name };
  const value = input.value.trim();
  setError(id, "");
  if (value && !KEY.test(value)) return refuse(id, "Paste the key exactly; it has only letters, digits and . _ ~ + / = -");
  if (value) body.value = value;
  let ready = Boolean(value) || keySet(name);
  if (name === "TAPI_APP_KEY") {
    const other = $("key-TAPI_APP_ID");
    const appId = other ? other.value.trim() : "";
    setError("key-TAPI_APP_ID", "");
    if (appId && !KEY.test(appId)) return refuse("key-TAPI_APP_ID", "Paste the app ID exactly; it has only letters, digits and . _ ~ + / = -");
    if (appId) body.app_id = appId;
    ready = ready && (Boolean(appId) || keySet("TAPI_APP_ID"));
  }
  if (!ready) {
    showResult(region, "missing", name === "TAPI_APP_KEY"
      ? "Paste the app ID and the app key first: they're tested together."
      : "Paste the key first, then test it.");
    return false;
  }
  if (name === "TAPI_APP_KEY" && !window.confirm(LOOKUP_WARNING)) return false;
  await once(button, async () => {
    showResult(region, "testing", "asking the service…");
    try {
      const data = await call("testkey", body);
      showResult(region, str(data.state), str(data.message) || "No details.");
    } catch (error) {
      if (error.field) setError(`key-${error.field}`, error.message);
      showResult(region, "problem", error.message);
    }
  });
  return true;
}

function keyTestButton(name, input) {
  if (!TESTED.includes(name)) return null;
  const id = `key-${name}`;
  const paired = name === "TAPI_APP_KEY";
  const region = h("p", { class: "key-result", id: `${id}-result`, role: "status", "aria-live": "polite" });
  const button = h("button", {
    type: "button", class: "quiet", id: `${id}-test`, "aria-describedby": paired ? `${id}-test-hint` : null,
  }, "Test this key");
  button.addEventListener("click", () => testKey(name, input, button, region));
  // A result is about what was in the box then; once that changes, it's gone.
  input.addEventListener("input", () => { region.className = "key-result"; fill(region); });
  return h("div", { class: "key-test" },
    paired ? h("p", { class: "hint", id: `${id}-test-hint` },
      "Tests the app ID and app key together, and uses 1 of today's 30 lookups, so it asks first.") : null,
    button,
    region);
}

function signUp(key) {
  const steps = SIGN_UP[key.name];
  const link = /^https:\/\//.test(key.doc)
    ? h("a", { href: key.doc, target: "_blank", rel: "noopener noreferrer" }, "The full guide (opens in a new tab)")
    : null;
  if (!steps && !link) return null;
  return h("details", { class: "sign-up" },
    h("summary", {}, "How to get this key"),
    steps ? h("ol", {}, steps.map((step) => h("li", {}, step))) : null,
    link ? h("p", {}, link) : null);
}

function drawKeys() {
  fill($("keys-list"), state.keys.map((key) => {
    const id = `key-${key.name}`;
    const input = h("input", {
      id, type: "text", autocomplete: "off", spellcheck: "false", class: "key-input",
      "aria-describedby": `${id}-hint ${id}-needed ${id}-error`,
    });
    return h("div", { class: "field key" },
      h("label", { for: id }, key.label),
      h("p", { class: "hint", id: `${id}-hint` }, `${key.hint} `,
        h("strong", { class: key.set ? "is-set" : "not-set" }, key.set ? "Set." : "Not set.")),
      signUp(key),
      h("p", { class: "needed", id: `${id}-needed` }),
      h("p", { class: "error", id: `${id}-error`, hidden: true }),
      input,
      keyTestButton(key.name, input));
  }));
  state.keysDrawn = true;
}

function enterKeys() {
  go("keys");
  if (!state.keysDrawn) drawKeys();
  for (const key of state.keys) {
    const needed = NEEDED[key.name] ? NEEDED[key.name]() : "";
    note(`key-${key.name}-needed`, needed && !key.set ? needed : "");
  }
}

function pastedKeys() {
  const keys = {};
  for (const key of state.keys) {
    const value = $(`key-${key.name}`).value.trim();
    if (value) keys[key.name] = value;
  }
  return keys;
}

function leaveKeys() {
  const pasted = pastedKeys();
  for (const key of state.keys) {
    const id = `key-${key.name}`;
    setError(id, "");
    if (pasted[key.name] && !KEY.test(pasted[key.name])) {
      return refuse(id, "Paste the key exactly; it has only letters, digits and . _ ~ + / = -");
    }
    const needed = NEEDED[key.name] ? NEEDED[key.name]() : "";
    if (needed && !key.set && !pasted[key.name]) {
      return refuse(id, "Paste this key, or go back and change your choices.");
    }
  }
  enterFinish();
  return true;
}

// Step 6: check, search and save.

function summaryRow(term, ...details) {
  return [h("dt", {}, term), h("dd", {}, ...details)];
}

function minutesText(key) {
  const minutes = state.walkMinutes[key];
  return minutes === undefined ? "no walk time" : `${minutes} min walk`;
}

function enterFinish() {
  go("finish");
  const keys = Object.keys(pastedKeys()).map((name) => (state.keys.find((k) => k.name === name) || {}).label || name);
  fill($("summary"),
    summaryRow("Home", state.home.label, h("span", { class: "hint" }, " (only a position rounded to about 1 km is saved, for the rain warning)")),
    summaryRow("Station", state.station ? `${state.station.name} (${state.station.crs}), ${minutesText(state.station.crs)}` : "No trains"),
    state.filter ? summaryRow("Only trains calling at", `${state.filter.name} (${state.filter.crs})`) : null,
    summaryRow("Bus stops", state.chosen.length
      ? h("ul", {}, state.chosen.map((s) => h("li", {}, `${s.label}: ${stopName(s)}, ${minutesText(s.atco)}`)))
      : "No buses"),
    summaryRow("Keys to save", keys.length ? keys.join(", ") : "None; the keys already set stay"));
  const buses = state.chosen.length > 0;
  $("routes-box").hidden = !buses;
  $("search-hint").hidden = !buses;
  setError("finish", "");
}

function showJob(job) {
  $("job-box").hidden = false;
  const step = str(job.step) || (job.state === "done" ? "Search finished" : "Starting the search");
  if ($("job-status").textContent !== step) $("job-status").textContent = step;
  const done = num(job.done);
  const total = num(job.total);
  const bar = $("job-progress");
  if (total && done !== null) {
    bar.max = total;
    bar.value = Math.min(done, total);
    $("job-count").textContent = `${Math.min(done, total)} of ${total}`;
  } else {
    bar.removeAttribute("value"); // indeterminate: still working out how much there is
    $("job-count").textContent = "";
  }
}

async function search() {
  const stops = Object.fromEntries(state.chosen.map((s) => [s.label, s.atco]));
  const walk = {};
  for (const stop of state.chosen) {
    if (state.walkMinutes[stop.atco] !== undefined) walk[stop.label] = state.walkMinutes[stop.atco];
  }
  const body = { stops, routes: $("routes").checked, walk };
  const bus = pastedKeys().BODS_API_KEY;
  if (bus) body.keys = { BODS_API_KEY: bus };
  let job;
  try {
    job = obj((await call("start", body)).job);
  } catch (error) {
    if (error.status !== 409) throw error;
    job = obj((await call("job")).job); // one already running, perhaps from before a reload: follow it
  }
  $("job-cancel").hidden = false;
  let failures = 0;
  while (job && (job.state === "running" || job.state === "queued")) {
    showJob(job);
    await sleep(POLL_MS);
    try {
      job = obj((await call("job")).job);
      failures = 0;
    } catch (error) {
      if (++failures >= POLL_FAILURES) throw error;
    }
  }
  $("job-cancel").hidden = true;
  if (!job || job.state !== "done") {
    throw new ApiError(str(job && job.message) === "Cancelled"
      ? "Search cancelled. Press Save to start again."
      : `The search failed: ${str(job && job.message) || "no reason given"}.`, 0, null);
  }
  showJob(job);
  return job;
}

function payload() {
  const keys = pastedKeys();
  const body = {
    home: { lat: state.home.lat, lon: state.home.lon, ...(state.home.country ? { country: state.home.country } : {}) },
    station_crs: state.station ? state.station.crs : null,
    filter_crs: state.filter ? state.filter.crs : null,
    stops: Object.fromEntries(state.chosen.map((s) => [s.label, s.atco])),
    walk: state.walkMinutes,
  };
  if (Object.keys(keys).length) body.keys = keys;
  return body;
}

async function save(button) {
  if (state.saving) return;
  state.saving = true;
  for (const el of document.querySelectorAll("#finish-actions button")) el.setAttribute("aria-disabled", "true");
  setError("finish", "");
  try {
    const job = state.chosen.length ? await search() : null;
    say("Saving…");
    const data = await call("finish", payload());
    finished(data, job);
  } catch (error) {
    setError("finish", error.message);
    shout(error.message);
    button.focus();
  } finally {
    state.saving = false;
    for (const el of document.querySelectorAll("#finish-actions button")) el.removeAttribute("aria-disabled");
    $("job-cancel").hidden = true;
  }
}

function finished(data, job) {
  fill($("done-message"), str(data.message) || "Saved.");
  const found = job ? obj(job.found) : null;
  const items = [];
  if (found) {
    const lines = obj(found.lines) || {};
    for (const operator of list(found.operators).map(str).filter(Boolean)) {
      const its = list(lines[operator]).map(str).filter(Boolean);
      items.push(`Bus company ${operator}${its.length ? `: ${its.length === 1 ? "line" : "lines"} ${its.join(", ")}` : ""}.`);
    }
    for (const text of list(found.notes).map(str).filter(Boolean)) items.push(text);
  }
  fill($("done-found"), items.map((text) => h("li", {}, text)));
  const links = list(data.links).map(obj).filter((l) => l && str(l.label) && LINK.test(str(l.href)));
  fill($("done-links"), links.map((l) => h("li", {}, h("a", { href: l.href }, l.label))));
  go("done");
  say("Setup is done.");
}

function setUpFinish() {
  $("save").addEventListener("click", (event) => save(event.currentTarget));
  $("job-cancel").addEventListener("click", async () => {
    try { await call("cancel"); say("Cancelling the search…"); } catch (error) { shout(error.message); }
  });
}

// Start.

function setUpSteps() {
  for (const button of document.querySelectorAll("[data-back]")) {
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-disabled") === "true") return;
      go(button.dataset.back);
    });
  }
  setUpHome();
  setUpStation();
  setUpStops();
  $("walk-next").addEventListener("click", leaveWalk);
  $("keys-next").addEventListener("click", leaveKeys);
  setUpFinish();
}

async function begin() {
  try {
    await signIn();
    const data = await call("begin");
    state.title = str(data.title).slice(0, 60) || state.title;
    fill($("title"), state.title);
    if (NOUN.test(str(data.noun))) state.noun = data.noun;
    for (const el of document.querySelectorAll("[data-noun]")) el.textContent = state.noun;
    state.keys = list(data.keys).map(obj).filter((k) => k && /^[A-Z_]{1,64}$/.test(str(k.name)))
      .map((k) => ({ name: k.name, label: str(k.label) || k.name, hint: str(k.hint), set: k.set === true, doc: str(k.doc) }));
    state.links = list(data.links);
  } catch (error) {
    note("loading", error.message);
    return;
  }
  note("loading", "");
  setUpSteps();
  go("home", { focus: false });
}

begin();

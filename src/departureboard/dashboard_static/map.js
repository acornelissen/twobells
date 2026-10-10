// The bus map dialog. Leaflet is loaded only when a map first opens. Stop
// names come from BODS, so tooltips and popups get DOM nodes, never strings
// (Leaflet would parse a string as HTML). The ordered list beside the map
// says the same things in words.

import { $, h, s as svg, fill, setText, str, num, list, obj, validId, openDialog, setUpDialog } from "./dom.js";
import { getJSON } from "./api.js";
import { age, now, parseAt } from "./time.js";
import { busDialogExtras } from "./extras.js";

const LEAFLET = "./vendor/leaflet-1.9.4/";
export const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

let L = null;
let leafletLoading = null;
let map = null;
let layers = null;
let busMarker = null;
let busPosition = null;
let openId = null;
let openRow = null; // the snapshot's row for the open bus, for its extras
let request = 0;

const dialog = () => $("bus-dialog");
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// Loads Leaflet once, for this map and the area map; resolves to the module.
export function loadLeaflet() {
  if (!leafletLoading) {
    const css = new Promise((resolve, reject) => {
      const link = h("link", { rel: "stylesheet", href: new URL(`${LEAFLET}leaflet.css`, import.meta.url).href });
      link.addEventListener("load", resolve);
      link.addEventListener("error", reject);
      document.head.append(link);
    });
    leafletLoading = Promise.all([import("./vendor/leaflet-1.9.4/leaflet-src.esm.js"), css])
      .then(([module]) => { L = module; return module; })
      .catch((error) => { leafletLoading = null; throw error; });
  }
  return leafletLoading;
}

export function setUpMap() {
  setUpDialog(dialog(), () => {
    openId = null;
    openRow = null;
    request++;
    $("osm-credit").hidden = true;
  });
  $("map-zoom-in").addEventListener("click", () => map && map.zoomIn(1, { animate: !reduceMotion() }));
  $("map-zoom-out").addEventListener("click", () => map && map.zoomOut(1, { animate: !reduceMotion() }));
  $("map-centre").addEventListener("click", () => {
    if (map && busPosition) map.setView(busPosition, Math.max(map.getZoom(), 16), { animate: !reduceMotion() });
  });
}

function ensureMap() {
  if (map) return;
  const animate = !reduceMotion();
  map = L.map($("map"), {
    zoomControl: false, // our own buttons, outside the map, are real <button>s
    attributionControl: false, // the credit is our own DOM text under the map
    zoomAnimation: animate, fadeAnimation: animate, markerZoomAnimation: animate,
  });
  L.tileLayer(TILES, {
    maxZoom: 19,
    referrerPolicy: "strict-origin-when-cross-origin", // OSM's tile policy wants a referrer
  }).addTo(map);
  layers = L.layerGroup().addTo(map);
}

// A bus from the front, upright on a dark disc. Drawing only.
function busGlyph() {
  return svg("svg", { class: "bus-glyph", viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" },
    svg("path", { class: "bus-mirrors", d: "M4 7.5 H2.75 V11 M20 7.5 H21.25 V11" }),
    svg("rect", { class: "bus-body", x: 5.5, y: 17, width: 3, height: 5, rx: 1 }),
    svg("rect", { class: "bus-body", x: 15.5, y: 17, width: 3, height: 5, rx: 1 }),
    svg("rect", { class: "bus-body", x: 4, y: 1.5, width: 16, height: 18, rx: 3 }),
    svg("rect", { class: "bus-dark", x: 7, y: 3, width: 10, height: 1.75, rx: 0.5 }),
    svg("rect", { class: "bus-dark", x: 6, y: 6, width: 12, height: 6.5, rx: 1 }),
    svg("circle", { class: "bus-dark", cx: 7.75, cy: 15.75, r: 1.4 }),
    svg("circle", { class: "bus-dark", cx: 16.25, cy: 15.75, r: 1.4 }));
}

// The bus marker, here and on the area map: the bus stays upright with its
// line number under it, and a chevron outside the disc turns to the bearing
// (clockwise from north) through the CSSOM, which the CSP allows (it forbids
// style attributes in markup). No bearing, no chevron.
export function busPin(bearing, line) {
  let heading = null;
  if (bearing !== null) {
    heading = svg("svg", { class: "bus-heading", viewBox: "0 0 60 60", "aria-hidden": "true", focusable: "false" },
      svg("path", { d: "M30 1 L39 12.5 L30 9.5 L21 12.5 Z" }));
    heading.style.transform = `rotate(${bearing}deg)`;
  }
  return h("span", { class: "bus-pin" }, heading, busGlyph(),
    line ? h("span", { class: "bus-pin-line" }, line) : null);
}

function busIcon(bearing, line) {
  return L.divIcon({ html: busPin(bearing, line), className: "bus-marker", iconSize: [34, 34], iconAnchor: [17, 17] });
}

function stopTooltip(stop) {
  return h("span", {}, h("strong", {}, str(stop.name)), " ", str(stop.time), stop.yours === true ? " (your stop)" : "");
}

function reportedText(vehicle) {
  const recorded = parseAt(vehicle.recorded_at);
  const seconds = recorded !== null ? (now() - recorded) / 1000 : num(vehicle.age_s);
  return seconds === null ? "" : `reported ${age(seconds)}`;
}

function drawMap(data, fit) {
  const stops = list(data.stops).filter((s) => obj(s) && num(s.lat) !== null && num(s.lon) !== null);
  layers.clearLayers();
  const points = stops.map((s) => [s.lat, s.lon]);
  // Along the roads when the server sends them, else straight from stop to stop.
  const road = list(data.path).filter((p) => Array.isArray(p) && num(p[0]) !== null && num(p[1]) !== null);
  const line = road.length > 1 ? road : points;
  if (line.length > 1) L.polyline(line, { className: "route-line", interactive: false }).addTo(layers);
  for (const stop of stops) {
    const yours = stop.yours === true;
    L.circleMarker([stop.lat, stop.lon], {
      className: yours ? "stop-yours" : "stop-dot", radius: yours ? 9 : 5,
    }).bindTooltip(stopTooltip(stop), { permanent: yours, direction: "top", offset: [0, -6] }).addTo(layers);
  }
  const vehicle = obj(data.vehicle);
  busPosition = vehicle && num(vehicle.lat) !== null && num(vehicle.lon) !== null ? [vehicle.lat, vehicle.lon] : null;
  busMarker = null;
  if (busPosition) {
    busMarker = L.marker(busPosition, { icon: busIcon(num(vehicle.bearing), str(data.line)), keyboard: false, zIndexOffset: 1000 })
      .bindPopup(h("span", {}, h("strong", {}, `${str(data.line)} to ${str(data.destination)}`), ` ${reportedText(vehicle)}`))
      .addTo(layers);
  }
  $("map-centre").disabled = !busPosition;
  if (fit) {
    const all = busPosition ? [...points, busPosition] : points;
    if (all.length) map.fitBounds(all, { padding: [24, 24], maxZoom: 16, animate: false });
  }
}

// The text alternative: the stops in order with times, and where the bus is.
function drawText(data) {
  const vehicle = obj(data.vehicle);
  const where = str(data.where);
  setText($("map-where"), vehicle
    ? `Bus: ${[where || "position known", reportedText(vehicle)].filter(Boolean).join(", ")}.`
    : "No live position for this bus; the stops and times are from the timetable.");
  $("map-stops").replaceChildren(...list(data.stops).filter(obj).map((stop) => h("li",
    { class: stop.yours === true ? "yours" : null },
    h("span", { class: "cp-time" }, str(stop.time)), " ",
    h("span", { class: "cp-name" }, str(stop.name)),
    stop.yours === true ? h("strong", {}, " (your stop)") : null)));
}

function show(data, fit) {
  if (!data) {
    setText($("map-status"), "This bus has left the board.");
    $("map-centre").disabled = true;
    return;
  }
  const stop = str(data.stop_name) || (str(data.stop) ? `Stop ${str(data.stop)}` : "");
  setText($("bus-title"), `${str(data.line)} to ${str(data.destination)}${stop ? `, ${stop}` : ""}`);
  setText($("map-status"), "");
  drawText(data);
  if (L) {
    ensureMap();
    map.invalidateSize();
    drawMap(data, fit);
  }
}

// The arrival, inbound bus and ghost warning, from the snapshot's row.
function drawExtras() {
  fill($("bus-extras"), busDialogExtras(openRow));
}

export async function openMap(id, opener, row = null) {
  if (!validId(id)) return;
  openId = id;
  openRow = obj(row);
  const mine = ++request;
  setText($("bus-title"), "Bus map");
  setText($("map-status"), "Loading the map…");
  setText($("map-where"), "");
  drawExtras();
  $("map-stops").replaceChildren();
  $("map-centre").disabled = true;
  $("osm-credit").hidden = false;
  openDialog(dialog(), opener);
  try {
    const [data] = await Promise.all([getJSON(`/api/bus/${encodeURIComponent(id)}`), loadLeaflet()]);
    if (mine === request) show(obj(data), true);
  } catch {
    if (mine === request) setText($("map-status"), "The map couldn't be loaded. The bus may have left the board, or the server is busy.");
  }
}

// Called with each new snapshot while the dialog is open: moves the bus
// without refitting, so a map you have panned stays put. rowsById maps id
// to { row, mode }; a bus gone from the board keeps its last extras.
export async function refreshMap(rowsById = new Map()) {
  if (!openId || !dialog().open) return;
  const entry = rowsById.get(openId);
  if (entry) {
    openRow = entry.row;
    drawExtras();
  }
  if (!L) return;
  const id = openId;
  const mine = request;
  try {
    const data = await getJSON(`/api/bus/${encodeURIComponent(id)}`);
    if (mine === request && openId === id) show(obj(data), false);
  } catch { /* keep the last map */ }
}

// The bus map in the departure sheet (sheet.js). Leaflet is loaded only when
// a map first opens. Stop names come from BODS, so tooltips and popups get
// DOM nodes, never strings (Leaflet would parse a string as HTML). The
// ordered list under the map says the same things in words.

import { $, h, s as svg, setText, str, num, list, obj, validId, reduceMotion } from "./dom.js";
import { getJSON } from "./api.js";
import { loadLeaflet, makeMap } from "./basemap.js";
import { age, now, parseAt } from "./time.js";

let L = null;
let map = null;
let layers = null;
let busMarker = null;
let busPosition = null;
let openId = null;
let request = 0;

export function setUpMap() {
  $("map-zoom-in").addEventListener("click", () => map && map.zoomIn(1, { animate: !reduceMotion() }));
  $("map-zoom-out").addEventListener("click", () => map && map.zoomOut(1, { animate: !reduceMotion() }));
  $("map-centre").addEventListener("click", () => {
    if (map && busPosition) map.setView(busPosition, Math.max(map.getZoom(), 16), { animate: !reduceMotion() });
  });
}

function ensureMap() {
  if (map) return;
  map = makeMap($("map"), { zoomControl: false }); // our own buttons, outside the map, are real <button>s
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

// The line a map draws: along the roads when the server sends at least two
// usable [lat, lon] points, else straight through `stops` (the same pairs).
export function roadLine(path, stops = []) {
  const road = list(path).filter((p) => Array.isArray(p) && num(p[0]) !== null && num(p[1]) !== null);
  return road.length > 1 ? road : stops;
}

function drawMap(data, fit) {
  const stops = list(data.stops).filter((s) => obj(s) && num(s.lat) !== null && num(s.lon) !== null);
  layers.clearLayers();
  const points = stops.map((s) => [s.lat, s.lon]);
  const line = roadLine(data.path, points);
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
  setText($("map-status"), "");
  drawText(data);
  if (L) {
    ensureMap();
    map.invalidateSize();
    drawMap(data, fit);
  }
}

// Loads the map of bus `id` into the open sheet.
export async function showBus(id) {
  if (!validId(id)) return;
  openId = id;
  const mine = ++request;
  setText($("map-status"), "Loading the map…");
  setText($("map-where"), "");
  $("map-stops").replaceChildren();
  $("map-centre").disabled = true;
  $("osm-credit").hidden = false;
  try {
    // L is kept once Leaflet loads, even if the bus doesn't: then the next
    // snapshot (refreshMap) draws the map.
    const leaflet = loadLeaflet().then((module) => { L = module; });
    const [data] = await Promise.all([getJSON(`/api/bus/${encodeURIComponent(id)}`), leaflet]);
    if (mine === request) show(obj(data), true);
  } catch {
    if (mine === request) setText($("map-status"), "The map couldn't be loaded. The bus may have left the board, or the server is busy.");
  }
}

// When the sheet closes: a load still on its way is dropped.
export function closeMap() {
  openId = null;
  request++;
  $("osm-credit").hidden = true;
}

// Called with each new snapshot while a bus's sheet is open: moves the bus
// without refitting, so a map you have panned stays put.
export async function refreshMap() {
  if (!openId) return;
  if (!L) return;
  const id = openId;
  const mine = request;
  try {
    const data = await getJSON(`/api/bus/${encodeURIComponent(id)}`);
    if (mine === request && openId === id) show(obj(data), false);
  } catch { /* keep the last map */ }
}

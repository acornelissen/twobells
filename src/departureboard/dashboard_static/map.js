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

// The bus marker: an arrow pointing along the bus's bearing, turned through
// the CSSOM (the CSP allows that; it forbids style attributes in markup).
function busIcon(bearing) {
  // Drawn pointing north, so it turns by the bearing itself (clockwise from north).
  const arrow = bearing !== null
    ? svg("svg", { class: "bus-arrow", viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" },
      svg("path", { d: "M12 2 L20 21 L12 16 L4 21 Z" }))
    : svg("svg", { class: "bus-arrow", viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" },
      svg("circle", { cx: 12, cy: 12, r: 7 }));
  if (bearing !== null) arrow.style.transform = `rotate(${bearing}deg)`;
  return L.divIcon({ html: h("span", { class: "bus-pin" }, arrow), className: "bus-marker", iconSize: [36, 36], iconAnchor: [18, 18] });
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
  if (points.length > 1) L.polyline(points, { className: "route-line", interactive: false }).addTo(layers);
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
    busMarker = L.marker(busPosition, { icon: busIcon(num(vehicle.bearing)), keyboard: false, zIndexOffset: 1000 })
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
  setText($("bus-title"), `${str(data.line)} to ${str(data.destination)}${str(data.stop) ? `, Stop ${str(data.stop)}` : ""}`);
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

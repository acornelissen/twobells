// "Around here": the trains on the line, drawn as a list of stations with
// each placed train between them, and the area map of every live bus on
// your lines near your stops (GET /api/area). The map loads Leaflet only
// when opened; its markers and popups are DOM nodes, never strings (Leaflet
// would parse a string as HTML), and a list beside it says the same in words.

import { $, h, s as svg, fill, setText, str, num, list, obj, openDialog, setUpDialog } from "./dom.js";
import { getJSON } from "./api.js";
import { age, now, parseAt } from "./time.js";
import { loadLeaflet, TILES } from "./map.js";

const AREA_EVERY_MS = 25_000; // the summary asks for the area at most this often
const MAX_VEHICLES = 80;

let L = null;
let map = null;
let layers = null;
let markers = new Map(); // list index -> Leaflet marker
let area = null; // the last /api/area answer
let areaAsked = 0;
let request = 0;
let fitted = false;

const dialog = () => $("area-dialog");
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// --- The line: stations in order, trains placed between them.

// One train's stations in calling order: before this station, this station, after.
function journeyNames(row, station) {
  const names = (points) => list(points).map((p) => str(obj(p)?.name)).filter(Boolean);
  return [...names(row.previous_points), station, ...names(row.calling_points)];
}

// Joins journeys into one line of stations. The longest leads; a station
// another journey adds goes in next to its neighbour there.
export function lineOf(journeys) {
  const sorted = journeys.filter((j) => j.length > 1).sort((a, b) => b.length - a.length);
  if (!sorted.length) return [];
  const line = [...new Set(sorted[0])];
  for (const journey of sorted.slice(1)) {
    const shared = journey.filter((name) => line.includes(name));
    // Run the same way as the line: reverse a journey whose shared stations go backwards.
    const ordered = shared.length > 1 && line.indexOf(shared[0]) > line.indexOf(shared[shared.length - 1])
      ? [...journey].reverse() : journey;
    ordered.forEach((name, i) => {
      if (line.includes(name)) return;
      const before = ordered.slice(0, i).reverse().find((n) => line.includes(n));
      const after = ordered.slice(i + 1).find((n) => line.includes(n));
      if (before !== undefined) line.splice(line.indexOf(before) + 1, 0, name);
      else if (after !== undefined) line.splice(line.indexOf(after), 0, name);
    });
  }
  return line;
}

// Where a train is on the line: { at: index } at a station, or
// { from, to } between two; null when it isn't on the line or not placed.
export function placeOnLine(row, line) {
  const strip = obj(obj(row.position)?.strip);
  const at = num(strip?.train_at);
  const stops = list(strip?.stops).map(str);
  if (!strip || at === null || stops.length < 2) return null;
  const i = Math.max(0, Math.min(Math.floor(at), stops.length - 1));
  const part = at - i;
  const here = line.indexOf(stops[i]);
  if (here < 0) return null;
  if (part < 0.05 || i === stops.length - 1) return { at: here };
  const next = line.indexOf(stops[i + 1]);
  if (next < 0) return null;
  if (part > 0.95) return { at: next };
  return { from: Math.min(here, next), to: Math.max(here, next) };
}

// One train in the line, at line position `index`; the arrow points the way
// it is going, and the words say it too.
function trainItem(row, line, index) {
  const destination = str(row.destination);
  const towards = line.indexOf(destination);
  const where = str(obj(row.position)?.where);
  const arrow = towards < 0 ? "" : towards < index ? "▲" : "▼";
  return h("li", { class: "line-train" },
    h("span", { class: "glyph", "aria-hidden": "true" }, arrow), " ",
    h("strong", {}, `${str(row.scheduled)} to ${destination}`), where ? `: ${where}` : "");
}

// Moves Manchester (the filter station) to the top when it's at the bottom.
function oriented(line, filterName) {
  if (filterName && line.indexOf(filterName) > line.length / 2) return [...line].reverse();
  return line;
}

export function renderLine(snapshot) {
  const trains = obj(snapshot.trains);
  const part = $("line-part");
  part.hidden = !trains;
  if (!trains) return;
  const station = str(trains.station) || "This station";
  const rows = list(trains.rows).filter(obj);
  const filterName = rows.flatMap((r) => list(r.calling_points)).map(obj).filter(Boolean)
    .find((p) => str(p.crs) && str(p.crs) === str(trains.filter))?.name;
  const line = oriented(lineOf(rows.map((r) => journeyNames(r, station))), str(filterName));
  const at = new Map(); // station index -> [rows]
  const between = new Map(); // "from" index of a stretch -> [rows]
  let placed = 0;
  for (const row of rows) {
    const where = placeOnLine(row, line);
    if (!where) continue;
    placed++;
    const bucket = where.at !== undefined ? at : between;
    const key = where.at !== undefined ? where.at : where.from;
    bucket.set(key, [...(bucket.get(key) || []), row]);
  }
  setText($("line-summary"), !line.length ? "The line shows once trains with calling points are listed."
    : placed ? `${placed} ${placed === 1 ? "train" : "trains"} placed on the line.`
      : "No live train positions right now.");
  const items = [];
  line.forEach((name, i) => {
    const trainsHere = at.get(i) || [];
    items.push(h("li", { class: ["line-station", name === station ? "yours" : null] },
      h("span", { class: "line-name" }, name), name === station ? h("span", { class: "cp-state" }, " (this station)") : null,
      trainsHere.length ? h("ul", { class: "line-trains" }, trainsHere.map((r) => trainItem(r, line, i))) : null));
    const moving = between.get(i) || [];
    if (moving.length && i + 1 < line.length) {
      items.push(h("li", { class: "line-between" },
        h("span", { class: "vh" }, `Between ${name} and ${line[i + 1]}: `),
        h("ul", { class: "line-trains" }, moving.map((r) => trainItem(r, line, i + 0.5)))));
    }
  });
  fill($("line-diagram"), items);
}

// --- The area map.

function reported(vehicle) {
  const recorded = parseAt(vehicle.recorded_at);
  const seconds = recorded !== null ? (now() - recorded) / 1000 : num(vehicle.age_s);
  return seconds === null ? "" : `reported ${age(seconds)}`;
}

function vehicles(data) {
  return list(data?.vehicles).filter((v) => obj(v) && num(v.lat) !== null && num(v.lon) !== null)
    .slice(0, MAX_VEHICLES);
}

function describe(vehicle) {
  const next = str(vehicle.next_stop);
  return [`${str(vehicle.line)} to ${str(vehicle.destination)}`, next ? `next stop ${next}` : "",
    reported(vehicle)].filter(Boolean).join(", ");
}

function summary(data) {
  const found = vehicles(data);
  if (!data) return "No live buses around your stops yet.";
  if (!found.length) return "No live buses on your lines around your stops right now.";
  return `${found.length} ${found.length === 1 ? "bus" : "buses"} on your lines around your stops.`;
}

function busIcon(vehicle) {
  const bearing = num(vehicle.bearing);
  const arrow = svg("svg", { class: "area-arrow", viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" },
    bearing !== null ? svg("path", { d: "M12 0 L16.5 3.8 L12 2.8 L7.5 3.8 Z" }) : null);
  if (bearing !== null) arrow.style.transform = `rotate(${bearing}deg)`;
  return L.divIcon({
    html: h("span", { class: "area-pin" }, arrow, h("span", { class: "area-pin-line" }, str(vehicle.line))),
    className: "area-marker", iconSize: [40, 40], iconAnchor: [20, 20],
  });
}

function ensureMap() {
  if (map) return;
  const animate = !reduceMotion();
  map = L.map($("area-map"), {
    zoomControl: false, attributionControl: false,
    zoomAnimation: animate, fadeAnimation: animate, markerZoomAnimation: animate,
  });
  L.tileLayer(TILES, { maxZoom: 19, referrerPolicy: "strict-origin-when-cross-origin" }).addTo(map);
  layers = L.layerGroup().addTo(map);
}

function fitAll() {
  if (!map || !area) return;
  const points = [...list(area.stops), ...vehicles(area)].filter(obj)
    .filter((p) => num(p.lat) !== null && num(p.lon) !== null).map((p) => [p.lat, p.lon]);
  const box = list(area.box).map(num);
  if (points.length > 1) map.fitBounds(points, { padding: [32, 32], maxZoom: 16, animate: false });
  else if (box.length === 4 && box.every((v) => v !== null)) map.fitBounds([[box[1], box[0]], [box[3], box[2]]], { animate: false });
  else if (points.length) map.setView(points[0], 15, { animate: false });
}

function drawMap() {
  layers.clearLayers();
  markers = new Map();
  for (const route of list(area.routes).filter(obj)) {
    const points = list(route.points).filter((p) => Array.isArray(p) && num(p[0]) !== null && num(p[1]) !== null);
    if (points.length > 1) L.polyline(points, { className: "area-route", interactive: false }).addTo(layers);
  }
  for (const stop of list(area.stops).filter(obj)) {
    if (num(stop.lat) === null || num(stop.lon) === null) continue;
    L.circleMarker([stop.lat, stop.lon], { className: "stop-yours", radius: 8 })
      .bindTooltip(h("span", {}, h("strong", {}, `Stop ${str(stop.label)}`), ` ${str(stop.name)}`),
        { permanent: true, direction: "top", offset: [0, -6] })
      .addTo(layers);
  }
  vehicles(area).forEach((vehicle, i) => {
    const marker = L.marker([vehicle.lat, vehicle.lon], { icon: busIcon(vehicle), keyboard: false, zIndexOffset: 1000 })
      .bindPopup(h("span", {}, h("strong", {}, `${str(vehicle.line)} to ${str(vehicle.destination)}`),
        str(vehicle.next_stop) ? `. Next stop ${str(vehicle.next_stop)}` : "", `. ${reported(vehicle)}.`))
      .addTo(layers);
    markers.set(i, marker);
  });
}

// Shows one bus on the map, from its button in the list.
function showBus(i) {
  const marker = markers.get(i);
  if (!map || !marker) return;
  map.setView(marker.getLatLng(), Math.max(map.getZoom(), 16), { animate: !reduceMotion() });
  marker.openPopup();
}

function drawList() {
  const found = vehicles(area);
  setText($("area-dialog-summary"), summary(area));
  fill($("area-vehicles"), found.map((vehicle, i) => h("li", {},
    h("span", {}, describe(vehicle)), " ",
    L ? h("button", { type: "button", class: "link", on: { click: () => showBus(i) } },
      "Show on map", h("span", { class: "vh" }, `: ${str(vehicle.line)} to ${str(vehicle.destination)}`)) : null)));
  fill($("area-stops"), list(area?.stops).filter(obj).map((stop) =>
    h("li", {}, h("strong", {}, `Stop ${str(stop.label)}`), ` ${str(stop.name)}`)));
}

function show() {
  setText($("area-status"), area ? "" : "There's no area map yet: the buses haven't been checked, or there's no timetable.");
  drawList();
  if (!L || !area) return;
  ensureMap();
  map.invalidateSize();
  drawMap();
  if (!fitted) { fitAll(); fitted = true; }
}

async function fetchArea() {
  areaAsked = Date.now();
  const mine = ++request;
  try {
    const data = await getJSON("/api/area");
    if (mine === request) area = obj(data);
  } catch { /* keep the last one */ }
}

export function setUpArea() {
  setUpDialog(dialog(), () => { $("osm-credit").hidden = true; });
  $("area-open").addEventListener("click", (event) => openArea(event.currentTarget));
  $("area-zoom-in").addEventListener("click", () => map && map.zoomIn(1, { animate: !reduceMotion() }));
  $("area-zoom-out").addEventListener("click", () => map && map.zoomOut(1, { animate: !reduceMotion() }));
  $("area-fit").addEventListener("click", fitAll);
}

async function openArea(opener) {
  fitted = false;
  setText($("area-status"), "Loading the map…");
  $("osm-credit").hidden = false;
  openDialog(dialog(), opener);
  try {
    const [, module] = await Promise.all([fetchArea(), loadLeaflet()]);
    L = module;
    show();
  } catch {
    setText($("area-status"), "The map couldn't be loaded. Try again in a moment.");
    drawList();
  }
}

// Called with each snapshot: the line, the summary, and the open map.
export async function renderArea(snapshot) {
  renderLine(snapshot);
  const section = $("area");
  section.hidden = !obj(snapshot.trains) && !obj(snapshot.buses);
  const buses = !!obj(snapshot.buses);
  $("area-buses").hidden = !buses;
  if (!buses) return;
  if (dialog().open || Date.now() - areaAsked > AREA_EVERY_MS) await fetchArea();
  setText($("area-summary"), summary(area));
  if (dialog().open) show();
}

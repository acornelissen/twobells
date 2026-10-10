// Maps: Leaflet, loaded once, the first time any map opens. Leaflet gets
// DOM nodes, never strings.

import { $, h } from "../dashboard_static/dom.js";
import { icon } from "../dashboard_static/icons.js";
import { note } from "./say.js";
import { state } from "./state.js";

const LEAFLET = "../dashboard_static/vendor/leaflet-1.9.4/";
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const UK_CENTRE = [54.5, -3.0];

export let L = null;
let leafletLoading = null;
export const maps = {};
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

export function mapFor(name) {
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

export const isOpen = (name) => Boolean(L && maps[name] && !$(`${name}-map-box`).hidden);

export function pin(text, extra = "") {
  return L.divIcon({
    html: h("span", { class: ["pin", extra] }, text),
    className: "pin-marker",
    iconSize: [32, 32],
    iconAnchor: [16, 16],
  });
}

function homePin() {
  return L.divIcon({
    html: h("span", { class: "pin pin-home" }, icon("house")),
    className: "pin-marker",
    iconSize: [32, 32],
    iconAnchor: [16, 16],
  });
}

export function addHome(layer) {
  if (!state.home) return null;
  return L.marker([state.home.lat, state.home.lon], { icon: homePin(), keyboard: false, zIndexOffset: 1000 })
    .bindTooltip(h("span", {}, "Home"), { direction: "top", offset: [0, -14] })
    .addTo(layer);
}

// A "Show the map" button: opens the map below it, loading Leaflet the first
// time, then draws it.
export function setUpMapToggle(name, draw) {
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

export function fit(name, points, zoom) {
  const { map } = maps[name];
  if (points.length > 1) map.fitBounds(points, { padding: [32, 32], maxZoom: 17, animate: false });
  else if (points.length === 1) map.setView(points[0], zoom, { animate: false });
  else map.setView(UK_CENTRE, 5, { animate: false });
}

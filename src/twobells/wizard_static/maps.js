// Maps: Leaflet, loaded once, the first time any map opens
// (dashboard_static/basemap.js). Leaflet gets DOM nodes, never strings.

import { $, h } from "../dashboard_static/dom.js";
import { loadLeaflet, makeMap } from "../dashboard_static/basemap.js";
import { icon } from "../dashboard_static/icons.js";
import { note } from "./say.js";
import { state } from "./state.js";

const UK_CENTRE = [54.5, -3.0];

export let L = null;
export const maps = {};

export function mapFor(name) {
  if (!maps[name]) {
    const map = makeMap($(`${name}-map`));
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
    .bindTooltip(h("span", {}, state.only ? "Roughly home" : "Home"), { direction: "top", offset: [0, -14] })
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
      L = await loadLeaflet();
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

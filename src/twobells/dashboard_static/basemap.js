// Leaflet, loaded once, the first time any map opens, and a map on
// OpenStreetMap's tiles. The dashboard's maps (map.js, area.js) and the setup
// wizard's (wizard_static/maps.js) all start here, so this imports only
// dom.js: the wizard is served nothing else of the dashboard's.

import { h, reduceMotion } from "./dom.js";

const LEAFLET = "./vendor/leaflet-1.9.4/";
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

let L = null;
let leafletLoading = null;

// Resolves to the Leaflet module; a failed load is tried again next time.
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

// A map in `element`, once loadLeaflet has resolved. It doesn't animate for a
// reader who asks for less motion. `zoomControl: false` where our own
// buttons, outside the map, zoom it.
export function makeMap(element, { zoomControl = true } = {}) {
  const animate = !reduceMotion();
  const map = L.map(element, {
    zoomControl,
    attributionControl: false, // the credit is our own DOM text under the map
    zoomAnimation: animate, fadeAnimation: animate, markerZoomAnimation: animate,
  });
  L.tileLayer(TILES, {
    maxZoom: 19,
    referrerPolicy: "strict-origin-when-cross-origin", // OSM's tile policy wants a referrer
  }).addTo(map);
  return map;
}

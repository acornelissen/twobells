// Step 3: bus stops.

import { $, h, fill, str, num, list, obj } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { L, addHome, fit, isOpen, mapFor, pin, setUpMapToggle } from "./maps.js";
import { note, refuse, say, setError, shout } from "./say.js";
import { distance, homeKey, state, stopName } from "./state.js";
import { go } from "./steps.js";
import { enterWalk } from "./walk.js";

const LABEL = /^[A-Za-z0-9]{1,3}$/;
const MAX_STOPS = 10;
const BEARINGS = {
  N: "north", NE: "north-east", E: "east", SE: "south-east",
  S: "south", SW: "south-west", W: "west", NW: "north-west",
};

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

export async function enterStops() {
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

export function setUpStops() {
  setUpMapToggle("stops", drawStopsMap);
  $("stops-next").addEventListener("click", leaveStops);
}

// The tram stops step, only where home is near Manchester's Metrolink.
// They come from a list bundled with the app, so finding them sends home
// nowhere. Each ticked stop gets a label on the board, T then U, V and W, in
// the order listed. With none near home (and none saved), the step isn't
// there at all, and the walk times come next.

import { $, h, fill, str, num, list, obj } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { note, refuse, say, setError, shout } from "./say.js";
import { distance, homeKey, state } from "./state.js";
import { go, numberSteps } from "./steps.js";
import { enterWalk } from "./walk.js";

const CODE = /^9400ZZMA[A-Z]{3}$/;
const LABELS = ["T", "U", "V", "W"];
export const MAX_TRAMS = LABELS.length;

const ticked = () => state.trams.filter((stop) => state.tramsTicked.has(stop.code));

// The tram stops near home, once for each home; and today's, so a stop
// chosen before is listed even when it isn't among the nearest.
async function findTrams() {
  if (state.tramsFor === homeKey()) return;
  state.trams = [];
  if (state.home) {
    const data = await call("trams", { lat: state.home.lat, lon: state.home.lon });
    state.trams = list(data.trams).map(obj)
      .filter((s) => s && CODE.test(str(s.code)))
      .map((s) => ({ code: s.code, name: str(s.name) || s.code, distance_m: num(s.distance_m) }));
  }
  for (const saved of state.saved.trams) {
    if (!state.trams.some((stop) => stop.code === saved.code)) {
      state.trams.push({ code: saved.code, name: saved.name || saved.code, distance_m: null });
    }
  }
  state.tramsFor = homeKey();
}

function drawTrams() {
  fill($("trams-list"), state.trams.map((stop, i) => {
    const id = `tram-${i}`;
    const about = stop.distance_m === null ? "One of your tram stops now" : `${distance(stop.distance_m)} away`;
    const box = h("input", {
      type: "checkbox", id, value: stop.code, checked: state.tramsTicked.has(stop.code), "aria-describedby": `${id}-about`,
    });
    box.addEventListener("change", () => {
      setError("trams", "");
      if (box.checked && state.tramsTicked.size >= MAX_TRAMS) {
        box.checked = false;
        setError("trams", `Pick at most ${MAX_TRAMS} tram stops.`);
        shout(`Pick at most ${MAX_TRAMS} tram stops.`);
        return;
      }
      if (box.checked) state.tramsTicked.add(stop.code);
      else state.tramsTicked.delete(stop.code);
    });
    return h("li", { class: "stop" },
      h("div", { class: "option" }, box, h("label", { for: id }, `${stop.name} tram stop`)),
      h("p", { class: "hint about", id: `${id}-about` }, `${about}.`));
  }));
}

function enterTrams() {
  go("trams");
  drawTrams();
}

// After the bus stops: the tram step where there are tram stops to offer,
// else on to the walk times. With nothing picked yet (no station, no stops),
// there must be tram stops to pick from: false, and why, if there are none.
export async function afterStops(nothingYet) {
  try {
    await findTrams();
  } catch (error) {
    // The list is bundled, so this is rare; the step is left out, and trams can be set up later.
    note("stops-status", `${error.message} Tram stops aren't offered.`);
    state.trams = [];
  }
  state.askTrams = state.trams.length > 0;
  numberSteps();
  if (!state.askTrams) {
    state.chosenTrams = [];
    if (nothingYet) return false;
    enterWalk();
    return true;
  }
  enterTrams();
  return true;
}

function leaveTrams() {
  setError("trams", "");
  const chosen = ticked().map((stop, i) => ({ label: LABELS[i], code: stop.code, name: stop.name }));
  if (!chosen.length && !state.station && !state.chosen.length) {
    const first = $("trams-list").querySelector("input");
    return refuse("trams", `Pick at least one: with no station, bus stops or tram stops, the ${state.noun} has nothing to show.`,
      first || $("trams-heading"));
  }
  state.chosenTrams = chosen;
  say(chosen.length ? `${chosen.length} tram ${chosen.length === 1 ? "stop" : "stops"} picked.` : "No trams.");
  enterWalk();
  return true;
}

export function setUpTrams() {
  $("trams-next").addEventListener("click", leaveTrams);
}

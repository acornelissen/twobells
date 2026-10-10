// Step 4: walk times.

import { $, h, fill, num, obj } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { enterKeys } from "./keys.js";
import { note, refuse, say, setError, shout } from "./say.js";
import { homeKey, state, stopName } from "./state.js";
import { go } from "./steps.js";

const MAX_WALK = 60;

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

export async function enterWalk() {
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

export function leaveWalk() {
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

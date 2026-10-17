// Step 4: walk times.

import { $, h, fill, num, obj } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { enterFinish } from "./finish.js";
import { enterKeys } from "./keys.js";
import { note, refuse, say, setError, shout } from "./say.js";
import { homeKey, state, stopName } from "./state.js";
import { go, shownSteps } from "./steps.js";

const MAX_WALK = 60;

const SOURCES = {
  route: ["Route", "Worked out along streets and paths."],
  estimate: ["Estimate", "Guessed from the distance; check it."],
  none: ["Not known", "Type it in, or leave it empty."],
  edited: ["Yours", "Changed by you."],
  saved: ["Saved", "As it is now."],
  rough: ["Estimate", "Worked out from roughly where home is; check it."],
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

// A shortcut starts each walk from today's, where there is one: the
// station's while it is the same station, and each stop's still ticked.
function keepSavedWalks() {
  const saved = new Map(state.saved.stops.map((stop) => [stop.atco, stop.walk]));
  for (const tram of state.saved.trams) saved.set(tram.code, tram.walk);
  if (state.saved.station) saved.set(state.saved.station.crs, state.saved.station.walk);
  for (const walk of state.walk) {
    const minutes = saved.get(walk.key);
    if (minutes !== undefined && minutes !== null) Object.assign(walk, { minutes, source: "saved" });
  }
}

export async function enterWalk() {
  go("walk");
  // A shortcut asks only about its own part.
  const station = state.only === "stops" ? null : state.station;
  const stops = state.only === "station" ? [] : state.chosen;
  const trams = state.only ? [] : state.chosenTrams;
  const key = JSON.stringify([homeKey(), station && station.crs, stops.map((s) => s.atco), trams.map((t) => t.code)]);
  if (state.walkFor === key) return; // keep what was typed
  state.walk = [];
  if (station) state.walk.push({ key: station.crs, what: `${station.name} station` });
  for (const stop of stops) state.walk.push({ key: stop.atco, what: `stop ${stop.label}, ${stopName(stop)}` });
  for (const tram of trams) state.walk.push({ key: tram.code, what: tram.place || `${tram.name} tram stop` });
  for (const walk of state.walk) Object.assign(walk, { minutes: null, source: "none" });
  if (state.only) keepSavedWalks();
  const asking = state.walk.filter((walk) => walk.source === "none").map((walk) => walk.key);
  fill($("walk-list"));
  if (asking.length && state.home) {
    note("walk-status", "Working out the walks…");
    try {
      const data = await call("walk", {
        home: { lat: state.home.lat, lon: state.home.lon },
        station_crs: station && asking.includes(station.crs) ? station.crs : null,
        stop_atcos: stops.map((s) => s.atco).filter((atco) => asking.includes(atco)),
        ...(trams.length ? { tram_codes: trams.map((t) => t.code).filter((code) => asking.includes(code)) } : {}),
      });
      const found = obj(data.walk) || {};
      for (const walk of state.walk) {
        const answer = asking.includes(walk.key) ? obj(found[walk.key]) : null;
        const minutes = answer ? num(answer.minutes) : null;
        if (minutes !== null && Number.isInteger(minutes) && minutes >= 0) {
          walk.minutes = Math.min(minutes, MAX_WALK);
          if (state.only) walk.source = "rough"; // a shortcut's home is only roughly where it is
          else walk.source = answer.source === "route" ? "route" : "estimate";
        }
      }
      note("walk-status", "");
      say("Walk times worked out. Check them below.");
    } catch (error) {
      note("walk-status", `${error.message} Type the walks in yourself.`);
      shout(error.message);
    }
  } else {
    note("walk-status", "");
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
  // A shortcut skips the keys while the one it needs is set.
  if (shownSteps().includes("keys")) enterKeys();
  else enterFinish();
  return true;
}

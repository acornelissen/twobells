// Step 8: check, search and save.

import { $, h, fill, str, num, list, obj, own } from "../dashboard_static/dom.js";
import { ApiError, POLL_FAILURES, POLL_MS, call, sleep } from "./api.js";
import { modeViews, showViews } from "./presets.js";
import { followRoads, setUpRoads } from "./roads.js";
import { say, setError, shout } from "./say.js";
import { state, stopName } from "./state.js";
import { go } from "./steps.js";

const LINK = /^(\/(?!\/)|https?:\/\/)/;

// The keys pasted on the keys step, by name: sent with the search (the
// bus key) and when saving.
export function pastedKeys() {
  const keys = {};
  for (const key of state.keys) {
    const input = $(`key-${key.name}`); // none where a shortcut skipped the keys
    const value = input ? input.value.trim() : "";
    if (value) keys[key.name] = value;
  }
  return keys;
}

// Whether a shortcut's stops are today's, each by the same label.
function sameStopsAsSaved() {
  const saved = state.saved.stops;
  return saved.length === state.chosen.length
    && state.chosen.every((stop) => saved.some((s) => s.label === stop.label && s.atco === stop.atco));
}

// Whether saving first searches for the bus companies: with stops, unless a
// shortcut changes only the station, or keeps today's stops as they are.
function searching() {
  if (state.only === "station" || !state.chosen.length) return false;
  return !(state.only === "stops" && sameStopsAsSaved());
}

function summaryRow(term, ...details) {
  return [h("dt", {}, term), h("dd", {}, ...details)];
}

function minutesText(key) {
  const minutes = state.walkMinutes[key];
  return minutes === undefined ? "no walk time" : `${minutes} min walk`;
}

export function enterFinish() {
  go("finish");
  const keys = Object.keys(pastedKeys()).map((name) => (state.keys.find((k) => k.name === name) || {}).label || name);
  // A shortcut shows, and saves, only its own part.
  const shows = (part) => !state.only || state.only === part;
  fill($("summary"),
    state.only ? null : summaryRow("Home", state.home.label,
      h("span", { class: "hint" }, " (only a position rounded to about 1 km is saved, for the rain warning)")),
    shows("station") ? summaryRow("Station", state.station
      ? `${state.station.name} (${state.station.crs}), ${minutesText(state.station.crs)}` : "No trains") : null,
    shows("station") && state.filter ? summaryRow("Only trains calling at", `${state.filter.name} (${state.filter.crs})`) : null,
    shows("stops") ? summaryRow("Bus stops", state.chosen.length
      ? h("ul", {}, state.chosen.map((s) => h("li", {}, `${s.label}: ${stopName(s)}, ${minutesText(s.atco)}`)))
      : "No buses") : null,
    summaryRow("Keys to save", keys.length ? keys.join(", ") : "None; the keys already set stay"),
    state.askViews ? summaryRow("Show the departures on", state.views
      .filter((view) => state.chosenViews.includes(view.name)).map((view) => view.label).join(", ")) : null,
    state.askShow && modeViews().length ? summaryRow("Modes of transport", h("ul", {}, modeViews().map((view) => {
      const names = own(state.chosenModes, view.name) || [];
      const labels = state.modes.offered.filter((m) => names.includes(m.name)).map((m) => m.label);
      return h("li", {}, `${view.label}: ${labels.join(" and ")}`);
    }))) : null,
    state.askShow ? summaryRow("What to show", h("ul", {}, showViews().map((view) => {
      const preset = state.show.presets.find((p) => p.name === own(state.chosenShow, view.name));
      return h("li", {}, `${view.label}: ${preset ? preset.label : "as it is now"}`);
    }))) : null);
  $("only-hint").hidden = !state.only;
  const buses = searching();
  $("routes-box").hidden = !buses;
  $("search-hint").hidden = !buses;
  setError("finish", "");
}

function showJob(job) {
  $("job-box").hidden = false;
  const step = str(job.step) || (job.state === "done" ? "Search finished" : "Starting the search");
  if ($("job-status").textContent !== step) $("job-status").textContent = step;
  const done = num(job.done);
  const total = num(job.total);
  const bar = $("job-progress");
  if (total && done !== null) {
    bar.max = total;
    bar.value = Math.min(done, total);
    $("job-count").textContent = `${Math.min(done, total)} of ${total}`;
  } else {
    bar.removeAttribute("value"); // indeterminate: still working out how much there is
    $("job-count").textContent = "";
  }
}

async function search() {
  const stops = Object.fromEntries(state.chosen.map((s) => [s.label, s.atco]));
  const walk = {};
  for (const stop of state.chosen) {
    if (state.walkMinutes[stop.atco] !== undefined) walk[stop.label] = state.walkMinutes[stop.atco];
  }
  const body = { stops, routes: $("routes").checked, walk };
  const bus = pastedKeys().BODS_API_KEY;
  if (bus) body.keys = { BODS_API_KEY: bus };
  let job;
  try {
    job = obj((await call("start", body)).job);
  } catch (error) {
    if (error.status !== 409) throw error;
    job = obj((await call("job")).job); // one already running, perhaps from before a reload: follow it
  }
  $("job-cancel").hidden = false;
  let failures = 0;
  while (job && (job.state === "running" || job.state === "queued")) {
    showJob(job);
    await sleep(POLL_MS);
    try {
      job = obj((await call("job")).job);
      failures = 0;
    } catch (error) {
      if (++failures >= POLL_FAILURES) throw error;
    }
  }
  $("job-cancel").hidden = true;
  if (!job || job.state !== "done") {
    throw new ApiError(str(job && job.message) === "Cancelled"
      ? "Search cancelled. Press Save to start again."
      : `The search failed: ${str(job && job.message) || "no reason given"}.`, 0, null);
  }
  showJob(job);
  return job;
}

function payload() {
  const keys = pastedKeys();
  const station = { station_crs: state.station ? state.station.crs : null, filter_crs: state.filter ? state.filter.crs : null };
  const stops = { stops: Object.fromEntries(state.chosen.map((s) => [s.label, s.atco])) };
  let body;
  if (state.only === "station") body = { only: "station", ...station, walk: state.walkMinutes };
  else if (state.only === "stops") body = { only: "stops", ...stops, walk: state.walkMinutes };
  else {
    body = {
      home: { lat: state.home.lat, lon: state.home.lon, ...(state.home.country ? { country: state.home.country } : {}) },
      ...station,
      ...stops,
      walk: state.walkMinutes,
    };
  }
  if (Object.keys(keys).length) body.keys = keys;
  if (state.chosenViews.length) body.views = state.chosenViews;
  if (state.askShow) body.show = state.chosenShow;
  if (state.askShow && Object.keys(state.chosenModes).length) body.modes = state.chosenModes;
  return body;
}

async function save(button) {
  if (state.saving) return;
  state.saving = true;
  for (const el of document.querySelectorAll("#finish-actions button")) el.setAttribute("aria-disabled", "true");
  setError("finish", "");
  try {
    const job = searching() ? await search() : null;
    const roads = job ? await followRoads(job) : "none";
    say(roads === "found" ? "Found the roads for the bus maps. Saving…" : "Saving…");
    const data = await call("finish", payload());
    finished(data, job, roads);
  } catch (error) {
    setError("finish", error.message);
    shout(error.message);
    button.focus();
  } finally {
    state.saving = false;
    for (const el of document.querySelectorAll("#finish-actions button")) el.removeAttribute("aria-disabled");
    $("job-cancel").hidden = true;
  }
}

function finished(data, job, roads) {
  fill($("done-message"), str(data.message) || "Saved.");
  const found = job ? obj(job.found) : null;
  const items = [];
  if (found) {
    const lines = obj(found.lines) || {};
    for (const operator of list(found.operators).map(str).filter(Boolean)) {
      const its = list(lines[operator]).map(str).filter(Boolean);
      items.push(`Bus company ${operator}${its.length ? `: ${its.length === 1 ? "line" : "lines"} ${its.join(", ")}` : ""}.`);
    }
    for (const text of list(found.notes).map(str).filter(Boolean)) items.push(text);
  }
  if (roads === "carried") {
    items.push("The roads for the bus maps are still being found, and finish on their own. "
      + "The settings site's Overview says how far they have got.");
  }
  fill($("done-found"), items.map((text) => h("li", {}, text)));
  const links = list(data.links).map(obj).filter((l) => l && str(l.label) && LINK.test(str(l.href)));
  fill($("done-links"), links.map((l) => h("li", {}, h("a", { href: l.href }, l.label))));
  go("done");
  say("Setup is done.");
}

export function setUpFinish() {
  $("save").addEventListener("click", (event) => save(event.currentTarget));
  $("job-cancel").addEventListener("click", async () => {
    try { await call("cancel"); say("Cancelling the search…"); } catch (error) { shout(error.message); }
  });
  setUpRoads();
}

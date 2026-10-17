// The first-run setup wizard: from home to saved settings in seven steps, or
// eight where the install offers more than one view to turn on. Two
// shortcuts start part way, at the station or the stops (?step=station,
// ?step=stops), from today's choices, and save only that part.
//
// It talks only to its host's setup API (setup/api.py), by relative URLs, so
// the page works under whatever prefix the host mounts it at. Every step
// works without a map: maps are opt-in (their pictures come from
// OpenStreetMap, which then sees roughly where home is), load Leaflet only
// when first opened, and show what the lists already say. Text from the
// server goes in as text through h() and fill(), never as HTML, and Leaflet
// gets DOM nodes, never strings.
//
// This is the entry point. Each step is its own module (home.js to
// finish.js, and roads.js for the wait after the search), beside the
// helpers they share: api.js, say.js, state.js, steps.js and maps.js.

import { $, fill, str, num, list, obj, own } from "../dashboard_static/dom.js";
import { call, signIn } from "./api.js";
import { setUpFinish } from "./finish.js";
import { setUpHome } from "./home.js";
import { leaveKeys } from "./keys.js";
import { KEEP, leaveShow } from "./presets.js";
import { note } from "./say.js";
import { state } from "./state.js";
import { enterStation, setUpStation } from "./station.js";
import { LEAVE, go, numberSteps } from "./steps.js";
import { enterStops, setUpStops } from "./stops.js";
import { leaveViews } from "./views.js";
import { leaveWalk } from "./walk.js";

const NOUN = /^[a-z]{1,20}$/;
const VIEW = /^[a-z]{1,20}$/;
const PRESET = /^[a-z]{1,20}$/;
const MODE = /^[a-z]{1,20}$/;
const PATH = /^\/(?!\/)[A-Za-z0-9._~/-]*$/; // a plain path on this site, like /settings
const CRS = /^[A-Za-z]{3}$/;
const LABEL = /^[A-Za-z0-9]{1,3}$/;
const ATCO = /^[0-9]{3}[0-9A-Za-z]{1,9}$/;
const MAX_WALK = 60;
// The shortcuts a link may ask for with ?step=, and their page titles.
// Anything else starts the whole wizard at home.
const SHORTCUTS = { station: "Change your station", stops: "Change your bus stops" };

// The shortcut `search` (the page's query) asks for, or "".
export function shortcut(search) {
  const asked = new URLSearchParams(search).get("step");
  return asked !== null && Object.hasOwn(SHORTCUTS, asked) ? asked : "";
}

// Today's choices from saved, checked as anything from the server is.
function readSaved(data) {
  const home = obj(data.home);
  const station = obj(data.station);
  const filter = obj(data.filter);
  const minutes = (value) => {
    const m = num(value);
    return m !== null && Number.isInteger(m) && m >= 0 && m <= MAX_WALK ? m : null;
  };
  return {
    home: home && num(home.lat) !== null && num(home.lon) !== null ? { lat: home.lat, lon: home.lon } : null,
    station: station && CRS.test(str(station.crs))
      ? { crs: station.crs, name: str(station.name) || station.crs, walk: minutes(station.walk) } : null,
    filter: filter && CRS.test(str(filter.crs)) ? { crs: filter.crs, name: str(filter.name) || filter.crs } : null,
    stops: list(data.stops).map(obj).filter((s) => s && LABEL.test(str(s.label)) && ATCO.test(str(s.atco)))
      .map((s) => ({ label: s.label, atco: s.atco, name: str(s.name), indicator: str(s.indicator), walk: minutes(s.walk) })),
  };
}

// What to show, from begin, checked: the presets, and for each view offered
// that begin says anything of, the one to start at (a preset or KEEP; KEEP
// for anything else, so nothing changes unasked) and what each shows there.
function readShow(data) {
  const show = obj(data) || {};
  const presets = list(show.presets).map(obj)
    .filter((p) => p && PRESET.test(str(p.name)) && p.name !== KEEP)
    .map((p) => ({ name: p.name, label: str(p.label) || p.name }))
    .filter((p, i, all) => all.findIndex((q) => q.name === p.name) === i);
  const names = presets.map((p) => p.name);
  const given = obj(show.views) || {};
  const views = {};
  for (const view of state.views) {
    const one = obj(own(given, view.name));
    if (!one) continue;
    const says = obj(one.says) || {};
    views[view.name] = {
      now: [KEEP, ...names].includes(one.now) ? one.now : KEEP,
      says: Object.fromEntries(names.map((name) => [name, str(own(says, name))])),
    };
  }
  return { presets, views };
}

// The modes of transport, from begin, checked: those setup sets up, and for
// each view offered that begin says anything of, how many it has room for,
// why, and the modes its list names now.
function readModes(data) {
  const modes = obj(data) || {};
  const offered = list(modes.offered).map(obj)
    .filter((m) => m && MODE.test(str(m.name)))
    .map((m) => ({ name: m.name, label: str(m.label) || m.name }))
    .filter((m, i, all) => all.findIndex((n) => n.name === m.name) === i);
  const names = offered.map((m) => m.name);
  const given = obj(modes.views) || {};
  const views = {};
  for (const view of state.views) {
    const one = obj(own(given, view.name));
    if (!one) continue;
    const most = num(one.most);
    views[view.name] = {
      most: most !== null && Number.isInteger(most) && most >= 1 ? most : null,
      says: str(one.says),
      now: Array.isArray(one.now) ? one.now.map(str).filter((name) => names.includes(name)) : null,
    };
  }
  return { offered, views };
}

// Today's choices as where the steps start: the station picked and where
// trains must call; the stops are ticked as each is found (stops.js). A
// shortcut starts with home as roughly saved, and the other part as it is.
function startFromSaved() {
  const { saved } = state;
  if (saved.station) state.stationChoice = saved.station.crs;
  $("filter").value = saved.filter ? saved.filter.crs : "";
  if (!state.only) return;
  if (!saved.station) state.stationChoice = "none";
  state.home = saved.home ? { ...saved.home, label: "roughly where home is", country: "" } : null;
  state.station = saved.station ? { crs: saved.station.crs, name: saved.station.name } : null;
  state.filter = saved.filter;
  state.chosen = saved.stops.map(({ label, atco, name, indicator }) => ({ label, atco, name, indicator }));
}

// Start.

function setUpSteps() {
  for (const button of document.querySelectorAll("[data-back]")) {
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-disabled") === "true") return;
      if (button.dataset.back === LEAVE) window.location.assign(state.back);
      else go(button.dataset.back);
    });
  }
  setUpHome();
  setUpStation();
  setUpStops();
  $("walk-next").addEventListener("click", leaveWalk);
  $("keys-next").addEventListener("click", leaveKeys);
  $("views-next").addEventListener("click", leaveViews);
  $("show-next").addEventListener("click", leaveShow);
  setUpFinish();
}

async function begin() {
  try {
    await signIn();
    const data = await call("begin");
    state.title = str(data.title).slice(0, 60) || state.title;
    if (NOUN.test(str(data.noun))) state.noun = data.noun;
    for (const el of document.querySelectorAll("[data-noun]")) el.textContent = state.noun;
    state.keys = list(data.keys).map(obj).filter((k) => k && /^[A-Z_]{1,64}$/.test(str(k.name)))
      .map((k) => ({ name: k.name, label: str(k.label) || k.name, hint: str(k.hint), set: k.set === true, doc: str(k.doc) }));
    state.links = list(data.links);
    state.back = PATH.test(str(data.back)) ? data.back : "";
    const views = obj(data.views) || {};
    state.views = list(views.offered).map(obj).filter((v) => v && VIEW.test(str(v.name)))
      .map((v) => ({ name: v.name, label: str(v.label) || v.name, hint: str(v.hint) }));
    state.viewsOn = list(views.on).map(str).filter((name) => state.views.some((v) => v.name === name));
    // One view offered: nothing to ask, so it is sent as chosen. None (an
    // older host): no views are sent, and the host keeps those it has.
    state.askViews = state.views.length > 1;
    state.chosenViews = state.views.length === 1 ? [state.views[0].name] : [];
    // What to show: asked where the host offers presets (an older one doesn't).
    state.show = readShow(data.show);
    state.askShow = state.show.presets.length > 0 && Object.keys(state.show.views).length > 0;
    state.modes = readModes(data.modes);
    state.only = shortcut(window.location.search);
    if (state.only) {
      state.title = SHORTCUTS[state.only];
      // A shortcut leaves the views, and what they show, as they are.
      state.askViews = false;
      state.chosenViews = [];
      state.askShow = false;
    }
    fill($("title"), state.title);
    // Without today's choices, the steps start from nothing, as on a first run.
    state.saved = readSaved(await call("saved").catch(() => ({})));
  } catch (error) {
    note("loading", error.message);
    return;
  }
  note("loading", "");
  setUpSteps();
  numberSteps();
  startFromSaved();
  // A shortcut's first step takes focus, as every step does once moved to.
  if (state.only === "station") enterStation();
  else if (state.only === "stops") enterStops();
  else go("home", { focus: false });
}

begin();

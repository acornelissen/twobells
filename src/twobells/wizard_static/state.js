// What the person has told the wizard so far, shared by every step, and
// the words for it that more than one step says.

import { num } from "../dashboard_static/dom.js";

export const state = {
  csrf: "",
  title: "Setup", // the host's, like "Set up your board", from begin
  noun: "board", // what the text calls the host, like "dashboard", from begin
  keys: [], // [{name, label, hint, set, doc}]: the host's own, from begin
  views: [], // [{name, label, hint}]: the views the install offers, from begin
  viewsOn: [], // the names of those on now, from begin
  askViews: false, // whether there is a choice, so a step for it
  chosenViews: [], // names, when leaving the views step (or the one offered)
  viewsDrawn: false,
  // What to show, from begin: the presets, [{name, label}], and for each
  // view offered {now, says}: the preset to start at (or "keep" for what it
  // shows now) and what each preset shows there.
  show: { presets: [], views: {} },
  askShow: false, // whether there is a step for it
  showPicked: {}, // view -> the preset picked (or "keep"), kept while moving between steps
  chosenShow: {}, // view -> the preset to save, when leaving the step; a view kept as it is isn't in it
  // Modes of transport, from begin: those setup sets up, [{name, label}], and
  // for each view offered {most, says, now}: how many it has room for (null:
  // all), why in words, and the modes its list names now (null without one).
  modes: { offered: [], views: {} },
  modesTicked: {}, // view -> the mode names ticked, kept while moving between steps
  chosenModes: {}, // view -> the mode names to save, when leaving the step; a view not asked isn't in it
  links: [],
  only: "", // a shortcut's part, "station" or "stops" (the page's ?step=); "" for the whole wizard
  back: "", // where Back on a shortcut's first step goes, a path like /settings, from begin
  // Today's choices, from saved: {home: {lat, lon} | null, to about a kilometre,
  // station: {crs, name, walk} | null, filter: {crs, name} | null,
  // stops: [{label, atco, name, indicator, walk}], trams: [{label, code, name, walk}]};
  // walk is minutes or null.
  saved: { home: null, station: null, filter: null, stops: [], trams: [] },
  home: null, // {lat, lon, label, country}; exact, so it stays in this page and the server's memory
  stations: [],
  stationsFor: "",
  stationChoice: "", // the radio picked: a code, "other" or "none"
  station: null, // {crs, name, lat?, lon?} or null for no trains
  filter: null, // {crs, name} or null
  stops: [], // found near home
  stopsFor: "",
  labels: new Map(), // atco -> label typed, for stops ticked
  chosen: [], // [{label, atco, name, indicator}], when leaving the stops step
  // Tram stops: those near home, today's and those found by name, [{code,
  // name, kind, distance_m, found}]; whether there is a step for them (not
  // when their lists can't be read); the codes ticked; and [{label, code,
  // name, kind, place}] when leaving the step.
  trams: [],
  tramsFor: "",
  askTrams: true,
  tramsTicked: new Set(),
  chosenTrams: [],
  walk: [], // [{key, what, minutes, source}]
  walkFor: "",
  walkMinutes: {}, // key -> minutes, when leaving the walk step
  keysDrawn: false,
  saving: false,
  step: "",
};

// The modes this run of the wizard sets up: trains with a station, buses with
// stops, trams with tram stops, in the order begin offers them.
export function pickedModes() {
  const picked = { train: Boolean(state.station), bus: state.chosen.length > 0, metro: state.chosenTrams.length > 0 };
  return state.modes.offered.filter((mode) => picked[mode.name]);
}

export const homeKey = () => (state.home ? `${state.home.lat},${state.home.lon}` : "");

export function stopName(stop) {
  return stop.indicator ? `${stop.name}, ${stop.indicator}` : stop.name;
}

export function distance(metres) {
  const m = num(metres);
  if (m === null) return "";
  if (m < 1000) return `${Math.max(10, Math.round(m / 10) * 10)} m`;
  return m < 10_000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m / 1000)} km`; // a stop found far away
}

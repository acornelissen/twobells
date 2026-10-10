// Step 2: the station.

import { $, h, fill, str, num, list, obj } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { L, addHome, fit, isOpen, mapFor, pin, setUpMapToggle } from "./maps.js";
import { note, refuse, say, setError, shout } from "./say.js";
import { distance, homeKey, state } from "./state.js";
import { go, once } from "./steps.js";
import { enterStops } from "./stops.js";

const CRS = /^[A-Za-z]{3}$/;

function checkedStation() {
  const checked = document.querySelector('input[name="station"]:checked');
  return checked ? checked.value : "";
}

function drawStations() {
  // The choice made before, if it still applies; else the nearest station.
  let current = state.stationChoice;
  if (!["other", "none"].includes(current) && !state.stations.some((s) => s.crs === current)) {
    current = state.stations.length ? state.stations[0].crs : "";
  }
  fill($("station-list"), state.stations.map((station, i) => {
    const id = `station-${station.crs}`;
    return h("div", { class: "option" },
      h("input", { type: "radio", name: "station", id, value: station.crs, checked: station.crs === current }),
      h("label", { for: id }, h("span", { class: "num" }, `${i + 1}.`), ` ${station.name} (${station.crs}), `,
        `${distance(station.distance_m)} away`));
  }));
  $("station-other").checked = current === "other";
  $("station-none").checked = current === "none";
  showStationCode();
  if (isOpen("station")) drawStationMap();
}

function showStationCode() {
  $("station-code-box").hidden = checkedStation() !== "other";
}

function drawStationMap() {
  const { layer } = mapFor("station");
  layer.clearLayers();
  addHome(layer);
  const points = state.home ? [[state.home.lat, state.home.lon]] : [];
  state.stations.forEach((station, i) => {
    points.push([station.lat, station.lon]);
    L.marker([station.lat, station.lon], { icon: pin(String(i + 1)), keyboard: false })
      .bindTooltip(h("span", {}, `${i + 1}. ${station.name}`), { direction: "top", offset: [0, -14] })
      .on("click", () => {
        const radio = $(`station-${station.crs}`);
        if (radio) { radio.checked = true; showStationCode(); say(`${station.name} picked.`); }
      })
      .addTo(layer);
  });
  fit("station", points, 14);
}

export async function enterStation() {
  go("station");
  if (state.stationsFor === homeKey()) return;
  state.stations = [];
  fill($("station-list"));
  note("station-status", "Finding the stations near home…");
  try {
    const data = await call("stations", { lat: state.home.lat, lon: state.home.lon });
    state.stations = list(data.stations).map(obj)
      .filter((s) => s && CRS.test(str(s.crs)) && num(s.lat) !== null && num(s.lon) !== null)
      .map((s) => ({ crs: s.crs, name: str(s.name), lat: s.lat, lon: s.lon, distance_m: num(s.distance_m) }));
    state.stationsFor = homeKey();
    note("station-status", state.stations.length ? ""
      : "No stations within 40 km. Type a station's code, or pick no trains.");
    say(state.stations.length ? `${state.stations.length} stations found; the nearest is picked.` : "No stations found nearby.");
  } catch (error) {
    note("station-status", `${error.message} You can type a station's code instead.`);
    shout(error.message);
  }
  drawStations();
}

async function knownStation(field, value) {
  try {
    const data = await call("station", { crs: value });
    const station = obj(data.station);
    return station ? { crs: str(station.crs), name: str(station.name), lat: num(station.lat), lon: num(station.lon) } : null;
  } catch (error) {
    refuse(field, error.message);
    return null;
  }
}

async function leaveStation(button) {
  const choice = checkedStation();
  for (const name of ["station", "station-code", "filter"]) setError(name, "");
  if (!choice) {
    return refuse("station", "Pick a station, or no trains.", document.querySelector('input[name="station"]'));
  }
  const code = $("station-code").value.trim();
  const filter = $("filter").value.trim();
  if (choice === "other" && !CRS.test(code)) return refuse("station-code", "Type a 3-letter code, like GLO.");
  if (filter && !CRS.test(filter)) return refuse("filter", "Type a 3-letter code, like MAN, or leave it empty.");
  if (filter && choice === "none") return refuse("filter", "Pick a station first, or leave this empty.");
  await once(button, async () => {
    let station = null;
    if (choice === "other") {
      station = await knownStation("station-code", code);
      if (!station) return;
    } else if (choice !== "none") {
      station = state.stations.find((s) => s.crs === choice) || null;
    }
    let destination = null;
    if (filter) {
      destination = await knownStation("filter", filter);
      if (!destination) return;
    }
    state.stationChoice = choice;
    state.station = station;
    state.filter = destination;
    enterStops();
  });
}

export function setUpStation() {
  $("station-set").addEventListener("change", showStationCode);
  setUpMapToggle("station", drawStationMap);
  $("station-next").addEventListener("click", (event) => leaveStation(event.currentTarget));
}

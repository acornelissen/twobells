// Wires the page together: the connection, each snapshot's render, the
// one-second tick, the theme toggle, and the row buttons.

import { $, setText, store, str, obj, focusKey, refocus } from "./dom.js";
import { connect, STATE } from "./api.js";
import { clockText, parseAt } from "./time.js";
import { renderBuses, renderTrains, stopNames, tickBoards, toggleMore } from "./board.js";
import { tickStrips } from "./strip.js";
import { renderWeather } from "./weather.js";
import { noteSnapshot } from "./live.js";
import { isOn, tickAlerts } from "./alerts.js";
import { renderHero, setUpHero } from "./hero.js";
import { openSheet, refreshSheet, setUpSheet } from "./sheet.js";
import { setUpMap } from "./map.js";
import { markBusRows, renderDisruptions } from "./disruptions.js";
import { renderArea, setUpArea } from "./area.js";
import { icon } from "./icons.js";
import { pill, placeName } from "./header.js";

let snapshot = null;
let rowsById = new Map(); // id -> { row, mode }
let connection = null;
let connectionState = STATE.connecting;

// What the departure sheet needs beyond a row: the board's station, named in
// a train's journey, the names of your stops, and when the snapshot was made.
const sheetContext = () => ({
  station: str(obj(snapshot?.trains)?.station),
  stops: stopNames(obj(snapshot?.buses)?.stops),
  generatedMs: parseAt(snapshot?.generated_at),
});

// What the hero needs beyond the rows: your stops' names and the rain sentence.
const heroContext = () => ({
  stops: stopNames(obj(snapshot?.buses)?.stops),
  rain: str(obj(snapshot?.weather)?.rain),
});

// --- Theme: "modern" (default) or "board"; theme.js applied it before paint.

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme-toggle").setAttribute("aria-pressed", String(theme === "board"));
}

$("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "board" ? "modern" : "board";
  store.set("dashboard.theme", next);
  applyTheme(next);
});
applyTheme(document.documentElement.dataset.theme === "board" ? "board" : "modern");

// --- The status pill: live or not, and whether every part is current.

function renderConnection() {
  const last = connection ? connection.lastOk() : 0;
  const [state, name, words] = pill(connectionState, snapshot, last, Date.now());
  $("connection").dataset.state = state;
  $("connection-glyph").replaceChildren(icon(name));
  setText($("connection-text"), words);
}

// --- Snapshots.

function indexRows(trains, buses) {
  const map = new Map();
  for (const row of trains) if (str(row.id)) map.set(row.id, { row, mode: "train" });
  for (const row of buses) if (str(row.id)) map.set(row.id, { row, mode: "bus" });
  return map;
}

// Renders each part on its own, so one bad section can't blank the page.
function attempt(what, fn, fallback) {
  try { return fn(); } catch (error) { console.error(`Couldn't render ${what}:`, error); return fallback; }
}

// The train and bus boards, and the disruption links on bus rows. Every row
// is indexed, shown or not, for the sheet, the announcer and the alerts.
function drawBoards() {
  const alerts = { isOn };
  const trains = attempt("trains", () => renderTrains(snapshot, alerts), []);
  const buses = attempt("buses", () => renderBuses(snapshot, alerts), []);
  rowsById = indexRows(trains, buses);
}

// Draws the boards again between snapshots (a row dropped out, a reminder
// set), keeping the focus where it was.
function redrawBoards() {
  const key = focusKey();
  drawBoards();
  attempt("disruption links", () => markBusRows(snapshot, rowsById));
  refocus(key);
}

function render(data) {
  snapshot = data;
  const key = focusKey();
  const title = str(data.title) || "Departures";
  setText($("title"), placeName(title, obj(data.trains)?.station));
  document.title = title;
  drawBoards();
  attempt("weather", () => renderWeather(data));
  attempt("disruptions", () => { renderDisruptions(data); markBusRows(data, rowsById); });
  refocus(key);
  attempt("announcements", () => noteSnapshot(data, rowsById));
  attempt("departure sheet", () => refreshSheet(rowsById, sheetContext()));
  renderArea(data).catch((error) => console.error("Couldn't render the area:", error));
  tick();
}

// --- The one-second tick: clock, countdowns, report ages, alerts.

function tick() {
  setText($("clock"), clockText());
  renderConnection();
  if (!snapshot) return;
  if (tickBoards()) redrawBoards();
  tickStrips(new Map([...rowsById].map(([id, e]) => [id, e.row])), parseAt(snapshot.generated_at));
  attempt("next to catch", () => renderHero(rowsById, heroContext()));
  tickAlerts(rowsById);
}

// --- Row buttons, by delegation so re-rendered rows need no rewiring.

function open(button) {
  const entry = rowsById.get(button.dataset.id);
  if (entry) openSheet(entry, button, sheetContext());
}

$("main").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (button) {
    if (button.dataset.action === "open") open(button);
    else if (button.dataset.action === "more") toggleMore(button);
    return;
  }
  // A click anywhere else on a row opens its sheet too, unless it lands on a
  // link or ends a text selection. Keyboards have the destination button.
  const row = event.target.closest("tr[data-id]");
  if (!row || event.target.closest("a") || String(window.getSelection())) return;
  const opener = row.querySelector('button[data-action="open"]');
  if (opener) open(opener);
});

// A reminder set or cleared, in the sheet or the hero: fire it at once if it
// is due, and show its bell in the row.
const remindChanged = () => {
  tickAlerts(rowsById);
  redrawBoards();
};
setUpSheet(remindChanged);
setUpHero(remindChanged);
setUpMap();
setUpArea();

connection = connect({
  onSnapshot: (data) => render(data),
  onState: (state) => { connectionState = state; renderConnection(); },
});

tick();
setInterval(tick, 1000);

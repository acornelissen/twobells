// Wires the page together: the connection, each snapshot's render, the
// one-second tick, the theme toggle, and the row buttons.

import { $, setText, store, str, obj, focusKey, refocus } from "./dom.js";
import { connect, STATE } from "./api.js";
import { age, clockText, parseAt } from "./time.js";
import { renderBuses, renderTrains, tickBoards } from "./board.js";
import { tickStrips } from "./strip.js";
import { renderWeather } from "./weather.js";
import { noteSnapshot } from "./live.js";
import { isOn, renderLeave, tickAlerts, toggle } from "./alerts.js";
import { openTrain, refreshTrain, setUpTrain } from "./train.js";
import { openHistory, setUpHistory } from "./history.js";
import { openMap, refreshMap, setUpMap } from "./map.js";
import { markBusRows, renderDisruptions } from "./disruptions.js";

let snapshot = null;
let rowsById = new Map(); // id -> { row, mode }
let connection = null;
let connectionState = STATE.connecting;

// The board's station, named in the train dialog's journey.
const station = () => str(obj(snapshot?.trains)?.station);

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

// --- Connection state, as a glyph plus words.

const CONNECTION = {
  [STATE.connecting]: ["◌", "Connecting…"],
  [STATE.live]: ["●", "Live"],
  [STATE.reconnecting]: ["◌", "Reconnecting…"],
  [STATE.polling]: ["↻", "Updating every 30 s"],
  [STATE.offline]: ["✕", "Offline"],
};

function renderConnection() {
  const [glyph, words] = CONNECTION[connectionState] || CONNECTION[STATE.offline];
  const last = connection ? connection.lastOk() : 0;
  const since = connectionState !== STATE.live && last ? `, last update ${age((Date.now() - last) / 1000)}` : "";
  const el = $("connection");
  el.dataset.state = connectionState;
  setText($("connection-glyph"), glyph);
  setText($("connection-text"), words + since);
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

function render(data) {
  snapshot = data;
  const key = focusKey();
  const title = str(data.title) || "Departures";
  setText($("title"), title);
  document.title = title;
  const alerts = { isOn };
  const trains = attempt("trains", () => renderTrains(data, alerts), []);
  const buses = attempt("buses", () => renderBuses(data, alerts), []);
  attempt("weather", () => renderWeather(data));
  rowsById = indexRows(trains, buses);
  attempt("disruptions", () => { renderDisruptions(data); markBusRows(data, rowsById); });
  refocus(key);
  attempt("announcements", () => noteSnapshot(data, rowsById));
  attempt("train dialog", () => refreshTrain(rowsById, station()));
  refreshMap(rowsById);
  tick();
}

// --- The one-second tick: clock, countdowns, report ages, alerts.

function tick() {
  setText($("clock"), clockText());
  renderConnection();
  if (!snapshot) return;
  tickBoards();
  tickStrips(new Map([...rowsById].map(([id, e]) => [id, e.row])), parseAt(snapshot.generated_at));
  renderLeave(rowsById);
  tickAlerts(rowsById);
}

// --- Row buttons, by delegation so re-rendered rows need no rewiring.

$("main").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.id;
  const entry = rowsById.get(id);
  switch (button.dataset.action) {
    case "details":
      if (entry) openTrain(entry.row, button, station());
      break;
    case "history":
      openHistory(id, button);
      break;
    case "map":
      openMap(id, button, entry ? entry.row : null);
      break;
    case "alert":
      if (entry) {
        const on = toggle(id, entry.row);
        button.setAttribute("aria-pressed", String(on));
        setText(button.querySelector(".glyph"), on ? "✓" : "+");
        tickAlerts(rowsById);
      }
      break;
  }
});

setUpTrain();
setUpHistory();
setUpMap();

connection = connect({
  onSnapshot: (data) => render(data),
  onState: (state) => { connectionState = state; renderConnection(); },
});

tick();
setInterval(tick, 1000);

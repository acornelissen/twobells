// The train and bus tables. Each is a real <table> with a caption and scoped
// headers; on narrow screens CSS lays the rows out as cards, and the explicit
// ARIA roles keep the table semantics that some browsers drop when a table's
// display changes.

import { h, vh, setText, str, num, list, obj, validId } from "./dom.js";
import { hhmm, now, parseAt, relative } from "./time.js";
import { renderStrip, renderTrainStrip } from "./strip.js";
import { arrivesLine, busStatusExtras } from "./extras.js";

const MAX_ROWS = 10;

const GLYPH = { on_time: "✓", late: "!", cancelled: "✕", unknown: "◷" };
const WORDS = { on_time: "On time", late: "Late", cancelled: "Cancelled", unknown: "No report" };

export const statusKey = (status) => (GLYPH[status] ? status : "unknown");

// Status as a glyph plus words; the colour comes from the class and only
// reinforces the words.
export function statusBadge(status, text) {
  const key = statusKey(status);
  return h("span", { class: ["badge", `st-${key}`] },
    h("span", { class: "glyph", "aria-hidden": "true" }, GLYPH[key]),
    h("span", { class: "badge-text" }, str(text) || WORDS[key]));
}

// A label shown above a cell's value only in the narrow card layout, where
// the column headers are hidden. Screen readers get the real headers instead.
const cellLabel = (text) => h("span", { class: "cell-label", "aria-hidden": "true" }, text);

const td = (cls, label, ...children) => h("td", { role: "cell", class: cls }, label ? cellLabel(label) : null, ...children);

function headRow(columns) {
  return h("thead", { role: "rowgroup" },
    h("tr", { role: "row" }, columns.map(([text, cls, hidden]) =>
      h("th", { scope: "col", role: "columnheader", class: cls }, hidden ? vh(text) : text))));
}

function table(caption, columns, rows) {
  return h("table", { role: "table", class: "board" },
    h("caption", {}, caption),
    headRow(columns),
    h("tbody", { role: "rowgroup" }, rows));
}

// When to leave: the time, and a countdown that tick() keeps current.
function leaveCell(row) {
  const at = parseAt(row.leave_at);
  if (at === null) {
    const why = row.status === "cancelled" ? "No need to leave" : "No walk time set";
    return td("c-leave", "Leave", h("span", { "aria-hidden": "true" }, "–"), vh(why));
  }
  return td("c-leave", "Leave",
    h("span", { class: "leave-at" }, hhmm(row.leave_at)), " ",
    h("span", { class: "countdown", dataset: { leaveAt: row.leave_at } }, relative(at - now())));
}

function recordCell(row, id, context) {
  const phrase = str(obj(row.history)?.phrase);
  if (!id) return td("c-record", "Record", phrase || "–");
  return td("c-record", "Record", h("button", {
    type: "button", class: "link", dataset: { action: "history", id, focusKey: `history:${id}` },
  }, phrase || "History", vh(` for ${context}`)));
}

function actionButton(action, id, text, context, extra = {}) {
  return h("button", {
    type: "button", class: ["act", extra.class], dataset: { action, id, focusKey: `${action}:${id}` },
    "aria-pressed": extra.pressed === undefined ? null : String(extra.pressed),
  }, extra.glyph ? h("span", { class: "glyph", "aria-hidden": "true" }, extra.glyph) : null, text, vh(` for ${context}`));
}

function alertButton(row, id, context, alerts) {
  // An alert needs a time to leave; a cancelled train has none.
  if (!id || parseAt(row.leave_at) === null) return null;
  const on = alerts.isOn(id);
  return actionButton("alert", id, "Alert me", context, { class: "toggle", pressed: on, glyph: on ? "✓" : "+" });
}

function rowAttrs(row, id) {
  const key = statusKey(row.status);
  return { role: "row", class: ["row", `row-${key}`], dataset: id ? { id } : {} };
}

function reasons(row) {
  const out = [];
  for (const reason of [str(row.cancel_reason), str(row.delay_reason)]) {
    if (reason) out.push(h("span", { class: "reason" }, reason));
  }
  return out;
}

function trainRow(row, alerts) {
  const id = validId(row.id) ? row.id : null;
  const scheduled = str(row.scheduled);
  const destination = str(row.destination);
  const context = `the ${scheduled} to ${destination}`;
  const platform = str(row.platform);
  return h("tr", rowAttrs(row, id),
    h("th", { scope: "row", role: "rowheader", class: "c-time" }, cellLabel("Due"), h("span", { class: "time" }, scheduled)),
    td("c-dest", "To", h("span", { class: "dest" }, destination),
      str(row.origin) ? h("span", { class: "sub" }, `from ${str(row.origin)}`) : null,
      arrivesLine(row)),
    td("c-plat", "Platform", platform || h("span", { class: "muted" }, "Not yet")),
    td("c-status", "Status", statusBadge(row.status, row.status_text), reasons(row)),
    td("c-where", "Where", renderTrainStrip(row)),
    leaveCell(row),
    recordCell(row, id, context),
    td("c-actions", null,
      id ? actionButton("details", id, "Details", context) : null,
      alertButton(row, id, context, alerts)),
  );
}

function busRow(row, alerts, stops, generatedMs) {
  const id = validId(row.id) ? row.id : null;
  const scheduled = str(row.scheduled);
  const line = str(row.line);
  const destination = str(row.destination);
  const stop = str(row.stop);
  const context = `the ${scheduled} ${line} to ${destination}`;
  return h("tr", rowAttrs(row, id),
    h("th", { scope: "row", role: "rowheader", class: "c-time" }, cellLabel("Due"), h("span", { class: "time" }, scheduled)),
    td("c-dest", "Bus", h("span", { class: "line" }, line), " ", h("span", { class: "dest" }, destination),
      arrivesLine(row)),
    td("c-plat", "Stop", stop ? `Stop ${stop}` : "–", stops[stop] ? vh(`, ${stops[stop]}`) : null),
    td("c-status", "Status", statusBadge(row.status, row.status_text), reasons(row), busStatusExtras(row)),
    td("c-where", "Where", renderStrip(row, generatedMs)),
    leaveCell(row),
    recordCell(row, id, context),
    td("c-actions", null,
      id ? actionButton("map", id, "Map", context) : null,
      alertButton(row, id, context, alerts)),
  );
}

// The "Updated 07:01" / "Not updated since…" line under a section heading.
function statusLine(el, status, extra = []) {
  const st = obj(status) || {};
  const parts = [];
  let problem = false;
  const last = hhmm(st.last_success);
  if (str(st.error)) {
    problem = true;
    parts.push(`Problem: ${str(st.error)}.`, last ? `Showing data from ${last}.` : "No data yet.");
  } else if (st.stale) {
    problem = true;
    parts.push(last ? `Not updated since ${last}.` : "Waiting for the first update.");
  } else if (last) {
    parts.push(`Updated ${last}.`);
  }
  if (str(st.note)) parts.push(str(st.note));
  parts.push(...extra);
  el.replaceChildren(problem ? h("span", { class: "glyph", "aria-hidden": "true" }, "⚠︎ ") : "", parts.join(" "));
  el.classList.toggle("problem", problem);
}

export function renderTrains(snapshot, alerts) {
  const section = document.getElementById("trains");
  const trains = obj(snapshot.trains);
  section.hidden = !trains;
  if (!trains) return [];
  const rows = list(trains.rows).filter(obj).slice(0, MAX_ROWS);
  statusLine(document.getElementById("trains-status"), trains.status);
  const walk = num(obj(snapshot.walk)?.train);
  const station = str(trains.station);
  setText(document.getElementById("trains-walk"),
    walk !== null ? `${walk} min walk to ${station || "the station"}.` : "");
  const caption = `Next trains from ${station || "the station"}${str(trains.filter) ? ` calling at ${str(trains.filter)}` : ""}`;
  const board = document.getElementById("trains-board");
  if (!rows.length) {
    board.replaceChildren(h("p", { class: "empty" }, "No trains are listed right now."));
  } else {
    board.replaceChildren(table(caption, [
      ["Due", "c-time"], ["To", "c-dest"], ["Platform", "c-plat"], ["Status", "c-status"], ["Where", "c-where"],
      ["Leave", "c-leave"], ["Record", "c-record"], ["Actions", "c-actions", true],
    ], rows.map((row) => trainRow(row, alerts))));
  }
  renderMessages(list(trains.messages).map(str).filter(Boolean));
  return rows;
}

export function renderBuses(snapshot, alerts) {
  const section = document.getElementById("buses");
  const buses = obj(snapshot.buses);
  section.hidden = !buses;
  if (!buses) return [];
  const rows = list(buses.rows).filter(obj).slice(0, MAX_ROWS);
  const tt = obj(buses.timetable) || {};
  const extra = tt.state === "building" ? ["Building the bus timetable…"]
    : tt.state === "missing" ? ["There is no bus timetable yet."] : [];
  statusLine(document.getElementById("buses-status"), buses.status, extra);
  const stops = obj(buses.stops) || {};
  const walks = obj(obj(snapshot.walk)?.bus) || {};
  const walkText = Object.keys(stops).sort()
    .filter((k) => num(walks[k]) !== null)
    // Stops often share a street name ("Henry Street"), so the label leads.
    .map((k) => `${num(walks[k])} min to Stop ${k}${str(stops[k]) ? ` (${str(stops[k])})` : ""}`);
  setText(document.getElementById("buses-walk"), walkText.length ? `Walk: ${walkText.join(", ")}.` : "");
  const generatedMs = parseAt(snapshot.generated_at);
  const board = document.getElementById("buses-board");
  if (!rows.length) {
    board.replaceChildren(h("p", { class: "empty" },
      tt.state === "building" ? "Bus times will show once the timetable is built." : "No buses are listed right now."));
  } else {
    board.replaceChildren(table("Next buses", [
      ["Due", "c-time"], ["Bus", "c-dest"], ["Stop", "c-plat"], ["Status", "c-status"], ["Where", "c-where"],
      ["Leave", "c-leave"], ["Record", "c-record"], ["Actions", "c-actions", true],
    ], rows.map((row) => busRow(row, alerts, stops, generatedMs))));
  }
  return rows;
}

function renderMessages(messages) {
  const section = document.getElementById("messages");
  section.hidden = messages.length === 0;
  document.getElementById("message-list").replaceChildren(...messages.map((m) => h("li", {}, m)));
}

// Keeps the countdowns current; runs every second, touches only changed text.
export function tickBoards() {
  const t = now();
  for (const el of document.querySelectorAll(".countdown[data-leave-at]")) {
    const at = parseAt(el.dataset.leaveAt);
    if (at === null) continue;
    setText(el, relative(at - t));
    el.closest("tr")?.classList.toggle("missed", at - t < -30_000);
  }
}

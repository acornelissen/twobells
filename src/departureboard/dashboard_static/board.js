// The train and bus boards, laid out like the signs: trains like a station's
// departure screen (Time, Destination, Plat, Expected, Leave), buses like a
// stop's countdown sign (Bus, Stop, Due, Leave). Each is a real <table> with
// a caption and scoped headers; on narrow screens CSS folds each row into two
// lines, and the explicit ARIA roles keep the table semantics that some
// browsers drop when a table's display changes.
//
// A row has one control, its destination: a button that opens the departure
// sheet (sheet.js). A row drops out once its leaving time is more than 2
// minutes gone, leaving one dimmed "Just missed" line until it departs. The
// first 5 rows show; the rest wait behind "Show later departures".

import { h, vh, setText, str, num, list, obj, validId } from "./dom.js";
import { hhmm, now, parseAt, relative } from "./time.js";
import { arrivesLine, busStatusExtras } from "./extras.js";
import { icon } from "./icons.js";

export const SHOWN = 5; // rows before "Show later departures"
const MAX_ROWS = 20; // the most the settings allow
const GO_MS = 2 * 60_000; // "Go now" lasts this long past the leaving time

// On time is plain words; only the exceptions get an icon.
const ICON = { late: "triangle-alert", cancelled: "x", unknown: "clock" };
const WORDS = { on_time: "On time", late: "Late", cancelled: "Cancelled", unknown: "No report" };

export const statusKey = (status) => (WORDS[status] ? status : "unknown");

const lowerFirst = (text) => text.charAt(0).toLowerCase() + text.slice(1);

// Status as words, with an icon for the exceptions; the colour comes from the
// class and only reinforces the words. `spoken` is the words in full
// ("Expected 07:19") where the shown ones are short ("Exp 07:19"): screen
// readers get that instead.
export function statusBadge(status, text, spoken) {
  const key = statusKey(status);
  const shown = str(text) || WORDS[key];
  const said = str(spoken);
  return h("span", { class: ["badge", `st-${key}`] },
    ICON[key] ? h("span", { class: "glyph", "aria-hidden": "true" }, icon(ICON[key])) : null,
    said && said !== shown
      ? [h("span", { class: "badge-text", "aria-hidden": "true" }, shown), vh(said)]
      : h("span", { class: "badge-text" }, shown));
}

// A label shown beside a cell's value only in the narrow layout, where the
// column headers are hidden. Screen readers get the real headers instead.
const cellLabel = (text) => h("span", { class: "cell-label", "aria-hidden": "true" }, text);

const td = (cls, label, ...children) => h("td", { role: "cell", class: cls }, label ? cellLabel(label) : null, ...children);

// [shown, class, said]: `said` is the header in full where `shown` is short.
function headRow(columns) {
  return h("thead", { role: "rowgroup" },
    h("tr", { role: "row" }, columns.map(([text, cls, said]) =>
      h("th", { scope: "col", role: "columnheader", class: cls },
        said ? [h("span", { "aria-hidden": "true" }, text), vh(said)] : text))));
}

// --- Which rows show ---

// The rows to show at time `t` (ms): all but those whose leaving time is more
// than 2 minutes gone. Cancelled rows, and rows with no walk time, stay. Of
// the rows dropped, the latest that hasn't left yet is `missed`, for the
// "Just missed" line.
export function arrange(rows, t) {
  const shown = [];
  let missed = null;
  for (const row of rows) {
    const leave = parseAt(row.leave_at);
    if (leave === null || t <= leave + GO_MS) {
      shown.push(row);
      continue;
    }
    const leaves = parseAt(row.expected_at) ?? parseAt(row.scheduled_at);
    if (leaves !== null && leaves > t && (!missed || leave > missed.leave)) missed = { row, leave };
  }
  return { shown, missed: missed ? missed.row : null };
}

// What a board looks like at `t`, as a string: when it changes, the board is
// drawn again.
function arrangement(rows, t) {
  const { shown, missed } = arrange(rows, t);
  return `${shown.map((row) => str(row.id)).join(" ")}|${missed ? str(missed.id) : ""}`;
}

// Per board: the rows last drawn, how they were arranged, and whether the
// later departures are open.
const boards = {
  train: { rows: [], drawn: "", more: false, table: "trains-table" },
  bus: { rows: [], drawn: "", more: false, table: "buses-table" },
};

// --- Cells shared by both boards ---

// "in 26 min", then "Go now" from the last minute before the leaving time to
// 2 minutes after it (the row has gone by then).
export const leaveText = (ms) => (ms >= 60_000 ? relative(ms) : "Go now");

// When to leave: a countdown that tickBoards() keeps current, the clock time
// under it, and a bell when a reminder is set.
function leaveCell(row, id, alerts) {
  const at = parseAt(row.leave_at);
  if (at === null) {
    const why = row.status === "cancelled" ? "No need to leave" : "No walk time set";
    return td("c-leave", null, h("span", { "aria-hidden": "true" }, "–"), vh(why));
  }
  const ms = at - now();
  return td("c-leave", "Leave",
    h("span", { class: ["countdown", ms < 60_000 ? "go" : null], dataset: { leaveAt: row.leave_at } }, leaveText(ms)),
    id && alerts.isOn(id)
      ? h("span", { class: "reminder" }, h("span", { class: "glyph", "aria-hidden": "true" }, icon("bell-ring")), vh(", reminder set"))
      : null,
    " ", h("span", { class: "leave-at" }, vh("at "), hhmm(row.leave_at)));
}

// The row's one control: its destination, as a button that opens the
// departure sheet. Its name says which departure, ending with the words it
// shows: `lead` "the 07:12 to " makes "More about the 07:12 to Manchester
// Piccadilly".
function openButton(id, lead, destination) {
  const dest = h("span", { class: "dest" }, destination);
  if (!id) return dest;
  return h("button", {
    type: "button", class: "open", dataset: { action: "open", id, focusKey: `open:${id}` },
  }, vh(`More about ${lead}`), dest);
}

// "Often late: 4 of 10" from the punctuality phrase ("Late 4 of the last 10
// weekdays (avg 6 min)", dashboard/history.py), only when at least 3 days and
// a third of them went wrong. Otherwise "", as most rows have nothing to say.
export function recordNote(phrase) {
  const m = /^(?:Cancelled (\d+)(?: and late (\d+))?|Late (\d+)) of the last (\d+) /.exec(str(phrase));
  if (!m) return "";
  const cancelled = Number(m[1] || 0);
  const late = Number(m[2] || m[3] || 0);
  const days = Number(m[4]);
  const bad = cancelled + late;
  if (bad < 3 || bad * 3 < days) return "";
  const what = cancelled && late ? "late or cancelled" : cancelled ? "cancelled" : "late";
  return `Often ${what}: ${bad} of ${days}`;
}

// The short note in the row; screen readers get the whole phrase.
function recordChip(row) {
  const phrase = str(obj(row.history)?.phrase);
  const note = recordNote(phrase);
  if (!note) return null;
  return h("span", { class: "record" }, h("span", { "aria-hidden": "true" }, note), vh(`${phrase}.`));
}

function rowAttrs(row, id, later) {
  return {
    role: "row", class: ["row", `row-${statusKey(row.status)}`, later ? "later" : null], dataset: id ? { id } : {},
    hidden: later && !boards[later].more,
  };
}

// Notes the rows a board is drawn with, so tickBoards() knows when they change.
function remember(mode, rows, t) {
  boards[mode].rows = rows;
  boards[mode].drawn = arrangement(rows, t);
}

// The board itself: the "Just missed" line, the table, and the button for the
// later departures.
function drawBoard(mode, el, { caption, columns, cls, empty }, rows, drawRow) {
  const board = boards[mode];
  const t = now();
  const { shown, missed } = arrange(rows, t);
  remember(mode, rows, t);
  const missedLine = missed ? h("p", { class: "just-missed" }, justMissed(missed, mode)) : null;
  if (!shown.length) {
    el.replaceChildren(...[missedLine, h("p", { class: "empty" }, empty)].filter(Boolean));
    return;
  }
  const later = shown.length - SHOWN;
  el.replaceChildren(...[
    missedLine,
    h("table", { role: "table", class: ["board", cls], id: board.table },
      h("caption", {}, caption),
      headRow(columns),
      h("tbody", { role: "rowgroup" }, shown.map((row, i) => drawRow(row, i, i >= SHOWN ? mode : null)))),
    later > 0 ? h("button", {
      type: "button", class: "more", "aria-expanded": String(board.more), "aria-controls": board.table,
      dataset: { action: "more", mode, focusKey: `more:${mode}` },
    }, h("span", { class: "glyph", "aria-hidden": "true" }, icon("chevron-down")), "Show later departures") : null,
  ].filter(Boolean));
}

// "Just missed 07:40 to Manchester Piccadilly"; a bus says its line too.
function justMissed(row, mode) {
  const line = mode === "bus" && str(row.line) ? ` ${str(row.line)}` : "";
  return `Just missed ${str(row.scheduled)}${line} to ${str(row.destination)}`;
}

// Opens or closes a board's later departures. No redraw: the rows are there.
export function toggleMore(button) {
  const board = boards[button.dataset.mode];
  if (!board) return;
  board.more = !board.more;
  button.setAttribute("aria-expanded", String(board.more));
  for (const tr of document.querySelectorAll(`#${board.table} tr.later`)) tr.hidden = !board.more;
}

// --- Trains ---

// The first row's stops, as on a station's screen: "Calling at Dinting,
// Hattersley, Manchester Piccadilly". Cancelled stops are left out.
function callingAt(row) {
  const names = list(row.calling_points).filter((p) => obj(p) && p.cancelled !== true).map((p) => str(p.name)).filter(Boolean);
  return names.length ? h("span", { class: "sub calling-at" }, `Calling at ${names.join(", ")}`) : null;
}

function trainRow(row, first, later, alerts) {
  const id = validId(row.id) ? row.id : null;
  const scheduled = str(row.scheduled);
  const destination = str(row.destination);
  const platform = str(row.platform);
  const cancelled = row.status === "cancelled";
  const where = str(obj(row.position)?.where);
  return h("tr", rowAttrs(row, id, later),
    h("th", { scope: "row", role: "rowheader", class: "c-time" }, h("span", { class: "time" }, scheduled)),
    td("c-dest", null,
      openButton(id, `the ${scheduled} to `, destination),
      arrivesLine(row),
      first && !cancelled ? callingAt(row) : null,
      first && where ? h("span", { class: "sub where" }, where) : null,
      cancelled && str(row.cancel_reason) ? h("span", { class: "reason" }, str(row.cancel_reason)) : null,
      recordChip(row)),
    td("c-plat", "Plat", platform || [h("span", { "aria-hidden": "true" }, "–"), vh("not yet known")]),
    td("c-status", null, statusBadge(row.status, row.status_text, row.status_spoken)),
    leaveCell(row, id, alerts),
  );
}

// A section's own line under its heading, only when it has something to say:
// a problem ("Not updated since 07:01"), the feed's note, or `extra`. When
// all is well the header's status pill speaks for every section.
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
  }
  if (str(st.note)) parts.push(str(st.note));
  parts.push(...extra);
  el.replaceChildren(problem ? h("span", { class: "glyph", "aria-hidden": "true" }, icon("triangle-alert")) : "",
    problem ? " " : "", parts.join(" "));
  el.classList.toggle("problem", problem);
  el.hidden = parts.length === 0;
}

// The full name of the station the trains are shown calling at, from their
// calling points ("Manchester Piccadilly" for "MAN"), else the code itself.
function filterName(crs, rows) {
  for (const row of rows) {
    const point = list(row.calling_points).find((p) => obj(p) && str(p.crs) === crs && str(p.name));
    if (point) return str(point.name);
  }
  return crs;
}

// Draws the trains; returns every row, shown or not, for the rest of the page.
export function renderTrains(snapshot, alerts) {
  const section = document.getElementById("trains");
  const trains = obj(snapshot.trains);
  section.hidden = !trains;
  if (!trains) {
    remember("train", [], now());
    return [];
  }
  const rows = list(trains.rows).filter(obj).slice(0, MAX_ROWS);
  statusLine(document.getElementById("trains-status"), trains.status);
  const walk = num(obj(snapshot.walk)?.train);
  const station = str(trains.station);
  const walkEl = document.getElementById("trains-walk");
  setText(walkEl, walk !== null ? `${walk} min walk to ${station || "the station"}.` : "");
  walkEl.hidden = walk === null;
  const filter = str(trains.filter);
  const caption = `Next trains from ${station || "the station"}${filter ? ` to ${filterName(filter, rows)}` : ""}`;
  drawBoard("train", document.getElementById("trains-board"), {
    caption, cls: "trains", empty: "No trains are listed right now.",
    columns: [["Time", "c-time"], ["Destination", "c-dest"], ["Plat", "c-plat", "Platform"], ["Expected", "c-status"],
      ["Leave", "c-leave"]],
  }, rows, (row, i, later) => trainRow(row, i === 0, later, alerts));
  renderMessages(trains);
  return rows;
}

// --- Buses ---

// What each of your stops is called on the page, by board label: its name and
// the letter on its sign ("Henry Street A"). Stops that share a name and have
// no letter are told apart by the stop buses go on to, else by the label.
export function stopNames(stops) {
  const byName = new Map();
  for (const label of Object.keys(obj(stops) || {}).sort()) {
    const stop = obj(stops[label]);
    if (!stop) continue;
    const name = str(stop.name) || `Stop ${label}`;
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push({ label, letter: str(stop.letter), towards: str(stop.towards) });
  }
  const names = {};
  for (const [name, group] of byName) {
    const shared = group.length > 1;
    const marks = group.map((s) => s.letter || (shared && s.towards ? `towards ${s.towards}` : ""));
    group.forEach((s, i) => {
      const mark = shared && (!marks[i] || marks.indexOf(marks[i]) !== marks.lastIndexOf(marks[i])) ? `(${s.label})` : marks[i];
      names[s.label] = mark ? `${name} ${mark}` : name;
    });
  }
  return names;
}

// A stop's name for display, its short sign letter or number kept on the
// line with the word before it, so "Henry Street A" never wraps to leave the
// "A" alone. The text is unchanged; only where it may wrap.
export function stopLabel(text) {
  const match = /^(.*?)(\S+ \S{1,3})$/.exec(text);
  return match ? [match[1], h("span", { class: "nobreak" }, match[2])] : text;
}

// The stop's name for a row: from `names` (stopNames), else the row's own.
export function stopText(row, names) {
  const stop = str(row.stop);
  return str(names[stop]) || str(row.stop_name) || (stop ? `Stop ${stop}` : "");
}

// "4 min" on a live countdown, "Due" in the last minute.
export function dueText(ms) {
  const mins = Math.trunc(ms / 60_000);
  return mins < 1 ? "Due" : `${mins} min`;
}

// When the bus comes. A live bus counts down in minutes, with a quiet live
// mark; a bus known only from the timetable shows its clock time and
// "sched.", never minutes that would look live. Cancelled is reversed out,
// with the time struck through.
function dueCell(row) {
  const scheduled = str(row.scheduled);
  if (row.status === "cancelled") {
    return td("c-due", null, h("span", { class: "time" }, scheduled), " ",
      statusBadge(row.status, row.status_text, row.status_spoken));
  }
  const iso = str(row.expected_at) || str(row.scheduled_at);
  const at = parseAt(iso);
  if (row.live === true && at !== null) {
    return td("c-due", null,
      h("span", { class: "due", dataset: { dueAt: iso } }, dueText(at - now())),
      h("span", { class: "live-mark", "aria-hidden": "true" }, icon("circle")),
      vh(`, live, ${lowerFirst(str(row.status_spoken) || str(row.status_text) || "on time")}`));
  }
  return td("c-due", null, h("span", { class: "time" }, scheduled),
    h("span", { class: "sched", "aria-hidden": "true" }, "sched."), vh(", timetable only"));
}

function busRow(row, later, names, alerts) {
  const id = validId(row.id) ? row.id : null;
  const scheduled = str(row.scheduled);
  const line = str(row.line);
  const destination = str(row.destination);
  const where = obj(row.vehicle) ? str(obj(row.strip)?.where) : "";
  const walk = num(row.walk_min);
  return h("tr", rowAttrs(row, id, later),
    h("th", { scope: "row", role: "rowheader", class: "c-dest" },
      h("span", { class: "line" }, line), " ",
      openButton(id, `the ${scheduled} ${line} to `, destination),
      where ? h("span", { class: "sub where" }, where) : null,
      busStatusExtras(row),
      recordChip(row)),
    td("c-stop", null, h("span", { class: "stop" }, stopLabel(stopText(row, names))),
      walk !== null ? h("span", { class: "sub walk" }, `${walk} min walk`) : null),
    dueCell(row),
    leaveCell(row, id, alerts),
  );
}

// Draws the buses; returns every row, shown or not, for the rest of the page.
export function renderBuses(snapshot, alerts) {
  const section = document.getElementById("buses");
  const buses = obj(snapshot.buses);
  section.hidden = !buses;
  if (!buses) {
    remember("bus", [], now());
    return [];
  }
  const rows = list(buses.rows).filter(obj).slice(0, MAX_ROWS);
  const tt = obj(buses.timetable) || {};
  const extra = tt.state === "building" ? ["Building the bus timetable…"]
    : tt.state === "missing" ? ["There is no bus timetable yet."] : [];
  statusLine(document.getElementById("buses-status"), buses.status, extra);
  const names = stopNames(buses.stops);
  drawBoard("bus", document.getElementById("buses-board"), {
    caption: "Next buses", cls: "buses",
    empty: tt.state === "building" ? "Bus times will show once the timetable is built." : "No buses are listed right now.",
    columns: [["Bus", "c-dest"], ["Stop", "c-stop"], ["Due", "c-due"], ["Leave", "c-leave"]],
  }, rows, (row, i, later) => busRow(row, later, names, alerts));
  return rows;
}

// The station's messages as { text, kind, severity }: "station" ones are
// about its facilities (ticket office, lifts), "service" ones are disruption.
// A plain string, as an older server sends, is taken as disruption.
export function trainMessages(trains) {
  return list(obj(trains)?.messages).map((m) => (typeof m === "string" ? { text: m, kind: "service", severity: "" }
    : { text: str(obj(m)?.text), kind: obj(m)?.kind === "station" ? "station" : "service", severity: str(obj(m)?.severity) }))
    .filter((m) => m.text);
}

// Station notices are a quiet "Station information" line in the trains
// section, never called disruption. Disruption messages join the disruption
// summary under the hero (disruptions.js).
export function renderMessages(trains) {
  const station = trainMessages(trains).filter((m) => m.kind === "station");
  const info = document.getElementById("station-info");
  info.hidden = station.length === 0;
  info.replaceChildren(...station.map((m) => h("p", {},
    h("span", { class: "glyph", "aria-hidden": "true" }, icon("info")), "Station information: ", m.text)));
}

// Keeps the countdowns current; runs every second and touches only changed
// text. Returns true when a row has dropped out (or a "Just missed" line has
// gone), so the boards need drawing again.
export function tickBoards() {
  const t = now();
  for (const el of document.querySelectorAll(".countdown[data-leave-at]")) {
    const at = parseAt(el.dataset.leaveAt);
    if (at === null) continue;
    setText(el, leaveText(at - t));
    el.classList.toggle("go", at - t < 60_000);
  }
  for (const el of document.querySelectorAll(".due[data-due-at]")) {
    const at = parseAt(el.dataset.dueAt);
    if (at !== null) setText(el, dueText(at - t));
  }
  return Object.values(boards).some((board) => board.drawn !== arrangement(board.rows, t));
}

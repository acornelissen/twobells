// The train and bus boards, laid out like the signs: trains like a station's
// departure screen (Time, Destination, Plat, Expected, Leave), buses like a
// stop's countdown sign (Bus, Destination, Stop, Due, Leave). Each is a real
// <table> with a caption and scoped headers; on narrow screens CSS folds each
// row into two lines, and the explicit ARIA roles keep the table semantics
// that some browsers drop when a table's display changes.
//
// A row has one control, its destination: a button that opens the departure
// sheet (sheet.js). A row drops out once its leaving time is more than 2
// minutes gone, leaving one dimmed "Just missed" line until it departs. The
// first 5 rows show; the rest wait behind "Show later departures". On a wall
// tablet (kiosk.js) there are no controls: 4 rows, the destination as text.
// Rows draw their position strips unless the settings turned them off
// (applyRowStrips marks <html data-row-strips="off">) or the page is on a wall tablet.
// What the settings leave out (show.js) isn't drawn: where each train and bus
// is, the calling-at line, the punctuality note, a reminder's bell, the last buses.
// A row's time, destination and status carry a data-flap key, its board,
// place and field ("train:0:time"), for the Split-flap look (flap.js).

import { h, vh, setText, str, num, list, obj, own, store, validId } from "./dom.js";
import { hhmm, now, parseAt, relative } from "./time.js";
import { arrivesLine, busStatusExtras, platformWord, trainNoun } from "./extras.js";
import { applyFlaps, flapText } from "./flap.js";
import { icon } from "./icons.js";
import { isKiosk } from "./kiosk.js";
import { shows } from "./show.js";
import { rowStrip } from "./strip.js";

export const SHOWN = 5; // rows before "Show later departures"
export const KIOSK_ROWS = 4; // rows on a wall tablet, which has no "Show later departures"
const MAX_ROWS = 20; // the most the settings allow
export const GO_MS = 2 * 60_000; // "Go now" lasts this long past the leaving time (the hero's too)

// On time is plain words; only the exceptions get an icon.
const ICON = { late: "triangle-alert", cancelled: "x", unknown: "clock" };
const WORDS = { on_time: "On time", late: "Late", cancelled: "Cancelled", unknown: "No report" };

export const statusKey = (status) => (own(WORDS, status) ? status : "unknown");

const lowerFirst = (text) => text.charAt(0).toLowerCase() + text.slice(1);

// Status as words, with an icon for the exceptions; the colour comes from the
// class and only reinforces the words. `spoken` is the words in full
// ("Expected 07:19") where the shown ones are short ("Exp 07:19"): screen
// readers get that instead. `flap` is the words' data-flap key on a board.
export function statusBadge(status, text, spoken, flap = null) {
  const key = statusKey(status);
  const shown = str(text) || WORDS[key];
  const said = str(spoken);
  const dataset = flap ? { flap } : {};
  return h("span", { class: ["badge", `st-${key}`] },
    ICON[key] ? h("span", { class: "glyph", "aria-hidden": "true" }, icon(ICON[key])) : null,
    said && said !== shown
      ? [h("span", { class: "badge-text", "aria-hidden": "true", dataset }, shown), vh(said)]
      : h("span", { class: "badge-text", dataset }, shown));
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
    id && alerts.isOn(id) && shows("remind")
      ? h("span", { class: "reminder" }, h("span", { class: "glyph", "aria-hidden": "true" }, icon("bell-ring")), vh(", reminder set"))
      : null,
    " ", h("span", { class: "leave-at" }, vh("at "), hhmm(row.leave_at)));
}

// The row's one control: its destination, as a button that opens the
// departure sheet. Its name says which departure, ending with the words it
// shows: `lead` "the 07:12 to " makes "More about the 07:12 to Manchester
// Piccadilly". On a wall tablet it is plain text. `flap` is its data-flap key.
function openButton(id, lead, destination, flap) {
  const dest = h("span", { class: "dest", dataset: { flap } }, destination);
  if (!id || isKiosk()) return dest;
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
  if (!shows("punctuality")) return null;
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
// later departures. A `quiet` caption is for screen readers only, where the
// section's heading already says it.
function drawBoard(mode, el, { caption, quiet, columns, cls, empty }, rows, drawRow) {
  const board = boards[mode];
  const t = now();
  const { shown, missed } = arrange(rows, t);
  remember(mode, rows, t);
  const missedLine = missed ? h("p", { class: "just-missed" }, justMissed(missed, mode)) : null;
  if (!shown.length) {
    el.replaceChildren(...[missedLine, h("p", { class: "empty" }, empty)].filter(Boolean));
    return;
  }
  const kiosk = isKiosk();
  const drawn = kiosk ? shown.slice(0, KIOSK_ROWS) : shown;
  const later = kiosk ? 0 : shown.length - SHOWN;
  el.replaceChildren(...[
    missedLine,
    h("table", { role: "table", class: ["board", cls], id: board.table },
      h("caption", { class: quiet ? "vh" : null }, caption),
      headRow(columns),
      h("tbody", { role: "rowgroup" }, drawn.map((row, i) => drawRow(row, i, i >= SHOWN ? mode : null, flapKey(mode, i))))),
    later > 0 ? h("button", {
      type: "button", class: "more", "aria-expanded": String(board.more), "aria-controls": board.table,
      dataset: { action: "more", mode, focusKey: `more:${mode}` },
    }, h("span", { class: "glyph", "aria-hidden": "true" }, icon("chevron-down")), "Show later departures") : null,
  ].filter(Boolean));
  applyFlaps(el);
}

// A row's fields' data-flap keys, by its place on its board: in the
// Split-flap look, when the rows move up, each place's letters flip over
// to its new departure's, as a station's flap board does.
const flapKey = (mode, i) => (field) => `${mode}:${i}:${field}`;

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

// Marks the page with whether the settings turned the rows' position strips
// off (data-row-strips="off"). A snapshot from an older server doesn't say:
// they are on. Call it before drawing the boards.
export function applyRowStrips(root, snapshot) {
  if (snapshot?.row_strips === false) root.dataset.rowStrips = "off";
  else delete root.dataset.rowStrips;
}

// Whether rows draw their position strips: not when the settings turned
// them off, and never on a wall tablet, which has no room for them.
const drawsStrips = () => !isKiosk() && document.documentElement?.dataset.rowStrips !== "off";

// Where the train or bus is, in words, with its strip drawn small before
// them where there is one (rowStrip) and rows draw them.
function whereLine(where, kind, strip, at) {
  const drawing = drawsStrips() ? rowStrip(kind, strip, at) : null;
  return h("span", { class: ["sub", "where", drawing ? "has-strip" : null] }, drawing, where);
}

// The first row's "Calling at" line, in the server's words (core/calling.py):
// "Calling at: Dinting, Broadbottom and Manchester Piccadilly. This train is
// formed of 3 coaches." Every look but Standard (Dot matrix, TFT, Split-flap)
// scrolls it along one line, as a station's screen does; the trains section's Pause button
// (#calling-pause, outside the table, so a row keeps its one control) stops
// it. Paused, with reduced motion, and in the Standard look it wraps (app.css).
const CALLING_KEY = "dashboard.calling";
const callingPaused = () => store.get(CALLING_KEY) === "paused";

// Seconds for one pass: a steady pace, whatever the line's length.
export const callingSeconds = (text) => Math.round(8 + 0.2 * text.length);

function callingAt(row) {
  const said = str(row.calling_at);
  if (!said) return null;
  const text = h("span", { class: "calling-text" }, said);
  // Each pass is timed by the wall clock, not from when the row was drawn, so
  // a new snapshot redrawing the board leaves the line where it was.
  const seconds = callingSeconds(said);
  text.style.setProperty("--calling-s", `${seconds}s`);
  text.style.setProperty("--calling-delay", `-${((Date.now() / 1000) % seconds).toFixed(2)}s`);
  return h("span", { class: ["sub", "calling-at", callingPaused() ? "paused" : null] },
    h("span", { class: "calling-window" }, text));
}

// The Pause button: there while the board has a calling-at line, pressed
// while it is paused. CSS shows it only where the line scrolls.
function showCallingPause() {
  const button = document.getElementById("calling-pause");
  if (!button) return;
  button.hidden = !document.querySelector("#trains-board .calling-at");
  button.setAttribute("aria-pressed", String(callingPaused()));
}

// Stops or starts the calling-at line, and remembers it in this browser.
export function toggleCalling(button) {
  const paused = !callingPaused();
  store.set(CALLING_KEY, paused ? "paused" : "running");
  button.setAttribute("aria-pressed", String(paused));
  for (const line of document.querySelectorAll("#trains-board .calling-at")) line.classList.toggle("paused", paused);
}

// The Plat cell: "1", "Bus" for a replacement bus or "Ferry" for a ferry, "–"
// until it's known, or "2 (changed)" on the late colours when it isn't the
// platform the train was given before, or usually has. The words say it; the
// colour backs them up.
function platformCell(row) {
  const platform = str(row.platform);
  const word = platformWord(row);
  let shown = [h("span", { "aria-hidden": "true" }, "–"), vh("not yet known")];
  if (word) shown = word;
  else if (platform && row.platform_changed === true) shown = h("span", { class: "plat-changed" }, `${platform} (changed)`);
  else if (platform) shown = platform;
  return td("c-plat", "Plat", shown);
}

function trainRow(row, first, later, alerts, flap) {
  const id = validId(row.id) ? row.id : null;
  const scheduled = str(row.scheduled);
  const destination = str(row.destination);
  const via = str(row.via);
  const bus = row.replacement_bus === true;
  const noun = trainNoun(row);
  const cancelled = row.status === "cancelled";
  const position = obj(row.position);
  const where = str(position?.where);
  const strip = obj(position?.strip);
  // Row 1 always says where its train is; a later row does when it has a
  // strip to draw.
  const placed = where && shows("where") && (first || (strip && drawsStrips()))
    ? whereLine(where, "train", strip, num(strip?.train_at)) : null;
  return h("tr", rowAttrs(row, id, later),
    h("th", { scope: "row", role: "rowheader", class: "c-time" },
      h("span", { class: "time", dataset: { flap: flap("time") } }, scheduled)),
    td("c-dest", null,
      openButton(id, `the ${scheduled}${noun === "train" ? "" : ` ${noun}`} to `, destination, flap("destination")),
      via ? h("span", { class: "sub via" }, via) : null,
      bus ? h("span", { class: "sub replacement" }, "Replacement bus") : null,
      arrivesLine(row),
      first && !cancelled && shows("calling_at") ? callingAt(row) : null,
      placed,
      cancelled && str(row.cancel_reason) ? h("span", { class: "reason" }, str(row.cancel_reason)) : null,
      recordChip(row)),
    platformCell(row),
    td("c-status", null, statusBadge(row.status, row.status_text, row.status_spoken, flap("status"))),
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
  }, rows, (row, i, later, flap) => trainRow(row, i === 0, later, alerts, flap));
  showCallingPause();
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

// Stop names up to this long stay whole on one line ("Henry Street A");
// longer ones may wrap.
const SHORT_STOP = 18;

// A stop's name for display, its short sign letter or number kept on the
// line with the word before it, so "Henry Street A" never wraps to leave the
// "A" alone. The text is unchanged; only where it may wrap.
export function stopLabel(text) {
  if (text && text.length <= SHORT_STOP) return ["", h("span", { class: "nobreak" }, text)];
  const match = /^(.*?)(\S+ \S{1,3})$/.exec(text);
  return match ? [match[1], h("span", { class: "nobreak" }, match[2])] : text;
}

// A bus row's stop: the name on its own line, then a quiet line with the
// way it faces, when that is what tells it apart, and the walk:
// "Henry Street" / "towards Market Hall · 8 min walk". In the narrow layout
// it all runs on one line, where the first dot shows. Said with commas.
function stopCell(text, walk) {
  const split = /^(.*) (towards .+)$/.exec(text);
  const [name, towards] = split ? [split[1], split[2]] : [text, ""];
  const more = [
    towards ? h("span", { class: "towards" }, towards) : null,
    towards && walk !== null ? [h("span", { class: "more-sep", "aria-hidden": "true" }, " · "), vh(", ")] : null,
    walk !== null ? h("span", { class: "walk" }, `${walk} min walk`) : null,
  ];
  return td("c-stop", null, h("span", { class: "stop" }, stopLabel(name)),
    towards || walk !== null
      ? [h("span", { class: "walk-sep", "aria-hidden": "true" }, " · "), vh(", "), h("span", { class: "sub" }, more)]
      : null);
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
// with the time struck through. The time or the countdown is the row's
// "due" field (`flap`), so one turning into the other flips.
function dueCell(row, flap) {
  const scheduled = str(row.scheduled);
  const due = flap("due");
  if (row.status === "cancelled") {
    return td("c-due", null, h("span", { class: "time", dataset: { flap: due } }, scheduled), " ",
      statusBadge(row.status, row.status_text, row.status_spoken, flap("status")));
  }
  const iso = str(row.expected_at) || str(row.scheduled_at);
  const at = parseAt(iso);
  if (row.live === true && at !== null) {
    return td("c-due", null,
      h("span", { class: "due", dataset: { dueAt: iso, flap: due } }, dueText(at - now())),
      h("span", { class: "live-mark", "aria-hidden": "true" }, icon("circle")),
      vh(`, live, ${lowerFirst(str(row.status_spoken) || str(row.status_text) || "on time")}`));
  }
  return td("c-due", null, h("span", { class: "time", dataset: { flap: due } }, scheduled),
    h("span", { class: "sched", "aria-hidden": "true" }, "sched."), vh(", timetable only"));
}

function busRow(row, later, names, alerts, flap) {
  const id = validId(row.id) ? row.id : null;
  const scheduled = str(row.scheduled);
  const line = str(row.line);
  const destination = str(row.destination);
  const strip = obj(row.strip);
  // Every bus with stops to draw shows them, live or not: a timetable-only
  // one has no bus on its strip and says "No live position".
  const where = str(strip?.where);
  const walk = num(row.walk_min);
  // The row header is said with every cell, so it is the line and where it
  // goes, nothing more: "237 to Ashton-under-Lyne Interchange". The rest is
  // in the destination's own cell.
  return h("tr", rowAttrs(row, id, later),
    h("th", { scope: "row", role: "rowheader", class: "c-line" },
      h("span", { class: "line", dataset: { flap: flap("line") } }, line), vh(` to ${destination}`)),
    td("c-dest", null,
      openButton(id, `the ${scheduled} ${line} to `, destination, flap("destination")),
      where && shows("where") ? whereLine(where, "bus", strip, num(strip?.bus_at)) : null,
      busStatusExtras(row),
      recordChip(row)),
    stopCell(stopText(row, names), walk),
    dueCell(row, flap),
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
    caption: "Next buses", quiet: true, cls: "buses",
    empty: tt.state === "building" ? "Bus times will show once the timetable is built." : "No buses are listed right now.",
    columns: [["Bus", "c-line"], ["Destination", "c-dest"], ["Stop", "c-stop"], ["Due", "c-due"], ["Leave", "c-leave"]],
  }, rows, (row, i, later, flap) => busRow(row, later, names, alerts, flap));
  renderLastBuses(buses);
  return rows;
}

// --- Last buses ---

// Each line and destination's last bus today, from the buses part: those
// that have a line, a destination and a time.
export function lastBuses(buses) {
  return list(obj(buses)?.last_buses).filter(obj)
    .filter((b) => str(b.line) && str(b.destination) && /^\d\d:\d\d$/.test(str(b.scheduled)));
}

// "Last 341 to Hyde Bus Station 23:05", and ", gone" once it has.
export function lastBusText(bus) {
  const gone = num(bus.remaining) === 0 ? ", gone" : "";
  return `Last ${str(bus.line)} to ${str(bus.destination)} ${str(bus.scheduled)}${gone}`;
}

// The last bus for a bus row's line and destination, or null.
export function lastBusFor(row, buses) {
  return buses.find((b) => str(b.line) === str(row.line) && str(b.destination) === str(row.destination)) || null;
}

// Under the buses, the last buses the server says to show now: in the
// evening, or once few of a line's are left.
function renderLastBuses(buses) {
  const el = document.getElementById("last-buses");
  const shown = shows("last_buses") ? lastBuses(buses).filter((b) => b.show === true) : [];
  el.hidden = !shown.length;
  el.replaceChildren(...shown.map((b) => h("li", { class: num(b.remaining) === 0 ? "gone" : null },
    h("span", { class: "glyph", "aria-hidden": "true" }, icon("moon")), " ", lastBusText(b))));
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
    if (at !== null) flapText(el, dueText(at - t));
  }
  return Object.values(boards).some((board) => board.drawn !== arrangement(board.rows, t));
}

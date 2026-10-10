// The train dialog: everything the board knows about one train, with where
// it is now and its journey (the stops before this station, this station,
// and the stops after) as one ordered list. It reads the current snapshot,
// and updates in place when a new one arrives while it is open.

import { $, h, s, vh, patch, setText, str, num, list, obj, openDialog, setUpDialog } from "./dom.js";
import { hhmm } from "./time.js";
import { statusBadge } from "./board.js";
import { arrivesFact } from "./extras.js";

const dialog = () => $("train-dialog");
let openId = null;
let openStation = "";

export function setUpTrain() {
  setUpDialog(dialog(), () => { openId = null; });
}

function fact(term, value) {
  return value ? [h("dt", {}, term), h("dd", {}, value)] : [];
}

// Where the train is along the journey (previous points, this station,
// calling points, numbered from 0): { at } at a stop, { gap, part } on the
// way from stop `gap` to the next (part is 0 to 1), or null when unknown.
// The position strip places it; the last stop with an actual time does when
// the strip can't, or when it shows the train further on (the strip ends one
// stop after this station).
export function placeOnJourney(row, before, after) {
  const count = before.length + 1 + after.length;
  const strip = obj(obj(row.position)?.strip);
  const trainAt = num(strip?.train_at);
  const yours = num(strip?.yours);
  const stops = list(strip?.stops);
  let place = null;
  if (trainAt !== null && yours !== null && stops.length > 1) {
    const offset = before.length - yours; // the strip's first stop, in the journey
    const along = Math.min(Math.max(trainAt, 0), stops.length - 1);
    const i = Math.floor(along);
    const part = along - i;
    if (part < 0.02) place = { at: offset + i };
    else if (part > 0.98) place = { at: offset + i + 1 };
    else place = { gap: offset + i, part };
    const first = place.at ?? place.gap;
    if (first < 0 || first >= count || (place.gap !== undefined && first + 1 >= count)) place = null;
  }
  let last = -1;
  [...before, null, ...after].forEach((p, i) => { if (p && str(p.actual)) last = i; });
  if (last >= 0 && (!place || (place.at ?? place.gap) < last)) {
    place = last === count - 1 ? { at: last } : { gap: last, part: 0.5 };
  }
  return place;
}

// What the train has done at stop i, in words for the list.
function progress(place, i) {
  if (!place) return "";
  if (place.at === i) return "here";
  const heading = place.at ?? place.gap + 1; // the stop it is at or heading for
  if (i < heading) return "passed";
  return i === heading ? "next" : "";
}

const TAGS = { passed: "passed", next: "next stop", here: "train here" };

// A small train, drawn as on the row strips. Drawing only.
const trainGlyph = () => s("svg", { class: "cp-train-glyph", viewBox: "0 0 16 16", "aria-hidden": "true", focusable: "false" },
  s("rect", { x: 3, y: 1, width: 10, height: 11, rx: 3 }),
  s("rect", { class: "cp-train-window", x: 4.75, y: 3, width: 6.5, height: 3.5, rx: 1 }),
  s("path", { class: "cp-train-wheels", d: "M5.5 12 L4 15 M10.5 12 L12 15" }));

// One stop in the journey. The line, dot and train are drawing only
// (aria-hidden or CSS); the time, name and words say it all. At a stop, its
// "train here" tag says where the train is; between two, a hidden sentence
// does, as no tag can.
function stopItem(stop, i, names, place, where) {
  const done = progress(place, i);
  const item = h("li", { class: ["cp", done ? `cp-${done}` : null, stop.yours ? "yours" : null, stop.cancelled ? "cp-cancelled" : null] },
    i < names.length - 1 ? h("span", { class: "cp-rail", "aria-hidden": "true" }) : null,
    h("span", { class: "cp-time" }, stop.time), " ",
    h("span", { class: "cp-name" }, stop.name),
    stop.yours ? h("span", { class: "cp-state" }, " (this station)") : null,
    done ? [" ", h("span", { class: "cp-tag" }, TAGS[done])] : null,
    stop.state ? " " : null, stop.state);
  if (place && (place.at === i || place.gap === i)) {
    item.append(h("span", { class: "cp-train", "aria-hidden": "true" }, trainGlyph()));
  }
  if (place && place.gap === i) {
    item.append(vh(`. Train is between ${stop.name} and ${names[i + 1]}${where ? ` (${where})` : ""}.`));
    // How far along the gap, for the CSS; the CSP allows the CSSOM.
    item.style.setProperty("--part", place.part.toFixed(3));
  }
  return item;
}

function pointStop(p) {
  const scheduled = str(p.scheduled);
  let state = null;
  if (p.cancelled === true) state = statusBadge("cancelled", "Cancelled");
  else if (str(p.actual)) state = h("span", { class: "cp-state" }, `actual ${str(p.actual)}`);
  else if (str(p.expected) && str(p.expected) !== scheduled) state = statusBadge("late", `expected ${str(p.expected)}`);
  else if (str(p.expected)) state = h("span", { class: "cp-state" }, "on time");
  return { name: str(p.name), time: scheduled, state, cancelled: p.cancelled === true };
}

// This station, marked in words, not just by its bigger dot.
function thisStop(row, station) {
  const scheduled = str(row.scheduled);
  const expected = str(row.expected);
  let state = null;
  if (row.status === "cancelled") state = statusBadge("cancelled", "Cancelled");
  else if (expected && expected !== scheduled) state = statusBadge("late", `expected ${expected}`);
  else if (row.status === "on_time") state = h("span", { class: "cp-state" }, "on time");
  return { name: station || "This station", time: scheduled, state, yours: true, cancelled: row.status === "cancelled" };
}

// The journey as a route line: a dot per stop, solid up to the train and
// dashed after it, with the train drawn where it is.
function journey(row, station) {
  const before = list(row.previous_points).filter(obj);
  const after = list(row.calling_points).filter(obj);
  if (!before.length && !after.length) return h("p", {}, "Calling points aren't available for this train.");
  const stops = [...before.map(pointStop), thisStop(row, station), ...after.map(pointStop)];
  const names = stops.map((stop) => stop.name);
  const place = placeOnJourney(row, before, after);
  const where = str(obj(row.position)?.where);
  // role=list: Safari drops list semantics when the numbers are styled away.
  return h("ol", { class: ["calling", "journey"], role: "list" },
    stops.map((stop, i) => stopItem(stop, i, names, place, where)));
}

function render(row) {
  const title = $("train-title");
  const body = $("train-body");
  if (!row) {
    patch(body, h("p", {}, "This train has left the board."));
    return;
  }
  setText(title, `${str(row.scheduled)} to ${str(row.destination)}`);
  const length = num(row.length);
  const expected = str(row.expected);
  const where = str(obj(row.position)?.where);
  // Updated in place, so a screen reader's place in it stays put. The
  // reasons keep their places when empty, so nothing after them moves.
  patch(body,
    h("p", { class: "dialog-status" }, statusBadge(row.status, row.status_text, row.status_spoken)),
    h("p", { class: "reason", hidden: !str(row.cancel_reason) }, str(row.cancel_reason)),
    h("p", { class: "reason", hidden: !str(row.delay_reason) }, str(row.delay_reason)),
    h("dl", { class: "facts" },
      fact("Operator", str(row.service)),
      fact("From", str(row.origin)),
      fact("Scheduled", str(row.scheduled)),
      fact("Expected", expected && expected !== str(row.scheduled) ? expected : ""),
      fact("Platform", str(row.platform) || "Not yet known"),
      fact("Coaches", length ? `${length} ${length === 1 ? "coach" : "coaches"}` : ""),
      fact("Leave by", hhmm(row.leave_at)),
      fact("Arrives", arrivesFact(row)),
    ),
    h("h3", {}, "Where it is now"),
    h("p", { class: where ? null : "muted" }, where || "No live position"),
    h("h3", {}, "Journey"),
    journey(row, openStation),
  );
}

// `station` is the board's station, named in the journey.
export function openTrain(row, opener, station = "") {
  openId = row.id;
  openStation = station;
  render(row);
  openDialog(dialog(), opener);
}

// Called with each new snapshot: updates the open dialog from its row.
export function refreshTrain(rowsById, station = "") {
  if (!openId || !dialog().open) return;
  openStation = station;
  const entry = rowsById.get(openId);
  render(entry ? entry.row : null);
}

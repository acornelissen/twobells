// The train dialog: everything the board knows about one train, with where
// it is now and its journey (the stops before this station, this station,
// and the stops after) as one ordered list. It reads the current snapshot,
// and refreshes when a new one arrives while it is open.

import { $, h, fill, setText, str, num, list, obj, openDialog, setUpDialog } from "./dom.js";
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

function callingPoint(point) {
  const p = obj(point);
  if (!p) return null;
  const name = str(p.name);
  const scheduled = str(p.scheduled);
  let state;
  if (p.cancelled === true) state = statusBadge("cancelled", "Cancelled");
  else if (str(p.actual)) state = h("span", { class: "cp-state" }, `actual ${str(p.actual)}`);
  else if (str(p.expected) && str(p.expected) !== scheduled) state = statusBadge("late", `expected ${str(p.expected)}`);
  else if (str(p.expected)) state = h("span", { class: "cp-state" }, "on time");
  return h("li", { class: ["cp", p.cancelled === true ? "cp-cancelled" : null] },
    h("span", { class: "cp-time" }, scheduled), " ",
    h("span", { class: "cp-name" }, name), state ? " " : null, state);
}

// This station in the journey, marked in words, not just by weight.
function thisStation(row, station) {
  return h("li", { class: ["cp", "yours"] },
    h("span", { class: "cp-time" }, str(row.scheduled)), " ",
    h("span", { class: "cp-name" }, station || "This station"),
    station ? h("span", { class: "cp-state" }, " (this station)") : null);
}

function journey(row, station) {
  const before = list(row.previous_points).map(callingPoint).filter(Boolean);
  const after = list(row.calling_points).map(callingPoint).filter(Boolean);
  if (!before.length && !after.length) return h("p", {}, "Calling points aren't available for this train.");
  return h("ol", { class: "calling" }, before, thisStation(row, station), after);
}

function render(row) {
  const title = $("train-title");
  const body = $("train-body");
  if (!row) {
    body.replaceChildren(h("p", {}, "This train has left the board."));
    return;
  }
  setText(title, `${str(row.scheduled)} to ${str(row.destination)}`);
  const length = num(row.length);
  const expected = str(row.expected);
  const where = str(obj(row.position)?.where);
  fill(body,
    h("p", { class: "dialog-status" }, statusBadge(row.status, row.status_text)),
    str(row.cancel_reason) ? h("p", { class: "reason" }, str(row.cancel_reason)) : null,
    str(row.delay_reason) ? h("p", { class: "reason" }, str(row.delay_reason)) : null,
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

// Called with each new snapshot: redraws the open dialog from its row.
export function refreshTrain(rowsById, station = "") {
  if (!openId || !dialog().open) return;
  openStation = station;
  const entry = rowsById.get(openId);
  render(entry ? entry.row : null);
}

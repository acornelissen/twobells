// The departure sheet: one dialog for any row, opened from its destination.
// It holds the status, reasons and facts; Remind me; the journey for a train,
// or the strip, map and stops for a bus; and the departure's punctuality,
// loaded when it opens. A new snapshot updates it in place (dom.js patch), so
// a screen reader's place in it stays put.

import { $, h, fill, patch, setText, str, num, obj, openDialog, setUpDialog } from "./dom.js";
import { hhmm, parseAt } from "./time.js";
import { lastBusFor, statusBadge, stopText } from "./board.js";
import { trainJourney, trainSummary } from "./train.js";
import { arrivesFact, fact, ghostBadge, inboundText, noShowPhrase } from "./extras.js";
import { renderStrip } from "./strip.js";
import { closeMap, refreshMap, showBus } from "./map.js";
import { closeHistory, loadHistory } from "./history.js";
import { isOn, remindNote, toggle } from "./alerts.js";
import { icon } from "./icons.js";

const dialog = () => $("sheet");
let openId = null;
let openMode = "";
let openRow = null;
// What the rows don't say: { station, stops (stopNames), generatedMs, lastBuses }.
let context = { station: "", stops: {}, generatedMs: null, lastBuses: [] };
let onRemind = () => {};

// `remindChanged` runs when Remind me turns a reminder on or off.
export function setUpSheet(remindChanged) {
  onRemind = remindChanged;
  setUpDialog(dialog(), () => {
    openId = null;
    openRow = null;
    closeMap();
    closeHistory();
  });
  $("sheet-remind").addEventListener("click", () => {
    if (!openId || !openRow) return;
    toggle(openId, openRow, (text) => setText($("remind-note"), text));
    drawRemind(openRow);
    onRemind();
  });
}

function title(row) {
  const scheduled = str(row.scheduled);
  const destination = str(row.destination);
  if (openMode === "bus") return `${scheduled} ${str(row.line)} to ${destination}`;
  return row.replacement_bus === true ? `${scheduled} replacement bus to ${destination}` : `${scheduled} to ${destination}`;
}

// When the last bus of the row's line and destination goes today: "23:05",
// with its stop when it leaves from another of yours, or that this is it.
export function lastBusFact(row, buses, stops) {
  const last = lastBusFor(row, buses);
  if (!last) return "";
  if (str(last.scheduled_at) && str(last.scheduled_at) === str(row.scheduled_at)) return "This is the last today";
  const from = str(last.stop) && str(last.stop) !== str(row.stop) ? ` from ${stopText(last, stops)}` : "";
  return `${str(last.scheduled)}${from}${num(last.remaining) === 0 ? ", gone" : ""}`;
}

// A bus's status, the warning when it may not be running, its facts, and
// where it is now on the strip.
function busSummary(row) {
  const scheduled = str(row.scheduled);
  const expected = str(row.expected);
  const walk = num(row.walk_min);
  const stop = stopText(row, context.stops);
  return [
    h("p", { class: "dialog-status" }, statusBadge(row.status, row.status_text, row.status_spoken)),
    h("p", { class: "dialog-status", hidden: row.ghost !== true }, ghostBadge(row)),
    h("dl", { class: "facts" },
      fact("Stop", walk !== null ? `${stop}, ${walk} min walk` : stop),
      fact("Scheduled", scheduled),
      fact("Expected", expected && expected !== scheduled ? expected : ""),
      fact("Leave by", hhmm(row.leave_at)),
      fact("Arrives", arrivesFact(row)),
      fact("Bus coming in", inboundText(row)),
      fact("Tracker", noShowPhrase(row)),
      fact("Last bus", lastBusFact(row, context.lastBuses || [], context.stops)),
    ),
    h("h3", {}, "Where it is now"),
    renderStrip(row, context.generatedMs),
  ];
}

// Remind me, for a row with a time to leave; a cancelled departure or one
// with no walk time has none.
function drawRemind(row) {
  const button = $("sheet-remind");
  const can = !!row && parseAt(row.leave_at) !== null;
  const on = can && isOn(openId);
  // Hiding the focused button would drop the focus out of the dialog.
  if (!can && document.activeElement === button) $("sheet-title").focus();
  button.hidden = !can;
  button.setAttribute("aria-pressed", String(on));
  fill(button, h("span", { class: "glyph", "aria-hidden": "true" }, icon(on ? "bell-ring" : "bell")), "Remind me");
  let note = "";
  if (can) note = remindNote();
  else if (row && row.status !== "cancelled") note = "Set a walk time in the settings for a reminder.";
  setText($("remind-note"), note);
}

function draw(row) {
  openRow = row;
  if (!row) {
    patch($("sheet-summary"), h("p", {}, `This ${openMode === "bus" ? "bus" : "train"} has left the board.`));
    patch($("sheet-train"));
    drawRemind(null);
    return;
  }
  setText($("sheet-title"), title(row));
  patch($("sheet-summary"), openMode === "bus" ? busSummary(row) : trainSummary(row));
  if (openMode === "train") patch($("sheet-train"), trainJourney(row, context.station));
  drawRemind(row);
}

// Opens the sheet for entry { row, mode }, from the button `opener`.
export function openSheet(entry, opener, ctx) {
  const row = obj(entry?.row);
  if (!row) return;
  openId = str(row.id);
  openMode = entry.mode === "bus" ? "bus" : "train";
  context = ctx;
  const sheet = dialog();
  sheet.dataset.mode = openMode;
  $("sheet-train").hidden = openMode !== "train";
  $("sheet-bus").hidden = openMode !== "bus";
  draw(row);
  openDialog(sheet, opener);
  if (openMode === "bus") showBus(openId);
  loadHistory(openId);
}

// Called with each new snapshot: updates the open sheet from its row.
// rowsById maps id to { row, mode }.
export function refreshSheet(rowsById, ctx) {
  if (!openId || !dialog().open) return;
  context = ctx;
  const entry = rowsById.get(openId);
  draw(entry ? entry.row : null);
  if (openMode === "bus") refreshMap();
}

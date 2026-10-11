// The hero, "Next to catch": the departure to leave for next, a countdown to
// its leaving time, how sure that departure is, what to catch instead, the
// rain sentence, and its own Remind me.
//
// It is drawn every second, so the countdown is deliberately not a live
// region: the announcer (live.js) is told only when the departure it points
// at changes and the change has held for 30 s, so a bus whose tracker comes
// and goes doesn't flip the announcement back and forth.

import { $, h, patch, setText, vh, str, num, obj, validId } from "./dom.js";
import { hhmm, now, parseAt, relative } from "./time.js";
import { describe, isOn, remindNote, toggle } from "./alerts.js";
import { GO_MS, stopText } from "./board.js";
import { announce, unannounce } from "./live.js";
import { icon } from "./icons.js";
import { isKiosk } from "./kiosk.js";

const NOW_MS = 60_000; // "Leave now" from the last minute until the row drops (GO_MS)
const SECONDS_MS = 2 * 60_000; // seconds shown below this
const SOON_MS = 3 * 60_000; // amber from here
const LIVE_GRACE_MS = 5 * 60_000; // a live departure this soon after a ghost bus is taken instead
const HOLD_MS = 30_000; // a new departure is announced once the hero has pointed at it this long

let said; // the id last announced, the first one shown counting: undefined before the first draw
let saidBefore; // the one before it, while the words for `said` may still be waiting to be spoken
let pointing = null; // { id, since }: a departure other than `said` the hero points at, and since when
let shown = null; // { row, mode } on show, for Remind me
let onRemind = () => {};

// "Leave now" in the last minute, "Leave in 1:53" under 2 minutes, then whole
// minutes rounded down ("Leave in 6 min", "Leave in 1 h 4 min"), so it never
// promises more time than there is. Returns [shown, said].
export function leaveWords(ms) {
  if (ms <= NOW_MS) return ["Leave now", "Leave now"];
  if (ms < SECONDS_MS) {
    const total = Math.floor(ms / 1000);
    const min = Math.floor(total / 60), sec = total % 60;
    return [`Leave in ${min}:${String(sec).padStart(2, "0")}`, `Leave in ${min} min${sec ? ` ${sec} s` : ""}`];
  }
  const words = `Leave ${relative(ms)}`;
  return [words, words];
}

// How sure the departure is: "live" (a train on the live board, or a bus with
// a live position), "timetable" (a bus without), "ghost" (a bus due with no
// tracker, which may not be running).
export function confidence(row, mode) {
  if (row.ghost === true) return "ghost";
  return mode === "train" || row.live === true ? "live" : "timetable";
}

const SURE = {
  live: ["circle", "Live"],
  timetable: ["clock", "Timetable only"],
  ghost: ["triangle-alert", "May not be running"],
};

// Every departure you can still make on foot at `t`, soonest leaving first:
// not cancelled, and its leaving time not more than GO_MS gone, as long as
// its row says "Go now". [{ row, mode, leave }].
export function catchable(rowsById, t = now()) {
  const found = [];
  for (const { row, mode } of rowsById.values()) {
    const leave = parseAt(row.leave_at);
    if (leave === null || leave + GO_MS < t || row.status === "cancelled") continue;
    found.push({ row, mode, leave });
  }
  return found.sort((a, b) => a.leave - b.leave);
}

// The one to leave for: the soonest, unless it is a bus that may not be
// running and a live departure leaves within 5 minutes after it.
export function nextCatchable(rowsById, t = now()) {
  const all = catchable(rowsById, t);
  const first = all[0];
  if (!first || confidence(first.row, first.mode) !== "ghost") return first || null;
  return all.find((e) => e.leave <= first.leave + LIVE_GRACE_MS && confidence(e.row, e.mode) === "live") || first;
}

// What to catch instead: the next one after `chosen` in each mode, the same
// mode included, soonest first.
export function thenOptions(rowsById, chosen, t = now()) {
  const after = catchable(rowsById, t).filter((e) => e.row !== chosen.row && e.leave >= chosen.leave);
  return ["train", "bus"].map((mode) => after.find((e) => e.mode === mode)).filter(Boolean)
    .sort((a, b) => a.leave - b.leave);
}

const time = (text) => h("span", { class: "time" }, text);
const badge = (line) => [h("span", { class: "line" }, line), vh(" bus")];
const sep = () => [h("span", { "aria-hidden": "true" }, " · "), vh(", ")];

// "17:55 [237] Ashton-under-Lyne Interchange · Henry Street A · 5 min walk",
// or "18:22 train to Manchester Piccadilly · Platform 1 · 12 min walk".
function whatLine(row, mode, stops) {
  const walk = num(row.walk_min);
  const parts = [];
  if (mode === "bus") {
    parts.push([time(str(row.scheduled)), " ", badge(str(row.line)), vh(" to"), " ", str(row.destination)]);
    const stop = stopText(row, stops);
    if (stop) parts.push(stop);
  } else {
    parts.push([time(str(row.scheduled)), ` train to ${str(row.destination)}`]);
    if (str(row.platform)) parts.push(`Platform ${str(row.platform)}`);
  }
  if (walk !== null) parts.push(`${walk} min walk`);
  return parts.flatMap((part, i) => (i ? [sep(), part] : [part]));
}

// "Then: 18:10 train (leave 18:00) · 18:12 [341] (leave 18:02)".
function thenLine(options) {
  if (!options.length) return [];
  return ["Then: ", options.flatMap(({ row, mode }, i) => [
    i ? sep() : null,
    time(str(row.scheduled)), " ", mode === "bus" ? badge(str(row.line)) : "train",
    " (leave ", time(hhmm(row.leave_at)), ")",
  ])];
}

function drawRemind() {
  const button = $("leave-remind");
  const note = $("notify-note");
  // A wall tablet has no controls: no Remind me.
  const id = shown && validId(shown.row.id) && !isKiosk() ? shown.row.id : "";
  // Hidden while focused, the focus would fall to nowhere: the hero's heading takes it.
  if (!id && !button.hidden && document.activeElement === button) $("leave-heading").focus({ preventScroll: true });
  button.hidden = !id;
  const on = !!id && isOn(id);
  button.setAttribute("aria-pressed", String(on));
  patch(button, h("span", { class: "glyph", "aria-hidden": "true" }, icon(on ? "bell-ring" : "bell")), "Remind me",
    id ? vh(` for ${describe(shown.row, shown.mode, false)}`) : null);
  // The same honest words as the sheet's, shown once the reminder is set.
  setText(note, on ? remindNote() : "");
  note.hidden = !on;
}

// Wires Remind me; `remindChanged` runs when it turns a reminder on or off.
export function setUpHero(remindChanged) {
  onRemind = remindChanged;
  $("leave-remind").addEventListener("click", () => {
    if (!shown || !validId(shown.row.id)) return;
    toggle(shown.row.id, shown.row, drawRemind);
    drawRemind();
    onRemind();
  });
}

// Tells the live region when the hero points at a new departure (`id`, of
// entry `next`) and has done for HOLD_MS. If it goes back to the last one
// before the words are spoken, they are taken back.
function noteTarget(id, next, t) {
  if (said === undefined) said = id;
  if (!id || id === said) {
    pointing = null;
    return;
  }
  if (id === saidBefore && unannounce("hero")) {
    said = saidBefore;
    saidBefore = undefined;
    pointing = null;
    return;
  }
  if (!pointing || pointing.id !== id) pointing = { id, since: t };
  if (t - pointing.since < HOLD_MS) return;
  announce("hero", `Next to catch is now ${describe(next.row, next.mode, false)}.`);
  saidBefore = said;
  said = id;
  pointing = null;
}

// Draws the hero. rowsById maps id to { row, mode }; `context` is
// { stops (board.js stopNames), rain (the weather's rain sentence) }.
export function renderHero(rowsById, context) {
  const t = now();
  const next = nextCatchable(rowsById, t);
  const banner = $("leave");
  const id = next ? str(next.row.id) || null : null;
  noteTarget(id, next, t);
  shown = next;
  const rain = str(obj(context)?.rain);
  setText($("leave-rain"), rain);
  $("leave-rain").hidden = !rain;
  for (const part of ["leave-sure", "leave-then"]) $(part).hidden = !next;
  drawRemind();
  if (!next) {
    banner.classList.remove("soon", "now");
    setText($("leave-time"), "Nothing to leave for");
    setText($("leave-what"), "No departure with a walk time is coming up.");
    return;
  }
  const ms = next.leave - t;
  banner.classList.toggle("now", ms <= NOW_MS);
  banner.classList.toggle("soon", ms > NOW_MS && ms <= SOON_MS);
  const [words, said] = leaveWords(ms);
  patch($("leave-time"), said === words ? words : [h("span", { "aria-hidden": "true" }, words), vh(said)]);
  patch($("leave-what"), whatLine(next.row, next.mode, obj(context?.stops) || {}));
  const sure = confidence(next.row, next.mode);
  const [name, text] = SURE[sure];
  patch($("leave-sure"), h("span", { class: ["sure", `sure-${sure}`] },
    h("span", { class: "glyph", "aria-hidden": "true" }, icon(name)), text));
  const then = thenLine(thenOptions(rowsById, next, t));
  patch($("leave-then"), then);
  $("leave-then").hidden = !then.length;
}

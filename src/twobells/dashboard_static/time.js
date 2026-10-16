// The server's clock and the wording of times.
//
// Countdowns run against the server clock, not the device's, so a phone with
// a wrong clock still says "Leave in 4 min" correctly. Each observation says
// "the server's clock read at least T when this arrived"; a snapshot may be a
// little old when it lands, never from the future, so the largest recent
// observation is the best estimate.

const WINDOW_MS = 15 * 60 * 1000;
let observations = []; // [localMs, offsetMs]
let offsetMs = 0;
let boardOffsetMin = null; // the board's UTC offset, from generated_at

export function observe(serverMs) {
  if (!Number.isFinite(serverMs)) return;
  const local = Date.now();
  observations.push([local, serverMs - local]);
  observations = observations.filter(([t]) => local - t <= WINDOW_MS).slice(-50);
  offsetMs = Math.max(...observations.map(([, o]) => o));
}

// From a snapshot's generated_at, which also carries the board's UTC offset.
export function observeSnapshot(generatedAt) {
  const ms = parseAt(generatedAt);
  if (ms === null) return;
  observe(ms);
  const m = /([+-])(\d\d):(\d\d)$/.exec(generatedAt);
  if (m) boardOffsetMin = (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3]));
  else if (generatedAt.endsWith("Z")) boardOffsetMin = 0;
}

// From an HTTP Date header (whole seconds, so it can only read low).
export function observeHeader(dateHeader) {
  const ms = Date.parse(dateHeader || "");
  if (Number.isFinite(ms)) observe(ms);
}

export const now = () => Date.now() + offsetMs;

const ISO = /^\d{4}-\d\d-\d\dT(\d\d):(\d\d)(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)$/;

// Milliseconds since the epoch for an ISO time with an offset, or null.
export function parseAt(iso) {
  if (typeof iso !== "string" || !ISO.test(iso)) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

// "07:12" from an ISO time, in the board's own offset as the server wrote it.
export function hhmm(iso) {
  const m = typeof iso === "string" ? ISO.exec(iso) : null;
  return m ? `${m[1]}:${m[2]}` : "";
}

const DAY = /^(\d{4})-(\d\d)-(\d\d)$/;
const dayFormat = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

// "Fri 9 Oct" from a day as "2026-10-09"; anything else as it is, or "".
export function dayText(day) {
  const text = typeof day === "string" ? day : "";
  const m = DAY.exec(text);
  return m ? dayFormat.format(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : text;
}

// The board's wall clock now, as a Date whose UTC fields read it.
function boardDate() {
  const shift = (boardOffsetMin ?? -new Date().getTimezoneOffset()) * 60000;
  return new Date(now() + shift);
}

// The board's wall-clock time now, as "07:12".
export const clockText = () => boardDate().toISOString().slice(11, 16);

// The board's wall-clock time now, as [hour, minute, second].
export function clockParts() {
  const at = boardDate();
  return [at.getUTCHours(), at.getUTCMinutes(), at.getUTCSeconds()];
}

// "in 5 min", "in 1 h 5 min", "now", or "2 min ago". Whole minutes rounded
// towards zero, so "in 4 min" never promises more time than there is.
export function relative(ms) {
  const mins = Math.trunc(ms / 60000);
  if (mins === 0) return "now";
  const span = (m) => (m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}` : `${m} min`);
  return mins > 0 ? `in ${span(mins)}` : `${span(-mins)} ago`;
}

// "40 s ago" or "3 min ago", for how old a report is.
export function age(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

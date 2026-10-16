// Kiosk mode, for a tablet on the wall: the page at "/?kiosk". theme.js marks
// <html data-kiosk="on"> before the first paint; app.css then hides every
// control and fits the page to one screen, and the modules leave their
// controls out (board.js, hero.js, disruptions.js, weather.js). This module
// does what a wall screen needs over days: a 1-2 px shift every few minutes
// against burn-in, dimming in the quiet windows with a platform clock large
// in place of the departures, a banner when the page is behind, and a reload
// once a day to pick up a new version.
//
// The Screen Wake Lock API needs a secure context, which the LAN's plain
// http isn't, so the tablet's own screen-sleep setting keeps it on.

import { $, h, patch, setText } from "./dom.js";
import { icon } from "./icons.js";
import { clockFace, setClock } from "./platformclock.js";

export const SHIFT_MS = 3 * 60_000;
// Where the page sits, in px, a step at a time round a small diamond: no
// more than 2 px from home, each step 1 or 2 px from the last.
export const SHIFTS = [[0, 0], [1, 1], [2, 0], [1, -1], [0, -2], [-1, -1], [-2, 0], [-1, 1], [0, 2]];
export const RELOAD_AT = [3, 30]; // the tablet's own time, inside the usual quiet window
export const RETRY_MS = 5 * 60_000; // when the server can't be reached at reload time

export const isKiosk = () => document.documentElement?.dataset.kiosk === "on";

// The page's offset at `step` (0, 1, 2, ... every SHIFT_MS), as [x, y] px.
export const shiftAt = (step) => SHIFTS[step % SHIFTS.length];

// Moves the page to `step`'s offset. It is a jump, not an animation, so
// reduced motion needs nothing more.
export function applyShift(root, step) {
  const [x, y] = shiftAt(step);
  root.style.setProperty("--shift-x", `${x}px`);
  root.style.setProperty("--shift-y", `${y}px`);
}

// Dims the page while the snapshot says a quiet window is on.
export function applyDim(root, snapshot) {
  if (snapshot?.quiet === true) root.dataset.dim = "on";
  else delete root.dataset.dim;
}

// In a quiet window, the platform clock (platformclock.js) at `parts`
// ([hour, minute, second] on the board's clock), with the time in words
// under it. app.css shows it in place of the departures while the page is
// dimmed; it dims and shifts with the page. Drawn every second, and only
// while dimmed: the face is built the first time.
export function drawNightClock(root, [hour, minute, second]) {
  if (root.dataset.dim !== "on") return;
  const box = $("night-clock");
  if (!box) return;
  if (!box.firstChild) {
    box.append(clockFace(), h("p", { class: "night-time" }, h("span", { class: "vh" }, "Time now: "),
                              h("time", { id: "night-time" })));
  }
  setClock(box.firstChild, hour, minute, second);
  const words = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  const time = $("night-time");
  setText(time, words);
  if (time.getAttribute("datetime") !== words) time.setAttribute("datetime", words);
}

// The banner's words for the status pill (header.js pill()): its own words
// when the page is behind or can't reach the server, else "".
export const bannerText = (state, words) => (state === "stale" || state === "offline" ? words : "");

export function drawBanner(state, words) {
  const el = $("stale-banner");
  const text = bannerText(state, words);
  el.hidden = !text;
  // patch(): drawn every second, the nodes stay put while the words hold.
  patch(el, text ? [h("span", { class: "glyph", "aria-hidden": "true" }, icon("triangle-alert")), " ", text] : []);
}

// Milliseconds from `t` (local ms) to the next `[hour, minute]` on the
// tablet's own clock: today's if it is still to come, else tomorrow's.
export function msUntil([hour, minute], t) {
  const day = new Date(t);
  const next = new Date(day.getFullYear(), day.getMonth(), day.getDate(), hour, minute);
  if (next.getTime() <= t) next.setDate(next.getDate() + 1);
  return next.getTime() - t;
}

// Reloads the page once a day at RELOAD_AT, so a wall tablet picks up a new
// version; only once the server answers, so the tablet never lands on an
// error page, else it tries again every RETRY_MS. The clock, timer, check
// and reload come in, for tests.
export function scheduleReload({ now = Date.now, setTimer = setTimeout, reachable, reload }) {
  const attempt = async () => {
    let ok = false;
    try { ok = await reachable(); } catch { ok = false; }
    if (ok) reload();
    else setTimer(attempt, RETRY_MS);
  };
  setTimer(attempt, msUntil(RELOAD_AT, now()));
}

// Whether the page itself can be fetched now.
async function pageAnswers() {
  const res = await fetch(location.href, { method: "HEAD", cache: "no-store", credentials: "same-origin" });
  return res.ok;
}

// Starts the shift and the daily reload.
export function startKiosk() {
  const root = document.documentElement;
  let step = 0;
  applyShift(root, step);
  setInterval(() => applyShift(root, ++step), SHIFT_MS);
  scheduleReload({ reachable: pageAnswers, reload: () => location.reload() });
}

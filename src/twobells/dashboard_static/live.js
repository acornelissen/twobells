// The page's one polite live region. It speaks only real changes, at most
// once every 30 s: a departure newly cancelled, an expected time moving by
// 2 min or more, a bus newly flagged as having no tracker (once per
// departure, however often the flag comes and goes), a train's platform
// newly changed (once per departure and platform), and disruption newly in
// force on your journeys (a train board message, or roadworks at or near
// your stops), once. The disruption summary line itself is never spoken.
// Station notices (a closed ticket office, lifts out of use) are not
// disruption and are never spoken. The hero says when the departure to catch
// changes, and the header when its status pill does (announce()). Countdowns
// and clocks never go through it. While a
// dialog is open the region sits inside it (dom.js openDialog), as the page
// behind a modal dialog is inert.

import { $, str } from "./dom.js";
import { now as clockNow, parseAt } from "./time.js";
import { describe } from "./alerts.js";
import { inForceNow } from "./disruptions.js";

const GAP_MS = 30_000;
const MOVE_MS = 2 * 60_000;
const MAX_SPOKEN = 3;

let lastSpoken = -Infinity;
let pending = new Map(); // one entry per departure or message; the latest wins
let timer = 0;
let previous = null; // id -> { status, expectedMs (as last spoken, or first seen) }
let previousDisruption = null; // keys of what was in force on your journeys
let ghostsSeen = new Set(); // ids already flagged as ghosts: each is spoken once at most
let platformsSeen = new Set(); // "id|platform" of changed platforms: each is spoken once at most

const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function expectedMs(row) {
  return parseAt(row.expected_at) ?? (row.status === "on_time" ? parseAt(row.scheduled_at) : null);
}

function speak() {
  timer = 0;
  if (!pending.size) return;
  const items = [...pending.values()];
  pending.clear();
  const more = items.length - MAX_SPOKEN;
  const text = items.slice(0, MAX_SPOKEN).join(" ") + (more > 0 ? ` And ${more} more ${more === 1 ? "change" : "changes"}.` : "");
  const region = $("live");
  // Clear first so the same words twice in a row are still spoken.
  region.textContent = "";
  requestAnimationFrame(() => { region.textContent = text; });
  lastSpoken = Date.now();
}

function queue(key, text) {
  pending.delete(key);
  pending.set(key, text);
  if (timer) return;
  const wait = Math.max(0, lastSpoken + GAP_MS - Date.now());
  timer = setTimeout(speak, wait);
}

// Queues words of the page's own, such as the hero's new departure. `key`
// keeps one entry per subject: newer words replace older ones not yet spoken.
export function announce(key, text) {
  queue(key, text);
}

// Takes back words queued with announce() that are not yet spoken. Returns
// whether there were any.
export function unannounce(key) {
  return pending.delete(key);
}

// Compares a new snapshot's rows with the last one's. rowsById maps id to
// { row, mode }. The first snapshot only sets the baseline.
export function noteSnapshot(snapshot, rowsById) {
  const disruption = new Map(inForceNow(snapshot, clockNow()).map((d) => [d.key, d.text]));
  const next = new Map();
  for (const [id, { row, mode }] of rowsById) {
    const now = { status: row.status, expectedMs: expectedMs(row) };
    next.set(id, now);
    const before = previous && previous.get(id);
    const what = capital(describe(row, mode, false));
    if (row.ghost === true && !ghostsSeen.has(id)) {
      ghostsSeen.add(id);
      // Not on the first snapshot, nor for a row that is new to the board.
      if (before) queue(`ghost:${id}`, `${what} has no tracker yet and may not be running.`);
    }
    // A replacement bus has no platform to change.
    const changed = mode === "train" && row.platform_changed === true && row.replacement_bus !== true;
    const platform = changed ? str(row.platform) : "";
    if (platform && !platformsSeen.has(`${id}|${platform}`)) {
      platformsSeen.add(`${id}|${platform}`);
      // As for ghosts: not on the first snapshot, nor for a row new to the board.
      if (before) queue(`platform:${id}`, `${what} now leaves from platform ${platform}.`);
    }
    if (!before) continue;
    if (now.status === "cancelled" && before.status !== "cancelled") {
      queue(id, `${what} is now cancelled.`);
    } else if (now.status !== "cancelled" && now.expectedMs !== null && before.expectedMs !== null
        && Math.abs(now.expectedMs - before.expectedMs) >= MOVE_MS) {
      const expected = str(row.expected) || str(row.scheduled);
      queue(id, `${what} is now expected at ${expected}.`);
    } else if (before.expectedMs !== null) {
      // Small moves add up: compare with the time last spoken, not the last snapshot.
      now.expectedMs = before.expectedMs;
    }
  }
  if (previousDisruption) {
    for (const [key, text] of disruption) {
      if (!previousDisruption.has(key)) queue(key, `New disruption: ${text}${text.endsWith(".") ? "" : "."}`);
    }
  }
  previous = next;
  previousDisruption = new Set(disruption.keys());
  ghostsSeen = new Set([...ghostsSeen].filter((id) => rowsById.has(id)));
  platformsSeen = new Set([...platformsSeen].filter((key) => rowsById.has(key.split("|")[0])));
}

// The page's one polite live region. It speaks only real changes, at most
// once every 30 s: a departure newly cancelled, an expected time moving by
// 2 min or more, a bus newly flagged as having no tracker (once per
// departure, however often the flag comes and goes), and a new disruption
// message. Station notices (a closed ticket office, lifts out of use) are not
// disruption and are never spoken. The hero says when the departure to catch
// changes (announce()). Countdowns and clocks never go through it.

import { $, str } from "./dom.js";
import { parseAt } from "./time.js";
import { describe } from "./alerts.js";
import { trainMessages } from "./board.js";

const GAP_MS = 30_000;
const MOVE_MS = 2 * 60_000;
const MAX_SPOKEN = 3;

let lastSpoken = -Infinity;
let pending = new Map(); // one entry per departure or message; the latest wins
let timer = 0;
let previous = null; // id -> { status, expectedMs (as last spoken, or first seen) }
let previousMessages = null;
let ghostsSeen = new Set(); // ids already flagged as ghosts: each is spoken once at most

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

// Compares a new snapshot's rows with the last one's. rowsById maps id to
// { row, mode }. The first snapshot only sets the baseline.
export function noteSnapshot(snapshot, rowsById) {
  const messages = new Set(trainMessages(snapshot.trains).filter((m) => m.kind === "service").map((m) => m.text));
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
  if (previousMessages) {
    for (const message of messages) if (!previousMessages.has(message)) queue(`message:${message}`, `New disruption: ${message}`);
  }
  previous = next;
  previousMessages = messages;
  ghostsSeen = new Set([...ghostsSeen].filter((id) => rowsById.has(id)));
}

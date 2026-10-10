// The "Leave now" banner and the reminders set with "Remind me" in the
// departure sheet.

import { $, h, setText, store, str, num, obj } from "./dom.js";
import { countdown, now, parseAt } from "./time.js";

const KEY = "dashboard.alerts";
const LEAD_MS = 60_000; // an alert fires when it is a minute to leaving time
const LATE_MS = 5 * 60_000; // and not at all once leaving time is this far gone

// { id: { expires, fired, cancelled } }, kept across reloads until it expires.
let alerts = {};

function save() { store.setJSON(KEY, alerts); }

function load() {
  const saved = store.getJSON(KEY, {});
  alerts = {};
  const t = Date.now();
  for (const [id, a] of Object.entries(obj(saved) || {})) {
    if (/^[0-9a-f]{16}$/.test(id) && obj(a) && num(a.expires) > t) {
      alerts[id] = { expires: a.expires, fired: !!a.fired, cancelled: !!a.cancelled };
    }
  }
  save();
}
load();

export const isOn = (id) => Object.hasOwn(alerts, id);

const notifySupported = () => "Notification" in globalThis;
const canNotify = () => globalThis.isSecureContext === true && notifySupported();

function showNote(text) {
  const note = $("notify-note");
  setText(note, text);
  note.hidden = !text;
}

// What a reminder can do in this browser, said before it is set. Notifications
// need a secure context: on the LAN's plain http they can't be had at all.
export function remindNote() {
  if (!canNotify()) {
    return globalThis.isSecureContext === true
      ? "The reminder shows only on this page, while it is open: this browser can't show notifications."
      : "The reminder shows only on this page, while it is open.";
  }
  if (Notification.permission === "denied") return "Notifications are blocked, so the reminder shows only on this page.";
  return "A minute before you need to leave, the page shows a reminder, and a notification if you allow them.";
}

// What a row is called in an alert: "the 07:12 train to Manchester Piccadilly",
// plus ", platform 1" or " from Henry Street (Stop A)" when `where` is set.
export function describe(row, mode, where = true) {
  const scheduled = str(row.scheduled);
  const dest = str(row.destination);
  if (mode === "bus") {
    const stop = str(row.stop_name) || (str(row.stop) ? `Stop ${str(row.stop)}` : "");
    return `the ${scheduled} ${str(row.line)} bus to ${dest}${where && stop ? ` from ${stop}` : ""}`;
  }
  const platform = str(row.platform);
  return `the ${scheduled} train to ${dest}${where && platform ? `, platform ${platform}` : ""}`;
}

// Turns a row's reminder on or off. Runs inside the click, so the browser lets
// it ask for notification permission; `note` is told what the reminder can do.
export function toggle(id, row, note = showNote) {
  if (isOn(id)) {
    delete alerts[id];
    save();
    return false;
  }
  const latest = Math.max(...["leave_at", "expected_at", "scheduled_at"].map((k) => parseAt(row[k]) ?? 0));
  alerts[id] = { expires: Date.now() + Math.max(0, latest - now()) + 60 * 60_000, fired: false, cancelled: false };
  save();
  if (canNotify() && Notification.permission === "default") {
    Notification.requestPermission().then(() => note(remindNote())).catch(() => {});
  } else {
    note(remindNote());
  }
  return true;
}

// Shows the in-page alert (role=alert speaks it) and, where allowed, a
// system notification with the same words.
function fire(id, title, body) {
  const banner = $("alert-banner");
  const item = h("div", { class: "alert-item" },
    h("p", {}, h("strong", {}, `${title}.`), " ", body),
    h("button", {
      type: "button", class: "quiet",
      on: {
        click: () => {
          const hadFocus = item.contains(document.activeElement);
          item.remove();
          if (hadFocus) $("leave-heading").focus();
        },
      },
    }, "Dismiss"));
  banner.append(item);
  if (canNotify() && Notification.permission === "granted") {
    try { new Notification(title, { body, tag: id }); } catch { /* some phones need a service worker */ }
  }
}

// Fires due alerts. rowsById maps id to { row, mode }.
export function tickAlerts(rowsById) {
  const t = now();
  let changed = false;
  for (const [id, a] of Object.entries(alerts)) {
    const entry = rowsById.get(id);
    if (Date.now() > a.expires) { delete alerts[id]; changed = true; continue; }
    if (!entry) continue;
    const { row, mode } = entry;
    if (row.status === "cancelled") {
      if (!a.cancelled) {
        a.cancelled = true; changed = true;
        const what = describe(row, mode, false);
        fire(id, mode === "bus" ? "Bus cancelled" : "Train cancelled", `${what[0].toUpperCase()}${what.slice(1)} is cancelled.`);
      }
      continue;
    }
    const leave = parseAt(row.leave_at);
    if (leave === null || a.fired) continue;
    if (t >= leave - LEAD_MS && t <= leave + LATE_MS) {
      a.fired = true; changed = true;
      fire(id, "Time to leave", `Leave now for ${describe(row, mode)}.`);
    }
  }
  if (changed) save();
}

// The next departure you can still make on foot: leaving time not yet passed,
// not cancelled. Returns { row, mode, leave } or null.
export function nextCatchable(rowsById) {
  const t = now();
  let best = null;
  for (const { row, mode } of rowsById.values()) {
    const leave = parseAt(row.leave_at);
    if (leave === null || leave < t || row.status === "cancelled") continue;
    if (!best || leave < best.leave) best = { row, mode, leave };
  }
  return best;
}

// The countdown banner. Updated every second, so it is deliberately not a
// live region: the announcer speaks only real changes.
export function renderLeave(rowsById) {
  const next = nextCatchable(rowsById);
  const banner = $("leave");
  const big = $("leave-time");
  const what = $("leave-what");
  if (!next) {
    banner.classList.remove("soon", "now");
    setText(big, "Nothing to leave for");
    setText(what, "No departure with a walk time is coming up.");
    return;
  }
  const ms = next.leave - now();
  banner.classList.toggle("now", ms <= LEAD_MS);
  banner.classList.toggle("soon", ms > LEAD_MS && ms <= 3 * LEAD_MS);
  setText(big, ms <= LEAD_MS ? "Leave now" : `Leave in ${countdown(ms)}`);
  setText(what, `for ${describe(next.row, next.mode)}.`);
}

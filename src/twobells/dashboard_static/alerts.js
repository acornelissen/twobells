// The reminders set with "Remind me" in the departure sheet or the hero, and
// the in-page alerts they fire.

import { $, h, setText, store, str, num, obj } from "./dom.js";
import { now, parseAt } from "./time.js";
import { trainNoun } from "./extras.js";

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

// What a row is called in an alert: "the 07:12 train to Manchester Piccadilly"
// (or "replacement bus"), plus ", platform 1" or " from Henry Street (Stop A)"
// when `where` is set.
export function describe(row, mode, where = true) {
  const scheduled = str(row.scheduled);
  const dest = str(row.destination);
  if (mode === "bus") {
    const stop = str(row.stop_name) || (str(row.stop) ? `Stop ${str(row.stop)}` : "");
    return `the ${scheduled} ${str(row.line)} bus to ${dest}${where && stop ? ` from ${stop}` : ""}`;
  }
  if (mode === "metro") {
    const stop = str(row.stop_name);
    const noun = row.replacement_bus === true ? "replacement bus" : "tram";
    return `the ${scheduled} ${noun} to ${dest}${where && stop ? ` from ${stop}` : ""}`;
  }
  const platform = str(row.platform);
  return `the ${scheduled} ${trainNoun(row)} to ${dest}${where && platform ? `, platform ${platform}` : ""}`;
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
// system notification with the same words. While a dialog is open the page
// behind it is inert, so the alert shows in the dialog too; dismissing either
// copy dismisses both.
function fire(id, title, body) {
  const dialog = document.querySelector("dialog[open]");
  const banners = [$("alert-banner"), dialog && dialog.querySelector("[data-alerts]")].filter(Boolean);
  const copies = [];
  const dismiss = () => {
    const focused = copies.find((item) => item.contains(document.activeElement));
    const inDialog = !!focused && !!dialog && dialog.contains(focused);
    for (const item of copies) item.remove();
    if (inDialog) dialog.querySelector("[data-dialog-title]").focus();
    else if (focused) $("leave-heading").focus();
  };
  for (const banner of banners) {
    const item = h("div", { class: "alert-item" },
      h("p", {}, h("strong", {}, `${title}.`), " ", body),
      h("button", { type: "button", class: "quiet", on: { click: dismiss } }, "Dismiss"));
    copies.push(item);
    banner.append(item);
  }
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

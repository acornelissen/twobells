// The disruptions panel: bus roadworks, diversions and stop closures, and
// rail engineering works, in words, those in force first, each part most
// relevant first. Bus rows that one touches get a "Disruption" link to it.
// Rail messages from the train board stay in their own panel (board.js).

import { $, h, vh, fill, setText, str, list, obj, validId } from "./dom.js";
import { now, parseAt } from "./time.js";
import { icon } from "./icons.js";

const MODE = { bus: "Bus", train: "Train" };
// The server's relevance tiers, most relevant first: at one of our stops, at a
// stop on our routes nearby, or on our line inside our area.
const RELEVANCE = { your_stop: 0, nearby: 1, line: 2 };
const rank = (item) => RELEVANCE[item.relevance] ?? 2;
// Severity in words, and the badge colours that reinforce them.
const SEVERITY = {
  slight: ["Minor disruption", "st-unknown", "info"],
  normal: ["Disruption", "st-late", "triangle-alert"],
  severe: ["Severe disruption", "st-cancelled", "octagon-x"],
  unknown: ["Disruption", "st-late", "triangle-alert"],
};
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const PARTS = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/;

// "2026-10-12" for a date in the board's own time, as the server wrote it.
const dayOf = (iso) => (PARTS.exec(str(iso)) || []).slice(1, 4).join("-");

function nextDay(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
}

// "08:00 today", "16:00 tomorrow" or "Fri 30 Oct 16:00", in the board's time.
export function whenText(iso, today) {
  const m = PARTS.exec(str(iso));
  if (!m) return "";
  const [, y, mo, d, hh, mm] = m;
  const day = `${y}-${mo}-${d}`;
  const time = `${hh}:${mm}`;
  if (day === today) return `${time} today`;
  if (today && day === nextDay(today)) return `${time} tomorrow`;
  const weekday = DAYS[new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d))).getUTCDay()];
  return `${weekday} ${Number(d)} ${MONTHS[Number(mo) - 1]} ${time}`;
}

// The item's periods as [fromMs, toMs or Infinity, fromIso, toIso], in order.
function periods(item) {
  return list(item.periods).filter(obj).map((p) => {
    const from = parseAt(p.from_at);
    const to = p.to_at === null ? Infinity : parseAt(p.to_at);
    return from === null || to === null ? null : [from, to, str(p.from_at), p.to_at === null ? "" : str(p.to_at)];
  }).filter(Boolean).sort((a, b) => a[0] - b[0]);
}

// Whether an item is in force at a moment (ms).
const inForce = (item, at) => periods(item).some(([from, to]) => from <= at && at <= to);

// The items still to come or in force, each with its current or next period.
function arranged(items, t) {
  const found = [];
  for (const item of items) {
    const left = periods(item).filter(([, to]) => to >= t);
    if (!left.length) continue;
    const current = left.find(([from]) => from <= t);
    found.push({ item, period: current || left[0], current: !!current, more: left.length - 1 });
  }
  const order = (a, b) => rank(a.item) - rank(b.item) || a.period[0] - b.period[0];
  return { current: found.filter((f) => f.current).sort(order), coming: found.filter((f) => !f.current).sort(order) };
}

function whenLine({ period, current, more }, today) {
  const [, , fromIso, toIso] = period;
  const until = toIso ? `until ${whenText(toIso, today)}` : "no end time given";
  const text = current ? `In force now, ${until}.` : `From ${whenText(fromIso, today)}${toIso ? ` ${until}` : ""}.`;
  return more > 0 ? `${text} Then ${more} more ${more === 1 ? "time" : "times"} this week.` : text;
}

function severityBadge(severity) {
  const [words, cls, name] = SEVERITY[severity] || SEVERITY.unknown;
  return h("span", { class: ["badge", cls] }, h("span", { class: "glyph", "aria-hidden": "true" }, icon(name)),
    h("span", { class: "badge-text" }, words));
}

function linkHost(link) {
  try { return new URL(link).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function facts(item) {
  const out = [];
  if (str(item.reason)) out.push(str(item.reason));
  out.push(item.planned === true ? "Planned" : "Unplanned");
  const lines = list(item.lines).map(str).filter(Boolean);
  if (lines.length) out.push(`${lines.length === 1 ? "Line" : "Lines"} ${lines.join(", ")}`);
  const stops = list(item.stops).map(str).filter(Boolean);
  if (stops.length) out.push(`Your ${stops.length === 1 ? "stop" : "stops"} ${stops.join(" and ")}`);
  const places = list(item.places).map(str).filter(Boolean);
  if (places.length) out.push(`At ${places.slice(0, 6).join(", ")}${places.length > 6 ? " and more" : ""}`);
  return out.join(". ") + ".";
}

function entry(arranged, today) {
  const { item } = arranged;
  const title = str(item.title);
  const link = str(item.link);
  const host = link.startsWith("https://") ? linkHost(link) : "";
  return h("li", {
    class: "dis-item", id: `disruption-${item.id}`, tabindex: "-1", dataset: { focusKey: `disruption:${item.id}` },
  },
    h("h4", { class: "dis-title" }, h("span", { class: "dis-mode" }, `${MODE[item.mode] || "Bus"}:`), " ", title),
    h("p", { class: "dis-when" }, severityBadge(item.severity), " ", whenLine(arranged, today)),
    h("p", { class: "dis-facts" }, facts(item)),
    str(item.summary) ? h("p", { class: "dis-summary" }, str(item.summary)) : null,
    host ? h("p", { class: "dis-link" }, h("a", { href: link, rel: "noopener noreferrer" },
      `More from ${host}`, vh(` about ${title}`))) : null);
}

function group(heading, found, today) {
  if (!found.length) return null;
  return [h("h3", {}, heading), h("ul", { class: "dis-list" }, found.map((f) => entry(f, today)))];
}

// The panel's status line ("Bus disruptions: problem: …" and any notes),
// and whether it reports a problem.
function statusText(status) {
  const parts = [];
  let problem = false;
  for (const [mode, label] of [["bus", "Bus disruptions"], ["train", "Engineering works"]]) {
    const st = obj(obj(status)?.[mode]);
    if (!st) continue;
    if (str(st.error)) parts.push(`${label}: problem: ${str(st.error)}.`);
    else if (st.stale) parts.push(`${label}: not updated lately.`);
    problem ||= !!(str(st.error) || st.stale);
    if (str(st.note)) parts.push(`${label}: ${str(st.note)}.`);
  }
  return [parts.join(" "), problem];
}

// The items the page can use: valid ids and modes only.
function items(snapshot) {
  const part = obj(snapshot.disruptions);
  return part ? list(part.items).filter((i) => obj(i) && validId(i.id) && MODE[i.mode]) : [];
}

export function renderDisruptions(snapshot) {
  const section = $("disruptions");
  const part = obj(snapshot.disruptions);
  section.hidden = !part;
  if (!part) return;
  const today = dayOf(snapshot.generated_at);
  const { current, coming } = arranged(items(snapshot), now());
  const [status, problem] = statusText(part.status);
  const statusEl = $("disruptions-status");
  setText(statusEl, status);
  statusEl.hidden = !status;
  statusEl.classList.toggle("problem", problem);
  fill($("disruptions-body"),
    current.length || coming.length ? null
      : h("p", { class: "empty" }, "No roadworks, diversions or engineering works on your lines this week."),
    group("In force now", current, today),
    group("Coming up this week", coming, today));
}

// Moves focus to an item when its row link is followed, so keyboard and
// screen reader users land on it, not just the scroll position.
function follow(event) {
  const target = document.getElementById(event.currentTarget.getAttribute("href").slice(1));
  if (!target) return;
  event.preventDefault();
  target.focus();
}

// Adds a "Disruption" link to each bus row whose line or stop an item in
// force at the bus's time names. rowsById maps id to { row, mode }.
export function markBusRows(snapshot, rowsById) {
  const found = items(snapshot).filter((i) => i.mode === "bus");
  if (!found.length) return;
  for (const tr of document.querySelectorAll("#buses-board tr[data-id]")) {
    const entryFor = rowsById.get(tr.dataset.id);
    if (!entryFor || entryFor.mode !== "bus") continue;
    const row = entryFor.row;
    const at = parseAt(row.expected_at) ?? parseAt(row.scheduled_at);
    if (at === null) continue;
    const hit = found.find((i) => (list(i.lines).includes(str(row.line)) || list(i.stops).includes(str(row.stop)))
      && inForce(i, at));
    const cell = tr.querySelector(".c-dest");
    if (!hit || !cell) continue;
    cell.append(h("a", { class: "dis-mark", href: `#disruption-${hit.id}`, on: { click: follow } },
      h("span", { class: "glyph", "aria-hidden": "true" }, icon("triangle-alert")), "Disruption",
      vh(` for the ${str(row.scheduled)} ${str(row.line)}: ${str(hit.title)}`)));
  }
}

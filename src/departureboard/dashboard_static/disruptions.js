// The disruption summary under the hero: one line for what affects your
// journeys now, with every item behind it in a <details>. Items are bus
// roadworks, diversions and stop closures, rail engineering works, and the
// train board's disruption messages, in words, those in force first, each
// part most relevant first; "Coming up this week" stays folded inside. Bus
// rows that one touches get a "Disruption" link to it.

import { $, h, vh, fill, setText, str, list, obj, own, validId } from "./dom.js";
import { now, parseAt } from "./time.js";
import { icon } from "./icons.js";
import { stopNames, trainMessages } from "./board.js";
import { isKiosk } from "./kiosk.js";

const MODE = { bus: "Bus", train: "Train" };
// The server's relevance tiers, most relevant first: at one of our stops, at a
// stop on our routes nearby, or on our line inside our area.
const RELEVANCE = { your_stop: 0, nearby: 1, line: 2 };
const rank = (item) => own(RELEVANCE, item.relevance) ?? 2;
// The tiers the summary line speaks of: those on your journeys.
const ON_YOUR_JOURNEYS = new Set(["your_stop", "nearby"]);
const WHERE = { your_stop: " (your stop)", nearby: " (nearby)" };
// Darwin's message severities in the items' terms.
const RAIL_SEVERITY = { minor: "slight", normal: "normal", major: "normal", severe: "severe" };
// A summary longer than this is cut to two lines, with "More" to show it all.
const LONG = 100;
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
  const [words, cls, name] = own(SEVERITY, severity) || SEVERITY.unknown;
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

// The items whose whole summary is showing, by id, kept across snapshots.
const opened = new Set();

function toggleSummary(event) {
  const button = event.currentTarget;
  const id = button.dataset.item;
  const open = !opened.has(id);
  if (open) opened.add(id);
  else opened.delete(id);
  button.setAttribute("aria-expanded", String(open));
  setText(button, open ? "Less" : "More");
  button.parentNode.querySelector(".dis-summary").classList.toggle("clamp", !open);
}

// The item's summary: a long one is cut to two lines on screen, with "More"
// to show the rest. Screen readers get it all either way.
function summaryPart(item) {
  const text = str(item.summary);
  if (!text) return null;
  if (text.length <= LONG) return h("p", { class: "dis-summary" }, text);
  const open = opened.has(item.id);
  return [h("p", { class: ["dis-summary", open ? null : "clamp"] }, text),
    h("button", {
      type: "button", class: "link dis-more", "aria-expanded": String(open),
      dataset: { item: item.id, focusKey: `dis-more:${item.id}` }, on: { click: toggleSummary },
    }, open ? "Less" : "More")];
}

function entry(arranged, today) {
  const { item } = arranged;
  const title = str(item.title);
  const link = str(item.link);
  const host = link.startsWith("https://") ? linkHost(link) : "";
  return h("li", {
    class: "dis-item", id: `disruption-${item.id}`, tabindex: "-1", dataset: { focusKey: `disruption:${item.id}` },
  },
    h("h4", { class: "dis-title" }, h("span", { class: "dis-mode" }, `${own(MODE, item.mode) || "Bus"}:`), " ", title),
    h("p", { class: "dis-when" }, severityBadge(item.severity), " ", whenLine(arranged, today)),
    h("p", { class: "dis-facts" }, facts(item)),
    summaryPart(item),
    host ? h("p", { class: "dis-link" }, h("a", {
      href: link, rel: "noopener noreferrer", dataset: { focusKey: `dis-link:${item.id}` },
    }, `More from ${host}`, vh(` about ${title}`))) : null);
}

// One of the train board's disruption messages, as a row like the items'.
function messageEntry(message) {
  return h("li", { class: "dis-item" },
    h("h4", { class: "dis-title" }, h("span", { class: "dis-mode" }, "Train:"), " ", message.text),
    h("p", { class: "dis-when" }, severityBadge(message.severity), " In force now."));
}

// The train board's disruption messages, with the items' severities.
function serviceMessages(snapshot) {
  return trainMessages(snapshot.trains).filter((m) => m.kind === "service")
    .map((m) => ({ text: m.text, severity: own(RAIL_SEVERITY, m.severity) || "unknown" }));
}

// "Roadworks at Henry Street A (your stop) until 18:00 today": a bus item by
// its cause and the first of our stops (else places) it names; a rail one by
// its title.
function headline({ item, period }, names, today) {
  const where = list(item.stops).map((label) => str(names[str(label)])).find(Boolean)
    || list(item.places).map(str).find(Boolean);
  const what = item.mode === "bus" && where
    ? `${str(item.reason) || "Disruption"} at ${where}${own(WHERE, item.relevance) || ""}` : str(item.title);
  return period[3] ? `${what} until ${whenText(period[3], today)}` : what;
}

// What affects your journeys at `t` (ms): the items in force at or near your
// stops, and the train board's disruption messages, as { key, text, severe },
// the severe ones first. The summary line leads with the first; the live
// region announces each new one once.
export function inForceNow(snapshot, t) {
  const today = dayOf(snapshot.generated_at);
  const names = stopNames(obj(snapshot.buses)?.stops);
  const found = [
    ...arranged(items(snapshot), t).current.filter((f) => ON_YOUR_JOURNEYS.has(f.item.relevance)).map((f) => ({
      key: `item:${f.item.id}`, text: headline(f, names, today), severe: f.item.severity === "severe",
    })),
    ...serviceMessages(snapshot).map((m) => ({ key: `message:${m.text}`, text: m.text, severe: m.severity === "severe" })),
  ];
  return found.sort((a, b) => Number(b.severe) - Number(a.severe));
}

// The summary line as [shown, said]: the first thing in force on your
// journeys and how many more are listed, or that there is none.
function summaryLine(relevant, listed) {
  if (relevant.length) {
    const lead = relevant[0].text.replace(/\.$/, "");
    const more = listed - 1;
    return more > 0 ? [`${lead} · ${more} more this week`, `${lead}. ${more} more this week.`] : [lead, lead];
  }
  const none = "No disruption on your journeys";
  const notices = `${listed} ${listed === 1 ? "notice" : "notices"} this week`;
  return listed ? [`${none} · ${notices}`, `${none}. ${notices}.`] : [none, none];
}

// The section's status line ("Bus disruptions: problem: …" and any notes),
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
  return part ? list(part.items).filter((i) => obj(i) && validId(i.id) && own(MODE, i.mode)) : [];
}

// Draws the summary line and the items behind it. Nothing at all shows
// without disruption data; the <details> elements stay put, so what is open
// stays open across snapshots.
export function renderDisruptions(snapshot) {
  const section = $("disruptions");
  const part = obj(snapshot.disruptions);
  const messages = serviceMessages(snapshot);
  section.hidden = !part && !messages.length;
  if (section.hidden) return;
  const t = now();
  const today = dayOf(snapshot.generated_at);
  const { current, coming } = arranged(items(snapshot), t);
  const relevant = inForceNow(snapshot, t);
  const severe = relevant.some((r) => r.severe);
  const listed = messages.length + current.length + coming.length;
  const [status, problem] = statusText(part?.status);
  const statusEl = $("disruptions-status");
  setText(statusEl, status);
  statusEl.hidden = !status;
  statusEl.classList.toggle("problem", problem);

  const details = $("disruptions-details");
  details.hidden = listed === 0;
  if (isKiosk()) {
    // A wall tablet shows the summary line only: it never opens, nor takes focus.
    details.open = false;
    details.querySelector("summary").setAttribute("tabindex", "-1");
  }
  $("disruptions-none").hidden = listed > 0;
  details.classList.toggle("bad", severe);
  details.classList.toggle("late", relevant.length > 0 && !severe);
  details.classList.toggle("quiet", relevant.length === 0);
  const [shown, said] = summaryLine(relevant, listed);
  fill($("disruptions-summary"), h("span", { "aria-hidden": "true" }, shown), vh(said));
  const glyph = severe ? "octagon-x" : relevant.length ? "triangle-alert" : "info";
  const glyphEl = $("disruptions-glyph");
  if (glyphEl.dataset.icon !== glyph) {
    glyphEl.dataset.icon = glyph;
    glyphEl.replaceChildren(icon(glyph));
  }

  $("disruptions-now-part").hidden = messages.length + current.length === 0;
  fill($("disruptions-now"), messages.map(messageEntry), current.map((f) => entry(f, today)));
  $("disruptions-coming").hidden = coming.length === 0;
  setText($("disruptions-coming-count"), String(coming.length));
  fill($("disruptions-later"), coming.map((f) => entry(f, today)));
}

// Opens the item when its row link is followed (and "Coming up this week",
// if it is there), and moves focus to it, so keyboard and screen reader users
// land on it, not just the scroll position.
function follow(event) {
  const target = document.getElementById(event.currentTarget.getAttribute("href").slice(1));
  if (!target) return;
  event.preventDefault();
  for (let el = target.parentNode; el; el = el.parentNode) if (el.nodeName === "DETAILS") el.open = true;
  target.focus();
}

// Adds a "Disruption" link to each bus row whose line or stop an item in
// force at the bus's time names. rowsById maps id to { row, mode }. A wall
// tablet has nowhere to go, so no links.
export function markBusRows(snapshot, rowsById) {
  const found = items(snapshot).filter((i) => i.mode === "bus");
  if (!found.length || isKiosk()) return;
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

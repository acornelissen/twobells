// The rail industry's punctuality measures as tables, for the Performance
// page (performance.js) and each departure's sheet (history.js), from the
// `periods` in GET /api/performance and /api/history/<id>. Counts with their
// percentages, "62% (31 of 50)", in plain tables, never charts.

import { h, num, str, obj, list } from "./dom.js";

export const NOT_ENOUGH = "Not enough days yet";
const MAX_SPANS = 4;

// A count from the feed: a whole number, 0 or more, else 0.
const count = (v) => {
  const n = num(v);
  return n !== null && Number.isInteger(n) && n >= 0 ? n : 0;
};

// The bus late threshold, in whole minutes (the settings allow 1 to 60).
export function lateMinutes(v) {
  const n = num(v);
  return n !== null && Number.isInteger(n) && n >= 1 && n <= 60 ? n : 2;
}

// "62%" of n out of `of`, rounded, but never 100% unless every one was, nor
// 0% unless none was.
export function percent(n, of) {
  if (of <= 0) return "";
  let p = Math.round((100 * n) / of);
  if (p === 100 && n < of) p = 99;
  if (p === 0 && n > 0) p = 1;
  return `${p}%`;
}

// "Last 7 days", "Last 28 days".
export const spanName = (span) => `Last ${span} days`;

// The columns for a mode, as [key, heading, what it means]. Trains get the
// industry's measures; buses get on time by the bus late threshold, and no
// cancellations, as bus data never says a bus was cancelled.
export function columns(mode, late) {
  if (mode === "bus") {
    return [
      ["on_time", "On time", `under ${lateMinutes(late)} min late`],
      ["within_15", "T-15", "under 15 min late"],
    ];
  }
  return [
    ["right_time", "Right Time", "at or before its time"],
    ["within_3", "T-3", "under 3 min late"],
    ["within_15", "T-15", "under 15 min late"],
    ["cancelled", "Cancelled", ""],
  ];
}

// Every column for a mode, as [key, heading, what it means]: the measures,
// then departures with no delay known (left out of the measures), and the
// days anything was recorded.
function allColumns(mode, late) {
  return [
    ...columns(mode, late),
    mode === "bus" ? ["unknown", "No tracker", "not counted"] : ["unknown", "Not known", "no time given"],
    ["days", "Days", ""],
  ];
}

const heading = (scope, [, text, sub]) =>
  h("th", { scope }, text, sub ? [" ", h("span", { class: "sub" }, sub)] : null);

// Why a period has no figures, or "" when it has.
function missing(period) {
  const p = obj(period);
  if (!p) return "Nothing recorded yet";
  if (p.enough !== true) return NOT_ENOUGH;
  return count(p.counted) ? "" : "No times known yet";
}

// One figure of a period that has them: a measure as "62% (31 of 50)", a
// count as itself.
function figure(key, period, measures) {
  const counted = count(period.counted);
  const n = count(period[key]);
  if (!measures.has(key)) return h("td", {}, String(n));
  const within = Math.min(n, counted);
  return h("td", {}, percent(within, counted), h("span", { class: "of" }, ` (${within} of ${counted})`));
}

// The header row of a table with a row per route, after the heading `first`.
export function headRow(first, mode, late) {
  return h("tr", {}, h("th", { scope: "col" }, first), allColumns(mode, late).map((col) => heading("col", col)));
}

// The cells of a row per route for one period, or one cell saying why there are none.
export function cells(mode, late, period) {
  const cols = allColumns(mode, late);
  const why = missing(period);
  if (why) return [h("td", { colspan: cols.length }, why)];
  const measures = new Set(columns(mode, late).map(([key]) => key));
  return cols.map(([key]) => figure(key, period, measures));
}

// A route's figures the other way round, a row per measure and a column per
// span, to fit a departure's sheet: [header row, body rows]. A span with no
// figures says why once, down its column.
export function spanRows(mode, late, spans) {
  const cols = allColumns(mode, late);
  const measures = new Set(columns(mode, late).map(([key]) => key));
  const head = h("tr", {}, h("th", { scope: "col" }, "Measure"),
    spans.map(([span]) => h("th", { scope: "col" }, spanName(span))));
  const body = cols.map((col, i) => h("tr", {}, heading("row", col), spans.map(([, period]) => {
    const why = missing(period);
    if (!why) return figure(col[0], period, measures);
    return i === 0 ? h("td", { rowspan: cols.length }, why) : null;
  })));
  return [head, body];
}

// The periods of a route or a total, by span: {7: {...}, 28: {...}}.
export function bySpan(periods) {
  const found = new Map();
  for (const p of list(periods).filter(obj).slice(0, MAX_SPANS)) {
    const span = num(p.span);
    if (span !== null && Number.isInteger(span) && span > 0) found.set(span, p);
  }
  return found;
}

// A table that scrolls sideways when it doesn't fit: a region named by its
// caption, and a tab stop while it overflows (fitTables), so keyboard users
// can reach and scroll it.
export function scrolling(table, name) {
  return h("div", { class: "scroll", role: "region", "aria-label": str(name) }, table);
}

export function fitTables() {
  for (const el of document.querySelectorAll('.scroll[role="region"]')) {
    if (el.scrollWidth > el.clientWidth) el.setAttribute("tabindex", "0");
    else el.removeAttribute("tabindex");
  }
}
globalThis.addEventListener?.("resize", fitTables);

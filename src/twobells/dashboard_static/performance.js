// The Performance page (/performance): how the trains and buses have run
// over the last 7 and 28 days by the rail industry's measures, all of them
// together and route by route, from GET /api/performance, as tables
// (measures.js). It loads once, as the figures change slowly; reloading the
// page brings them up to date. The snapshot gives the page its place and
// the stops' names. Its header has the dashboard's Look picker (look.js)
// and links. After the buses, the trains that reached the filter station
// late enough for Delay Repay (renderRepay), or why that is off.

import { $, h, fill, setText, str, num, obj, list } from "./dom.js";
import { setUpLook } from "./look.js";
import { getJSON } from "./api.js";
import { dayText, hhmm } from "./time.js";
import { placeName } from "./header.js";
import { stopNames } from "./board.js";
import { bySpan, cells, fitTables, headRow, lateMinutes, scrolling, spanName } from "./measures.js";

const MAX_ROUTES = 50;
const MAX_REPAY = 100;
const COMPENSATION = "https://www.nationalrail.co.uk/help-and-assistance/compensation-and-refunds/";
const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/;
const DAY = /^\d{4}-\d\d-\d\d$/;
const PARTS = [
  ["train", "trains", "All trains"],
  ["bus", "buses", "All buses"],
];

// --- The figures.

// A route's name in its row: "Manchester Piccadilly", "341 to Hyde Bus
// Station" with its stop under it.
function routeHeading(mode, route, names) {
  const label = str(route.label);
  const stop = str(route.stop);
  if (mode !== "bus" || !stop) return label;
  const name = str(Object.hasOwn(names, stop) ? names[stop] : "") || `Stop ${stop}`;
  return [label, " ", h("span", { class: "sub" }, `from ${name}`)];
}

// One table per span: the total first, then each route.
function partTables(mode, part, total, names) {
  const routes = list(part.routes).filter((r) => obj(r) && str(r.label)).slice(0, MAX_ROUTES);
  const late = part.late_minutes;
  const spans = [...bySpan(obj(part.all)?.periods).keys()];
  return spans.map((span) => {
    const caption = `${spanName(span)}, ${mode === "bus" ? "buses" : "trains"}`;
    const row = (heading, periods, cls) =>
      h("tr", { class: cls }, h("th", { scope: "row" }, heading), cells(mode, late, bySpan(periods).get(span)));
    return [
      h("h3", {}, spanName(span)),
      scrolling(h("table", { class: "history measures" },
        h("caption", { class: "vh" }, caption),
        h("thead", {}, headRow("Route", mode, late)),
        h("tbody", {},
          row(total, obj(part.all)?.periods, "total"),
          routes.map((route) => row(routeHeading(mode, route, names), route.periods)))), caption),
    ];
  });
}

// --- Delay Repay: the trains that got to the filter station 15 or more minutes late.

const clock = (v) => (CLOCK.test(str(v)) ? str(v) : "");

// A train from the report, checked, or null if it can't be shown.
function repayTrain(t) {
  const train = obj(t);
  const late = num(train?.minutes_late);
  if (!train || !DAY.test(str(train.day)) || !clock(train.scheduled) || !clock(train.booked)
      || !clock(train.arrived) || late === null || !Number.isInteger(late) || late < 15 || late > 1440) {
    return null;
  }
  return { ...train, minutes_late: late };
}

function repayRow(train) {
  const what = `${train.scheduled} to ${str(train.destination) || "its destination"}`;
  return h("tr", {},
    h("th", { scope: "row" }, dayText(train.day)),
    h("td", {}, what),
    h("td", {}, train.booked),
    h("td", {}, train.arrived, train.reported === true ? null : h("span", { class: "sub" }, " estimated")),
    h("td", {}, `${train.minutes_late} min`),
    h("td", {}, train.minutes_late >= 30 ? "30 min or more" : "15 to 29 min"),
    h("td", {}, str(train.operator)));
}

// The section, or nothing when there are no trains to watch.
export function renderRepay(part) {
  const repay = obj(part);
  const section = $("perf-repay");
  section.hidden = !repay || repay.off === "no_trains";
  if (section.hidden) return;
  const body = $("perf-repay-body");
  if (repay.off === "no_filter" || !str(repay.station)) {
    fill(body, h("p", {}, "Delay Repay needs to know where your journeys end. Set ",
      h("strong", {}, "Only trains calling at"), " on the Trains and buses settings page, and the dashboard "
      + "starts noting when your trains get there."));
    return;
  }
  const where = str(repay.station_name) || str(repay.station);
  const trains = list(repay.trains).map(repayTrain).filter(Boolean).slice(0, MAX_REPAY);
  if (!trains.length) {
    fill(body, h("p", {}, `No train reached ${where} 15 or more minutes late in the last 28 days.`));
    return;
  }
  const intro = h("p", {}, `Trains that reached ${where} 15 or more minutes late in the last 28 days. `
    + "Claim from the company you travelled with: ",
    h("a", { href: COMPENSATION, rel: "noopener noreferrer" }, "National Rail's compensation page"),
    " links to each one's Delay Repay.");
  const caption = `Trains 15 or more minutes late at ${where}`;
  fill(body, intro, scrolling(h("table", { class: "history measures repay" },
    h("caption", { class: "vh" }, caption),
    h("thead", {}, h("tr", {}, ["Date", "Train", "Booked", "Arrived", "Late", "Delay Repay", "Operator"]
      .map((name) => h("th", { scope: "col" }, name)))),
    h("tbody", {}, trains.map(repayRow))), caption));
}

// Whether a mode has anything recorded in the longest span.
const recorded = (part) => list(obj(part.all)?.periods).some((p) => obj(p) && Number(p.days) > 0);

export function renderReport(report, names = {}) {
  const data = obj(report);
  if (!data) {
    setText($("perf-status"), "There's no punctuality history yet. It starts once the dashboard has seen departures leave.");
    return;
  }
  const at = hhmm(data.generated_at);
  setText($("perf-status"), at ? `Figures at ${at}.` : "");
  for (const [mode, key, total] of PARTS) {
    const part = obj(data[key]);
    const section = $(`perf-${key}`);
    section.hidden = !part;
    if (!part) continue;
    fill($(`perf-${key}-body`), recorded(part)
      ? partTables(mode, part, total, names)
      : h("p", {}, `No ${key} recorded in the last 28 days.`));
  }
  const buses = obj(data.buses);
  if (buses) {
    const late = lateMinutes(buses.late_minutes);
    setText($("perf-bus-late"), `${late} minute${late === 1 ? "" : "s"}`);
  }
  renderRepay(data.delay_repay);
  fitTables();
}

// The place for the heading and the window's title, and the stops' names,
// from the snapshot; nothing if it can't be had.
async function context() {
  try {
    const snapshot = obj(await getJSON("/api/snapshot"));
    if (!snapshot) return {};
    const place = placeName(str(snapshot.title), obj(snapshot.trains)?.station);
    return { place: place === "Departures" ? "" : place, names: stopNames(obj(snapshot.buses)?.stops) };
  } catch {
    return {};
  }
}

export async function load() {
  const [found, more] = await Promise.all([
    getJSON("/api/performance").then((data) => ({ data }), () => null),
    context(),
  ]);
  if (more.place) {
    setText($("title"), `Performance at ${more.place}`);
    document.title = `Performance · ${more.place}`;
  }
  if (!found) {
    setText($("perf-status"), "The figures couldn't be loaded. Reload the page to try again.");
    return;
  }
  renderReport(found.data, more.names || {});
}

if (document.getElementById("perf-status")) {
  setUpLook();
  load();
}

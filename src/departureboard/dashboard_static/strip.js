// The little route strip in a bus's departure sheet: the stops before yours,
// yours, and maybe the next, with the bus drawn where it is. The words are
// the real content (role=img with a label); the drawing only reinforces them.
// A train's sheet has its whole journey instead (train.js).

import { h, s, str, num, list, obj, validId } from "./dom.js";
import { age, now, parseAt } from "./time.js";

const W = 200, H = 28, PAD = 8, Y = 18;

// How old a bus report is now, in seconds, or null.
function reportAge(vehicle, generatedMs) {
  const recorded = parseAt(vehicle.recorded_at);
  if (recorded !== null) return Math.max(0, (now() - recorded) / 1000);
  const ageS = num(vehicle.age_s);
  if (ageS === null) return null;
  return ageS + (generatedMs ? Math.max(0, (now() - generatedMs) / 1000) : 0);
}

// The label read out for a bus strip, as it should read right now.
export function stripLabel(row, generatedMs) {
  const vehicle = obj(row.vehicle);
  const strip = obj(row.strip);
  if (!vehicle) return "No live position";
  const where = strip ? str(strip.where) : "";
  const seconds = reportAge(vehicle, generatedMs);
  // Ten-second steps: the label is not live, but it needn't churn either.
  const reported = seconds === null ? "" : `reported ${age(seconds < 90 ? Math.round(seconds / 10) * 10 : seconds)}`;
  return [where || "Live position", reported].filter(Boolean).join(", ");
}

// A bus from the side, drawn at x. Drawing only.
const busGlyph = (x) => s("g", { class: "strip-bus" },
  s("rect", { x: x - 7, y: 2, width: 14, height: 9, rx: 2 }),
  s("path", { d: `M${x - 3} 11 L${x + 3} 11 L${x} 14 Z` }),
);

// The drawing and its words. `at` is the bus's place in stop units, or null
// to draw no bus.
function drawStrip(id, stops, yoursAt, at, label, where) {
  const n = stops.length;
  const yours = Math.min(Math.max(0, yoursAt ?? n - 1), n - 1);
  const x = (i) => PAD + (i * (W - 2 * PAD)) / (n - 1);
  const svg = s("svg", {
    class: "strip", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label, focusable: "false",
    dataset: { strip: "bus", id },
  },
    s("line", { class: "strip-line", x1: x(0), y1: Y, x2: x(n - 1), y2: Y }),
    s("line", { class: "strip-ahead", x1: x(yours), y1: Y, x2: x(n - 1), y2: Y }),
    stops.map((_, i) => s("circle", {
      class: i === yours ? "strip-yours" : i > yours ? "strip-after" : "strip-stop",
      cx: x(i), cy: Y, r: i === yours ? 5 : 3,
    })),
  );
  if (at !== null) svg.append(busGlyph(x(Math.min(Math.max(at, 0), n - 1))));
  // The words also show beside the drawing; the img label already says them.
  return h("span", { class: "strip-wrap" }, svg,
    where ? h("span", { class: "strip-where", "aria-hidden": "true" }, where) : null);
}

// The strip for one bus row.
export function renderStrip(row, generatedMs) {
  const strip = obj(row.strip);
  const stops = strip ? list(strip.stops).map(str) : [];
  const label = stripLabel(row, generatedMs);
  if (!strip || stops.length < 2) {
    return h("span", { class: "strip-none" }, label);
  }
  const busAt = obj(row.vehicle) ? num(strip.bus_at) : null;
  return drawStrip(validId(row.id) ? row.id : "", stops, num(strip.yours), busAt, label, str(strip.where));
}

// Keeps the "reported N s ago" label on a bus strip current between snapshots.
export function tickStrips(rowsById, generatedMs) {
  for (const svg of document.querySelectorAll('svg[data-strip="bus"]')) {
    const row = rowsById.get(svg.dataset.id);
    if (!row) continue;
    const label = stripLabel(row, generatedMs);
    if (svg.getAttribute("aria-label") !== label) svg.setAttribute("aria-label", label);
  }
}

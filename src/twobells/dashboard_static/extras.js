// Row extras (docs/dashboard-api.md, "Row extras"): when a train or bus gets
// where it's going, the bus coming in to work a departure, and a calm warning
// for a bus due soon with no tracker. All feed text goes in as text; the
// words carry the meaning and colour only reinforces them.

import { h, vh, str, num, obj } from "./dom.js";
import { icon } from "./icons.js";

const lowerFirst = (text) => text.charAt(0).toLowerCase() + text.slice(1);

function arrival(row) {
  const a = obj(row.arrives);
  const at = a ? str(a.at) : "";
  if (!at) return null;
  const name = str(a.name);
  return {
    at,
    // The place is only worth saying when it isn't the destination already shown.
    name: name && name !== str(row.destination) ? name : "",
    fullName: name || str(row.destination),
    minutes: num(a.minutes),
  };
}

// "arr 23:31 · 32 min" under the destination; read out in full words.
export function arrivesLine(row) {
  const a = arrival(row);
  if (!a) return null;
  const shown = `arr ${a.at}${a.name ? ` at ${a.name}` : ""}${a.minutes !== null ? ` · ${a.minutes} min` : ""}`;
  return h("span", { class: "sub arrives" }, h("span", { "aria-hidden": "true" }, shown), vh(arrivesWords(row)));
}

// "Arrives Manchester Piccadilly 23:31, a 32 minute journey."
function arrivesWords(row) {
  const a = arrival(row);
  if (!a) return "";
  const journey = a.minutes !== null ? `, a ${a.minutes} minute journey` : "";
  return `Arrives ${a.fullName ? `${a.fullName} ` : ""}${a.at}${journey}.`;
}

// For the dialogs' facts: "23:31 at Manchester Piccadilly (32 min journey)".
export function arrivesFact(row) {
  const a = arrival(row);
  if (!a) return "";
  return `${a.at}${a.fullName ? ` at ${a.fullName}` : ""}${a.minutes !== null ? ` (${a.minutes} min journey)` : ""}`;
}

// "Bus arriving from Ashton, 4 min late (due 23:29), 3 stops away": the
// inbound bus's own facts, never a new time for this departure.
export function inboundText(row) {
  const inbound = obj(row.inbound);
  const text = inbound ? str(inbound.text) : "";
  if (!text) return "";
  const where = str(inbound.where);
  return where ? `${text}, ${lowerFirst(where)}` : text;
}

export function inboundLine(row) {
  const text = inboundText(row);
  if (!text) return null;
  return h("span", { class: "reason inbound" }, h("span", { class: "glyph", "aria-hidden": "true" }, icon("corner-down-right")), " ", text);
}

// The ghost warning: words plus an icon, calm rather than alarming.
export function ghostBadge(row) {
  if (row.ghost !== true) return null;
  return h("span", { class: ["badge", "ghost"] },
    h("span", { class: "glyph", "aria-hidden": "true" }, icon("circle-dashed")),
    h("span", { class: "badge-text" }, "May not be running: no tracker yet"));
}

export const noShowPhrase = (row) => str(obj(row.history)?.no_show_phrase);

// What the status cell adds for a bus: the ghost warning with how often
// that has happened, and the bus coming in to work it.
export function busStatusExtras(row) {
  const noShow = row.ghost === true ? noShowPhrase(row) : "";
  return [
    ghostBadge(row),
    noShow ? h("span", { class: "reason" }, noShow) : null,
    inboundLine(row),
  ];
}

// One term and value for the departure sheet's facts list, or nothing when
// there's no value.
export function fact(term, value) {
  return value ? [h("dt", {}, term), h("dd", {}, value)] : [];
}

// The punctuality history dialog: the phrase, then a table of days.

import { $, h, vh, fill, setText, str, num, list, obj, validId, openDialog, setUpDialog } from "./dom.js";
import { getJSON } from "./api.js";
import { statusBadge } from "./board.js";

const WORDS = { on_time: "On time", late: "Late", cancelled: "Cancelled", unknown: "Unknown" };
const DAY_TYPES = { weekday: "weekdays", saturday: "Saturdays", sunday: "Sundays and bank holidays" };
const dateFormat = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

const dialog = () => $("history-dialog");
let request = 0;

export function setUpHistory() {
  setUpDialog(dialog(), () => { request++; });
}

function dayText(day) {
  const m = /^(\d{4})-(\d\d)-(\d\d)$/.exec(str(day));
  return m ? dateFormat.format(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))) : str(day);
}

function dayRow(d) {
  const delay = num(d.delay_min);
  return h("tr", {},
    h("th", { scope: "row" }, dayText(d.day)),
    h("td", {}, statusBadge(d.status, WORDS[d.status] || "Unknown")),
    h("td", {}, delay === null ? [h("span", { "aria-hidden": "true" }, "–"), vh("Not known")] : `${delay} min`),
    h("td", {}, d.live === true ? "Live" : "Timetable"),
    h("td", {}, str(d.reason)));
}

function render(data) {
  const body = $("history-body");
  if (!data) {
    body.replaceChildren(h("p", {}, "There's no history for this departure."));
    return;
  }
  setText($("history-title"), str(data.label) || "Punctuality");
  const days = list(data.days).filter(obj).slice(0, 90);
  const dayType = DAY_TYPES[data.day_type] || "days";
  fill(body,
    h("p", { class: "phrase" }, str(data.phrase) || "Not enough days recorded yet."),
    days.length
      ? h("div", { class: "scroll" }, h("table", { class: "history" },
        h("caption", {}, `The last ${days.length} ${dayType}, newest first`),
        h("thead", {}, h("tr", {},
          ["Day", "Status", "Delay", "Source", "Reason"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, days.map(dayRow))))
      : h("p", {}, `Nothing recorded on ${dayType} yet.`),
  );
}

export async function openHistory(id, opener) {
  if (!validId(id)) return;
  const mine = ++request;
  setText($("history-title"), "Punctuality");
  $("history-body").replaceChildren(h("p", {}, "Loading…"));
  openDialog(dialog(), opener);
  try {
    const data = await getJSON(`/api/history/${encodeURIComponent(id)}`);
    if (mine === request) render(obj(data));
  } catch {
    if (mine === request) $("history-body").replaceChildren(h("p", {}, "The history couldn't be loaded. Try again in a moment."));
  }
}

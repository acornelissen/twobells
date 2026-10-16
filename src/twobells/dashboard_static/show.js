// What the page shows of what it can leave out: the settings' [dashboard]
// show, sent as the snapshot's `show`, the items it names (settings/show.py).
// applyShow marks the page with what is left out (<html data-hide="weather
// coaches">), and each module asks shows() before it draws an item: one
// left out is not drawn at all, and leaves no gap. A snapshot without
// `show`, from an older server, shows everything. The departures' times
// always show.

import { list } from "./dom.js";

// Every item the page can leave out, as the settings name them.
export const ITEMS = [
  "next_to_catch", "where", "disruptions", "weather", "last_buses", "area_map", "remind", "calling_at",
  "coaches", "punctuality", "performance", "fleet_numbers", "announce",
];

// Marks `root` with what the snapshot leaves out. Call it before drawing.
export function applyShow(root, snapshot) {
  const shown = Array.isArray(snapshot?.show) ? list(snapshot.show) : ITEMS;
  const hidden = ITEMS.filter((item) => !shown.includes(item));
  if (hidden.length) root.dataset.hide = hidden.join(" ");
  else delete root.dataset.hide;
}

// Whether the page shows `item`.
export const shows = (item) => !(document.documentElement?.dataset.hide || "").split(" ").includes(item);

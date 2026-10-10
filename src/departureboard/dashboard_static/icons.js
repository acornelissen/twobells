// Icons: one set, drawn on a 24-unit grid with a 2-unit round stroke in the
// text colour, built as SVG nodes so the page needs no icon font or extra
// request. Always decoration beside words that say the same thing, so every
// icon is aria-hidden. Sizes come from CSS (.icon, 1em by default).
//
// The shapes are Lucide's (lucide-static 0.460.0), under the ISC licence:
// Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as
// part of Feather (MIT). All other copyright (c) for Lucide are held by
// Lucide Contributors 2022. Permission to use, copy, modify, and/or distribute
// this software for any purpose with or without fee is hereby granted,
// provided that the above copyright notice and this permission notice appear
// in all copies. THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
// WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
// MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
// SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
// WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION
// OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
// CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import { s } from "./dom.js";

const SHAPES = {
  "arrow-down": [["path", { d: "M12 5v14" }], ["path", { d: "m19 12-7 7-7-7" }]],
  "arrow-up": [["path", { d: "m5 12 7-7 7 7" }], ["path", { d: "M12 19V5" }]],
  "bell": [["path", { d: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" }], ["path", { d: "M10.3 21a1.94 1.94 0 0 0 3.4 0" }]],
  "bell-ring": [["path", { d: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" }], ["path", { d: "M10.3 21a1.94 1.94 0 0 0 3.4 0" }], ["path", { d: "M4 2C2.8 3.7 2 5.7 2 8" }], ["path", { d: "M22 8c0-2.3-.8-4.3-2-6" }]],
  "check": [["path", { d: "M20 6 9 17l-5-5" }]],
  "chevron-down": [["path", { d: "m6 9 6 6 6-6" }]],
  "circle": [["circle", { cx: "12", cy: "12", r: "10" }]],
  "circle-dashed": [["path", { d: "M10.1 2.182a10 10 0 0 1 3.8 0" }], ["path", { d: "M13.9 21.818a10 10 0 0 1-3.8 0" }], ["path", { d: "M17.609 3.721a10 10 0 0 1 2.69 2.7" }], ["path", { d: "M2.182 13.9a10 10 0 0 1 0-3.8" }], ["path", { d: "M20.279 17.609a10 10 0 0 1-2.7 2.69" }], ["path", { d: "M21.818 10.1a10 10 0 0 1 0 3.8" }], ["path", { d: "M3.721 6.391a10 10 0 0 1 2.7-2.69" }], ["path", { d: "M6.391 20.279a10 10 0 0 1-2.69-2.7" }]],
  "clock": [["circle", { cx: "12", cy: "12", r: "10" }], ["polyline", { points: "12 6 12 12 16 14" }]],
  "cloud": [["path", { d: "M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" }]],
  "cloud-drizzle": [["path", { d: "M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242" }], ["path", { d: "M8 19v1" }], ["path", { d: "M8 14v1" }], ["path", { d: "M16 19v1" }], ["path", { d: "M16 14v1" }], ["path", { d: "M12 21v1" }], ["path", { d: "M12 16v1" }]],
  "cloud-fog": [["path", { d: "M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242" }], ["path", { d: "M16 17H7" }], ["path", { d: "M17 21H9" }]],
  "cloud-lightning": [["path", { d: "M6 16.326A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 .5 8.973" }], ["path", { d: "m13 12-3 5h4l-3 5" }]],
  "cloud-moon": [["path", { d: "M10.188 8.5A6 6 0 0 1 16 4a1 1 0 0 0 6 6 6 6 0 0 1-3 5.197" }], ["path", { d: "M13 16a3 3 0 1 1 0 6H7a5 5 0 1 1 4.9-6Z" }]],
  "cloud-rain": [["path", { d: "M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242" }], ["path", { d: "M16 14v6" }], ["path", { d: "M8 14v6" }], ["path", { d: "M12 16v6" }]],
  "cloud-snow": [["path", { d: "M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242" }], ["path", { d: "M8 15h.01" }], ["path", { d: "M8 19h.01" }], ["path", { d: "M12 17h.01" }], ["path", { d: "M12 21h.01" }], ["path", { d: "M16 15h.01" }], ["path", { d: "M16 19h.01" }]],
  "cloud-sun": [["path", { d: "M12 2v2" }], ["path", { d: "m4.93 4.93 1.41 1.41" }], ["path", { d: "M20 12h2" }], ["path", { d: "m19.07 4.93-1.41 1.41" }], ["path", { d: "M15.947 12.65a4 4 0 0 0-5.925-4.128" }], ["path", { d: "M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6Z" }]],
  "corner-down-right": [["polyline", { points: "15 10 20 15 15 20" }], ["path", { d: "M4 4v7a4 4 0 0 0 4 4h12" }]],
  "house": [["path", { d: "M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8" }], ["path", { d: "M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" }]],
  "info": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "M12 16v-4" }], ["path", { d: "M12 8h.01" }]],
  "minus": [["path", { d: "M5 12h14" }]],
  "moon": [["path", { d: "M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" }]],
  "octagon-x": [["path", { d: "m15 9-6 6" }], ["path", { d: "M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z" }], ["path", { d: "m9 9 6 6" }]],
  "plus": [["path", { d: "M5 12h14" }], ["path", { d: "M12 5v14" }]],
  "refresh-cw": [["path", { d: "M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }], ["path", { d: "M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" }], ["path", { d: "M8 16H3v5" }]],
  "sun": [["circle", { cx: "12", cy: "12", r: "4" }], ["path", { d: "M12 2v2" }], ["path", { d: "M12 20v2" }], ["path", { d: "m4.93 4.93 1.41 1.41" }], ["path", { d: "m17.66 17.66 1.41 1.41" }], ["path", { d: "M2 12h2" }], ["path", { d: "M20 12h2" }], ["path", { d: "m6.34 17.66-1.41 1.41" }], ["path", { d: "m19.07 4.93-1.41 1.41" }]],
  "triangle-alert": [["path", { d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" }], ["path", { d: "M12 9v4" }], ["path", { d: "M12 17h.01" }]],
  "x": [["path", { d: "M18 6 6 18" }], ["path", { d: "m6 6 12 12" }]],
};

export const ICONS = Object.freeze(Object.keys(SHAPES));

// An icon by name, or null for a name not in the set.
export function icon(name, cls) {
  const shapes = SHAPES[name];
  if (!shapes) return null;
  return s("svg", {
    class: ["icon", `icon-${name}`, cls], viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false",
    fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round",
  }, shapes.map(([tag, attrs]) => s(tag, attrs)));
}

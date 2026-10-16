// The platform clock: our own drawing of the classic British platform clock,
// a white face with black batons for the hours and minutes, black baton
// hands and a red seconds hand, and no maker's name or logo. A wall tablet
// shows it large in its quiet windows (kiosk.js), with the time in words
// beside it; the drawing is decoration, hidden from screen readers.
//
// The hands turn by CSS custom properties on the <svg> (app.css), set
// through the CSSOM, as the CSP forbids style attributes. The seconds hand
// sweeps from one second to the next by a CSS transition; with reduced
// motion app.css turns transitions off, so it ticks instead.

import { s } from "./dom.js";

// The drawing is 200 units square, the face's centre at 100,100: a viewBox
// from 0,0, so the hands' transform-origin is the same in every browser.
const C = 100;
const R = 96;

// The hour, minute and seconds hands, in degrees clockwise from 12. The hour
// and minute hands move on a little every second, as a real clock's do.
export const handAngles = (hour, minute, second) =>
  [(hour % 12) * 30 + minute * 0.5 + second / 120, minute * 6 + second * 0.1, second * 6];

// `angle` brought on from `before` (null: none yet) by the least forward turn,
// so the seconds hand never sweeps backwards: from 354 (59 s) to 0 (0 s) it
// goes on to 360, not back round the face.
export function onwards(before, angle) {
  if (before === null || !Number.isFinite(before)) return angle;
  return before + ((((angle - before) % 360) + 360) % 360);
}

const round = (n) => Math.round(n * 100) / 100;

// A straight bar pointing up from the centre, from `start` to `end` (as
// fractions of the radius; negative is a tail behind the centre), `width` wide.
function bar(start, end, width, attrs) {
  return s("rect", {
    x: round(C - (width * R) / 2), y: round(C - end * R),
    width: round(width * R), height: round((end - start) * R), ...attrs,
  });
}

// The face, with its hands at 12 until setClock() turns them.
export function clockFace() {
  const marks = [];
  for (let mark = 0; mark < 60; mark++) {
    const hour = mark % 5 === 0;
    marks.push(bar(hour ? 0.66 : 0.8, 0.88, hour ? 0.075 : 0.024,
                   { class: "pc-ink", transform: `rotate(${mark * 6} ${C} ${C})` }));
  }
  return s("svg", { class: "platform-clock", viewBox: "0 0 200 200", "aria-hidden": "true", focusable: "false" },
    s("circle", { class: "pc-face", cx: C, cy: C, r: R }),
    s("circle", { class: "pc-rim", cx: C, cy: C, r: round(R * 0.955), fill: "none", "stroke-width": round(R * 0.022) }),
    marks,
    s("g", { class: "pc-hand pc-hour" }, bar(-0.14, 0.54, 0.095, { class: "pc-ink" })),
    s("g", { class: "pc-hand pc-minute" }, bar(-0.16, 0.82, 0.068, { class: "pc-ink" })),
    s("g", { class: "pc-hand pc-second" },
      bar(-0.24, 0.86, 0.022, { class: "pc-red" }),
      s("circle", { class: "pc-red", cx: C, cy: C, r: round(R * 0.055) })));
}

// Turns a face's hands to the time. The seconds hand's angle only ever
// grows (onwards()), so its sweep is always forwards.
export function setClock(svg, hour, minute, second) {
  const [h, m, sec] = handAngles(hour, minute, second);
  const before = svg.dataset.second === undefined ? null : Number(svg.dataset.second);
  const sweep = onwards(before, sec);
  svg.dataset.second = String(sweep);
  svg.style.setProperty("--pc-hour", `${round(h)}deg`);
  svg.style.setProperty("--pc-minute", `${round(m)}deg`);
  svg.style.setProperty("--pc-second", `${round(sweep)}deg`);
}

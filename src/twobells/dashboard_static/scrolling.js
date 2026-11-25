// Whether the first train's "Calling at" line scrolls: the page is marked
// data-scrolling="off" (app.css then shows two lines, cut short) when the
// settings turned scrolling text off, or when this browser's "Scrolling text"
// button in the footer did, kept under "dashboard.scrolling" as the Look is.
// The button only turns scrolling off for this browser: when the settings
// have it off it is left out, and nothing here turns it back on. WCAG 2.2.2:
// moving text that goes on for more than 5 seconds can be paused.

import { $, store, reduceMotion } from "./dom.js";
import { isKiosk } from "./kiosk.js";

const KEY = "dashboard.scrolling";
// The looks that scroll it (app.css); Standard never does.
const SCROLLING_LOOKS = ["board", "tft", "flap"];

let allowed = true; // the settings let it scroll

const browserOff = () => store.get(KEY) === "off";

function mark(root) {
  if (!allowed || browserOff()) root.dataset.scrolling = "off";
  else delete root.dataset.scrolling;
}

// Marks the page with whether scrolling text is off (the settings or this
// browser). A snapshot from an older server doesn't say: the settings allow it.
export function applyScrolling(root, snapshot) {
  allowed = snapshot?.scrolling !== false;
  mark(root);
}

// The button: shown only where a look scrolls the line, the settings allow it,
// the device hasn't asked for less motion and it isn't a wall tablet.
export function drawScrolling() {
  const button = $("scrolling");
  if (!button) return;
  const useful = allowed && !isKiosk() && !reduceMotion() && SCROLLING_LOOKS.includes(document.documentElement.dataset.theme);
  button.hidden = !useful;
  const on = !browserOff();
  button.setAttribute("aria-pressed", String(on));
  button.querySelector("[data-state]").textContent = on ? "on" : "off";
}

export function setUpScrolling() {
  const button = $("scrolling");
  if (!button) return;
  button.addEventListener("click", () => {
    store.set(KEY, browserOff() ? "on" : "off");
    mark(document.documentElement);
    drawScrolling();
  });
  drawScrolling();
}

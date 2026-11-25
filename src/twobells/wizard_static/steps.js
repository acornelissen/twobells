// Moving between steps. The new step's heading takes focus, so a screen
// reader starts there and keyboard users carry on from the top. Each step is
// a history entry carrying only its name and its position, so the browser's
// Back and Forward move one step.

import { $, h, fill } from "../dashboard_static/dom.js";
import { icon } from "../dashboard_static/icons.js";
import { state } from "./state.js";

const STEPS = ["home", "station", "stops", "trams", "walk", "keys", "views", "show", "finish", "done"];
const TITLES = {
  home: "Home", station: "Station", stops: "Bus stops", trams: "Tram stops", walk: "Walk times",
  keys: "Keys", views: "Views", show: "What to show", finish: "Check and save", done: "Done",
};
// A step as the button going on to it says it: "Next: walk times".
const NEXT = {
  station: "station", stops: "bus stops", trams: "tram stops", walk: "walk times", keys: "keys", views: "views", show: "what to show",
  finish: "check and save",
};
// A shortcut's steps: its own, the walk times, the keys only while the one
// it needs isn't set, and saving.
const SHORTCUTS = {
  station: ["station", "walk", "keys", "finish", "done"],
  stops: ["stops", "walk", "keys", "finish", "done"],
};
const NEEDS = { station: "RDM_API_KEY", stops: "BODS_API_KEY" };

// A Back button's data-back on a shortcut's first step: it leaves for the settings.
export const LEAVE = "settings";

const keySet = (name) => state.keys.some((key) => key.name === name && key.set);

// The steps shown, in order: the tram stops unless their lists can't be
// read (trams.js), the views step only where there is a choice, what to show
// only where the host offers its presets, and on a shortcut only its own.
export function shownSteps() {
  if (state.only) return SHORTCUTS[state.only].filter((step) => step !== "keys" || !keySet(NEEDS[state.only]));
  return STEPS.filter((step) => (step !== "views" || state.askViews) && (step !== "show" || state.askShow)
    && (step !== "trams" || state.askTrams));
}

// Numbers each step's heading and the progress list for the steps shown,
// names the step each Next goes on to, and points each Back at the step
// before. On a shortcut's first step, Back leaves for the settings, or
// isn't there if the host gave nowhere to go.
export function numberSteps() {
  const counted = shownSteps().filter((step) => step !== "done");
  for (const item of $("progress").children) {
    item.hidden = !counted.includes(item.dataset.step);
    // A step's tick, and the word a screen reader says for it, shown once it is done.
    if (!item.querySelector(".icon")) item.append(icon("check"), h("span", { class: "vh" }, " (done)"));
  }
  counted.forEach((step, i) => {
    fill($(`${step}-number`), `Step ${i + 1} of ${counted.length}:`);
    const next = $(`${step}-next`);
    if (next && counted[i + 1]) next.textContent = `Next: ${NEXT[counted[i + 1]]}`;
    const back = $(`step-${step}`).querySelector("[data-back]");
    if (!back) return;
    back.dataset.back = i ? counted[i - 1] : LEAVE;
    back.textContent = i ? "Back" : "Back to settings";
    back.hidden = !i && !state.back;
  });
}

const visited = []; // the steps shown on this page, one for each history entry made, by position
let position = 0; // the position of the step shown: its place in visited, and the n in its entry's state

export function go(step, { focus = true, entry = true } = {}) {
  for (const name of STEPS) $(`step-${name}`).hidden = name !== step;
  const at = STEPS.indexOf(step);
  for (const item of $("progress").children) {
    const index = STEPS.indexOf(item.dataset.step);
    if (index === at) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    item.classList.toggle("done", index < at);
  }
  document.title = `${TITLES[step]}: ${state.title}`;
  if (entry && step !== state.step) {
    // The page as loaded is the first entry; each step moved on to adds one,
    // and drops what was forward of the step it was moved on from.
    if (visited.length) {
      position += 1;
      visited.length = position;
      window.history.pushState({ step, n: position }, "");
    } else {
      window.history.replaceState({ step, n: 0 }, "");
    }
    visited.push(step);
  }
  state.step = step;
  guard(step);
  if (focus) $(`${step}-heading`).focus();
}

// Browser Back and Forward (or a gesture) go to the step the entry names, when
// this page showed that step at that position: the state is input, so the
// name and the position must both match what the page recorded. Forward then
// only returns to a step already passed, never past the checks Next makes, and
// an entry from before a reload, whose choices are gone, matches nothing. After
// saving there is no way back: Back goes on out past the wizard's entries in
// one move. While a save runs, a move puts the entry back, so the wizard stays
// on Finish with its progress.
function popped(event) {
  if (state.saving) {
    const { step } = state;
    window.history.pushState({ step, n: position }, "");
    return;
  }
  const entry = event.state;
  const step = entry && entry.step;
  const n = entry && entry.n;
  if (!Number.isInteger(n) || n < 0 || n >= visited.length || visited[n] !== step) return;
  if (state.step === "done") {
    if (n < position) window.history.go(-(n + 1));
    return;
  }
  if (step === "done" || !shownSteps().includes(step)) return;
  position = n;
  go(step, { entry: false });
}

export function setUpHistory() {
  window.addEventListener("popstate", popped);
}

// The Back button: one entry back where that is the step it names, else a step of its own.
export function stepBack(step) {
  if (position > 0 && visited[position - 1] === step) window.history.back();
  else go(step);
}

// Asks before leaving, as the settings pages do, from the first step past the
// start until the setup is saved: leaving or reloading throws the choices away.
let warning = false;

function ask(event) {
  event.preventDefault();
  event.returnValue = true; // older browsers ask only when this is set
}

function guard(step) {
  const choices = step !== "done" && step !== shownSteps()[0];
  if (choices && !warning) window.addEventListener("beforeunload", ask);
  if (!choices && warning) window.removeEventListener("beforeunload", ask);
  warning = choices;
}

// One action at a time per button: a second press while the first is still
// going does nothing. aria-disabled, not disabled, so focus stays put.
export async function once(button, work) {
  if (button.getAttribute("aria-disabled") === "true") return;
  button.setAttribute("aria-disabled", "true");
  try { await work(); } finally { button.removeAttribute("aria-disabled"); }
}

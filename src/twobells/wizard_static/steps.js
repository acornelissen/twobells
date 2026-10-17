// Moving between steps. The new step's heading takes focus, so a screen
// reader starts there and keyboard users carry on from the top.

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

// The steps shown, in order: the tram stops only where home is near some
// (trams.js), the views step only where there is a choice, what to show
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

export function go(step, { focus = true } = {}) {
  for (const name of STEPS) $(`step-${name}`).hidden = name !== step;
  const at = STEPS.indexOf(step);
  for (const item of $("progress").children) {
    const index = STEPS.indexOf(item.dataset.step);
    if (index === at) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    item.classList.toggle("done", index < at);
  }
  document.title = `${TITLES[step]}: ${state.title}`;
  state.step = step;
  if (focus) $(`${step}-heading`).focus();
}

// One action at a time per button: a second press while the first is still
// going does nothing. aria-disabled, not disabled, so focus stays put.
export async function once(button, work) {
  if (button.getAttribute("aria-disabled") === "true") return;
  button.setAttribute("aria-disabled", "true");
  try { await work(); } finally { button.removeAttribute("aria-disabled"); }
}

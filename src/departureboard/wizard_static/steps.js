// Moving between steps. The new step's heading takes focus, so a screen
// reader starts there and keyboard users carry on from the top.

import { $, h, fill } from "../dashboard_static/dom.js";
import { icon } from "../dashboard_static/icons.js";
import { state } from "./state.js";

const STEPS = ["home", "station", "stops", "walk", "keys", "views", "finish", "done"];
const TITLES = {
  home: "Home", station: "Station", stops: "Bus stops", walk: "Walk times",
  keys: "Keys", views: "Views", finish: "Check and save", done: "Done",
};

// The steps shown, in order: the views step only where there is a choice.
const shownSteps = () => STEPS.filter((step) => step !== "views" || state.askViews);

// Numbers each step's heading and the progress list for the steps shown,
// and points the buttons either side of the views step past it if it isn't.
export function numberSteps() {
  const counted = shownSteps().filter((step) => step !== "done");
  for (const item of $("progress").children) {
    item.hidden = !counted.includes(item.dataset.step);
    // A step's tick, and the word a screen reader says for it, shown once it is done.
    if (!item.querySelector(".icon")) item.append(icon("check"), h("span", { class: "vh" }, " (done)"));
  }
  counted.forEach((step, i) => fill($(`${step}-number`), `Step ${i + 1} of ${counted.length}:`));
  $("keys-next").textContent = state.askViews ? "Next: views" : "Next: check and save";
  $("finish-back").dataset.back = state.askViews ? "views" : "keys";
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

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
const made = []; // the same, but kept when an edit makes later steps stale: the entries the browser really has
let fixing = false; // a go() putting the browser back on the page's entry is on its way
let here = 0; // the n of the entry the browser is on, as its last move said: not position while an undo is on its way
let position = 0; // the position of the step shown: its place in visited, and the n in its entry's state
let held = null; // a step moved on to while an undo going back is on its way, with its focus: shown once it lands
let holding = 0; // the timer that shows it anyway if the browser drops the undo
const WAIT = 500; // ms

// What the person has chosen as it stands: the step's own fields and the
// answers kept in state, so a change by a map pick or a button shows as well
// as one typed. Compared when the step is left by the browser, to tell
// whether the steps after it still follow from what it says.
const ANSWERS = ["home", "station", "stationChoice", "filter", "stops", "trams", "labels", "tramsTicked", "showPicked",
  "modesTicked", "walkMinutes", "chosenViews", "railLater"];
let seen = ""; // answers() as the step shown was arrived at

function answers(step) {
  const fields = [...$(`step-${step}`).querySelectorAll("input, select, textarea")]
    .map((el) => [el.id, el.value, el.checked === true]);
  const kept = ANSWERS.map((name) => state[name]);
  return JSON.stringify([fields, kept], (key, value) => {
    if (value instanceof Map || value instanceof Set) return [...value];
    return value;
  });
}

export function go(step, { focus = true, entry = true } = {}) {
  // The browser forward of the step shown, with an undo taking it back: a push
  // now would keep the entries the undo steps back over, and the undo would
  // land on one of them, so the step waits for it. Behind, a push drops the
  // entry the undo goes to, so it goes nowhere and need not be waited for.
  if (entry && step !== state.step && fixing && here > position) {
    if (!held) holding = setTimeout(letGo, WAIT);
    held = { step, focus };
    return;
  }
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
    // and drops what was forward of the entry the browser is really on. A
    // push from behind makes any undo still on its way pointless, so it is
    // forgotten; from forward, it only comes once the undo is given up on.
    if (visited.length) {
      fixing = false;
      position = here + 1;
      here = position;
      visited[position - 1] = made[position - 1]; // from the entry the browser is on, not the step shown
      visited.length = position;
      made.length = position; // the browser drops the entries forward of here too
      window.history.pushState({ step, n: position }, "");
    } else {
      window.history.replaceState({ step, n: 0 }, "");
    }
    visited.push(step);
    made[position] = step;
  }
  state.step = step;
  seen = answers(step);
  guard(step);
  if (focus) $(`${step}-heading`).focus();
}

// Shows the step held back, once the undo has landed or been waited for long enough.
function letGo() {
  clearTimeout(holding);
  const move = held;
  held = null;
  fixing = false;
  if (move) go(move.step, { focus: move.focus });
}

// Browser Back and Forward (or a gesture) go to the step the entry names, when
// this page showed that step at that position: the state is input, so the
// name and the position must both match what the page recorded. Forward then
// only returns to a step already passed, never past the checks Next makes, and
// an entry from before a reload, whose choices are gone, matches nothing. After
// saving there is no way back: Back goes on out past the wizard's own entries
// in one move.
//
// An entry this page made but will not show (Forward past a changed answer,
// any move while a save runs) is undone with history.go, so the browser stays
// on the entry of the step shown, n entries from where it landed. It is tried
// once: if that lands anywhere but home, the move is left alone. An entry that
// is not this page's own is left alone without moving the history.
function popped(event) {
  const entry = event.state;
  const step = entry && entry.step;
  const n = entry && entry.n;
  const ours = Number.isInteger(n) && n >= 0 && made[n] === step;
  if (ours) here = n;
  const undone = fixing;
  fixing = false;
  if (undone) return letGo();
  if (!ours || n === position) return;
  const undo = () => {
    fixing = true;
    window.history.go(position - n);
  };
  if (state.saving) return undo();
  // Choices changed on this step: the steps after it were made for the old
  // ones, so their entries match nothing and Forward cannot reach them.
  const now = answers(state.step);
  if (now !== seen) {
    visited.length = position + 1;
    seen = now;
  }
  if (state.step === "done") {
    if (n < position) window.history.go(-(n + 1));
    return;
  }
  if (n >= visited.length || visited[n] !== step || step === "done" || !shownSteps().includes(step)) return undo();
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

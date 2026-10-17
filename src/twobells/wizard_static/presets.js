// Step 7, where the host offers presets: what each view on shows. First,
// where setup is setting up more than one mode of transport (a station and
// bus stops), which of them each view shows: a tickbox each, no more ticked
// than the view has room for, with the reason in words. Then a group of
// radios each, Just the times, The usual and Everything, with what each
// shows there; and first, where begin says so, Keep what it shows now (a
// list that is no preset's, or an install set up before the lists). The
// full checklists are on each view's settings page.

import { $, h, fill, own } from "../dashboard_static/dom.js";
import { enterFinish } from "./finish.js";
import { refuse, setError } from "./say.js";
import { pickedModes, state } from "./state.js";
import { go } from "./steps.js";

export const KEEP = "keep";

// A view's settings page, as its tab is named: the view's name, capitalised.
const page = (view) => view.name.charAt(0).toUpperCase() + view.name.slice(1);
const lower = (text) => text.charAt(0).toLowerCase() + text.slice(1);

// The views asked about: those being turned on (or on now, where there was
// no choice), each that the host offers presets for, in the order offered.
export function showViews() {
  const on = state.chosenViews.length ? state.chosenViews : state.viewsOn;
  return state.views.filter((view) => on.includes(view.name) && own(state.show.views, view.name));
}

// The views asked which modes they show: those asked what they show, while
// there is more than one mode to choose from, each that begin says the room of.
export function modeViews() {
  if (pickedModes().length < 2) return [];
  return showViews().filter((view) => own(state.modes.views, view.name));
}

// The modes ticked to start with: as ticked before, else those the view's
// list names of the modes picked, else every mode picked, up to its room.
function startingModes(view) {
  const picked = pickedModes().map((mode) => mode.name);
  const before = (own(state.modesTicked, view.name) || []).filter((name) => picked.includes(name));
  if (before.length) return before;
  const now = (state.modes.views[view.name].now || []).filter((name) => picked.includes(name));
  const most = state.modes.views[view.name].most;
  return (now.length ? now : picked).slice(0, most || picked.length);
}

function modesGroup(view) {
  const ticked = startingModes(view);
  const name = `modes-${view.name}`;
  const { says } = state.modes.views[view.name];
  const boxes = pickedModes().map((mode) => {
    const id = `${name}-${mode.name}`;
    const box = h("input", { type: "checkbox", id, value: mode.name, checked: ticked.includes(mode.name) });
    box.addEventListener("change", () => setError(name, "", firstBox(view)));
    return h("div", { class: "option" }, box, h("label", { for: id }, mode.label));
  });
  return h("fieldset", { id: name, "aria-describedby": `${says ? `${name}-hint ` : ""}${name}-error` },
    h("legend", {}, `Modes of transport on ${lower(view.label)}`),
    says ? h("p", { class: "hint", id: `${name}-hint` }, says) : null,
    h("p", { class: "error", id: `${name}-error`, hidden: true }),
    h("div", { class: "options" }, boxes));
}

const firstBox = (view) => $(`modes-${view.name}-${pickedModes()[0].name}`);

// The names a view's radios can have, Keep first where it is offered.
function choices(view) {
  const presets = state.show.presets.map((preset) => preset.name);
  return state.show.views[view.name].now === KEEP ? [KEEP, ...presets] : presets;
}

function option(view, name, picked) {
  const id = `show-${view.name}-${name}`;
  const preset = state.show.presets.find((p) => p.name === name);
  const label = preset ? preset.label : "Keep what it shows now";
  const says = preset ? state.show.views[view.name].says[name]
    : `Change it item by item on the settings' ${page(view)} page.`;
  const radio = h("input", {
    type: "radio", name: `show-${view.name}`, id, value: name, checked: name === picked,
    "aria-describedby": says ? `${id}-hint` : null,
  });
  radio.addEventListener("change", () => { state.showPicked[view.name] = name; });
  return h("div", { class: "option" }, radio,
    h("div", { class: "option-text" },
      h("label", { for: id }, label),
      says ? h("p", { class: "hint", id: `${id}-hint` }, says) : null));
}

// Drawn each time the step opens, as the views on and the modes picked may
// have changed; what was picked for a view stays picked.
function drawShow() {
  const views = showViews();
  const asked = modeViews().map((view) => view.name);
  fill($("show-list"), views.map((view) => {
    const picked = own(state.showPicked, view.name) || state.show.views[view.name].now;
    return [
      asked.includes(view.name) ? modesGroup(view) : null,
      h("fieldset", { id: `show-${view.name}` },
        h("legend", {}, `What ${lower(view.label)} shows`),
        h("div", { class: "options" }, choices(view).map((name) => option(view, name, picked)))),
    ];
  }));
  const pages = views.map(page);
  fill($("show-pages"), `the settings' ${pages.join(" and ")} ${pages.length === 1 ? "page" : "pages"}`);
}

export function enterShow() {
  go("show");
  drawShow();
}

// The modes ticked for each view asked, checked; false (and the first
// problem said and focused) unless each has at least one, and no more than
// it has room for.
function leaveModes() {
  const chosen = {};
  for (const view of modeViews()) {
    const ticked = pickedModes().map((mode) => mode.name)
      .filter((name) => $(`modes-${view.name}-${name}`).checked);
    state.modesTicked[view.name] = ticked;
    const { most, says } = state.modes.views[view.name];
    const name = `modes-${view.name}`;
    setError(name, "", firstBox(view));
    if (!ticked.length) {
      return refuse(name, `Tick at least one, or no departures show on ${lower(view.label)}.`, firstBox(view));
    }
    if (most && ticked.length > most) {
      return refuse(name, `${says} Untick ${ticked.length - most}.`, firstBox(view));
    }
    chosen[view.name] = ticked;
  }
  state.chosenModes = chosen;
  return true;
}

// The preset picked for each view, to save; a view kept as it is is left out.
export function leaveShow() {
  if (!leaveModes()) return false;
  const chosen = {};
  for (const view of showViews()) {
    const picked = choices(view).find((name) => $(`show-${view.name}-${name}`).checked)
      || state.show.views[view.name].now;
    state.showPicked[view.name] = picked;
    if (picked !== KEEP) chosen[view.name] = picked;
  }
  state.chosenShow = chosen;
  enterFinish();
  return true;
}

// Step 7, where the host offers presets: what each view on shows. A group of
// radios each, Just the times, The usual and Everything, with what each
// shows there; and first, where begin says so, Keep what it shows now (a
// list that is no preset's, or an install set up before the lists). The
// full checklists are on each view's settings page.

import { $, h, fill, own } from "../dashboard_static/dom.js";
import { enterFinish } from "./finish.js";
import { state } from "./state.js";
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

// Drawn each time the step opens, as the views on may have changed; what
// was picked for a view stays picked.
function drawShow() {
  const views = showViews();
  fill($("show-list"), views.map((view) => {
    const picked = own(state.showPicked, view.name) || state.show.views[view.name].now;
    return h("fieldset", { id: `show-${view.name}` },
      h("legend", {}, `What ${lower(view.label)} shows`),
      h("div", { class: "options" }, choices(view).map((name) => option(view, name, picked))));
  }));
  const pages = views.map(page);
  fill($("show-pages"), `the settings' ${pages.join(" and ")} ${pages.length === 1 ? "page" : "pages"}`);
}

export function enterShow() {
  go("show");
  drawShow();
}

// The preset picked for each view, to save; a view kept as it is is left out.
export function leaveShow() {
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

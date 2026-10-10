// Step 6, where the install offers more than one view: which to turn on.
// Those on now are ticked; with none on, the first offered.

import { $, h, fill } from "../dashboard_static/dom.js";
import { enterFinish } from "./finish.js";
import { refuse, setError } from "./say.js";
import { state } from "./state.js";
import { go } from "./steps.js";

function drawViews() {
  const ticked = state.viewsOn.length ? state.viewsOn : [state.views[0].name];
  fill($("views-list"), state.views.map((view) => {
    const id = `view-${view.name}`;
    const checkbox = h("input", {
      type: "checkbox", id, value: view.name, checked: ticked.includes(view.name),
      "aria-describedby": view.hint ? `${id}-hint views-error` : "views-error",
    });
    checkbox.addEventListener("change", () => setError("views", "", firstView()));
    return h("div", { class: "option" }, checkbox,
      h("div", { class: "option-text" },
        h("label", { for: id }, view.label),
        view.hint ? h("p", { class: "hint", id: `${id}-hint` }, view.hint) : null));
  }));
  state.viewsDrawn = true;
}

const firstView = () => $(`view-${state.views[0].name}`);

export function enterViews() {
  go("views");
  if (!state.viewsDrawn) drawViews();
}

export function leaveViews() {
  const chosen = state.views.filter((view) => $(`view-${view.name}`).checked).map((view) => view.name);
  setError("views", "", firstView());
  if (!chosen.length) {
    return refuse("views", "Tick at least one view, or the departures show nowhere.", firstView());
  }
  state.chosenViews = chosen;
  enterFinish();
  return true;
}

// The first-run setup wizard: from home to saved settings in six steps, or
// seven where the install offers more than one view to turn on.
//
// It talks only to its host's setup API (setup/api.py), by relative URLs, so
// the page works under whatever prefix the host mounts it at. Every step
// works without a map: maps are opt-in (their pictures come from
// OpenStreetMap, which then sees roughly where home is), load Leaflet only
// when first opened, and show what the lists already say. Text from the
// server goes in as text through h() and fill(), never as HTML, and Leaflet
// gets DOM nodes, never strings.
//
// This is the entry point. Each step is its own module (home.js to
// finish.js, and roads.js for the wait after the search), beside the
// helpers they share: api.js, say.js, state.js, steps.js and maps.js.

import { $, fill, str, list, obj } from "../dashboard_static/dom.js";
import { call, signIn } from "./api.js";
import { setUpFinish } from "./finish.js";
import { setUpHome } from "./home.js";
import { leaveKeys } from "./keys.js";
import { note } from "./say.js";
import { state } from "./state.js";
import { setUpStation } from "./station.js";
import { go, numberSteps } from "./steps.js";
import { setUpStops } from "./stops.js";
import { leaveViews } from "./views.js";
import { leaveWalk } from "./walk.js";

const NOUN = /^[a-z]{1,20}$/;
const VIEW = /^[a-z]{1,20}$/;

// Start.

function setUpSteps() {
  for (const button of document.querySelectorAll("[data-back]")) {
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-disabled") === "true") return;
      go(button.dataset.back);
    });
  }
  setUpHome();
  setUpStation();
  setUpStops();
  $("walk-next").addEventListener("click", leaveWalk);
  $("keys-next").addEventListener("click", leaveKeys);
  $("views-next").addEventListener("click", leaveViews);
  setUpFinish();
}

async function begin() {
  try {
    await signIn();
    const data = await call("begin");
    state.title = str(data.title).slice(0, 60) || state.title;
    fill($("title"), state.title);
    if (NOUN.test(str(data.noun))) state.noun = data.noun;
    for (const el of document.querySelectorAll("[data-noun]")) el.textContent = state.noun;
    state.keys = list(data.keys).map(obj).filter((k) => k && /^[A-Z_]{1,64}$/.test(str(k.name)))
      .map((k) => ({ name: k.name, label: str(k.label) || k.name, hint: str(k.hint), set: k.set === true, doc: str(k.doc) }));
    state.links = list(data.links);
    const views = obj(data.views) || {};
    state.views = list(views.offered).map(obj).filter((v) => v && VIEW.test(str(v.name)))
      .map((v) => ({ name: v.name, label: str(v.label) || v.name, hint: str(v.hint) }));
    state.viewsOn = list(views.on).map(str).filter((name) => state.views.some((v) => v.name === name));
    // One view offered: nothing to ask, so it is sent as chosen. None (an
    // older host): no views are sent, and the host keeps those it has.
    state.askViews = state.views.length > 1;
    state.chosenViews = state.views.length === 1 ? [state.views[0].name] : [];
  } catch (error) {
    note("loading", error.message);
    return;
  }
  note("loading", "");
  setUpSteps();
  numberSteps();
  go("home", { focus: false });
}

begin();

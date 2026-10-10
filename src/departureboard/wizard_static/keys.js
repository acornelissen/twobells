// Step 5: keys. Never shown, only whether each is set; a pasted one is sent
// when saving (and the bus key for the search, if pasted), and to test it.

import { $, h, fill, str } from "../dashboard_static/dom.js";
import { call } from "./api.js";
import { enterFinish, pastedKeys } from "./finish.js";
import { note, refuse, setError } from "./say.js";
import { state } from "./state.js";
import { go, once } from "./steps.js";
import { enterViews } from "./views.js";

const KEY = /^[A-Za-z0-9._~+/=-]{1,200}$/;
const NEEDED = {
  RDM_API_KEY: () => state.station !== null ? "Needed for trains." : "",
  BODS_API_KEY: () => state.chosen.length ? "Needed for buses." : "",
};

// The short way to get each key, from docs/keys.md; its link has the full guide.
const SIGN_UP = {
  RDM_API_KEY: [
    "Register at raildata.org.uk as an individual not affiliated with a company. Approval can take up to 3 working days.",
    "In the Data product catalogue, subscribe (free) to Live Departure Board. Not Live Fastest Departure Boards, or anything with Staff in its name.",
    "Open Subscriptions, then Live Departure Board, then the Specification tab, and copy the Consumer key. Not the Consumer secret.",
  ],
  BODS_API_KEY: [
    "Create an account at data.bus-data.dft.gov.uk. For intended use, pick Personal interests or other.",
    "If BODS emails you to confirm your address, follow its link.",
    "Sign in, open Account settings and copy the API key.",
  ],
};

// "Test this key": the host makes one harmless call to the key's service.
// The wizard offers only the keys every view uses; each view's own are
// signed up for and tested on its settings tab.
const TESTED = ["RDM_API_KEY", "BODS_API_KEY"];
const RESULTS = { // state: [glyph, words]; the colour only repeats them
  testing: ["…", "Testing"],
  works: ["✓", "Works"],
  wrong_key: ["✗", "Wrong key"],
  not_subscribed: ["✗", "Not subscribed"],
  service_down: ["!", "Service not answering"],
  missing: ["!", "No key"],
  unknown: ["?", "Can't tell"],
  problem: ["!", "Not tested"],
};

function showResult(region, result, text) {
  const [glyph, words] = RESULTS[result] || RESULTS.unknown;
  region.className = `key-result key-result-${RESULTS[result] ? result : "unknown"}`;
  fill(region, h("span", { class: "key-glyph", "aria-hidden": "true" }, glyph), h("strong", {}, `${words}:`), ` ${text}`);
}

function keySet(name) {
  const key = state.keys.find((k) => k.name === name);
  return Boolean(key && key.set);
}

async function testKey(name, input, button, region) {
  const id = `key-${name}`;
  const body = { name };
  const value = input.value.trim();
  setError(id, "");
  if (value && !KEY.test(value)) return refuse(id, "Paste the key exactly; it has only letters, digits and . _ ~ + / = -");
  if (value) body.value = value;
  if (!value && !keySet(name)) {
    showResult(region, "missing", "Paste the key first, then test it.");
    return false;
  }
  await once(button, async () => {
    showResult(region, "testing", "asking the service…");
    try {
      const data = await call("testkey", body);
      showResult(region, str(data.state), str(data.message) || "No details.");
    } catch (error) {
      if (error.field) setError(`key-${error.field}`, error.message);
      showResult(region, "problem", error.message);
    }
  });
  return true;
}

function keyTestButton(name, input) {
  if (!TESTED.includes(name)) return null;
  const id = `key-${name}`;
  const region = h("p", { class: "key-result", id: `${id}-result`, role: "status", "aria-live": "polite" });
  const button = h("button", { type: "button", class: "quiet", id: `${id}-test` }, "Test this key");
  button.addEventListener("click", () => testKey(name, input, button, region));
  // A result is about what was in the box then; once that changes, it's gone.
  input.addEventListener("input", () => { region.className = "key-result"; fill(region); });
  return h("div", { class: "key-test" }, button, region);
}

function signUp(key) {
  const steps = SIGN_UP[key.name];
  const link = /^https:\/\//.test(key.doc)
    ? h("a", { href: key.doc, target: "_blank", rel: "noopener noreferrer" }, "The full guide (opens in a new tab)")
    : null;
  if (!steps && !link) return null;
  return h("details", { class: "sign-up" },
    h("summary", {}, "How to get this key"),
    steps ? h("ol", {}, steps.map((step) => h("li", {}, step))) : null,
    link ? h("p", {}, link) : null);
}

function drawKeys() {
  fill($("keys-list"), state.keys.map((key) => {
    const id = `key-${key.name}`;
    const input = h("input", {
      id, type: "text", autocomplete: "off", spellcheck: "false", class: "key-input",
      "aria-describedby": `${id}-hint ${id}-needed ${id}-error`,
    });
    return h("div", { class: "field key" },
      h("label", { for: id }, key.label),
      h("p", { class: "hint", id: `${id}-hint` }, `${key.hint} `,
        h("strong", { class: key.set ? "is-set" : "not-set" }, key.set ? "Set." : "Not set.")),
      signUp(key),
      h("p", { class: "needed", id: `${id}-needed` }),
      h("p", { class: "error", id: `${id}-error`, hidden: true }),
      input,
      keyTestButton(key.name, input));
  }));
  state.keysDrawn = true;
}

export function enterKeys() {
  go("keys");
  if (!state.keysDrawn) drawKeys();
  for (const key of state.keys) {
    const needed = NEEDED[key.name] ? NEEDED[key.name]() : "";
    note(`key-${key.name}-needed`, needed && !key.set ? needed : "");
  }
}

export function leaveKeys() {
  const pasted = pastedKeys();
  for (const key of state.keys) {
    const id = `key-${key.name}`;
    setError(id, "");
    if (pasted[key.name] && !KEY.test(pasted[key.name])) {
      return refuse(id, "Paste the key exactly; it has only letters, digits and . _ ~ + / = -");
    }
    const needed = NEEDED[key.name] ? NEEDED[key.name]() : "";
    if (needed && !key.set && !pasted[key.name]) {
      return refuse(id, "Paste this key, or go back and change your choices.");
    }
  }
  if (state.askViews) enterViews();
  else enterFinish();
  return true;
}

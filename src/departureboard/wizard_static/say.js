// Saying things: polite news, and errors at once.

import { $, h, fill } from "../dashboard_static/dom.js";

function announce(id, text) {
  const region = $(id);
  region.textContent = "";
  // Emptied first, so the same words twice are still read out.
  setTimeout(() => { region.textContent = text; }, 50);
}
export const say = (text) => announce("live", text);
export const shout = (text) => announce("alert", text);

export function note(id, text) {
  const el = $(id);
  el.textContent = text;
  el.hidden = !text;
}

// An error beside a control: `name`-error holds it, and the control (if an
// input) is marked invalid. The error's id is already in its aria-describedby.
export function setError(name, text, control = $(name)) {
  const error = $(`${name}-error`);
  if (!error) return;
  fill(error, text ? [h("span", { class: "tag" }, "Error:"), ` ${text}`] : null);
  error.hidden = !text;
  if (control && control.tagName === "INPUT") {
    if (text) control.setAttribute("aria-invalid", "true");
    else control.removeAttribute("aria-invalid");
  }
}

// A problem with what was typed: shown by the control, which takes focus so
// its label and the error are read together.
export function refuse(name, text, control = $(name)) {
  setError(name, text, control);
  if (control) control.focus();
  return false;
}

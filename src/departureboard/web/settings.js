// The settings pages' cues for changes not saved yet (web/settingsite.py
// serves it as /settings/settings.js, to a signed-in visitor's pages only).
//
// Each settings form (form.settings-form, and each key's form.keyform) is
// compared with what the server drew: a box's defaultValue, a tick's
// defaultChecked, a list's defaultSelected; a field the server drew with
// what a refused save sent (data-unsaved) counts as changed. A field changed is marked
// .is-changed with a "Changed" tag in words; a time window's row counts as
// one field. The form's save bar (or a key form's buttons) says "2 unsaved
// changes" in a polite live region, once typing stops. Leaving the page asks
// first while any form has changes, until that form is sent. A time
// window's days get Weekdays, Weekends and Every day buttons.
//
// The pages work as well without it: Save is never disabled. It never
// fetches, stores or logs anything. A key box is read only to tell whether
// it is empty, and no value is ever copied anywhere. Words go in by
// textContent; nothing is parsed as HTML.
"use strict";

(() => {
  const SAY_AFTER_MS = 1000; // the live region waits this long after the last change
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const PRESETS = [
    ["Weekdays", ["mon", "tue", "wed", "thu", "fri"]],
    ["Weekends", ["sat", "sun"]],
    ["Every day", DAYS],
  ];
  const forms = [...document.querySelectorAll("form.settings-form, form.keyform")];
  if (!forms.length) return;
  const sent = new Set(); // forms on their way to the server
  const tags = new Map(); // a field's "Changed" tag and the space before it, while it has one
  const counts = new Map(); // each form's fields changed
  const said = new Map(); // each form's live region
  const timers = new Map(); // each form's wait before saying its count
  let warning = false;

  const kind = (control) => (control.getAttribute("type") || "").toLowerCase();

  function controls(form) {
    return [...form.querySelectorAll("input, select, textarea")]
      .filter((control) => !["hidden", "submit", "button"].includes(kind(control)));
  }

  // Whether a control differs from what the page was drawn with.
  function changed(control) {
    if (control.classList.contains("key-box")) return control.value !== ""; // only whether a key is typed
    const type = kind(control);
    if (type === "checkbox" || type === "radio") return control.checked !== control.defaultChecked;
    if (control.nodeName === "SELECT") {
      const options = [...control.options];
      const drawn = Math.max(0, options.findIndex((option) => option.defaultSelected));
      return control.selectedIndex !== drawn;
    }
    return control.value !== control.defaultValue;
  }

  // The field a control belongs to: its time window's row, else its .field.
  const fieldOf = (control) => control.closest(".window") || control.closest(".field");

  // Whether the server drew the control's field with what a refused save
  // sent (data-unsaved): it differs from what is saved, whatever it is drawn with.
  const refused = (control) => control.closest("[data-unsaved]") !== null;

  function tagged(field, on) {
    field.classList.toggle("is-changed", on);
    const tag = tags.get(field);
    if (!on) {
      if (tag) for (const node of tag) node.remove();
      tags.delete(field);
      return;
    }
    if (tag) return;
    const made = document.createElement("span");
    made.setAttribute("class", "changed-tag");
    made.textContent = "Changed";
    const space = document.createTextNode(" "); // so a label is said "Title Changed", not run together
    const label = field.querySelector("legend, label");
    if (label && !label.classList.contains("vh")) label.append(space, made);
    else if (label) label.after(space, made); // a label shown only to screen readers: the tag still shows
    else field.prepend(space, made);
    tags.set(field, [space, made]);
  }

  // Mark each field of `form` as changed or not, and how many are.
  function mark(form) {
    const fields = new Map();
    for (const control of controls(form)) {
      const field = fieldOf(control);
      if (field) fields.set(field, fields.get(field) || changed(control) || refused(control));
    }
    let count = 0;
    for (const [field, on] of fields) {
      tagged(field, on);
      if (on) count += 1;
    }
    return count;
  }

  function words(count) {
    if (!count) return "";
    return `${count} unsaved change${count === 1 ? "" : "s"}`;
  }

  function say(form) {
    timers.delete(form);
    const region = said.get(form);
    const text = words(counts.get(form) || 0);
    if (region && region.textContent !== text) region.textContent = text;
  }

  function guard() {
    const dirty = forms.some((form) => !sent.has(form) && (counts.get(form) || 0) > 0);
    if (dirty && !warning) window.addEventListener("beforeunload", ask);
    if (!dirty && warning) window.removeEventListener("beforeunload", ask);
    warning = dirty;
  }

  function ask(event) {
    event.preventDefault();
    event.returnValue = true; // older browsers ask only when this is set
  }

  function update(form) {
    counts.set(form, mark(form));
    guard();
    clearTimeout(timers.get(form));
    timers.set(form, setTimeout(() => say(form), SAY_AFTER_MS));
  }

  function presets(row, form) {
    const boxes = row.querySelector(".day-boxes");
    if (!boxes) return;
    const holder = document.createElement("div");
    holder.setAttribute("class", "presets");
    for (const [name, days] of PRESETS) {
      const button = document.createElement("button");
      button.setAttribute("type", "button");
      button.textContent = name;
      button.addEventListener("click", () => {
        for (const box of row.querySelectorAll('input[type="checkbox"]')) {
          const day = DAYS.find((short) => box.id.endsWith(`-${short}`));
          if (day) box.checked = days.includes(day);
        }
        update(form);
      });
      holder.append(button);
    }
    boxes.after(holder);
  }

  for (const form of forms) {
    const bar = form.querySelector(".savebar, .key-actions");
    if (bar) {
      const region = document.createElement("p");
      region.setAttribute("class", "unsaved");
      region.setAttribute("role", "status");
      const first = bar.querySelector("button"); // said just before Save
      if (first) first.before(region);
      else bar.append(region);
      said.set(form, region);
    }
    for (const row of form.querySelectorAll(".window")) presets(row, form);
    form.addEventListener("input", () => update(form));
    form.addEventListener("change", () => update(form));
    form.addEventListener("submit", () => {
      sent.add(form);
      guard();
    });
    counts.set(form, mark(form)); // a browser may have put back what was typed before a reload
    say(form);
  }
  guard();

  // Back to this page from the next one: it is as it was left, and not sent.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    sent.clear();
    for (const form of forms) update(form);
  });
})();

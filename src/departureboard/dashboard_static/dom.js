// DOM helpers. Every piece of feed text goes in through textContent (via text
// nodes here), never parsed as HTML; tests/test_dashboard_static.py bans the HTML sinks.

const SVG_NS = "http://www.w3.org/2000/svg";

export const $ = (id) => document.getElementById(id);

function build(el, attrs, children) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    // Inline styles and handler attributes would break the CSP and invite
    // injection; styles go through classes or el.style, handlers through `on`.
    if (key === "style" || (key.startsWith("on") && key !== "on")) throw new Error(`h(): ${key} is not allowed`);
    if (key === "on") {
      for (const [type, fn] of Object.entries(value)) el.addEventListener(type, fn);
    } else if (key === "dataset") {
      Object.assign(el.dataset, value);
    } else if (key === "class") {
      el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// h("td", {class: "x"}, "text", otherNode) builds an HTML element.
export function h(tag, attrs, ...children) {
  return build(document.createElement(tag), attrs, children);
}

// The same for SVG elements.
export function s(tag, attrs, ...children) {
  return build(document.createElementNS(SVG_NS, tag), attrs, children);
}

// Replaces an element's children by h()'s rules: null and false are skipped
// (native replaceChildren would print them as "null"), arrays flattened.
export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// Like fill(), but keeps the nodes already there wherever the new ones line
// up with them (the same kind of node, the same tag, in the same place),
// changing only the text, attributes and inline style that differ. A
// screen reader's place and the focus stay put when little has changed.
// Event listeners are not carried over: use it for content without them.
export function patch(el, ...children) {
  const fresh = document.createElement("div");
  append(fresh, children);
  patchChildren(el, [...fresh.childNodes]);
  return el;
}

function patchChildren(el, wanted) {
  wanted.forEach((node, i) => {
    const old = el.childNodes[i];
    if (!old) {
      el.append(node);
    } else if (old.nodeType !== node.nodeType || old.nodeName !== node.nodeName) {
      old.replaceWith(node);
    } else if (node.nodeType !== Node.ELEMENT_NODE) {
      if (old.nodeValue !== node.nodeValue) old.nodeValue = node.nodeValue;
    } else {
      patchAttributes(old, node);
      patchChildren(old, [...node.childNodes]);
    }
  });
  while (el.childNodes.length > wanted.length) el.lastChild.remove();
}

function patchAttributes(old, node) {
  for (const { name } of [...old.attributes]) {
    if (name !== "style" && !node.hasAttribute(name)) old.removeAttribute(name);
  }
  for (const { name, value } of [...node.attributes]) {
    if (name !== "style" && old.getAttribute(name) !== value) old.setAttribute(name, value);
  }
  // The inline style, one property at a time through the CSSOM, as the CSP
  // forbids style attributes.
  for (const name of Array.from(old.style)) {
    if (!node.style.getPropertyValue(name)) old.style.removeProperty(name);
  }
  for (const name of Array.from(node.style)) {
    const value = node.style.getPropertyValue(name);
    if (old.style.getPropertyValue(name) !== value) old.style.setProperty(name, value);
  }
}

// Visually hidden text for screen readers, such as the row a button acts on.
export const vh = (text) => h("span", { class: "vh" }, text);

// Rewrites text only when it changed, so nothing re-renders for no reason.
export function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

// localStorage can be missing or throw (private windows, blocked storage).
export const store = {
  get(key, fallback = null) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* the page works without it */ }
  },
  getJSON(key, fallback) {
    try { return JSON.parse(this.get(key)) ?? fallback; } catch { return fallback; }
  },
  setJSON(key, value) { this.set(key, JSON.stringify(value)); },
};

// Feed values are untrusted: these coerce them to the type the page needs.
export const str = (v) => (typeof v === "string" ? v : "");
export const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
export const list = (v) => (Array.isArray(v) ? v : []);
export const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);

// Row ids are 16 lowercase hex characters; anything else never reaches a URL.
export const validId = (id) => typeof id === "string" && /^[0-9a-f]{16}$/.test(id);

// Elements that should keep focus across a re-render carry data-focus-key.
export function focusKey() {
  const el = document.activeElement;
  return el && el.dataset ? el.dataset.focusKey || null : null;
}

export function refocus(key) {
  if (!key) return;
  const el = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
  if (el && document.activeElement !== el) el.focus({ preventScroll: true });
}

// Modal dialogs: showModal() for the focus trap and Escape, focus moved to the
// title on open and back to the button that opened it on close (found again by
// its focus key if a new snapshot has re-rendered the table meanwhile).
export function setUpDialog(dialog, onClose) {
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  // A click on the backdrop lands on the dialog element itself.
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    if (onClose) onClose();
    const key = dialog.dataset.returnKey;
    const back = (key && document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`)) || dialog.returnTo;
    dialog.returnTo = null;
    if (back && back.isConnected) back.focus();
    else document.getElementById("main").focus();
  });
}

export function openDialog(dialog, opener) {
  dialog.dataset.returnKey = (opener && opener.dataset && opener.dataset.focusKey) || "";
  dialog.returnTo = opener || null;
  if (!dialog.open) dialog.showModal();
  dialog.querySelector("[data-dialog-title]").focus();
}

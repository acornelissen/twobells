// Just enough of the DOM for the dashboard's modules, the wizard's and the
// settings pages' settings.js to run under node, for their behaviour tests
// (tests/dashboard/test_static.py, tests/setup/test_wizard.py,
// tests/web/test_settings_js.py). Importing it sets the globals (document,
// Node), so import it before the module under test.

class FakeNode {
  constructor(nodeType, nodeName) {
    this.nodeType = nodeType;
    this.nodeName = nodeName;
    this.parentNode = null;
    this.childNodes = [];
  }

  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get isConnected() { return this.parentNode ? this.parentNode.isConnected : this === document.body; }

  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }

  append(...nodes) {
    for (const node of nodes) {
      const child = node instanceof FakeNode ? node : new FakeText(String(node));
      child.remove();
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }

  prepend(...nodes) {
    const rest = [...this.childNodes];
    for (const child of rest) child.remove();
    this.append(...nodes, ...rest);
  }

  // Insert nodes beside this one, before it or after it, in its parent.
  _beside(nodes, after) {
    const parent = this.parentNode;
    const made = nodes.map((node) => (node instanceof FakeNode ? node : new FakeText(String(node))));
    for (const node of made) node.remove();
    const at = parent.childNodes.indexOf(this) + (after ? 1 : 0);
    parent.childNodes.splice(at, 0, ...made);
    for (const node of made) node.parentNode = parent;
  }

  before(...nodes) { this._beside(nodes, false); }
  after(...nodes) { this._beside(nodes, true); }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }

  replaceWith(node) {
    const parent = this.parentNode;
    node.remove();
    parent.childNodes[parent.childNodes.indexOf(this)] = node;
    node.parentNode = parent;
    this.parentNode = null;
  }

  replaceChildren(...nodes) {
    for (const child of [...this.childNodes]) child.remove();
    this.append(...nodes);
  }

  get textContent() {
    return this.nodeType === 3 ? this.nodeValue : this.childNodes.map((c) => c.textContent).join("");
  }

  set textContent(text) { this.replaceChildren(new FakeText(String(text))); }
}

class FakeText extends FakeNode {
  constructor(text) {
    super(3, "#text");
    this.nodeValue = text;
  }
}

// el.style through the CSSOM calls only, listing its property names as the
// real one does when iterated.
class FakeStyle {
  constructor() { this.props = new Map(); }
  setProperty(name, value) { this.props.set(name, String(value)); }
  removeProperty(name) { this.props.delete(name); }
  getPropertyValue(name) { return this.props.get(name) || ""; }
  [Symbol.iterator]() { return this.props.keys(); }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super(1, tag);
    this.attrs = new Map();
    this.dataset = {};
    this.style = new FakeStyle();
    this.listeners = [];
  }

  setAttribute(name, value) {
    if (name === "style") throw new Error("the CSP forbids style attributes");
    this.attrs.set(name, String(value));
  }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  removeAttribute(name) { this.attrs.delete(name); }
  get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
  get id() { return this.getAttribute("id") || ""; }
  get tagName() { return this.nodeName; }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  // A form control's state as a browser keeps it: its attribute (a text
  // area's text) until set, which stays its default.
  get value() { return this._value ?? this.defaultValue; }
  set value(value) { this._value = value; }
  get defaultValue() { return this.nodeName === "TEXTAREA" ? this.textContent : this.getAttribute("value") ?? ""; }
  get checked() { return this._checked ?? this.defaultChecked; }
  set checked(on) { this._checked = Boolean(on); }
  get defaultChecked() { return this.hasAttribute("checked"); }
  get selected() { return this._selected ?? this.defaultSelected; }
  set selected(on) { this._selected = Boolean(on); }
  get defaultSelected() { return this.hasAttribute("selected"); }
  get options() { return this.querySelectorAll("option"); }
  // A list shows its first option when none is selected.
  get selectedIndex() {
    const found = this.options.findIndex((option) => option.selected);
    return found >= 0 ? found : (this.options.length ? 0 : -1);
  }
  set selectedIndex(index) { this.options.forEach((option, at) => { option.selected = at === index; }); }
  closest(selector) {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) if (matches(node, selector)) return node;
    return null;
  }
  get validity() { return { badInput: false }; }
  addEventListener(type, fn) { this.listeners.push([type, fn]); }
  focus() { document.activeElement = this; }
  get classList() {
    const names = () => (this.getAttribute("class") || "").split(" ").filter(Boolean);
    const set = (list) => this.setAttribute("class", list.join(" "));
    return {
      contains: (name) => names().includes(name),
      toggle: (name, force) => {
        const on = force === undefined ? !names().includes(name) : !!force;
        set(on ? [...new Set([...names(), name])] : names().filter((n) => n !== name));
        return on;
      },
      remove: (...gone) => set(names().filter((n) => !gone.includes(n))),
    };
  }
  querySelectorAll(selector) { return elements(this).slice(1).filter((el) => matches(el, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

// Just enough of CSS selectors for the page's own: compounds of a tag, #id,
// .class, [attr], [attr="value"] and :checked, joined by spaces
// (descendants), in a list separated by commas. h() puts data-* in the
// dataset, so those attributes are read from there.
function compound(text) {
  const parts = text.match(/^[\w*]+|#[\w-]+|\.[\w-]+|\[[^\]]+\]|:checked/g) || [];
  const attr = (el, name) => (name.startsWith("data-")
    ? el.dataset[name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] ?? el.getAttribute(name)
    : el.getAttribute(name));
  return (el) => parts.every((part) => {
    if (part[0] === "#") return el.id === part.slice(1);
    if (part[0] === ".") return (el.getAttribute("class") || "").split(" ").includes(part.slice(1));
    if (part === ":checked") return el.checked === true;
    if (part[0] === "[") {
      const [, name, value] = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(part);
      const found = attr(el, name);
      return value === undefined ? found !== null && found !== undefined : found === value;
    }
    return part === "*" || el.nodeName.toUpperCase() === part.toUpperCase();
  });
}

function matches(el, selector) {
  return selector.split(",").some((one) => matchesOne(el, one));
}

function matchesOne(el, selector) {
  const steps = selector.trim().split(/\s+/).map(compound);
  if (!steps[steps.length - 1](el)) return false;
  let i = steps.length - 2;
  for (let node = el.parentNode; node && i >= 0; node = node.parentNode) {
    if (node.nodeType === 1 && steps[i](node)) i--;
  }
  return i < 0;
}

globalThis.Node = FakeNode;
FakeNode.ELEMENT_NODE = 1;
FakeNode.TEXT_NODE = 3;

globalThis.document = {
  body: new FakeElement("BODY"),
  activeElement: null,
  createElement: (tag) => new FakeElement(tag.toUpperCase()),
  createElementNS: (ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
  getElementById(id) {
    const find = (el) => (el.id === id ? el : el.childNodes.filter((c) => c.nodeType === 1).map(find).find(Boolean));
    return find(this.body) || null;
  },
  querySelectorAll(selector) { return elements(this.body).filter((el) => matches(el, selector)); },
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
};

// What a screen reader would say: the text, skipping aria-hidden parts.
export function spoken(node) {
  if (node.nodeType === 3) return node.nodeValue;
  if (node.getAttribute("aria-hidden") === "true") return "";
  return node.childNodes.map(spoken).join("");
}

// Every element under node, in document order, node first.
export function elements(node) {
  return [node, ...node.childNodes.filter((c) => c.nodeType === 1).flatMap(elements)];
}

export const byClass = (node, name) =>
  elements(node).filter((el) => (el.getAttribute("class") || "").split(" ").includes(name));

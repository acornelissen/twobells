// Just enough of the DOM for the dashboard's modules to build nodes under
// node, for tests/test_dashboard_static.py. Importing it sets the globals
// (document, Node), so import it before the module under test.

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

  append(...nodes) {
    for (const node of nodes) {
      const child = node instanceof FakeNode ? node : new FakeText(String(node));
      child.remove();
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }

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
    };
  }
  querySelectorAll(selector) { return elements(this).slice(1).filter((el) => matches(el, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

// Just enough of CSS selectors for the page's own: compounds of a tag, #id,
// .class, [attr] and [attr="value"], joined by spaces (descendants). h() puts
// data-* in the dataset, so those attributes are read from there.
function compound(text) {
  const parts = text.match(/^[\w*]+|#[\w-]+|\.[\w-]+|\[[^\]]+\]/g) || [];
  const attr = (el, name) => (name.startsWith("data-")
    ? el.dataset[name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] ?? el.getAttribute(name)
    : el.getAttribute(name));
  return (el) => parts.every((part) => {
    if (part[0] === "#") return el.id === part.slice(1);
    if (part[0] === ".") return (el.getAttribute("class") || "").split(" ").includes(part.slice(1));
    if (part[0] === "[") {
      const [, name, value] = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(part);
      const found = attr(el, name);
      return value === undefined ? found !== null && found !== undefined : found === value;
    }
    return part === "*" || el.nodeName.toUpperCase() === part.toUpperCase();
  });
}

function matches(el, selector) {
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

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { initializeSite, PLAN_STORAGE_KEY } = require("../website/assets/app.js");

// Minimal standard DOM/storage doubles for controller tests, not browser/render evidence.
class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.style = {};
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.checked = false;
    this.tokens = new Set();
    this.classList = {
      add: (name) => this.tokens.add(name),
      toggle: (name, enabled) => enabled ? this.tokens.add(name) : this.tokens.delete(name),
    };
  }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  emit(type, extra = {}) {
    const event = { target: this, preventDefault() {}, ...extra };
    for (const handler of this.listeners[type] || []) handler(event);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  contains(target) { return target === this || this.children.some((child) => child.contains(target)); }
  focus() { this.focused = true; }
  reset() {}
  click() {
    if (this.tag === "input") { this.checked = !this.checked; this.emit("change"); }
    this.emit("click");
  }
}
function fixture(page, storage = new Map(), search = "") {
  const ids = page === "planner"
    ? ["launch-planner", "project-name", "project-scope", "project-focus", "launch-checklist", "planner-status", "list-title", "checklist-progress", "progress-fill", "download-list", "clear-plan"]
    : ["solution-category", "solution-level", "solution-count", "solution-empty", "reset-filters"];
  const elements = Object.fromEntries(ids.map((id) => ["#" + id, new Element()]));
  const menu = new Element("button");
  menu.setAttribute("aria-expanded", "false");
  elements[".menu-toggle"] = menu;
  elements["#main-nav"] = new Element("nav");
  const cards = [
    { category: "web", level: "beginner" }, { category: "web", level: "beginner" },
    { category: "content", level: "beginner" }, { category: "content", level: "intermediate" },
    { category: "security", level: "beginner" }, { category: "security", level: "intermediate" },
  ].map((data) => { const card = new Element("article"); card.dataset = data; return card; });
  const doc = new Element("document");
  doc.documentElement = new Element("html");
  doc.body = new Element("body");
  doc.querySelector = (selector) => elements[selector] || null;
  doc.querySelectorAll = (selector) => selector === "[data-solution]" ? cards : [];
  doc.createElement = (tag) => new Element(tag);
  const blobs = [];
  const downloads = [];
  const revoked = [];
  const create = doc.createElement;
  doc.createElement = (tag) => {
    const el = create(tag);
    if (tag === "a") el.click = () => downloads.push({ filename: el.download, url: el.href });
    return el;
  };
  const win = {
    location: { pathname: page === "planner" ? "/planner.html" : "/solutions.html", search, hash: "" },
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    URL: { createObjectURL: (blob) => { blobs.push(blob); return "blob:test-" + blobs.length; }, revokeObjectURL: (url) => revoked.push(url) },
    setTimeout: (fn) => fn(),
    matchMedia: () => ({ addEventListener() {} }),
  };
  win.history = { replaceState: (_state, _title, path) => { win.lastReplacedPath = path; win.location.search = ""; } };
  if (page === "solutions") {
    elements["#solution-category"].value = "all";
    elements["#solution-level"].value = "all";
  }
  return { doc, win, elements, cards, blobs, downloads, revoked, storage };
}
const get = (f, id) => f.elements["#" + id];
const boxes = (f) => get(f, "launch-checklist").children.map((row) => row.children[0].children[0]);

test("real filter handlers combine selections, report empty results, and reset", () => {
  const f = fixture("solutions");
  initializeSite(f.doc, f.win);
  get(f, "solution-category").value = "security";
  get(f, "solution-category").emit("change");
  assert.equal(f.cards.filter((card) => !card.hidden).length, 2);
  get(f, "solution-level").value = "intermediate";
  get(f, "solution-level").emit("change");
  assert.equal(f.cards.filter((card) => !card.hidden).length, 1);
  get(f, "solution-category").value = "web";
  get(f, "solution-category").emit("change");
  assert.equal(get(f, "solution-count").textContent, "0 solutions to explore");
  assert.equal(get(f, "solution-empty").hidden, false);
  get(f, "reset-filters").click();
  assert.equal(f.cards.filter((card) => !card.hidden).length, 6);
  assert.equal(get(f, "solution-empty").hidden, true);
});

test("selected-solution completion persists, and clear survives another page load", () => {
  const storage = new Map();
  let f = fixture("planner", storage, "?solution=Static%20website");
  initializeSite(f.doc, f.win);
  assert.equal(get(f, "project-name").value, "Static website");
  boxes(f)[0].click();
  assert.equal(get(f, "checklist-progress").textContent, "1 / 8 checked");
  f = fixture("planner", storage, "?solution=Static%20website");
  initializeSite(f.doc, f.win);
  assert.equal(boxes(f)[0].checked, true);
  get(f, "clear-plan").click();
  assert.equal(f.win.lastReplacedPath, "/planner.html");
  assert.equal(storage.has(PLAN_STORAGE_KEY), false);
  assert.equal(get(f, "project-name").value, "");
  f = fixture("planner", storage);
  initializeSite(f.doc, f.win);
  assert.equal(get(f, "project-name").value, "");
  assert.equal(boxes(f)[0].checked, false);
});

test("form submission builds the right list and the download contains current state", async () => {
  const f = fixture("planner");
  initializeSite(f.doc, f.win);
  get(f, "project-name").value = "My GBM launch";
  get(f, "project-scope").value = "launch";
  get(f, "project-focus").value = "security";
  get(f, "launch-planner").emit("submit");
  assert.equal(boxes(f).length, 13);
  assert.equal(get(f, "list-title").textContent, "My GBM launch");
  boxes(f)[0].click();
  get(f, "download-list").click();
  assert.equal(f.downloads[0].filename, "gbm-cloud-launch-checklist.txt");
  const downloaded = await f.blobs[0].text();
  assert.ok(downloaded.includes("GBM Cloud"));
  assert.ok(downloaded.includes("My GBM launch"));
  assert.ok(downloaded.includes("[x] Define"));
  assert.equal(f.doc.body.children.length, 0);
  assert.equal(f.revoked.length, 1);
});

test("storage denial does not prevent checklist updates or downloading", () => {
  const f = fixture("planner");
  f.win.localStorage = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
  initializeSite(f.doc, f.win);
  assert.equal(boxes(f).length, 8);
  get(f, "project-name").value = "Temporary plan";
  get(f, "project-scope").value = "prototype";
  get(f, "project-focus").value = "balanced";
  get(f, "launch-planner").emit("submit");
  assert.ok(get(f, "planner-status").textContent.includes("cannot save"));
  boxes(f)[0].click();
  assert.equal(get(f, "checklist-progress").textContent, "1 / 8 checked");
  get(f, "download-list").click();
  assert.equal(f.downloads.length, 1);
  get(f, "clear-plan").click();
  assert.equal(boxes(f)[0].checked, false);
});

test("mobile menu updates labels, closes on Escape and closes on a link", () => {
  const f = fixture("solutions");
  initializeSite(f.doc, f.win);
  const button = f.elements[".menu-toggle"];
  button.click();
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal(button.getAttribute("aria-label"), "Close navigation");
  f.doc.emit("keydown", { key: "Escape" });
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(button.focused, true);
  button.click();
  f.elements["#main-nav"].emit("click", { target: { closest: () => ({ href: "about.html" }) } });
  assert.equal(button.getAttribute("aria-expanded"), "false");
});

// Runs static/tour.js against a tiny fake DOM and prints what the tour showed, as JSON.
//
// Used by tests/test_docs.py (skipped where node is not installed). Not a browser: just enough
// of the DOM for the tour's own calls, so its step logic (skipping, Back, a target vanishing,
// what it records when it ends) can be driven without one.
//
//   node tests/tour_harness.js static/tour.js <scenario>
'use strict';

const fs = require('fs');
const vm = require('vm');

class ClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(tag, attrs) {
    this.tagName = tag.toUpperCase();
    this.attrs = Object.assign({}, attrs || {});
    this.children = [];
    this.parent = null;
    this.classList = new ClassList();
    (this.attrs.class || '').split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    this.listeners = {};
    this.style = { setProperty() {} };
    this.textContent = '';
    this.tabIndex = 0;
    this.offsetWidth = 300;
    this.offsetHeight = 120;
  }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  set className(v) { v.split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((x) => x !== this);
    this.parent = null;
  }
  get isConnected() {
    let n = this;
    while (n.parent) n = n.parent;
    return n === doc.root;
  }
  matches(sel) { return matches(this, sel); }
  closest(sel) {
    for (let n = this; n && n !== doc.root; n = n.parent) if (matches(n, sel)) return n;
    return null;
  }
  querySelector(sel) { return find(this, sel); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  // Bubbles straight from the element to the document, which is as far as the tour listens.
  dispatch(type, extra) {
    const e = Object.assign({ type, target: this, preventDefault() { this.prevented = true; } }, extra);
    for (let n = this; n; n = n.parent) (n.listeners[type] || []).forEach((fn) => fn(e));
    (doc.listeners[type] || []).forEach((fn) => fn(e));
    return e;
  }
  click() { return this.dispatch('click'); }
  getClientRects() { return [1]; }
  getBoundingClientRect() { return { top: 100, bottom: 140, left: 100, right: 300, width: 200, height: 40 }; }
  scrollIntoView() {}
  focus() {}
}

function matches(el, sel) {
  let m;
  if ((m = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(sel))) {
    return m[1] in el.attrs && (m[2] === undefined || el.attrs[m[1]] === m[2]);
  }
  if ((m = /^([a-z]+)\[([a-z-]+)\]$/.exec(sel))) {
    return el.tagName === m[1].toUpperCase() && m[2] in el.attrs;
  }
  if ((m = /^\.([a-z_-]+)$/i.exec(sel))) return el.classList.contains(m[1]);
  return false;  // descendant selectors: the queue page's card steps, not driven here
}

function find(root, sel) {
  for (const c of root.children) {
    if (matches(c, sel)) return c;
    const deeper = find(c, sel);
    if (deeper) return deeper;
  }
  return null;
}

const doc = {
  root: new El('html'),
  readyState: 'loading',
  listeners: {},
  activeElement: null,
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((x) => x !== fn);
  },
  createElement(tag) { return new El(tag); },
  querySelector(sel) { return find(this.root, sel); },
};
doc.body = doc.root.appendChild(new El('body', { 'data-signed-in': '' }));

const store = {};
const frames = [];
const win = {
  innerWidth: 1200,
  innerHeight: 800,
  location: { search: '', href: 'http://x/', pathname: '/', hash: '' },
  history: { state: null, replaceState() {} },
  listeners: {},
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((x) => x !== fn);
  },
  matchMedia() { return { matches: true }; },
  requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
  localStorage: {
    getItem(k) { return k in store ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
  },
};

// ---------- the page ----------

function anchor(parent, name, extra) {
  return parent.appendChild(new El('div', Object.assign({ 'data-tour': name }, extra || {})));
}

const scenario = process.argv[3];
const main = doc.body.appendChild(new El('main', { 'data-tour-page': 'submit' }));
const startLink = doc.body.appendChild(new El('a', { 'data-tour-start': '' }));
const els = {};
els['submit-file'] = anchor(main, 'submit-file');
els.preflight = anchor(main, 'preflight', { hidden: '' });  // as rendered: hidden until a file
els.note = anchor(main, 'note');
els.submit = anchor(main, 'submit');
els['tab-update'] = anchor(main, 'tab-update');
els['nav-mine'] = anchor(doc.body, 'nav-mine');
els['nav-help'] = anchor(doc.body, 'nav-help');

vm.createContext(Object.assign(win, { window: win, document: doc, URL, setTimeout() {} }));
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), win);

function pop() { return doc.querySelector('.tour'); }
function shown() {
  const p = pop();
  if (!p) return null;
  return { count: p.querySelector('.tour__count').textContent, title: p.querySelector('.tour__title').textContent };
}
function next() { pop().querySelector('.tour__next').click(); }
function back() { pop().querySelector('.tour__back').click(); }
function flush() {
  (win.listeners.scroll || []).forEach((fn) => fn());
  while (frames.length) frames.shift()();
}

const out = {};
startLink.click();
out.first = shown();

if (scenario === 'walk') {
  out.titles = [];
  for (let i = 0; i < 20 && pop(); i++) {
    out.titles.push(shown().title);
    next();
  }
  out.stored = store['wpTour.v1.submit'] || null;
}

if (scenario === 'back-stays-when-nothing-earlier') {
  next(); next(); next();  // on step 4 (submit)
  els['submit-file'].remove();  // takes the preflight step's stand-in with it
  els.note.remove();
  back();  // every earlier step is gone: stay on this one
  out.afterBack = shown();
}

if (scenario === 'back-lands-earlier') {
  next(); next(); next();  // on step 4 (submit)
  els.note.remove();
  back();  // the note is gone: the step before it is the preflight stand-in
  out.afterBack = shown();
}

if (scenario === 'middle-vanishes') {
  next(); next();  // on step 3 (the note)
  els.note.remove();
  flush();
  out.afterVanish = shown();
}

if (scenario === 'last-vanishes') {
  for (let i = 0; i < 6; i++) next();  // on the last step (help)
  out.onLast = shown();
  els['nav-help'].remove();
  flush();
  out.afterVanish = shown();
  out.stored = store['wpTour.v1.submit'] || null;
}

if (scenario === 'all-vanish') {
  Object.keys(els).forEach((k) => els[k].remove());
  flush();
  out.afterVanish = shown();
  out.stored = store['wpTour.v1.submit'] || null;
}

process.stdout.write(JSON.stringify(out));

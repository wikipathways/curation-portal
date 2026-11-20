// viewer.js: the interactive pathway viewer (contract K6). Loaded after app.js (window.WP).
//
// The render used to be an <img> with a layer of buttons laid over it, positioned in
// percentages. That kept a hostile GPML's drawing inert, and it also meant nothing in the drawing
// could be pointed at except the data-node boxes: not an interaction, not a group, not the
// catalyst on an anchor. Here the SVG is fetched, rebuilt element by element from an allowlist
// (only the vocabulary app/preview/svg.py emits, see the block below), and inlined, so every
// element carries its data-id and the graph sidecar says what it means.
//
// Zoom and pan move the viewBox, not a CSS transform, so text stays crisp at any scale.
// No build step, no libraries: ES2017 in one IIFE, like the rest of static/.
(function () {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';

  // The only elements and attributes that survive the sanitiser, plus data-*. Kept as a JSON
  // block so tests/test_viewer_markup.py can check it still covers everything the renderer
  // writes (app.preview.svg.ALLOWED_ELEMENTS / ALLOWED_ATTRIBUTES): a renderer that starts
  // emitting something new would otherwise have it silently stripped here.
  var ALLOW = /* WP-SVG-ALLOWLIST */{
    "elements": ["svg", "title", "defs", "marker", "g", "path", "rect", "ellipse", "circle",
      "polygon", "polyline", "line", "text", "tspan"],
    "attributes": ["xmlns", "viewBox", "width", "height", "id", "class", "x", "y", "x1", "y1",
      "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "transform", "fill", "fill-opacity",
      "stroke", "stroke-width", "stroke-dasharray", "stroke-opacity", "stroke-linejoin",
      "stroke-linecap", "marker-start", "marker-end", "markerWidth", "markerHeight", "refX",
      "refY", "orient", "markerUnits", "text-anchor", "dominant-baseline", "font-family",
      "font-size", "font-weight", "font-style", "opacity", "role", "aria-label"]
  }/* END */;

  var ALLOWED_ELEMENTS = {};
  var ALLOWED_ATTRIBUTES = {};
  ALLOW.elements.forEach(function (n) { ALLOWED_ELEMENTS[n] = true; });
  ALLOW.attributes.forEach(function (n) { ALLOWED_ATTRIBUTES[n] = true; });
  // Text nodes are kept only where they are drawn. Whitespace between elements is noise, and
  // there is no reason to carry text anywhere else.
  var TEXT_PARENTS = { text: true, tspan: true, title: true };
  // The renderer caps a pathway at 20 000 elements. Anything far beyond that was not written by
  // it, and walking it would only stall the tab.
  var MAX_NODES = 60000;
  var MAX_ZOOM = 24;
  var PAN_STEP = 40;

  // How each diff classification reads on screen (issue #24). Unchanged is deliberately absent:
  // saying so on every one of a hundred nodes buries the five that did change.
  var CHANGE_WORD = {
    added: 'added',
    removed: 'removed',
    reannotated: 're-annotated',
    relabelled: 'relabelled',
    moved: 'moved'
  };
  // The badge drawn on a changed node, so the classification never rests on colour alone.
  var CHANGE_BADGE = { added: '+', removed: '−', reannotated: '≠', relabelled: 'T', moved: '↔' };
  var CHANGE_ORDER = ['added', 'removed', 'reannotated', 'relabelled', 'moved'];
  var GROUP_WORD = { complex: 'Complex', group: 'Group', none: 'Group (no outline)', pathway: 'Pathway group' };

  var mounted = [];
  var diffCache = {};
  var mountCount = 0;

  // ---------- small helpers ----------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function svgEl(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }

  function getJSON(url, signal) {
    if (!url) return Promise.resolve(null);
    return fetch(url, { credentials: 'same-origin', signal: signal })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function getText(url, signal) {
    if (!url) return Promise.resolve(null);
    return fetch(url, { credentials: 'same-origin', signal: signal })
      .then(function (r) { return r.ok ? r.text() : null; })
      .catch(function () { return null; });
  }

  // One diff.json per card, however many viewers ask for it: the two sides of an update would
  // otherwise fetch it twice, and the change list has to be built from one copy anyway.
  function sharedDiff(url) {
    if (!url) return Promise.resolve(null);
    if (!diffCache[url]) {
      diffCache[url] = getJSON(url).then(function (d) {
        if (!d) delete diffCache[url];
        return d;
      });
    }
    return diffCache[url];
  }

  function isHttps(url) { return typeof url === 'string' && /^https:\/\//.test(url); }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }

  function validBox(b) {
    return Array.isArray(b) && b.length === 4 && b.every(function (v) { return num(v) !== null; })
      && b[2] > 0 && b[3] > 0;
  }

  function labelRow(dl, term, value) {
    var dt = document.createElement('dt');
    dt.textContent = term;
    var dd = document.createElement('dd');
    if (value && typeof value === 'object' && value.href) {
      var a = document.createElement('a');
      a.href = value.href;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = value.text;
      dd.appendChild(a);
    } else if (value instanceof Node) {
      // Markup the caller built, e.g. the struck-through previous value. Assigning it to
      // textContent instead renders the string "[object HTMLElement]", which it once did.
      dd.appendChild(value);
    } else {
      dd.textContent = value;
    }
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function strike(text) {
    var s = document.createElement('s');
    s.textContent = text;
    return s;
  }

  // Reading order for arrow-key navigation (issue #19). A plain sort on (top, left) is not it:
  // a node sitting a hair higher than its neighbours comes before the entire rest of its row, so
  // the cursor appears to jump around the diagram. Bucket into rows first, then order each row
  // left to right. The row's limit is fixed by the node that opened it, so a staircase layout
  // cannot greedily swallow the whole page into one row.
  function readingOrder(items, box) {
    var rows = [];
    items.slice()
      .sort(function (a, b) { return (box(a)[1] - box(b)[1]) || (box(a)[0] - box(b)[0]); })
      .forEach(function (n) {
        var row = rows[rows.length - 1];
        var b = box(n);
        if (row && b[1] < row.limit) row.items.push(n);
        else rows.push({ items: [n], limit: b[1] + b[3] / 2 });
      });
    var out = [];
    rows.forEach(function (row) {
      row.items.sort(function (a, b) { return box(a)[0] - box(b)[0]; });
      out.push.apply(out, row.items);
    });
    return out;
  }

  // ---------- sanitiser ----------
  // The SVG is rebuilt rather than cleaned in place: a fresh element is created for each allowed
  // source element and only allowed attributes are copied across, so anything the walk does not
  // understand is simply never created. Iterative, because a hostile file can nest deep enough to
  // exhaust the stack of a recursive walk.
  function cleanValue(name, value, prefix) {
    var v = String(value);
    var squashed = v.replace(/[\s\u0000-\u001f]+/g, '').toLowerCase();
    if (squashed.indexOf('javascript:') !== -1) return null;
    if (squashed.indexOf('url(') !== -1) {
      // Only same-document references (marker-end="url(#…)") survive, re-pointed at this
      // mount's ids. Two SVGs on one page (the diff) would otherwise share marker ids.
      var m = /^\s*url\(#([A-Za-z0-9_.:-]+)\)\s*$/.exec(v);
      return m ? 'url(#' + prefix + m[1] + ')' : null;
    }
    if (name === 'id') return /^[A-Za-z0-9_.:-]+$/.test(v) ? prefix + v : null;
    return v;
  }

  function attributeAllowed(attr) {
    if (attr.namespaceURI) return false; // xlink:href, xml:base, xmlns:* and friends
    var name = attr.name;
    if (/^on/i.test(name) || /href/i.test(name) || name === 'xmlns') return false;
    if (/^data-[a-z0-9_.-]+$/i.test(name)) return true;
    return ALLOWED_ATTRIBUTES[name] === true;
  }

  function sanitizeSvg(text, prefix) {
    var doc;
    try {
      doc = new DOMParser().parseFromString(text, 'image/svg+xml');
    } catch (e) {
      return null;
    }
    if (!doc || doc.getElementsByTagName('parsererror').length) return null;
    var src = doc.documentElement;
    if (!src || src.namespaceURI !== SVG_NS || src.localName !== 'svg') return null;
    var budget = 0;
    var root = svgEl('svg');
    var stack = [[src, root]];
    while (stack.length) {
      var pair = stack.pop();
      var from = pair[0], to = pair[1];
      for (var i = 0; i < from.attributes.length; i++) {
        var attr = from.attributes[i];
        if (!attributeAllowed(attr)) continue;
        var value = cleanValue(attr.name, attr.value, prefix);
        if (value !== null) to.setAttribute(attr.name, value);
      }
      for (var c = from.firstChild; c; c = c.nextSibling) {
        if (++budget > MAX_NODES) return null;
        if (c.nodeType === 1) {
          if (c.namespaceURI !== SVG_NS || !ALLOWED_ELEMENTS[c.localName]) continue;
          var copy = document.createElementNS(SVG_NS, c.localName);
          to.appendChild(copy);
          stack.push([c, copy]);
        } else if ((c.nodeType === 3 || c.nodeType === 4) && TEXT_PARENTS[from.localName]) {
          to.appendChild(document.createTextNode(c.nodeValue));
        }
      }
    }
    return root;
  }

  // ---------- graph ----------
  // The v2 sidecar (contract K2) when there is one; otherwise the older -nodes.json hotspots,
  // which carry the same properties positioned in percentages of the viewBox. Normalised to one
  // shape so the rest of the viewer has a single code path.
  function normaliseGraph(graph, hotspots, svgBox) {
    var g = {
      legacy: false, viewBox: null, nodes: [], edges: [], groups: [], labels: [], shapes: [],
      stats: {}
    };
    if (graph && graph.version === 2) {
      g.viewBox = validBox(graph.viewBox) ? graph.viewBox.slice() : svgBox;
      ['nodes', 'edges', 'groups', 'labels', 'shapes'].forEach(function (k) {
        g[k] = Array.isArray(graph[k]) ? graph[k].filter(function (x) { return x && x.id; }) : [];
      });
      g.stats = graph.stats || {};
    } else {
      g.legacy = true;
      g.viewBox = svgBox;
      if (Array.isArray(hotspots) && svgBox) {
        hotspots.forEach(function (h, i) {
          var x = svgBox[0] + (+h.left || 0) / 100 * svgBox[2];
          var y = svgBox[1] + (+h.top || 0) / 100 * svgBox[3];
          var w = (+h.width || 0) / 100 * svgBox[2];
          var hh = (+h.height || 0) / 100 * svgBox[3];
          g.nodes.push({
            id: 'h-' + i, graph_id: h.graph_id || '', kind: 'DataNode', type: h.type,
            label: h.label, database: h.database, identifier: h.identifier, url: h.url,
            comment: h.comment, bbox: [x, y, w, hh], cx: x + w / 2, cy: y + hh / 2,
            group: null, states: []
          });
        });
      }
    }
    g.nodes.forEach(function (n) {
      if (!validBox(n.bbox)) {
        var cx = num(n.cx) || 0, cy = num(n.cy) || 0;
        n.bbox = [cx - 1, cy - 1, 2, 2];
      }
    });
    return g;
  }

  function parseViewBox(svg) {
    var raw = (svg.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
    return validBox(raw) ? raw : null;
  }

  // ---------- the viewer ----------
  function Viewer(root) {
    this.root = root;
    this.side = root.getAttribute('data-side') || 'after';
    this.svgUrl = root.getAttribute('data-svg-url') || '';
    this.graphUrl = root.getAttribute('data-graph-url') || '';
    this.diffUrl = root.getAttribute('data-diff-url') || '';
    this.renderUrl = root.getAttribute('data-render-svg-url') || '';
    this.mode = root.getAttribute('data-mode') || 'full';
    this.canvas = root.querySelector('.viewer__canvas');
    this.img = root.querySelector('.viewer__fallback');
    this.tooltip = root.querySelector('.viewer__tooltip');
    this.panel = root.querySelector('.node-panel');
    this.panelBody = root.querySelector('.node-panel__body');
    this.legend = root.querySelector('.viewer__legend');
    this.status = root.querySelector('.viewer__status');
    this.search = root.querySelector('.viewer__search-input');
    this.prefix = 'wpv' + (++mountCount) + '-';
    this.pair = null;
    this.svg = null;
    this.mounting = null;
    this.destroyed = false;
    this.pointers = {};
    this.order = [];
    this.current = 0;
    this.matches = [];
    this.matchIndex = -1;
    this.selected = null;
    this.hl = [];
    this.anim = 0;
    this.dbZoom = 1;
    this.lastFocus = null;
    root.__wpViewer = this;
    this.wire();
  }

  // Load and inline the drawing. Idempotent: every caller gets the same promise, so a click on
  // a toolbar button in lite mode can queue its action behind the activation that click caused.
  Viewer.prototype.mount = function () {
    if (this.mounting) return this.mounting;
    var self = this;
    var ac = typeof AbortController === 'function' ? new AbortController() : null;
    this.abort = ac;
    var signal = ac ? ac.signal : undefined;
    this.root.classList.add('viewer--loading');
    var nodesUrl = this.svgUrl.replace(/\.svg(\?|$)/, '-nodes.json$1');
    var graph = getJSON(this.graphUrl, signal).then(function (g) {
      // Absent, an older cache, or not v2: the hotspots are the next best thing.
      return g && g.version === 2 ? { graph: g } : getJSON(nodesUrl, signal).then(function (h) {
        return { hotspots: h };
      });
    });
    this.mounting = Promise.all([getText(this.svgUrl, signal), graph, sharedDiff(this.diffUrl)])
      .then(function (res) {
        if (self.destroyed) return false;
        var svg = res[0] ? sanitizeSvg(res[0], self.prefix) : null;
        if (!svg) return self.fail();
        self.install(svg, res[1] || {}, res[2]);
        return true;
      })
      .catch(function () { return self.destroyed ? false : self.fail(); });
    return this.mounting;
  };

  // The <img> already on screen is the fallback, so failing is mostly a matter of leaving it.
  Viewer.prototype.fail = function () {
    this.root.classList.remove('viewer--loading');
    this.root.classList.add('viewer--static');
    this.say('The interactive view could not be loaded; this is the plain picture.');
    return false;
  };

  Viewer.prototype.say = function (text) {
    if (this.status) this.status.textContent = text;
  };

  Viewer.prototype.install = function (svg, loaded, diff) {
    var self = this;
    var svgBox = parseViewBox(svg);
    var graph = normaliseGraph(loaded.graph, loaded.hotspots, svgBox);
    this.graph = graph;
    // Sized by the canvas, not by the file; the viewBox is what zooms.
    svg.removeAttribute('width');
    svg.removeAttribute('height');
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    svg.setAttribute('class', ((svg.getAttribute('class') || '') + ' viewer__svg').trim());
    // role="img" would hide the focusable nodes from assistive technology, and the <title>
    // would put a browser tooltip over the whole drawing, on top of this viewer's own.
    svg.setAttribute('role', 'group');
    Array.prototype.slice.call(svg.childNodes).forEach(function (c) {
      if (c.nodeType === 1 && c.localName === 'title') svg.removeChild(c);
    });

    this.els = {};
    svg.querySelectorAll('[data-id]').forEach(function (e) {
      var id = e.getAttribute('data-id');
      if (!self.els[id]) self.els[id] = e;
    });
    this.statesByParent = {};
    svg.querySelectorAll('[data-parent]').forEach(function (e) {
      var p = e.getAttribute('data-parent');
      (self.statesByParent[p] = self.statesByParent[p] || []).push(e);
    });

    this.byId = {};
    graph.nodes.forEach(function (n) { self.byId[n.id] = { kind: 'node', item: n }; });
    graph.edges.forEach(function (e) { self.byId[e.id] = { kind: 'edge', item: e }; });
    graph.groups.forEach(function (g) { self.byId[g.id] = { kind: 'group', item: g }; });
    graph.labels.forEach(function (l) { self.byId[l.id] = { kind: 'label', item: l }; });
    this.edgesByEnd = {};
    this.anchoredOn = {};
    this.anchorOwner = {};
    graph.edges.forEach(function (e) {
      [e.source, e.target].forEach(function (end) {
        if (end) (self.edgesByEnd[end] = self.edgesByEnd[end] || []).push(e);
      });
      [e.target_edge, e.source_edge].forEach(function (on) {
        if (on) (self.anchoredOn[on] = self.anchoredOn[on] || []).push(e);
      });
      (e.anchors || []).forEach(function (a) { if (a && a.id) self.anchorOwner[a.id] = e.id; });
    });

    var overlay = svgEl('g', { 'class': 'viewer__overlay', 'aria-hidden': 'true' });
    this.hits = svgEl('g', { 'class': 'viewer__hits' });
    this.marks = svgEl('g', { 'class': 'viewer__marks', 'aria-hidden': 'true' });
    this.ends = svgEl('g', { 'class': 'viewer__ends', 'aria-hidden': 'true' });
    this.rings = svgEl('g', { 'class': 'viewer__rings', 'aria-hidden': 'true' });
    // The hit layer holds the legacy node boxes, which must stay clickable and focusable, so it
    // sits outside the aria-hidden overlay.
    svg.appendChild(this.hits);
    svg.appendChild(overlay);
    overlay.appendChild(this.marks);
    overlay.appendChild(this.ends);
    overlay.appendChild(this.rings);

    // A one-pixel line is not something anyone can click. A wide transparent copy of each
    // interaction's geometry takes the pointer instead; its stroke width is in screen pixels
    // (non-scaling, see viewer.css) so it stays easy to hit at every zoom.
    svg.querySelectorAll('.wp-edge').forEach(function (g) {
      var line = g.querySelector('.wp-edge__line') || g.querySelector('path, polyline, line');
      if (!line) return;
      var hit = svgEl(line.localName, { 'class': 'viewer__edge-hit' });
      ['d', 'points', 'x1', 'y1', 'x2', 'y2', 'transform'].forEach(function (a) {
        if (line.hasAttribute(a)) hit.setAttribute(a, line.getAttribute(a));
      });
      g.appendChild(hit);
    });

    this.applyDiff(diff);

    graph.nodes.forEach(function (n) {
      var e = self.els[n.id];
      if (!e) {
        // No drawn element (the legacy hotspots, or a render that dropped it): a transparent
        // box from the sidecar's geometry keeps the node reachable.
        e = svgEl('rect', {
          'class': 'viewer__hit', 'data-id': n.id,
          x: n.bbox[0], y: n.bbox[1], width: n.bbox[2], height: n.bbox[3]
        });
        self.hits.appendChild(e);
        self.els[n.id] = e;
      }
      n.el = e;
      e.setAttribute('tabindex', '-1');
      e.setAttribute('role', 'button');
      e.setAttribute('aria-label', self.nodeName(n));
      if (n.__change && n.__change !== 'unchanged') e.classList.add('is-' + n.__change);
    });

    // Roving tabindex over the nodes (issue #19): one tab stop for the whole diagram, the arrow
    // keys for the rest. A hundred-node pathway would otherwise be a hundred stops before the
    // checklist.
    this.order = readingOrder(graph.nodes, function (n) { return n.bbox; });
    this.order.forEach(function (n, i) { n.__order = i; });
    if (this.order.length) this.order[0].el.setAttribute('tabindex', '0');

    this.drawEnds();

    this.svg = svg;
    this.fit = validBox(graph.viewBox) ? graph.viewBox.slice() : null;
    this.canvas.appendChild(svg);
    if (!this.fit) {
      try {
        var bb = svg.getBBox();
        this.fit = [bb.x - 15, bb.y - 15, bb.width + 30, bb.height + 30];
      } catch (e) {
        this.fit = [0, 0, 100, 100];
      }
    }
    this.vb = this.fit.slice();
    this.applyViewBox();
    this.img.hidden = true;
    this.root.classList.remove('viewer--loading');
    this.root.classList.add('viewer--ready');
    this.buildLegend();
    if (this.pair) this.pair.rebuild();
    else this.updateChangeButtons();
  };

  Viewer.prototype.nodeName = function (n) {
    var parts = [n.label || 'Unlabelled node'];
    if (n.type) parts.push(n.type);
    if (CHANGE_WORD[n.__change]) parts.push(CHANGE_WORD[n.__change]);
    return parts.join(', ');
  };

  // Classes by id when the diff names them (K3), else by position in the index-aligned arrays
  // the first version of the diff wrote, as long as the lengths prove both came from one render.
  Viewer.prototype.applyDiff = function (diff) {
    var self = this;
    var nodes = this.graph.nodes;
    this.diff = diff || null;
    this.counterpart = {};
    if (!diff) return;
    var side = this.side;
    var other = side === 'before' ? 'after' : 'before';
    var ids = diff.ids && diff.ids[side];
    if (ids && !this.graph.legacy) {
      nodes.forEach(function (n) {
        var v = ids[n.id];
        if (v == null) return;
        var entry = typeof v === 'string' ? { change: v } : v;
        n.__change = entry.change;
        n.__was = entry.was;
      });
    } else if (Array.isArray(diff[side]) && diff[side].length === nodes.length) {
      nodes.forEach(function (n, i) {
        var entry = diff[side][i] || {};
        n.__change = entry.change;
        n.__was = entry.was;
      });
    } else {
      // A length mismatch means the two were written by different renders and every colour
      // would be on the wrong box. No comparison beats one that is quietly a node out.
      this.diff = null;
      return;
    }
    if (!this.graph.legacy && Array.isArray(diff.pairs)) {
      diff.pairs.forEach(function (p) {
        if (p && p[side] && p[other]) self.counterpart[p[side]] = p[other];
      });
    }
    this.root.classList.add('viewer--diffed');
    nodes.forEach(function (n) {
      if (!CHANGE_WORD[n.__change]) return;
      var b = n.bbox, pad = 3;
      var mark = svgEl('rect', {
        'class': 'viewer__mark viewer__mark--' + n.__change,
        x: b[0] - pad, y: b[1] - pad, width: b[2] + 2 * pad, height: b[3] + 2 * pad, rx: 4
      });
      self.marks.appendChild(mark);
      var r = Math.max(4, Math.min(8, b[3] / 3));
      var badge = svgEl('g', { 'class': 'viewer__badge viewer__badge--' + n.__change });
      badge.appendChild(svgEl('circle', { cx: b[0] + b[2] + pad, cy: b[1] - pad, r: r }));
      var t = svgEl('text', {
        x: b[0] + b[2] + pad, y: b[1] - pad, 'text-anchor': 'middle',
        'dominant-baseline': 'central', 'font-size': r * 1.4
      });
      t.textContent = CHANGE_BADGE[n.__change];
      badge.appendChild(t);
      self.marks.appendChild(badge);
    });
  };

  // A line end that is not attached to anything is the commonest drawing defect after a missing
  // identifier, and invisible in the picture. Marked, but only shown on request.
  Viewer.prototype.drawEnds = function () {
    var self = this;
    var count = 0;
    this.graph.edges.forEach(function (e) {
      var pts = Array.isArray(e.points) ? e.points : [];
      if (!pts.length || !Array.isArray(e.dangling)) return;
      e.dangling.forEach(function (which) {
        var p = which === 'source' ? pts[0] : pts[pts.length - 1];
        if (!p || num(p[0]) === null || num(p[1]) === null) return;
        self.ends.appendChild(svgEl('circle', { 'class': 'viewer__end', cx: p[0], cy: p[1], r: 5 }));
        count++;
      });
    });
    this.danglingCount = num(this.graph.stats.dangling_ends) !== null ? this.graph.stats.dangling_ends : count;
  };

  // ---------- view box ----------
  Viewer.prototype.isZoomed = function () {
    return !!(this.vb && this.fit && this.vb[2] < this.fit[2] * 0.999);
  };

  Viewer.prototype.clampBox = function (b) {
    var f = this.fit;
    var w = Math.max(f[2] / MAX_ZOOM, Math.min(f[2], b[2]));
    var h = b[3] * (w / b[2]);
    // Keep the centre over the diagram, so holding an arrow key cannot walk it off screen.
    var cx = Math.max(f[0], Math.min(f[0] + f[2], b[0] + b[2] / 2));
    var cy = Math.max(f[1], Math.min(f[1] + f[3], b[1] + b[3] / 2));
    if (w >= f[2] * 0.999) { cx = f[0] + f[2] / 2; cy = f[1] + f[3] / 2; h = f[3]; w = f[2]; }
    return [cx - w / 2, cy - h / 2, w, h];
  };

  Viewer.prototype.applyViewBox = function () {
    var b = this.vb;
    this.svg.setAttribute('viewBox', b.map(function (v) { return +v.toFixed(2); }).join(' '));
    this.root.classList.toggle('viewer--zoomed', this.isZoomed());
  };

  // opts.sync false: do not drag the other side of the pair along (it is the one moving us).
  // Anything but an animation frame cancels a running animation, or its remaining frames would
  // snap the view back from under the zoom or pan that interrupted it.
  Viewer.prototype.setBox = function (b, opts) {
    if (!this.svg) return;
    opts = opts || {};
    if (!opts.anim) cancelAnimationFrame(this.anim);
    this.vb = opts.raw ? b.slice() : this.clampBox(b);
    this.applyViewBox();
    if (opts.sync !== false && this.pair) this.pair.follow(this);
  };

  Viewer.prototype.animateTo = function (b, opts) {
    var self = this;
    var target = this.clampBox(b);
    cancelAnimationFrame(this.anim);
    if (reducedMotion()) { this.setBox(target, opts); return; }
    var from = this.vb.slice();
    var start = null;
    function step(t) {
      if (start === null) start = t;
      var k = Math.min(1, (t - start) / 220);
      var e = 1 - Math.pow(1 - k, 3);
      self.setBox(from.map(function (v, i) { return v + (target[i] - v) * e; }),
        { raw: true, anim: true, sync: opts && opts.sync });
      if (k < 1) self.anim = requestAnimationFrame(step);
    }
    this.anim = requestAnimationFrame(step);
  };

  Viewer.prototype.clientToUser = function (x, y) {
    var m = this.svg.getScreenCTM();
    if (!m) return null;
    var p = this.svg.createSVGPoint();
    p.x = x; p.y = y;
    return p.matrixTransform(m.inverse());
  };

  Viewer.prototype.zoomAt = function (factor, clientX, clientY) {
    if (!this.svg) return;
    var p = this.clientToUser(clientX, clientY);
    var b = this.vb;
    if (!p) p = { x: b[0] + b[2] / 2, y: b[1] + b[3] / 2 };
    var w = Math.max(this.fit[2] / MAX_ZOOM, Math.min(this.fit[2], b[2] / factor));
    var f = b[2] / w;
    this.setBox([p.x - (p.x - b[0]) / f, p.y - (p.y - b[1]) / f, w, b[3] / f]);
  };

  Viewer.prototype.zoomCentre = function (factor) {
    if (this.root.classList.contains('viewer--db')) { this.zoomDb(factor); return; }
    var r = this.canvas.getBoundingClientRect();
    this.zoomAt(factor, r.left + r.width / 2, r.top + r.height / 2);
  };

  Viewer.prototype.panBy = function (dxPx, dyPx) {
    var m = this.svg.getScreenCTM();
    var k = m ? m.a : 1;
    var b = this.vb;
    this.setBox([b[0] - dxPx / k, b[1] - dyPx / k, b[2], b[3]]);
  };

  Viewer.prototype.fitView = function (opts) {
    if (this.root.classList.contains('viewer--db')) { this.dbZoom = 1; this.zoomDb(1); return; }
    if (!this.svg) return;
    this.setBox(this.fit, { sync: false });
    if ((!opts || opts.sync !== false) && this.pair) this.pair.each(this, function (v) { v.fitView({ sync: false }); });
  };

  // Bring a box into view: centred, zoomed in far enough to read it when the whole diagram is
  // on screen, left at the current zoom when it is already readable.
  Viewer.prototype.reveal = function (bbox, opts) {
    if (!this.svg || !validBox(bbox)) return;
    var b = this.vb;
    var w = b[2];
    if (!this.isZoomed()) w = Math.min(this.fit[2], Math.max(bbox[2] * 8, this.fit[2] / 3));
    var h = w * (b[3] / b[2]);
    var cx = bbox[0] + bbox[2] / 2, cy = bbox[1] + bbox[3] / 2;
    var inside = bbox[0] >= b[0] && bbox[1] >= b[1] && bbox[0] + bbox[2] <= b[0] + b[2]
      && bbox[1] + bbox[3] <= b[1] + b[3];
    if (inside && this.isZoomed() && opts && opts.ifNeeded) return;
    this.animateTo([cx - w / 2, cy - h / 2, w, h], opts);
  };

  Viewer.prototype.shows = function (bbox) {
    var b = this.vb;
    return !!b && bbox[0] >= b[0] && bbox[1] >= b[1] && bbox[0] + bbox[2] <= b[0] + b[2]
      && bbox[1] + bbox[3] <= b[1] + b[3];
  };

  // ---------- selection ----------
  Viewer.prototype.clearHighlight = function () {
    this.hl.forEach(function (e) { e.classList.remove('is-hl', 'is-selected'); });
    this.hl = [];
    this.root.classList.remove('viewer--focus');
    while (this.rings && this.rings.firstChild) this.rings.removeChild(this.rings.firstChild);
    this.selected = null;
  };

  Viewer.prototype.mark = function (id, selected) {
    var e = this.els[id];
    if (!e) return;
    e.classList.add('is-hl');
    if (selected) e.classList.add('is-selected');
    this.hl.push(e);
    (this.statesByParent[id] || []).forEach(function (s) { s.classList.add('is-hl'); this.hl.push(s); }, this);
  };

  // An end of an edge, resolved through anchors: a catalysis drawn onto the anchor of a
  // conversion is about that conversion, so its "other end" is the conversion and both of its
  // ends, not an invisible point in the middle of a line.
  Viewer.prototype.markEnd = function (end, edgeOn) {
    if (!end) return;
    var owner = edgeOn || this.anchorOwner[end];
    if (/^a-/.test(end) || owner) {
      var host = this.byId[owner];
      if (host) {
        this.mark(host.item.id);
        if (host.item.source && !/^a-/.test(host.item.source)) this.mark(host.item.source);
        if (host.item.target && !/^a-/.test(host.item.target)) this.mark(host.item.target);
      }
      return;
    }
    this.mark(end);
    var hit = this.byId[end];
    if (hit && hit.kind === 'group') {
      (hit.item.members || []).forEach(function (m) { this.mark(m); }, this);
    }
  };

  Viewer.prototype.highlightEdge = function (e) {
    this.mark(e.id);
    this.markEnd(e.source, e.source_edge);
    this.markEnd(e.target, e.target_edge);
    (this.anchoredOn[e.id] || []).forEach(function (a) {
      this.mark(a.id);
      if (a.target_edge === e.id) this.markEnd(a.source, a.source_edge);
      if (a.source_edge === e.id) this.markEnd(a.target, a.target_edge);
    }, this);
  };

  Viewer.prototype.ring = function (bbox, cls) {
    if (!validBox(bbox)) return;
    var pad = 4;
    this.rings.appendChild(svgEl('rect', {
      'class': 'viewer__ring' + (cls ? ' ' + cls : ''),
      x: bbox[0] - pad, y: bbox[1] - pad, width: bbox[2] + 2 * pad, height: bbox[3] + 2 * pad, rx: 5
    }));
  };

  Viewer.prototype.incident = function (node) {
    var list = (this.edgesByEnd[node.id] || []).slice();
    if (node.group) {
      (this.edgesByEnd[node.group] || []).forEach(function (e) { if (list.indexOf(e) === -1) list.push(e); });
    }
    return list;
  };

  // Select whatever an id names. opts: panel (open the properties), centre, focus,
  // fromPair (the other side asked; do not bounce back).
  Viewer.prototype.select = function (id, opts) {
    if (!this.svg) return;
    opts = opts || {};
    if (/^st-/.test(id) && this.els[id]) id = this.els[id].getAttribute('data-parent') || id;
    var hit = this.byId[id];
    if (!hit) return;
    this.clearHighlight();
    this.selected = hit;
    this.root.classList.add('viewer--focus');
    var item = hit.item;
    if (hit.kind === 'node') {
      this.mark(item.id, true);
      if (item.group) this.mark(item.group);
      this.incident(item).forEach(this.highlightEdge, this);
      this.ring(item.bbox);
      this.setCurrent(item.__order, opts.focus);
    } else if (hit.kind === 'edge') {
      this.mark(item.id, true);
      this.highlightEdge(item);
    } else if (hit.kind === 'group') {
      this.mark(item.id, true);
      this.markEnd(item.id);
      this.ring(item.bbox);
    } else {
      this.mark(item.id, true);
      this.ring(item.bbox);
    }
    if (opts.panel || !this.panel.hidden) this.showPanel(hit);
    if (opts.centre) this.reveal(this.boxOf(hit), { ifNeeded: !!opts.ifNeeded, sync: opts.sync });
    if (!opts.fromPair && this.pair) this.pair.mirrorSelection(this, hit, !!opts.centre);
  };

  Viewer.prototype.boxOf = function (hit) {
    var item = hit.item;
    if (validBox(item.bbox)) return item.bbox;
    var pts = Array.isArray(item.points) ? item.points.filter(function (p) {
      return p && num(p[0]) !== null && num(p[1]) !== null;
    }) : [];
    if (!pts.length) return null;
    var xs = pts.map(function (p) { return p[0]; }), ys = pts.map(function (p) { return p[1]; });
    var x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys);
    return [x0, y0, Math.max(1, Math.max.apply(null, xs) - x0), Math.max(1, Math.max.apply(null, ys) - y0)];
  };

  Viewer.prototype.setCurrent = function (i, focus) {
    if (i == null || !this.order.length) return;
    i = Math.max(0, Math.min(this.order.length - 1, i));
    var prev = this.order[this.current];
    if (prev && prev.el) prev.el.setAttribute('tabindex', '-1');
    this.current = i;
    var n = this.order[i];
    n.el.setAttribute('tabindex', '0');
    if (focus) {
      this.restoring = true;
      try { n.el.focus({ preventScroll: true }); } catch (e) { n.el.focus(); }
      this.restoring = false;
    }
  };

  // Arrowing between nodes. The properties follow the cursor only while they are open: a
  // screen reader already hears each node's name and change from its label, and Enter opens the
  // rest. Zoomed in, the view follows the cursor so focus never lands off screen.
  Viewer.prototype.moveTo = function (i) {
    if (!this.order.length) return;
    i = Math.max(0, Math.min(this.order.length - 1, i));
    var n = this.order[i];
    if (!this.panel.hidden) this.select(n.id, { focus: true });
    else this.setCurrent(i, true);
    if (this.isZoomed()) this.reveal(n.bbox, { ifNeeded: true });
  };

  // ---------- the properties panel ----------
  Viewer.prototype.endName = function (id) {
    if (!id) return 'nothing (unconnected)';
    var hit = this.byId[id];
    if (!hit && (/^a-/.test(id) || this.anchorOwner[id])) {
      var host = this.byId[this.anchorOwner[id]];
      return host ? 'the ' + (host.item.type_label || 'interaction') + ' ' + this.edgeEnds(host.item) : 'an anchor';
    }
    if (!hit) return id;
    if (hit.kind === 'node') return hit.item.label || '(unlabelled node)';
    if (hit.kind === 'group') return (GROUP_WORD[hit.item.style] || 'Group').toLowerCase();
    if (hit.kind === 'label') return '“' + (hit.item.text || '') + '”';
    return 'the ' + (hit.item.type_label || 'interaction') + ' ' + this.edgeEnds(hit.item);
  };

  Viewer.prototype.edgeEnds = function (e) {
    var self = this;
    function short(id) {
      var h = id && self.byId[id];
      if (h && h.kind === 'node') return h.item.label || '(unlabelled)';
      if (h && h.kind === 'group') return 'a group';
      return id ? 'a line' : 'nothing';
    }
    return 'from ' + short(e.source) + ' to ' + short(e.target);
  };

  Viewer.prototype.linkButton = function (id, text) {
    var b = el('button', 'node-panel__link', text);
    b.type = 'button';
    b.setAttribute('data-select', id);
    return b;
  };

  Viewer.prototype.showPanel = function (hit) {
    var body = this.panelBody;
    var item = hit.item;
    if (this.panel.hidden) {
      var a = document.activeElement;
      this.lastFocus = a && this.root.contains(a) && !this.panel.contains(a) ? a : null;
    }
    // Unhide before filling. The body is a polite live region, and a live region inside a
    // hidden subtree announces nothing.
    this.panel.hidden = false;
    body.textContent = '';
    var h = el('p', 'node-panel__title');
    h.tabIndex = -1;
    var dl = el('dl', 'node-panel__props');
    if (hit.kind === 'node') this.fillNode(h, dl, item);
    else if (hit.kind === 'edge') this.fillEdge(h, dl, item);
    else if (hit.kind === 'group') this.fillGroup(h, dl, item);
    else this.fillLabel(h, dl, item);
    body.appendChild(h);
    body.appendChild(dl);
    this.panel.setAttribute('aria-label', hit.kind === 'edge' ? 'Interaction properties'
      : hit.kind === 'group' ? 'Group properties' : 'Data node properties');
  };

  Viewer.prototype.fillNode = function (h, dl, node) {
    var self = this;
    // A node with no TextLabel is legal GPML and reads as an empty panel otherwise.
    h.textContent = node.label || '(unlabelled node)';
    if (CHANGE_WORD[node.__change]) {
      var tag = el('span', 'node-panel__change node-panel__change--' + node.__change,
        CHANGE_BADGE[node.__change] + ' ' + CHANGE_WORD[node.__change]);
      h.appendChild(document.createTextNode(' '));
      h.appendChild(tag);
    }
    labelRow(dl, 'Type', node.type || 'Unknown');
    if (node.identifier) {
      labelRow(dl, node.database || 'Identifier',
        isHttps(node.url) ? { href: node.url, text: node.identifier } : node.identifier);
    } else {
      // The single most common curation defect, and the reason a curator clicks a node at all.
      labelRow(dl, 'Identifier', 'Not annotated');
    }
    if (node.comment) labelRow(dl, 'Comment', node.comment);
    // What this node used to say, struck through beside what it says now (issue #24). A
    // re-annotation is invisible in the drawing, so the panel is the only place to read it.
    if (node.__was) {
      var was = node.__was;
      var wasXref = (was.database || was.identifier)
        ? ((was.database ? was.database + ':' : '') + (was.identifier || '')) : 'not annotated';
      labelRow(dl, 'Was', strike(node.__change === 'relabelled' ? (was.label || '(unlabelled)') : wasXref));
    }
    if (node.group && this.byId[node.group]) {
      var g = this.byId[node.group].item;
      labelRow(dl, 'In', this.linkButton(g.id, GROUP_WORD[g.style] || 'Group'));
    }
    if (Array.isArray(node.states) && node.states.length) {
      var ul = el('ul', 'node-panel__list');
      node.states.forEach(function (s) {
        var text = s.label || '(unlabelled state)';
        if (s.identifier) text += ' · ' + (s.database ? s.database + ':' : '') + s.identifier;
        ul.appendChild(el('li', null, text));
      });
      labelRow(dl, 'States', ul);
    }
    var edges = this.incident(node);
    if (edges.length) {
      var list = el('ul', 'node-panel__list');
      edges.forEach(function (e) {
        var out = e.source === node.id || (node.group && e.source === node.group);
        var other = out ? e.target : e.source;
        var word = e.type_label || e.type || 'line';
        var li = el('li');
        li.appendChild(self.linkButton(e.id, word + (out ? ' to ' : ' from ') + self.endName(other)));
        list.appendChild(li);
      });
      labelRow(dl, 'Interactions', list);
    }
  };

  Viewer.prototype.fillEdge = function (h, dl, e) {
    var self = this;
    h.textContent = e.type_label ? e.type_label.charAt(0).toUpperCase() + e.type_label.slice(1)
      : (e.kind === 'GraphicalLine' ? 'Graphical line' : 'Interaction');
    labelRow(dl, 'Kind', e.kind === 'GraphicalLine' ? 'Graphical line (no biological meaning)' : 'Interaction');
    if (e.type && e.type !== e.type_label) labelRow(dl, 'Arrow', e.type);
    labelRow(dl, 'From', e.source ? this.linkButton(this.targetOf(e.source, e.source_edge), this.endName(e.source)) : 'Nothing: unconnected');
    labelRow(dl, 'To', e.target ? this.linkButton(this.targetOf(e.target, e.target_edge), this.endName(e.target)) : 'Nothing: unconnected');
    if (e.identifier) {
      labelRow(dl, e.database || 'Identifier', e.identifier);
    }
    if (e.comment) labelRow(dl, 'Comment', e.comment);
    var onto = this.anchoredOn[e.id] || [];
    if (onto.length) {
      var ul = el('ul', 'node-panel__list');
      onto.forEach(function (a) {
        var li = el('li');
        var from = a.target_edge === e.id ? a.source : a.target;
        li.appendChild(self.linkButton(a.id, self.endName(from) + ' → ' + (a.type_label || 'line')));
        ul.appendChild(li);
      });
      labelRow(dl, 'Acting on it', ul);
    }
    if (Array.isArray(e.dangling) && e.dangling.length) {
      var warn = el('span', 'node-panel__warn',
        'The ' + e.dangling.join(' and ') + ' end is not attached to anything. In the drawing it may '
        + 'look connected; in the data it is not.');
      labelRow(dl, 'Warning', warn);
    }
  };

  // An anchor id is not selectable; the edge it sits on is.
  Viewer.prototype.targetOf = function (id, onEdge) {
    if (onEdge && this.byId[onEdge]) return onEdge;
    if (this.anchorOwner[id]) return this.anchorOwner[id];
    return id;
  };

  Viewer.prototype.fillGroup = function (h, dl, g) {
    var self = this;
    h.textContent = GROUP_WORD[g.style] || 'Group';
    var members = g.members || [];
    labelRow(dl, 'Members', String(members.length));
    if (members.length) {
      var ul = el('ul', 'node-panel__list');
      members.forEach(function (m) {
        var li = el('li');
        li.appendChild(self.linkButton(m, self.endName(m)));
        ul.appendChild(li);
      });
      labelRow(dl, 'Contains', ul);
    }
    if (g.parent && this.byId[g.parent]) labelRow(dl, 'Inside', this.linkButton(g.parent, 'the enclosing group'));
  };

  Viewer.prototype.fillLabel = function (h, dl, l) {
    h.textContent = l.text || '(empty label)';
    labelRow(dl, 'Kind', 'Label');
    if (isHttps(l.href)) labelRow(dl, 'Link', { href: l.href, text: l.href });
  };

  // opts.restoreFocus false: close without touching focus (the other side of a pair let go of
  // its selection; the reader is over there, on the node they just picked).
  Viewer.prototype.closePanel = function (opts) {
    if (this.panel.hidden) return false;
    // Focus goes back only if it was in this viewer, or on nothing because it sat in the panel
    // that is about to go. Anywhere else, the reader has moved on and taking it would be theft.
    var a = document.activeElement;
    var restore = !(opts && opts.restoreFocus === false)
      && (!a || a === document.body || this.root.contains(a));
    this.panel.hidden = true;
    if (restore) {
      // Hand focus back to where the reader was, not to <body> at the top of the page. If that
      // was a node, it is the node the arrows have since moved to, not the one the panel opened on.
      var node = this.order[this.current] && this.order[this.current].el;
      var back = this.lastFocus;
      if (!back || !this.root.contains(back) || (this.svg && this.svg.contains(back))) back = node || this.canvas;
      this.restoring = true;
      try { back.focus({ preventScroll: true }); } catch (e) { /* detached */ }
      this.restoring = false;
    }
    this.lastFocus = null;
    return true;
  };

  // ---------- search ----------
  Viewer.prototype.runSearch = function (q) {
    var self = this;
    while (this.marks && this.marks.querySelector('.viewer__match')) {
      this.marks.removeChild(this.marks.querySelector('.viewer__match'));
    }
    this.matches = [];
    this.matchIndex = -1;
    q = (q || '').trim().toLowerCase();
    if (!q || !this.svg) { this.say(''); return; }
    this.matches = this.order.filter(function (n) {
      var label = (n.label || '').toLowerCase();
      var id = (n.identifier || '').toLowerCase();
      var xref = ((n.database || '') + ':' + (n.identifier || '')).toLowerCase();
      return label.indexOf(q) !== -1 || (id && id.indexOf(q) !== -1) || (q.indexOf(':') !== -1 && xref.indexOf(q) !== -1);
    });
    this.matches.forEach(function (n) {
      var b = n.bbox, pad = 5;
      self.marks.appendChild(svgEl('rect', {
        'class': 'viewer__match', x: b[0] - pad, y: b[1] - pad,
        width: b[2] + 2 * pad, height: b[3] + 2 * pad, rx: 6
      }));
    });
    this.say(this.matches.length
      ? this.matches.length + (this.matches.length === 1 ? ' node matches' : ' nodes match') + '. Press Enter to go to ' + (this.matches.length === 1 ? 'it.' : 'each in turn.')
      : 'No node matches “' + q + '”.');
  };

  Viewer.prototype.nextMatch = function (back) {
    if (!this.matches.length) return;
    var len = this.matches.length;
    this.matchIndex = ((this.matchIndex + (back ? -1 : 1)) % len + len) % len;
    var n = this.matches[this.matchIndex];
    this.select(n.id, { panel: true, centre: true });
    this.say((this.matchIndex + 1) + ' of ' + len + ': ' + (n.label || '(unlabelled node)'));
  };

  // ---------- legend ----------
  Viewer.prototype.buildLegend = function () {
    var self = this;
    var box = this.legend;
    box.textContent = '';
    function section(title, counts, words) {
      var keys = Object.keys(counts);
      if (!keys.length) return;
      box.appendChild(el('p', 'viewer__legend-title', title));
      var ul = el('ul', 'viewer__legend-list');
      keys.sort().forEach(function (k) {
        var li = el('li', 'viewer__legend-item');
        li.textContent = (words && words[k] ? words[k] : k) + ' (' + counts[k] + ')';
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    function tally(list, key) {
      var out = {};
      list.forEach(function (x) { var k = key(x); if (k) out[k] = (out[k] || 0) + 1; });
      return out;
    }
    if (this.diff) {
      var counts = tally(this.graph.nodes, function (n) { return CHANGE_WORD[n.__change] ? n.__change : null; });
      box.appendChild(el('p', 'viewer__legend-title', 'Changes on this side'));
      var ul = el('ul', 'diff-legend viewer__legend-diff');
      CHANGE_ORDER.forEach(function (k) {
        if (!counts[k]) return;
        var li = el('li', 'diff-legend__item diff-legend__item--' + k);
        var sw = el('span', 'diff-legend__swatch');
        sw.setAttribute('aria-hidden', 'true');
        li.appendChild(sw);
        li.appendChild(document.createTextNode(CHANGE_BADGE[k] + ' ' + counts[k] + ' ' + CHANGE_WORD[k]));
        ul.appendChild(li);
      });
      if (!ul.children.length) ul.appendChild(el('li', null, 'No data node changed on this side.'));
      box.appendChild(ul);
      if (counts.moved) {
        // Same switch as the one in the summary above the diagrams; both drive the card.
        var lab = el('label', 'viewer__legend-toggle');
        var cb = el('input', 'diff-hide-moved');
        cb.type = 'checkbox';
        var card = this.root.closest('.review-card');
        cb.checked = !(card && card.classList.contains('review-card--hide-moved'));
        lab.appendChild(cb);
        lab.appendChild(el('span', null, 'Show nodes that only moved'));
        box.appendChild(lab);
      }
    }
    section('Data nodes', tally(this.graph.nodes, function (n) { return n.type || 'Unknown'; }));
    section('Interactions', tally(this.graph.edges, function (e) {
      return e.type_label || e.type || (e.kind === 'GraphicalLine' ? 'graphical line' : 'line');
    }));
    section('Groups', tally(this.graph.groups, function (g) { return g.style || 'group'; }), GROUP_WORD);
    if (this.danglingCount) {
      var t = el('label', 'viewer__legend-toggle');
      var c = el('input', 'viewer__ends-toggle');
      c.type = 'checkbox';
      c.checked = this.root.classList.contains('viewer--ends');
      t.appendChild(c);
      t.appendChild(el('span', null, 'Show unconnected line ends (' + this.danglingCount + ')'));
      box.appendChild(t);
      c.addEventListener('change', function () { self.root.classList.toggle('viewer--ends', c.checked); });
    }
    if (this.graph.legacy) {
      box.appendChild(el('p', 'viewer__legend-note',
        'This render predates the interactive viewer: only data nodes can be selected until it is drawn again.'));
    }
    if (!box.children.length) box.appendChild(el('p', 'viewer__legend-note', 'Nothing drawn here to explain.'));
  };

  // ---------- Database render ----------
  // The repository's own drawing, for comparison. An <img> of a same-origin copy, never inlined:
  // it is somebody else's SVG and this viewer only vouches for the one it sanitised.
  Viewer.prototype.showSource = function (db) {
    var root = this.root;
    if (db && !this.dbBox) {
      this.dbBox = el('div', 'viewer__db');
      var scroller = el('div', 'viewer__db-scroll');
      this.dbImg = el('img', 'viewer__db-img');
      this.dbImg.alt = "The pathway as drawn by the repository's pipeline";
      this.dbImg.src = this.renderUrl;
      this.dbImg.draggable = false;
      scroller.appendChild(this.dbImg);
      this.dbBox.appendChild(scroller);
      this.dbBox.appendChild(el('p', 'viewer__caption', "Drawn by the repository's pipeline"));
      this.canvas.parentNode.insertBefore(this.dbBox, this.canvas.nextSibling);
    }
    root.classList.toggle('viewer--db', !!db);
    this.canvas.hidden = !!db;
    if (this.dbBox) this.dbBox.hidden = !db;
    root.querySelectorAll('[data-v="source-portal"], [data-v="source-db"]').forEach(function (b) {
      b.setAttribute('aria-pressed', String((b.getAttribute('data-v') === 'source-db') === !!db));
    });
    if (db) { this.closePanel(); this.zoomDb(1); }
  };

  Viewer.prototype.zoomDb = function (factor) {
    if (!this.dbImg) return;
    this.dbZoom = Math.max(1, Math.min(8, this.dbZoom * factor));
    this.dbImg.style.width = (this.dbZoom * 100) + '%';
  };

  // ---------- full screen ----------
  Viewer.prototype.fullscreenTarget = function () {
    var pairEl = this.root.closest('[data-viewer-pair]');
    return pairEl && pairEl.querySelectorAll('[data-viewer]').length > 1 ? pairEl : this.root;
  };

  Viewer.prototype.toggleFullscreen = function () {
    var target = this.fullscreenTarget();
    var current = document.fullscreenElement || document.webkitFullscreenElement;
    if (current) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      return;
    }
    var req = target.requestFullscreen || target.webkitRequestFullscreen;
    if (!req) {
      if (window.WP) window.WP.toast('This browser cannot show the diagram full screen.', 'error');
      return;
    }
    if (this.pair) this.pair.each(null, function (v) { v.mount(); });
    var p = req.call(target);
    if (p && p.catch) p.catch(function () { /* refused, e.g. not from a user gesture */ });
  };

  // ---------- change list (diff mode) ----------
  Viewer.prototype.updateChangeButtons = function () {
    var list = this.pair ? this.pair.changes : [];
    this.root.querySelectorAll('[data-v="prev-change"], [data-v="next-change"]').forEach(function (b) {
      b.disabled = !!(this.svg && !list.length);
    }, this);
    // A disabled button cannot take focus, so it cannot be the toolbar's one tab stop either.
    if (!this.toolbarButtons().some(function (b) { return b.tabIndex === 0; })) {
      var first = this.toolbarButtons()[0];
      if (first) this.setToolbarStop(first, false);
    }
  };

  // ---------- toolbar ----------
  // A roving tabindex, as over the nodes: one stop for the toolbar, the arrows for the rest.
  Viewer.prototype.toolbarButtons = function () {
    return Array.prototype.filter.call(this.root.querySelectorAll('.viewer__toolbar button'), function (b) {
      return !b.disabled && !b.hidden;
    });
  };

  Viewer.prototype.setToolbarStop = function (btn, focus) {
    this.root.querySelectorAll('.viewer__toolbar button').forEach(function (b) {
      b.setAttribute('tabindex', b === btn ? '0' : '-1');
    });
    if (focus) btn.focus();
  };

  Viewer.prototype.roveToolbar = function (from, key) {
    var list = this.toolbarButtons();
    var n = list.length;
    if (!n) return;
    var i = list.indexOf(from);
    var j = key === 'Home' ? 0 : key === 'End' ? n - 1
      : ((i + (key === 'ArrowRight' ? 1 : -1)) % n + n) % n;
    this.setToolbarStop(list[j], true);
  };

  Viewer.prototype.stepChange = function (dir) {
    if (this.pair) this.pair.step(dir, this);
  };

  // ---------- events ----------
  Viewer.prototype.wire = function () {
    var self = this;
    var root = this.root;
    var canvas = this.canvas;
    // Every listener is kept so destroy() can take it off again. unmountAll() then mountAll() on
    // the same element would otherwise leave the old handlers running next to the new ones.
    var listeners = this.listeners = [];
    function on(target, type, fn, opts) {
      target.addEventListener(type, fn, opts);
      listeners.push([target, type, fn, opts]);
    }

    // Lite mode (the queue): nothing is fetched beyond the <img> until somebody reaches for the
    // diagram. A queue of forty cards would otherwise fire some eighty requests on load for
    // diagrams nobody is looking at.
    if (this.mode === 'lite') {
      var activate = function (e) {
        if (e.type === 'wheel' && !(e.ctrlKey || e.metaKey)) return;
        ['pointerenter', 'focusin', 'click', 'wheel'].forEach(function (t) { root.removeEventListener(t, activate); });
        self.activate();
      };
      ['pointerenter', 'focusin', 'click'].forEach(function (t) { on(root, t, activate); });
      on(root, 'wheel', activate, { passive: true });
    }

    on(root, 'click', function (e) {
      var btn = e.target.closest('[data-v]');
      if (btn && root.contains(btn) && btn.tagName === 'BUTTON') { self.onButton(btn.getAttribute('data-v'), btn); return; }
      var link = e.target.closest('[data-select]');
      if (link && self.panel.contains(link)) {
        var id = link.getAttribute('data-select');
        self.activate().then(function () {
          self.select(id, { panel: true, centre: true, ifNeeded: true });
          var t = self.panelBody.querySelector('.node-panel__title');
          if (t) t.focus();
        });
        return;
      }
      if (e.target.closest('.node-panel__close')) self.closePanel();
    });

    if (this.search) {
      on(this.search, 'input', function () {
        self.activate().then(function () { self.runSearch(self.search.value); });
      });
      on(this.search, 'keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); self.nextMatch(e.shiftKey); }
        else if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          self.search.value = '';
          self.runSearch('');
          canvas.focus();
        }
      });
    }

    on(canvas, 'wheel', function (e) {
      if (!self.svg) {
        // Ctrl+wheel and a trackpad pinch are how a lite viewer is woken, and the drawing takes a
        // moment to arrive. Left alone, those first events zoom the whole page instead. Dropped,
        // not queued: the next one zooms the diagram. A viewer that gave up on its drawing hands
        // the gesture back to the browser.
        if ((e.ctrlKey || e.metaKey) && !root.classList.contains('viewer--static')) e.preventDefault();
        return;
      }
      // A queue card is mostly diagram, so swallowing every wheel event would trap the page
      // scroll. Ctrl/Meta+wheel (also what a trackpad pinch sends) zooms from any scale; a plain
      // wheel only once zoomed in and navigating inside the diagram.
      if (!(e.ctrlKey || e.metaKey) && !self.isZoomed()) return;
      e.preventDefault();
      self.zoomAt(Math.pow(1.0015, -e.deltaY * (e.deltaMode === 1 ? 16 : 1)), e.clientX, e.clientY);
    }, { passive: false });

    var drag = null;
    on(canvas, 'pointerdown', function (e) {
      if (!self.svg || (e.pointerType === 'mouse' && e.button !== 0)) return;
      self.pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(self.pointers);
      if (ids.length === 1) {
        drag = { x: e.clientX, y: e.clientY, moved: false, target: e.target, id: e.pointerId };
      } else if (ids.length === 2) {
        var a = self.pointers[ids[0]], b = self.pointers[ids[1]];
        drag = { pinch: Math.hypot(a.x - b.x, a.y - b.y) || 1, moved: true };
        try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* gone */ }
      }
    });
    on(canvas, 'pointermove', function (e) {
      if (!self.svg) return;
      if (!self.pointers[e.pointerId]) { self.hover(e); return; }
      var last = self.pointers[e.pointerId];
      self.pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(self.pointers);
      if (ids.length === 2 && drag && drag.pinch) {
        var a = self.pointers[ids[0]], b = self.pointers[ids[1]];
        var d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        self.zoomAt(d / drag.pinch, (a.x + b.x) / 2, (a.y + b.y) / 2);
        drag.pinch = d;
        return;
      }
      if (!drag || drag.pinch) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return;
      // Panning only means something once zoomed in; at fit the drawing is all on screen.
      if (!self.isZoomed()) return;
      if (!drag.moved) {
        drag.moved = true;
        try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* gone */ }
        root.classList.add('viewer--panning');
        self.hideTooltip();
      }
      self.panBy(e.clientX - last.x, e.clientY - last.y);
    });
    function end(e) {
      if (!self.pointers[e.pointerId]) return;
      delete self.pointers[e.pointerId];
      root.classList.remove('viewer--panning');
      try { canvas.releasePointerCapture(e.pointerId); } catch (err) { /* not captured */ }
      if (e.type === 'pointerup' && drag && !drag.moved && drag.id === e.pointerId) self.clickAt(drag.target);
      if (!Object.keys(self.pointers).length) drag = null;
    }
    on(canvas, 'pointerup', end);
    on(canvas, 'pointercancel', end);
    on(canvas, 'pointerleave', function () { self.hideTooltip(); });

    on(root, 'keydown', function (e) { self.onKey(e); });

    // Whichever toolbar button was used last, by mouse or keyboard, is where Tab comes back to.
    on(root, 'focusin', function (e) {
      var b = e.target;
      if (b.tagName === 'BUTTON' && b.closest('.viewer__toolbar') && b.tabIndex !== 0) self.setToolbarStop(b, false);
    });

    // Tabbing onto a node makes it the roving stop, wherever it was in the order.
    on(canvas, 'focusin', function (e) {
      var t = e.target.closest && e.target.closest('[data-id]');
      if (!t || !self.byId) return;
      var hit = self.byId[t.getAttribute('data-id')];
      if (hit && hit.kind === 'node' && !self.restoring) self.setCurrent(hit.item.__order, false);
    });
  };

  Viewer.prototype.activate = function () {
    var self = this;
    if (this.pair) this.pair.each(this, function (v) { v.mount(); });
    return this.mount().then(function () { return self.svg != null; });
  };

  Viewer.prototype.onButton = function (what, btn) {
    var self = this;
    if (what === 'source-db') { this.showSource(true); return; }
    if (what === 'source-portal') { this.showSource(false); return; }
    if (what === 'fullscreen') { this.toggleFullscreen(); return; }
    if (what === 'legend') {
      var open = this.legend.hidden;
      this.legend.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
      return;
    }
    this.activate().then(function (ok) {
      if (!ok) return;
      if (what === 'zoom-in') self.zoomCentre(1.4);
      else if (what === 'zoom-out') self.zoomCentre(1 / 1.4);
      else if (what === 'fit') self.fitView();
      else if (what === 'next-change') self.stepChange(1);
      else if (what === 'prev-change') self.stepChange(-1);
    });
  };

  Viewer.prototype.clickAt = function (target) {
    var t = target && target.closest ? target.closest('[data-id]') : null;
    var id = t && this.svg.contains(t) ? t.getAttribute('data-id') : null;
    if (id && /^st-/.test(id)) id = t.getAttribute('data-parent');
    if (!id || !this.byId[id]) {
      // Empty canvas: let go of the selection, as clicking off a selection does anywhere else.
      this.clearHighlight();
      this.closePanel();
      return;
    }
    this.select(id, { panel: true });
  };

  Viewer.prototype.hover = function (e) {
    var t = e.target.closest ? e.target.closest('[data-id]') : null;
    var hit = t && this.svg.contains(t) ? this.byId[t.getAttribute('data-id')] : null;
    if (t && !hit && /^st-/.test(t.getAttribute('data-id'))) hit = this.byId[t.getAttribute('data-parent')];
    if (!hit) { this.hideTooltip(); return; }
    var item = hit.item, text;
    if (hit.kind === 'node') {
      text = (item.label || '(unlabelled node)') + ' · ' + (item.type || 'Unknown')
        + ' · ' + (item.identifier ? (item.database ? item.database + ':' : '') + item.identifier : 'not annotated');
      if (CHANGE_WORD[item.__change]) text += ' · ' + CHANGE_WORD[item.__change];
    } else if (hit.kind === 'edge') {
      text = (item.type_label || item.type || 'line') + ' ' + this.edgeEnds(item);
    } else if (hit.kind === 'group') {
      text = (GROUP_WORD[item.style] || 'Group') + ' of ' + (item.members || []).length;
    } else {
      text = item.text || 'Label';
    }
    var tip = this.tooltip;
    tip.textContent = text;
    tip.hidden = false;
    var r = this.root.getBoundingClientRect();
    var x = e.clientX - r.left + 14, y = e.clientY - r.top + 16;
    var w = tip.offsetWidth;
    if (x + w > r.width - 4) x = Math.max(4, e.clientX - r.left - w - 10);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  };

  Viewer.prototype.hideTooltip = function () {
    if (this.tooltip) this.tooltip.hidden = true;
  };

  Viewer.prototype.onKey = function (e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t.closest('input, textarea, select')) return;
    var key = e.key;
    if (key === 'Escape') {
      if (this.closePanel()) { e.stopPropagation(); return; }
      if (this.selected || this.matches.length) {
        this.clearHighlight();
        e.stopPropagation();
        return;
      }
      return; // the browser leaves full screen on its own
    }
    var nodeEl = this.svg && t.closest('[data-id]');
    var onNode = nodeEl && this.byId[nodeEl.getAttribute('data-id')]
      && this.byId[nodeEl.getAttribute('data-id')].kind === 'node';
    var onButton = t.closest('button, a');
    if (onButton && onButton.tagName === 'BUTTON' && onButton.closest('.viewer__toolbar')
      && (key === 'ArrowLeft' || key === 'ArrowRight' || key === 'Home' || key === 'End')) {
      this.roveToolbar(onButton, key);
      e.preventDefault();
      return;
    }
    var handled = true;
    if (/^Arrow/.test(key)) {
      if (onNode) {
        this.moveTo(this.current + (key === 'ArrowRight' || key === 'ArrowDown' ? 1 : -1));
      } else if (this.svg && this.isZoomed() && !onButton) {
        this.panBy(key === 'ArrowLeft' ? PAN_STEP : key === 'ArrowRight' ? -PAN_STEP : 0,
          key === 'ArrowUp' ? PAN_STEP : key === 'ArrowDown' ? -PAN_STEP : 0);
      } else {
        // Nothing to pan to: leave the arrows to scroll the page.
        handled = false;
      }
    } else if ((key === 'Home' || key === 'End') && onNode) {
      this.moveTo(key === 'Home' ? 0 : this.order.length - 1);
    } else if ((key === 'Enter' || key === ' ') && onNode) {
      this.select(nodeEl.getAttribute('data-id'), { panel: true });
    } else if (key === '+' || key === '=') {
      this.onButton('zoom-in');
    } else if (key === '-' || key === '_') {
      this.onButton('zoom-out');
    } else if (key === '0') {
      this.onButton('fit');
    } else if (key === 'f' || key === 'F') {
      this.toggleFullscreen();
    } else if (key === '/' && this.search) {
      this.search.focus();
    } else if ((key === 'n' || key === 'N') && this.diffUrl) {
      this.onButton('next-change');
    } else if ((key === 'p' || key === 'P') && this.diffUrl) {
      this.onButton('prev-change');
    } else {
      handled = false;
    }
    if (handled) e.preventDefault();
  };

  Viewer.prototype.destroy = function () {
    this.destroyed = true;
    cancelAnimationFrame(this.anim);
    if (this.abort) { try { this.abort.abort(); } catch (e) { /* already done */ } }
    (this.listeners || []).forEach(function (l) { l[0].removeEventListener(l[1], l[2], l[3]); });
    this.listeners = [];
    if (this.svg && this.svg.parentNode) this.svg.parentNode.removeChild(this.svg);
    // Anything still holding this instance (a pending promise, a frame) finds nothing to draw on.
    this.svg = null;
    if (this.img) this.img.hidden = false;
    // Back to the markup the server sent, so a later mountAll() starts from the same place.
    this.panel.hidden = true;
    this.panelBody.textContent = '';
    this.legend.hidden = true;
    this.legend.textContent = '';
    this.root.querySelectorAll('[data-v="legend"]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    this.hideTooltip();
    this.root.classList.remove('viewer--ready', 'viewer--loading', 'viewer--focus', 'viewer--zoomed');
    if (this.pair) this.pair.remove(this);
    delete this.root.__wpViewer;
    var i = mounted.indexOf(this);
    if (i !== -1) mounted.splice(i, 1);
  };

  // ---------- the before/after pair ----------
  // Both sides of an update, kept in step: the same view in user units (GPML coordinates are
  // shared across an edit, so the same box shows the same part of the pathway), a selection on
  // one side selects its counterpart on the other, and one change list runs across both.
  function Pair(viewers) {
    this.viewers = viewers;
    this.changes = [];
    this.index = -1;
    this.syncing = false;
    viewers.forEach(function (v) { v.pair = this; }, this);
  }

  Pair.prototype.each = function (except, fn) {
    this.viewers.forEach(function (v) { if (v !== except && !v.destroyed) fn(v); });
  };

  Pair.prototype.remove = function (v) {
    this.viewers = this.viewers.filter(function (x) { return x !== v; });
    v.pair = null;
  };

  Pair.prototype.follow = function (src) {
    if (this.syncing) return;
    this.syncing = true;
    try {
      this.each(src, function (v) { if (v.svg) v.setBox(src.vb, { raw: true, sync: false }); });
    } finally {
      this.syncing = false;
    }
  };

  // The two sides share one viewBox, so the counterpart is never centred on its own: that would
  // leave the sides showing different parts of the pathway. When the source is centring, the
  // shared view already follows it. When it is not (a click, a keypress) and the counterpart sits
  // outside the view, as a node that moved can, the counterpart's side pans and the source
  // follows. A side that only lets go of its selection never takes focus: the reader is on the
  // other side, on the node they just picked.
  Pair.prototype.mirrorSelection = function (src, hit, srcCentring) {
    if (this.syncing) return;
    this.syncing = true;
    var pan = null;
    try {
      this.each(src, function (v) {
        if (!v.svg) return;
        var other = hit.kind === 'node' ? src.counterpart[hit.item.id] : null;
        if (other && v.byId[other]) {
          v.select(other, { fromPair: true });
          var box = v.boxOf(v.byId[other]);
          if (!srcCentring && !pan && box && !v.shows(box)) pan = { viewer: v, box: box };
        } else {
          v.clearHighlight();
          v.closePanel({ restoreFocus: false });
        }
      });
    } finally {
      this.syncing = false;
    }
    // Outside the guard, so follow() can drag the source along.
    if (pan) pan.viewer.reveal(pan.box, {});
  };

  // The change list in reading order across both sides: what changed on the "after" side, and
  // what was removed from the "before" side, which exists nowhere else. A changed node that has
  // a counterpart is listed once, on the side where it now lives.
  Pair.prototype.rebuild = function () {
    var items = [];
    var card = this.viewers[0] && this.viewers[0].root.closest('.review-card');
    var hideMoved = !!(card && card.classList.contains('review-card--hide-moved'));
    this.viewers.forEach(function (v) {
      if (!v.svg || !v.diff) return;
      v.graph.nodes.forEach(function (n) {
        if (!CHANGE_WORD[n.__change]) return;
        if (hideMoved && n.__change === 'moved') return;
        if (v.side === 'before' && n.__change !== 'removed') return;
        items.push({ viewer: v, node: n });
      });
    });
    this.changes = readingOrder(items, function (it) { return it.node.bbox; });
    this.index = -1;
    this.viewers.forEach(function (v) { v.updateChangeButtons(); });
  };

  Pair.prototype.step = function (dir, from) {
    if (!this.changes.length) {
      from.say('Nothing changed on the data nodes here.');
      return;
    }
    var len = this.changes.length;
    this.index = ((this.index + dir) % len + len) % len;
    var it = this.changes[this.index];
    it.viewer.select(it.node.id, { panel: true, centre: true, focus: true });
    it.viewer.say('Change ' + (this.index + 1) + ' of ' + len + ': '
      + (it.node.label || '(unlabelled node)') + ', ' + CHANGE_WORD[it.node.__change]
      + (it.viewer.side === 'before' ? ' (before)' : ''));
  };

  // ---------- public API ----------
  function viewersIn(root) {
    if (!root) return [];
    var list = [];
    if (root.matches && root.matches('[data-viewer]')) list.push(root);
    if (root.querySelectorAll) list.push.apply(list, root.querySelectorAll('[data-viewer]'));
    return list;
  }

  function mountAll(root) {
    var made = [];
    viewersIn(root || document).forEach(function (r) {
      if (r.__wpViewer || !r.getAttribute('data-svg-url')) return;
      // Inside a closed <details> ("Review details" under the publish tracker) the canvas has no
      // size to fit the drawing to. It is mounted when the panel opens (the toggle listener).
      if (r.closest('details:not([open])')) return;
      var v = new Viewer(r);
      mounted.push(v);
      made.push(v);
    });
    // Pair the two sides of each update before anything loads, so a lite card wakes both.
    var pairs = [];
    made.forEach(function (v) {
      var box = v.root.closest('[data-viewer-pair]');
      if (!box || pairs.indexOf(box) !== -1) return;
      pairs.push(box);
      var vs = Array.prototype.map.call(box.querySelectorAll('[data-viewer]'), function (r) { return r.__wpViewer; })
        .filter(Boolean);
      // A lone side with a diff still gets a Pair: it owns the change list N and P walk.
      if (vs.length > 1 || (vs.length === 1 && vs[0].diffUrl)) new Pair(vs); // eslint-disable-line no-new
    });
    made.forEach(function (v) { if (v.mode !== 'lite') v.mount(); });
    return made.length;
  }

  function unmountAll(root) {
    viewersIn(root || document).forEach(function (r) { if (r.__wpViewer) r.__wpViewer.destroy(); });
  }

  // Link two viewers by hand (their root elements or the instances).
  function link(a, b) {
    var va = a && (a.__wpViewer || a), vb = b && (b.__wpViewer || b);
    if (!(va instanceof Viewer) || !(vb instanceof Viewer) || va === vb) return null;
    if (va.pair) va.pair.remove(va);
    if (vb.pair) vb.pair.remove(vb);
    var p = new Pair([va, vb]);
    p.rebuild();
    return p;
  }

  // ---------- page-wide listeners ----------
  // A refreshed card (refresh.js) arrives as new markup: the viewers inside the old one are
  // detached and done with, and the new ones need mounting.
  document.addEventListener('wp:card-replaced', function (e) {
    mounted.slice().forEach(function (v) { if (!v.root.isConnected) v.destroy(); });
    if (e.detail) mountAll(e.detail);
  });

  // A <details> opening may reveal viewers that were left unmounted while it was closed. Toggle
  // does not bubble, hence the capture. mountAll skips anything already mounted.
  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (d && d.tagName === 'DETAILS' && d.open && d.isConnected) mountAll(d);
  }, true);

  document.addEventListener('fullscreenchange', function () {
    var fs = document.fullscreenElement;
    mounted.forEach(function (v) {
      var on = !!fs && (fs === v.root || fs.contains(v.root));
      v.root.classList.toggle('viewer--fullscreen', on);
      var b = v.root.querySelector('[data-v="fullscreen"]');
      if (b) {
        b.setAttribute('aria-pressed', String(on));
        b.textContent = on ? 'Exit full screen' : 'Full screen';
      }
    });
  });

  // A node that only moved is the category issue #24 itself calls noise, and after a re-layout
  // it can be every node on the diagram. The node stays selectable either way; only its outline
  // goes, so hiding the noise never hides the node behind it.
  document.addEventListener('change', function (e) {
    var box = e.target.closest && e.target.closest('.diff-hide-moved');
    if (!box) return;
    var card = box.closest('.review-card');
    if (!card) return;
    card.classList.toggle('review-card--hide-moved', !box.checked);
    card.querySelectorAll('.diff-hide-moved').forEach(function (other) { other.checked = box.checked; });
    var seen = [];
    card.querySelectorAll('[data-viewer]').forEach(function (r) {
      var v = r.__wpViewer;
      if (v && v.pair && seen.indexOf(v.pair) === -1) { seen.push(v.pair); v.pair.rebuild(); }
    });
  });

  window.WPViewer = { mountAll: mountAll, unmountAll: unmountAll, link: link };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { mountAll(document); });
  } else {
    mountAll(document);
  }
})();

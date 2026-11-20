// tour.js: a short guided tour of each page, for someone using the portal for the first time.
//
// Steps are keyed by <main data-tour-page="submit|review|queue"> and point at elements carrying
// data-tour="<name>". A step whose element is absent or hidden is skipped, which is how one list
// serves curators and submitters (only curators have the decision buttons) and pages before and
// after a feature lands. No overlay blocks the page: the target gets an outline and the popover
// sits beside it, so the tour never stands between someone and what they came to do.
//
// Starts by itself once per page for a signed-in visitor on a screen at least 700px wide; narrower
// screens get a small "Take the tour" prompt instead, because a popover over a phone-width form
// covers the thing it is describing. ?tour=1 or any [data-tour-start] link starts it on demand.
// Loaded after app.js (window.WP); uses nothing from it.
(function () {
  'use strict';

  var STEPS = {
    submit: [
      { anchor: 'submit-file', title: 'Choose your pathway',
        body: 'Pick the .gpml file you saved from PathVisio. Nothing is sent yet: the portal checks the file and lists anything worth fixing right below.' },
      { anchor: 'preflight', title: 'What the checks found',
        body: 'Only a "Blocked" finding stops a submission. The rest is advice; fixing it now saves a round trip with a curator.' },
      { anchor: 'note', title: 'A note for the curators',
        body: 'Optional. Say where the pathway comes from, what it covers or what you are unsure about. Curators read it first.' },
      { anchor: 'submit', title: 'Submit',
        body: 'Opens a pull request in your name. Nothing is published until a curator approves it.' },
      { anchor: 'tab-update', title: 'Changing a published pathway?',
        body: 'Use Update existing: type the WPID, download the current version, make your edit on that copy and upload it.' },
      { anchor: 'nav-mine', title: 'Your submissions',
        body: 'Every pathway you have sent, with its state. Open one to follow the review, answer a curator or upload a fix.' },
      { anchor: 'nav-help', title: 'Help',
        body: 'How submitting and reviewing work, what each check means, and what to do when something goes wrong. The footer can restart this tour.' }
    ],
    review: [
      { anchor: 'refresh', title: 'Up to date',
        body: 'When the portal last checked GitHub. Refresh updates this review without reloading the page; while something is running it refreshes itself.' },
      { anchor: 'tracker', title: 'Publication progress',
        body: 'After approval: accepted, the publish workflow, published with its WPID, and live on the site.' },
      { anchor: 'quality', title: 'Automated checks',
        body: 'What the portal found in the uploaded file, problems first. Where the repository runs the same check, its answer is shown too.' },
      { anchor: 'diff-summary', title: 'What changed',
        body: 'For an update: the data nodes added, removed, re-annotated or moved, and any change to the title, description or references.' },
      { anchor: 'viewer', title: 'The pathway',
        body: 'Drag to move; use + and −, or Ctrl and the mouse wheel, to zoom. Click a node to see its identifier and interactions; its neighbours light up.' },
      { anchor: 'viewer-toolbar', title: 'Diagram tools',
        body: 'Fit, fullscreen, search by label or identifier, and a legend. For an update, step through the changes one by one.' },
      { anchor: 'checklist', title: 'Curation checklist',
        body: 'What a curator answers before approving. Items marked "auto" were suggested from the file; required items carry a dot.' },
      { anchor: 'actions', title: 'Decide',
        body: 'Approve, request changes or reject. Approve unlocks once every required item is marked Pass.' },
      { anchor: 'revise', title: 'Upload a fix',
        body: 'Asked for changes? Choose the corrected file here. It goes onto the same pull request and the review returns to the queue.' },
      { anchor: 'conversation', title: 'Conversation',
        body: 'The comments on the pull request. Writing here posts on GitHub under your own name; it does not change the review.' }
    ],
    queue: [
      { anchor: 'queue-filter', title: 'Filter the queue',
        body: 'One tab per state. "Mine" and, for curators, "Assigned to me" narrow it to your own work.' },
      { selector: '.review-list .review-card', title: 'One card per submission',
        body: 'The pathway, its state and age, a summary of the checks and a small diagram that becomes interactive when you point at it.' },
      { selector: '.review-list .review-card__expand', title: 'Open the full review',
        body: 'The full page has the large diagram, the checklist, the decision buttons and the conversation.' },
      { anchor: 'nav-help', title: 'Help',
        body: 'What every state, check and checklist item means, and how long publication takes.' }
    ]
  };

  var STORE_PREFIX = 'wpTour.v1.';
  var AUTO_MIN_WIDTH = 700;
  var GAP = 12;
  var MARGIN = 8;

  var main = document.querySelector('main[data-tour-page]');
  var page = main ? main.getAttribute('data-tour-page') : '';
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Storage can be missing or throw (private windows, blocked site data). The tour then simply
  // forgets that it ran, which costs one extra auto-start, never a broken page.
  function stored() {
    try { return window.localStorage.getItem(STORE_PREFIX + page); } catch (e) { return null; }
  }
  function remember(value) {
    try { window.localStorage.setItem(STORE_PREFIX + page, value); } catch (e) { /* ignore */ }
  }

  function visible(el) {
    return !!el && el.isConnected && !el.closest('[hidden]') && el.getClientRects().length > 0;
  }

  function locate(step) {
    var el = step.anchor
      ? document.querySelector('[data-tour="' + step.anchor + '"]')
      : document.querySelector(step.selector);
    return visible(el) ? el : null;
  }

  // The steps that have something to point at right now, in order, never the same element twice.
  function available() {
    var out = [];
    var seen = [];
    (STEPS[page] || []).forEach(function (step) {
      var el = locate(step);
      if (el && seen.indexOf(el) === -1) { seen.push(el); out.push(step); }
    });
    return out;
  }

  // ---------- the popover ----------

  var pop = null;
  var steps = [];
  var index = 0;
  var target = null;
  var returnFocus = null;
  var frame = 0;

  function button(cls, text) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.textContent = text;
    return b;
  }

  function build() {
    pop = document.createElement('div');
    pop.className = 'tour';
    pop.setAttribute('role', 'dialog');
    // Not modal: the page stays usable, and a screen reader is not told otherwise.
    pop.setAttribute('aria-modal', 'false');
    pop.setAttribute('aria-labelledby', 'tour-title');
    pop.setAttribute('aria-describedby', 'tour-body');
    pop.tabIndex = -1;

    var count = document.createElement('p');
    count.className = 'tour__count';
    var title = document.createElement('h2');
    title.className = 'tour__title';
    title.id = 'tour-title';
    var body = document.createElement('p');
    body.className = 'tour__body';
    body.id = 'tour-body';

    var actions = document.createElement('div');
    actions.className = 'tour__actions';
    var skip = button('tour__skip', 'Skip tour');
    var nav = document.createElement('span');
    nav.className = 'tour__nav';
    var back = button('btn btn--ghost btn--sm tour__back', 'Back');
    var next = button('btn btn--primary btn--sm tour__next', 'Next');
    nav.appendChild(back);
    nav.appendChild(next);
    actions.appendChild(skip);
    actions.appendChild(nav);

    var arrow = document.createElement('span');
    arrow.className = 'tour__arrow';
    arrow.setAttribute('aria-hidden', 'true');

    pop.appendChild(count);
    pop.appendChild(title);
    pop.appendChild(body);
    pop.appendChild(actions);
    pop.appendChild(arrow);

    skip.addEventListener('click', function () { finish('dismissed'); });
    back.addEventListener('click', function () { go(index - 1); });
    next.addEventListener('click', function () { go(index + 1); });
    pop.addEventListener('keydown', function (e) {
      // Enter on Back or Skip does what that button says; anywhere else in the popover it moves on.
      if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); go(index + 1); }
    });
    document.body.appendChild(pop);
  }

  function onKey(e) {
    if (e.key === 'Escape' && pop) { e.preventDefault(); finish('dismissed'); }
  }

  function schedule() {
    if (frame) return;
    frame = window.requestAnimationFrame(function () { frame = 0; place(); });
  }

  function place() {
    if (!pop) return;
    if (!visible(target)) {
      // A card refreshed under the tour replaces its elements; follow the step to the new one.
      var again = locate(steps[index]);
      if (!again) { go(index + 1); return; }
      mark(again);
    }
    if (window.innerWidth < 520) {
      // A bottom sheet on a phone (tour.css); there is no room beside anything.
      pop.style.top = '';
      pop.style.left = '';
      pop.setAttribute('data-placement', 'sheet');
      return;
    }
    var r = target.getBoundingClientRect();
    var w = pop.offsetWidth;
    var h = pop.offsetHeight;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var below = vh - r.bottom - GAP;
    var above = r.top - GAP;
    // Below unless it does not fit and above does; the flip that keeps it on screen.
    var placement = (below >= h + MARGIN || below >= above) ? 'below' : 'above';
    var top = placement === 'below' ? r.bottom + GAP : r.top - GAP - h;
    top = Math.max(MARGIN, Math.min(top, vh - h - MARGIN));
    var left = r.left + r.width / 2 - w / 2;
    left = Math.max(MARGIN, Math.min(left, vw - w - MARGIN));
    pop.style.top = Math.round(top) + 'px';
    pop.style.left = Math.round(left) + 'px';
    pop.setAttribute('data-placement', placement);
    var arrowX = Math.max(16, Math.min(r.left + r.width / 2 - left, w - 16));
    pop.style.setProperty('--tour-arrow-x', Math.round(arrowX) + 'px');
  }

  function mark(el) {
    if (target) target.classList.remove('tour-target');
    target = el;
    target.classList.add('tour-target');
  }

  function go(i) {
    if (i < 0) return;
    if (i >= steps.length) { finish('done'); return; }
    var el = locate(steps[i]);
    if (!el) {
      // Gone since the tour started (a panel closed, a card moved tabs). Skip it in the same
      // direction the visitor was going.
      steps.splice(i, 1);
      go(i >= steps.length ? steps.length : i);
      return;
    }
    index = i;
    mark(el);
    var step = steps[i];
    pop.querySelector('.tour__count').textContent = (i + 1) + ' of ' + steps.length;
    pop.querySelector('.tour__title').textContent = step.title;
    pop.querySelector('.tour__body').textContent = step.body;
    pop.querySelector('.tour__back').hidden = i === 0;
    pop.querySelector('.tour__next').textContent = i === steps.length - 1 ? 'Done' : 'Next';
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
    place();
    // Focus the popover, not a button, so a screen reader reads its title and text first.
    pop.focus({ preventScroll: true });
    // Smooth scrolling moves the target after this call; follow it until it settles.
    schedule();
  }

  function start() {
    if (pop) return true;
    steps = available();
    if (!steps.length) return false;
    hidePrompt();
    returnFocus = document.activeElement;
    build();
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    go(0);
    return true;
  }

  function finish(outcome) {
    remember(outcome);
    if (target) target.classList.remove('tour-target');
    target = null;
    if (pop) pop.remove();
    pop = null;
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('scroll', schedule);
    window.removeEventListener('resize', schedule);
    if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === 'function') {
      returnFocus.focus({ preventScroll: true });
    }
    returnFocus = null;
  }

  // A refreshed review card (refresh.js) replaces the elements a step points at.
  document.addEventListener('wp:card-replaced', function () { if (pop) schedule(); });

  // ---------- the small-screen prompt ----------

  var prompt = null;

  function showPrompt() {
    prompt = document.createElement('div');
    prompt.className = 'tour-prompt';
    prompt.setAttribute('role', 'region');
    prompt.setAttribute('aria-label', 'Tour of this page');
    var text = document.createElement('span');
    text.textContent = 'New here?';
    var startBtn = button('btn btn--sm tour-prompt__start', 'Take the tour');
    var close = button('tour-prompt__close', '×');
    close.setAttribute('aria-label', 'No thanks');
    startBtn.addEventListener('click', function () { start(); });
    close.addEventListener('click', function () { remember('dismissed'); hidePrompt(); });
    prompt.appendChild(text);
    prompt.appendChild(startBtn);
    prompt.appendChild(close);
    document.body.appendChild(prompt);
  }

  function hidePrompt() {
    if (prompt) prompt.remove();
    prompt = null;
  }

  // ---------- starting ----------

  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('[data-tour-start]');
    if (!link) return;
    // This page has a tour: run it here. Otherwise the link goes to the submit page's tour.
    if (STEPS[page] && start()) e.preventDefault();
  });

  function requested() {
    return /[?&]tour=1(?:&|$)/.test(window.location.search);
  }

  function boot() {
    if (!STEPS[page]) return;
    if (requested()) {
      // Drop the parameter so reloading the page does not start the tour again.
      try {
        var url = new URL(window.location.href);
        url.searchParams.delete('tour');
        window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
      } catch (err) { /* old browser: the tour simply restarts on reload */ }
      start();
      return;
    }
    if (!document.body.hasAttribute('data-signed-in') || stored()) return;
    if (!available().length) return;
    if (window.innerWidth >= AUTO_MIN_WIDTH) start();
    else showPrompt();
  }

  // Give the page a moment to lay out (fonts, the diagram's first paint) before measuring.
  if (document.readyState === 'complete') setTimeout(boot, 300);
  else window.addEventListener('load', function () { setTimeout(boot, 300); });
})();

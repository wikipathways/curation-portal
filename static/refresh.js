// refresh.js: redraw one review card, or the queue, without reloading the page.
//
// Every action used to end in location.reload(): the whole page again, the diagram re-fetched,
// the scroll position and any open panel lost. A card now asks for itself
// (GET /dashboard/<pr>/card) and is swapped in place. The same request, on a timer the server
// chooses (data-poll-after / X-Poll-After), is how the card follows the background loop: a
// publish run starting, the pathway being published, the site going live.
//
// Contract (K5): window.WPRefresh = {card(el), page()}; after every swap the document receives
// `wp:card-replaced` with the new card element as `detail` (viewer.js re-mounts on it).
(function () {
  'use strict';

  var WP = window.WP || {};
  var MAX_BACKOFF = 300; // seconds
  var timers = new WeakMap();
  var failures = new WeakMap();

  function toast(msg, type) { if (WP.toast) WP.toast(msg, type); }

  function cardUrl(el, recheck) {
    var pr = el.getAttribute('data-pr');
    var expanded = el.getAttribute('data-expanded') === '1' ? '1' : '0';
    // recheck=1 also asks the background loop to compare the review with GitHub now (when one
    // is due). Only a person pressing Refresh sends it; the timer reads the database only.
    return '/dashboard/' + encodeURIComponent(pr) + '/card?expanded=' + expanded +
      (recheck ? '&recheck=1' : '');
  }

  // Which <details> were open. A panel carrying data-details-key is matched by that key; the rest
  // by position among themselves, since the new card has the same structure and the n-th one is
  // the same panel. The key is for a panel that comes and goes: "Review details" first appears
  // when a review is approved, and matched by position it would inherit whatever panel used to
  // sit first, and open itself.
  function unkeyed(el) {
    return Array.prototype.filter.call(el.querySelectorAll('details'), function (d) {
      return !d.hasAttribute('data-details-key');
    });
  }

  function openDetails(el) {
    var open = { keys: [], positions: [] };
    el.querySelectorAll('details[data-details-key]').forEach(function (d) {
      if (d.open) open.keys.push(d.getAttribute('data-details-key'));
    });
    unkeyed(el).forEach(function (d, i) { if (d.open) open.positions.push(i); });
    return open;
  }

  function restoreDetails(el, open) {
    el.querySelectorAll('details[data-details-key]').forEach(function (d) {
      d.open = open.keys.indexOf(d.getAttribute('data-details-key')) !== -1;
    });
    unkeyed(el).forEach(function (d, i) { d.open = open.positions.indexOf(i) !== -1; });
  }

  // Text typed into a card's textareas survives a refresh: a curator half-way through a change
  // request must not lose it to the auto-refresh.
  function typedText(el) {
    var typed = {};
    el.querySelectorAll('textarea[id], input[type="text"][id]').forEach(function (t) {
      if (t.value) typed[t.id] = t.value;
    });
    return typed;
  }

  function restoreTyped(el, typed) {
    Object.keys(typed).forEach(function (id) {
      var t = el.querySelector('#' + CSS.escape(id));
      if (t && !t.value) {
        t.value = typed[id];
        // The panel holding it was open, or the text would not have been typed.
        var panel = t.closest('[hidden]');
        if (panel && panel !== el) panel.hidden = false;
      }
    });
  }

  // The last element that had focus. A button disabled while its request runs drops focus to
  // <body>; this is how a swap still knows the person was working in that card.
  var lastFocused = null;
  document.addEventListener('focusin', function (e) { lastFocused = e.target; });

  // The element focus is (or, if it fell to <body>, was last) on, when that is inside `el`.
  function focusedIn(el) {
    var a = document.activeElement;
    if (a && a !== document.body && a !== document.documentElement) {
      return el.contains(a) ? a : null;
    }
    // Only a control that lost focus by being disabled (its request is running), not one the
    // person simply clicked away from.
    return lastFocused && lastFocused.disabled && el.contains(lastFocused) ? lastFocused : null;
  }

  // Focus is restored to the equivalent control: same tag, class list and data-key/data-state.
  // Failing that (an SVG node of the viewer, a button that is gone), to the card's Refresh
  // button or its heading, so a keyboard user is not dropped at the top of the page.
  function focusKey(el) {
    var a = focusedIn(el);
    if (!a) return null;
    var item = a.closest('[data-key]');
    return {
      // getAttribute, not className: an SVG element's className is an object, not a string.
      cls: a.getAttribute('class') || '',
      tag: a.tagName,
      key: item ? item.getAttribute('data-key') : null,
      state: a.getAttribute('data-state'),
      self: a === el
    };
  }

  function focusFallback(el) {
    var target = el.querySelector('[data-refresh-card]:not([disabled])') ||
      el.querySelector('.review-card__title');
    if (!target) target = el;
    if (!target.matches('a[href], button, input, select, textarea, [tabindex]')) {
      target.setAttribute('tabindex', '-1');
    }
    target.focus({ preventScroll: true });
  }

  function restoreFocus(el, key) {
    if (!key) return;
    if (!key.self) {
      var candidates = el.querySelectorAll(key.tag);
      for (var i = 0; i < candidates.length; i++) {
        var c = candidates[i];
        if ((c.getAttribute('class') || '') !== key.cls) continue;
        if (key.state !== null && c.getAttribute('data-state') !== key.state) continue;
        var item = c.closest('[data-key]');
        if (key.key !== null && (!item || item.getAttribute('data-key') !== key.key)) continue;
        if (c.disabled) break;
        c.focus({ preventScroll: true });
        if (document.activeElement === c) return;
        break;
      }
    }
    focusFallback(el);
  }

  // One polite live region for the whole page. The "Checked …" text on each card is rewritten
  // by the timer and is deliberately not live; only a Refresh press is announced, here.
  var liveRegion = null;
  function announce(msg) {
    if (!liveRegion) {
      liveRegion = document.createElement('div');
      liveRegion.className = 'visually-hidden';
      liveRegion.setAttribute('role', 'status');
      liveRegion.setAttribute('aria-live', 'polite');
      document.body.appendChild(liveRegion);
    }
    liveRegion.textContent = '';
    // A fresh text node after clearing, so the same words twice are still read out.
    setTimeout(function () { liveRegion.textContent = msg; }, 50);
  }

  // "4 min ago", as the server's `ago` filter words it (main.py _ago). For <time data-ago>,
  // whose wording is left out of the card's ETag and so kept current here.
  function ago(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return null;
    var s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 45) return 'just now';
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return null; // older: the server's day-level wording stands
  }

  function refreshTimes(el) {
    el.querySelectorAll('time[data-ago][datetime]').forEach(function (t) {
      var words = ago(t.getAttribute('datetime'));
      if (words !== null && t.textContent !== words) t.textContent = words;
    });
  }

  // Something the person is in the middle of: an open decision panel, a chosen revision file,
  // focus in a text field, or the diagram in use (fullscreen, a node's panel open, focus or the
  // pointer on the viewer). A swap re-mounts the viewer and would lose its zoom, pan and
  // selection. The timer leaves such a card alone; the Refresh button does not.
  function busy(el) {
    if (el.querySelector('.changes-panel:not([hidden]), .reject-panel:not([hidden])')) return true;
    var file = el.querySelector('.revise-file');
    if (file && file.files && file.files.length) return true;
    var fs = document.fullscreenElement;
    if (fs && (el.contains(fs) || fs.contains(el))) return true;
    if (el.querySelector('.node-panel:not([hidden])')) return true;
    var a = document.activeElement;
    if (a && el.contains(a) && a.closest('[data-viewer]')) return true;
    try {
      if (el.querySelector('[data-viewer]:hover')) return true;
    } catch (e) { /* :hover unsupported in querySelector: ignore */ }
    return !!(a && el.contains(a) && /^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName));
  }

  function parseCard(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    return doc.querySelector('article.review-card');
  }

  // On a queue tab, a card whose status no longer belongs there folds to one line.
  function movedOut(oldEl, newEl) {
    var list = oldEl.closest('[data-queue-status]');
    var tab = list ? list.getAttribute('data-queue-status') : '';
    if (!tab || oldEl.getAttribute('data-expanded') === '1') return false;
    return newEl.getAttribute('data-status') !== tab;
  }

  function collapse(oldEl, newEl) {
    var note = document.createElement('p');
    note.className = 'review-moved';
    note.setAttribute('role', 'status');
    note.setAttribute('data-pr', newEl.getAttribute('data-pr'));
    var title = newEl.querySelector('.review-card__title');
    note.appendChild(document.createTextNode(
      (title ? title.textContent.trim().split(',')[0] : 'This review') + ': moved to ' +
      (newEl.getAttribute('data-status-label') || newEl.getAttribute('data-status')) + '. '));
    var a = document.createElement('a');
    a.href = '/dashboard/' + newEl.getAttribute('data-pr');
    a.textContent = 'Open it';
    note.appendChild(a);
    // Focus was in the card (on the button just pressed, perhaps): move it to the note's link,
    // or it falls to <body> along with the card.
    var hadFocus = !!focusedIn(oldEl);
    if (window.WPViewer && window.WPViewer.unmountAll) window.WPViewer.unmountAll(oldEl);
    oldEl.replaceWith(note);
    clearTimeout(timers.get(oldEl));
    if (hadFocus) a.focus({ preventScroll: true });
    return note;
  }

  function swap(oldEl, newEl, etag) {
    var open = openDetails(oldEl);
    var typed = typedText(oldEl);
    var focus = focusKey(oldEl);
    newEl.setAttribute('data-etag', etag || '');
    restoreDetails(newEl, open);
    restoreTyped(newEl, typed);
    clearTimeout(timers.get(oldEl));
    oldEl.replaceWith(newEl);
    restoreFocus(newEl, focus);
    document.dispatchEvent(new CustomEvent('wp:card-replaced', { detail: newEl }));
    schedule(newEl);
    return newEl;
  }

  // Fetch this card and swap it in. Resolves to the card now in the document (the same element
  // when nothing changed). Rejects on a network or server error, after scheduling a back-off.
  function card(el, opts) {
    if (!el || !el.isConnected) return Promise.resolve(el);
    var headers = { 'Accept': 'text/html' };
    var etag = el.getAttribute('data-etag');
    if (etag) headers['If-None-Match'] = etag;
    el.setAttribute('aria-busy', 'true');
    return fetch(cardUrl(el, opts && opts.recheck), { headers: headers, credentials: 'same-origin' })
      .then(function (r) {
        el.removeAttribute('aria-busy');
        var after = parseInt(r.headers.get('X-Poll-After') || '', 10);
        if (r.status === 304) {
          failures.delete(el);
          // "Checked 2 min ago" is left out of the ETag (it changes every minute), so it is
          // updated here rather than by swapping a card that has not otherwise changed.
          var checked = r.headers.get('X-Checked');
          var span = el.querySelector('.review-card__checked');
          if (checked !== null && span) span.textContent = checked;
          refreshTimes(el);
          if (!isNaN(after)) el.setAttribute('data-poll-after', String(after));
          schedule(el);
          return el;
        }
        if (r.status === 404) {
          // The review is gone (a test cleanup, a different deployment): stop asking.
          el.setAttribute('data-poll-after', '0');
          return el;
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text().then(function (html) {
          failures.delete(el);
          var fresh = parseCard(html);
          if (!fresh) throw new Error('no card in the response');
          if (!isNaN(after)) fresh.setAttribute('data-poll-after', String(after));
          if (movedOut(el, fresh)) return collapse(el, fresh);
          return swap(el, fresh, r.headers.get('ETag'));
        });
      })
      .catch(function (err) {
        el.removeAttribute('aria-busy');
        failures.set(el, (failures.get(el) || 0) + 1);
        schedule(el);
        throw err;
      });
  }

  // ---- the timer ----
  function delayFor(el) {
    var base = parseInt(el.getAttribute('data-poll-after') || '0', 10);
    if (!base || base < 0) return 0;
    var n = failures.get(el) || 0;
    // Doubling per consecutive failure, so a portal that is down is not asked every 5 s.
    return Math.min(MAX_BACKOFF, base * Math.pow(2, n));
  }

  function visible(el) {
    if (el.getAttribute('data-expanded') === '1') return true;
    var r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < (window.innerHeight || document.documentElement.clientHeight);
  }

  function schedule(el) {
    clearTimeout(timers.get(el));
    var delay = delayFor(el);
    if (!delay) return;
    timers.set(el, setTimeout(function () { tick(el); }, delay * 1000));
  }

  function tick(el) {
    if (!el.isConnected) return;
    // A hidden tab or a card scrolled out of view waits; it is asked again when it shows.
    if (document.hidden || !visible(el) || busy(el)) { schedule(el); return; }
    card(el).catch(function () { /* back-off already scheduled */ });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    document.querySelectorAll('article.review-card[data-poll-after]').forEach(function (el) {
      // Coming back to the tab: catch up at once rather than after a full interval.
      if (delayFor(el)) tick(el);
    });
  });

  // ---- the queue's Refresh: the whole list, without a reload ----
  function page() {
    var container = document.querySelector('#main .container');
    if (!container) { location.reload(); return Promise.resolve(); }
    return fetch(location.href, { headers: { 'Accept': 'text/html' }, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.querySelector('#main .container');
        if (!fresh) throw new Error('no page content in the response');
        var y = window.scrollY;
        if (window.WPViewer && window.WPViewer.unmountAll) window.WPViewer.unmountAll(container);
        container.querySelectorAll('article.review-card').forEach(function (el) {
          clearTimeout(timers.get(el));
        });
        container.replaceChildren.apply(container, Array.prototype.slice.call(fresh.childNodes).map(
          function (n) { return document.importNode(n, true); }));
        window.scrollTo(0, y);
        container.querySelectorAll('article.review-card').forEach(function (el) {
          document.dispatchEvent(new CustomEvent('wp:card-replaced', { detail: el }));
          schedule(el);
        });
      });
  }

  document.addEventListener('click', function (e) {
    var one = e.target.closest('[data-refresh-card]');
    if (one) {
      var el = one.closest('article.review-card');
      if (!el || one.disabled) return;
      one.disabled = true;
      card(el, { recheck: true })
        .then(function (now) {
          var btn = now && now.querySelector && now.querySelector('[data-refresh-card]');
          if (btn) {
            btn.disabled = false;
            // Disabling it dropped focus (a 304 keeps the same button); a swap brought a new one.
            var a = document.activeElement;
            if (now !== el || !a || a === document.body || a === btn) btn.focus({ preventScroll: true });
          }
          var span = now && now.querySelector && now.querySelector('.review-card__checked');
          announce('Review refreshed.' + (span && span.textContent.trim() ? ' ' + span.textContent.trim() + '.' : ''));
        })
        .catch(function () {
          one.disabled = false;
          toast('Could not refresh this review. Try again in a moment.', 'error');
        });
      return;
    }
    var all = e.target.closest('[data-refresh-page]');
    if (all) {
      if (all.disabled) return;
      all.disabled = true;
      page()
        .then(function () { toast('Queue refreshed.', 'success'); })
        .catch(function () { toast('Could not refresh the queue. Try again in a moment.', 'error'); })
        .finally(function () {
          var btn = document.querySelector('[data-refresh-page]');
          if (btn) btn.disabled = false;
        });
    }
  });

  document.querySelectorAll('article.review-card[data-poll-after]').forEach(schedule);

  window.WPRefresh = { card: card, page: page };
})();

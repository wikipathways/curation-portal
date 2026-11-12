// wikipathways-submit client behavior
// No build step: plain ES5-ish JS, event delegation, optimistic UI updates.
(function () {
  'use strict';

  // Whether approving merges the pull request or hands it to the target repository's own
  // publication workflow. Set from the server context; the two read very differently on screen.
  var PIPELINE_MODE = document.body.getAttribute('data-publish-mode') === 'pipeline';

  // ---------- toasts ----------
  function toast(message, type) {
    var region = document.getElementById('toast-region');
    if (!region) return;
    var el = document.createElement('div');
    el.className = 'toast' + (type ? ' toast--' + type : '');
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    var text = document.createElement('span');
    text.textContent = message;
    el.appendChild(text);
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.innerHTML = '&times;';
    close.addEventListener('click', function () { el.remove(); });
    el.appendChild(close);
    // The region is fixed to a corner and does not scroll, so an unbounded stack grows off the
    // top of the screen — taking its own close buttons with it — and covers the card actions
    // underneath. Three is enough to see that several things went wrong.
    while (region.children.length >= 3) { region.firstChild.remove(); }
    region.appendChild(el);
    // An error is not a notification you can afford to miss: it is usually a list of validation
    // reasons, and six seconds is not enough to read one, let alone copy it. Errors stay until
    // dismissed; everything else still self-clears.
    if (type !== 'error') {
      setTimeout(function () { if (el.parentNode) el.remove(); }, 6000);
    }
  }

  // Where there is somewhere permanent on the page to put an error, put it there instead of in
  // a toast: the result card holds the full text, can be re-read and copied, and does not sit
  // on top of the controls.
  function showError(outletId, message) {
    var out = document.getElementById(outletId);
    if (!out) { toast(message, 'error'); return; }
    out.className = 'result-card result-card--error';
    out.setAttribute('role', 'alert');
    out.textContent = message;
    out.hidden = false;
  }

  // Speak to a pathway author, not to a developer: no status codes in the message. The API's
  // own `detail` is already written for humans, so prefer it; fall back per status.
  var STATUS_FALLBACK = {
    401: 'Your session has expired. Log in with GitHub again.',
    403: 'You do not have permission to do that.',
    404: 'That pathway or review no longer exists.',
    409: 'Someone else is editing this pathway right now.',
    413: 'That file is too large to upload.',
    502: 'GitHub did not accept the change. Try again in a moment.',
    503: 'That part of the app is not configured yet.'
  };
  function describeError(status, body) {
    var detail = body && body.detail;
    if (detail && Array.isArray(detail.errors)) return detail.errors.join('; ');
    if (typeof detail === 'string') return detail;
    if (detail && detail.reason) {
      return detail.held_by
        ? 'This pathway is checked out by @' + detail.held_by + '. Try again once their edit is merged.'
        : String(detail.reason);
    }
    return STATUS_FALLBACK[status] || 'Something went wrong. Try again.';
  }

  function postForm(url, fields) {
    var fd = new FormData();
    for (var k in fields) if (Object.prototype.hasOwnProperty.call(fields, k)) fd.append(k, fields[k]);
    return fetch(url, { method: 'POST', body: fd }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, body: j };
      });
    });
  }

  // ---------- logout ----------
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action="logout"]');
    if (!btn) return;
    if (btn.disabled) return;
    btn.disabled = true;
    // Navigate either way: the session lives on the server, so reloading / shows the truth
    // whether or not the POST landed. Without this the button just looks dead.
    fetch('/auth/logout', { method: 'POST' })
      .catch(function () { toast('Could not reach the server; reloading to check your session.', 'error'); })
      .finally(function () { location.href = '/'; });
  });

  // ---------- tabs (index.html, logged in) ----------
  var tabs = document.querySelectorAll('.tab');
  function selectTab(name) {
    tabs.forEach(function (x) {
      var on = x.getAttribute('data-tab') === name;
      x.classList.toggle('tab--active', on);
      x.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.tab-panel').forEach(function (p) {
      p.classList.toggle('tab-panel--hidden', p.getAttribute('data-panel') !== name);
    });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { selectTab(t.getAttribute('data-tab')); });
  });


  // ---------- pre-flight on file choice ----------
  // /api/validate has existed since MVP-2 and nothing ever called it: the form posted straight to
  // /api/submit, so the first thing a submitter heard about their file was either a pull request
  // or a 422. Everything the database's own pipeline will complain about — a short title, a thin
  // description, unannotated nodes, an empty citation id that kills its metadata step — was
  // knowable here, at the one moment the fix is still free.
  //
  // Advisory throughout. The four blocking reasons still 422 on submit; nothing below stops
  // anyone, and the submit button is never disabled by it.
  var PREFLIGHT_WORD = {
    block: 'Blocked', fail: 'Problem', warn: 'Worth a look', pass: 'Fine', na: 'Nothing to check'
  };

  function span(cls, text) {
    var el = document.createElement('span');
    el.className = cls;
    el.textContent = text;
    return el;
  }

  function renderPreflight(findings) {
    var host = document.getElementById('preflight');
    if (!host) { return; }
    host.innerHTML = '';
    var loud = (findings || []).filter(function (f) {
      return f.severity === 'warn' || f.severity === 'fail' || f.severity === 'block';
    });
    if (!loud.length) {
      host.className = 'preflight preflight--clean';
      host.appendChild(span('preflight__lede', 'Checked. Nothing to flag before this is submitted.'));
      host.hidden = false;
      return;
    }
    var worst = loud.some(function (f) { return f.severity !== 'warn'; }) ? 'fail' : 'warn';
    host.className = 'preflight preflight--' + worst;
    host.appendChild(span(
      'preflight__lede',
      'Checked before submitting. None of this stops you — it is what the database is likely ' +
      'to raise.'
    ));
    var list = document.createElement('ul');
    list.className = 'preflight__list';
    loud.forEach(function (f) {
      var li = document.createElement('li');
      li.className = 'preflight__item preflight__item--' + f.severity;
      // Built as nodes with textContent rather than an HTML string. A finding's detail quotes
      // data-node labels straight out of the uploaded file, so it is attacker-controlled text on
      // a page the attacker is already authenticated to — the one place in this form where
      // innerHTML would be a real hole rather than a theoretical one.
      li.appendChild(span(
        'preflight__pill preflight__pill--' + f.severity,
        PREFLIGHT_WORD[f.severity] || f.severity
      ));
      li.appendChild(span('preflight__title', f.title || ''));
      li.appendChild(span('preflight__detail', f.detail || ''));
      list.appendChild(li);
    });
    host.appendChild(list);
    host.hidden = false;
  }

  var newFile = document.getElementById('new-file');
  if (newFile) {
    newFile.addEventListener('change', function () {
      var host = document.getElementById('preflight');
      var file = newFile.files[0];
      if (host) { host.hidden = true; host.innerHTML = ''; }
      if (!file) { return; }
      var fd = new FormData();
      fd.append('file', file);
      fetch('/api/validate', { method: 'POST', body: fd })
        .then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
        })
        .then(function (res) {
          // A 422 here is the file being refused outright, and submit will say the same thing in
          // the same words. Reported now so nobody uploads twice to hear it.
          if (!res.ok) { showError('result-card', describeError(res.status, res.body)); return; }
          renderPreflight((res.body.quality || {}).findings);
        })
        .catch(function () {
          // The pre-flight is a courtesy; a submitter who cannot reach it can still submit, and
          // that request will report its own failure. Saying nothing beats a scary red box about
          // a check nobody asked for.
        });
    });
  }

  // ---------- new-pathway submit (single action — validate + open PR together) ----------
  var submitForm = document.getElementById('submit-form');
  if (submitForm) {
    submitForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var file = document.getElementById('new-file').files[0];
      var submitBtn = document.getElementById('submit-btn');
      var resultCard = document.getElementById('result-card');
      if (!file) { showError('result-card', 'Choose a .gpml file first.'); return; }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Submitting…';
      var fd = new FormData();
      fd.append('file', file);
      var submitDesc = document.getElementById('submit-description');
      if (submitDesc) { fd.append('description', submitDesc.value || ''); }
      fetch('/api/submit', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit new pathway';
          if (!res.ok) { showError('result-card', describeError(res.status, res.body)); return; }
          var pr = '<a href="' + res.body.pr_url + '" target="_blank" rel="noopener">#' + res.body.pr_number + '</a>';
          var mine = ' <a href="/dashboard/' + res.body.pr_number + '">Follow the review</a>.';
          // Not "Assigned WP0001": where the database assigns the identifier at publication,
          // the app committed a placeholder and has no id to report. Saying otherwise sends
          // people back to the update tab with a number that means a different pathway.
          resultCard.className = 'result-card';
          // A previous failure left role="alert" on this container; leaving it there makes a
          // screen reader interrupt with the success message as though it were another error.
          resultCard.removeAttribute('role');
          resultCard.innerHTML = PIPELINE_MODE
            ? 'Submitted. Opened pull request ' + pr + ' (<code>' + res.body.path + '</code>). ' +
              'The WPID is assigned by the database when a curator approves it, not now.' + mine
            : 'Assigned <strong>' + res.body.wpid + '</strong>. Opened pull request ' + pr +
              ' (<code>' + res.body.path + '</code>).' + mine;
          resultCard.hidden = false;
          toast('Pathway submitted.', 'success');
        })
        .catch(function () {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit new pathway';
          showError('result-card', 'Could not reach the server. Try again.');
        });
    });
  }

  // ---------- WPID field: normalise to WP#### and verify the pathway exists ----------
  var wpidInput = document.getElementById('update-wpid');
  var wpidStatus = document.getElementById('update-wpid-status');
  function wpidNumber(v) { return (v || '').replace(/\D/g, ''); }
  // A WikiPathways identifier never carries a leading zero, so "WP0001" is not WP1 — it is the
  // placeholder this app commits before the database assigns an id. Stripping the zeros would
  // silently point the upload at a real, unrelated pathway.
  function isPadded(digits) { return digits.length > 1 && digits.charAt(0) === '0'; }
  function paddedMessage(digits) {
    // WP0001 is not a typo for WP1 — it is what a submission carries before it has an id, so
    // offering WP1 there would send them to a real and unrelated pathway.
    if (digits === '0001') {
      return 'WP0001 is the placeholder a new submission carries until the database assigns its ' +
        'id, not a pathway. Find your submission under My submissions to revise it.';
    }
    var real = digits.replace(/^0+/, '');
    return 'WikiPathways identifiers have no leading zeros.' +
      (real ? ' Did you mean WP' + real + '?' : '');
  }
  if (wpidInput) {
    wpidInput.addEventListener('blur', function () {
      var num = wpidNumber(wpidInput.value);
      if (!num) { wpidInput.value = ''; if (wpidStatus) wpidStatus.hidden = true; return; }
      wpidInput.value = 'WP' + num;  // always display WP####
      if (!wpidStatus) return;
      if (isPadded(num)) {
        delete wpidInput.dataset.state;
        wpidStatus.hidden = false;
        wpidStatus.className = 'wpid-status wpid-status--err';
        wpidStatus.textContent = paddedMessage(num);
        return;
      }
      wpidStatus.hidden = false;
      wpidStatus.className = 'wpid-status wpid-status--checking';
      wpidStatus.textContent = 'Checking WP' + num + '…';
      fetch('/api/pathways/' + num)
        .then(function (r) {
          // A 401/502 must NOT read as "absent": telling someone an existing pathway does not
          // exist sends them to the New-pathway tab, which burns a WPID on a duplicate.
          if (r.ok) return r.json();
          return r.json().catch(function () { return {}; }).then(function (body) {
            return { lookupFailed: true, message: describeError(r.status, body) };
          });
        })
        .then(function (info) {
          if (!info) { wpidStatus.hidden = true; return; }
          if (info.lookupFailed) {
            delete wpidInput.dataset.state;
            wpidStatus.className = 'wpid-status wpid-status--error';
            wpidStatus.textContent = 'Could not check WP' + num + '. ' + info.message;
            return;
          }
          wpidInput.dataset.state = info.state;
          if (info.state === 'on_main') {
            wpidStatus.className = 'wpid-status wpid-status--ok';
            wpidStatus.textContent = 'Found ' + info.wpid + (info.name ? ': ' + info.name : '') + '. Uploading opens an update.';
            setUpdateLabel('Submit update');
          } else if (info.state === 'pending_new') {
            wpidStatus.className = 'wpid-status wpid-status--pending';
            wpidStatus.textContent = info.wpid + ' is still an open submission (pull request #' + info.pr_number + '). Uploading revises it.';
            setUpdateLabel('Submit revision');
          } else {
            wpidStatus.className = 'wpid-status wpid-status--err';
            wpidStatus.textContent = info.wpid + ' does not exist yet. Use the "New pathway" tab.';
            // Otherwise the button keeps whatever the *previous* lookup put there — offering
            // "Submit revision" for a pathway the line above says does not exist.
            setUpdateLabel(updateLabelForState());
          }
        })
        .catch(function () { wpidStatus.hidden = true; });
    });
  }

  // /?wpid=WP554 opens the update form with that pathway filled in. A curator's change request
  // on an *update* has to send the submitter somewhere that works, and without this the link
  // would land them on the New pathway tab — the one thing that must not happen, since it would
  // file their fix as a second, duplicate pathway.
  //
  // Placed after the blur listener above, not next to the tab handlers: it dispatches a blur to
  // run the presence lookup, and a dispatch made before that listener exists reaches nothing.
  (function prefillFromQuery() {
    if (!wpidInput || !tabs.length) return;
    var match = /[?&]wpid=(WP)?(\d+)/i.exec(window.location.search);
    if (!match) return;
    selectTab('update');
    wpidInput.value = 'WP' + match[2];
    wpidInput.dispatchEvent(new Event('blur'));
  })();

  // The blur handler and the click handler race: blur fires first when someone tabs or clicks
  // straight from the WPID field to the button, so a lookup can land after the submit handler
  // already wrote "Submitting…" and disabled the button. The user then sees an idle-looking
  // label on a dead button. While a submission is in flight the label is not the lookup's to set.
  function setUpdateLabel(text) {
    var btn = document.getElementById('update-btn');
    if (!btn || btn.disabled) return;
    btn.textContent = text;
  }
  function updateLabelForState() {
    var state = wpidInput ? wpidInput.dataset.state : null;
    return state === 'pending_new' ? 'Submit revision' : 'Submit update';
  }

  // Bound to the form's submit, not the button's click: with a single text field the browser
  // implicitly submits on Enter, which used to reload the page and discard the whole form.
  var updateForm = document.getElementById('update-form');
  if (updateForm) {
    updateForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var wpid = (document.getElementById('update-wpid').value || '').trim().replace(/^WP/i, '');
      var file = document.getElementById('update-file').files[0];
      if (!/^\d+$/.test(wpid)) { showError('update-result', 'Enter a numeric WPID, like 554.'); return; }
      if (isPadded(wpid)) { showError('update-result', paddedMessage(wpid)); return; }
      if (!file) { showError('update-result', 'Choose a .gpml file first.'); return; }
      // Compose the "what changed" note from the ticked options + the free-text details.
      var updateDesc = document.getElementById('update-description');
      var detail = updateDesc ? (updateDesc.value || '').trim() : '';
      var otherBox = document.getElementById('update-change-other');
      if (otherBox && otherBox.checked && !detail) {
        showError('update-result', 'You ticked Other, so say what changed in Details.'); return;
      }
      var changes = [];
      document.querySelectorAll('.update-change:checked').forEach(function (cb) {
        changes.push(cb.getAttribute('data-label'));
      });
      var parts = [];
      if (changes.length) { parts.push(changes.join('; ') + '.'); }
      if (detail) { parts.push(detail); }
      var description = parts.join('\n');
      var btn = document.getElementById('update-btn');
      btn.disabled = true;
      btn.textContent = 'Submitting…';
      var prevOut = document.getElementById('update-result');
      if (prevOut) { prevOut.hidden = true; prevOut.innerHTML = ''; prevOut.className = 'result-card'; }
      var fd = new FormData();
      fd.append('file', file);
      fd.append('description', description);
      // Recomputed from the field's last lookup, not from a label captured before it: an
      // in-flight lookup could have been about to change it.
      function reset() { btn.disabled = false; btn.textContent = updateLabelForState(); }
      // Route by where the WPID lives: an existing pathway → update; a still-open new
      // submission → revise (commit onto its PR); nowhere → tell the user.
      fetch('/api/pathways/' + wpid)
        .then(function (r) {
          if (r.ok) return r.json();
          return r.json().catch(function () { return {}; }).then(function (body) {
            return { lookupFailed: true, message: describeError(r.status, body) };
          });
        })
        .then(function (info) {
          if (info.lookupFailed) {
            // Unknown whether WP#### exists — stop rather than route the upload on a guess.
            reset();
            showError('update-result', 'Could not check WP' + wpid + '. ' + info.message);
            return null;
          }
          if (info.state === 'absent') {
            reset();
            showError('update-result', info.wpid + ' does not exist yet. Use the "New pathway" tab.');
            return null;
          }
          // A revision is keyed by pull request, not by WPID: a new submission has no WPID
          // until the repository publishes it, so there is no /api/pathways/<id>/revise.
          var revising = info.state === 'pending_new';
          if (revising && !info.pr_number) {
            reset();
            showError('update-result', 'Could not find the pull request for ' + info.wpid + '. Open it from the dashboard instead.');
            return null;
          }
          var url = revising
            ? '/api/reviews/' + info.pr_number + '/revise'
            : '/api/pathways/' + wpid + '/update';
          var verb = revising ? 'revise' : 'update';
          return fetch(url, { method: 'POST', body: fd })
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j, verb: verb }; }); });
        })
        .then(function (res) {
          if (!res) return;
          reset();
          if (!res.ok) { showError('update-result', describeError(res.status, res.body)); return; }
          var word = res.verb === 'revise' ? 'Revised' : 'Updated';
          var noun = res.verb === 'revise' ? 'Revision' : 'Update';
          var out = document.getElementById('update-result');
          out.className = 'result-card';
          out.removeAttribute('role');
          out.innerHTML =
            word + ' <strong>' + (res.verb === 'revise' && PIPELINE_MODE ? 'the submission' : res.body.wpid) +
            '</strong> on pull request ' +
            '<a href="' + res.body.pr_url + '" target="_blank" rel="noopener">#' + res.body.pr_number + '</a> ' +
            '(<code>' + res.body.path + '</code>). <a href="/dashboard/' + res.body.pr_number + '">Follow the review</a>.';
          out.hidden = false;
          toast(noun + ' submitted.', 'success');
        })
        .catch(function () {
          reset();
          showError('update-result', 'Could not reach the server. Try again.');
        });
    });
  }

  // ---------- review cards (dashboard.html / review_detail.html) ----------
  // Mirrors the state_icon() macro in templates/dashboard.html: the pill's glyph is the
  // non-colour cue for its state, so a JS update has to redraw it, not just the word.
  var PILL_ICON = {
    pass: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8.5l3 3 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    fail: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    na: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 8h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    pending: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="4" stroke="currentColor" stroke-width="2"/></svg>'
  };
  var PILL_LABEL = { pass: 'Pass', fail: 'Fail', na: 'N/A', pending: 'Not checked' };
  function setPillContent(pill, state) {
    // The glyph is the non-colour cue, and the word beside it is the label the template wrote.
    // Replacing the pill's whole contents with the raw state would drop the icon and print the
    // identifier, so the row a curator just clicked would be the one that looks wrong.
    pill.innerHTML = (PILL_ICON[state] || '') + ' ';
    var text = document.createElement('span');
    text.className = 'state-pill__text';
    text.textContent = PILL_LABEL[state] || state;
    pill.appendChild(text);
  }

  function recomputeApprove(card) {
    var approveBtn = card.querySelector('.btn--approve');
    if (!approveBtn) return;
    var required = card.querySelectorAll('.checklist__item[data-required="true"]');
    // Naming the outstanding items rather than only disabling the button: a control disabled for
    // a reason the page never states is what made issue #27 expensive to find. Kept in step with
    // the same title the template writes on first render.
    var blocking = [];
    required.forEach(function (li) {
      var pill = li.querySelector('.state-pill');
      if (!pill || pill.dataset.state !== 'pass') {
        blocking.push(li.getAttribute('data-label') || li.getAttribute('data-key'));
      }
    });
    if (!blocking.length) {
      approveBtn.removeAttribute('aria-disabled');
      approveBtn.removeAttribute('title');
      approveBtn.disabled = false;
    } else {
      approveBtn.setAttribute('aria-disabled', 'true');
      approveBtn.setAttribute('title', 'Still to mark Pass: ' + blocking.join(', '));
      approveBtn.disabled = true;
    }
  }

  // Reconcile every pill/chip in a card against the authoritative server checklist, so a write
  // that the server resolved differently than our optimistic click (e.g. after a concurrent
  // update, issue #15) becomes visible instead of silently diverging.
  function reconcileChecklist(card, checklist) {
    if (!checklist) return;
    checklist.forEach(function (srv) {
      var item = card.querySelector('.checklist__item[data-key="' + srv.key + '"]');
      if (!item) return;
      var pill = item.querySelector('.state-pill');
      if (pill) {
        pill.className = 'state-pill state-pill--' + srv.state;
        pill.setAttribute('data-state', srv.state);
        setPillContent(pill, srv.state);
      }
      item.querySelectorAll('.chip-btn').forEach(function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-state') === srv.state ? 'true' : 'false');
      });
      // `required` moves with the state now (issue #27): marking an item N/A takes it off the
      // approval gate, and marking it anything else puts it back. Reading it off the server's
      // answer rather than re-deriving it keeps one rule in one place.
      var required = srv.required ? 'true' : 'false';
      item.setAttribute('data-required', required);
      var bullet = item.querySelector('.checklist__req');
      if (bullet) bullet.hidden = !srv.required;
    });
    recomputeApprove(card);
  }

  // Only one of the two decision panels is open at a time: they sit in the same place and both
  // end in an irreversible action, so leaving the other one open invites the wrong click.
  function togglePanel(card, selector, focusSelector) {
    if (!card) return;
    card.querySelectorAll('.changes-panel, .reject-panel').forEach(function (p) {
      if (!p.matches(selector)) p.hidden = true;
    });
    var panel = card.querySelector(selector);
    if (!panel) return;
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      var focusable = panel.querySelector(focusSelector);
      if (focusable) focusable.focus();
    }
  }

  document.querySelectorAll('.review-card').forEach(recomputeApprove);

  document.addEventListener('click', function (e) {
    var chip = e.target.closest('.chip-btn');
    if (chip) {
      var card = chip.closest('.review-card');
      var item = chip.closest('.checklist__item');
      var pr = card.getAttribute('data-pr');
      var key = item.getAttribute('data-key');
      var state = chip.getAttribute('data-state');
      var siblingChips = item.querySelectorAll('.chip-btn');
      siblingChips.forEach(function (b) { b.disabled = true; });
      postForm('/api/reviews/' + pr + '/checklist', { key: key, state: state })
        .then(function (res) {
          siblingChips.forEach(function (b) { b.disabled = false; });
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          // Trust the server's checklist (the full ReviewDetail), not just the clicked item.
          reconcileChecklist(card, res.body && res.body.checklist);
        })
        .catch(function () {
          siblingChips.forEach(function (b) { b.disabled = false; });
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var approveBtn = e.target.closest('.btn--approve');
    if (approveBtn) {
      if (approveBtn.getAttribute('aria-disabled') === 'true') return;
      var card2 = approveBtn.closest('.review-card');
      var pr2 = card2.getAttribute('data-pr');
      var originalLabel = approveBtn.textContent;
      approveBtn.disabled = true;
      // Nothing is merged where the repository publishes through its own Actions. Saying
      // "Merging…" there sends the curator to GitHub to look for a merge that never happens.
      approveBtn.textContent = PIPELINE_MODE ? 'Handing it over…' : 'Merging…';
      fetch('/api/reviews/' + pr2 + '/approve', { method: 'POST' })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          if (!res.ok) {
            approveBtn.disabled = false;
            approveBtn.textContent = originalLabel;
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast(PIPELINE_MODE
            ? 'Approved. The repository assigns the WPID and publishes it from here.'
            : 'Approved and merged.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          approveBtn.disabled = false;
          approveBtn.textContent = originalLabel;
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var changesBtn = e.target.closest('.btn--changes');
    if (changesBtn) {
      togglePanel(changesBtn.closest('.review-card'), '.changes-panel', '.changes-note');
      return;
    }

    var rejectBtn = e.target.closest('.btn--reject');
    if (rejectBtn) {
      togglePanel(rejectBtn.closest('.review-card'), '.reject-panel', '.reject-note');
      return;
    }

    var rejectSend = e.target.closest('.btn--reject-send');
    if (rejectSend) {
      var cardJ = rejectSend.closest('.review-card');
      var prJ = cardJ.getAttribute('data-pr');
      var noteJ = cardJ.querySelector('.reject-note');
      rejectSend.disabled = true;
      rejectSend.textContent = 'Rejecting…';
      postForm('/api/reviews/' + prJ + '/reject', { note: noteJ ? noteJ.value : '' })
        .then(function (res) {
          if (!res.ok) {
            rejectSend.disabled = false;
            rejectSend.textContent = 'Reject this submission';
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast('Submission rejected.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          rejectSend.disabled = false;
          rejectSend.textContent = 'Reject this submission';
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var recordBtn = e.target.closest('.btn--record-wpid');
    if (recordBtn) {
      var cardW = recordBtn.closest('.review-card');
      var prW = cardW.getAttribute('data-pr');
      var field = cardW.querySelector('.published-wpid');
      var digits = wpidNumber(field ? field.value : '');
      if (!digits || isPadded(digits)) {
        toast(digits ? paddedMessage(digits) : 'Enter the WPID the database assigned, like WP5678.', 'error');
        if (field) field.focus();
        return;
      }
      recordBtn.disabled = true;
      recordBtn.textContent = 'Recording…';
      postForm('/api/reviews/' + prW + '/published-wpid', { wpid: digits })
        .then(function (res) {
          if (!res.ok) {
            recordBtn.disabled = false;
            recordBtn.textContent = 'Record it';
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast('Recorded as WP' + digits + '.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          recordBtn.disabled = false;
          recordBtn.textContent = 'Record it';
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var reviseBtn = e.target.closest('.btn--revise');
    if (reviseBtn) {
      var cardR = reviseBtn.closest('.review-card');
      var prR = cardR.getAttribute('data-pr');
      var fileEl = cardR.querySelector('.revise-file');
      if (!fileEl || !fileEl.files || !fileEl.files.length) {
        toast('Choose a GPML file first.', 'error');
        return;
      }
      reviseBtn.disabled = true;
      reviseBtn.textContent = 'Uploading\u2026';
      postForm('/api/reviews/' + prR + '/revise', { file: fileEl.files[0] })
        .then(function (res) {
          if (res.ok) {
            toast('Revision committed onto pull request #' + res.body.pr_number + '.', 'success');
            setTimeout(function () { location.reload(); }, 900);
            return;
          }
          // 422 carries the per-reason validation list; anything else has a plain detail string.
          var d = res.body && res.body.detail;
          var msg = d && d.errors ? d.errors.join('; ') : (d || 'Upload failed.');
          toast(msg, 'error');
          reviseBtn.disabled = false;
          reviseBtn.textContent = 'Commit onto this pull request';
        })
        .catch(function () {
          toast('Could not reach the server. Try again.', 'error');
          reviseBtn.disabled = false;
          reviseBtn.textContent = 'Commit onto this pull request';
        });
      return;
    }

    var sendBtn = e.target.closest('.btn--changes-send');
    if (sendBtn) {
      var card3 = sendBtn.closest('.review-card');
      var pr3 = card3.getAttribute('data-pr');
      var noteEl = card3.querySelector('.changes-note');
      sendBtn.disabled = true;
      sendBtn.textContent = 'Sending…';
      postForm('/api/reviews/' + pr3 + '/request-changes', { note: noteEl ? noteEl.value : '' })
        .then(function (res) {
          sendBtn.disabled = false;
          sendBtn.textContent = 'Send request';
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          toast('Changes requested.', 'success');
          setTimeout(function () { location.reload(); }, 600);
        })
        .catch(function () {
          sendBtn.disabled = false;
          sendBtn.textContent = 'Send request';
          toast('Could not reach the server. Try again.', 'error');
        });
    }
  });

  // ---------- pan / zoom of a rendered preview ----------
  // Zoom by resizing the element (not CSS transform: scale) so the SVG re-rasterizes crisply at
  // the new size; pan with a translate. Scaling an <img>-loaded SVG with transform only magnifies
  // the already-rasterized bitmap, which is blurry.
  function initZoom(root) {
    var img = root.querySelector('.zoom__img');
    var viewport = root.querySelector('.zoom__viewport');
    if (!img || !viewport) return;
    // The stage wraps the image and the hotspot overlay. Sizing and panning it rather than the
    // image is what keeps the two registered with each other at every zoom level.
    var stage = root.querySelector('.zoom__stage') || img;
    var scale = 1, tx = 0, ty = 0, min = 1, max = 8;
    var dragging = false, sx = 0, sy = 0, baseW = 0, baseH = 0;

    function measure() {
      // Fitted size at scale 1 (max-width/height:100%, natural aspect). Basis for crisp resizing.
      var prev = stage.style.cssText;
      stage.style.width = ''; stage.style.height = '';
      stage.style.maxWidth = '100%'; stage.style.maxHeight = '100%'; stage.style.transform = 'none';
      var r = img.getBoundingClientRect();
      baseW = r.width; baseH = r.height;
      stage.style.cssText = prev;
    }
    function apply() {
      if (scale > 1.001 && baseW) {
        stage.style.maxWidth = 'none'; stage.style.maxHeight = 'none';
        stage.style.width = (baseW * scale) + 'px';
        stage.style.height = (baseH * scale) + 'px';
      } else {
        stage.style.width = ''; stage.style.height = '';
        stage.style.maxWidth = '100%'; stage.style.maxHeight = '100%';
      }
      stage.style.transform = 'translate(' + tx + 'px,' + ty + 'px)';
      root.classList.toggle('zoom--zoomed', scale > 1.001);
      // Keep the clickable overlay on the image after a zoom or pan. Set by initHotspots, which
      // runs after this, so it is absent on the first apply() and present from then on.
      if (root.__syncOverlay) root.__syncOverlay();
    }
    function clamp(s) { return Math.max(min, Math.min(max, s)); }
    // Keep the diagram overlapping its frame. Without this, holding an arrow key walks it
    // entirely out of a viewport that clips at overflow:hidden, with nothing left on screen.
    function clampPan() {
      if (!baseW) return;
      var r = viewport.getBoundingClientRect();
      var limitX = Math.max(0, (baseW * scale - r.width) / 2);
      var limitY = Math.max(0, (baseH * scale - r.height) / 2);
      tx = Math.max(-limitX, Math.min(limitX, tx));
      ty = Math.max(-limitY, Math.min(limitY, ty));
    }
    function reset() { scale = 1; tx = 0; ty = 0; apply(); }
    function zoomAt(factor, cx, cy) {
      if (!baseW) measure();
      var rect = viewport.getBoundingClientRect();
      var ox = cx - rect.left - rect.width / 2 - tx;
      var oy = cy - rect.top - rect.height / 2 - ty;
      var ns = clamp(scale * factor), ratio = ns / scale;
      tx -= ox * (ratio - 1); ty -= oy * (ratio - 1); scale = ns;
      if (scale === 1) { tx = 0; ty = 0; }
      apply();
    }
    function center() { var r = viewport.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }

    if (img.complete && img.naturalWidth) { measure(); } else { img.addEventListener('load', measure); }

    viewport.addEventListener('wheel', function (e) {
      // Don't hijack the page scroll: a queue card is mostly preview, so swallowing every wheel
      // event traps the reader. Ctrl+wheel (the pinch gesture) zooms from any scale; a plain
      // wheel only once they have zoomed in and are navigating inside the diagram.
      if (!e.ctrlKey && scale <= 1.001) return;
      e.preventDefault();
      zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY);
    }, { passive: false });
    viewport.addEventListener('pointerdown', function (e) {
      if (scale <= 1) return;
      dragging = true; sx = e.clientX - tx; sy = e.clientY - ty;
      viewport.setPointerCapture(e.pointerId);
    });
    viewport.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      tx = e.clientX - sx; ty = e.clientY - sy; apply();
    });
    function end(e) { if (dragging) { dragging = false; try { viewport.releasePointerCapture(e.pointerId); } catch (_) {} } }
    viewport.addEventListener('pointerup', end);
    viewport.addEventListener('pointercancel', end);

    // Keyboard: the viewport clips at overflow:hidden and has no scrollbars, so without this a
    // curator who cannot use a pointer can zoom to 8x and then only ever see the centre of the
    // diagram. Reading node labels in a dense pathway is the review.
    var PAN_STEP = 40;
    viewport.addEventListener('keydown', function (e) {
      var handled = true;
      // Only pan once there is something to pan to. The viewport is focusable and every card
      // carries one or two, so swallowing the arrow keys at scale 1 would stop a keyboard user
      // scrolling the queue at all — it would slide the un-zoomed diagram out of its frame
      // instead, which is not even a thing they asked for.
      var panning = scale > 1.001;
      if (e.key === 'ArrowLeft' && panning) { tx += PAN_STEP; }
      else if (e.key === 'ArrowRight' && panning) { tx -= PAN_STEP; }
      else if (e.key === 'ArrowUp' && panning) { ty += PAN_STEP; }
      else if (e.key === 'ArrowDown' && panning) { ty -= PAN_STEP; }
      else if (e.key === 'Home') { reset(); }
      else if (e.key === '+' || e.key === '=') { var ci = center(); zoomAt(1.3, ci[0], ci[1]); }
      else if (e.key === '-' || e.key === '_') { var co = center(); zoomAt(1 / 1.3, co[0], co[1]); }
      else { handled = false; }
      if (!handled) return;
      e.preventDefault();
      clampPan();
      apply();
    });

    // Pan whatever the caller names into view. The viewport clips at overflow:hidden and moves
    // by transform, so it has no scroll offset for the browser's own scroll-into-view to write:
    // focusing a hotspot outside the visible edge moves focus and shows nothing. Zoomed out
    // there is nothing off-screen, so this is a no-op at scale 1.
    root.__revealRect = function (rect) {
      if (scale <= 1.001) return;
      var v = viewport.getBoundingClientRect();
      var pad = 8, dx = 0, dy = 0;
      if (rect.left < v.left + pad) dx = (v.left + pad) - rect.left;
      else if (rect.right > v.right - pad) dx = (v.right - pad) - rect.right;
      if (rect.top < v.top + pad) dy = (v.top + pad) - rect.top;
      else if (rect.bottom > v.bottom - pad) dy = (v.bottom - pad) - rect.bottom;
      if (!dx && !dy) return;
      tx += dx; ty += dy;
      clampPan();
      apply();
    };

    var zin = root.querySelector('[data-zoom-in]'), zout = root.querySelector('[data-zoom-out]'), zr = root.querySelector('[data-zoom-reset]');
    if (zin) zin.addEventListener('click', function () { var c = center(); zoomAt(1.3, c[0], c[1]); });
    if (zout) zout.addEventListener('click', function () { var c = center(); zoomAt(1 / 1.3, c[0], c[1]); });
    if (zr) zr.addEventListener('click', reset);
    apply();
  }
  document.querySelectorAll('[data-zoom]').forEach(initZoom);

  // ---- clickable data nodes (issue #14) -------------------------------------------------
  // The drawing is served into an <img>, so nothing inside it can be clicked and nothing inside
  // it can run. The hotspots are therefore laid over the picture, positioned in percentages of
  // the viewBox so they stay registered at any zoom, and the properties come from a sidecar the
  // renderer wrote in the same pass that drew the node.
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
      // textContent instead renders the string "[object HTMLElement]", which it did.
      dd.appendChild(value);
    } else {
      dd.textContent = value;
    }
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  // How each diff classification reads on screen (issue #24). Unchanged is deliberately absent:
  // saying so on every one of a hundred nodes buries the five that did change.
  var CHANGE_WORD = {
    added: 'added',
    removed: 'removed',
    reannotated: 're-annotated',
    relabelled: 'relabelled',
    moved: 'moved',
  };

  function showNode(panel, node) {
    var body = panel.querySelector('.node-panel__body');
    // Unhide before filling. The body is a polite live region, and a live region inside a
    // hidden subtree announces nothing — populating first and revealing second would leave a
    // screen-reader user arrowing through nodes in silence, which is the whole point of it.
    panel.hidden = false;
    body.textContent = '';
    var h = document.createElement('p');
    h.className = 'node-panel__title';
    // A node with no TextLabel is legal GPML and reads as an empty panel otherwise.
    h.textContent = node.label || '(unlabelled node)';
    if (CHANGE_WORD[node.__change]) {
      var tag = document.createElement('span');
      tag.className = 'node-panel__change node-panel__change--' + node.__change;
      tag.textContent = CHANGE_WORD[node.__change];
      h.appendChild(document.createTextNode(' '));
      h.appendChild(tag);
    }
    body.appendChild(h);

    var dl = document.createElement('dl');
    dl.className = 'node-panel__props';
    labelRow(dl, 'Type', node.type || 'Unknown');
    if (node.identifier) {
      labelRow(dl, node.database || 'Identifier',
        node.url && /^https:\/\//.test(node.url)
          ? { href: node.url, text: node.identifier }
          : node.identifier);
    } else {
      // The single most common curation defect, and the reason a curator clicks a node at all.
      labelRow(dl, 'Identifier', 'Not annotated');
    }
    if (node.comment) labelRow(dl, 'Comment', node.comment);
    // What this node used to say, struck through beside what it says now (issue #24). A
    // re-annotation is invisible in the drawing — the box is identical and only the Xref behind
    // it moved — so the panel is the only place it can be read at all.
    if (node.__was) {
      var wasXref = (node.__was.database || node.__was.identifier)
        ? ((node.__was.database ? node.__was.database + ':' : '') + (node.__was.identifier || ''))
        : 'not annotated';
      if (node.__change === 'relabelled') labelRow(dl, 'Was', strike(node.__was.label || '(unlabelled)'));
      else labelRow(dl, 'Was', strike(wasXref));
    }
    body.appendChild(dl);
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
  function readingOrder(nodes) {
    var rows = [];
    nodes.slice()
      .sort(function (a, b) { return (a.top - b.top) || (a.left - b.left); })
      .forEach(function (n) {
        var row = rows[rows.length - 1];
        if (row && n.top < row.limit) row.items.push(n);
        else rows.push({ items: [n], limit: n.top + n.height / 2 });
      });
    var out = [];
    rows.forEach(function (row) {
      row.items.sort(function (a, b) { return a.left - b.left; });
      out.push.apply(out, row.items);
    });
    return out;
  }

  function initHotspots(root) {
    var img = root.querySelector('.zoom__img');
    var layer = root.querySelector('.zoom__hotspots');
    var panel = root.querySelector('.node-panel');
    if (!img || !layer || !panel || !img.getAttribute('src')) return;
    // /previews/<pr>/<side>.svg -> /previews/<pr>/<side>-nodes.json. Derived rather than passed
    // through the template so the preview contract and every call site stay untouched.
    var src = img.getAttribute('src');
    if (!/\.svg$/.test(src)) return;

    // Pin the layer to the image's own box rather than trusting it to fill the stage. The
    // image is letterboxed inside the stage by max-width/max-height, and that spare margin is
    // not shared equally on both axes, so a layer stretched to the stage puts every hotspot a
    // node's height out of place -- observed doing exactly that. Measuring is immune to however
    // the sizing chain resolves.
    function syncLayer() {
      layer.style.left = img.offsetLeft + 'px';
      layer.style.top = img.offsetTop + 'px';
      layer.style.width = img.offsetWidth + 'px';
      layer.style.height = img.offsetHeight + 'px';
    }
    // Explicit sync points, deliberately not a ResizeObserver on the image. Observing the image
    // and then writing layout from the callback is a feedback loop: the write triggers a
    // recalculation, the recalculation re-fires the observer, and the renderer locks up. It did
    // exactly that here -- the tab stopped painting. These three cover every way the image can
    // change size, and none of them is re-entrant.
    img.addEventListener('load', syncLayer);
    window.addEventListener('resize', syncLayer);
    // initZoom owns the only other thing that resizes it. It calls this after each apply().
    root.__syncOverlay = syncLayer;
    if (img.complete && img.naturalWidth) syncLayer();

    // Roving tabindex over the hotspots (issue #19). Buttons are what make the overlay usable
    // without a mouse and also what put one tab stop per node in the middle of the page — the
    // 88-node pathway that came through the portal is 88 stops before the checklist, 176 on an
    // update card. Exactly one hotspot is in the tab order at a time; the arrow keys move
    // between them. tabindex="-1" on all of them would have bought the same saving by deleting
    // the keyboard path, and the properties panel is precisely what a screen-reader user needs,
    // because they cannot read the diagram at all.
    var order = [];
    var current = 0;
    // Set while focus is being handed back to a hotspot after the panel was dismissed. Selection
    // follows focus, so without this the close button would put focus on the node it was
    // describing and the focus handler would immediately reopen the panel it just closed.
    var restoring = false;

    function select(i, moveFocus) {
      if (!order.length) return;
      i = Math.max(0, Math.min(order.length - 1, i));
      order[current].tabIndex = -1;
      order[current].classList.remove('hotspot--active');
      current = i;
      var b = order[current];
      b.tabIndex = 0;
      b.classList.add('hotspot--active');
      if (moveFocus) {
        b.focus();
        // initZoom runs over the same roots first, so this is set by now. Arrowing across a
        // zoomed-in diagram otherwise walks focus off the clipped edge.
        if (root.__revealRect) root.__revealRect(b.getBoundingClientRect());
      }
      if (!restoring) showNode(panel, b.__node);
    }

    function refocusCurrent() {
      if (!order.length) return;
      restoring = true;
      order[current].focus();
      restoring = false;
    }

    // Which frame this is, and where its diff lives. /previews/<pr>/after.svg -> "after" and
    // /previews/<pr>/diff.json. Both derived from the src so the preview contract and every
    // call site stay untouched.
    var side = (src.match(/\/(before|after)\.svg$/) || [])[1];
    var diffUrl = src.replace(/\/(before|after)\.svg$/, '/diff.json');

    Promise.all([
      fetch(src.replace(/\.svg$/, '-nodes.json'), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; }),
      // 404 is the normal answer for a new pathway: one side, nothing to compare against. The
      // overlay must not wait on it failing, hence one Promise.all rather than a chain.
      side ? fetch(diffUrl, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; }) : Promise.resolve(null),
    ])
      .then(function (both) {
        var nodes = both[0], diff = both[1];
        // null = nothing on file (a placeholder side, or a cache predating this). Leave the
        // static image exactly as it was rather than showing an overlay we cannot trust.
        if (!nodes || !nodes.length) return;
        // The diff is index-aligned with this exact file, so a length mismatch means the two
        // were written by different renders and every colour would be on the wrong box. Drop
        // the whole comparison rather than show one that is quietly a node out.
        var marks = diff && diff[side] && diff[side].length === nodes.length ? diff[side] : null;
        if (marks) {
          nodes.forEach(function (n, i) { n.__change = marks[i].change; n.__was = marks[i].was; });
          root.classList.add('zoom--diffed');
        }
        readingOrder(nodes).forEach(function (node, i) {
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'hotspot' + (node.__change ? ' hotspot--' + node.__change : '');
          b.__node = node;
          // Only the first is reachable by Tab; the rest are reached with the arrow keys.
          b.tabIndex = i === 0 ? 0 : -1;
          b.style.left = node.left + '%';
          b.style.top = node.top + '%';
          b.style.width = node.width + '%';
          b.style.height = node.height + '%';
          // The colour is the whole point of the diff and is exactly what a screen-reader user
          // cannot see, so the classification goes in the name as well.
          b.setAttribute('aria-label', (node.label || 'Unlabelled node')
            + (CHANGE_WORD[node.__change] ? ', ' + CHANGE_WORD[node.__change] : '')
            + ' — show properties');
          b.addEventListener('click', function (e) {
            e.stopPropagation();
            select(order.indexOf(b), false);
          });
          // Selection follows focus, so arrowing across the diagram reads out each node in turn
          // rather than requiring a second keystroke per node to open the panel. Safari does not
          // focus a button on click, which is why the click handler selects on its own.
          b.addEventListener('focus', function () { select(order.indexOf(b), false); });
          order.push(b);
          layer.appendChild(b);
        });
        layer.hidden = false;
        root.classList.add('zoom--interactive');
      })
      .catch(function () { /* overlay is an enhancement; the picture still works */ });

    layer.addEventListener('keydown', function (e) {
      if (!order.length) return;
      var next;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = current + 1;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = current - 1;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = order.length - 1;
      else return;
      e.preventDefault();
      // The viewport handles these too — arrows pan the diagram and Home resets the zoom. Both
      // would fire on the way up and yank the view out from under the node being read.
      e.stopPropagation();
      select(next, true);
    });

    function close() {
      panel.hidden = true;
      layer.querySelectorAll('.hotspot--active').forEach(function (o) {
        o.classList.remove('hotspot--active');
      });
    }
    panel.querySelector('.node-panel__close').addEventListener('click', function () {
      // Focus is about to be destroyed along with the button's visibility; hand it back to the
      // node the panel was describing rather than dropping it on <body> at the top of the page.
      close();
      refocusCurrent();
    });
    root.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || panel.hidden) return;
      // Escape means "close the panel", not "leave the overlay": focus stays on the node so the
      // arrow keys keep working from where the reader was.
      e.stopPropagation();
      close();
      if (layer.contains(document.activeElement) || panel.contains(document.activeElement)) {
        refocusCurrent();
      }
    });
  }
  document.querySelectorAll('[data-zoom]').forEach(initHotspots);

  // A node that only moved is the category the issue itself calls noise, and after a re-layout
  // it can be every node on the diagram. The hotspot stays clickable either way; only its
  // colour goes, so hiding the noise never hides the node behind it (issue #24).
  document.addEventListener('change', function (e) {
    var box = e.target.closest('.diff-hide-moved');
    if (!box) return;
    var card = box.closest('.review-card');
    if (card) card.classList.toggle('review-card--hide-moved', !box.checked);
  });

  document.addEventListener('change', function (e) {
    var select = e.target.closest('.assign__select');
    if (!select) return;
    var card = select.closest('.review-card');
    var pr = card.getAttribute('data-pr');
    select.disabled = true;
    postForm('/api/reviews/' + pr + '/assign', { curator: select.value })
      .then(function (res) {
        select.disabled = false;
        if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
        // The server-rendered "assigned to" line would otherwise contradict the select.
        var line = card.querySelector('.review-card__submitter');
        if (line && res.body && res.body.submitter) {
          line.textContent = '';
          line.appendChild(document.createTextNode('Submitted by '));
          var who = document.createElement('strong');
          who.textContent = '@' + res.body.submitter;
          line.appendChild(who);
          if (res.body.assigned_curator) {
            line.appendChild(document.createTextNode(' · assigned to '));
            var cur = document.createElement('strong');
            cur.textContent = '@' + res.body.assigned_curator;
            line.appendChild(cur);
          }
        }
        toast(select.value ? 'Assigned to @' + select.value + '.' : 'Unassigned.', 'success');
      })
      .catch(function () { select.disabled = false; toast('Could not reach the server. Try again.', 'error'); });
  });

  // ---------- "On GitHub, not in the portal" (dashboard, curators) ----------
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.btn--adopt');
    if (!btn || btn.disabled) return;
    var pr = btn.getAttribute('data-pr');
    btn.disabled = true;
    btn.textContent = 'Adopting…';
    postForm('/api/reviews/' + pr + '/adopt', {})
      .then(function (res) {
        if (res.ok && res.body && res.body.adopted) {
          location.href = '/dashboard/' + pr;
          return;
        }
        btn.disabled = false;
        btn.textContent = 'Adopt';
        var why = res.ok && res.body && res.body.reason
          ? 'Not adopted: pull request #' + pr + ' ' + res.body.reason + '.'
          : describeError(res.status, res.body);
        toast(why, 'error');
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = 'Adopt';
        toast('Could not reach the server. Try again.', 'error');
      });
  });

  // ---------- pull request conversation (review page) ----------
  // Everything from GitHub is set as text. Comment bodies are Markdown written by anyone who can
  // comment on the pull request; none of it is ever parsed as HTML here.
  function conversationEntry(item) {
    var li = document.createElement('li');
    li.className = 'conversation__item conversation__item--' + item.role;
    var head = document.createElement('p');
    head.className = 'conversation__head';
    var who = document.createElement('strong');
    who.textContent = '@' + item.author;
    head.appendChild(who);
    if (item.role === 'curator') head.appendChild(span('conversation__tag', 'curator'));
    var what = item.verdict ? ' ' + item.verdict
      : item.kind === 'line' ? ' commented on ' + (item.path || 'the diff')
      : ' commented';
    head.appendChild(document.createTextNode(what));
    if (item.created_at) {
      var when = document.createElement('time');
      when.dateTime = item.created_at;
      when.textContent = ' · ' + item.created_at.slice(0, 10);
      head.appendChild(when);
    }
    if (item.url) {
      var a = document.createElement('a');
      a.href = item.url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.className = 'conversation__link';
      a.textContent = 'on GitHub';
      head.appendChild(document.createTextNode(' · '));
      head.appendChild(a);
    }
    var body = document.createElement('div');
    body.className = 'conversation__body';
    body.textContent = item.body || (item.verdict ? '' : '(empty)');
    if (item.collapsed) {
      var fold = document.createElement('details');
      var sum = document.createElement('summary');
      sum.appendChild(head);
      fold.appendChild(sum);
      fold.appendChild(body);
      li.appendChild(fold);
    } else {
      li.appendChild(head);
      if (body.textContent) li.appendChild(body);
    }
    if (item.verdict === 'requested changes') li.classList.add('conversation__item--changes');
    return li;
  }

  function loadConversation(section) {
    var pr = section.getAttribute('data-pr');
    var status = section.querySelector('.conversation__status');
    var list = section.querySelector('.conversation__list');
    return fetch('/api/reviews/' + pr + '/conversation')
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
      })
      .then(function (res) {
        if (!res.ok) { status.textContent = describeError(res.status, res.body); return; }
        list.textContent = '';
        var items = res.body.items || [];
        var people = items.filter(function (i) { return !i.collapsed; }).length;
        status.textContent = items.length === 0
          ? 'Nobody has commented on this pull request yet.'
          : people + (people === 1 ? ' comment' : ' comments') + ' from people' +
            (items.length > people ? ', and ' + (items.length - people) + ' from bots, folded.' : '.');
        items.forEach(function (i) { list.appendChild(conversationEntry(i)); });
      })
      .catch(function () { status.textContent = 'Could not load the conversation. Reload to try again.'; });
  }

  document.querySelectorAll('.conversation[data-pr]').forEach(loadConversation);

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.btn--comment');
    if (!btn || btn.disabled) return;
    var section = btn.closest('.conversation');
    var box = section.querySelector('.comment-box__text');
    var text = (box.value || '').trim();
    if (!text) { toast('Write something first.', 'error'); box.focus(); return; }
    btn.disabled = true;
    postForm('/api/reviews/' + section.getAttribute('data-pr') + '/comments', { body: text })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
        box.value = '';
        toast('Comment posted on GitHub.', 'success');
        loadConversation(section);
      })
      .catch(function () { btn.disabled = false; toast('Could not reach the server. Try again.', 'error'); });
  });
})();

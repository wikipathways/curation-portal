"""The folded "Review details" in a real browser: viewer.js and refresh.js together.

A viewer inside the closed fold is left unmounted (its canvas has no size to fit a drawing to) and
mounts when the fold opens. refresh.js keeps an opened fold open across a card swap, matched by its
key rather than its position, so a fold that first appears (on approval) does not inherit the open
state of whatever panel used to come first. Skipped where no Chrome is installed, like the viewer's
own browser tests.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess

import pytest

from tests.test_viewer_browser import _PRELUDE, CHROME, ROOT, _solo, site  # noqa: F401

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium to run it in")


def _card(body: str, *, fold: bool, open_: bool = False, extra: str = "") -> str:
    inner = (
        f'<details class="review-card__more" data-details-key="review-details"'
        f'{" open" if open_ else ""}><summary>Review details</summary>'
        f'<div class="review-card__more-body">{extra}{body}</div></details>'
        if fold
        else f"{extra}{body}"
    )
    return (
        '<article class="review-card review-card--expanded" data-pr="8" data-status="approved" '
        f'data-poll-after="0" data-expanded="1"><section class="tracker"></section>{inner}'
        "</article>"
    )


def _run(site, page_body: str, scenario: str) -> dict:  # noqa: F811
    root, base = site
    shutil.copy(ROOT / "static" / "refresh.js", root / "refresh.js")
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="app.css"><link rel="stylesheet" href="viewer.css">'
        f"{_PRELUDE}</head><body><main id=\"main\"><div class=\"container\">{page_body}</div>"
        '</main><pre id="out"></pre><script src="viewer.js"></script>'
        '<script src="refresh.js"></script>'
        "<script>\n" + scenario + "\n</script></body></html>"
    )
    (root / "page.html").write_text(page, encoding="utf-8")
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={root / 'chrome-profile'}",
        "--window-size=1400,1000", "--virtual-time-budget=15000", "--dump-dom",
        f"{base}/page.html",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.insert(1, "--no-sandbox")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    m = re.search(r'<pre id="out">(.*?)</pre>', proc.stdout, re.S)
    assert m and m.group(1).strip(), f"scenario produced nothing\n{proc.stderr[-2000:]}"
    result = json.loads(html.unescape(m.group(1)))
    assert result.pop("errors") == []
    return result


def test_a_viewer_in_the_closed_fold_mounts_when_it_opens(site):  # noqa: F811
    out = _run(
        site,
        _card(_solo("after.svg"), fold=True),
        """
        // After viewer.js has mounted the page (it waits for DOMContentLoaded).
        window.addEventListener('load', function () {
          var r = document.querySelector('[data-viewer]');
          var before = !!r.__wpViewer;
          var d = document.querySelector('details[data-details-key]');
          d.open = true;
          setTimeout(function () {
            ready(function (vs) {
              done({ before: before, after: !!vs[0],
                     ready: vs[0].root.classList.contains('viewer--ready'),
                     width: vs[0].canvas.getBoundingClientRect().width > 0 });
            });
          }, 50);
        });
        """,
    )
    assert out == {"before": False, "after": True, "ready": True, "width": True}


def test_an_opened_fold_stays_open_across_a_refresh(site):  # noqa: F811
    fresh = _card(_solo("after.svg"), fold=True)
    out = _run(
        site,
        _card(_solo("after.svg"), fold=True),
        "var FRESH = " + json.dumps(fresh) + ";\n"
        + """
        window.fetch = function () {
          return Promise.resolve(new Response(FRESH, { status: 200, headers: { ETag: '"2"' } }));
        };
        var old = document.querySelector('article.review-card');
        old.querySelector('details[data-details-key]').open = true;
        WPRefresh.card(old).then(function (now) {
          setTimeout(function () {
            ready(function (vs) {
              done({ swapped: now !== old,
                     open: now.querySelector('details[data-details-key]').open,
                     mounted: !!vs[0] && vs[0].root.isConnected });
            });
          }, 50);
        });
        """,
    )
    assert out == {"swapped": True, "open": True, "mounted": True}


def test_a_fold_that_first_appears_starts_closed(site):  # noqa: F811
    # The approved card arrives with the fold; the open card it replaces had a panel open first
    # in the card (the quality summary, say). Matched by position, the fold would open itself.
    panel = '<details class="quality__rest" open><summary>more</summary></details>'
    fresh = _card(_solo("after.svg"), fold=True, extra=panel.replace(" open", ""))
    out = _run(
        site,
        _card(_solo("after.svg"), fold=False, extra=panel),
        "var FRESH = " + json.dumps(fresh) + ";\n"
        + """
        window.fetch = function () {
          return Promise.resolve(new Response(FRESH, { status: 200, headers: { ETag: '"2"' } }));
        };
        var old = document.querySelector('article.review-card');
        WPRefresh.card(old).then(function (now) {
          done({ fold: now.querySelector('details[data-details-key]').open,
                 inner: now.querySelector('details.quality__rest').open,
                 mounted: !!document.querySelector('[data-viewer]').__wpViewer });
        });
        """,
    )
    # The fold stays closed, the panel that was open keeps its state, and nothing is mounted in
    # the closed fold.
    assert out == {"fold": False, "inner": True, "mounted": False}


def test_an_opened_fold_stays_open_across_the_queue_refresh(site):  # noqa: F811
    # The queue's Refresh replaces the whole list rather than one card. It used to swap the cards
    # in without carrying over which panels were open, so an opened fold closed on every press.
    fresh_page = (
        '<!doctype html><html><body><main id="main"><div class="container">'
        + _card(_solo("after.svg"), fold=True)
        + "</div></main></body></html>"
    )
    out = _run(
        site,
        _card(_solo("after.svg"), fold=True),
        "var FRESH = " + json.dumps(fresh_page) + ";\n"
        + """
        window.fetch = function () {
          return Promise.resolve(new Response(FRESH, { status: 200 }));
        };
        var old = document.querySelector('article.review-card');
        old.querySelector('details[data-details-key]').open = true;
        WPRefresh.page().then(function () {
          setTimeout(function () {
            var now = document.querySelector('article.review-card');
            ready(function (vs) {
              done({ swapped: now !== old,
                     open: now.querySelector('details[data-details-key]').open,
                     mounted: !!vs[0] && vs[0].root.isConnected });
            });
          }, 50);
        });
        """,
    )
    assert out == {"swapped": True, "open": True, "mounted": True}

"""static/viewer.js in a real browser, against the fixtures in tests/fixtures/viewer/.

Each test serves the viewer, its stylesheets and the fixtures from a temporary directory, renders
the page with the real _viewer.html macro, runs one scenario in headless Chrome and reads the
result back out of the dumped DOM. Skipped when no Chrome or Chromium is installed (set
VIEWER_TEST_CHROME to point at one).
"""

from __future__ import annotations

import functools
import html
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_viewer_markup import _render_pair

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "viewer"


def _chrome() -> str | None:
    env = os.environ.get("VIEWER_TEST_CHROME")
    if env:
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


CHROME = _chrome()
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium to run the viewer in")

# Reduced motion makes every view change land at once, so a scenario can read the viewBox right
# after the call that moved it. `ready` waits for every mounted viewer to finish loading.
_PRELUDE = """
<script>
window.WP = { toast: function () {} };
window.matchMedia = function (q) {
  return { matches: /reduce/.test(q), addListener: function () {} };
};
window.__errors = [];
window.onerror = function (m) { window.__errors.push(String(m)); };
function done(result) {
  result.errors = window.__errors;
  document.getElementById('out').textContent = JSON.stringify(result);
}
function key(el, k) {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
}
function viewers() {
  var roots = document.querySelectorAll('[data-viewer]');
  return Array.prototype.map.call(roots, function (r) { return r.__wpViewer; });
}
function ready(cb) {
  (function poll() {
    var vs = viewers();
    var all = vs.every(function (v) {
      return v && (v.svg || v.root.classList.contains('viewer--static'));
    });
    if (all) cb(vs);
    else setTimeout(poll, 20);
  })();
}
function side(el) {
  var r = el && el.closest && el.closest('[data-viewer]');
  if (!r) return 'none';
  var what = el.getAttribute('data-id') || el.getAttribute('data-v') || el.className;
  return r.getAttribute('data-side') + ':' + what;
}
</script>
"""


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - keep pytest output clean
        pass


@pytest.fixture
def site(tmp_path):
    for name in ("viewer.js", "viewer.css", "app.css"):
        shutil.copy(ROOT / "static" / name, tmp_path / name)
    for f in FIXTURES.iterdir():
        shutil.copy(f, tmp_path / f.name)
    handler = functools.partial(_Quiet, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _update(mode: str = "full") -> str:
    return _render_pair(
        {
            "pr_number": 7,
            "kind": "update",
            "pipeline": None,
            "preview": {
                "status": "ready",
                "before_svg_url": "before.svg",
                "after_svg_url": "after.svg",
                "before_graph_url": "before-graph.json",
                "after_graph_url": "after-graph.json",
                "diff_url": "diff.json",
            },
        },
        mode,
    )


def _solo(svg: str, mode: str = "full") -> str:
    return _render_pair(
        {
            "pr_number": 8,
            "kind": "new",
            "pipeline": None,
            "preview": {
                "status": "ready",
                "after_svg_url": svg,
                "after_graph_url": None,
                "diff_url": None,
            },
        },
        mode,
    )


def _run(site, body: str, scenario: str) -> dict:
    root, base = site
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="app.css"><link rel="stylesheet" href="viewer.css">'
        f"{_PRELUDE}</head><body><main>"
        f'<article class="review-card review-card--expanded">{body}</article>'
        '</main><pre id="out"></pre><script src="viewer.js"></script>'
        "<script>\n" + scenario + "\n</script></body></html>"
    )
    (root / "page.html").write_text(page, encoding="utf-8")
    profile = root / "chrome-profile"
    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        "--window-size=1400,1000",
        "--virtual-time-budget=15000",
        "--dump-dom",
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


# -- the sanitiser ---------------------------------------------------------------------------


def test_sanitiser_strips_everything_hostile(site):
    out = _run(
        site,
        _solo("hostile.svg"),
        """
        ready(function (vs) {
          var svg = vs[0].canvas.querySelector('svg');
          done({ pwned: String(window.__pwned),
                 ready: vs[0].root.classList.contains('viewer--ready'),
                 svg: svg ? svg.outerHTML : null, prefix: vs[0].prefix });
        });
        """,
    )
    assert out["pwned"] == "undefined"
    assert out["ready"] is True
    svg = out["svg"]
    assert svg
    lowered = svg.lower()
    for bad in (
        "<script",
        "<style",
        "foreignobject",
        "<a ",
        "<use",
        "<image",
        "<animate",
        "<set",
        "onload",
        "onclick",
        "href",
        "javascript",
        "style=",
        "evil",
        "data-evil",
    ):
        assert bad not in lowered, bad
    # What should survive does, with its id re-pointed at this mount's prefix: a reference to
    # it from a url() stays a same-document reference, and anything else in a url() is gone.
    prefix = out["prefix"]
    assert f'id="{prefix}ok"' in svg and 'class="kept' in svg and 'data-id="n-x"' in svg
    assert f'stroke="url(#{prefix}ok)"' in svg
    assert re.findall(r"url\(([^)]*)\)", svg) == [f"#{prefix}ok"]
    assert "transform=" not in svg  # "java\tscript:" is still javascript:
    assert "Label" in svg and "<tspan>inner</tspan>" in svg
    assert svg.count("<svg") == 2  # the nested <svg>, minus its onload


def test_sanitiser_refuses_a_drawing_past_the_node_cap(site):
    root, _ = site
    (root / "huge.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">' + "<g/>" * 61000 + "</svg>"
    )
    out = _run(
        site,
        _solo("huge.svg"),
        """
        ready(function (vs) {
          done({ static: vs[0].root.classList.contains('viewer--static'), svg: !!vs[0].svg,
                 img: !vs[0].img.hidden });
        });
        """,
    )
    # The plain picture stays; nothing was inlined.
    assert out == {"static": True, "svg": False, "img": True}


# -- the linked pair -------------------------------------------------------------------------


def test_stepping_through_changes_keeps_focus_on_the_change(site):
    # The change list runs across both sides; a removed node exists only on the before side.
    # Letting go of the selection on the other side must not pull focus over there.
    out = _run(
        site,
        _update(),
        """
        ready(function (vs) {
          var b = vs[0], a = vs[1], steps = [];
          a.canvas.focus();
          for (var i = 0; i < a.pair.changes.length + 1; i++) {
            key(document.activeElement, 'n');
          }
          // The keypresses resolve after activate(); give them a moment.
          setTimeout(function () {
            var it = a.pair.changes[a.pair.index];
            steps.push({ target: it.viewer.side + ':' + it.node.id,
                         focus: side(document.activeElement) });
            // Both panels open, then Enter on a node that has no counterpart.
            var bn = b.els['n-d002']; bn.focus(); key(bn, 'Enter');
            var add = a.els['n-d003']; add.focus(); key(add, 'Enter');
            var names = a.pair.changes.map(function (c) {
              return c.viewer.side + ':' + c.node.id;
            });
            done({ changes: names,
                   steps: steps, afterEnter: side(document.activeElement),
                   beforePanel: b.panel.hidden, afterPanel: a.panel.hidden });
          }, 300);
        });
        """,
    )
    assert out["changes"][-1] == "before:n-d009"
    # Five presses over four changes: wrapped round to the first one.
    assert out["steps"] == [{"target": out["changes"][0], "focus": out["changes"][0]}]
    assert out["afterEnter"] == "after:n-d003"
    assert out["beforePanel"] is True and out["afterPanel"] is False


def test_stepping_onto_a_removed_node_focuses_it(site):
    out = _run(
        site,
        _update(),
        """
        ready(function (vs) {
          var a = vs[1], seen = [];
          a.canvas.focus();
          (function next(i) {
            if (i === a.pair.changes.length) { done({ seen: seen }); return; }
            key(document.activeElement, 'n');
            setTimeout(function () {
              var it = a.pair.changes[a.pair.index];
              seen.push([it.viewer.side + ':' + it.node.id, side(document.activeElement)]);
              next(i + 1);
            }, 50);
          })(0);
        });
        """,
    )
    assert len(out["seen"]) == 4
    for target, focus in out["seen"]:
        assert target == focus


def test_selecting_a_node_keeps_both_sides_on_one_view(site):
    out = _run(
        site,
        _update(),
        """
        ready(function (vs) {
          var b = vs[0], a = vs[1], res = {};
          function fit(box) {
            [a, b].forEach(function (v) { v.fit = box; v.vb = box.slice(); v.applyViewBox(); });
          }
          // A diagram much wider than a node: centring the counterpart alone would zoom one side.
          fit([0, 0, 4000, 3000]);
          b.clickAt(b.els['n-d002']);
          res.click = { b: b.vb, a: a.vb };
          fit([0, 0, 400, 300]);
          // Zoomed onto the before side's n-d005, which moved 20 units right on the after side
          // and is out of view there: the after side pans to it, and the before side follows.
          b.setBox([300, 40, 62, 40], { raw: true });
          b.clickAt(b.els['n-d005']);
          res.moved = { b: b.vb, a: a.vb, shows: a.shows(a.byId['n-d005'].item.bbox),
                        selected: a.selected && a.selected.item.id };
          done(res);
        });
        """,
    )
    assert out["click"]["a"] == out["click"]["b"] == [0, 0, 4000, 3000]
    moved = out["moved"]
    assert moved["a"] == moved["b"]
    assert moved["shows"] is True and moved["selected"] == "n-d005"


def test_unmount_and_remount_on_the_same_element(site):
    out = _run(
        site,
        _update(),
        """
        ready(function (vs) {
          var root = vs[1].root;
          var old = vs[1];
          WPViewer.unmountAll(root);
          WPViewer.mountAll(root);
          ready(function (vs2) {
            var v = vs2[1];
            root.querySelector('[data-v="legend"]').click();
            var legendOpen = !v.legend.hidden;
            var n = v.els['n-d001']; n.focus();
            var from = v.current;
            key(n, 'ArrowRight');
            done({ fresh: v !== old, oldSvg: old.svg === null, legendOpen: legendOpen,
                   from: from, to: v.current,
                   svgs: root.querySelectorAll('svg.viewer__svg').length });
          });
        });
        """,
    )
    assert out == {
        "fresh": True,
        "oldSvg": True,
        "legendOpen": True,
        "from": 0,
        "to": 1,
        "svgs": 1,
    }


# -- the wheel before the drawing has arrived -------------------------------------------------


def test_ctrl_wheel_never_zooms_the_page_while_the_drawing_loads(site):
    out = _run(
        site,
        _solo("after.svg", "lite") + _solo("missing.svg"),
        """
        function wheel(v) {
          var e = new WheelEvent('wheel',
            { deltaY: -100, ctrlKey: true, bubbles: true, cancelable: true });
          v.canvas.dispatchEvent(e);
          return e.defaultPrevented;
        }
        setTimeout(function () {
          var vs = viewers(), lite = vs[0], broken = vs[1];
          var res = { liteMounted: !!lite.svg, lite: wheel(lite), plainWheel: false };
          var plain = new WheelEvent('wheel', { deltaY: 100, bubbles: true, cancelable: true });
          lite.canvas.dispatchEvent(plain);
          res.plainWheel = plain.defaultPrevented;
          ready(function () {
            // A viewer that gave up on its drawing hands page zoom back to the browser.
            res.broken = wheel(broken);
            res.brokenStatic = broken.root.classList.contains('viewer--static');
            res.liteWoke = !!lite.svg;
            done(res);
          });
        }, 200);
        """,
    )
    assert out == {
        "liteMounted": False,
        "lite": True,
        "plainWheel": False,
        "broken": False,
        "brokenStatic": True,
        "liteWoke": True,
    }


# -- the toolbar -----------------------------------------------------------------------------


def test_toolbar_is_one_tab_stop_and_the_arrows_move_inside_it(site):
    out = _run(
        site,
        _update(),
        """
        ready(function (vs) {
          var v = vs[1], tb = v.root.querySelector('.viewer__toolbar'), seen = [];
          function stops() {
            var bs = tb.querySelectorAll('button');
            return Array.prototype.filter.call(bs, function (b) { return b.tabIndex === 0; })
              .map(function (b) { return b.getAttribute('data-v'); });
          }
          seen.push(stops());
          var first = tb.querySelector('button'); first.focus();
          function press(k) {
            key(document.activeElement, k);
            seen.push([document.activeElement.getAttribute('data-v')].concat(stops()));
          }
          press('ArrowRight'); press('End'); press('ArrowRight'); press('ArrowLeft');
          // Tab stops in the card: per side the toolbar, the search box, the canvas, one node.
          var card = document.querySelector('.review-card');
          var all = card.querySelectorAll('button, input, [tabindex]');
          var tabbable = Array.prototype.filter.call(all, function (e) {
            return e.tabIndex >= 0 && !e.disabled && !e.closest('[hidden]');
          }).map(side);
          // Clicking a button makes it the stop Tab comes back to.
          var legend = tb.querySelector('[data-v="legend"]'); legend.focus(); legend.click();
          seen.push(stops());
          done({ seen: seen, role: tb.getAttribute('role'), tabbable: tabbable });
        });
        """,
    )
    assert out["role"] == "toolbar"
    assert out["seen"] == [
        ["zoom-out"],
        ["fit", "fit"],
        ["next-change", "next-change"],
        ["zoom-out", "zoom-out"],
        ["next-change", "next-change"],
        ["legend"],
    ]
    # The after side's toolbar stop is where the arrows left it.
    assert sorted(out["tabbable"]) == sorted(
        [f"{s}:{w}" for s in ("before", "after") for w in ("search", "viewer__canvas", "n-d001")]
        + ["before:zoom-out", "after:next-change"]
    )

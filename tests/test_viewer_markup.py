"""The pathway viewer's server-side half: the markup static/viewer.js mounts on, and its allowlist.

The interactive behaviour itself (sanitiser, selection, pan/zoom, diff marks) runs in a browser
and is exercised against the hand-written fixtures in tests/fixtures/viewer/. What a Python test
can pin is the contract around it: which pages get which mode, which URLs the client is handed,
and that the client's allowlist still covers everything the renderer may emit.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.github import FakeGitHubClient
from app.main import templates
from tests.test_api import _DIFF_AFTER, _DIFF_BEFORE, GOOD_GPML, _authed_app, _login, _settings

ROOT = Path(__file__).resolve().parent.parent
VIEWER_JS = ROOT / "static" / "viewer.js"
FIXTURES = ROOT / "tests" / "fixtures" / "viewer"
SVG_NS = "{http://www.w3.org/2000/svg}"

# Contract K1: the only vocabulary the renderer writes (plus data-*).
K1_ELEMENTS = {
    "svg",
    "title",
    "defs",
    "marker",
    "g",
    "path",
    "rect",
    "ellipse",
    "circle",
    "polygon",
    "polyline",
    "line",
    "text",
    "tspan",
}
K1_ATTRIBUTES = {
    "xmlns",
    "viewBox",
    "width",
    "height",
    "id",
    "class",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "d",
    "points",
    "transform",
    "fill",
    "fill-opacity",
    "stroke",
    "stroke-width",
    "stroke-dasharray",
    "stroke-opacity",
    "stroke-linejoin",
    "stroke-linecap",
    "marker-start",
    "marker-end",
    "markerWidth",
    "markerHeight",
    "refX",
    "refY",
    "orient",
    "markerUnits",
    "text-anchor",
    "dominant-baseline",
    "font-family",
    "font-size",
    "font-weight",
    "font-style",
    "opacity",
    "role",
    "aria-label",
}
# What must never be let through, whatever the renderer starts emitting.
NEVER = {
    "script",
    "style",
    "foreignObject",
    "use",
    "image",
    "a",
    "href",
    "xlink:href",
    "animate",
    "set",
}


def _allowlist() -> dict:
    text = VIEWER_JS.read_text(encoding="utf-8")
    m = re.search(r"/\* WP-SVG-ALLOWLIST \*/(.*?)/\* END \*/", text, re.S)
    assert m, "viewer.js lost its allowlist block"
    return json.loads(m.group(1))


def _viewers(page: str) -> list[str]:
    return re.findall(r'<div class="viewer viewer--[a-z]+"[^>]*>', page)


def _update_app(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
        existing_contents={f"{repo}#pathways/WP5636/WP5636.gpml": _DIFF_BEFORE.decode()},
    )
    return _authed_app(tmp_path, curators=["curator"], fake=fake)


# -- the allowlist (contract K8) -----------------------------------------------------------


def test_allowlist_block_is_json_and_matches_the_contract():
    allow = _allowlist()
    assert set(allow["elements"]) == K1_ELEMENTS
    assert set(allow["attributes"]) == K1_ATTRIBUTES
    assert not NEVER & (set(allow["elements"]) | set(allow["attributes"]))
    assert not [a for a in allow["attributes"] if a.lower().startswith("on")]


def test_allowlist_covers_everything_the_renderer_may_emit():
    # A renderer that starts writing an element the client does not know would have it stripped
    # without a word. The renderer's own constants are the source of truth.
    svg = pytest.importorskip("app.preview.svg")
    allow = _allowlist()
    assert set(allow["elements"]) >= set(svg.ALLOWED_ELEMENTS)
    assert set(allow["attributes"]) >= set(svg.ALLOWED_ATTRIBUTES)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_viewer_js_parses():
    for name in ("viewer.js", "app.js"):
        r = subprocess.run(
            ["node", "--check", str(ROOT / "static" / name)], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr


# -- the fixtures the browser harness runs against -------------------------------------------


@pytest.mark.parametrize("side", ["before", "after"])
def test_fixtures_follow_the_svg_and_graph_contracts(side):
    allow = _allowlist()
    tree = ET.parse(FIXTURES / f"{side}.svg").getroot()
    assert (FIXTURES / f"{side}.svg").read_text().startswith("<svg")
    data_ids = set()
    for el in tree.iter():
        assert el.tag.startswith(SVG_NS) and el.tag[len(SVG_NS) :] in allow["elements"], el.tag
        for name in el.attrib:
            assert name.startswith("data-") or name in allow["attributes"], name
        if "data-id" in el.attrib:
            data_ids.add(el.attrib["data-id"])
    graph = json.loads((FIXTURES / f"{side}-graph.json").read_text())
    assert graph["version"] == 2
    for kind in ("nodes", "edges", "groups", "labels", "shapes"):
        for item in graph[kind]:
            assert item["id"] in data_ids, item["id"]
    svg = pytest.importorskip("app.preview.svg")
    svg.assert_safe_svg((FIXTURES / f"{side}.svg").read_bytes())


def test_fixture_diff_names_ids_on_both_sides():
    diff = json.loads((FIXTURES / "diff.json").read_text())
    for side in ("before", "after"):
        ids = {n["id"] for n in json.loads((FIXTURES / f"{side}-graph.json").read_text())["nodes"]}
        assert set(diff["ids"][side]) == ids
        assert len(diff[side]) == len(ids)


# -- which pages get which viewer --------------------------------------------------------------


def test_queue_renders_lite_viewers_and_the_review_page_full_ones(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit", files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")}
        ).json()["pr_number"]
        _login(c, "curator")
        queue = c.get("/dashboard").text
        detail = c.get(f"/dashboard/{pr}").text

    # Lite on the queue: nothing beyond the <img> is fetched until somebody reaches for it.
    lite = _viewers(queue)
    assert lite and all('data-mode="lite"' in v for v in lite)
    assert "viewer__search-input" not in queue
    full = _viewers(detail)
    assert len(full) == 1 and 'data-mode="full"' in full[0]
    assert "viewer__search-input" in detail
    v = full[0]
    assert f'data-pr="{pr}"' in v and 'data-side="after"' in v
    assert f'data-svg-url="/previews/{pr}/after.svg' in v
    # A new pathway has nothing to compare against: no diff URL, no change stepping.
    assert 'data-diff-url=""' in v
    assert 'data-v="next-change"' not in detail
    # The fallback picture is in the markup itself, for no-JS and for a failed fetch.
    assert f'class="viewer__fallback" src="/previews/{pr}/after.svg' in detail
    assert 'data-tour="viewer"' in detail and 'data-tour="viewer-toolbar"' in detail


def test_update_renders_a_linked_pair_with_the_diff(tmp_path):
    app, current = _update_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(_DIFF_AFTER), "application/xml")},
        ).json()["pr_number"]
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text

    viewers = _viewers(page)
    assert [re.search(r'data-side="(\w+)"', v).group(1) for v in viewers] == ["before", "after"]
    for v in viewers:
        assert re.search(rf'data-diff-url="/previews/{pr}/diff\.json[^"]*"', v), v
    # Full screen in diff mode takes both sides, so they share one wrapper.
    assert page.count("data-viewer-pair") == 1
    assert 'data-v="next-change"' in page and 'data-v="prev-change"' in page
    assert 'data-tour="diff-summary"' in page
    # The sentence and the "no render" wording the card always had are still there.
    assert "1 re-annotated" in page


def test_failed_render_still_says_so(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit", files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")}
        ).json()["pr_number"]
        app.state.preview.status = lambda _pr: "failed"
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
    assert "Could not draw this pathway from its GPML." in page
    assert not _viewers(page)


# -- the macro against the view-model contract (K4 / K5) --------------------------------------


def _render_pair(r: dict, mode: str = "full") -> str:
    module = templates.env.get_template("_viewer.html").make_module({})
    return str(module.viewer_pair(r, mode))


def _preview(pr: int, **extra) -> dict:
    p = {
        "status": "ready",
        "before_svg_url": f"/previews/{pr}/before.svg?rev=r1",
        "after_svg_url": f"/previews/{pr}/after.svg?rev=r1",
        "rev": "r1",
    }
    p.update(extra)
    return p


def test_viewer_takes_its_urls_from_the_view_model():
    r = {
        "pr_number": 7,
        "kind": "update",
        "pipeline": {"render_svg_url": "/previews/7/pipeline.svg"},
        "preview": _preview(
            7,
            before_graph_url="/previews/7/before-graph.json?rev=r1",
            after_graph_url="/previews/7/after-graph.json?rev=r1",
            diff_url="/previews/7/diff.json?rev=r1",
            renderer=2,
        ),
    }
    html = _render_pair(r)
    before, after = _viewers(html)
    assert 'data-graph-url="/previews/7/before-graph.json?rev=r1"' in before
    assert 'data-graph-url="/previews/7/after-graph.json?rev=r1"' in after
    assert 'data-diff-url="/previews/7/diff.json?rev=r1"' in after
    # The pipeline's drawing is of the submitted version, so only the "after" side offers it.
    assert 'data-render-svg-url=""' in before
    assert 'data-render-svg-url="/previews/7/pipeline.svg"' in after
    assert html.count('data-v="source-db"') == 1
    assert "Database render" in html and "Portal render" in html


def test_new_pathway_has_no_diff_even_when_the_key_is_present():
    r = {
        "pr_number": 8,
        "kind": "new",
        "pipeline": None,
        "preview": _preview(8, after_graph_url=None, diff_url=None),
    }
    html = _render_pair(r, "lite")
    (only,) = _viewers(html)
    assert 'data-diff-url=""' in only and 'data-graph-url=""' in only
    assert 'data-mode="lite"' in only
    assert 'data-v="source-db"' not in html
    assert "next-change" not in html


def test_diff_url_falls_back_for_a_view_model_without_the_key():
    # A view-model from before the graph sidecar existed: the diff summary is the evidence a
    # comparison is on file, and the client still needs to know where.
    r = {
        "pr_number": 9,
        "kind": "update",
        "pipeline": None,
        "preview": _preview(9, diff={"changed": True, "counts": {}, "sentence": "", "meta": None}),
    }
    html = _render_pair(r)
    for v in _viewers(html):
        assert 'data-diff-url="/previews/9/diff.json?rev=r1"' in v
        assert 'data-graph-url=""' in v


def test_viewer_markup_carries_no_inline_script_or_handlers():
    r = {
        "pr_number": 10,
        "kind": "update",
        "pipeline": {"render_svg_url": "/previews/10/pipeline.svg"},
        "preview": _preview(10, diff_url="/previews/10/diff.json"),
    }
    html = _render_pair(r)
    assert "<script" not in html
    assert not re.search(r"\son[a-z]+\s*=", html)

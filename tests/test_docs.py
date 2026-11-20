"""The /docs help page, the example pathways it links, and the onboarding tour's anchors."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.docs import REFUSING_RULES
from app.main import build_app
from app.quality import RULES, inspect_gpml
from app.review.checklist import CURATION_CHECKLIST
from tests.test_api import _authed_app, _login

_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ("example-pathway.gpml", "example-pathway-revision.gpml")

#: The anchors the tour is written against (the plan's K7 contract). The review-page ones owned by
#: the viewer and refresh work appear in templates only once that work lands, which is why the
#: tour, not the templates, is checked for them here.
SUBMIT_ANCHORS = (
    "submit-file", "preflight", "note", "submit", "tab-update", "nav-mine", "nav-help",
)
REVIEW_ANCHORS = (
    "viewer",
    "viewer-toolbar",
    "diff-summary",
    "refresh",
    "tracker",
    "quality",
    "checklist",
    "actions",
    "revise",
    "conversation",
)


def _app(tmp_path, **kw) -> TestClient:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'reg.db'}", **kw)
    return TestClient(build_app(settings))


@pytest.mark.parametrize("mode", ["direct", "pipeline"])
def test_docs_is_public_and_lists_every_rule_and_checklist_item(tmp_path, mode):
    kw = {"publish_mode": mode}
    if mode == "pipeline":
        kw["submit_identity"] = "fork"
    with _app(tmp_path, **kw) as c:
        r = c.get("/docs")  # no session: someone deciding whether to sign in can read it
    assert r.status_code == 200
    page = r.text
    for rule in RULES:
        assert f'id="rule-{rule.id}"' in page, rule.id
    for item in CURATION_CHECKLIST:
        assert f'id="check-{item.key}"' in page, item.key
    # The sections the table of contents promises all exist.
    for anchor in re.findall(r'<a href="#([a-z-]+)">', page.split('class="docs-toc"')[1]):
        assert f'id="{anchor}"' in page, anchor
    # Mode-specific prose: the pipeline's labels only where there is a pipeline.
    assert ("<code>accepted</code>" in page) == (mode == "pipeline")


def test_every_rule_and_checklist_item_explains_itself():
    # The help page is generated from these; an empty one would ship a blank row.
    for rule in RULES:
        assert len(rule.explain.split()) >= 8, rule.id
    for item in CURATION_CHECKLIST:
        assert len(item.help.split()) >= 8, item.key


def test_refusing_rules_are_the_ones_that_block():
    # REFUSING_RULES is declared in app.docs, not on the rules; hold it to what the rules return.
    probes = [
        b"<Pathway",  # not readable XML
        b"<html/>",  # readable, but no <Pathway>
        b"<Pathway/>",  # a pathway with no namespace, title or organism
    ]
    blocking = {
        f.id for probe in probes for f in inspect_gpml(probe).findings if f.severity == "block"
    }
    assert blocking == REFUSING_RULES


def test_swagger_and_openapi_stay_reachable(tmp_path):
    with _app(tmp_path) as c:
        assert c.get("/api/docs").status_code == 200
        assert c.get("/openapi.json").status_code == 200
        assert "/docs" not in c.get("/openapi.json").json()["paths"]


def test_robots_lets_crawlers_read_the_help(tmp_path):
    with _app(tmp_path) as c:
        assert "Allow: /docs" in c.get("/robots.txt").text


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_pathways_are_valid_drawable_and_anonymous(tmp_path, name):
    from app.preview.render import render_gpml
    from app.submit.gpml import validate_gpml

    data = (_ROOT / "static" / "examples" / name).read_bytes()
    validate_gpml(data)
    assert render_gpml(data).lstrip().startswith(b"<svg")
    # The only remark is the missing Author, left out so the file names nobody.
    loud = {f.id for f in inspect_gpml(data).findings if f.severity in ("warn", "fail", "block")}
    assert loud == {"gpml.author"}
    assert b"Author=" not in data
    with _app(tmp_path) as c:
        served = c.get(f"/static/examples/{name}")
        assert served.status_code == 200 and served.content == data


def test_submit_page_carries_the_tour_anchors_and_example_link(tmp_path):
    app, _ = _authed_app(tmp_path)
    with TestClient(app) as c:
        _login(c, "alice")
        page = c.get("/").text
    assert 'data-tour-page="submit"' in page
    assert "data-signed-in" in page
    for anchor in SUBMIT_ANCHORS:
        assert f'data-tour="{anchor}"' in page, anchor
    assert 'href="/static/examples/example-pathway.gpml" download' in page
    assert 'href="/docs"' in page and "data-tour-start" in page


def test_anonymous_pages_do_not_start_the_tour_by_themselves(tmp_path):
    with _app(tmp_path) as c:
        page = c.get("/").text
    assert "data-signed-in" not in page
    assert 'data-tour="nav-help"' in page


def test_review_page_carries_its_tour_anchors(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    data = (_ROOT / "static" / "examples" / EXAMPLES[0]).read_bytes()
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit", files={"file": ("p.gpml", io.BytesIO(data), "application/xml")}
        ).json()["pr_number"]
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        queue = c.get("/dashboard").text
    assert 'data-tour-page="review"' in page
    for anchor in ("quality", "checklist", "actions", "revise", "conversation"):
        assert f'data-tour="{anchor}"' in page, anchor
    assert 'data-tour-page="queue"' in queue and 'data-tour="queue-filter"' in queue


def test_the_tour_has_a_step_for_every_anchor():
    tour = (_ROOT / "static" / "tour.js").read_text(encoding="utf-8")
    steps = set(re.findall(r"anchor: '([a-z-]+)'", tour))
    assert set(SUBMIT_ANCHORS) | set(REVIEW_ANCHORS) | {"queue-filter"} <= steps
    # And every anchor a template sets is one the tour knows, so a renamed step cannot leave a
    # dead attribute behind (or the reverse).
    in_templates = set()
    for path in (_ROOT / "templates").rglob("*.html"):
        in_templates |= set(re.findall(r'data-tour="([a-z-]+)"', path.read_text(encoding="utf-8")))
    assert in_templates <= steps, in_templates - steps
    # The pages it is keyed by are the ones the templates name.
    for page in ("submit", "review", "queue"):
        assert re.search(rf"\b{page}: \[", tour), page

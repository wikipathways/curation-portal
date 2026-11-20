"""The /docs help page, the example pathways it links, and the onboarding tour's anchors."""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
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


def _flat(html: str) -> str:
    """The page with whitespace runs collapsed, so a phrase broken across lines still matches."""
    return re.sub(r"\s+", " ", html)


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
        docs = c.get("/docs").text
    assert "data-signed-in" not in page
    assert 'data-tour="nav-help"' in page
    # Nor offer it: signed out the submit form is not there, so the tour would be one step long.
    assert "data-tour-start" not in page and "data-tour-start" not in docs
    assert 'href="/docs"' in page


def test_the_example_link_warns_against_submitting_it(tmp_path):
    # Every submission of the example would be a real pull request on the content repository.
    app, _ = _authed_app(tmp_path)
    with TestClient(app) as c:
        _login(c, "alice")
        page = _flat(c.get("/").text)
        docs = _flat(c.get("/docs").text)
    assert "please do not submit it" in page
    assert "Please do not submit an example" in docs


def test_the_file_check_is_not_said_to_send_nothing(tmp_path):
    # The preflight uploads the file to the portal (POST /api/validate); only GitHub sees nothing.
    with _app(tmp_path) as c:
        docs = _flat(c.get("/docs").text)
    tour = (_ROOT / "static" / "tour.js").read_text(encoding="utf-8")
    for claim in ("sent anywhere", "without sending anything.", "Nothing is sent"):
        assert claim not in docs and claim not in tour, claim
    assert "before anything is sent to GitHub" in docs
    assert "without sending anything to GitHub" in docs


@pytest.mark.parametrize("identity", ["fork", "bot", "user"])
def test_docs_describe_the_configured_submit_identity(tmp_path, identity):
    with _app(tmp_path, publish_mode="pipeline", submit_identity=identity) as c:
        page = _flat(c.get("/docs").text)
    fork_prose = ("creates your own copy", "<dt>Fork</dt>", 'id="troubleshooting-stale-fork"')
    for text in fork_prose:
        assert (text in page) == (identity == "fork"), text
    # The pull request is the submitter's own except when the portal's account opens it.
    for text in ("the pull request in your name", "A pull request opens in your name"):
        assert (text in page) == (identity != "bot"), text
    assert ("as it does for any pull request you open" in page) == (identity != "bot")
    if identity == "bot":
        assert "through its own GitHub account, because most people cannot write" in page
    if identity == "user":
        assert "with your own GitHub account and opens the pull request from there" in page


def test_checklist_help_does_not_deny_what_the_code_fills_in(tmp_path):
    from app.docs import _checklist
    from app.pipeline.drafts import DraftArtifacts, reference_check

    for item in _checklist():
        if item["auto"]:
            assert "never ticked automatically" not in item["help"].lower(), item["key"]
    # In pipeline mode the repository's bibliography does tick references_valid; the help says so.
    two = [{"id": "a"}, {"id": "b"}]
    artifacts = DraftArtifacts("WP1__PR1", True, [], two, {}, None, None, None)
    assert reference_check(artifacts, gpml_reference_count=2)[0] == "pass"
    help_text = next(d.help for d in CURATION_CHECKLIST if d.key == "references_valid")
    assert "resolved every reference" in help_text


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


def test_a_step_whose_anchor_starts_hidden_has_a_visible_stand_in():
    # tour.js skips a step whose element is hidden when the tour starts. The submit page's
    # findings box is hidden until a file is chosen, i.e. always on a first visit, so its step
    # must name a `near` anchor that is rendered visible, or the tour silently loses it.
    tour = (_ROOT / "static" / "tour.js").read_text(encoding="utf-8")
    near = dict(re.findall(r"anchor: '([a-z-]+)', near: '([a-z-]+)'", tour))
    hidden, shown = set(), set()
    for path in (_ROOT / "templates").rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<[^>]*data-tour=\"[a-z-]+\"[^>]*>", html):
            name = re.search(r'data-tour="([a-z-]+)"', tag).group(1)
            (hidden if re.search(r"\shidden(?=[\s>=])", tag) else shown).add(name)
    assert "preflight" in hidden
    for name in hidden:
        assert name in near, f"{name} starts hidden and has no near: stand-in"
        assert near[name] in shown, near[name]


def _tour(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [
            node,
            str(_ROOT / "tests" / "tour_harness.js"),
            str(_ROOT / "static" / "tour.js"),
            scenario,
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def test_tour_walks_every_submit_step_including_the_hidden_findings_box():
    r = _tour("walk")
    assert r["titles"][:3] == [
        "Choose your pathway", "What the checks found", "A note for the curators",
    ]
    assert r["titles"][-1] == "Help" and r["stored"] == "done"


def test_tour_back_skips_a_vanished_step_backwards():
    # From Submit (4 of 7), with the note gone, Back lands on the step before the note.
    assert _tour("back-lands-earlier")["afterBack"] == {
        "count": "2 of 6", "title": "What the checks found",
    }


def test_tour_back_stays_put_when_nothing_earlier_is_left():
    assert _tour("back-stays-when-nothing-earlier")["afterBack"] == {
        "count": "1 of 4", "title": "Submit",
    }


def test_tour_follows_on_when_the_step_on_screen_vanishes():
    assert _tour("middle-vanishes")["afterVanish"] == {"count": "3 of 6", "title": "Submit"}
    # The last step vanishing goes back one, and does not record the tour as finished.
    r = _tour("last-vanishes")
    assert r["afterVanish"] == {"count": "6 of 6", "title": "Your submissions"}
    assert r["stored"] is None
    # Everything vanishing ends it, but not as "done".
    r = _tour("all-vanish")
    assert r["afterVanish"] is None and r["stored"] == "interrupted"

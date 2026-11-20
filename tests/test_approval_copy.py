"""Approving an update does not say the repository assigns its WPID.

Sandbox WP5430 (2026-11-20): approving an update toasted "Approved. The repository assigns the
WPID and publishes it from here." An update has had its WPID all along. The same sentence was in
the card's banner, the approval comment on the pull request and the tracker's Published stage;
each now names the WPID for an update and keeps the old wording for a new pathway.
"""
from __future__ import annotations

import io
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.github import FakeGitHubClient
from app.review.service import approval_comment
from app.review.status import presentation
from app.review.tracker import publish_stages
from tests.test_api import _login
from tests.test_pipeline_api import REPO, _pass_every_required_item, _pipeline_app, _submit

PATH = "pathways/WP5636/WP5636.gpml"
REV_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r19990101000000"></Pathway>'
)


def test_the_banner_names_the_wpid_of_an_approved_update():
    update = presentation("approved", kind="update", wpid=5430).blurb
    assert "WP5430" in update and "assigns" not in update
    assert presentation("published", kind="update", wpid=5430).blurb == (
        "Published to WP5430 by the repository."
    )
    new = presentation("approved", kind="new", wpid=None).blurb
    assert "assigns the WPID" in new
    assert presentation("published", kind="new").blurb == (
        "Published by the repository, with the WPID it assigned."
    )
    # Without a kind (the docs' table of states) the sentence covers both.
    generic = presentation("approved").blurb
    assert "if it is new" in generic
    # Other states are the same for both kinds.
    assert presentation("open", kind="update", wpid=1) == presentation("open")


def test_the_approval_comment_names_the_wpid_of_an_update():
    update = approval_comment("curator", kind="update", wpid=5430)
    assert "publishes the edit to WP5430" in update
    assert "assigns the WPID" not in update
    new = approval_comment("curator", kind="new", wpid=None)
    assert "assigns the WPID" in new and "approved this pathway" in new


def _tracker_review(**kw):
    base = {
        "status": "approved",
        "approved_at": datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
        "approved_by": "curator",
        "published_at": None,
        "wpid": None,
        "kind": "new",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_tracker_does_not_wait_for_an_updates_wpid():
    now = datetime(2026, 11, 20, 12, 2, tzinfo=UTC)
    update = {s["key"]: s for s in publish_stages(
        _tracker_review(kind="update", wpid=5430), {}, now)}
    assert update["published"]["detail"] == "It goes to WP5430 when it publishes."
    failed = {s["key"]: s for s in publish_stages(
        _tracker_review(kind="update", wpid=5430, status="publish_failed"), {}, now)}
    assert "WPID" not in failed["published"]["detail"]
    new = {s["key"]: s for s in publish_stages(_tracker_review(), {}, now)}
    assert new["published"]["detail"] == "The WPID is assigned when it publishes."


def test_approving_an_update_end_to_end(tmp_path):
    fake = FakeGitHubClient(
        default_branches={f"{REPO}#main": "basesha"},
        existing_files={f"{REPO}#{PATH}": "blob-v1"},
        existing_contents={f"{REPO}#{PATH}": REV_GPML.decode()},
    )
    app = _pipeline_app(tmp_path, curators=("marvinm2",), fake=fake)
    with TestClient(app) as c:
        app.state.drafts = None
        r = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        )
        assert r.status_code == 201, r.text
        pr = r.json()["pr_number"]
        _login(c, "marvinm2")
        _pass_every_required_item(c, pr)
        approved = c.post(f"/api/reviews/{pr}/approve")
        assert approved.status_code == 200, approved.text
        # What the toast reads (static/app.js).
        assert approved.json()["kind"] == "update" and approved.json()["wpid"] == 5636
        comments = fake.issue_comments[(REPO, pr)]
        assert any("publishes the edit to WP5636" in b for b in comments)
        assert not any("assigns the WPID" in b for b in comments)
        page = c.get(f"/dashboard/{pr}").text
        assert "It publishes the edit to WP5636" in page
        assert "It goes to WP5636 when it publishes." in page
        assert "assigns the WPID" not in page


def test_approving_a_new_pathway_keeps_the_wpid_wording(tmp_path):
    app = _pipeline_app(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = None
        pr = _submit(c)["pr_number"]
        _login(c, "marvinm2")
        _pass_every_required_item(c, pr)
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 200
        comments = app.state._fake.issue_comments[(REPO, pr)]
        assert any("it assigns the WPID" in b for b in comments)
        assert "It assigns the WPID, publishes the pathway" in c.get(f"/dashboard/{pr}").text

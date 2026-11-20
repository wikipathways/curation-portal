"""The update form's WPID lookup says when an edit of the pathway is already under review.

Sandbox PR #93 (2026-11-20): the lookup said "Found WP5430 … Uploading opens an update" while the
submitter's own update, sent back for changes, was open on it. The upload then went onto that
pull request, correctly, so the lookup was the only thing wrong. Each test here checks that the
lookup's ``open_review.outcome`` matches what the update route then actually does.
"""
from __future__ import annotations

import io
from datetime import timedelta

from fastapi.testclient import TestClient

from app.github import FakeGitHubClient
from app.models import PathwayLock, Review, ReviewStatus, utcnow
from tests.test_api import GOOD_GPML, REV_GPML, _authed_app, _settings


def _fake():
    settings = _settings()
    repo, branch = settings.content_repo, settings.default_branch
    return FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
        existing_contents={f"{repo}#pathways/WP5636/WP5636.gpml": GOOD_GPML.decode()},
    )


def _upload(c):
    return c.post(
        "/api/pathways/5636/update",
        files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
    )


def _app(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    return app, current, fake


def test_no_review_means_no_open_review(tmp_path):
    app, _current, _fake_ = _app(tmp_path)
    with TestClient(app) as c:
        info = c.get("/api/pathways/5636").json()
    assert info["state"] == "on_main"
    assert info["open_review"] is None


def test_the_submitters_own_open_update_takes_the_upload(tmp_path):
    app, current, fake = _app(tmp_path)
    with TestClient(app) as c:
        app.state.bot_client_provider = lambda: fake  # the scan runs, as on a deployment
        pr = _upload(c).json()["pr_number"]
        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "attach NQO1"})

        current["user"] = "alice"
        o = c.get("/api/pathways/5636").json()["open_review"]
        assert o == {
            "pr_number": pr,
            "kind": "update",
            "status": "changes_requested",
            "status_label": "Changes requested",
            "yours": True,
            "submitter": "alice",
            "held_by": None,
            "outcome": "revise",
        }
        # And that is what the route does: the same pull request, no second one.
        again = _upload(c)
        assert again.status_code == 201, again.text
        assert again.json()["pr_number"] == pr


def test_someone_elses_open_update_is_refused_and_named(tmp_path):
    app, current, fake = _app(tmp_path)
    with TestClient(app) as c:
        app.state.bot_client_provider = lambda: fake
        pr = _upload(c).json()["pr_number"]

        current["user"] = "bob"
        o = c.get("/api/pathways/5636").json()["open_review"]
        assert o["outcome"] == "locked"
        assert o["yours"] is False
        assert o["held_by"] == "alice" and o["submitter"] == "alice"
        assert o["pr_number"] == pr
        refused = _upload(c)
        assert refused.status_code == 409
        assert refused.json()["detail"]["held_by"] == "alice"


def test_someone_elses_update_is_still_refused_once_the_lock_expired(tmp_path):
    """The lock is not the only guard: the open-pull-request scan finds theirs."""
    app, current, fake = _app(tmp_path)
    with TestClient(app) as c:
        app.state.bot_client_provider = lambda: fake
        pr = _upload(c).json()["pr_number"]
        with app.state.session_factory() as s:
            s.get(PathwayLock, 5636).expires_at = utcnow() - timedelta(minutes=1)
            s.commit()

        current["user"] = "bob"
        o = c.get("/api/pathways/5636").json()["open_review"]
        assert o["outcome"] == "locked"
        assert o["submitter"] == "alice" and o["held_by"] is None
        assert o["pr_number"] == pr
        # The lookup did not delete the expired lock; reading is not the place for that.
        with app.state.session_factory() as s:
            assert s.get(PathwayLock, 5636) is not None
        assert _upload(c).status_code == 409

        # Its own submitter is still let through onto the same pull request.
        current["user"] = "alice"
        assert c.get("/api/pathways/5636").json()["open_review"]["outcome"] == "revise"
        mine = _upload(c)
        assert mine.status_code == 201, mine.text
        assert mine.json()["pr_number"] == pr


def test_an_approved_update_refuses_everyone_until_it_finishes(tmp_path):
    app, current, fake = _app(tmp_path)
    with TestClient(app) as c:
        app.state.bot_client_provider = lambda: fake
        pr = _upload(c).json()["pr_number"]
        with app.state.session_factory() as s:
            s.get(Review, pr).status = ReviewStatus.APPROVED
            s.commit()

        for user, yours in (("alice", True), ("bob", False)):
            current["user"] = user
            o = c.get("/api/pathways/5636").json()["open_review"]
            assert o["outcome"] == "wait" and o["yours"] is yours
            assert o["status"] == "approved" and o["status_label"] == "Approved"
            refused = _upload(c)
            assert refused.status_code == 409
            assert "wait for it to finish" in refused.json()["detail"]


def test_a_finished_review_is_not_an_open_one(tmp_path):
    app, current, fake = _app(tmp_path)
    with TestClient(app) as c:
        app.state.bot_client_provider = lambda: fake
        pr = _upload(c).json()["pr_number"]
        with app.state.session_factory() as s:
            s.get(Review, pr).status = ReviewStatus.PUBLISHED
            s.delete(s.get(PathwayLock, 5636))
            s.commit()
        assert c.get("/api/pathways/5636").json()["open_review"] is None


def test_the_lookup_needs_a_session(tmp_path):
    """Who is editing a pathway, and whether it is yours, is for signed-in users only."""
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    from app.main import build_app

    with TestClient(build_app(settings)) as c:
        assert c.get("/api/pathways/5636").status_code == 401

"""A decision is about one commit of one pull request on one repository.

Two defects found in the 2026-11 review, each reproduced before it was fixed:

- approval was not bound to the revision the curator reviewed, so a push to the branch between
  review and approval was published unreviewed (in fork mode the branch is the submitter's own);
- the webhook ignored the delivery's repository, so a pull request with the same number on any
  other repository the App is installed on could close or reject a review here.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.curators import ConfigCurators
from app.github import FakeGitHubClient, GitHubError
from app.main import (
    build_app,
    get_bot_client,
    get_bot_optional,
    get_current_user,
    get_github_client,
)
from app.models import ReviewStatus
from app.review.checklist import ChecklistState
from app.review.service import CurationService, ReviewStale

REPO = "wikipathways/sandbox-wp-db"
CURATOR = "marvinm2"


def _fake() -> FakeGitHubClient:
    return FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})


def _service(session_factory, gh, *, mode="pipeline") -> CurationService:
    return CurationService(
        session_factory,
        gh,
        repo=REPO,
        curators=ConfigCurators([CURATOR]),
        publish_mode=mode,
    )


def _submit(svc, gh, *, branch="WP0001_alice_20261111-120000") -> tuple[int, str]:
    """Open a pull request the way the portal does: branch, commit, PR, register with the sha."""
    gh.create_branch(REPO, branch, "basesha")
    head = gh.put_file(REPO, branch, "pathways/WP0001/WP0001.gpml", "<Pathway/>", "Add")
    pr = gh.open_pull_request(REPO, head=branch, base="main", title="t", body="b")
    svc.register(
        pr_number=pr.number,
        wpid=None,
        submitter="alice",
        kind="new",
        head_branch=branch,
        head_sha=head,
        base_repo=REPO,
    )
    for item in svc.get(pr.number).checklist:
        if item.get("required"):
            svc.set_checklist_item(pr.number, item["key"], ChecklistState.PASS.value)
    return pr.number, branch


# -- approval is bound to the reviewed head ---------------------------------------------------


def test_the_portal_records_the_commit_it_made(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr, _ = _submit(svc, gh)
    review = svc.get(pr)
    assert review.head_sha and review.head_sha == gh.get_pull_request(REPO, pr).head_sha
    assert review.base_repo == REPO


def test_a_push_after_review_is_not_approved(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr, branch = _submit(svc, gh)
    # Somebody pushes to the branch after the curator looked.
    gh.put_file(REPO, branch, "pathways/WP0001/WP0001.gpml", "<Pathway evil=''/>", "x", sha="s")

    with pytest.raises(ReviewStale):
        svc.approve(pr, CURATOR)

    assert "accepted" not in gh.list_labels(REPO, pr)
    review = svc.get(pr)
    assert review.status == ReviewStatus.OPEN
    # The row now describes the new head, so a curator who has checked it can approve.
    assert review.head_sha == gh.get_pull_request(REPO, pr).head_sha
    assert svc.approve(pr, CURATOR).status == ReviewStatus.APPROVED


def test_direct_mode_merges_only_the_reviewed_commit(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, mode="direct")
    pr, branch = _submit(svc, gh)
    gh.put_file(REPO, branch, "pathways/WP0001/WP0001.gpml", "<Pathway x=''/>", "x", sha="s")

    with pytest.raises(ReviewStale):
        svc.approve(pr, CURATOR)
    assert pr not in gh.merged


def test_the_merge_itself_carries_the_sha_guard(session_factory):
    """The window between the head check and the merge is closed by GitHub, not by luck."""
    gh = _fake()
    pr = gh.open_pull_request(REPO, head="b", base="main", title="t", body="b")
    gh.pull_meta[pr.number]["head_sha"] = "moved"
    with pytest.raises(GitHubError):
        gh.merge_pull_request(REPO, pr.number, sha="reviewed")
    assert pr.number not in gh.merged


def test_a_new_head_withdraws_an_approval(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr, _ = _submit(svc, gh)
    svc.approve(pr, CURATOR)
    assert "accepted" in gh.list_labels(REPO, pr)

    review = svc.handle_head_moved(pr, "a-commit-nobody-reviewed")

    assert review.status == ReviewStatus.OPEN
    assert review.approved_at is None
    assert "accepted" not in gh.list_labels(REPO, pr)
    assert "changed after it was approved" in (review.decision_note or "")


def test_the_portals_own_commit_is_not_a_moved_head(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr, _ = _submit(svc, gh)
    svc.approve(pr, CURATOR)
    review = svc.handle_head_moved(pr, svc.get(pr).head_sha)
    assert review.status == ReviewStatus.APPROVED


def test_a_legacy_row_without_a_recorded_head_still_approves(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = gh.open_pull_request(REPO, head="WP0001_bob_1", base="main", title="t", body="b")
    svc.register(pr_number=pr.number, wpid=None, submitter="bob", kind="new")
    for item in svc.get(pr.number).checklist:
        if item.get("required"):
            svc.set_checklist_item(pr.number, item["key"], ChecklistState.PASS.value)
    assert svc.approve(pr.number, CURATOR).status == ReviewStatus.APPROVED


# -- the webhook is scoped to the content repository ------------------------------------------


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    return body, {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig}


@pytest.fixture
def webhook_app(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        content_repo=REPO,
        publish_mode="pipeline",
        github_webhook_secret="whsec",
        curators=[CURATOR],
        preview_cache_dir=str(tmp_path / "cache"),
    )
    gh = _fake()
    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: gh
    app.dependency_overrides[get_bot_optional] = lambda: gh
    app.dependency_overrides[get_bot_client] = lambda: gh
    app.dependency_overrides[get_current_user] = lambda: CURATOR
    return app, gh


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "closed", "pull_request": {"merged": False}},
        {"action": "labeled", "label": {"name": "rejected"}, "pull_request": {}},
    ],
    ids=["closed", "labeled-rejected"],
)
def test_a_delivery_from_another_repository_is_ignored(webhook_app, payload):
    app, gh = webhook_app
    with TestClient(app) as c:
        r = c.post(
            "/api/submit",
            files={"file": ("p.gpml", _GPML, "application/xml")},
        )
        assert r.status_code == 201, r.text
        pr = r.json()["pr_number"]

        delivery = dict(payload, number=pr, repository={"full_name": "someone-else/unrelated"})
        body, headers = _signed(delivery)
        resp = c.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ignored"] == "repository:someone-else/unrelated"
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "open"


def test_a_synchronize_on_an_approved_portal_review_withdraws_it(webhook_app):
    app, gh = webhook_app
    with TestClient(app) as c:
        r = c.post("/api/submit", files={"file": ("p.gpml", _GPML, "application/xml")})
        assert r.status_code == 201, r.text
        pr = r.json()["pr_number"]
        detail = c.get(f"/api/reviews/{pr}").json()
        for item in detail["checklist"]:
            if item.get("required"):
                c.post(
                    f"/api/reviews/{pr}/checklist",
                    data={"key": item["key"], "state": "pass"},
                )
        ap = c.post(f"/api/reviews/{pr}/approve")
        assert ap.status_code == 200, ap.text

        body, headers = _signed(
            {
                "action": "synchronize",
                "number": pr,
                "pull_request": {"number": pr, "head": {"sha": "pushed-after-approval"}},
                "repository": {"full_name": REPO},
            }
        )
        resp = c.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 200
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "open"
        assert "accepted" not in gh.list_labels(REPO, pr)


_GPML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Binding test pathway" '
    b'Organism="Homo sapiens" Version="20261111">\n'
    b'  <Graphics BoardWidth="200" BoardHeight="200"/>\n'
    b"</Pathway>\n"
)


# -- the three gaps the 2026-11-11 security review left (closed in Wave 2) ---------------------


def test_approval_is_refused_when_the_page_showed_an_older_commit(session_factory):
    """A push to an unapproved review moves its head silently. The curator's page still shows
    the old commit, and approving from it must not approve the new one unseen."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr, branch = _submit(svc, gh)
    seen = svc.get(pr).head_sha
    new = gh.put_file(
        REPO, branch, "pathways/WP0001/WP0001.gpml", "<Pathway x='1'/>", "Push", sha="s"
    )
    svc.handle_head_moved(pr, new)  # the synchronize webhook, before anyone approved

    with pytest.raises(ReviewStale):
        svc.approve(pr, CURATOR, seen_head=seen)
    assert svc.approve(pr, CURATOR, seen_head=new).status == ReviewStatus.APPROVED


def test_a_label_that_could_not_be_removed_is_removed_by_the_next_reconcile(session_factory):
    from datetime import timedelta

    gh = _fake()
    svc = _service(session_factory, gh)
    pr, branch = _submit(svc, gh)
    svc.approve(pr, CURATOR)
    assert "accepted" in gh.list_labels(REPO, pr)

    # The bot is unavailable when the push arrives: the approval is withdrawn here, and the
    # label stays on GitHub where the repository could still publish it.
    offline = CurationService(
        session_factory, None, repo=REPO, curators=ConfigCurators([CURATOR]),
        publish_mode="pipeline",
    )
    new = gh.put_file(
        REPO, branch, "pathways/WP0001/WP0001.gpml", "<Pathway x='1'/>", "Push", sha="s"
    )
    offline.handle_head_moved(pr, new)
    assert svc.get(pr).status == ReviewStatus.OPEN
    assert "accepted" in gh.list_labels(REPO, pr)

    later = CurationService(
        session_factory, gh, repo=REPO, curators=ConfigCurators([CURATOR]),
        publish_mode="pipeline", reconcile_min_interval=timedelta(seconds=0),
    )
    later.reconcile()
    assert "accepted" not in gh.list_labels(REPO, pr)
    assert not (svc.get(pr).pipeline_run or {}).get("unlabel")


def test_an_adopted_review_whose_refresh_fails_loses_its_approval(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        content_repo=REPO,
        publish_mode="pipeline",
        curators=[CURATOR],
        github_webhook_secret="whsec",
        preview_cache_dir=str(tmp_path / "cache"),
    )
    app = build_app(settings)
    gh = _fake()
    for dep in (get_github_client, get_bot_optional, get_bot_client):
        app.dependency_overrides[dep] = lambda: gh
    app.dependency_overrides[get_current_user] = lambda: CURATOR
    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Glycolysis" '
        'Organism="Homo sapiens" Author="egonw"><Graphics BoardWidth="9" BoardHeight="9"/>'
        "</Pathway>"
    )
    gh.seed_foreign_pr(
        REPO, 85, author="egonw", title="Plugin", head_branch="plugin", head_sha="old",
        files=[("pathways/WP554/WP554.gpml", "modified", gpml)],
    )
    with TestClient(app) as c:
        assert c.post("/api/reviews/85/adopt").json()["adopted"] is True
        svc = app.state.worker._make_curation(gh)
        for item in svc.get(85).checklist:
            if item.get("required"):
                svc.set_checklist_item(85, item["key"], ChecklistState.PASS.value)
        svc.approve(85, CURATOR)

        # A new commit whose file cannot be read: the refresh is skipped.
        gh.pull_meta[85]["head_sha"] = "new"
        payload = {
            "action": "synchronize",
            "number": 85,
            "pull_request": {"number": 85, "head": {"sha": "new"}},
            "repository": {"full_name": REPO},
        }
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        review = svc.get(85)
        assert review.status == ReviewStatus.OPEN
        assert review.head_sha == "new"
        assert "accepted" not in gh.list_labels(REPO, 85)

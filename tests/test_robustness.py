"""Smaller defects from the 2026-11 review, each one a way the portal could end up stuck or wrong.

- a rejection GitHub refused was still recorded, and REJECTED is terminal;
- a failure to mint the App's installation token turned every page that uses the bot into a 500;
- a TLS deployment with a missing Docker secret started on the public development defaults;
- a submitter whose lock expired was refused by their own fork-mode pull request.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.curators import ConfigCurators
from app.github import FakeGitHubClient
from app.locks import LockUnavailable, PathwayLockRegistry
from app.main import build_app, get_current_user
from app.models import ReviewStatus
from app.review.checklist import ChecklistState
from app.review.service import CurationService

REPO = "wikipathways/sandbox-wp-db"
CURATOR = "marvinm2"


def _service(session_factory, gh, mode="pipeline") -> CurationService:
    return CurationService(
        session_factory, gh, repo=REPO, curators=ConfigCurators([CURATOR]), publish_mode=mode
    )


def _register(svc, gh) -> int:
    pr = gh.open_pull_request(REPO, head="WP0001_a_1", base="main", title="t", body="b")
    svc.register(pr_number=pr.number, wpid=None, submitter="alice", kind="new")
    return pr.number


# -- a refused rejection does not stand -------------------------------------------------------


@pytest.mark.parametrize(
    "mode,step", [("pipeline", "add_labels"), ("direct", "close_pull_request")]
)
def test_a_rejection_github_refused_is_undone(session_factory, mode, step):
    gh = FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"}, fail_on={step})
    svc = _service(session_factory, gh, mode)
    pr = _register(svc, gh)

    with pytest.raises(Exception):  # noqa: B017 — GitHubError, whatever the fake raises
        svc.reject(pr, CURATOR, "not a pathway")

    review = svc.get(pr)
    assert review.status == ReviewStatus.OPEN
    assert review.decided_by is None
    assert review.decision_note is None


def test_an_approved_review_whose_label_came_off_returns_to_open(session_factory):
    gh = FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    for item in svc.get(pr).checklist:
        if item.get("required"):
            svc.set_checklist_item(pr, item["key"], ChecklistState.PASS.value)
    svc.approve(pr, CURATOR)
    gh.fail_on = {"add_labels"}

    with pytest.raises(Exception):  # noqa: B017
        svc.reject(pr, CURATOR)

    # `accepted` came off before `rejected` was refused, so the approval is gone on GitHub.
    assert "accepted" not in gh.list_labels(REPO, pr)
    assert svc.get(pr).status == ReviewStatus.OPEN


# -- the App token ----------------------------------------------------------------------------


class _BrokenApp:
    def installation_token(self) -> str:
        raise RuntimeError("GitHub is down")


def test_a_failing_app_token_does_not_take_the_dashboard_down(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        preview_cache_dir=str(tmp_path / "cache"),
    )
    app = build_app(settings)
    app.dependency_overrides[get_current_user] = lambda: CURATOR
    with TestClient(app) as c:
        app.state.bot_app = _BrokenApp()
        assert c.get("/dashboard").status_code == 200
        approve = c.post("/api/reviews/1/approve")
        assert approve.status_code == 503
        assert "authenticate" in approve.json()["detail"]


# -- secrets behind TLS -----------------------------------------------------------------------


def test_tls_refuses_the_development_session_secret():
    with pytest.raises(ValidationError, match="session_secret"):
        Settings(
            _env_file=None,
            session_https_only=True,
            database_url="postgresql://u@h/db",
        )


def test_tls_refuses_a_sqlite_database():
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, session_https_only=True, session_secret="s3cret")


def test_development_keeps_its_defaults():
    Settings(_env_file=None)


# -- a fork-mode submitter is not locked out by their own pull request --------------------------


def test_the_scan_skips_the_callers_own_pull_request(session_factory):
    reg = PathwayLockRegistry(session_factory, open_pr_scanner=lambda wpid: 42)
    with pytest.raises(LockUnavailable):
        reg.acquire(554, "alice")
    assert reg.acquire(554, "alice", own_pr=42).held_by == "alice"


def test_another_pull_request_still_blocks(session_factory):
    reg = PathwayLockRegistry(session_factory, open_pr_scanner=lambda wpid: 43)
    with pytest.raises(LockUnavailable):
        reg.acquire(554, "alice", own_pr=42)

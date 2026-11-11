from __future__ import annotations

import pytest

from app.github import FakeGitHubClient, GitHubError
from app.locks import LockUnavailable, PathwayLockRegistry
from app.submit import InvalidGpml
from app.update import PathwayNotFound, UpdateService

REPO = "wikipathways/wikipathways-database"
WPID = 5636
PATH = "pathways/WP5636/WP5636.gpml"

REVISION = (
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Organism="Homo sapiens" Version="WP5636_r19990101000000"></Pathway>'
)
BAD_GPML = "<html>nope</html>"


@pytest.fixture
def locks(session_factory):
    return PathwayLockRegistry(session_factory)


def _fake_github(*, exists: bool = True, **kw) -> FakeGitHubClient:
    existing = {f"{REPO}#{PATH}": "oldblobsha"} if exists else {}
    return FakeGitHubClient(
        default_branches={f"{REPO}#main": "basesha"},
        existing_files=existing,
        **kw,
    )


def test_update_happy_path(locks, session_factory):
    gh = _fake_github()
    svc = UpdateService(locks, gh, repo=REPO)

    result = svc.update_pathway(wpid=WPID, gpml=REVISION, submitter="alice")

    assert result.wpid_str == "WP5636"
    assert result.branch == "update/WP5636"
    assert result.path == PATH
    assert result.pr_number == 1

    # Branch cut off the base; file updated (with the old blob sha) and WPID re-stamped fresh.
    assert gh.branch_bases[(REPO, "update/WP5636")] == "basesha"
    content, message, _sha = gh.files[(REPO, "update/WP5636", PATH)]
    assert message == "Update WP5636"
    assert 'Version="WP5636_r' in content
    assert "r19990101000000" not in content  # revision bumped

    # PR opened with the title/body a reviewer sees.
    meta = gh.pull_meta[1]
    assert meta["title"] == "Update WP5636"
    assert meta["base"] == "main"
    assert meta["head"] == "update/WP5636"
    assert "@alice edited **WP5636**" in meta["body"]
    assert "curation portal" in meta["body"]
    assert "What changed" not in meta["body"]  # no note supplied

    # Lock is held for the duration of the PR, with the PR number attached.
    lock = locks.get(WPID)
    assert lock is not None
    assert lock.held_by == "alice"
    assert lock.pr_number == 1


def test_update_description_flows_into_pr_body(locks):
    gh = _fake_github()
    svc = UpdateService(locks, gh, repo=REPO)

    svc.update_pathway(
        wpid=WPID, gpml=REVISION, submitter="alice",
        description="Fixed the arrow direction between AKT1 and MTOR.",
    )

    body = gh.pull_meta[1]["body"]
    assert "**What changed**" in body
    assert "Fixed the arrow direction" in body


def test_update_refused_when_locked_by_other(locks):
    locks.acquire(WPID, "bob")  # someone else checked it out first
    svc = UpdateService(locks, _fake_github(), repo=REPO)
    with pytest.raises(LockUnavailable) as ei:
        svc.update_pathway(wpid=WPID, gpml=REVISION, submitter="alice")
    assert ei.value.held_by == "bob"


def test_update_nonexistent_pathway_raises_and_releases_lock(locks):
    gh = _fake_github(exists=False)
    svc = UpdateService(locks, gh, repo=REPO)
    with pytest.raises(PathwayNotFound):
        svc.update_pathway(wpid=WPID, gpml=REVISION, submitter="alice")
    # Lock released so the (non-existent) pathway isn't left checked out; no PR opened.
    assert not locks.is_locked(WPID)
    assert gh.pulls == []


def test_update_github_failure_releases_lock(locks):
    gh = _fake_github(fail_on={"open_pull_request"})
    svc = UpdateService(locks, gh, repo=REPO)
    with pytest.raises(GitHubError):
        svc.update_pathway(wpid=WPID, gpml=REVISION, submitter="alice")
    assert not locks.is_locked(WPID)


def test_update_invalid_gpml_never_locks(locks):
    gh = _fake_github()
    svc = UpdateService(locks, gh, repo=REPO)
    with pytest.raises(InvalidGpml):
        svc.update_pathway(wpid=WPID, gpml=BAD_GPML, submitter="alice")
    assert not locks.is_locked(WPID)


def test_same_user_reupload_reuses_pr(locks):
    # Re-uploading while still checked out reuses the same branch + open PR (no second PR),
    # and the lock stays held by the same user.
    gh = _fake_github()
    svc = UpdateService(locks, gh, repo=REPO)
    first = svc.update_pathway(wpid=WPID, gpml=REVISION, submitter="alice")
    second = svc.update_pathway(wpid=WPID, gpml=REVISION, submitter="alice")
    assert first.pr_number == 1
    assert second.pr_number == 1  # same PR updated, not a new one
    assert len(gh.pulls) == 1
    assert locks.get(WPID).pr_number == 1

from __future__ import annotations

import pytest

from app.github import BranchAlreadyExists, FakeGitHubClient
from app.models import WpidReservation
from app.submit import InvalidGpml, NoPendingSubmission, SubmissionService
from app.wpid import WpidAllocator

REPO = "wikipathways/wikipathways-database"

GOOD_GPML = (
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Organism="Homo sapiens" Version="WP1_r00000000000000"></Pathway>'
)
BAD_GPML = "<html>not a pathway</html>"


@pytest.fixture
def allocator(session_factory):
    return WpidAllocator(session_factory, floor_provider=lambda: 5636)


def _fake_github(**kw) -> FakeGitHubClient:
    return FakeGitHubClient(default_branches={f"{REPO}#main": "basesha123"}, **kw)


def _reservations(session_factory) -> list[WpidReservation]:
    with session_factory() as s:
        return list(s.query(WpidReservation).all())


def test_submit_new_pathway_happy_path(allocator, session_factory):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    result = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert result.wpid == 5637
    assert result.wpid_str == "WP5637"
    assert result.branch == "submit/WP5637"
    assert result.path == "pathways/WP5637/WP5637.gpml"
    assert result.pr_number == 1
    assert result.pr_url.endswith("/pull/1")

    # Branch created off the base SHA.
    assert gh.branch_bases[(REPO, "submit/WP5637")] == "basesha123"
    # File committed at the canonical path with the WPID stamped into Version.
    content, message, _sha = gh.files[(REPO, "submit/WP5637", "pathways/WP5637/WP5637.gpml")]
    assert 'Version="WP5637_r' in content
    assert "WP1_r00000000000000" not in content  # placeholder overwritten
    assert message == "Add WP5637: Mitophagy"
    # PR opened, with the human-facing title/body a reviewer sees.
    assert gh.pulls[0].head_branch == "submit/WP5637"
    meta = gh.pull_meta[1]
    assert meta["title"] == "New pathway WP5637: Mitophagy"
    assert meta["base"] == "main"
    assert meta["head"] == "submit/WP5637"
    assert "**WPID:** WP5637, assigned by the app" in meta["body"]
    assert "**Organism:** Homo sapiens" in meta["body"]
    assert "@alice submitted this pathway" in meta["body"]
    # No note was supplied, so the body carries no submitter-note section.
    assert "Note from the submitter" not in meta["body"]

    # Reservation persisted with the PR number attached.
    rows = _reservations(session_factory)
    assert len(rows) == 1
    assert rows[0].wpid == 5637
    assert rows[0].pr_number == 1


REVISED_GPML = (
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy v2" '
    'Organism="Homo sapiens"></Pathway>'
)


def test_revise_commits_onto_the_open_submission_pr(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)
    first = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")  # WP5637, PR #1

    revised = svc.revise_new_pathway(wpid=first.wpid, gpml=REVISED_GPML, submitter="alice")

    assert revised.pr_number == first.pr_number  # same PR, no new one
    assert revised.branch == "submit/WP5637"
    assert len(gh.pulls) == 1
    content, message, _sha = gh.files[(REPO, "submit/WP5637", "pathways/WP5637/WP5637.gpml")]
    assert message == "Revise WP5637"
    assert "Mitophagy v2" in content
    assert 'Version="WP5637_r' in content  # WPID re-stamped, not renumbered


def test_revise_without_open_submission_raises(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)
    with pytest.raises(NoPendingSubmission):
        svc.revise_new_pathway(wpid=5637, gpml=GOOD_GPML, submitter="alice")


def test_submit_description_flows_into_pr_body(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    svc.submit_new_pathway(
        gpml=GOOD_GPML,
        submitter="alice",
        description="Curated from Reactome R-HSA-1234; two data nodes need HGNC ids.",
    )

    body = gh.pull_meta[1]["body"]
    assert "**Note from the submitter**" in body
    assert "Curated from Reactome R-HSA-1234" in body


def test_submit_blank_description_adds_no_note(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice", description="   ")

    assert "Note from the submitter" not in gh.pull_meta[1]["body"]


def test_submit_invalid_gpml_reserves_nothing(allocator, session_factory):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)

    with pytest.raises(InvalidGpml):
        svc.submit_new_pathway(gpml=BAD_GPML, submitter="alice")

    # Validation precedes allocation, so no WPID was consumed and no GitHub calls happened.
    assert _reservations(session_factory) == []
    assert gh.pulls == []
    assert not gh.files


def test_github_failure_rolls_back_wpid(allocator, session_factory):
    gh = _fake_github(fail_on={"open_pull_request"})
    svc = SubmissionService(allocator, gh, repo=REPO)

    with pytest.raises(Exception):  # noqa: B017 - GitHubError surfaces
        svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    # WPID returned to the pool: no reservation left, and the next submit reuses 5637.
    assert _reservations(session_factory) == []
    gh2 = _fake_github()
    svc2 = SubmissionService(allocator, gh2, repo=REPO)
    assert svc2.submit_new_pathway(gpml=GOOD_GPML, submitter="bob").wpid == 5637


def test_leftover_branch_is_stepped_over(allocator, session_factory):
    # A previous submission whose PR was closed unmerged left submit/WP5637 behind, and the
    # floor did not account for it (a race, or a branch made by hand). The submit must move on
    # to the next id rather than failing with "branch already exists".
    gh = _fake_github()
    gh.branches[(REPO, "submit/WP5637")] = "stale000"
    svc = SubmissionService(allocator, gh, repo=REPO)

    result = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert result.wpid == 5638
    assert result.branch == "submit/WP5638"
    # The collided id was returned to the pool, so only the successful one is reserved.
    assert [r.wpid for r in _reservations(session_factory)] == [5638]


def test_branch_collisions_beyond_the_retry_budget_burn_no_wpid(allocator, session_factory):
    gh = _fake_github()
    for wpid in (5637, 5638, 5639, 5640):
        gh.branches[(REPO, f"submit/WP{wpid}")] = "stale000"
    svc = SubmissionService(allocator, gh, repo=REPO)

    with pytest.raises(BranchAlreadyExists):
        svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert _reservations(session_factory) == []
    assert gh.pulls == []


def test_submits_get_incrementing_wpids(allocator):
    gh = _fake_github()
    svc = SubmissionService(allocator, gh, repo=REPO)
    first = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    second = svc.submit_new_pathway(gpml=GOOD_GPML, submitter="bob")
    assert (first.wpid, second.wpid) == (5637, 5638)
    assert {b[1] for b in gh.branches} == {"main", "submit/WP5637", "submit/WP5638"}

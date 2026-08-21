"""A branch name identifies an edit only within one repository (issue #22).

The app pushes every submission branch to the content repo itself, so today a head branch is
unambiguous. That stops being true the moment a pull request arrives from a fork — and on
``wikipathways/wikipathways-database`` that is already the normal case, 36 of the last 53 closed
pull requests. ``submit/WP0001`` then exists in as many repositories as there are submitters.

Two things break silently at that point, and both are exercised here:

- ``find_open_pr`` hard-wired the base owner into GitHub's ``head=owner:branch`` filter, so a
  cross-repository pull request came back as "there is no open pull request". Revise raises
  ``NoPendingSubmission``, an update opens a *second* pull request for one pathway, and
  ``GET /api/pathways/{wpid}`` reports ``absent`` for a pathway with a live submission.
- The lock's open-PR scanner skipped any head ref starting ``submit/WP<id>`` or ``update/WP<id>``
  as "one of ours", *before* its file-based check ran. A contributor's fork branch of that name
  would be skipped as ours and the check-out handed out over their genuine concurrent edit —
  precisely the divergence the lock exists to prevent.

These are not fork-mode features. They are wrong today for a raw fork pull request opened by a
power user, which is the ordinary way work reaches that repository.
"""
from __future__ import annotations

import httpx
import pytest

from app.github import FakeGitHubClient, PullRequest
from app.github.client import HttpGitHubClient

BASE = "wikipathways/wikipathways-database"
FORK = "contributor/wikipathways-database"
BRANCH = "WP0001_contributor_20260803-120000"


def _pull(number: int, ref: str, head_repo: str | None) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/{BASE}/pull/{number}",
        "head": {"ref": ref, "repo": {"full_name": head_repo} if head_repo else None},
    }


# -- find_open_pr: the head filter ---------------------------------------------------------


def test_find_open_pr_asks_github_for_the_head_owner_it_was_given():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("head", ""))
        return httpx.Response(200, json=[_pull(7, "submit/WP0001", FORK)])

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))

    client.find_open_pr(BASE, "submit/WP0001")
    client.find_open_pr(BASE, "submit/WP0001", head_repo=FORK)

    assert seen == ["wikipathways:submit/WP0001", "contributor:submit/WP0001"]


def test_find_open_pr_reports_where_the_head_branch_actually_lives():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_pull(7, "submit/WP0001", FORK)])

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    pr = client.find_open_pr(BASE, "submit/WP0001", head_repo=FORK)

    assert pr is not None
    # Not merely the owner: revise needs `owner/name` to address the branch for a put_file.
    assert pr.head_repo == FORK


def test_a_same_repo_head_is_reported_as_none_not_as_the_base_repo_name():
    # None is the "no cross-repo anything here" signal the review row stores, and every row
    # written so far is that. Spelling it out as the base repo would make a plain submission
    # indistinguishable from one whose fork happens to share the target's name.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_pull(7, "submit/WP0001", BASE)])

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    assert client.find_open_pr(BASE, "submit/WP0001").head_repo is None


def test_a_deleted_fork_does_not_crash_the_lookup():
    # GitHub nulls head.repo once the fork behind a pull request is gone. Nothing downstream can
    # do anything useful with that branch either way, so it must read as "same as base" rather
    # than raise on a missing key.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_pull(7, "submit/WP0001", None)])

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    assert client.find_open_pr(BASE, "submit/WP0001").head_repo is None


def test_the_fake_will_not_hand_a_revise_somebody_elses_pull_request():
    # The branch name is identical in both repositories; only the head repo tells them apart.
    fake = FakeGitHubClient()
    fake.pulls.append(PullRequest(1, "u/1", "submit/WP0001", head_repo=FORK))
    fake.pulls.append(PullRequest(2, "u/2", "submit/WP0001", head_repo=None))

    assert fake.find_open_pr(BASE, "submit/WP0001").number == 2
    assert fake.find_open_pr(BASE, "submit/WP0001", head_repo=FORK).number == 1
    assert fake.find_open_pr(BASE, "submit/WP0001", head_repo="somebody/else") is None


def test_the_fake_splits_a_cross_repository_head_the_way_github_spells_it():
    # GitHub takes `owner:branch` for a cross-repo head and names only the owner; a fork keeps
    # the base repository's name. Without the split, `head_branch` would carry the owner too and
    # every later lookup for that branch would miss.
    fake = FakeGitHubClient()
    pr = fake.open_pull_request(BASE, head="contributor:submit/WP0001", base="main", title="t",
                                body="b")

    assert pr.head_branch == "submit/WP0001"
    assert pr.head_repo == FORK


# -- the lock's open-PR scanner ------------------------------------------------------------


def _scanner_transport(pulls: list[dict], files: dict[int, list[str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=pulls)
        if "/pulls/" in path and path.endswith("/files"):
            number = int(path.rsplit("/", 2)[1])
            return httpx.Response(
                200, json=[{"filename": f} for f in files.get(number, [])]
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_a_forks_branch_named_like_ours_is_not_mistaken_for_ours():
    # The failure this guards: the scanner returns None, the lock is granted, and two people are
    # now editing WP123 in parallel with no way to merge the result.
    client = HttpGitHubClient(
        "tok",
        transport=_scanner_transport(
            [_pull(9, "update/WP123", FORK)],
            {9: ["pathways/WP123/WP123.gpml"]},
        ),
    )
    assert client.find_open_pr_touching(BASE, "pathways/WP123/") == 9


def test_our_own_branch_on_the_content_repo_is_still_skipped():
    # The skip is what keeps a submitter's own in-flight edit from reading as a foreign writer,
    # and it saves a file listing per pull request. Narrowing it to same-repo heads must not
    # cost that.
    files_read: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[_pull(9, "update/WP123", BASE)])
        if path.endswith("/files"):
            files_read.append(int(path.rsplit("/", 2)[1]))
            return httpx.Response(200, json=[{"filename": "pathways/WP123/WP123.gpml"}])
        return httpx.Response(404)

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))

    assert client.find_open_pr_touching(BASE, "pathways/WP123/") is None
    assert files_read == [], "the skip should happen before the file listing, not after"


def test_a_forks_unrelated_branch_is_still_judged_on_its_files():
    client = HttpGitHubClient(
        "tok",
        transport=_scanner_transport(
            [_pull(9, "my-edits", FORK)],
            {9: ["pathways/WP999/WP999.gpml"]},
        ),
    )
    assert client.find_open_pr_touching(BASE, "pathways/WP123/") is None


def test_the_fake_scanner_sees_a_fork_pull_requests_files():
    # `GET /repos/{base}/pulls/{n}/files` lists a cross-repository pull request's files like any
    # other. A fake that scoped them to the base repo would make every fork edit invisible to
    # the lock — the one writer it most needs to see.
    fake = FakeGitHubClient()
    fake.pulls.append(PullRequest(9, "u/9", "their-branch", head_repo=FORK))
    fake.files[(FORK, "their-branch", "pathways/WP123/WP123.gpml")] = ("<gpml/>", "m", "sha1")

    assert fake.find_open_pr_touching(BASE, "pathways/WP123/") == 9


# -- revise across the fork boundary --------------------------------------------------------


def test_revise_commits_to_the_fork_the_branch_is_on(pipeline_service_factory):
    service, fake = pipeline_service_factory
    fake.pulls.append(PullRequest(4, "u/4", BRANCH, head_repo=FORK))
    fake.branches[(FORK, BRANCH)] = "sha-fork"
    fake.files[(FORK, BRANCH, "pathways/WP0001/WP0001.gpml")] = ("<x/>", "m", "sha1")

    result = service.revise_new_pathway(
        gpml=_GPML,
        submitter="contributor",
        branch=BRANCH,
        head_repo=FORK,
    )

    assert result.pr_number == 4
    # The commit landed on the fork, not on the content repository the app cannot push a
    # contributor's branch to.
    written, _, _ = fake.files[(FORK, BRANCH, "pathways/WP0001/WP0001.gpml")]
    assert "Test pathway" in written
    assert not any(repo == BASE for (repo, _, _) in fake.files)


def test_revise_without_a_head_repo_still_targets_the_content_repository(pipeline_service_factory):
    service, fake = pipeline_service_factory
    branch = "WP0001_marvinm2_20260803-120000"
    fake.pulls.append(PullRequest(5, "u/5", branch))
    fake.branches[(BASE, branch)] = "sha-base"
    fake.files[(BASE, branch, "pathways/WP0001/WP0001.gpml")] = ("<x/>", "m", "sha1")

    result = service.revise_new_pathway(gpml=_GPML, submitter="marvinm2", branch=branch)

    assert result.pr_number == 5
    written, _, _ = fake.files[(BASE, branch, "pathways/WP0001/WP0001.gpml")]
    assert "Test pathway" in written


_GPML = """<?xml version="1.0" encoding="UTF-8"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Test pathway" Organism="Homo sapiens">
  <Graphics BoardWidth="500.0" BoardHeight="400.0"/>
  <DataNode TextLabel="TP53" GraphId="a1" Type="GeneProduct">
    <Graphics CenterX="100.0" CenterY="100.0" Width="80.0" Height="20.0"/>
    <Xref Database="Ensembl" ID="ENSG00000141510"/>
  </DataNode>
  <InfoBox CenterX="0.0" CenterY="0.0"/>
</Pathway>
"""


@pytest.fixture
def pipeline_service_factory():
    from app.submit.service import SubmissionMode, SubmissionService

    fake = FakeGitHubClient()
    service = SubmissionService(None, fake, repo=BASE, mode=SubmissionMode.PIPELINE)
    return service, fake


# -- reading a pull request's file list, against real-shaped GitHub JSON --------------------
#
# The fake parses whatever it is handed, so it agrees with any implementation, right or wrong.
# That is exactly how `open_pull_request` echoed its own request for weeks with 494 tests green.
# These go through the real client and real response shapes.


def _file(name: str, status: str = "modified") -> dict:
    return {
        "filename": name,
        "status": status,
        "additions": 1,
        "deletions": 0,
        "changes": 1,
        "sha": "0" * 40,
    }


def test_list_pr_files_reads_filename_and_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{BASE}/pulls/73/files"
        return httpx.Response(
            200, json=[_file("pathways/WP3894/WP3894.gpml"), _file("README.md", "added")]
        )

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    files = client.list_pr_files(BASE, 73)

    # `filename`, not `path` — GitHub's pulls/files uses the former and the trees API the latter,
    # and reading the wrong one yields an empty list rather than an error.
    assert [f.filename for f in files] == ["pathways/WP3894/WP3894.gpml", "README.md"]
    assert [f.status for f in files] == ["modified", "added"]


def test_list_pr_files_pages():
    """A big pull request must not be silently truncated at 100 files.

    Truncation here reads as "this pull request touches one pathway" when it touches thirty —
    the multi-pathway gate would pass and approval would publish one of them.
    """
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        pages.append(page)
        if page == "1":
            batch = [_file(f"pathways/WP{i}/WP{i}.gpml") for i in range(100)]
            return httpx.Response(200, json=batch)
        if page == "2":
            return httpx.Response(200, json=[_file("pathways/WP999/WP999.gpml")])
        return httpx.Response(200, json=[])

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    files = client.list_pr_files(BASE, 73)

    assert len(files) == 101
    assert pages == ["1", "2"]


def test_one_unreadable_pull_request_does_not_truncate_the_lock_scan():
    """The scan's whole job is to be sure. A 404 on one pull request must not end it.

    Ending early reads as "nothing touches this pathway", and the lock is then handed out over a
    real concurrent edit — the divergence the lock exists to prevent.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[_pull(7, "their-branch", FORK), _pull(8, "other-branch", FORK)],
            )
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=[_file("pathways/WP1001/WP1001.gpml")])

    client = HttpGitHubClient("tok", transport=httpx.MockTransport(handler))
    assert client.find_open_pr_touching(BASE, "pathways/WP1001/") == 8

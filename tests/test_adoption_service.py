"""Adopting a pull request the portal did not open, end to end against the fake.

The one thing these tests must actually prove is that the *head* of the pull request is what gets
reviewed. Until the fake learned to answer a commit ref, it returned the base-branch bytes for any
ref at all — so an adopted update would have rendered the base against itself, reported an empty
diff, and passed. `test_the_review_describes_the_head_not_the_base` is the test that fails when
that fix is reverted, which is the only thing that makes it worth having.
"""
from __future__ import annotations

import pytest

from app.curators import ConfigCurators
from app.github import FakeGitHubClient
from app.locks import PathwayLockRegistry
from app.preview import PreviewService
from app.review.adoption import AdoptionService
from app.review.service import CurationService, ReviewNotFound

REPO = "wikipathways/sandbox-wp-db"
HEAD_SHA = "e47f6026bcfa42d4f4991296a728eb0babb23f41"


def _gpml(*nodes: str, name: str = "Glycolysis") -> str:
    body = "\n".join(nodes)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="{name}" Organism="Homo sapiens">
  <Comment>A pathway used to exercise adoption of a foreign pull request end to end.</Comment>
  <Graphics BoardWidth="900" BoardHeight="600"/>
{body}
</Pathway>
"""


def _node(label: str, identifier: str, *, cx: int = 240, cy: int = 200) -> str:
    return f"""  <DataNode TextLabel="{label}" Type="GeneProduct">
    <Graphics CenterX="{cx}" CenterY="{cy}" Width="120" Height="34"/>
    <Xref Database="Ensembl" ID="{identifier}"/>
  </DataNode>"""


BASE_GPML = _gpml(_node("INSR", "ENSG00000171105"))
HEAD_GPML = _gpml(_node("INSR", "ENSG00000171105"), _node("AKT1", "ENSG00000142208", cy=320))


@pytest.fixture
def previews(tmp_path):
    return PreviewService(cache_dir=tmp_path / "preview-cache")


@pytest.fixture
def locks(session_factory):
    return PathwayLockRegistry(session_factory)


def _adoption(session_factory, gh, previews, locks=None) -> AdoptionService:
    curation = CurationService(
        session_factory,
        gh,
        repo=REPO,
        curators=ConfigCurators({"curator"}),
        locks=locks,
        previews=previews,
        publish_mode="pipeline",
    )
    return AdoptionService(
        github=gh,
        previews=previews,
        curation=curation,
        locks=locks,
        content_repo=REPO,
        default_branch="main",
    )


def _plugin_update(gh: FakeGitHubClient, *, number: int = 73) -> None:
    """PR #73 on the live sandbox: `Contribution: Update WP3894`, from the author's own fork."""
    gh.existing_contents[(REPO, "pathways/WP3894/WP3894.gpml")] = BASE_GPML
    gh.seed_foreign_pr(
        REPO,
        number,
        author="traybug23",
        title="Contribution: Update WP3894",
        head_branch="WP3894_traybug23_20260820-053517",
        head_repo="traybug23/sandbox-wp-db",
        head_sha=HEAD_SHA,
        files=[("pathways/WP3894/WP3894.gpml", "modified", HEAD_GPML)],
    )


def test_adopting_an_update_builds_a_full_review(session_factory, previews, locks):
    gh = FakeGitHubClient()
    _plugin_update(gh)
    svc = _adoption(session_factory, gh, previews, locks)

    outcome = svc.adopt(73)

    assert outcome.adopted is True
    review = svc._curation.get(73)
    assert (review.origin, review.adopted) == ("adopted", True)
    assert (review.kind, review.wpid) == ("update", 3894)
    # The author of the pull request, not a portal session — there is none.
    assert review.submitter == "traybug23"
    assert review.head_repo == "traybug23/sandbox-wp-db"
    assert review.head_sha == HEAD_SHA
    assert review.base_repo == REPO
    assert review.pathway_paths == ["pathways/WP3894/WP3894.gpml"]
    assert previews.status(73) == "ready"


def test_the_review_describes_the_head_not_the_base(session_factory, previews, locks):
    """The head adds AKT1; the base does not. The diff must say so.

    This is the assertion that catches a client which ignores the ref it was given: with the base
    served for both sides the diff is empty, every scoped checklist item resolves N/A, and nothing
    else in the suite notices.
    """
    gh = FakeGitHubClient()
    _plugin_update(gh)
    _adoption(session_factory, gh, previews, locks).adopt(73)

    diff = previews.diff(73)
    assert diff is not None
    assert diff["summary"]["added"] == 1
    # And it is the *after* side that gained it — the base has one node, the head has two.
    nodes = previews.nodes(73, "after")
    assert {n["label"] for n in nodes} == {"INSR", "AKT1"}
    assert {n["label"] for n in previews.nodes(73, "before")} == {"INSR"}


def test_a_new_pathway_at_a_title_derived_path_is_adopted(session_factory, previews, locks):
    """What the plugin actually writes for a new submission — no WPID anywhere in the tree."""
    gh = FakeGitHubClient()
    gh.seed_foreign_pr(
        REPO,
        63,
        author="traybug23",
        title="Contribution: testing new pathway",
        head_branch="contribution-1783602150469",
        head_repo="traybug23/sandbox-wp-db",
        head_sha="a" * 40,
        files=[
            (
                "pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml",
                "added",
                _gpml(_node("INSR", "ENSG00000171105"), name="Test glycolysis"),
            )
        ],
    )
    svc = _adoption(session_factory, gh, previews, locks)

    assert svc.adopt(63).adopted is True
    review = svc._curation.get(63)
    assert (review.kind, review.wpid) == ("new", None)
    assert review.pathway_path == "pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml"
    # No WPID yet, and the placeholder string is what the UI shows — never "WP0".
    assert review.wpid_str == "WP0001 (unassigned)"


def test_a_pull_request_with_no_pathway_files_is_not_adopted(session_factory, previews):
    """Live PR #67 changes only workflows. It must not appear in the curation queue."""
    gh = FakeGitHubClient()
    gh.seed_foreign_pr(
        REPO,
        67,
        author="marvinm2",
        title="Make pull requests from forks work",
        head_branch="fork-pr-support",
        head_repo=None,
        head_sha="b" * 40,
        files=[(".github/workflows/1_on_pull_request.yml", "modified", "on: pull_request\n")],
    )
    svc = _adoption(session_factory, gh, previews)

    outcome = svc.adopt(67)
    assert outcome.adopted is False
    assert outcome.reason == "changes no pathway files"
    with pytest.raises(ReviewNotFound):
        svc._curation.get(67)


def test_a_multi_pathway_pull_request_is_adopted_but_cannot_be_approved(
    session_factory, previews, locks
):
    """Live PR #58 touches WP1072 and WP179.

    Adopted, because a curator should see it; blocked, because the repository publishes one
    pathway per pull request and approving would publish at most one of them.
    """
    gh = FakeGitHubClient()
    gh.existing_contents[(REPO, "pathways/WP1072/WP1072.gpml")] = BASE_GPML
    gh.existing_contents[(REPO, "pathways/WP179/WP179.gpml")] = BASE_GPML
    gh.seed_foreign_pr(
        REPO,
        58,
        author="traybug23",
        title="test commit for pathvisio github plugin",
        head_branch="test-commit-flow",
        head_repo="traybug23/sandbox-wp-db",
        head_sha="c" * 40,
        files=[
            ("pathways/WP1072/WP1072.gpml", "modified", HEAD_GPML),
            ("pathways/WP179/WP179.gpml", "modified", HEAD_GPML),
        ],
    )
    svc = _adoption(session_factory, gh, previews, locks)

    assert svc.adopt(58).adopted is True
    review = svc._curation.get(58)
    item = next(i for i in review.checklist if i["key"] == "one_pathway_per_pr")
    assert item["state"] == "fail"
    assert item["required"] is True
    assert "WP1072" in item["note"] and "WP179" in item["note"]


def test_a_closed_pull_request_is_not_adopted(session_factory, previews):
    gh = FakeGitHubClient()
    _plugin_update(gh)
    gh.closed.add(73)
    outcome = _adoption(session_factory, gh, previews).adopt(73)
    assert (outcome.adopted, outcome.reason) == (False, "is closed")


# --- the lock -----------------------------------------------------------------------------


def test_adoption_takes_the_lock_without_the_scanner_refusing_on_itself(
    session_factory, previews
):
    """`acquire` would refuse here: its scanner finds the very pull request being adopted.

    A plugin branch is `WP<id>_<login>_<stamp>` on a fork, so the scanner's "one of ours" test —
    an `update/`/`submit/` branch on the base repo — never matches it.
    """
    scanned = []

    def scanner(wpid):
        scanned.append(wpid)
        return True  # yes, there is an open PR touching it: the one being adopted

    locks = PathwayLockRegistry(session_factory, open_pr_scanner=scanner)
    gh = FakeGitHubClient()
    _plugin_update(gh)

    _adoption(session_factory, gh, previews, locks).adopt(73)

    held = locks.get(3894)
    assert held is not None
    assert (held.held_by, held.pr_number) == ("traybug23", 73)
    assert scanned == []  # the scan is not run at all


def test_adoption_never_steals_a_lock_a_portal_user_holds(session_factory, previews, locks):
    locks.acquire(3894, "alice", pr_number=999)
    gh = FakeGitHubClient()
    _plugin_update(gh)

    # The review is still built — a curator needs to see that a second edit is in flight — but
    # the check-out stays with the person who has it.
    assert _adoption(session_factory, gh, previews, locks).adopt(73).adopted is True
    held = locks.get(3894)
    assert (held.held_by, held.pr_number) == ("alice", 999)


def test_closing_one_pull_request_does_not_free_another_ones_lock(session_factory, locks):
    """Six open pull requests touch WP1001 on the live target, so this is the ordinary case."""
    locks.adopt(1001, "traybug23", pr_number=60)
    assert locks.release_for_pr(1001, 75) is False
    assert locks.get(1001) is not None
    assert locks.release_for_pr(1001, 60) is True
    assert locks.get(1001) is None


# --- staying current ----------------------------------------------------------------------


def test_a_base_branch_move_does_not_re_render(session_factory, previews, locks):
    """`synchronize` fires when the base moves too. Re-rendering then re-posts the mirror."""
    gh = FakeGitHubClient()
    _plugin_update(gh)
    svc = _adoption(session_factory, gh, previews, locks)
    svc.adopt(73)

    outcome = svc.adopt(73)
    assert (outcome.adopted, outcome.reason) == (False, "head commit is unchanged")


def test_a_new_commit_re_derives_and_keeps_a_curators_answers(session_factory, previews, locks):
    gh = FakeGitHubClient()
    _plugin_update(gh)
    svc = _adoption(session_factory, gh, previews, locks)
    svc.adopt(73)
    svc._curation.set_checklist_item(73, "render_ok", "pass")
    svc._curation.request_changes(73, "curator", "please annotate the new node")

    # The author pushes a fix: a second commit on their own branch.
    revised = _gpml(
        _node("INSR", "ENSG00000171105"),
        _node("AKT1", "ENSG00000142208", cy=320),
        _node("TP53", "ENSG00000141510", cy=440),
    )
    gh.pull_meta[73]["head_sha"] = "d" * 40
    gh.commit_contents[(REPO, "d" * 40, "pathways/WP3894/WP3894.gpml")] = revised

    outcome = svc.adopt(73)

    assert (outcome.adopted, outcome.refreshed) == (True, True)
    review = svc._curation.get(73)
    assert review.head_sha == "d" * 40
    # A re-read after changes were requested puts it back in the queue, as a re-upload does.
    assert review.status.value == "open"
    # The curator answered this one by hand; re-deriving must not throw that away.
    assert next(i for i in review.checklist if i["key"] == "render_ok")["state"] == "pass"


def test_a_portal_registration_upgrades_a_row_the_webhook_adopted_first(
    session_factory, previews, locks
):
    """The race: `opened` arrives before the submission that opened it finishes registering.

    Rather than trying to recognise our own branches — the plugin uses the same shape — the
    portal registration wins on contact, whichever order they arrive in.
    """
    gh = FakeGitHubClient()
    _plugin_update(gh, number=79)
    svc = _adoption(session_factory, gh, previews, locks)
    svc.adopt(79)
    assert svc._curation.get(79).origin == "adopted"

    svc._curation.register(
        pr_number=79,
        wpid=3894,
        submitter="marvinm2",
        kind="update",
        head_branch="update/WP3894",
        head_repo=None,
    )

    review = svc._curation.get(79)
    assert (review.origin, review.adopted) == ("portal", False)
    assert review.submitter == "marvinm2"
    assert review.head_branch == "update/WP3894"
    assert review.head_repo is None


def test_the_rate_limiter_does_not_count_adopted_rows(session_factory, previews, locks):
    """Someone's plugin pull requests must not spend their portal quota.

    The limiter bounds what this app opens on a person's behalf. An adopted row records a pull
    request its author opened on GitHub without going near the portal — counting those would
    refuse their next portal submission, in words that read as if they had done something wrong.
    """
    from datetime import timedelta

    from app.ratelimit import SubmissionRateLimiter

    gh = FakeGitHubClient()
    svc = _adoption(session_factory, gh, previews, locks)
    for n in (60, 61, 62):
        gh.existing_contents[(REPO, "pathways/WP1001/WP1001.gpml")] = BASE_GPML
        gh.seed_foreign_pr(
            REPO,
            n,
            author="traybug23",
            title=f"Contribution: Update WP1001 ({n})",
            head_branch=f"contribution-{n}",
            head_repo="traybug23/sandbox-wp-db",
            head_sha=f"{n}" * 40,
            files=[("pathways/WP1001/WP1001.gpml", "modified", HEAD_GPML)],
        )
        svc.adopt(n)

    limiter = SubmissionRateLimiter(session_factory, limit=2, window=timedelta(hours=1))
    limiter.check("traybug23")  # three adopted rows, and the quota is untouched


def test_an_adopted_review_cannot_be_revised_through_the_portal(tmp_path):
    """The branch is on the author's own fork and the app never wrote it.

    Committing here would need push access to a stranger's repository — and under a curator's
    token it might even succeed, which is worse than the refusal.
    """
    import io

    from fastapi.testclient import TestClient

    from app.main import (
        build_app,
        get_bot_client,
        get_bot_optional,
        get_current_user,
        get_github_client,
    )
    from tests.test_api import GOOD_GPML, _seed_plugin_pr, _settings

    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        curators=["curator"],
        preview_cache_dir=str(tmp_path / "preview-cache"),
        publish_mode="pipeline",
        adopt_foreign_prs=True,
    )
    gh = _seed_plugin_pr(settings, 63)
    gh.pr_files[(settings.content_repo, 63)][0] = type(
        gh.pr_files[(settings.content_repo, 63)][0]
    )(filename="pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml", status="added")
    gh.commit_contents[
        (settings.content_repo, "e47f6026bcfa42d4f4991296a728eb0babb23f41",
         "pathways/TESTGLYCOLYSIS/TESTGLYCOLYSIS.gpml")
    ] = GOOD_GPML.decode()

    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: gh
    app.dependency_overrides[get_bot_optional] = lambda: gh
    app.dependency_overrides[get_bot_client] = lambda: gh
    app.dependency_overrides[get_current_user] = lambda: "curator"

    with TestClient(app) as c:
        assert c.post("/api/reviews/63/adopt").json()["adopted"] is True
        r = c.post(
            "/api/reviews/63/revise",
            files={"file": ("fix.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert r.status_code == 409
        assert "opened outside the portal" in r.json()["detail"]

"""Approval, rejection and publication against a repo that publishes through its own Actions.

The thing under test is a handshake, not a function call: the app applies a label, some workflow
it does not run does the work, and the PR comes back **closed without being merged**. Every test
here replays that sequence through FakeGitHubClient's simulators.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.curators import ConfigCurators
from app.github import FakeGitHubClient, GitHubError
from app.models import Review, ReviewStatus
from app.review.checklist import ChecklistState
from app.review.service import (
    MIRROR_MARKER,
    CurationService,
    NotACurator,
    ReviewNotActionable,
    ReviewNotFound,
    parse_publish_marker,
)
from tests.conftest import RecordingPreviews

REPO = "wikipathways/sandbox-wp-db"
CURATOR = "marvinm2"


def _service(session_factory, gh, **kw) -> CurationService:
    return CurationService(
        session_factory,
        gh,
        repo=REPO,
        curators=ConfigCurators([CURATOR]),
        publish_mode="pipeline",
        **kw,
    )


def _fake(**kw) -> FakeGitHubClient:
    return FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"}, **kw)


def _register(svc, gh, *, kind="new", wpid=None) -> int:
    pr = gh.open_pull_request(
        REPO, head="WP0001_alice_20260727-173500", base="main", title="t", body="b"
    )
    svc.register(
        pr_number=pr.number,
        wpid=wpid,
        submitter="alice",
        kind=kind,
        head_branch=pr.head_branch,
    )
    return pr.number


def _complete_checklist(svc, pr_number: int) -> None:
    review = svc.get(pr_number)
    for item in review.checklist:
        if item.get("required"):
            svc.set_checklist_item(pr_number, item["key"], ChecklistState.PASS.value)


# -- the marker parser ---------------------------------------------------------------------


def test_marker_is_parsed_out_of_a_comment():
    body = (
        '<!-- wikipathways-publish {"pr":54,"wpid":5678,"status":"published"} -->\n'
        "Published as WP5678."
    )
    assert parse_publish_marker(body) == {"pr": 54, "wpid": 5678, "status": "published"}


def test_a_comment_without_a_marker_parses_to_nothing():
    assert parse_publish_marker("Looks good to me, merging.") is None


def test_a_malformed_marker_is_not_a_crash():
    assert parse_publish_marker("<!-- wikipathways-publish {not json} -->") is None


# -- approve -------------------------------------------------------------------------------


def test_approve_labels_and_does_not_merge(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    review = svc.approve(pr, CURATOR)

    assert review.status == ReviewStatus.APPROVED
    assert gh.list_labels(REPO, pr) == ["accepted"]
    assert pr not in gh.merged
    assert pr not in gh.closed


def test_approve_tells_the_submitter_something_happened(session_factory):
    # The label is silent and the PR body gets rewritten, so the comment is the only signal.
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    svc.approve(pr, CURATOR)

    assert any("approved" in c for c in gh.issue_comments[(REPO, pr)])


def test_a_failed_label_leaves_the_review_reviewable(session_factory):
    # The label IS the mechanism here, so a failure must not leave the review sitting in
    # APPROVED waiting on a workflow that was never triggered.
    gh = _fake()
    gh.fail_on.add("add_labels")
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    with pytest.raises(GitHubError):
        svc.approve(pr, CURATOR)

    assert svc.get(pr).status == ReviewStatus.OPEN


def test_approve_still_refuses_a_non_curator(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    with pytest.raises(NotACurator):
        svc.approve(pr, "stranger")
    assert gh.list_labels(REPO, pr) == []


# -- publication ---------------------------------------------------------------------------


def test_publication_records_the_assigned_wpid(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=5678)
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5678
    assert review.published_at is not None
    assert review.decision_note is None


# -- reading the published file back -------------------------------------------------------
#
# The target repository's publish workflow renames WP0__PR<n>.gpml to WP<n>.gpml without opening
# it, so WP5426, WP5427, WP5428 and WP5429 all landed on the content repository still declaring
# Version="WP0001_r...". Four publications, none of them read back. The bytes were already being
# fetched here to answer "did it land"; they are now also asked what they say they are.


def test_a_file_that_declares_the_wrong_wpid_is_reported(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=5678, declares="WP0001")  # exactly what WP5429 did
    review = svc.handle_pr_closed(pr, merged=False)

    # The pathway is out; the status stands. The disagreement is recorded beside it.
    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5678
    assert "WP0001" in review.decision_note
    assert "Version" in review.decision_note


def test_an_announcement_ahead_of_the_push_is_called_early_not_wrong(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=5678, write_file=False)
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISHED
    assert "not visible" in review.decision_note


def test_a_close_with_no_announcement_is_a_failure_not_a_success(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=None)  # the observed failure mode
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISH_FAILED
    assert review.wpid is None
    assert "WPID" in review.decision_note


def test_an_update_keeps_its_own_id_through_publication(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh, kind="update", wpid=5636)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=5636)  # an edit is announced under the id it already had
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5636


def test_an_update_that_closes_without_an_announcement_is_not_called_published(session_factory):
    """An update already has a WPID and its file is already on main, so both of the signals a
    new pathway is judged on are satisfied before the submission even happened. Reading a silent
    close as success there would record every abandoned edit as a publication."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh, kind="update", wpid=5636)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    gh.simulate_3a(REPO, pr, wpid=None)  # closed, nothing announced
    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISH_FAILED
    assert review.wpid == 5636  # the id it came in with; nothing new was assigned


def test_a_closed_pr_on_an_unapproved_review_is_still_just_closed(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.CLOSED


def test_the_manual_escape_hatch_reaches_published(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)
    svc.handle_pr_closed(pr, merged=False)

    review = svc.record_published_wpid(pr, 5678, CURATOR)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5678


def test_the_escape_hatch_is_curator_only(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    with pytest.raises(NotACurator):
        svc.record_published_wpid(pr, 5678, "stranger")


# -- reconcile -----------------------------------------------------------------------------


def test_a_stuck_approval_eventually_reports_failure(session_factory):
    # The realistic case: the label goes on and the repo's dispatcher never fires.
    gh = _fake()
    svc = _service(
        session_factory,
        gh,
        publish_timeout=timedelta(minutes=30),
        reconcile_min_interval=timedelta(seconds=0),
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_dispatcher_failure(REPO, pr)

    with session_factory() as s:
        s.get(Review, pr).approved_at = datetime.now(UTC) - timedelta(hours=2)
        s.commit()

    assert svc.reconcile() == 1
    review = svc.get(pr)
    assert review.status == ReviewStatus.PUBLISH_FAILED
    assert "has not published it" in review.decision_note


def test_a_recent_approval_is_left_alone(session_factory):
    gh = _fake()
    svc = _service(
        session_factory, gh, publish_timeout=timedelta(minutes=30),
        reconcile_min_interval=timedelta(seconds=0),
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    assert svc.reconcile() == 0
    assert svc.get(pr).status == ReviewStatus.APPROVED


def test_removing_the_label_puts_it_back_in_the_queue(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.remove_label(REPO, pr, "accepted")

    assert svc.reconcile() == 1
    review = svc.get(pr)
    assert review.status == ReviewStatus.OPEN
    assert "removed" in review.decision_note


def test_reconcile_refreshes_the_labels_it_sees(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    gh.add_labels(REPO, pr, ["tests passed", "review required"])

    svc.reconcile()

    assert svc.get(pr).github_labels == ["review required", "tests passed"]


def test_the_throttle_stops_a_second_check(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(minutes=5))
    pr = _register(svc, gh)
    gh.add_labels(REPO, pr, ["tests passed"])

    svc.reconcile()
    gh.fail_on.add("get_pull_request")  # a second look would blow up

    assert svc.reconcile() == 0


# -- reject --------------------------------------------------------------------------------


def test_reject_comments_before_it_labels(session_factory):
    # The rejection workflow deletes the drafts, so the reason has to be on the record first.
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    svc.reject(pr, CURATOR, note="Duplicate of WP1234.")

    assert gh.issue_comments[(REPO, pr)]
    assert ("Duplicate of WP1234." in gh.issue_comments[(REPO, pr)][0])
    assert gh.label_log[-1] == (REPO, pr, "add", "rejected")


def test_reject_is_terminal_and_keeps_the_reason(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    review = svc.reject(pr, CURATOR, note="Out of scope.")

    assert review.status == ReviewStatus.REJECTED
    assert review.decision_note == "Out of scope."
    assert review.decided_by == CURATOR


def test_the_repos_rejection_workflow_does_not_undo_the_rejection(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    svc.reject(pr, CURATOR)

    gh.simulate_3b(REPO, pr)
    svc.reconcile()

    assert svc.get(pr).status == ReviewStatus.REJECTED


def test_reject_is_curator_only(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    with pytest.raises(NotACurator):
        svc.reject(pr, "stranger")
    assert gh.list_labels(REPO, pr) == []


def test_reject_on_an_unknown_pr_is_a_404(session_factory):
    svc = _service(session_factory, _fake())
    with pytest.raises(ReviewNotFound):
        svc.reject(999, CURATOR)


# -- labels applied on GitHub directly -----------------------------------------------------


def test_a_curator_labelling_on_github_moves_the_review(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    review = svc.handle_label_event(pr, "accepted", added=True, actor="egonw")

    assert review.status == ReviewStatus.APPROVED
    assert review.approved_by == "egonw"
    assert "incomplete checklist" in review.decision_note


def test_a_complete_checklist_leaves_no_discrepancy_note(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)

    review = svc.handle_label_event(pr, "accepted", added=True, actor="egonw")

    assert review.decision_note is None


def test_our_own_label_echo_is_a_no_op(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    assert svc.handle_label_event(pr, "accepted", added=True, actor=CURATOR) is None
    assert svc.get(pr).approved_by == CURATOR


def test_unlabelling_accepted_reopens_the_review(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    review = svc.handle_label_event(pr, "accepted", added=False, actor="egonw")

    assert review.status == ReviewStatus.OPEN


def test_the_publish_workflow_removing_accepted_does_not_unapprove(session_factory):
    """A bot removing `accepted` is the publish pipeline doing its own housekeeping.

    3A drops the label when it reports a failure, and again when it replaces it with
    `published`. Treating that as a withdrawn approval takes the review back to OPEN mid
    publication, and then the close is written as CLOSED — terminal — instead of being routed
    through `_settle_publication`, so the announced WPID is lost with nothing to re-check it.

    Measured on PR #78 of wikipathways/sandbox-wp-db, 2026-08-21: published as WP5425, recorded
    by the app as `closed` with no WPID.
    """
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    assert svc.handle_label_event(pr, "accepted", added=False, actor="github-actions[bot]") is None
    assert svc.get(pr).status == ReviewStatus.APPROVED

    # Still APPROVED, so a close now settles from the marker rather than falling through.
    gh.create_issue_comment(
        REPO,
        pr,
        f'<!-- wikipathways-publish {{"pr":{pr},"wpid":5425,"status":"published"}} -->\n'
        "Published as WP5425.",
    )
    review = svc.handle_pr_closed(pr, merged=False)
    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5425


def test_an_unrelated_label_changes_nothing(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)

    assert svc.handle_label_event(pr, "tests passed", added=True, actor="egonw") is None
    assert svc.get(pr).status == ReviewStatus.OPEN


# ---------------------------------------------------------------------------------------------
# Getting stuck, and getting unstuck. The publish workflow is the one part of the loop the app
# does not control, so what happens when it says nothing is the case that matters most.


def test_a_publication_that_never_happened_is_not_quietly_reclassified(session_factory):
    """PUBLISH_FAILED is not terminal — the review keeps being re-checked, in case a late run
    publishes it after all. That must not let the ordinary closed-pull-request path overwrite it
    with CLOSED, which *is* terminal and would strand the pathway with nobody looking."""
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)
    assert svc.handle_pr_closed(pr, merged=False).status == ReviewStatus.PUBLISH_FAILED
    note = svc.get(pr).decision_note

    svc.reconcile()
    svc.reconcile()

    assert svc.get(pr).status == ReviewStatus.PUBLISH_FAILED
    assert svc.get(pr).decision_note == note


def test_a_late_publication_is_still_recorded(session_factory):
    """The timeout says "the repository has not published this in 30 minutes", not "it never
    will". When the announcement finally arrives, the assigned WPID has to survive."""
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)
    svc.handle_pr_closed(pr, merged=False)
    assert svc.get(pr).status == ReviewStatus.PUBLISH_FAILED

    # A re-run of the repository's workflow finally succeeds.
    gh.create_issue_comment(
        REPO,
        pr,
        f'<!-- wikipathways-publish {{"pr":{pr},"wpid":5678,"status":"published"}} -->\n'
        "Published as WP5678.",
    )
    svc.reconcile()

    assert svc.get(pr).status == ReviewStatus.PUBLISHED
    assert svc.get(pr).wpid == 5678


def test_publishing_frees_the_cached_render(session_factory):
    """Issue #18, on the path the live deployment actually takes.

    Freeing was wired to every terminal transition a curator can reach through the dashboard, and
    to none of the one the repository reaches on its own -- so in pipeline mode the cache leaked
    on every pathway that published, which is the success case rather than an edge.
    """
    gh = _fake()
    previews = RecordingPreviews()
    svc = _service(session_factory, gh, previews=previews)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=5678)

    svc.handle_pr_closed(pr, merged=False)

    assert svc.get(pr).status == ReviewStatus.PUBLISHED
    assert previews.discarded == [pr]


def test_a_publication_that_failed_keeps_its_render(session_factory):
    # PUBLISH_FAILED is not terminal: it is waiting on a person, and the person it is waiting on
    # is the one who needs to look at the diagram.
    gh = _fake()
    previews = RecordingPreviews()
    svc = _service(
        session_factory, gh, previews=previews, reconcile_min_interval=timedelta(seconds=0)
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)

    svc.handle_pr_closed(pr, merged=False)

    assert svc.get(pr).status == ReviewStatus.PUBLISH_FAILED
    assert previews.discarded == []
    # ...and it stays out of the sweep's reach too, since it is not terminal.
    svc.reconcile()
    assert previews.swept == [{pr}]


def test_a_stuck_publication_is_not_freed_again_on_every_reconcile(session_factory):
    """`_settle_publication` re-runs on every reconcile of a stuck review, and only the *changed*
    branch acts -- the same guard that stops the mirror comment being re-posted forever."""
    gh = _fake()
    previews = RecordingPreviews()
    svc = _service(
        session_factory, gh, previews=previews, reconcile_min_interval=timedelta(seconds=0)
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=5678)
    svc.handle_pr_closed(pr, merged=False)
    assert previews.discarded == [pr]

    svc.reconcile()

    assert previews.discarded == [pr]


def test_an_announced_failure_is_read_as_a_failure(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.create_issue_comment(
        REPO,
        pr,
        f'<!-- wikipathways-publish {{"pr":{pr},"status":"failed","step":"push_assets"}} -->\n'
        "Publication failed.",
    )
    gh.closed.add(pr)

    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISH_FAILED
    assert "push_assets" in review.decision_note


def test_re_approving_re_applies_the_label_so_the_dispatcher_fires(session_factory):
    """GitHub emits no `labeled` event for a label that is already there, and the repository's
    dispatcher listens for nothing else. Adding it a second time would be a silent no-op."""
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)
    svc.handle_pr_closed(pr, merged=False)
    # A curator re-runs the workflow by hand, records the outcome, and someone re-opens the PR.
    gh.closed.discard(pr)
    svc._set_status(pr, ReviewStatus.OPEN, actor=None, note=None)
    gh.label_log.clear()

    svc.approve(pr, CURATOR)

    assert gh.label_log == [
        (REPO, pr, "remove", "accepted"),
        (REPO, pr, "add", "accepted"),
    ]


def test_rejecting_an_approved_review_takes_the_approval_back_off(session_factory):
    """A pull request carrying both labels is one whose next dispatcher run is a coin toss."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    svc.reject(pr, CURATOR, "on reflection, no")

    assert gh.list_labels(REPO, pr) == ["rejected"]


def test_requesting_changes_on_an_approved_review_takes_the_label_back_off(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    svc.request_changes(pr, CURATOR, "one more thing")

    assert gh.list_labels(REPO, pr) == []
    assert svc.get(pr).status == ReviewStatus.CHANGES_REQUESTED


def test_rejecting_by_label_on_github_still_frees_the_pathway(session_factory, locks):
    """REJECTED is terminal, so nothing downstream will ever release the lock. A curator who
    reaches for the label on GitHub rather than the dashboard must not leave the pathway checked
    out until the TTL runs out days later."""
    gh = _fake()
    svc = _service(session_factory, gh, locks=locks)
    pr = _register(svc, gh, kind="update", wpid=554)
    locks.acquire(554, "alice", pr_number=pr)
    assert locks.get(554) is not None

    svc.handle_label_event(pr, "rejected", added=True, actor="egonw")

    assert svc.get(pr).status == ReviewStatus.REJECTED
    assert locks.get(554) is None


def test_recording_a_wpid_by_hand_also_frees_the_pathway(session_factory, locks):
    gh = _fake()
    svc = _service(session_factory, gh, locks=locks)
    pr = _register(svc, gh, kind="update", wpid=554)
    locks.acquire(554, "alice", pr_number=pr)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)  # the form is only offered once a publication is outstanding

    svc.record_published_wpid(pr, 554, CURATOR)

    assert svc.get(pr).status == ReviewStatus.PUBLISHED
    assert locks.get(554) is None


def test_a_re_upload_re_derives_the_checklist_from_the_new_file(session_factory):
    """The update flow reuses the pull request, so `register` is the only place a revised update
    is seen. Reading a checklist derived from the file the submitter already replaced is how a
    curator fails a submission that was fixed."""
    from app.preview.metadata import parse_curation_metadata

    unannotated = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P" Organism="Homo sapiens">'
        '<DataNode TextLabel="IRS1" Type="GeneProduct"><Xref Database="" ID=""/></DataNode>'
        "</Pathway>"
    )
    annotated = unannotated.replace('Database="" ID=""', 'Database="Entrez Gene" ID="3667"')
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh, kind="update", wpid=554)
    svc.register(
        pr_number=pr, wpid=554, submitter="alice", kind="update",
        metadata=parse_curation_metadata(unannotated),
    )
    before = next(i for i in svc.get(pr).checklist if i["key"] == "datanodes_mapped")
    assert before["state"] == ChecklistState.FAIL.value

    svc.register(
        pr_number=pr, wpid=554, submitter="alice", kind="update",
        metadata=parse_curation_metadata(annotated),
    )

    after = next(i for i in svc.get(pr).checklist if i["key"] == "datanodes_mapped")
    assert after["state"] == ChecklistState.PASS.value


def test_a_re_upload_keeps_what_a_curator_answered_by_hand(session_factory):
    from app.preview.metadata import parse_curation_metadata

    gpml = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="P" Organism="Homo sapiens">'
        "</Pathway>"
    )
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh, kind="update", wpid=554)
    svc.set_checklist_item(pr, "description_ok", "pass", note="checked it myself")

    svc.register(
        pr_number=pr, wpid=554, submitter="alice", kind="update",
        metadata=parse_curation_metadata(gpml),
    )

    item = next(i for i in svc.get(pr).checklist if i["key"] == "description_ok")
    assert item["state"] == "pass"
    assert item["note"] == "checked it myself"



def test_a_wpid_cannot_be_recorded_on_a_review_with_no_publication_outstanding(session_factory):
    """PUBLISHED is terminal, so this would freeze a mistake: a typo'd pull request number would
    overwrite somebody else's rejection reason and never be reconciled back."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    svc.reject(pr, CURATOR, "duplicate")

    with pytest.raises(ReviewNotActionable):
        svc.record_published_wpid(pr, 5678, CURATOR)
    assert svc.get(pr).decision_note == "duplicate"


def test_a_publish_failure_can_be_approved_again(session_factory):
    """Re-applying the label is how a stuck publication is retried, and it is the state every
    approval on the live target lands in. Refusing it leaves the curator with nothing to do."""
    gh = _fake()
    svc = _service(session_factory, gh, reconcile_min_interval=timedelta(seconds=0))
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=None)
    svc.handle_pr_closed(pr, merged=False)
    assert svc.get(pr).status == ReviewStatus.PUBLISH_FAILED
    gh.closed.discard(pr)  # the curator re-opened it to re-run the workflow
    gh.label_log.clear()

    review = svc.approve(pr, CURATOR)

    assert review.status == ReviewStatus.APPROVED
    assert gh.label_log == [
        (REPO, pr, "remove", "accepted"),
        (REPO, pr, "add", "accepted"),
    ]


def test_the_mirror_comment_does_not_claim_a_merge_after_a_rejection(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    svc.reject(pr, CURATOR, "on reflection, no")

    body = gh.comments[(REPO, pr)][MIRROR_MARKER]
    assert "Approved and merged" not in body
    assert "Approved by" not in body


def test_a_published_marker_wins_over_a_later_failure_report(session_factory):
    """The repaired publish workflow announces the WPID as soon as the pushes land, then labels,
    edits the description and closes. Its failure reporter fires for any of those later steps and
    says so itself — reading only the newest marker would throw away a real publication."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.create_issue_comment(
        REPO, pr,
        f'<!-- wikipathways-publish {{"pr":{pr},"wpid":5678,"status":"published"}} -->\n'
        "Published as WP5678.",
    )
    gh.create_issue_comment(
        REPO, pr,
        f'<!-- wikipathways-publish {{"pr":{pr},"status":"failed","step":"close-pr"}} -->\n'
        "Both repositories were pushed before this failure.",
    )
    gh.closed.add(pr)

    review = svc.handle_pr_closed(pr, merged=False)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5678


# -- a hand-merged pipeline pull request ----------------------------------------------------
#
# Merging is never the success path here, but the button is right there and somebody pressed it
# on 2026-07-30 (PR #11 on marvinm2/sandbox-wp-db, while the publish workflow was mid-run — its
# own Close PR step then failed with "already merged"). That merge committed the WP0001
# placeholder to main, and because the app created rather than overwrote that path, every
# subsequent submission died on 422 `"sha" wasn't supplied` until it was deleted by hand.

PLACEHOLDER = "pathways/WP0001/WP0001.gpml"


def _merged_after_publishing(gh, pr: int, *, wpid: int | None = 5424) -> None:
    """What a hand-merge looks like from the app's side: the placeholder lands on main, the
    workflow's announcement is (usually) already there, and the PR reports itself merged."""
    if wpid is not None:
        gh.create_issue_comment(
            REPO, pr,
            f'<!-- wikipathways-publish {{"pr":{pr},"wpid":{wpid},"status":"published"}} -->\n'
            f"Published as WP{wpid}.",
        )
    gh.existing_files[(REPO, PLACEHOLDER)] = "strayblob"
    gh.merged.add(pr)


def test_a_hand_merged_submission_takes_the_placeholder_back_off_main(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    _merged_after_publishing(gh, pr)

    svc.handle_pr_closed(pr, merged=True)

    assert gh.get_file_sha(REPO, "main", PLACEHOLDER) is None
    assert [(path, branch) for _, branch, path, _ in gh.deleted] == [(PLACEHOLDER, "main")]


def test_the_repair_touches_nothing_when_the_placeholder_is_not_on_main(session_factory):
    """The ordinary case, and the second delivery of a duplicated webhook."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    _merged_after_publishing(gh, pr)
    gh.existing_files.pop((REPO, PLACEHOLDER))

    svc.handle_pr_closed(pr, merged=True)

    assert gh.deleted == []


def test_a_hand_merge_still_records_the_wpid_the_repository_published(session_factory):
    """The publication really happened; only the closing did not. Recording MERGED — a state
    this mode otherwise never reaches — would lose the id the repository assigned."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    _merged_after_publishing(gh, pr, wpid=5424)

    review = svc.handle_pr_closed(pr, merged=True)

    assert review.status == ReviewStatus.PUBLISHED
    assert review.wpid == 5424


def test_a_refused_repair_does_not_fail_the_webhook(session_factory):
    """Where the base branch is protected the delete is rejected. A working app and a log line
    beat a webhook that 500s at GitHub — submission survives the stray placeholder regardless."""
    gh = _fake(fail_on={"delete_file"})
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    _merged_after_publishing(gh, pr)

    review = svc.handle_pr_closed(pr, merged=True)

    assert review.status == ReviewStatus.PUBLISHED
    assert gh.get_file_sha(REPO, "main", PLACEHOLDER) == "strayblob"


def test_an_update_merged_by_hand_leaves_the_placeholder_alone(session_factory):
    """An update never writes the placeholder, so a stray one is not its doing and removing it
    would be this app deleting a file on main for a reason it cannot actually attribute."""
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh, kind="update", wpid=5636)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    _merged_after_publishing(gh, pr, wpid=5636)

    svc.handle_pr_closed(pr, merged=True)

    assert gh.deleted == []


def test_the_mirror_comment_warns_against_merging_while_the_pr_is_open(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)

    body = gh.comments[(REPO, pr)][MIRROR_MARKER]
    assert "Do not merge this pull request" in body
    assert "WP0001" in body


def test_the_merge_warning_is_dropped_once_the_pathway_is_published(session_factory):
    gh = _fake()
    svc = _service(session_factory, gh)
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.approve(pr, CURATOR)
    gh.simulate_3a(REPO, pr, wpid=5678)
    svc.handle_pr_closed(pr, merged=False)

    body = gh.comments[(REPO, pr)][MIRROR_MARKER]
    assert "Do not merge" not in body


def test_direct_mode_never_warns_against_merging(session_factory):
    """Merging is exactly how a direct-mode review ends — approve_and_merge does it."""
    gh = _fake()
    svc = CurationService(
        session_factory, gh, repo=REPO, curators=ConfigCurators([CURATOR]),
        publish_mode="direct",
    )
    pr = _register(svc, gh)
    _complete_checklist(svc, pr)
    svc.assign(pr, CURATOR)

    body = gh.comments[(REPO, pr)][MIRROR_MARKER]
    assert "Do not merge" not in body

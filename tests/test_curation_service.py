from __future__ import annotations

import httpx
import pytest

from app.curators import ConfigCurators
from app.github import FakeGitHubClient, GitHubError
from app.locks import PathwayLockRegistry
from app.models import ReservationStatus, Review, ReviewStatus, WpidReservation
from app.review.checklist import CURATION_CHECKLIST, is_complete
from app.review.service import (
    ChecklistIncomplete,
    CurationService,
    NotACurator,
    PreviewNotReady,
    ReviewNotFound,
)
from app.wpid import WpidAllocator
from tests.conftest import RecordingDrafts, RecordingPreviews

REPO = "wikipathways/wikipathways-database"
CURATORS = {"curator", "alice"}

REQUIRED_KEYS = [i.key for i in CURATION_CHECKLIST if i.required]


@pytest.fixture
def allocator(session_factory):
    return WpidAllocator(session_factory, floor_provider=lambda: 5636)


@pytest.fixture
def locks(session_factory):
    return PathwayLockRegistry(session_factory)


def _service(
    session_factory,
    github=None,
    allocator=None,
    locks=None,
    app_base_url="",
    previews=None,
    drafts=None,
) -> CurationService:
    return CurationService(
        session_factory,
        github,
        repo=REPO,
        curators=ConfigCurators(CURATORS),
        allocator=allocator,
        locks=locks,
        app_base_url=app_base_url,
        previews=previews,
        drafts=drafts,
    )


def _complete_required(svc: CurationService, pr_number: int) -> None:
    for key in REQUIRED_KEYS:
        svc.set_checklist_item(pr_number, key, "pass")


def test_register_is_idempotent_and_queue_lists_open(session_factory):
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")  # no duplicate
    svc.register(pr_number=2, wpid=5638, submitter="carol", kind="update")
    queue = svc.list_queue()
    assert [r.pr_number for r in queue] == [1, 2]
    assert queue[0].status == ReviewStatus.OPEN
    # Fresh review starts with the full checklist. Everything derived from the GPML is pending;
    # `one_pathway_per_pr` reads the pull request's file list, which this registration did not
    # supply, so it is `na` — nothing to check — and therefore non-blocking.
    assert len(queue[0].checklist) == len(CURATION_CHECKLIST)
    states = {i["key"]: i["state"] for i in queue[0].checklist}
    assert states.pop("one_pathway_per_pr") == "na"
    assert set(states.values()) == {"pending"}


def test_get_missing_raises(session_factory):
    with pytest.raises(ReviewNotFound):
        _service(session_factory).get(999)


def test_the_queue_pages_and_the_pages_join_up(session_factory):
    # Issue #17: the queue returned every review in a status, and the dashboard renders a full
    # card per row.
    svc = _service(session_factory)
    for pr in range(1, 8):
        svc.register(pr_number=pr, wpid=5636 + pr, submitter="bob", kind="new")

    first = svc.list_queue(limit=3)
    second = svc.list_queue(limit=3, offset=3)
    last = svc.list_queue(limit=3, offset=6)

    assert [r.pr_number for r in first] == [1, 2, 3]
    assert [r.pr_number for r in second] == [4, 5, 6]
    assert [r.pr_number for r in last] == [7]
    # Past the end is empty, not an error -- a bookmarked page after the queue shrank.
    assert svc.list_queue(limit=3, offset=99) == []


def test_paging_is_stable_when_submissions_share_a_timestamp(session_factory):
    """Two submissions in the same tick order arbitrarily on `created_at` alone, and that is
    invisible on one page: it becomes a review appearing on both pages, or on neither."""
    svc = _service(session_factory)
    for pr in range(1, 7):
        svc.register(pr_number=pr, wpid=5636 + pr, submitter="bob", kind="new")
    with session_factory() as s:
        stamp = s.get(Review, 1).created_at
        for pr in range(1, 7):
            s.get(Review, pr).created_at = stamp
        s.commit()

    seen = [r.pr_number for r in svc.list_queue(limit=2)]
    seen += [r.pr_number for r in svc.list_queue(limit=2, offset=2)]
    seen += [r.pr_number for r in svc.list_queue(limit=2, offset=4)]

    assert seen == [1, 2, 3, 4, 5, 6]


def test_an_unpaged_queue_is_still_the_whole_queue(session_factory):
    # The API route and the pipeline's own callers want the lot, and a default cap there would be
    # a silent truncation rather than a page.
    svc = _service(session_factory)
    for pr in range(1, 26):
        svc.register(pr_number=pr, wpid=5636 + pr, submitter="bob", kind="new")

    assert len(svc.list_queue()) == 25


def test_state_click_keeps_the_existing_note(session_factory):
    # The dashboard's Pass/Fail/N/A chips send no note. Treating that as an empty note wiped the
    # auto-derived explanation the curator is reading ("1 of 3 data nodes have no identifier").
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.set_checklist_item(1, "render_ok", "pending", note="1 of 3 data nodes unannotated")
    svc.set_checklist_item(1, "render_ok", "pass")  # a state click, not a note edit
    item = next(i for i in svc.get(1).checklist if i["key"] == "render_ok")
    assert item["state"] == "pass"
    assert item["note"] == "1 of 3 data nodes unannotated"
    # An explicit empty string still clears it — that is a deliberate edit.
    svc.set_checklist_item(1, "render_ok", "pass", note="")
    assert next(i for i in svc.get(1).checklist if i["key"] == "render_ok")["note"] == ""


def test_set_checklist_item_validates(session_factory):
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.set_checklist_item(1, "render_ok", "pass", note="looks good")
    review = svc.get(1)
    item = next(i for i in review.checklist if i["key"] == "render_ok")
    assert item["state"] == "pass"
    assert item["note"] == "looks good"
    with pytest.raises(ValueError):
        svc.set_checklist_item(1, "nonexistent", "pass")
    with pytest.raises(ValueError):
        svc.set_checklist_item(1, "render_ok", "not-a-state")


def test_na_on_a_required_item_does_not_wedge_approval(session_factory):
    # Issue #27. `is_complete` demands `pass` on every required item, so a required item left at
    # `na` was an approval gate nothing could open: `na` is already an answer, so waiting did
    # nothing, and a re-upload re-derived the same `na`. Marking an item N/A is a curator saying
    # it does not apply, and that has to take it off the gate.
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    for key in REQUIRED_KEYS:
        svc.set_checklist_item(1, key, "pass")

    svc.set_checklist_item(1, "references_valid", "na")
    item = next(i for i in svc.get(1).checklist if i["key"] == "references_valid")
    assert item["state"] == "na"
    assert item["required"] is False
    assert is_complete(svc.get(1).checklist) is True


def test_leaving_na_puts_the_item_back_on_the_gate(session_factory):
    # The other half of issue #27's rule, and the more dangerous one to get wrong: if `na` only
    # ever removed the requirement, a curator clicking N/A and then Fail would leave a *failed*
    # required item blocking nothing, and approval would open on it.
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    for key in REQUIRED_KEYS:
        svc.set_checklist_item(1, key, "pass")

    svc.set_checklist_item(1, "references_valid", "na")
    svc.set_checklist_item(1, "references_valid", "fail")
    item = next(i for i in svc.get(1).checklist if i["key"] == "references_valid")
    assert item["required"] is True
    assert is_complete(svc.get(1).checklist) is False


def test_na_on_an_optional_item_leaves_it_optional(session_factory):
    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.set_checklist_item(1, "ontology_tags", "na")
    svc.set_checklist_item(1, "ontology_tags", "pass")
    item = next(i for i in svc.get(1).checklist if i["key"] == "ontology_tags")
    assert item["required"] is False  # never required, in either direction


def test_concurrent_checklist_updates_all_persist(session_factory):
    # Regression for issue #15: setting distinct checklist items concurrently must not lose
    # updates. Each write is a read-modify-write of the whole JSON blob; without the row lock +
    # retry, interleaved writes overwrite each other and only the last survives.
    import threading

    svc = _service(session_factory)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")

    keys = [item.key for item in CURATION_CHECKLIST]  # every item, distinct
    barrier = threading.Barrier(len(keys))
    errors: list[Exception] = []

    def worker(key: str) -> None:
        try:
            barrier.wait()  # maximise interleaving
            svc.set_checklist_item(1, key, "pass", note=f"set-{key}")
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    review = svc.get(1)
    states = {item["key"]: item["state"] for item in review.checklist}
    notes = {item["key"]: item["note"] for item in review.checklist}
    # Every concurrently-set item survived — no lost update.
    assert all(states[k] == "pass" for k in keys), states
    assert all(notes[k] == f"set-{k}" for k in keys), notes


def test_assign_requests_pr_reviewer_on_github(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    review = svc.assign(1, "curator")
    assert review.assigned_curator == "curator"
    assert gh.review_requests.get(1) == ["curator"]  # real PR review request too


def test_assign_swallows_review_request_failure(session_factory):
    # GitHub declines (e.g. can't request review from the PR author) → app assignment still holds.
    gh = FakeGitHubClient(fail_on={"request_pr_reviewer"})
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    review = svc.assign(1, "curator")  # does not raise
    assert review.assigned_curator == "curator"
    assert gh.review_requests == {}


def test_request_changes_sets_status_and_posts_comment(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    review = svc.request_changes(1, "curator", note="Annotate the AKT1 node.")
    assert review.status == ReviewStatus.CHANGES_REQUESTED
    comments = gh.issue_comments[(REPO, 1)]
    assert len(comments) == 1
    assert "asked for changes" in comments[0]
    assert "@curator" in comments[0]
    assert "Annotate the AKT1 node." in comments[0]


def test_find_open_new_review_and_revise_rebuilds_checklist(session_factory):
    from app.preview.metadata import parse_curation_metadata

    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.request_changes(1, "curator")
    assert svc.find_open_new_review(5637).pr_number == 1

    revised = (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="X" Organism="Homo sapiens">'
        '<DataNode TextLabel="INSR"><Xref Database="Ensembl" ID="ENSG00000171105"/></DataNode>'
        "</Pathway>"
    )
    review = svc.revise(1, metadata=parse_curation_metadata(revised))
    assert review.status == ReviewStatus.OPEN  # re-opened
    dn = next(i for i in review.checklist if i["key"] == "datanodes_mapped")
    assert dn["state"] == "pass" and dn["auto"] is True  # checklist rebuilt from new content


def test_reupload_after_changes_requested_reopens_review(session_factory):
    svc = _service(session_factory)  # no github → comment step is skipped
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.request_changes(1, "curator")
    assert svc.get(1).status == ReviewStatus.CHANGES_REQUESTED
    # A re-upload re-registers the same PR → back into the review queue.
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="update")
    assert svc.get(1).status == ReviewStatus.OPEN


def _gated_service(session_factory, github):
    return CurationService(
        session_factory,
        github,
        repo=REPO,
        curators=ConfigCurators(CURATORS),
        require_preview_check=True,
        preview_workflow_file="pr-preview.yml",
        preview_artifact_name="pr-preview",
    )


def test_approve_blocked_until_preview_ready(session_factory):
    gh = FakeGitHubClient(previews={1: {"status": "pending"}})
    svc = _gated_service(session_factory, gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    _complete_required(svc, 1)

    # Checklist complete, but the PR-preview CI has not gone green → merge is refused.
    with pytest.raises(PreviewNotReady):
        svc.approve_and_merge(1, "curator")
    assert gh.merged == set()

    # Once the preview is ready, the same approval merges.
    gh.previews[1] = {"status": "ready"}
    review = svc.approve_and_merge(1, "curator")
    assert review.status == ReviewStatus.MERGED
    assert gh.merged == {1}


def test_reconcile_terminalises_out_of_band_prs(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    # Open PRs 1..3 in the fake so it knows their state; register a review for each, plus a
    # review (#4) whose PR was never opened (deleted / absent).
    for wpid in (5637, 5638, 5639):
        gh.open_pull_request(REPO, head=f"submit/WP{wpid}", base="main", title="t", body="b")
    for pr, wpid in ((1, 5637), (2, 5638), (3, 5639), (4, 5640)):
        svc.register(pr_number=pr, wpid=wpid, submitter="bob", kind="new")

    gh.merged.add(1)  # merged outside the app
    gh.closed.add(2)  # closed unmerged outside the app
    # 3 stays open; 4 is absent (no PR) → treated as closed

    assert svc.reconcile_open_reviews() == 3
    assert svc.get(1).status == ReviewStatus.MERGED
    assert svc.get(2).status == ReviewStatus.CLOSED
    assert svc.get(3).status == ReviewStatus.OPEN
    assert svc.get(4).status == ReviewStatus.CLOSED
    # Idempotent: a second pass reconciles nothing new.
    assert svc.reconcile_open_reviews() == 0


def test_approve_requires_curator(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    _complete_required(svc, 1)
    with pytest.raises(NotACurator):
        svc.approve_and_merge(1, "randomuser")
    assert gh.merged == set()  # not merged


def test_approve_requires_complete_checklist(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    # Leave one required item pending — named rather than positional, because `REQUIRED_KEYS[:-1]`
    # only blocked while the last declared required item happened to be one that blocks.
    for key in REQUIRED_KEYS:
        if key != "render_ok":
            svc.set_checklist_item(1, key, "pass")
    with pytest.raises(ChecklistIncomplete):
        svc.approve_and_merge(1, "curator")
    assert gh.merged == set()


def test_approve_merges_and_completes_lifecycle(session_factory, allocator, locks):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh, allocator=allocator, locks=locks)

    # A real submission: WPID reserved, pathway locked, review opened.
    wpid = allocator.allocate("bob")  # 5637
    locks.acquire(wpid, "bob")
    svc.register(pr_number=7, wpid=wpid, submitter="bob", kind="new")
    _complete_required(svc, 7)

    review = svc.approve_and_merge(7, "curator")

    assert review.status == ReviewStatus.MERGED
    assert review.approved_by == "curator"
    assert review.merged_at is not None
    # PR merged on GitHub.
    assert gh.merged == {7}
    # WPID reservation promoted to permanent.
    with session_factory() as s:
        assert s.get(WpidReservation, wpid).status == ReservationStatus.MERGED
    # Pathway lock released.
    assert not locks.is_locked(wpid)


def test_mirror_comment_is_best_effort(session_factory):
    # A comment failure must not sink the primary action (register / checklist / approve).
    gh = FakeGitHubClient(fail_on={"upsert_issue_comment"})
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")  # does not raise
    svc.set_checklist_item(1, "render_ok", "pass")  # does not raise
    assert svc.get(1).status == ReviewStatus.OPEN
    assert gh.comments == {}  # nothing recorded because every upsert failed


class _TransportFailingClient(FakeGitHubClient):
    """A bot client whose comment API fails at the transport layer (not a GitHubError)."""

    def upsert_issue_comment(self, repo, issue_number, body, *, marker):
        raise httpx.ConnectError("connection refused")


def test_mirror_comment_swallows_transport_errors(session_factory, allocator, locks):
    # A network blip talking to the comments API must NOT fail an action that already succeeded.
    gh = _TransportFailingClient()
    svc = _service(session_factory, github=gh, allocator=allocator, locks=locks)
    wpid = allocator.allocate("bob")
    locks.acquire(wpid, "bob")
    svc.register(pr_number=7, wpid=wpid, submitter="bob", kind="new")  # does not raise
    _complete_required(svc, 7)
    review = svc.approve_and_merge(7, "curator")  # merge succeeded; mirror failed silently
    assert review.status == ReviewStatus.MERGED
    assert gh.merged == {7}


def test_mirror_comment_written_when_bot_present(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "WP5639" in body and "generated from the curation dashboard" in body
    assert body.startswith("<!-- wikipathways-submit:mirror -->")
    # Says plainly that a bot wrote it, so GitHub-native reviewers know it is not a curator.
    assert "### Curation status for WP5639" in body
    assert "Written by the curation bot" in body
    # House style: no decorative emoji at all, and no AI-writing tells.
    for emoji in ("🤖", "🧬", "✅", "❌", "➖", "⬜"):
        assert emoji not in body
    assert "—" not in body


def test_mirror_comment_uses_the_right_article_for_each_kind(session_factory):
    # The kind was interpolated after a hard-coded "A", so every update PR on GitHub opened
    # with "A edit from @...". The article has to follow the noun.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.register(pr_number=2, wpid=5638, submitter="carol", kind="update")

    new_body = gh.comments[(REPO, 1)]["<!-- wikipathways-submit:mirror -->"]
    update_body = gh.comments[(REPO, 2)]["<!-- wikipathways-submit:mirror -->"]

    assert "A new pathway from @bob" in new_body
    assert "An edit from @carol" in update_body
    assert "A edit" not in update_body


def test_mirror_comment_links_to_the_render_when_a_public_url_is_set(session_factory):
    # CI publishes no image, so the mirror comment is the only thing that can point a
    # GitHub-native reviewer at where the before/after render actually lives.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh, app_base_url="https://curator.example.org/")
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "https://curator.example.org/dashboard/3" in body


def test_mirror_comment_omits_the_render_link_without_a_public_url(session_factory):
    # Local dev: better no link than one pointing at somebody's localhost.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "Before/after render:" not in body


def test_mirror_comment_carries_the_submitter_note(session_factory):
    # Issue #25. The note was written only into the pull request body, and a target repo that
    # generates its own body overwrites it there — silently, after the app's write succeeded.
    # The mirror comment is the app's own and is the one place on GitHub it survives.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(
        pr_number=3,
        wpid=5639,
        submitter="bob",
        kind="new",
        submitter_note="Curated from Reactome. The HGNC ids need checking.",
    )
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "What the submitter said about this change" in body
    assert "> Curated from Reactome. The HGNC ids need checking." in body


def test_a_multi_line_note_stays_inside_the_quote(session_factory):
    # Only the first line is prefixed if the note is quoted naively, so the rest escapes the
    # blockquote and reads as the bot's own words rather than the submitter's.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(
        pr_number=3, wpid=5639, submitter="bob", kind="new",
        submitter_note="First line.\n\nSecond line.",
    )
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "> First line." in body
    assert "> Second line." in body
    # The blank line between them has to stay quoted too, or the quote ends at it.
    assert "> First line.\n>\n> Second line." in body


def test_mirror_comment_says_nothing_when_there_is_no_note(session_factory):
    # The note is optional. An empty heading over an empty quote is worse than no heading.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new", submitter_note="   ")
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "What the submitter said" not in body


def test_a_blank_note_on_re_upload_keeps_the_previous_one(session_factory):
    # Blank means "nothing further to add", not "delete what I said". Treating it as an erase
    # would lose the explanation as soon as anyone re-uploaded without retyping it.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    kw = dict(pr_number=3, wpid=5639, submitter="bob", kind="new")
    svc.register(**kw, submitter_note="Why I did it.")
    svc.register(**kw, submitter_note="")
    assert svc.get(3).submitter_note == "Why I did it."
    # A real new note does replace it — it describes the file that just landed.
    svc.register(**kw, submitter_note="Fixed the ids.")
    assert svc.get(3).submitter_note == "Fixed the ids."
    assert "> Fixed the ids." in gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]


def test_revise_updates_the_note_and_the_mirror(session_factory):
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=3, wpid=5639, submitter="bob", kind="new", submitter_note="First go.")
    svc.revise(3, submitter_note="Addressed the review.")
    assert svc.get(3).submitter_note == "Addressed the review."
    body = gh.comments[(REPO, 3)]["<!-- wikipathways-submit:mirror -->"]
    assert "> Addressed the review." in body
    assert "First go." not in body


def test_the_note_outlives_the_render_cache(session_factory, tmp_path):
    # The other copy lives beside the render, which is deleted at every terminal transition
    # (issue #18) and never written at all for a GPML the renderer refuses. Rejecting a
    # submission must not destroy the record of why it was made.
    from app.preview import PreviewService

    previews = PreviewService(cache_dir=tmp_path / "cache")
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh, previews=previews)
    svc.register(
        pr_number=3, wpid=5639, submitter="bob", kind="new", submitter_note="Why I did it.",
    )
    svc.reject(3, "curator", note="not ready")
    assert svc.get(3).status == ReviewStatus.REJECTED
    assert svc.get(3).submitter_note == "Why I did it."


def test_approve_does_not_mutate_state_if_merge_fails(session_factory, allocator, locks):
    gh = FakeGitHubClient(fail_on={"merge_pull_request"})
    svc = _service(session_factory, github=gh, allocator=allocator, locks=locks)
    wpid = allocator.allocate("bob")
    locks.acquire(wpid, "bob")
    svc.register(pr_number=7, wpid=wpid, submitter="bob", kind="new")
    _complete_required(svc, 7)

    with pytest.raises(GitHubError):
        svc.approve_and_merge(7, "curator")

    # Merge failed → review still OPEN, lock still held, reservation still just RESERVED.
    assert svc.get(7).status == ReviewStatus.OPEN
    assert locks.is_locked(wpid)
    with session_factory() as s:
        assert s.get(WpidReservation, wpid).status == ReservationStatus.RESERVED


def test_rejecting_frees_the_cached_render(session_factory):
    # Issue #18. Reject is the case worth pinning: it is the one terminal transition that does
    # not post a mirror comment, so hanging the cleanup off the mirror would have leaked exactly
    # the state a curator reaches most often.
    gh = FakeGitHubClient()
    previews = RecordingPreviews()
    svc = _service(session_factory, github=gh, previews=previews)
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")
    assert previews.discarded == []

    svc.reject(3, "curator", note="not a pathway")
    assert previews.discarded == [3]


def test_rejecting_by_label_on_github_frees_the_cached_render(session_factory):
    # Same terminal state by a different door. Curators reach for the repository's own labels as
    # readily as for the dashboard -- that is why the app mirrors them at all -- so a cleanup
    # wired only to the dashboard button misses however many rejections happen on GitHub.
    gh = FakeGitHubClient()
    previews = RecordingPreviews()
    svc = _service(session_factory, github=gh, previews=previews)
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")

    svc.handle_label_event(3, "rejected", added=True, actor="curator")

    assert svc.get(3).status == ReviewStatus.REJECTED
    assert previews.discarded == [3]


def test_reconcile_sweeps_with_every_live_review_and_no_terminal_one(session_factory):
    # The sweep's contract is that anything absent from `keep` is collectable, so passing a
    # partial set would delete renders still in use. Pin that it is the whole live queue.
    gh = FakeGitHubClient()
    previews = RecordingPreviews()
    svc = _service(session_factory, github=gh, previews=previews)
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")
    svc.register(pr_number=4, wpid=5638, submitter="carol", kind="new")
    svc.reject(4, "curator", note="no")

    svc.reconcile()

    assert previews.swept == [{3}]


def test_reconcile_sweeps_the_drafts_cache_too(session_factory):
    # The drafts cache sits in the same directory and has the same defect, but no per-transition
    # path to back up -- it expires by TTL and keeps the file, so this is the only thing that
    # removes one.
    gh = FakeGitHubClient()
    drafts = RecordingDrafts()
    svc = _service(session_factory, github=gh, drafts=drafts)
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")

    svc.reconcile()

    assert drafts.sweeps == 1


def test_one_cache_failing_to_sweep_does_not_stop_the_other(session_factory):
    class _Broken:
        def sweep(self, *a, **kw):
            raise OSError("read-only file system")

    drafts = RecordingDrafts()
    svc = _service(
        session_factory, github=FakeGitHubClient(), previews=_Broken(), drafts=drafts
    )
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")

    svc.reconcile()

    assert drafts.sweeps == 1


def test_a_service_without_a_preview_cache_still_works(session_factory):
    # previews is optional; every test that does not care about renders should not have to
    # build one, and a deployment without a cache must not fail its terminal transitions.
    gh = FakeGitHubClient()
    svc = _service(session_factory, github=gh)
    svc.register(pr_number=4, wpid=5638, submitter="bob", kind="new")
    svc.reject(4, "curator", note="no")
    assert svc.get(4).status.value == "rejected"
    svc.reconcile()  # ...nor its dashboard loads

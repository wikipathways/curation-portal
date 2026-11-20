"""A published (or merged) review keeps its render; a rejected or closed one does not.

2026-11-20, after PR #91 published as WP5430: "the render removes after approving". The cache used
to be freed at every terminal transition (issue #18). The diagram now stays on the card and the
review page of an approved, published, publish-failed or merged review, and the hourly sweep bounds
the disk by removing a kept render ``preview_retention_days`` after the review finished.
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app.curators import ConfigCurators
from app.github import FakeGitHubClient
from app.models import Review, ReviewStatus, utcnow
from app.preview import PreviewService
from app.preview.backfill import refresh_stale_renders
from app.review.checklist import ChecklistState
from app.review.service import CurationService
from tests.conftest import RecordingPreviews
from tests.test_api import _login
from tests.test_pipeline_api import REPO, _pass_every_required_item, _pipeline_app, _submit
from tests.test_preview_cache_v2 import NEW, _add
from tests.test_preview_cache_v2 import _session_factory as _sf

CURATOR = "curator"


def _service(session_factory, gh, **kw) -> CurationService:
    return CurationService(
        session_factory,
        gh,
        repo=REPO,
        curators=ConfigCurators([CURATOR]),
        **kw,
    )


def _complete(svc, pr):
    for item in svc.get(pr).checklist:
        if item["required"]:
            svc.set_checklist_item(pr, item["key"], ChecklistState.PASS.value)


def _finish(session_factory, pr, status, *, days_ago):
    with session_factory() as s:
        review = s.get(Review, pr)
        review.status = status
        stamp = utcnow() - timedelta(days=days_ago)
        if status == ReviewStatus.PUBLISHED:
            review.published_at = stamp
        elif status == ReviewStatus.MERGED:
            review.merged_at = stamp
        s.commit()


# -- the transitions -------------------------------------------------------------------------


def test_merging_keeps_the_render(session_factory):
    gh = FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})
    previews = RecordingPreviews()
    svc = _service(session_factory, gh, previews=previews)
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")
    _complete(svc, 3)

    svc.approve_and_merge(3, CURATOR)

    assert svc.get(3).status == ReviewStatus.MERGED
    assert previews.discarded == []


def test_closing_without_merging_still_frees_the_render(session_factory):
    previews = RecordingPreviews()
    svc = _service(session_factory, FakeGitHubClient(), previews=previews)
    svc.register(pr_number=3, wpid=5637, submitter="bob", kind="new")

    svc.handle_pr_closed(3, merged=False)

    assert svc.get(3).status == ReviewStatus.CLOSED
    assert previews.discarded == [3]


def test_recording_a_wpid_by_hand_keeps_the_render(session_factory):
    gh = FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})
    previews = RecordingPreviews()
    svc = _service(session_factory, gh, previews=previews, publish_mode="pipeline")
    pr = gh.open_pull_request(REPO, head="b", base="main", title="t", body="b").number
    svc.register(pr_number=pr, wpid=None, submitter="bob", kind="new", head_branch="b")
    _complete(svc, pr)
    svc.approve(pr, CURATOR)

    svc.record_published_wpid(pr, 5430, CURATOR)

    assert svc.get(pr).status == ReviewStatus.PUBLISHED
    assert previews.discarded == []


# -- the sweep -------------------------------------------------------------------------------


def test_the_sweep_keeps_finished_renders_for_the_retention_and_no_longer(session_factory):
    previews = RecordingPreviews()
    svc = _service(
        session_factory, FakeGitHubClient(), previews=previews,
        preview_retention=timedelta(days=90),
    )
    for pr in range(1, 7):
        svc.register(pr_number=pr, wpid=5636 + pr, submitter="bob", kind="new")
    _finish(session_factory, 2, ReviewStatus.PUBLISHED, days_ago=10)
    _finish(session_factory, 3, ReviewStatus.PUBLISHED, days_ago=100)
    _finish(session_factory, 4, ReviewStatus.MERGED, days_ago=89)
    _finish(session_factory, 5, ReviewStatus.REJECTED, days_ago=0)
    _finish(session_factory, 6, ReviewStatus.APPROVED, days_ago=400)  # live: always kept

    svc.reconcile()

    assert previews.swept == [{1, 2, 4, 6}]


def test_a_retention_of_zero_lets_the_next_sweep_take_them(session_factory):
    previews = RecordingPreviews()
    svc = _service(
        session_factory, FakeGitHubClient(), previews=previews, preview_retention=timedelta(0)
    )
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    _finish(session_factory, 1, ReviewStatus.PUBLISHED, days_ago=0)

    svc.reconcile()

    assert previews.swept == [set()]


def test_the_drafts_cache_is_still_swept_with_the_live_set_only(session_factory):
    from tests.conftest import RecordingDrafts

    drafts = RecordingDrafts()
    svc = _service(
        session_factory, FakeGitHubClient(), previews=RecordingPreviews(), drafts=drafts
    )
    svc.register(pr_number=1, wpid=5637, submitter="bob", kind="new")
    svc.register(pr_number=2, wpid=5638, submitter="bob", kind="new")
    _finish(session_factory, 2, ReviewStatus.PUBLISHED, days_ago=1)

    svc.reconcile()

    assert drafts.kept == [{1}]


def test_the_real_cache_survives_the_retention_and_then_goes(tmp_path):
    sf = _sf(tmp_path)
    cache = tmp_path / "cache"
    previews = PreviewService(cache_dir=cache, sweep_min_age_seconds=0)
    previews.render_local(9, 5430, after_gpml=NEW)
    svc = _service(sf, FakeGitHubClient(), previews=previews)
    svc.register(pr_number=9, wpid=None, submitter="bob", kind="new")
    _finish(sf, 9, ReviewStatus.PUBLISHED, days_ago=30)

    svc.reconcile()
    assert (cache / "9").is_dir()

    _finish(sf, 9, ReviewStatus.PUBLISHED, days_ago=91)
    later = _service(
        sf, FakeGitHubClient(),
        previews=PreviewService(cache_dir=cache, sweep_min_age_seconds=0),
    )
    later.reconcile()
    assert not (cache / "9").exists()


def test_the_retention_setting_reaches_the_service(tmp_path):
    from tests.test_api import _settings

    assert _settings().preview_retention_days == 90
    assert _settings(preview_retention_days=7).preview_retention_days == 7


# -- the backfill ----------------------------------------------------------------------------


def test_the_backfill_does_not_redraw_a_kept_render(tmp_path):
    """A kept render is a finished review's picture, not a live one to bring up to date."""
    sf = _sf(tmp_path)
    previews = PreviewService(cache_dir=tmp_path / "cache")
    _add(sf, pr_number=4, wpid=5429, head_sha="h4", status=ReviewStatus.PUBLISHED,
         pathway_paths=["pathways/WP5429/WP5429.gpml"])
    previews.render_local(4, 5429, after_gpml=NEW, head_sha="h4")
    stamp = previews.stamp(4)
    stamp["renderer"] = 1
    (tmp_path / "cache" / "4" / "render.json").write_text(json.dumps(stamp))
    assert previews.needs_rerender(4)

    done = refresh_stale_renders(
        previews, sf, FakeGitHubClient(), content_repo="o/r", default_branch="main"
    )

    assert done == 0
    assert previews.stamp(4)["renderer"] == 1


# -- the card --------------------------------------------------------------------------------


def test_a_published_review_still_shows_its_diagram(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)
        assert client.post(f"/api/reviews/{pr}/approve").status_code == 200
        fake.simulate_3a(REPO, pr, wpid=5430)
        app.state.worker.tick(github=fake)

        page = client.get(f"/dashboard/{pr}").text
        queue = client.get("/dashboard?status=published").text
        svg = client.get(f"/previews/{pr}/after.svg")

    with app.state.session_factory() as s:
        assert s.get(Review, pr).status == ReviewStatus.PUBLISHED
    assert f"/previews/{pr}/after.svg" in page
    assert f"/previews/{pr}/after.svg" in queue
    assert "No render on file" not in page
    assert svg.status_code == 200

"""The "Record the published WPID" form is a fallback, not a step.

The WPID arrives by itself from the repository's announcement a minute or so after the publish
run, so in pipeline mode the form is offered only once that has visibly failed: the review is
``publish_failed``, or the publish workflow completed and ten minutes later no WPID has arrived.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.models import Review
from app.review.tracker import WPID_FALLBACK_AFTER_SECONDS, wpid_fallback_due
from tests.test_api import _login
from tests.test_pipeline_api import _pass_every_required_item, _pipeline_app, _submit

APPROVED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
RUN_AT = "2026-11-20T12:01:00Z"
RUN_START = datetime(2026, 11, 20, 12, 1, tzinfo=UTC)


def _review(**kw):
    base = {"status": "approved", "approved_at": APPROVED_AT, "wpid": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _run(status="completed", conclusion="success", created_at=RUN_AT, updated_at=None):
    publish = {"status": status, "conclusion": conclusion, "url": "u", "created_at": created_at}
    if updated_at is not None:
        publish["updated_at"] = updated_at
    return {"publish": publish}


def test_not_offered_while_the_automatic_path_is_still_working():
    later = RUN_START + timedelta(hours=1)
    # No publish run seen yet.
    assert not wpid_fallback_due(_review(), {}, later)
    # A run still going.
    assert not wpid_fallback_due(_review(), _run("in_progress", None), later)
    # A completed run, but the announcement has not had its ten minutes.
    just_before = RUN_START + timedelta(seconds=WPID_FALLBACK_AFTER_SECONDS - 1)
    assert not wpid_fallback_due(_review(), _run(), just_before)


def test_offered_ten_minutes_after_a_completed_run_with_no_wpid():
    due = RUN_START + timedelta(seconds=WPID_FALLBACK_AFTER_SECONDS)
    assert wpid_fallback_due(_review(), _run(), due)
    # A failed run counts as completed too: nothing more is coming from it.
    assert wpid_fallback_due(_review(), _run(conclusion="failure"), due)


def test_the_ten_minutes_count_from_when_a_long_run_finished():
    # A run that sat queued and ran long: created at 12:00:30, finished at 12:10:20. Counted from
    # its start, the form appeared 20 seconds after it finished, before its announcement had any
    # time to arrive, which is the normal flow the form is meant to stay out of.
    approved = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 11, 20, 12, 10, 20, tzinfo=UTC)
    run = _run(created_at="2026-11-20T12:00:30Z", updated_at="2026-11-20T12:10:20Z")
    review = _review(approved_at=approved)
    assert not wpid_fallback_due(review, run, finished + timedelta(seconds=20))
    assert not wpid_fallback_due(
        review, run, finished + timedelta(seconds=WPID_FALLBACK_AFTER_SECONDS - 1)
    )
    assert wpid_fallback_due(review, run, finished + timedelta(seconds=WPID_FALLBACK_AFTER_SECONDS))


def test_a_run_recorded_without_updated_at_counts_from_its_start():
    # Rows written before the finish time was recorded keep the older rule.
    due = RUN_START + timedelta(seconds=WPID_FALLBACK_AFTER_SECONDS)
    assert wpid_fallback_due(_review(), _run(), due)


def test_the_background_loop_records_when_the_publish_run_finished(tmp_path):
    # The finish time comes from GitHub's run (updated_at) by way of the loop, so the page that
    # decides on the form makes no GitHub call. The fake models a run that finished well after it
    # was created, as a queued run does.
    from tests.test_pipeline_api import REPO

    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        # A deployment that waits longer than ten minutes before calling a publication failed, so
        # the review is still ``approved`` half an hour on and only the form's clock decides.
        app.state.settings.publish_timeout_minutes = 120
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _approve(client, app, pr)
        approved_at = datetime.now(UTC) - timedelta(minutes=30)
        with app.state.session_factory() as s:
            review = s.get(Review, pr)
            review.approved_at = approved_at
            s.commit()
        started = (approved_at + timedelta(minutes=1)).isoformat()
        finished = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        fake.record_workflow_run(
            REPO, "3a_approved_pull_request.yml", conclusion="success",
            created_at=started, updated_at=finished,
        )
        app.state.worker.tick(github=fake)
        with app.state.session_factory() as s:
            publish = (s.get(Review, pr).pipeline_run or {}).get("publish") or {}
            status = s.get(Review, pr).status.value
        page = client.get(f"/dashboard/{pr}").text
    assert status == "approved"
    assert publish.get("updated_at") == finished
    # Started 29 minutes ago but finished a minute ago: not overdue yet.
    assert "Record the published WPID" not in page


def test_a_run_from_before_the_approval_does_not_count():
    later = APPROVED_AT + timedelta(hours=1)
    assert not wpid_fallback_due(_review(), _run(created_at="2026-11-20T11:00:00Z"), later)


def test_offered_on_a_failed_publication_and_never_once_settled():
    assert wpid_fallback_due(_review(status="publish_failed"), {}, APPROVED_AT)
    for status in ("open", "published", "rejected", "closed"):
        assert not wpid_fallback_due(_review(status=status), _run(), APPROVED_AT + timedelta(1))


def _approve(client, app, pr):
    _login(client, "marvinm2")
    _pass_every_required_item(client, pr)
    assert client.post(f"/api/reviews/{pr}/approve").status_code == 200


def _set_run(app, pr, run):
    with app.state.session_factory() as s:
        review = s.get(Review, pr)
        review.pipeline_run = {**(review.pipeline_run or {}), **run}
        s.commit()


def test_an_approved_card_does_not_ask_for_the_wpid(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _approve(client, app, pr)
        recent = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        _set_run(app, pr, _run(created_at=recent))
        page = client.get(f"/dashboard/{pr}").text
        queue = client.get("/dashboard?status=approved").text
    assert "Record the published WPID" not in page
    assert "published-wpid" not in queue


def test_the_form_appears_once_the_announcement_is_overdue(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _approve(client, app, pr)
        with app.state.session_factory() as s:
            review = s.get(Review, pr)
            review.approved_at = datetime.now(UTC) - timedelta(minutes=30)
            s.commit()
        stale = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        _set_run(app, pr, _run(created_at=stale))
        page = client.get(f"/dashboard/{pr}").text
        # The route itself works as it always did.
        recorded = client.post(f"/api/reviews/{pr}/published-wpid", data={"wpid": "5430"})
    assert "Record the published WPID" in page
    assert "has not" in page
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["status"] == "published"


def test_the_route_still_accepts_a_wpid_while_the_form_is_hidden(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _approve(client, app, pr)
        recorded = client.post(f"/api/reviews/{pr}/published-wpid", data={"wpid": "5430"})
    assert recorded.status_code == 200, recorded.text


def test_the_http_client_reads_a_runs_updated_at():
    from app.github.client import _workflow_run

    run = _workflow_run({
        "id": 1, "html_url": "u", "status": "completed", "conclusion": "success",
        "created_at": "2026-11-20T12:00:30Z", "updated_at": "2026-11-20T12:10:20Z",
    })
    assert run.updated_at == "2026-11-20T12:10:20Z"

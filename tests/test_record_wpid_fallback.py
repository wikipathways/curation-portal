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


def _run(status="completed", conclusion="success", created_at=RUN_AT):
    return {"publish": {"status": status, "conclusion": conclusion, "url": "u",
                        "created_at": created_at}}


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

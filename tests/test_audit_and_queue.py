"""Wave 2: the audit trail, and queue cards that say which pathway they are about."""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.models import Review
from app.review.audit import history
from tests.test_api import GOOD_GPML, _authed_app, _login, _signed


def _submit(c) -> int:
    resp = c.post(
        "/api/submit",
        files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["pr_number"]


def _events(app, pr):
    return [(e.actor, e.kind, e.detail) for e in reversed(history(app.state.session_factory, pr))]


def test_every_decision_is_attributed(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/assign", data={"curator": "curator"})
        r = c.post(f"/api/reviews/{pr}/checklist", data={"key": "naming_ok", "state": "fail"})
        assert r.status_code == 200, r.text
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "Add a PMID."})

        events = _events(app, pr)
        assert events[0][:2] == ("bob", "opened")
        assert ("curator", "assigned", {"from": None, "to": "curator"}) in events
        tick = next(e for e in events if e[1] == "checklist")
        assert tick[0] == "curator" and tick[2]["key"] == "naming_ok" and tick[2]["to"] == "fail"
        change = next(e for e in events if e[1] == "status")
        assert change[0] == "curator"
        assert change[2]["to"] == "changes_requested"
        assert "Add a PMID." in change[2]["note"]


def test_a_close_seen_from_github_is_attributed_to_github(tmp_path):
    app, current = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        body, sig = _signed(
            "whsec",
            {"action": "closed", "number": pr, "pull_request": {"number": pr, "merged": False}},
        )
        c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        status = [e for e in _events(app, pr) if e[1] == "status"]
        assert status[-1][0] == "github"
        assert status[-1][2]["to"] == "closed"


def test_derived_checklist_answers_are_not_in_the_trail(tmp_path):
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        svc = app.state.worker._make_curation(None)
        svc.set_checklist_item(pr, "naming_ok", "pass", auto=True)
        assert not [e for e in _events(app, pr) if e[1] == "checklist"]


def test_the_review_page_shows_the_history(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "Add a PMID."})
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        assert "History" in page
        assert "@curator</strong> moved it from" in page
        assert "Add a PMID." in page


def test_unassigning_works_and_only_curators_can_be_assigned(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator", "other"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/assign", data={"curator": "other"}).status_code == 200
        r = c.post(f"/api/reviews/{pr}/assign", data={"curator": ""})
        assert r.status_code == 200, r.text
        assert r.json()["assigned_curator"] is None
        assert c.post(f"/api/reviews/{pr}/assign", data={"curator": "bob"}).status_code == 422


def test_the_card_names_the_pathway_and_its_organism(tmp_path):
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        with app.state.session_factory() as s:
            row = s.get(Review, pr)
        assert row.title and row.organism
        _login(c, "bob")
        page = c.get("/dashboard").text
        assert f'<span class="review-card__name">{row.title}</span>' in page
        assert row.organism in page
        assert "Submitted by <strong>@bob</strong> today" in page


def test_assigned_to_me_lists_only_my_live_reviews(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator", "other"])
    with TestClient(app) as c:
        current["user"] = "bob"
        mine, theirs = _submit(c), _submit(c)
        current["user"] = "curator"
        c.post(f"/api/reviews/{mine}/assign", data={"curator": "curator"})
        c.post(f"/api/reviews/{theirs}/assign", data={"curator": "other"})
        c.post(f"/api/reviews/{mine}/request-changes", data={"note": "x"})

        _login(c, "curator")
        page = c.get("/dashboard?assigned=1").text
        assert "Assigned to you" in page
        assert f'data-pr="{mine}"' in page  # still live, though not "open"
        assert f'data-pr="{theirs}"' not in page

        _login(c, "bob")  # not a curator: no tab, and the filter does nothing
        page = c.get("/dashboard?assigned=1").text
        assert "Assigned to me" not in page


def test_every_fresh_session_factory_records_events(tmp_path):
    """The listener was skipped for a factory reusing a collected one's id (flaky, 2026-11-12)."""
    import gc

    from sqlalchemy import func, select

    from app.db import make_engine, make_session_factory
    from app.models import Base, ReviewEvent

    silent = 0
    for i in range(150):
        engine = make_engine(f"sqlite:///{tmp_path / f'r{i}.db'}")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        with factory() as s:
            s.add(Review(pr_number=1, submitter="bob", kind="new"))
            s.commit()
            silent += s.execute(select(func.count()).select_from(ReviewEvent)).scalar_one() == 0
        del factory
        engine.dispose()
        if i % 10 == 0:
            gc.collect()
    assert silent == 0

"""Once a review is on its way to the site, the card is its header and the publish tracker.

2026-11-20: "I like the progress steps now, but can we then minimise the panel of all the rest of
that review?" Everything else (checks, diagram, data nodes, checklist, actions) folds into one
closed "Review details", on the queue and on the review page. refresh.js keeps it open across a
swap once opened; viewer.js mounts the diagram when it opens.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.models import Review
from tests.test_api import _login
from tests.test_pipeline_api import REPO, _pass_every_required_item, _pipeline_app, _submit

FOLD_OPEN_TAG = '<details class="review-card__more" data-details-key="review-details">'


def _split(page: str) -> tuple[str, str]:
    """The card's visible part, and what is inside the fold."""
    head, _, rest = page.partition(FOLD_OPEN_TAG)
    inside = rest.split("</details>\n</article>")[0] if rest else ""
    return head, inside


def _approved(client, app):
    pr = _submit(client)["pr_number"]
    _login(client, "marvinm2")
    _pass_every_required_item(client, pr)
    assert client.post(f"/api/reviews/{pr}/approve").status_code == 200
    return pr


def test_an_open_review_is_not_folded(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text
    assert "review-card__more" not in page
    assert 'data-tour="checklist"' in page


def test_an_approved_review_shows_the_tracker_and_folds_the_rest(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _approved(client, app)
        page = client.get(f"/dashboard/{pr}").text
        queue = client.get("/dashboard?status=approved").text
        card = client.get(f"/dashboard/{pr}/card?expanded=0").text

    for html in (page, queue, card):
        assert FOLD_OPEN_TAG in html  # closed: no `open` attribute
        visible, inside = _split(html)
        assert 'data-tour="tracker"' in visible
        assert "review-card__head" in visible
        assert "Review details</summary>" in inside or "Review details</summary>" in html
        for what in ('data-tour="checklist"', "data-viewer-pair", 'class="quality'):
            assert what in inside, what
            assert what not in visible, what
    # The fold carries the tour anchor its step points at.
    assert 'data-tour="review-details"' in page
    # Exactly one tracker, outside the fold.
    assert page.count('data-tour="tracker"') == 1


def test_the_record_wpid_form_stays_beside_the_tracker(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _approved(client, app)
        with app.state.session_factory() as s:
            review = s.get(Review, pr)
            review.status = "publish_failed"
            review.approved_at = datetime.now(UTC) - timedelta(minutes=30)
            s.commit()
        page = client.get(f"/dashboard/{pr}").text
    visible, inside = _split(page)
    assert "Record the published WPID" in visible
    assert "Record the published WPID" not in inside
    assert page.count('class="published-wpid"') == 1


def test_a_published_review_is_folded_too(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _approved(client, app)
        fake.simulate_3a(REPO, pr, wpid=5430)
        app.state.worker.tick(github=fake)
        page = client.get(f"/dashboard/{pr}").text
    visible, inside = _split(page)
    assert "WP5430" in visible
    assert "data-viewer-pair" in inside


def test_the_fold_adds_no_inline_script_or_handler(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _approved(client, app)
        page = client.get(f"/dashboard/{pr}").text
    fold = page[page.index(FOLD_OPEN_TAG) - 200: page.index(FOLD_OPEN_TAG) + 300]
    assert not re.search(r"\son[a-z]+=", fold)
    assert "<script" not in fold

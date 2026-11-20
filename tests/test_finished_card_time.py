"""A finished review's card says when it finished, not when it was last checked.

Sandbox WP5430 (2026-11-20): a published review's card kept saying "Checked 3 min ago", and
Refresh did not move it, because a finished review is never re-checked. It now reads "Published
4 min ago" from the publication time, as a ``<time data-ago>`` that refresh.js keeps current and
the card's ETag leaves out.
"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import Review, ReviewStatus, utcnow
from tests.test_api import _login
from tests.test_pipeline_api import _pipeline_app, _submit


def _finish(app, pr, status, **fields):
    with app.state.session_factory() as s:
        r = s.get(Review, pr)
        r.status = status
        r.last_checked_at = utcnow() - timedelta(minutes=3)
        for k, v in fields.items():
            setattr(r, k, v)
        s.commit()


def test_a_published_card_says_when_it_was_published(tmp_path):
    app = _pipeline_app(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = None
        pr = _submit(c)["pr_number"]
        published = utcnow() - timedelta(minutes=4)
        _finish(app, pr, ReviewStatus.PUBLISHED, wpid=5430, published_at=published)
        _login(c, "marvinm2")

        first = c.get(f"/dashboard/{pr}/card")
        assert first.status_code == 200
        head = first.text.split('class="review-card__fresh"', 1)[1].split("</div>", 1)[0]
        assert "Checked" not in head
        m = re.search(
            r'<span class="review-card__outcome">Published <time datetime="([^"]+)" data-ago>'
            r"4 min ago</time></span>",
            head,
        )
        assert m, head
        assert m.group(1).startswith(published.replace(microsecond=0).isoformat()[:19])
        assert m.group(1).endswith("+00:00")  # an aware time, so the browser reads it as UTC
        # No "Checked" span, so the 304 path has nothing to overwrite.
        assert first.headers["x-checked"] == ""

        # Refresh (a curator's re-check request) still answers, and the unchanged card is a 304:
        # the minutes passing are not a change to it.
        again = c.get(
            f"/dashboard/{pr}/card?recheck=1", headers={"If-None-Match": first.headers["etag"]}
        )
        assert again.status_code == 304


def test_a_rejected_card_says_when_it_was_rejected(tmp_path):
    app = _pipeline_app(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = None
        pr = _submit(c)["pr_number"]
        _finish(app, pr, ReviewStatus.REJECTED)
        _login(c, "marvinm2")
        card = c.get(f"/dashboard/{pr}/card").text
    assert re.search(
        r'<span class="review-card__outcome">Rejected <time datetime="[^"]+" data-ago>just now'
        r"</time></span>",
        card,
    )


def test_a_live_card_still_says_when_it_was_checked(tmp_path):
    app = _pipeline_app(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = None
        pr = _submit(c)["pr_number"]
        _finish(app, pr, ReviewStatus.OPEN)
        _login(c, "marvinm2")
        r = c.get(f"/dashboard/{pr}/card")
    assert "review-card__outcome" not in r.text
    assert r.headers["x-checked"] == "Checked 3 min ago"

"""The checklist starts unanswered, with the automated findings shown beside it as hints.

Seen live on PR #91 (2026-11-20): `ontology_tags` sat at N/A on a pathway with two tags. Every
item now starts pending and only a curator answers; what the portal and the repository's pipeline
found is shown under the item ("Automated check: 2 ontology tags. Suggests Pass.").
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.review.service import MIRROR_MARKER
from tests.test_api import _authed_app, _login

TAGGED_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260520113005">'
    b'<OntologyTerm Term="mitophagy pathway" ID="PW:0000001" Ontology="Pathway Ontology"/>'
    b'<OntologyTerm Term="liver" ID="UBERON:0002107" Ontology="Uberon"/>'
    b"</Pathway>"
)


def _submit(c, gpml: bytes = TAGGED_GPML) -> int:
    resp = c.post(
        "/api/submit",
        files={"file": ("u.gpml", io.BytesIO(gpml), "application/xml")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["pr_number"]


def test_a_submission_starts_with_every_item_unanswered(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        detail = c.get(f"/api/reviews/{pr}").json()
        assert {i["state"] for i in detail["checklist"]} == {"pending"}
        onto = next(i for i in detail["checklist"] if i["key"] == "ontology_tags")
        assert (onto["suggested"], onto["hint"]) == ("pass", "2 ontology tags.")


def test_the_card_shows_the_hint_and_leaves_the_pill_unanswered(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        current["user"] = "curator"
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        assert "Automated check: 2 ontology tags. Suggests Pass." in page
        assert 'class="checklist__hint checklist__hint--pass"' in page
        # No item is answered for the curator, so no pill says anything but "Not checked".
        assert 'data-state="pass">' not in page.split('class="checklist"')[1].split("</ul>")[0]
        # And Approve names every required item as still to do.
        assert "Still to mark Pass: Rendered pathway displays correctly" in page


def test_the_mirror_comment_carries_the_hint_beside_the_unanswered_state(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        fake = app.state._fake
        body = next(
            comments[MIRROR_MARKER]
            for (_repo, n), comments in fake.comments.items()
            if n == pr and MIRROR_MARKER in comments
        )
        row = next(line for line in body.splitlines() if line.startswith("| Ontology tags"))
        assert "| PENDING |" in row
        assert "_Automated check: 2 ontology tags. Suggests Pass._" in row


def test_approval_needs_the_curators_answers(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 409
        checklist = c.get(f"/api/reviews/{pr}").json()["checklist"]
        for item in checklist:
            # The curator follows the hints; an N/A they choose takes the item off the gate.
            state = "na" if item.get("suggested") == "na" else "pass"
            r = c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": state})
            assert r.status_code == 200, r.text
        app.state._fake.previews[pr] = {"status": "ready"}  # direct mode's CI gate
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 200

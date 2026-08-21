from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import GithubOAuth
from app.config import Settings
from app.github import (
    CredentialsRejected,
    FakeGitHubClient,
    GitHubError,
    HttpGitHubClient,
)
from app.main import (
    build_app,
    get_bot_client,
    get_bot_optional,
    get_current_user,
    get_github_client,
)
from app.review.service import MIRROR_MARKER, WELCOME_MARKER

GOOD_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260520113005"></Pathway>'
)
BAD_GPML = b"<html>not a pathway</html>"
REV_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r19990101000000"></Pathway>'
)


def _settings(**kw):
    # _env_file=None keeps tests hermetic from the developer's local .env.
    kw.setdefault("dev_wpid_floor", 5636)
    return Settings(_env_file=None, **kw)


@pytest.fixture
def client(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    with TestClient(build_app(settings)) as c:
        yield c


def _authed_app(tmp_path, *, curators=(), fake=None, webhook_secret=None):
    """Build an app with GitHub + identity dependencies overridden.

    Returns (app, current) where ``current`` is a mutable dict; set ``current['user']`` to change
    who the session identity resolves to between requests.
    """
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        curators=list(curators),
        github_webhook_secret=webhook_secret,
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    fake = fake or FakeGitHubClient(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    current = {"user": "alice"}
    app.dependency_overrides[get_github_client] = lambda: fake
    # The same fake stands in for the bot (App) identity — merge + mirror comment run through it,
    # so ``fake.merged`` / ``fake.comments`` capture the privileged actions.
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.state._fake = fake  # for assertions
    return app, current


# -- read-only endpoints (no auth) ---------------------------------------------------------


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_validate_good_gpml(client):
    resp = client.post(
        "/api/validate",
        files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["organism"] == "Homo sapiens"
    assert body["embedded_wpid"] == "WP5636"


def test_validate_rejects_bad_gpml(client):
    resp = client.post(
        "/api/validate",
        files={"file": ("upload.gpml", io.BytesIO(BAD_GPML), "application/xml")},
    )
    assert resp.status_code == 422


# -- auth gating ---------------------------------------------------------------------------


def test_submit_requires_auth(client):
    # No session → 401 (not 503): the app is configured, the caller just isn't logged in.
    resp = client.post(
        "/api/submit",
        files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )
    assert resp.status_code == 401


def test_auth_me_anonymous(client):
    assert client.get("/auth/me").json() == {
        "authenticated": False,
        "login": None,
        "is_curator": False,
    }


# -- submit / update -----------------------------------------------------------------------


def test_submit_success(tmp_path):
    app, _current = _authed_app(tmp_path)
    with TestClient(app) as c:
        resp = c.post(
            "/api/submit",
            files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
            data={"description": "Curated from Reactome; please check the HGNC ids."},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["wpid"] == "WP5637"
    assert body["path"] == "pathways/WP5637/WP5637.gpml"
    # The submitter note travels through the Form field into the PR body.
    pr_body = app.state._fake.pull_meta[body["pr_number"]]["body"]
    assert "**Note from the submitter**" in pr_body
    assert "Curated from Reactome" in pr_body


def test_update_success_lock_and_release(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
    )
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "alice"
        r1 = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        )
        assert r1.status_code == 201

        # A different user is now blocked (lock held by alice) → 409.
        current["user"] = "bob"
        r2 = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        )
        assert r2.status_code == 409
        assert r2.json()["detail"]["held_by"] == "alice"

        # Non-curator cannot force-release (403); curator can.
        current["user"] = "bob"
        assert c.post("/api/pathways/5636/release").status_code == 403
        current["user"] = "curator"
        assert c.post("/api/pathways/5636/release").json() == {"released": True}


# -- curation dashboard --------------------------------------------------------------------


def test_pathway_info_reports_presence(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "base"},
        existing_contents={f"{repo}#pathways/WP5636/WP5636.gpml": GOOD_GPML.decode()},
    )
    app, _current = _authed_app(tmp_path, fake=fake)
    with TestClient(app) as c:
        found = c.get("/api/pathways/5636").json()
        assert found["exists"] is True and found["wpid"] == "WP5636"
        assert found["name"] == "Mitophagy" and found["state"] == "on_main"
        missing = c.get("/api/pathways/9999").json()
        assert missing["exists"] is False and missing["wpid"] == "WP9999"
        assert missing["state"] == "absent"


def test_request_changes_endpoint(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]

        # A non-curator cannot request changes.
        assert c.post(f"/api/reviews/{pr}/request-changes", data={"note": "x"}).status_code == 403

        current["user"] = "curator"
        r = c.post(f"/api/reviews/{pr}/request-changes", data={"note": "Annotate the nodes."})
        assert r.status_code == 200
        assert r.json()["status"] == "changes_requested"
        # It leaves the open queue and shows under changes_requested.
        assert c.get("/api/reviews").json() == []
        cr = c.get("/api/reviews?status=changes_requested").json()
        assert [x["pr_number"] for x in cr] == [pr]
        # The note went out as a PR comment.
        comments = app.state._fake.issue_comments[(app.state.settings.content_repo, pr)]
        assert any("Annotate the nodes." in b for b in comments)


def test_pathway_info_detects_pending_new_submission(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(default_branches={f"{repo}#{branch}": "base"})
    fake.open_pull_request(repo, head="submit/WP5642", base=branch, title="t", body="b")  # PR #1
    app, _current = _authed_app(tmp_path, fake=fake)
    with TestClient(app) as c:
        info = c.get("/api/pathways/5642").json()
        assert info["exists"] is False
        assert info["state"] == "pending_new"
        assert info["pr_number"] == 1


def test_revise_new_submission_end_to_end(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        sub = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()
        pr = sub["pr_number"]

        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "annotate the nodes"})
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "changes_requested"

        # A stranger cannot revise someone else's submission.
        current["user"] = "mallory"
        forbidden = c.post(
            f"/api/reviews/{pr}/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert forbidden.status_code == 403

        # The submitter revises → commits onto the SAME PR and re-opens the review.
        current["user"] = "bob"
        rev = c.post(
            f"/api/reviews/{pr}/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
            data={"description": "added identifiers"},
        )
        assert rev.status_code == 201
        assert rev.json()["pr_number"] == pr  # no new PR
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "open"  # back in the queue


def test_revise_without_pending_submission_404(tmp_path):
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "bob"
        r = c.post(
            "/api/reviews/9999/revise",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert r.status_code == 404


def test_dashboard_end_to_end(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]

        queue = c.get("/api/reviews").json()
        assert [r["pr_number"] for r in queue] == [pr]
        assert queue[0]["submitter"] == "bob"

        # Approving before the checklist is complete is refused (409).
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 409

        detail = c.get(f"/api/reviews/{pr}").json()
        for item in detail["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})

        # A non-curator cannot approve (403).
        current["user"] = "randouser"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 403

        # Even complete + curator, merge is blocked until the PR-preview CI is green (409).
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 409
        app.state._fake.previews[pr] = {"status": "ready"}

        # The curator approves → merges.
        ok = c.post(f"/api/reviews/{pr}/approve")
        assert ok.status_code == 200
        assert ok.json()["status"] == "merged"
        assert ok.json()["approved_by"] == "curator"
        assert pr in app.state._fake.merged
        assert c.get("/api/reviews").json() == []


def test_checklist_and_assign_require_curator(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        # A logged-in non-curator cannot mutate review state.
        assert (
            c.post(
                f"/api/reviews/{pr}/checklist", data={"key": "render_ok", "state": "pass"}
            ).status_code
            == 403
        )
        assert (
            c.post(f"/api/reviews/{pr}/assign", data={"curator": "curator"}).status_code == 403
        )
        # The curator can.
        current["user"] = "curator"
        assert (
            c.post(
                f"/api/reviews/{pr}/checklist", data={"key": "render_ok", "state": "pass"}
            ).status_code
            == 200
        )
        assert (
            c.post(f"/api/reviews/{pr}/assign", data={"curator": "curator"}).status_code == 200
        )


# -- GitHub App (bot) identity -------------------------------------------------------------


def test_submit_posts_mirror_comment(tmp_path):
    app, _current = _authed_app(tmp_path)
    with TestClient(app) as c:
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
    fake = app.state._fake
    repo = app.state.settings.content_repo
    assert (repo, pr) in fake.comments  # the bot mirrored the new submission
    assert "curation" in fake.comments[(repo, pr)][MIRROR_MARKER].lower()


def test_approve_merges_via_bot_and_updates_mirror(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        detail = c.get(f"/api/reviews/{pr}").json()
        for item in detail["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})
        app.state._fake.previews[pr] = {"status": "ready"}  # PR-preview CI green → merge allowed
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 200

    fake = app.state._fake
    repo = app.state.settings.content_repo
    assert pr in fake.merged  # merged through the bot identity
    # The mirror comment (single upserted comment) now reflects the merge.
    mirror = fake.comments[(repo, pr)][MIRROR_MARKER]
    assert "**merged**." in mirror and "**Approved and merged by @curator.**" in mirror


def test_approve_503_without_bot_identity(tmp_path):
    # Configured app, logged-in curator, but no GitHub App → merge cannot run as the bot.
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}", curators=["curator"]
    )
    app = build_app(settings)
    fake = FakeGitHubClient(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "curator"
    # Deliberately do NOT override the bot deps: state.bot_app is None → get_bot_client 503s.
    with TestClient(app) as c:
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        for item in c.get(f"/api/reviews/{pr}").json()["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 503
        assert pr not in fake.merged


# -- GitHub webhook: lock/reservation lifecycle on PR close (issue #8) ----------------------

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402


def _signed(secret: str, payload: dict):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def _pr_closed_body(secret, pr_number, *, merged):
    return _signed(
        secret,
        {
            "action": "closed",
            "number": pr_number,
            "pull_request": {"number": pr_number, "merged": merged},
        },
    )


def test_webhook_merged_finalizes_review(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        body, sig = _pr_closed_body("whsec", pr, merged=True)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200 and r.json()["tracked"] is True
        # Review is now merged even though nobody clicked Approve in the app.
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "merged"


def test_webhook_closed_unmerged_releases_lock_and_reservation(tmp_path):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
    )
    app, current = _authed_app(
        tmp_path, curators=["curator"], fake=fake, webhook_secret="whsec"
    )
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        ).json()["pr_number"]
        # Lock is held by alice on WP5636.
        assert app.state.locks.is_locked(5636)

        body, sig = _pr_closed_body("whsec", pr, merged=False)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        # PR closed outside the app → lock freed, review closed.
        assert not app.state.locks.is_locked(5636)
        assert c.get(f"/api/reviews/{pr}", ).json() is not None
        assert c.get("/api/reviews?status=closed").json()[0]["pr_number"] == pr


def test_webhook_rejects_bad_signature_and_missing_secret(tmp_path):
    # No secret configured → 503.
    app_no_secret, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app_no_secret) as c:
        assert c.post("/webhooks/github", content=b"{}").status_code == 503

    app, _ = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        body, _sig = _pr_closed_body("whsec", 1, merged=True)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert r.status_code == 401


def test_webhook_is_idempotent(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        body, sig = _pr_closed_body("whsec", pr, merged=True)
        h = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig}
        assert c.post("/webhooks/github", content=body, headers=h).status_code == 200
        # A duplicate delivery is a harmless no-op (review already terminal).
        r2 = c.post("/webhooks/github", content=body, headers=h)
        assert r2.status_code == 200 and r2.json()["tracked"] is True
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "merged"


# -- pathway preview serving (issue #11) ----------------------------------------------------



def test_preview_route_serves_the_app_render(tmp_path):
    # Submitting renders both sides in-process, so the SVG is servable straight away — no CI
    # artifact, no second source (the artifact path was retired with the CI render).
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]  # assigns WP5637

        after = c.get(f"/previews/{pr}/after.svg")
        assert after.status_code == 200
        assert after.headers["content-type"].startswith("image/svg+xml")
        assert after.content.startswith(b"<svg")
        assert "sandbox" in after.headers.get("content-security-policy", "")

        # Unknown side → 404; unknown PR → 404.
        assert c.get(f"/previews/{pr}/sideways.svg").status_code == 404
        assert c.get("/previews/999999/after.svg").status_code == 404


def test_preview_nodes_route_serves_the_clickable_hotspots(tmp_path):
    # Issue #14. The drawing goes into an <img>, so its markup is inert and the clickable layer
    # has to be served separately and laid over it.
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]

        r = c.get(f"/previews/{pr}/after-nodes.json")
        assert r.status_code == 200
        nodes = r.json()
        assert isinstance(nodes, list)
        for n in nodes:
            # Percentages of the viewBox, so the overlay survives the viewport's resize-based
            # zoom without the client knowing the coordinate system.
            assert 0 <= n["left"] <= 100 and 0 <= n["top"] <= 100
            assert {"label", "type", "database", "identifier", "url", "comment"} <= set(n)

        # A side that was never rendered has no hotspots on file: 404, so the client leaves the
        # static image alone rather than drawing an overlay it cannot trust.
        assert c.get(f"/previews/{pr}/before-nodes.json").status_code == 404
        assert c.get(f"/previews/{pr}/sideways-nodes.json").status_code == 404
        assert c.get("/previews/999999/after-nodes.json").status_code == 404


_DIFF_BEFORE = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260520113005">'
    b'<Graphics BoardWidth="400" BoardHeight="300"/>'
    b'<DataNode TextLabel="AKT1" Type="GeneProduct" GraphId="n1">'
    b'<Graphics CenterX="100" CenterY="100" Width="80" Height="20"/>'
    b'<Xref Database="Ensembl" ID="ENSG00000000000"/></DataNode>'
    b'<DataNode TextLabel="Dropped" Type="GeneProduct" GraphId="n2">'
    b'<Graphics CenterX="100" CenterY="200" Width="80" Height="20"/>'
    b'<Xref Database="Ensembl" ID="ENSG00000111111"/></DataNode>'
    b"</Pathway>"
)
_DIFF_AFTER = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260521113005">'
    b'<Graphics BoardWidth="400" BoardHeight="300"/>'
    b'<DataNode TextLabel="AKT1" Type="GeneProduct" GraphId="n1">'
    b'<Graphics CenterX="100" CenterY="100" Width="80" Height="20"/>'
    b'<Xref Database="Ensembl" ID="ENSG00000142208"/></DataNode>'
    b'<DataNode TextLabel="Fresh" Type="GeneProduct" GraphId="n3">'
    b'<Graphics CenterX="250" CenterY="200" Width="80" Height="20"/>'
    b'<Xref Database="Ensembl" ID="ENSG00000222222"/></DataNode>'
    b"</Pathway>"
)


def test_update_preview_says_what_changed(tmp_path):
    # Issue #24. Two pictures side by side left the curator to spot the difference by eye; the
    # counts and the per-node classification are what make an update legible rather than merely
    # visible. AKT1 keeps its box and changes its identifier — the case a picture cannot show.
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#pathways/WP5636/WP5636.gpml": "oldsha"},
        existing_contents={f"{repo}#pathways/WP5636/WP5636.gpml": _DIFF_BEFORE.decode()},
    )
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(_DIFF_AFTER), "application/xml")},
        ).json()["pr_number"]

        d = c.get(f"/previews/{pr}/diff.json")
        assert d.status_code == 200
        body = d.json()
        assert body["summary"]["reannotated"] == 1
        assert body["summary"]["added"] == 1
        assert body["summary"]["removed"] == 1

        # The overlay colours hotspot i from entry i, so the two files must stay the same length.
        for side in ("before", "after"):
            nodes = c.get(f"/previews/{pr}/{side}-nodes.json").json()
            assert len(body[side]) == len(nodes)

        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        # The sentence is server-rendered: it is what a curator reads before deciding whether to
        # look at the pictures at all, so it cannot wait on a fetch.
        assert "1 re-annotated" in page and "1 added" in page and "1 removed" in page


def test_a_new_pathway_has_nothing_to_diff_against(tmp_path):
    # One side, so there is no comparison to make and the card shows no summary at all — rather
    # than a comparison against an empty "before" reporting every node as newly added.
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        assert c.get(f"/previews/{pr}/diff.json").status_code == 404
        _login(c, "curator")
        assert "diff-summary" not in c.get(f"/dashboard/{pr}").text


def test_preview_diff_requires_a_known_review(tmp_path):
    # Same rule as the SVG and hotspot routes: an unknown PR is not a way to probe the cache.
    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        assert c.get("/previews/4242/diff.json").status_code == 404


def test_preview_nodes_route_requires_a_known_review(tmp_path):
    # Same rule as the SVG route: an unknown PR must not be a way to probe the cache directory.
    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        assert c.get("/previews/4242/after-nodes.json").status_code == 404


def _login(client, login: str) -> None:
    """Set the signed session cookie the HTML pages read.

    The JSON API resolves identity through the ``get_current_user`` dependency (overridden in
    tests), but the server-rendered pages read ``request.session`` directly, so a page test has
    to carry a real session cookie. Mirrors what Starlette's SessionMiddleware writes.
    """
    import base64
    import json as _json

    from itsdangerous import TimestampSigner

    data = base64.b64encode(_json.dumps({"gh_login": login}).encode())
    signer = TimestampSigner("dev-insecure-change-me")  # the default session_secret in tests
    client.cookies.set("session", signer.sign(data).decode())


def _fill_queue(app, count: int, *, submitter: str = "bob") -> None:
    """Put `count` open reviews in the registry without uploading `count` pathways.

    A submission is a render plus a pull request plus a mirror comment, and none of that is what
    a paging test is about -- it is about how many of the rows come back. The pull requests are
    opened on the fake all the same: the dashboard reconciles before it renders, and a review
    whose pull request does not exist is terminalised on sight, which empties the queue.
    """
    from app.models import Review, ReviewStatus, utcnow

    repo = app.state.settings.content_repo
    for i in range(1, count + 1):
        app.state._fake.open_pull_request(
            repo, head=f"WP{5636 + i}_bob_x", base="main", title="t", body="b"
        )
    with app.state.session_factory() as s:
        for pr in range(1, count + 1):
            s.add(
                Review(
                    pr_number=pr,
                    wpid=5636 + pr,
                    submitter=submitter,
                    kind="new",
                    status=ReviewStatus.OPEN,
                    checklist=[],
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        s.commit()


def test_the_queue_is_paged_rather_than_rendered_whole(tmp_path):
    # Issue #17. Against the real content repo the Open tab is the ordinary working view -- the
    # audit counted 51 pull requests in three months -- and every row is a full card.
    from app.main import QUEUE_PAGE_SIZE

    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        _fill_queue(app, QUEUE_PAGE_SIZE + 3)
        _login(c, "curator")
        first = c.get("/dashboard").text
        second = c.get("/dashboard", params={"page": 2}).text

    assert first.count('class="review-card"') == QUEUE_PAGE_SIZE
    assert second.count('class="review-card"') == 3
    assert f"Showing 1&ndash;{QUEUE_PAGE_SIZE} of {QUEUE_PAGE_SIZE + 3}" in first
    assert "page 1 of 2" in first
    # No review is on both pages, and none is missing from the pair. The closing quote matters:
    # without it `/dashboard/2` is also found inside `/dashboard/21`.
    on_first = {n for n in range(1, QUEUE_PAGE_SIZE + 4) if f'/dashboard/{n}"' in first}
    on_second = {n for n in range(1, QUEUE_PAGE_SIZE + 4) if f'/dashboard/{n}"' in second}
    assert on_first & on_second == set()
    assert on_first | on_second == set(range(1, QUEUE_PAGE_SIZE + 4))


def test_paging_keeps_the_filter_it_was_reached_through(tmp_path):
    # The pager builds its links off the live query string, so ?status= and ?mine=1 survive. A
    # Next that dropped the filter would silently move the reader to a different queue.
    from app.main import QUEUE_PAGE_SIZE

    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        _fill_queue(app, QUEUE_PAGE_SIZE + 1, submitter="curator")
        _login(c, "curator")
        page = c.get("/dashboard", params={"mine": 1}).text

    assert 'href="/dashboard?mine=1&amp;page=2"' in page


def test_pager_links_are_relative(tmp_path):
    """Nothing tells uvicorn to trust `X-Forwarded-Proto`, and Traefik forwards plain HTTP, so an
    absolute link would carry `http://` on a site served over `https://`."""
    from app.main import QUEUE_PAGE_SIZE

    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        _fill_queue(app, QUEUE_PAGE_SIZE + 1)
        _login(c, "curator")
        page = c.get("/dashboard").text

    assert 'href="/dashboard?page=2"' in page
    assert "http://testserver" not in page


def test_a_page_past_the_end_lands_on_the_last_one(tmp_path):
    # Reached by a bookmark or a back button after the queue shrank underneath it. An empty page
    # there reads as "everything is gone".
    from app.main import QUEUE_PAGE_SIZE

    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        _fill_queue(app, QUEUE_PAGE_SIZE + 1)
        _login(c, "curator")
        page = c.get("/dashboard", params={"page": 99})

    assert page.status_code == 200
    assert page.text.count('class="review-card"') == 1
    assert "page 2 of 2" in page.text


def test_a_queue_that_fits_on_one_page_shows_no_pager(tmp_path):
    app, _ = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        _fill_queue(app, 2)
        _login(c, "curator")
        page = c.get("/dashboard").text

    assert 'class="pager"' not in page


def test_dashboard_shows_the_render_after_changes_are_requested(tmp_path):
    # The render used to be computed for open reviews only, so every other filter showed the
    # "no render" state while the SVG sat in the cache.
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "add an identifier"})

        _login(c, "curator")
        page = c.get("/dashboard", params={"status": "changes_requested"})
        assert page.status_code == 200
        assert f"/previews/{pr}/after.svg".encode() in page.content
        assert b"No render on file" not in page.content


def test_the_submitter_note_reaches_github_in_the_mirror_comment(tmp_path):
    # Issue #25, found on production. The note went only into the pull request body, and the
    # target repo's own workflow replaced that body with its template — so a note reading, in
    # capitals, "this is a test, do not publish" left no trace on GitHub. The mirror comment is
    # the app's own and is updated in place, so it is the copy that survives.
    app, current = _authed_app(tmp_path, curators=["curator"])
    note = "Curated from Reactome; please check the HGNC identifiers."
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
            data={"description": note},
        ).json()["pr_number"]

        mirror = app.state._fake.comments[(app.state.settings.content_repo, pr)][
            "<!-- wikipathways-submit:mirror -->"
        ]
        assert "What the submitter said about this change" in mirror
        assert f"> {note}" in mirror

        # And it is on the review row, not only in the render cache — so it is still there once
        # the cache is pruned at a terminal transition.
        _login(c, "curator")
        assert c.get(f"/api/reviews/{pr}").json()["submitter_note"] == note
        assert note in c.get(f"/dashboard/{pr}").text


def test_hotspot_overlay_is_one_tab_stop_and_announces_itself(tmp_path):
    # Issue #19. The overlay puts a button on every data node, so a dense pathway inserted one
    # tab stop per node between the diagram and the checklist. Two things keep it to one stop
    # without deleting the keyboard path, and both are markup a later edit could quietly drop:
    # the toolbar role (which is what the roving tabindex in app.js implements) and the polite
    # live region that reads out each node's properties as the selection moves.
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        c.post("/api/submit", files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")})
        _login(c, "curator")
        page = c.get("/dashboard").text

    assert 'class="zoom__hotspots" hidden role="toolbar"' in page
    assert "Use the arrow keys to move between them" in page
    assert 'class="node-panel__body" aria-live="polite" aria-atomic="true"' in page
    # Not a dialog: focus stays on the node so the arrow keys keep working, which a dialog's
    # focus trap would break.
    assert 'class="node-panel" hidden role="dialog"' not in page


def test_preview_missing_side_serves_placeholder(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        # A new pathway has no "before" — the frame stays intact instead of breaking.
        r = c.get(f"/previews/{pr}/before.svg")
        assert r.status_code == 200 and b"Preview unavailable" in r.content


# -- OAuth flow ----------------------------------------------------------------------------


def test_login_redirects_to_github(tmp_path):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_oauth_client_id="cid123",
        github_oauth_client_secret="secret",
        oauth_redirect_uri="http://testserver/auth/callback",
    )
    with TestClient(build_app(settings)) as c:
        resp = c.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert loc.startswith("https://github.com/login/oauth/authorize")
        assert "client_id=cid123" in loc
        assert "state=" in loc


def test_login_503_when_unconfigured(client):
    assert client.get("/auth/login", follow_redirects=False).status_code == 503


def test_callback_exchanges_code_and_sets_session(tmp_path):
    # Mock GitHub's token + user endpoints so the flow runs without a network.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_faketoken"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "alice"})
        return httpx.Response(404)

    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_oauth_client_id="cid",
        github_oauth_client_secret="sec",
        oauth_redirect_uri="http://testserver/auth/callback",
        curators=["alice"],
    )
    app = build_app(settings)
    with TestClient(app) as c:
        # Inject the mock transport into the live oauth object.
        app.state.oauth = GithubOAuth("cid", "sec", transport=httpx.MockTransport(handler))
        # Seed a matching CSRF state via a login round-trip.
        login = c.get("/auth/login", follow_redirects=False)
        query = login.headers["location"].split("?", 1)[1]
        state = dict(p.split("=", 1) for p in query.split("&"))["state"]
        cb = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert cb.status_code == 302
        me = c.get("/auth/me").json()
        assert me == {"authenticated": True, "login": "alice", "is_curator": True}


def test_callback_rejects_bad_state(tmp_path):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_oauth_client_id="cid",
        github_oauth_client_secret="sec",
    )
    app = build_app(settings)
    with TestClient(app) as c:
        app.state.oauth = GithubOAuth("cid", "sec")
        # No prior /auth/login → no stored state → mismatch.
        resp = c.get("/auth/callback?code=abc&state=forged", follow_redirects=False)
        assert resp.status_code == 400


def _notice_client(tmp_path, notice):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}", site_notice=notice
    )
    return TestClient(build_app(settings))


def test_site_notice_shows_on_every_page_when_configured(tmp_path):
    # A deployment can be pointed at a target that cannot publish, and nothing on screen said so.
    # It has to appear on the submit page above all, since that is where the promise is made.
    notice = "Sandbox deployment: submissions here are not published."
    with _notice_client(tmp_path, notice) as c:
        for path in ("/", "/dashboard"):
            body = c.get(path).text
            assert notice in body
            assert 'class="site-notice"' in body


def test_no_site_notice_element_when_unset(tmp_path):
    # Empty must mean no banner at all, not an empty amber bar on every page.
    with _notice_client(tmp_path, "") as c:
        assert "site-notice" not in c.get("/").text


def test_blank_site_notice_is_treated_as_unset(tmp_path):
    with _notice_client(tmp_path, "   ") as c:
        assert "site-notice" not in c.get("/").text


def test_site_notice_is_escaped(tmp_path):
    # It comes from deploy config rather than a user, but config is not markup and this renders
    # on every page including logged-out ones.
    with _notice_client(tmp_path, "<script>alert(1)</script>") as c:
        body = c.get("/").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


def test_oversized_upload_is_refused_with_413(tmp_path):
    # Issue #16: every upload endpoint read the body into memory unbounded, so a large post — by
    # accident as easily as on purpose — took the single-replica process with it.
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}", max_upload_bytes=1024
    )
    with TestClient(build_app(settings)) as c:
        big = b"<Pathway>" + b"x" * 4096
        r = c.post(
            "/api/validate", files={"file": ("big.gpml", io.BytesIO(big), "application/xml")}
        )
        assert r.status_code == 413
        assert "larger than" in r.json()["detail"]


def test_upload_at_the_limit_is_still_accepted(tmp_path):
    # The boundary belongs to the caller: refusing at exactly the limit would make the documented
    # number a lie.
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}", max_upload_bytes=len(GOOD_GPML)
    )
    with TestClient(build_app(settings)) as c:
        r = c.post(
            "/api/validate",
            files={"file": ("ok.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert r.status_code == 200


def test_robots_txt_keeps_crawlers_off_the_oauth_and_preview_routes(client):
    # Issue #20. /auth/login is a real redirect into GitHub's OAuth flow, so a crawler walking it
    # mints authorization requests and the state entries behind them.
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    for path in ("/auth", "/previews", "/dashboard", "/api"):
        assert f"Disallow: {path}" in body


# -- a revoked authorisation (issue #28) ---------------------------------------------------


def test_a_revoked_authorisation_is_a_401_not_a_502(tmp_path):
    """The submitter can fix this and the server cannot, so it must not read as a server fault.

    Issue #28 filed this as tokens needing refresh. They cannot be refreshed: the user identity is
    an **OAuth App** token, and expiring user tokens with refresh tokens are a GitHub *App*
    feature (a GitHub App user token carries no scopes; these report `public_repo, read:user`).
    An OAuth App token has no expiry, so every way one dies is a revocation — and the only useful
    response to that is to ask for a fresh authorisation.
    """
    app, _current = _authed_app(tmp_path, fake=FakeGitHubClient(reject_credentials=True))
    with TestClient(app) as c:
        resp = c.post(
            "/api/submit",
            files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )

    assert resp.status_code == 401, "a revoked token used to surface as 502 'GitHub call failed'"
    detail = resp.json()["detail"]
    assert "revoked" in detail
    assert "/auth/login" in detail, "must say what to do, not just what went wrong"


def test_a_revoked_authorisation_clears_the_session(tmp_path):
    """Otherwise the submitter retries the same dead token until the cookie ages out, with
    nothing anywhere telling them to sign in again."""
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: FakeGitHubClient(
        reject_credentials=True
    )
    app.dependency_overrides[get_current_user] = lambda: "alice"
    with TestClient(app) as c:
        c.cookies.set("session", "x")  # any session; the handler must empty it
        before = c.post(
            "/api/submit",
            files={"file": ("upload.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        )
        assert before.status_code == 401
        # The session cookie is cleared or emptied, so /auth/me no longer claims a login.
        me = c.get("/auth/me")
    assert me.json().get("login") is None


def test_the_real_client_turns_a_github_401_into_credentials_rejected():
    """Against a real-shaped response, not the fake.

    Every previous mistake of this kind here was one the fake agreed with — `open_pull_request`
    echoing its request, `create_branch` treating a duplicate ref as a refusal. A MockTransport
    test is the only kind that catches the class.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = HttpGitHubClient(
        token="revoked", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    with pytest.raises(CredentialsRejected) as exc:
        client.get_branch_sha("owner/repo", "main")
    # Carries GitHub's own words, per the standing rule that an error never drops its evidence.
    assert "Bad credentials" in str(exc.value)


def test_a_403_is_still_a_plain_github_error():
    """Only 401 means the credential itself is rejected. A 403 is a permission or a rate limit —
    signing in again does not help, so it must not be relabelled as if it would."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limit exceeded"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    with pytest.raises(GitHubError) as exc:
        client.get_branch_sha("owner/repo", "main")
    assert not isinstance(exc.value, CredentialsRejected)


def test_the_bots_own_rejected_credentials_are_a_503_not_a_login_prompt(tmp_path):
    """Telling a curator to sign in again when it is the *App's* key that is wrong sends them off
    to do something that cannot possibly help, and hides a deployment fault behind a user error.

    The approval has to be driven all the way to a legitimately mergeable state first, because
    the gate refuses earlier for its own reasons (#27) and a 409 would pass a naive assertion
    that merely checked "not 401"."""
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        for item in c.get(f"/api/reviews/{pr}").json()["checklist"]:
            if item["required"]:
                c.post(f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"})
        app.state._fake.previews[pr] = {"status": "ready"}
        # Only now, with approval genuinely available, break the bot.
        bot = FakeGitHubClient(
            reject_credentials=True, login="wikipathways-bot", identity="bot"
        )
        app.dependency_overrides[get_bot_client] = lambda: bot
        resp = c.post(f"/api/reviews/{pr}/approve")

    assert resp.status_code == 503, "not 401 (would misdirect) and not 502 (hides the cause)"
    detail = resp.json()["detail"]
    assert "server configuration" in detail
    assert "/auth/login" not in detail, "must not send them to sign in; it would not help"


# -- telling the submitter something happened -----------------------------------------------


def test_a_submission_gets_a_thank_you_that_names_the_submitter(tmp_path):
    """A first-time contributor otherwise watches an automated status table appear and cannot
    tell whether a person is coming.

    The `@` mention is the functional part, not politeness: it is what makes GitHub email them
    in the modes where the pull request belongs to the bot and they are not its author.
    """
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]

    fake = app.state._fake
    repo = app.state.settings.content_repo
    welcome = fake.comments[(repo, pr)][WELCOME_MARKER]
    assert "@bob" in welcome
    assert "curator will review it" in welcome
    # It is a separate comment from the mirror, because the mirror is edited on every status
    # change and editing notifies nobody.
    assert MIRROR_MARKER in fake.comments[(repo, pr)]
    assert welcome != fake.comments[(repo, pr)][MIRROR_MARKER]


def test_the_thank_you_is_posted_once_not_once_per_upload(tmp_path):
    """A re-upload must not re-thank them. Keyed by its own marker, so the second write edits
    the first comment instead of adding another."""
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        repo = app.state.settings.content_repo
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        before = len(app.state._fake.comments[(repo, pr)])
        # Same pathway, uploaded again.
        revised = c.post(f"/api/reviews/{pr}/revise",
                         files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")})
        assert revised.status_code == 201, revised.text

    after = app.state._fake.comments[(repo, pr)]
    assert len(after) == before, "a re-upload added another comment"
    assert after[WELCOME_MARKER].count("Thanks for submitting") == 1


def test_requesting_changes_names_the_submitter_so_it_reaches_them(tmp_path):
    """It used to name only the curator. In fork mode the submitter is the pull request author
    and would be notified anyway, but under `bot` and `user` identities the pull request is not
    theirs and nothing would reach them at all."""
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        asked_resp = c.post(
            f"/api/reviews/{pr}/request-changes", data={"note": "Please annotate the nodes."}
        )
        assert asked_resp.status_code == 200, asked_resp.text
        repo = app.state.settings.content_repo

    fake = app.state._fake
    bodies = fake.issue_comments[(repo, pr)]
    asked = [b for b in bodies if "asked for changes" in b]
    assert asked, "no change request comment was posted"
    assert "@bob" in asked[0], "the submitter is not named, so GitHub may notify nobody"
    assert "@curator" in asked[0]
    assert "Please annotate the nodes." in asked[0]


def test_rejection_names_the_submitter_too(tmp_path):
    """A rejection is the message that most needs to reach the person who submitted, and under
    the bot and user identities the pull request is not theirs, so nothing else would."""
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        current["user"] = "curator"
        resp = c.post(f"/api/reviews/{pr}/reject", data={"note": "Duplicate of WP123."})
        assert resp.status_code == 200, resp.text
        repo = app.state.settings.content_repo

    posted = app.state._fake.issue_comments[(repo, pr)]
    rejected = [b for b in posted if "rejected this submission" in b]
    assert rejected, "no rejection comment was posted"
    assert "@bob" in rejected[0], "the submitter is not named, so GitHub may notify nobody"
    assert "@curator" in rejected[0]
    assert "Duplicate of WP123." in rejected[0]


# -- adopting a pull request the app did not open (webhook) ---------------------------------


def _pr_opened_body(secret, pr_number, *, action="opened"):
    return _signed(
        secret,
        {"action": action, "number": pr_number, "pull_request": {"number": pr_number}},
    )


def _seed_plugin_pr(settings, pr_number=73):
    """A cross-repository pull request shaped like the PathVisio plugin's, on the content repo."""
    repo = settings.content_repo
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{settings.default_branch}": "basesha"},
        existing_contents={f"{repo}#pathways/WP3894/WP3894.gpml": GOOD_GPML.decode()},
    )
    fake.seed_foreign_pr(
        repo,
        pr_number,
        author="traybug23",
        title="Contribution: Update WP3894",
        head_branch="WP3894_traybug23_20260820-053517",
        head_repo="traybug23/sandbox-wp-db",
        head_sha="e47f6026bcfa42d4f4991296a728eb0babb23f41",
        files=[("pathways/WP3894/WP3894.gpml", "modified", GOOD_GPML.decode())],
    )
    return fake


def test_webhook_opened_adopts_a_foreign_pull_request(tmp_path):
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        curators=["curator"],
        github_webhook_secret="whsec",
        preview_cache_dir=str(tmp_path / "preview-cache"),
        publish_mode="pipeline",
        adopt_foreign_prs=True,
    )
    fake = _seed_plugin_pr(settings)
    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "curator"

    with TestClient(app) as c:
        body, sig = _pr_opened_body("whsec", 73)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200 and r.json()["queued"] is True
        # TestClient runs background tasks before returning, so the row is there now.
        review = c.get("/api/reviews/73").json()
        assert review["origin"] == "adopted"
        assert review["submitter"] == "traybug23"
        assert review["wpid"] == 3894
        assert review["adopted"] is True
        # And it is in the queue the dashboard renders, not merely in the table.
        assert [r["pr_number"] for r in c.get("/api/reviews").json()] == [73]


def test_webhook_does_not_adopt_when_the_feature_is_off(tmp_path):
    """The default. Upgrading must not start putting other people's pull requests in the queue."""
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        github_webhook_secret="whsec",
        preview_cache_dir=str(tmp_path / "preview-cache"),
        publish_mode="pipeline",
    )
    fake = _seed_plugin_pr(settings)
    app = build_app(settings)
    app.dependency_overrides[get_bot_optional] = lambda: fake
    with TestClient(app) as c:
        body, sig = _pr_opened_body("whsec", 73)
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig},
        )
        assert r.json()["adoption"] == "disabled"
        assert c.get("/api/reviews/73").status_code == 404


def test_adoption_is_refused_outside_pipeline_mode(tmp_path):
    """Approving in direct mode *merges*, and a foreign pull request must never be merged.

    The plugin's new-pathway submissions arrive at a title-derived path, and its placeholder ones
    at the shared WP0001 slot every portal submission writes to — merging either onto `main` is
    the 2026-07-30 incident by a different door.
    """
    settings = _settings(
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        publish_mode="direct",
        adopt_foreign_prs=True,
    )
    assert settings.adopt_foreign_prs is False

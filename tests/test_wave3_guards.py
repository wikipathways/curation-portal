"""Wave 3: approving must not publish something nobody reviewed.

Two ways it could, both measured on production on 2026-11-11:

- **A stale base.** Most production curation is direct commits to ``main``. An update replaces the
  GPML wholesale, so one made against an older version silently undoes a direct edit made since.
- **Files besides the pathway.** Five of 26 production forks had diverged; a branch cut from such a
  fork carries its unrelated commits into the pull request (#120 carried a stray file).
"""
from __future__ import annotations

import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.curators import ConfigCurators
from app.github import FakeGitHubClient, PrFile
from app.models import Review, ReviewStatus
from app.review.checklist import ChecklistState
from app.review.service import BaseMoved, CurationService, UnexpectedFiles
from app.submit.targets import resolve_write_target
from tests.test_api import REV_GPML, _authed_app, _login, _settings, _signed

PATH = "pathways/WP5636/WP5636.gpml"


def _app(tmp_path, *, publish_mode="direct"):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo = settings.content_repo
    fake = FakeGitHubClient(
        default_branches={f"{repo}#main": "basesha"},
        existing_files={f"{repo}#{PATH}": "blob-v1"},
        existing_contents={f"{repo}#{PATH}": REV_GPML.decode()},
    )
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake, webhook_secret="whsec")
    return app, current, fake, repo


def _update(c, current) -> int:
    current["user"] = "alice"
    r = c.post(
        "/api/pathways/5636/update",
        files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
    )
    assert r.status_code == 201, r.text
    return r.json()["pr_number"]


def _ready(app, pr):
    svc = app.state.worker._make_curation(app.state._fake)
    for item in svc.get(pr).checklist:
        if item.get("required"):
            svc.set_checklist_item(pr, item["key"], ChecklistState.PASS.value)
    return svc


def test_an_update_records_the_version_it_replaces(tmp_path):
    app, current, _fake, _repo = _app(tmp_path)
    with TestClient(app) as c:
        pr = _update(c, current)
        with app.state.session_factory() as s:
            row = s.get(Review, pr)
        assert row.base_blob == "blob-v1"
        assert row.pathway_paths == [PATH]


def test_a_direct_commit_to_the_pathway_blocks_approval(tmp_path):
    app, current, fake, repo = _app(tmp_path)
    with TestClient(app) as c:
        pr = _update(c, current)
        svc = _ready(app, pr)
        fake.existing_files[(repo, PATH)] = "blob-v2"  # somebody committed to main directly

        current["user"] = "curator"
        r = c.post(f"/api/reviews/{pr}/approve")
        assert r.status_code == 409
        assert "changed on main" in r.json()["detail"]
        assert svc.get(pr).status == ReviewStatus.OPEN

        # The background check says so on the card before anyone presses the button.
        app.state.worker.tick(github=fake)
        assert svc.get(pr).pipeline_run["guards"]["base_moved"] == "blob-v2"
        _login(c, "curator")
        assert "The pathway changed on the main branch" in c.get(f"/dashboard/{pr}").text


def test_an_approval_the_base_has_overtaken_is_withdrawn(tmp_path):
    app, current, fake, repo = _app(tmp_path)
    with TestClient(app) as c:
        pr = _update(c, current)
        svc = CurationService(
            app.state.session_factory,
            fake,
            repo=repo,
            curators=ConfigCurators(["curator"]),
            publish_mode="pipeline",
            require_preview_check=False,
            reconcile_min_interval=timedelta(seconds=0),
        )
        for item in svc.get(pr).checklist:
            if item.get("required"):
                svc.set_checklist_item(pr, item["key"], ChecklistState.PASS.value)
        svc.approve(pr, "curator")
        assert "accepted" in fake.list_labels(repo, pr)

        fake.existing_files[(repo, PATH)] = "blob-v2"
        svc.reconcile()

        review = svc.get(pr)
        assert review.status == ReviewStatus.OPEN
        assert "Approval withdrawn" in review.decision_note
        assert "accepted" not in fake.list_labels(repo, pr)


def test_a_pull_request_carrying_other_files_cannot_be_approved(tmp_path):
    app, current, fake, repo = _app(tmp_path)
    with TestClient(app) as c:
        pr = _update(c, current)
        svc = _ready(app, pr)
        fake.pr_files[(repo, pr)] = [
            PrFile(filename=PATH, status="modified"),
            PrFile(filename="scripts/stray.py", status="added"),
        ]
        with pytest.raises(UnexpectedFiles) as caught:
            svc.approve(pr, "curator")
        assert "scripts/stray.py" in str(caught.value)

        app.state.worker.tick(github=fake)
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        assert "This pull request changes other files too." in page
        assert "scripts/stray.py" in page


def test_a_clean_update_still_approves(tmp_path):
    app, current, fake, repo = _app(tmp_path)
    with TestClient(app) as c:
        pr = _update(c, current)
        svc = CurationService(
            app.state.session_factory,
            fake,
            repo=repo,
            curators=ConfigCurators(["curator"]),
            publish_mode="pipeline",
            require_preview_check=False,
        )
        for item in svc.get(pr).checklist:
            if item.get("required"):
                svc.set_checklist_item(pr, item["key"], ChecklistState.PASS.value)
        fake.pr_files[(repo, pr)] = [PrFile(filename=PATH, status="modified")]
        assert svc.approve(pr, "curator").status == ReviewStatus.APPROVED


def test_base_moved_is_a_refusal_routes_answer_409():
    assert issubclass(BaseMoved, Exception) and issubclass(UnexpectedFiles, Exception)


def test_a_push_to_main_marks_the_pathways_open_updates_due(tmp_path):
    app, current, fake, _repo = _app(tmp_path)
    with TestClient(app) as c:
        pr = _update(c, current)
        app.state.worker.tick(github=fake)

        def push(paths, ref="refs/heads/main"):
            body, sig = _signed(
                "whsec", {"ref": ref, "commits": [{"modified": paths, "added": [], "removed": []}]}
            )
            return c.post(
                "/webhooks/github",
                content=body,
                headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig},
            ).json()

        assert push(["pathways/WP9/WP9.gpml"])["marked"] == 0
        assert push([PATH], ref="refs/heads/feature")["ignored"] == "ref"
        assert push([PATH])["marked"] == 1
        with app.state.session_factory() as s:
            assert s.get(Review, pr).last_checked_at is None


def test_a_diverged_fork_is_not_branched_from():
    user = FakeGitHubClient(login="alice")
    bot = FakeGitHubClient(login="wikipathways-bot")
    user.fork_ahead["alice/wikipathways-database"] = 3
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=bot,
        content_repo="wikipathways/wikipathways-database",
        submitter="alice",
    )
    assert target.identity == "bot" and target.client is bot

    user.fork_ahead.clear()
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=bot,
        content_repo="wikipathways/wikipathways-database",
        submitter="alice",
    )
    assert target.identity == "fork"


def test_a_fork_that_could_not_be_synced_is_not_branched_from():
    # Sandbox #89 (2026-11-19): the sync was refused (an upstream commit touched a workflow file),
    # the branch was cut from the fork's months-old main, and an edit read as a new file.
    repo = "wikipathways/wikipathways-database"
    user = FakeGitHubClient(login="alice", fork_can_sync=False)
    bot = FakeGitHubClient(login="wikipathways-bot")
    user.branches[(repo, "main")] = "upstream-head"
    user.branches[("alice/wikipathways-database", "main")] = "august-head"

    target = resolve_write_target(
        identity="fork", user_client=user, bot_client=bot, content_repo=repo, submitter="alice"
    )
    assert target.identity == "bot" and target.client is bot

    # Level again (synced by hand): back to the submitter's own fork.
    user.branches[("alice/wikipathways-database", "main")] = "upstream-head"
    target = resolve_write_target(
        identity="fork", user_client=user, bot_client=bot, content_repo=repo, submitter="alice"
    )
    assert target.identity == "fork"


def test_a_stale_fork_without_a_bot_still_submits_from_the_fork():
    user = FakeGitHubClient(login="alice")
    user.fork_behind["alice/wikipathways-database"] = 7
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=None,
        content_repo="wikipathways/wikipathways-database",
        submitter="alice",
    )
    assert target.identity == "fork"


def test_http_divergence_reads_both_counts():
    import httpx

    from app.github import HttpGitHubClient

    def handler(request):
        assert request.url.path.endswith("/compare/main...alice:main")
        return httpx.Response(200, json={"ahead_by": 0, "behind_by": 7})

    client = HttpGitHubClient(token="t", transport=httpx.MockTransport(handler))
    assert client.fork_divergence("o/r", "alice/r", "main") == (0, 7)


def test_the_update_tab_offers_the_current_version(tmp_path):
    app, current, _fake, repo = _app(tmp_path)
    with TestClient(app) as c:
        info = c.get("/api/pathways/5636").json()
        assert info["state"] == "on_main"
        assert info["gpml_url"] == f"https://raw.githubusercontent.com/{repo}/main/{PATH}"


def test_validate_checks_the_file_against_the_wpid_being_updated(tmp_path):
    app, _current, _fake, _repo = _app(tmp_path)
    with TestClient(app) as c:
        # REV_GPML declares WP5636.
        def finding(wpid):
            r = c.post(
                "/api/validate",
                files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
                data={"wpid": wpid},
            )
            assert r.status_code == 200, r.text
            return next(
                f for f in r.json()["quality"]["findings"] if f["id"] == "gpml.embedded_wpid"
            )

        assert finding("WP123")["severity"] == "warn"
        assert finding("5636")["severity"] == "pass"


def test_the_forms_say_what_submitting_licenses_and_the_revise_panel_checks_first(tmp_path):
    from tests.test_api import GOOD_GPML

    app, current, _fake, _repo = _app(tmp_path)
    with TestClient(app) as c:
        _login(c, "alice")
        page = c.get("/").text
        assert page.count("creativecommons.org/publicdomain/zero/1.0/") == 2
        assert 'id="update-preflight"' in page

        current["user"] = "alice"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        ).json()["pr_number"]
        review = c.get(f"/dashboard/{pr}").text
        assert f'data-preflight="revise-preflight-{pr}"' in review


def test_a_non_ascii_digit_wpid_is_ignored_not_a_500(tmp_path):
    app, _current, _fake, _repo = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/validate",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
            data={"wpid": "WP²"},
        )
        assert r.status_code == 200

"""Wave 2: the portal sees real traffic.

- reconcile moved off the request path into a background loop, with a claim so two callers never
  re-check one review at the same time;
- webhook deliveries for comments, reviews and workflow runs mark a review due instead of doing
  GitHub work inline;
- curators see open pull requests that touch pathways and have no review, and can adopt one;
- the review page shows the pull request's conversation and can post to it;
- a curator's "changes requested" on GitHub moves the review the same way the portal button does.
"""
from __future__ import annotations

import io
from datetime import timedelta

from fastapi.testclient import TestClient

from app.github import ConversationItem, FakeGitHubClient
from app.models import Review, ReviewStatus, utcnow
from app.review.service import MIRROR_MARKER
from tests.test_api import GOOD_GPML, _authed_app, _login, _signed

REPO = "wikipathways/wikipathways-database"


class CountingFake(FakeGitHubClient):
    """Records every operation, reads included, through the fake's own failure hook."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        super()._maybe_fail(op)


def _fake() -> CountingFake:
    return CountingFake(default_branches={f"{REPO}#main": "basesha"})


def _submit(c) -> int:
    resp = c.post(
        "/api/submit",
        files={"file": ("u.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["pr_number"]


def _hook(c, event: str, payload: dict, secret: str = "whsec"):
    body, sig = _signed(secret, payload)
    return c.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig},
    )


def _last_checked(app, pr: int):
    with app.state.session_factory() as s:
        return s.get(Review, pr).last_checked_at


# -- the loop and the claim ----------------------------------------------------------------


def test_the_dashboard_makes_no_github_calls(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "bob"
        _submit(c)
        fake.calls.clear()
        assert c.get("/dashboard").status_code == 200  # anonymous
        _login(c, "curator")
        assert c.get("/dashboard").status_code == 200
    assert fake.calls == []


def test_a_visitor_reading_a_review_page_spends_no_github_calls(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        fake.calls.clear()
        _login(c, "bob")
        assert c.get(f"/dashboard/{pr}").status_code == 200
    assert fake.calls == []


def test_the_tick_settles_a_pull_request_closed_outside_the_app(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, fake=fake)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        fake.closed.add(pr)
        app.state.worker.tick(github=fake)
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "closed"


def test_only_one_caller_wins_the_claim(tmp_path):
    app, current = _authed_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        svc = app.state.worker._make_curation(None)
        cutoff = utcnow() - timedelta(seconds=30)
        with app.state.session_factory() as s:
            s.get(Review, pr).last_checked_at = None
            s.commit()
        assert svc._claim(pr, cutoff) is True
        assert svc._claim(pr, cutoff) is False


def test_a_tick_does_not_run_twice_at_once(tmp_path):
    app, _ = _authed_app(tmp_path)
    with TestClient(app):
        worker = app.state.worker
        worker._lock.acquire()
        try:
            assert worker.tick(github=_fake()).skipped is True
        finally:
            worker._lock.release()


# -- webhook deliveries mark reviews due ---------------------------------------------------


def test_a_comment_on_a_tracked_pull_request_marks_it_due(tmp_path):
    app, current = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        app.state.worker.tick(github=app.state._fake)
        assert _last_checked(app, pr) is not None

        r = _hook(
            c,
            "issue_comment",
            {"action": "created", "issue": {"number": pr, "pull_request": {}}},
        )
        assert r.status_code == 200 and r.json()["marked"] == 1
        assert _last_checked(app, pr) is None


def test_a_comment_on_an_issue_is_ignored(tmp_path):
    app, _ = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        r = _hook(c, "issue_comment", {"action": "created", "issue": {"number": 7}})
        assert r.json()["ignored"] == "issue"


def test_a_comment_from_another_repository_is_ignored(tmp_path):
    app, current = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        body, sig = _signed(
            "whsec",
            {"action": "created", "issue": {"number": pr, "pull_request": {}}},
            repo="someone/else",
        )
        r = c.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sig},
        )
        assert r.json()["ignored"].startswith("repository:")


def test_a_finished_workflow_marks_the_review_with_its_head_due(tmp_path):
    app, current = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        app.state.worker.tick(github=app.state._fake)
        with app.state.session_factory() as s:
            head = s.get(Review, pr).head_sha
        assert head

        r = _hook(
            c,
            "workflow_run",
            # A fork pull request's run names no pull request, only the head commit.
            {"action": "completed", "workflow_run": {"head_sha": head, "pull_requests": []}},
        )
        assert r.json()["marked"] == 1
        assert _last_checked(app, pr) is None


def test_a_workflow_that_has_not_finished_is_ignored(tmp_path):
    app, _ = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        r = _hook(c, "workflow_run", {"action": "requested", "workflow_run": {}})
        assert r.json()["ignored"] == "action:requested"


# -- a change request made on GitHub -------------------------------------------------------


def _review_event(pr: int, *, user: str, state: str, body: str = "") -> dict:
    return {
        "action": "submitted",
        "pull_request": {"number": pr},
        "review": {"user": {"login": user}, "state": state, "body": body},
    }


def test_a_curators_change_request_on_github_moves_the_review(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        before = len(app.state._fake.issue_comments.get((REPO, pr), []))

        _hook(
            c,
            "pull_request_review",
            _review_event(pr, user="curator", state="changes_requested", body="Map the nodes."),
        )

        detail = c.get(f"/api/reviews/{pr}").json()
        assert detail["status"] == "changes_requested"
        with app.state.session_factory() as s:
            assert "Map the nodes." in s.get(Review, pr).decision_note
        # Already in the thread: the portal does not post it a second time.
        assert len(app.state._fake.issue_comments.get((REPO, pr), [])) == before


def test_a_strangers_change_request_or_a_github_approval_changes_nothing(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"], webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        _hook(c, "pull_request_review", _review_event(pr, user="eve", state="changes_requested"))
        _hook(c, "pull_request_review", _review_event(pr, user="curator", state="approved"))
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "open"


def test_the_portals_change_request_keeps_its_note(tmp_path):
    app, current = _authed_app(tmp_path, curators=["curator"])
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        current["user"] = "curator"
        c.post(f"/api/reviews/{pr}/request-changes", data={"note": "Add a reference."})
        with app.state.session_factory() as s:
            row = s.get(Review, pr)
        assert row.status == ReviewStatus.CHANGES_REQUESTED
        assert row.decided_by == "curator"
        assert "Add a reference." in row.decision_note


# -- the untracked panel -------------------------------------------------------------------


def _seed(fake, number, *, files, author="egonw", title="A plugin submission"):
    fake.seed_foreign_pr(
        REPO,
        number,
        author=author,
        title=title,
        head_branch=f"plugin-{number}",
        head_sha=f"sha{number}",
        files=files,
    )


def test_the_scan_lists_pull_requests_that_touch_pathways_and_have_no_review(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "bob"
        tracked = _submit(c)
        _seed(fake, 85, files=[("pathways/WP554/WP554.gpml", "modified", "<Pathway/>")])
        _seed(fake, 86, files=[("README.md", "modified", "docs")])
        _seed(fake, 87, files=[("pathways/WP9/WP9.info", "modified", "x")])

        assert app.state.worker.tick(github=fake).scanned is True
        items = {p.number: p for p in app.state.worker.untracked.items}

        assert tracked not in items
        assert 86 not in items  # touches no pathway
        assert items[85].adoptable and items[85].summary == "update of WP554"
        assert items[85].author == "egonw"
        assert not items[87].adoptable  # touches pathways/ but has no GPML to review
        assert "no GPML" in items[87].reason

        _login(c, "curator")
        page = c.get("/dashboard").text
        assert "On GitHub, not in the portal" in page
        assert "update of WP554" in page


def test_the_scan_reuses_file_lists_for_an_unchanged_head(tmp_path):
    fake = _fake()
    app, _ = _authed_app(tmp_path, fake=fake)
    with TestClient(app):
        _seed(fake, 85, files=[("pathways/WP554/WP554.gpml", "modified", "<Pathway/>")])
        app.state.worker.tick(github=fake)
        fake.calls.clear()
        app.state.worker.request_scan()
        app.state.worker.tick(github=fake)
        assert "list_open_pull_requests" in fake.calls
        assert "list_pr_files" not in fake.calls


def test_only_curators_see_the_panel(tmp_path):
    fake = _fake()
    app, _ = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        _seed(fake, 85, files=[("pathways/WP554/WP554.gpml", "modified", "<Pathway/>")])
        app.state.worker.tick(github=fake)
        _login(c, "bob")
        assert "On GitHub, not in the portal" not in c.get("/dashboard").text


def test_adopting_from_the_panel_takes_it_off_the_panel(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        _seed(
            fake,
            85,
            files=[("pathways/WP554/WP554.gpml", "modified", GOOD_GPML.decode())],
        )
        app.state.worker.tick(github=fake)
        assert [p.number for p in app.state.worker.untracked.items] == [85]

        current["user"] = "curator"
        r = c.post("/api/reviews/85/adopt")
        assert r.status_code == 200 and r.json()["adopted"] is True, r.text
        assert app.state.worker.untracked.items == []


def test_an_untracked_pull_request_opening_asks_for_a_rescan(tmp_path):
    app, _ = _authed_app(tmp_path, webhook_secret="whsec")
    with TestClient(app) as c:
        app.state.worker._scan_requested = False
        _hook(c, "pull_request", {"action": "opened", "number": 99, "pull_request": {"number": 99}})
        assert app.state.worker._scan_requested is True


# -- the conversation ----------------------------------------------------------------------


def test_the_conversation_folds_bots_and_marks_curators(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        fake.create_issue_comment(REPO, pr, "Could you add the PMID?")
        fake.comment_authors["Could you add the PMID?"] = "curator"
        fake.pr_reviews[pr] = [
            ConversationItem(
                kind="review",
                author="curator",
                body="Two nodes are unmapped.",
                created_at="2026-02-01T00:00:00Z",
                state="CHANGES_REQUESTED",
            )
        ]

        items = c.get(f"/api/reviews/{pr}/conversation").json()["items"]

        mirror = next(i for i in items if MIRROR_MARKER in i["body"])
        assert mirror["role"] == "portal" and mirror["collapsed"] is True
        ask = next(i for i in items if i["body"] == "Could you add the PMID?")
        assert ask["role"] == "curator" and ask["collapsed"] is False
        verdict = next(i for i in items if i["kind"] == "review")
        assert verdict["verdict"] == "requested changes"


def test_the_conversation_is_cached_until_a_comment_arrives(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, fake=fake, webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)
        c.get(f"/api/reviews/{pr}/conversation")
        fake.calls.clear()
        c.get(f"/api/reviews/{pr}/conversation")
        assert "list_conversation" not in fake.calls

        comment = {"action": "created", "issue": {"number": pr, "pull_request": {}}}
        _hook(c, "issue_comment", comment)
        c.get(f"/api/reviews/{pr}/conversation")
        assert "list_conversation" in fake.calls


def test_the_conversation_needs_a_signed_in_viewer(tmp_path):
    app, _ = _authed_app(tmp_path)
    del app.dependency_overrides[__import__("app.main", fromlist=["x"]).get_current_user]
    with TestClient(app) as c:
        assert c.get("/api/reviews/1/conversation").status_code == 401


def test_the_submitter_and_curators_can_comment_and_nobody_else(tmp_path):
    fake = _fake()
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = _submit(c)

        assert c.post(f"/api/reviews/{pr}/comments", data={"body": "Fixed."}).status_code == 201
        current["user"] = "curator"
        assert c.post(f"/api/reviews/{pr}/comments", data={"body": "Thanks."}).status_code == 201
        current["user"] = "eve"
        assert c.post(f"/api/reviews/{pr}/comments", data={"body": "spam"}).status_code == 403
        current["user"] = "bob"
        assert c.post(f"/api/reviews/{pr}/comments", data={"body": "   "}).status_code == 422

        posted = fake.issue_comments[(REPO, pr)]
        assert "Fixed." in posted and "Thanks." in posted and "spam" not in posted
        # Posting changes nothing about the review.
        assert c.get(f"/api/reviews/{pr}").json()["status"] == "open"

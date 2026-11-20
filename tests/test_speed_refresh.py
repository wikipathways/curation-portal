"""Stream C: pages off GitHub, cards that refresh themselves, and the publish tracker.

- a curator opening a review spends no GitHub call and no raw-file read; the page asks the
  background loop instead, which handles that review first (C1, C2);
- one card can be fetched on its own, with a weak ETag that answers an unchanged card with 304
  (C3);
- the portal's own mirror comment coming back through the webhook is not news (C6);
- the mirror and the approval comment are posted after the response, still posted (C9);
- the conversation cache fetches once for concurrent readers and serves a stale copy at once (C6);
- the workflow lookup reuses the head the reconcile already read (C7);
- the publish tracker and the site probe (C4); the curator team refresh (C10); Server-Timing (C0).
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app import obs
from app.curators import GitHubTeamCurators
from app.github import ConversationItem, FakeGitHubClient, HttpGitHubClient, WorkflowRun
from app.models import Review, ReviewStatus, utcnow
from app.pipeline import DraftsReader
from app.review.conversation import ConversationCache
from app.review.service import MIRROR_MARKER, WELCOME_MARKER
from app.review.tracker import publish_pending, publish_stages
from tests.test_api import _login
from tests.test_pipeline_api import REPO, _pass_every_required_item, _pipeline_app, _submit
from tests.test_wave2_visibility import CountingFake, _hook


def _counting_fake() -> CountingFake:
    return CountingFake(default_branches={f"{REPO}#main": "basesha"})


def _site(tmp_path, seen: list[str], files: dict[str, tuple[int, bytes, dict]] | None = None):
    """A real DraftsReader over a mock site that records every request it is asked."""
    files = files or {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        for suffix, (status, body, headers) in files.items():
            if request.url.path.endswith(suffix) or request.url.host + request.url.path == suffix:
                return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(404, text="nope")

    return DraftsReader(
        repo="wikipathways/sandbox-wp.gh.io",
        branch="main",
        site_base_url="https://sandbox.wikipathways.org",
        cache_dir=str(tmp_path / "drafts-cache"),
        transport=httpx.MockTransport(handler),
    )


def _pipeline(tmp_path, *, curators=("curator",)):
    fake = _counting_fake()
    app = _pipeline_app(tmp_path, curators=curators, fake=fake)
    return app, fake


def _last_checked(app, pr):
    with app.state.session_factory() as s:
        return s.get(Review, pr).last_checked_at


# -- C2: the review page spends nothing ------------------------------------------------------


def test_a_curator_opening_a_review_makes_no_github_calls(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        seen: list[str] = []
        app.state.drafts = _site(tmp_path, seen)
        pr = _submit(c)["pr_number"]
        fake.calls.clear()
        seen.clear()

        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}")

        assert page.status_code == 200
        assert fake.calls == []  # not one GitHub call, reads included
        assert seen == []  # and not one read of the drafts site
        # It asked the loop instead, and the card says to look again shortly.
        assert app.state.worker.priority_pending() == {pr}
        assert 'data-poll-after="2"' in page.text


def test_the_tick_handles_the_requested_review_first(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "curator")
        c.get(f"/dashboard/{pr}")
        fake.calls.clear()

        result = app.state.worker.tick(github=fake)

        assert result.prioritised == 1
        assert app.state.worker.priority_pending() == set()
        assert fake.calls[0] == "get_pull_request"  # the requested review went first
        assert _last_checked(app, pr) is not None
        # Checked just now: opening it again asks for nothing.
        c.get(f"/dashboard/{pr}")
        assert app.state.worker.priority_pending() == set()


def test_a_visitor_or_a_submitter_does_not_queue_a_recheck(tmp_path):
    app, _ = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        c.get(f"/dashboard/{pr}")  # anonymous
        _login(c, "marvinm2")  # the submitter, not a curator
        c.get(f"/dashboard/{pr}")
        assert app.state.worker.priority_pending() == set()


def test_a_fresh_approval_is_due_every_ten_seconds(tmp_path):
    app, fake = _pipeline(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "marvinm2")
        _pass_every_required_item(c, pr)
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 200
        svc = app.state.worker._make_curation(fake)
        with app.state.session_factory() as s:
            r = s.get(Review, pr)
            r.last_checked_at = utcnow() - timedelta(seconds=12)
            s.commit()
            s.refresh(r)
            assert svc.is_due(r)  # 12 s is past the publishing interval
            r.status = ReviewStatus.OPEN
            assert not svc.is_due(r)  # but not past the ordinary 30 s


# -- C3: the card fragment -------------------------------------------------------------------


def test_the_card_fragment_answers_an_unchanged_card_with_304(tmp_path):
    app, _ = _pipeline(tmp_path, curators=("curator", "marvinm2"))  # the API acts as marvinm2
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "curator")
        app.state.worker.tick(github=app.state._fake)

        first = c.get(f"/dashboard/{pr}/card?expanded=1")
        assert first.status_code == 200
        assert first.text.lstrip().startswith("<article")
        assert 'data-poll-after="30"' in first.text and 'data-expanded="1"' in first.text
        assert "data-refresh-card" in first.text
        etag = first.headers["etag"]
        assert etag.startswith('W/"')
        assert "Cookie" in first.headers["vary"]
        assert first.headers["cache-control"] == "private, no-cache"
        assert first.headers["x-poll-after"] == "30"
        assert first.headers["x-checked"].startswith("Checked")

        again = c.get(f"/dashboard/{pr}/card?expanded=1", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.headers["etag"] == etag

        # A change to the review is a new body and a new tag.
        assert c.post(f"/api/reviews/{pr}/assign", data={"curator": "curator"}).status_code == 200
        changed = c.get(f"/dashboard/{pr}/card?expanded=1", headers={"If-None-Match": etag})
        assert changed.status_code == 200 and changed.headers["etag"] != etag

        assert c.get("/dashboard/4242/card").status_code == 404


def test_the_checked_time_does_not_change_the_etag(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "curator")
        app.state.worker.tick(github=fake)
        etag = c.get(f"/dashboard/{pr}/card").headers["etag"]
        with app.state.session_factory() as s:
            s.get(Review, pr).last_checked_at = utcnow() - timedelta(seconds=20)
            s.commit()
        again = c.get(f"/dashboard/{pr}/card", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.headers["x-checked"] == "Checked just now"


def test_only_a_refresh_click_asks_the_loop_and_the_card_polls_until_it_lands(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "curator")
        app.state.worker.tick(github=fake)
        with app.state.session_factory() as s:
            s.get(Review, pr).last_checked_at = None  # due again (a webhook marked it)
            s.commit()

        timer = c.get(f"/dashboard/{pr}/card")
        assert app.state.worker.priority_pending() == set()  # the timer reads the database only
        assert timer.headers["x-poll-after"] == "30"

        click = c.get(
            f"/dashboard/{pr}/card?recheck=1", headers={"If-None-Match": timer.headers["etag"]}
        )
        assert app.state.worker.priority_pending() == {pr}
        assert click.headers["x-poll-after"] == "2"
        assert click.status_code == 304  # the interval alone is not a change to the card

        app.state.worker.tick(github=fake)
        assert c.get(f"/dashboard/{pr}/card").headers["x-poll-after"] == "30"


def test_the_queue_card_and_the_review_page_carry_the_refresh_hooks(tmp_path):
    app, _ = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        _submit(c)
        _login(c, "curator")
        queue = c.get("/dashboard").text
    assert 'data-refresh-page' in queue
    assert 'data-queue-status="open"' in queue
    assert 'data-tour="refresh"' in queue


# -- C4: the publish tracker -----------------------------------------------------------------


def _review(**kw):
    base = {
        "status": "approved",
        "approved_at": datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
        "approved_by": "curator",
        "published_at": None,
        "wpid": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


NOW = datetime(2026, 11, 20, 12, 2, tzinfo=UTC)


def test_the_tracker_is_empty_before_approval():
    assert publish_stages(_review(status="open"), None, NOW) == []
    assert publish_stages(_review(approved_at=None), None, NOW) == []


def test_the_tracker_shows_a_publish_run_that_started_after_the_approval():
    run = {"publish": {"status": "in_progress", "conclusion": None, "url": "u",
                       "created_at": "2026-11-20T12:01:00Z"}}
    stages = {s["key"]: s for s in publish_stages(_review(), run, NOW)}
    assert [s for s in stages] == ["accepted", "workflow", "published", "live"]
    assert stages["accepted"]["state"] == "done"
    assert stages["workflow"]["state"] == "active"
    assert stages["workflow"]["url"] == "u"
    assert stages["published"]["state"] == "waiting"
    assert publish_pending(_review(), run, NOW)


def test_the_tracker_ignores_a_run_from_before_the_approval():
    # It belongs to another pathway, whatever it says.
    run = {"publish": {"status": "completed", "conclusion": "failure", "url": "u",
                       "created_at": "2026-11-20T11:00:00Z"}}
    stages = {s["key"]: s for s in publish_stages(_review(), run, NOW)}
    assert stages["workflow"]["state"] == "waiting"
    assert stages["workflow"]["url"] is None


def test_the_tracker_follows_a_publication_to_the_live_site():
    published = _review(status="published", wpid=5430,
                        published_at=datetime(2026, 11, 20, 12, 1, tzinfo=UTC))
    stages = {s["key"]: s for s in publish_stages(published, {}, NOW)}
    assert stages["workflow"]["state"] == "done"
    assert stages["published"]["state"] == "done" and "WP5430" in stages["published"]["detail"]
    assert stages["live"]["state"] == "active"
    live = {s["key"]: s for s in publish_stages(published, {"live_at": NOW.isoformat()}, NOW)}
    assert live["live"]["state"] == "done"
    assert not publish_pending(published, {"live_at": NOW.isoformat()}, NOW)


def test_the_tracker_says_when_a_publication_failed():
    stages = {s["key"]: s for s in publish_stages(_review(status="publish_failed"), {}, NOW)}
    assert stages["workflow"]["state"] == "failed"
    assert stages["published"]["state"] == "failed"


def test_a_stalled_publication_stops_polling_after_half_an_hour():
    later = NOW + timedelta(hours=2)
    assert not publish_pending(_review(), {}, later)


def test_an_approved_review_shows_the_tracker_and_polls_fast(tmp_path):
    app, fake = _pipeline(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "marvinm2")
        _pass_every_required_item(c, pr)
        c.post(f"/api/reviews/{pr}/approve")
        app.state.worker.tick(github=fake)
        card = c.get(f"/dashboard/{pr}/card?expanded=1")
    assert "On its way to the site" in card.text
    assert 'data-tour="tracker"' in card.text
    assert card.headers["x-poll-after"] in ("2", "5")


def test_the_loop_records_when_a_published_page_is_up(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        seen: list[str] = []
        app.state.drafts = _site(
            tmp_path, seen, {"/pathways/WP5430": (200, b"", {})}
        )
        pr = _submit(c)["pr_number"]
        with app.state.session_factory() as s:
            r = s.get(Review, pr)
            r.status, r.wpid, r.published_at = ReviewStatus.PUBLISHED, 5430, utcnow()
            s.commit()
        result = app.state.worker.tick(github=fake)
        assert result.live == 1
        assert "HEAD /pathways/WP5430" in seen
        with app.state.session_factory() as s:
            assert s.get(Review, pr).pipeline_run["live_at"]
        # Once seen live it is not asked again.
        seen.clear()
        app.state.worker.tick(github=fake)
        assert "HEAD /pathways/WP5430" not in seen


def test_page_live_needs_a_newer_last_modified_for_an_update(tmp_path):
    since = datetime(2026, 11, 20, 12, 0, 30, tzinfo=UTC)
    old = {"Last-Modified": "Thu, 19 Nov 2026 10:00:00 GMT"}
    new = {"Last-Modified": "Fri, 20 Nov 2026 12:03:00 GMT"}
    for headers, expect in ((old, False), (new, True), ({}, None)):
        reader = _site(tmp_path, [], {"/pathways/WP7": (200, b"", headers)})
        assert reader.page_live(7, since) is expect
    assert _site(tmp_path, []).page_live(7, since) is False  # 404
    assert _site(tmp_path, [], {"/pathways/WP7": (200, b"", {})}).page_live(7) is True


# -- C1: drafts off the request path -----------------------------------------------------------


def test_cached_never_touches_the_network_and_survives_a_failed_refresh(tmp_path):
    seen: list[str] = []
    clock = {"t": 1000.0}
    state = {"up": True}
    info = b'{"wpid": "WP0__PR3", "title": "T", "description": "D"}'

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if not state["up"]:
            return httpx.Response(503)
        if request.url.path.endswith("-info.json"):
            return httpx.Response(200, content=info)
        return httpx.Response(404)

    reader = DraftsReader(
        repo="o/site", branch="main", site_base_url="https://s", cache_dir=tmp_path / "c",
        ttl_seconds=60, transport=httpx.MockTransport(handler), clock=lambda: clock["t"],
    )
    assert reader.cached("WP0__PR3").available is False and seen == []
    assert reader.fetch("WP0__PR3").available is True
    n = len(seen)
    clock["t"] += 3600  # long past the TTL
    assert reader.cached("WP0__PR3").available is True  # disk, however old
    assert len(seen) == n
    state["up"] = False
    assert reader.fetch("WP0__PR3").available is True  # the last good answer stands
    n = len(seen)
    assert reader.fetch("WP0__PR3").available is True
    assert len(seen) == n  # backing off: not asked again for two minutes


def test_the_pipeline_svg_is_cached_by_the_loop_and_served_same_origin(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        info = b'{"wpid": "WP0__PR1", "title": "Mitophagy", "description": "A pathway."}'
        seen: list[str] = []
        app.state.drafts = _site(
            tmp_path,
            seen,
            {"-info.json": (200, info, {}), "WP0__PR1.svg": (200, svg, {})},
        )
        pr = _submit(c)["pr_number"]
        assert pr == 1
        _login(c, "curator")
        assert c.get(f"/previews/{pr}/pipeline.svg").status_code == 404  # not cached yet
        assert "/previews/1/pipeline.svg" not in c.get(f"/dashboard/{pr}").text

        app.state.worker.tick(github=fake)

        resp = c.get(f"/previews/{pr}/pipeline.svg")
        assert resp.status_code == 200
        assert resp.content == svg
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "sandbox" in resp.headers["content-security-policy"]
        assert "/previews/1/pipeline.svg" in c.get(f"/dashboard/{pr}").text
        # The ordinary sides still answer at their own route.
        assert c.get(f"/previews/{pr}/after.svg").status_code == 200
        assert c.get("/previews/999/pipeline.svg").status_code == 404


def test_a_published_review_reads_no_drafts_at_all(tmp_path, monkeypatch):
    app, _ = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        with app.state.session_factory() as s:
            r = s.get(Review, pr)
            r.status, r.wpid = ReviewStatus.PUBLISHED, 5430
            s.commit()

        def boom(slug):
            raise AssertionError("a finished review must not read the drafts cache")

        monkeypatch.setattr(app.state.drafts, "cached", boom)
        _login(c, "curator")
        assert c.get(f"/dashboard/{pr}").status_code == 200


# -- C6: the webhook does not chase the portal's own comments ----------------------------------


def _comment_hook(c, pr, *, body, login, kind):
    return _hook(
        c,
        "issue_comment",
        {
            "action": "edited",
            "issue": {"number": pr, "pull_request": {}},
            "comment": {"body": body},
            "sender": {"login": login, "type": kind},
        },
    )


def test_the_portals_own_mirror_coming_back_is_not_news(tmp_path):
    from tests.test_api import _authed_app

    fake = CountingFake(default_branches={"wikipathways/wikipathways-database#main": "basesha"})
    app, current = _authed_app(tmp_path, fake=fake, webhook_secret="whsec")
    with TestClient(app) as c:
        current["user"] = "bob"
        pr = c.post(
            "/api/submit",
            files={"file": ("u.gpml", __import__("io").BytesIO(_good()), "application/xml")},
        ).json()["pr_number"]
        app.state.worker.tick(github=fake)
        checked = _last_checked(app, pr)
        assert checked is not None

        r = _comment_hook(c, pr, body=f"{MIRROR_MARKER}\nstatus", login="portal-bot[bot]",
                          kind="Bot")
        assert r.json() == {"ok": True, "ignored": "portal-comment"}
        r = _comment_hook(c, pr, body=f"{WELCOME_MARKER} hi", login="portal-bot[bot]",
                          kind="Bot")
        assert r.json()["ignored"] == "portal-comment"
        assert _last_checked(app, pr) == checked

        # A person quoting the marker is still a person commenting.
        r = _comment_hook(c, pr, body=f"see {MIRROR_MARKER}", login="egonw", kind="User")
        assert r.json()["marked"] == 1
        app.state.worker.tick(github=fake)
        # And the repository's publish marker, from github-actions[bot], is not a portal marker.
        r = _comment_hook(
            c, pr, body='<!-- wikipathways-publish {"status": "published"} -->',
            login="github-actions[bot]", kind="Bot",
        )
        assert r.json()["marked"] == 1


def _good() -> bytes:
    from tests.test_api import GOOD_GPML

    return GOOD_GPML


# -- C9: the mirror after the response -------------------------------------------------------


def test_actions_defer_the_mirror_and_still_post_it(tmp_path):
    app, fake = _pipeline(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _login(c, "marvinm2")
        _pass_every_required_item(c, pr)
        assert c.post(f"/api/reviews/{pr}/approve").status_code == 200
    mirror = fake.comments[(REPO, pr)][MIRROR_MARKER]
    assert "approved" in mirror.lower()
    assert any("approved this pathway" in b for b in fake.issue_comments[(REPO, pr)])


def test_a_deferred_mirror_is_scheduled_once_and_posts_the_final_state(tmp_path):
    app, fake = _pipeline(tmp_path, curators=("marvinm2",))
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        _pass_every_required_item(c, pr)
        queued: list = []
        svc = app.state.worker._make_curation(fake)
        svc._defer = lambda fn, *a: queued.append((fn, a))
        fake.calls.clear()
        svc.assign(pr, "marvinm2", actor="marvinm2")
        svc.approve(pr, "marvinm2")
        assert "upsert_issue_comment" not in fake.calls
        assert "create_issue_comment" not in fake.calls
        mirrors = [q for q in queued if q[0].__name__ == "_mirror_by_number"]
        assert len(mirrors) == 1  # two changes, one mirror
        for fn, args in queued:
            fn(*args)
        assert "approved" in fake.comments[(REPO, pr)][MIRROR_MARKER].lower()
        assert fake.calls.count("upsert_issue_comment") == 1


def test_pipeline_checks_are_written_in_one_go(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        info = b'{"wpid": "WP0__PR1", "title": "Mitophagy", "description": "A pathway."}'
        app.state.drafts = _site(
            tmp_path,
            [],
            {
                "-info.json": (200, info, {}),
                "-datanodes.tsv": (200, b"Label\tIdentifier\nIRS1\t3667\n", {}),
            },
        )
        pr = _submit(c)["pr_number"]
        with app.state.session_factory() as s:
            version = s.get(Review, pr).version
        fake.calls.clear()
        svc = app.state.worker._make_curation(fake)
        assert svc.refresh_pipeline_checks(pr) is not None
        with app.state.session_factory() as s:
            assert s.get(Review, pr).version == version + 1  # several items, one write
        assert fake.calls.count("upsert_issue_comment") == 1  # and one mirror


# -- C6: the conversation cache --------------------------------------------------------------


def _item(body="hi") -> ConversationItem:
    return ConversationItem(
        kind="comment", author="egonw", body=body, created_at="2026-11-20T12:00:00Z",
        html_url="https://github.com/x", state=None, path=None,
    )


def test_two_readers_share_one_fetch():
    cache = ConversationCache(ttl_seconds=60)
    calls = {"n": 0}
    started = threading.Event()

    def fetch():
        calls["n"] += 1
        started.set()
        time.sleep(0.2)
        return [_item()]

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get(7, fetch)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert results[0] is results[1]


def test_a_stale_conversation_is_served_at_once_and_refreshed():
    clock = {"t": 0.0}
    cache = ConversationCache(ttl_seconds=60, clock=lambda: clock["t"], max_stale_seconds=600)
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [_item(str(calls["n"]))]

    entry, stale = cache.get_or_stale(1, fetch)
    assert (stale, calls["n"]) == (False, 1)
    clock["t"] = 120
    entry, stale = cache.get_or_stale(1, fetch)
    assert stale is True and calls["n"] == 1 and entry.items[0].body == "1"
    cache.refresh(1, fetch, only_if_older_than=cache.ttl_seconds)
    assert calls["n"] == 2
    cache.refresh(1, fetch, only_if_older_than=cache.ttl_seconds)  # someone else just did
    assert calls["n"] == 2
    clock["t"] = 2000  # too old even to serve stale
    entry, stale = cache.get_or_stale(1, fetch)
    assert stale is False and calls["n"] == 3


def test_the_conversation_route_says_stale_and_refreshes_after_answering(tmp_path):
    app, fake = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        clock = {"t": 0.0}
        app.state.conversations = ConversationCache(ttl_seconds=60, clock=lambda: clock["t"])
        assert c.get(f"/api/reviews/{pr}/conversation").json()["stale"] is False
        clock["t"] = 90
        fake.calls.clear()
        body = c.get(f"/api/reviews/{pr}/conversation").json()
        assert body["stale"] is True
        # The background task ran after the answer: the copy is fresh again.
        assert "list_conversation" in fake.calls
        assert c.get(f"/api/reviews/{pr}/conversation").json()["stale"] is False


# -- C7: one GET fewer per reconcile -----------------------------------------------------------


def test_the_workflow_lookup_reuses_a_known_head():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"workflow_runs": []})

    gh = HttpGitHubClient("t", transport=httpx.MockTransport(handler))
    assert gh.latest_workflow_run_for_pr("o/r", 5, workflow_file="w.yml", head_sha="abc") is None
    assert seen == ["/repos/o/r/actions/workflows/w.yml/runs"]


def test_reconcile_passes_the_head_it_already_read(tmp_path):
    class Spy(CountingFake):
        heads: list = []

        def latest_workflow_run_for_pr(self, repo, pr_number, *, workflow_file, head_sha=None):
            Spy.heads.append(head_sha)
            return super().latest_workflow_run_for_pr(
                repo, pr_number, workflow_file=workflow_file, head_sha=head_sha
            )

    fake = Spy(default_branches={f"{REPO}#main": "basesha"})
    app = _pipeline_app(tmp_path, fake=fake)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        app.state.worker.tick(github=fake)
        with app.state.session_factory() as s:
            head = s.get(Review, pr).head_sha
    assert Spy.heads and Spy.heads[-1] == head


# -- C10: the curator team, off the request path -----------------------------------------------


def test_an_expired_team_is_served_as_it_stands_and_refreshed_by_the_loop():
    gh = FakeGitHubClient(team_members={"org/cur": ["alice"]})
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return gh

    clock = {"t": 0.0}
    reg = GitHubTeamCurators(
        provider, "org", "cur", ttl=timedelta(minutes=5), clock=lambda: clock["t"],
        background=True,
    )
    assert reg.members() == {"alice"} and calls["n"] == 1  # a cold start reads inline
    gh.team_members["org/cur"] = ["alice", "bob"]
    clock["t"] = 3600
    assert reg.members() == {"alice"} and calls["n"] == 1  # stale, but no request waits
    assert reg.refresh_if_stale() is True
    assert reg.members() == {"alice", "bob"} and calls["n"] == 2
    assert reg.refresh_if_stale() is False


def test_a_team_that_cannot_be_read_is_not_retried_by_every_request():
    failing = FakeGitHubClient(fail_on={"list_team_members"})
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return failing

    reg = GitHubTeamCurators(provider, "org", "cur", background=True, clock=lambda: 0.0)
    assert reg.members() == set()
    assert reg.members() == set()
    assert calls["n"] == 1


# -- C0: Server-Timing ----------------------------------------------------------------------


def test_every_response_says_where_its_time_went(tmp_path):
    app, _ = _pipeline(tmp_path)
    with TestClient(app) as c:
        app.state.drafts = _site(tmp_path, [])
        pr = _submit(c)["pr_number"]
        health = c.get("/health")
        assert health.headers["server-timing"].startswith("total;dur=")
        page = c.get(f"/dashboard/{pr}")
        timing = page.headers["server-timing"]
        assert 'db;desc="SQL x' in timing
        assert 'disk;desc="render cache x' in timing
        assert "gh;" not in timing


def test_instrumented_clients_count_into_the_current_request():
    client = obs.instrument_httpx(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))), "gh"
    )
    with obs.collecting() as stats:
        client.get("https://api.github.com/x")
        client.get("https://api.github.com/y")
        with obs.timed("disk"):
            pass
    assert stats.counts["gh"] == 2 and stats.counts["disk"] == 1
    assert 'gh;desc="GitHub x2"' in stats.server_timing()
    assert obs.current() is None


def test_a_run_record_carries_when_it_started():
    run = WorkflowRun(id=1, html_url="u", status="queued", conclusion=None,
                      created_at="2026-11-20T12:00:00Z")
    assert run.created_at

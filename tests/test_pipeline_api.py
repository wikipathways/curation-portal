"""The HTTP surface in pipeline mode: submitting into a repo that publishes via its own Actions."""
from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.github import FakeGitHubClient
from app.main import (
    build_app,
    get_bot_client,
    get_bot_optional,
    get_current_user,
    get_github_client,
)
from app.models import Review, ReviewStatus
from app.review.service import MIRROR_MARKER
from tests.test_api import _login

REPO = "wikipathways/sandbox-wp-db"

GOOD_GPML = (
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    b'Organism="Homo sapiens" Version="WP5636_r20260520113005"></Pathway>'
)


def _pipeline_app(tmp_path, *, curators=(), fake=None):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        content_repo=REPO,
        publish_mode="pipeline",
        submit_identity="bot",
        # The target repo has no pr-preview.yml; leaving this on would 409 every approval.
        require_preview_check=False,
        curators=list(curators),
        preview_cache_dir=str(tmp_path / "preview-cache"),
    )
    app = build_app(settings)
    fake = fake or FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "marvinm2"
    app.state._fake = fake
    return app


@pytest.fixture
def pipeline_client(tmp_path):
    app = _pipeline_app(tmp_path)
    with TestClient(app) as c:
        yield c


def _submit(client) -> dict:
    resp = client.post(
        "/api/submit",
        files={"file": ("mito.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        data={"description": "A first pass at mitophagy."},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_submit_reports_the_placeholder_not_an_assigned_id(pipeline_client):
    body = _submit(pipeline_client)

    assert body["wpid"] == "WP0001"
    assert body["path"] == "pathways/WP0001/WP0001.gpml"
    assert body["pr_number"] == 1


def test_submit_records_the_branch_and_no_wpid(pipeline_client, tmp_path):
    body = _submit(pipeline_client)
    factory = pipeline_client.app.state.session_factory

    with factory() as s:
        review = s.get(Review, body["pr_number"])
    assert review.wpid is None
    assert review.wpid_str == "WP0001 (unassigned)"
    assert review.head_branch.startswith("WP0001_marvinm2_")


def test_submit_labels_the_pr_with_the_repos_own_vocabulary(pipeline_client):
    body = _submit(pipeline_client)
    fake = pipeline_client.app.state._fake

    assert fake.list_labels(REPO, body["pr_number"]) == ["new pathway submission"]


def test_a_label_failure_does_not_cost_the_submission(tmp_path):
    # Descriptive labels are best-effort; only `accepted` is load-bearing.
    fake = FakeGitHubClient(
        default_branches={f"{REPO}#main": "basesha"}, fail_on={"add_labels"}
    )
    with TestClient(_pipeline_app(tmp_path, fake=fake)) as client:
        body = _submit(client)

    assert body["pr_number"] == 1
    assert fake.list_labels(REPO, 1) == []


def test_validate_advertises_the_placeholder_path(pipeline_client):
    resp = pipeline_client.post(
        "/api/validate",
        files={"file": ("mito.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )

    assert resp.json()["will_layout_to"] == "pathways/WP0001/WP0001.gpml"


def test_the_queue_shows_a_submission_with_no_wpid(pipeline_client):
    _submit(pipeline_client)
    rows = pipeline_client.get("/api/reviews").json()

    assert len(rows) == 1
    assert rows[0]["wpid"] is None


def test_mirror_comment_names_the_placeholder(pipeline_client):
    body = _submit(pipeline_client)
    fake = pipeline_client.app.state._fake

    comment = fake.comments[(REPO, body["pr_number"])][MIRROR_MARKER]
    assert "WP0001 (unassigned)" in comment


def test_a_failed_pipeline_run_is_reported_in_the_queue(pipeline_client):
    # The submitter loses their metadata tables and draft page when the target repo cannot read
    # the GPML, and the only other trace is a job buried in the Actions tab.
    _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion="failure")
    # The background loop records the run; the queue renders what it recorded.
    pipeline_client.app.state.worker.tick(github=fake)

    _login(pipeline_client, "marvinm2")
    page = pipeline_client.get("/dashboard").text

    assert "could not process this pathway" in page
    assert "See the failing run" in page
    # And it says the portal's own diagram still works, so a curator does not read the failure
    # as "there is nothing to review".
    assert "drawn by this portal and is unaffected" in page


def test_a_successful_pipeline_run_shows_no_warning(pipeline_client):
    _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion="success")

    _login(pipeline_client, "marvinm2")
    page = pipeline_client.get("/dashboard").text

    assert "could not process this pathway" not in page


def test_a_still_running_pipeline_shows_no_warning(pipeline_client):
    # in_progress has conclusion None; treating that as a failure would cry wolf on every
    # submission for the ten minutes the repository takes to process it.
    _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion=None)

    _login(pipeline_client, "marvinm2")
    page = pipeline_client.get("/dashboard").text

    assert "could not process this pathway" not in page


def test_the_review_page_names_the_target_repo_in_the_notice(pipeline_client):
    # The card macro is imported into review_detail.html, and an imported macro sees none of the
    # calling template's context without `with context` — so this rendered "could not process
    # this pathway" with the repo name silently missing, but only on the detail page.
    body = _submit(pipeline_client)
    fake = pipeline_client.app.state._fake
    fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion="failure")
    pipeline_client.app.state.worker.tick(github=fake)
    _login(pipeline_client, "marvinm2")

    page = pipeline_client.get(f"/dashboard/{body['pr_number']}").text

    assert f"{REPO} could not process this pathway" in page


def test_revise_commits_onto_the_same_pr_without_a_wpid(pipeline_client):
    # The revise route is keyed by pull request, not WPID: in pipeline mode a new submission has
    # no id until the target repo publishes it, so there is nothing to look it up by.
    body = _submit(pipeline_client)
    pr, branch = body["pr_number"], None
    factory = pipeline_client.app.state.session_factory
    with factory() as s:
        branch = s.get(Review, pr).head_branch

    revised = pipeline_client.post(
        f"/api/reviews/{pr}/revise",
        files={"file": ("mito.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
        data={"description": "added identifiers"},
    )

    assert revised.status_code == 201, revised.text
    assert revised.json()["pr_number"] == pr  # same PR, no second one
    assert revised.json()["wpid"] == "WP0001"
    fake = pipeline_client.app.state._fake
    # Committed onto the branch recorded at submission, which carries a timestamp and could not
    # have been derived.
    content, message, _ = fake.files[(REPO, branch, "pathways/WP0001/WP0001.gpml")]
    assert message == "Revise WP0001"
    assert 'Version="WP0001_r' in content


def test_revise_refuses_an_update_review(pipeline_client):
    body = _submit(pipeline_client)
    pr = body["pr_number"]
    factory = pipeline_client.app.state.session_factory
    with factory() as s:
        s.get(Review, pr).kind = "update"
        s.commit()

    resp = pipeline_client.post(
        f"/api/reviews/{pr}/revise",
        files={"file": ("mito.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    )

    assert resp.status_code == 409


def test_the_approve_button_does_not_promise_a_merge(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        body = _submit(client)
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{body['pr_number']}").text

    assert "Approve for publication" in page
    assert "Approve &amp; merge" not in page


def test_the_review_page_offers_a_revision_upload(tmp_path):
    # Without this the loop has no closing move: a curator asks for changes and the submitter
    # has nowhere in the portal to answer.
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        body = _submit(client)
        pr = body["pr_number"]
        client.post(f"/api/reviews/{pr}/request-changes", data={"note": "annotate IRS1"})
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text

    assert 'class="revise-file"' in page
    assert "Commit onto this pull request" in page


# ---------------------------------------------------------------------------------------------
# The whole loop, over HTTP. Every piece of this was covered somewhere — the service tests drive
# CurationService directly, the API tests stop at submit — but the sequence a curator actually
# performs had never run end to end through the app.


def _pass_every_required_item(client, pr: int) -> None:
    detail = client.get(f"/api/reviews/{pr}").json()
    for item in detail["checklist"]:
        if item["required"]:
            resp = client.post(
                f"/api/reviews/{pr}/checklist", data={"key": item["key"], "state": "pass"}
            )
            assert resp.status_code == 200, resp.text


def test_the_whole_lifecycle_from_submission_to_published(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)

        approved = client.post(f"/api/reviews/{pr}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        # The label *is* the approval: it is what the repository's dispatcher reacts to.
        assert "accepted" in fake.list_labels(REPO, pr)

        # The repository publishes, announces the id it assigned, and closes without merging.
        fake.simulate_3a(REPO, pr, wpid=5678)
        app.state.worker.tick(github=fake)
        page = client.get("/dashboard?status=published")
        assert page.status_code == 200

        detail = client.get(f"/api/reviews/{pr}").json()
        assert detail["status"] == "published"
        assert detail["wpid"] == 5678


def test_a_publication_that_never_happened_is_reachable_and_recoverable(tmp_path):
    """The failure this repository has actually shown: the pull request closes and nothing was
    said. It has to be findable in the queue and a curator has to be able to close it out."""
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)
        client.post(f"/api/reviews/{pr}/approve")

        fake.simulate_3a(REPO, pr, wpid=None)  # closed, nothing announced
        app.state.worker.tick(github=fake)
        queue = client.get("/dashboard?status=publish_failed")
        assert f"/dashboard/{pr}" in queue.text  # it has a tab and the card is on it

        assert client.get(f"/api/reviews/{pr}").json()["status"] == "publish_failed"

        recorded = client.post(f"/api/reviews/{pr}/published-wpid", data={"wpid": 5678})
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()["status"] == "published"
        assert recorded.json()["wpid"] == 5678


def test_rejecting_hands_the_pr_to_the_repositorys_rejection_workflow(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")

        resp = client.post(f"/api/reviews/{pr}/reject", data={"note": "duplicate of WP554"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
        assert "rejected" in fake.list_labels(REPO, pr)
        # The reason goes on the record before the label that triggers the workflow, because
        # that workflow closes the pull request.
        assert any("duplicate of WP554" in b for b in fake.issue_comments[(REPO, pr)])


def test_a_decided_review_cannot_be_decided_again(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)
        client.post(f"/api/reviews/{pr}/approve")

        assert client.post(f"/api/reviews/{pr}/approve").status_code == 409
        assert client.post(f"/api/reviews/{pr}/revise",
                           files={"file": ("m.gpml", io.BytesIO(GOOD_GPML), "application/xml")}
                           ).status_code == 409


def test_the_queue_offers_a_tab_for_every_state_a_review_can_reach(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        _submit(client)
        _login(client, "marvinm2")
        page = client.get("/dashboard").text

    for status in ("open", "changes_requested", "approved", "published", "publish_failed",
                   "rejected", "closed"):
        assert f"/dashboard?status={status}" in page, status
    # Nothing merges here, so a Merged tab would only ever be empty.
    assert "/dashboard?status=merged" not in page


def test_a_submitter_can_find_their_own_submission_without_a_wpid(tmp_path):
    app = _pipeline_app(tmp_path)  # not a curator
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")

        mine = client.get("/dashboard?mine=1")

        assert mine.status_code == 200
        assert f"/dashboard/{pr}" in mine.text
        assert "Your submissions" in mine.text


def test_a_submitter_who_is_not_a_curator_can_answer_a_change_request(tmp_path):
    app = _pipeline_app(tmp_path, curators=["someone-else"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        with app.state.session_factory() as s:
            s.get(Review, pr).status = "changes_requested"
            s.commit()
        _login(client, "marvinm2")

        page = client.get(f"/dashboard/{pr}").text

    assert 'class="revise-file"' in page  # the upload control, not just an instruction
    assert "WPNone" not in page  # a new pathway has no id to name


def test_the_placeholder_id_cannot_address_a_real_pathway(pipeline_client):
    """WP0001 is what a submission carries before it has an id. Stripping the leading zeros
    would send the upload to WP1, a real and unrelated pathway."""
    assert pipeline_client.get("/api/pathways/0001").status_code == 422
    assert pipeline_client.post(
        "/api/pathways/0001/update",
        files={"file": ("m.gpml", io.BytesIO(GOOD_GPML), "application/xml")},
    ).status_code == 422


def test_the_repositorys_own_artifacts_are_shown_when_it_produced_any(tmp_path, monkeypatch):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]

        # Stand in for the sister site repo the pipeline pushes its drafts into.
        from app.pipeline.drafts import DraftArtifacts

        slug = f"WP0__PR{pr}"
        app.state.drafts.fetch = lambda s: DraftArtifacts(  # type: ignore[assignment]
            slug=slug,
            available=True,
            datanodes=[{"Label": "IRS1", "Identifier": "3667"}],
            bibliography=[{"Citation": "x"}],
            info={"wpid": slug, "title": "Mitophagy", "description": "A pathway."},
            draft_url=f"https://sandbox.wikipathways.org/drafts/{slug}",
            svg_url=f"https://raw.example/{slug}.svg",
            thumb_url=None,
        )
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text

    assert "1 data nodes annotated" in page
    assert f"https://sandbox.wikipathways.org/drafts/{slug}" in page


def test_an_update_in_changes_requested_names_a_route_that_works(tmp_path):
    """The revise endpoint refuses updates, so pointing an update's submitter at the review page
    points them at nothing. They have to be sent back to the update form with their WPID."""
    app = _pipeline_app(tmp_path, curators=["someone-else"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        with app.state.session_factory() as s:
            r = s.get(Review, pr)
            r.kind, r.wpid, r.status = "update", 5636, "changes_requested"
            s.commit()
        _login(client, "marvinm2")

        page = client.get(f"/dashboard/{pr}").text

    assert "/?wpid=WP5636" in page
    assert f'href="/dashboard/{pr}">Open the full review' not in page  # not a link to itself


def test_the_queue_card_does_not_promise_an_upload_field_it_does_not_have(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        client_login = _login(client, "marvinm2")  # noqa: F841
        client.post(f"/api/reviews/{pr}/request-changes", data={"note": "annotate IRS1"})

        queue = client.get("/dashboard?status=changes_requested").text
        detail = client.get(f"/dashboard/{pr}").text

    assert "Upload the fixed GPML below" not in queue  # the field is only on the detail page
    assert "Upload a revision &rarr;" in queue or "Upload a revision →" in queue
    assert "Upload the fixed GPML below" in detail
    assert 'class="revise-file"' in detail


def test_a_publish_failure_still_offers_the_curator_something_to_do(tmp_path):
    """Every approval against the live target lands here, so a card with nothing on it but a
    form asking for an identifier that does not exist is the whole dashboard, most of the time."""
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)
        client.post(f"/api/reviews/{pr}/approve")
        fake.simulate_3a(REPO, pr, wpid=None)
        client.get("/dashboard?status=publish_failed")

        page = client.get(f"/dashboard/{pr}").text

    assert "Record the published WPID" in page
    assert "btn--reject" in page
    assert "btn--changes" in page
    assert "Approve for publication" in page  # re-approving re-fires the repository's dispatcher


# ---------------------------------------------------------------------------------------------
# The link to the target repo's own pathway page, under the diagram.


def _offline_drafts(app, tmp_path):
    """Point the app's reader at a site that serves nothing, over a mock transport.

    Deliberately a real `DraftsReader` rather than a stub: what is under test here is the wiring
    and the template, and the published URL has to come out of the same code the deployment uses.
    Serving nothing is also the honest shape of a published review — publication *moves* the
    drafts, so by then every draft file really is a 404.
    """
    from app.pipeline.drafts import DraftsReader

    app.state.drafts = DraftsReader(
        repo="wikipathways/sandbox-wp.gh.io",
        branch="main",
        site_base_url="https://sandbox.wikipathways.org",
        cache_dir=str(tmp_path / "drafts-cache"),
        transport=httpx.MockTransport(lambda req: httpx.Response(404, text="nope")),
    )


def _publish(client, pr: int, wpid: int) -> None:
    factory = client.app.state.session_factory
    with factory() as s:
        review = s.get(Review, pr)
        review.status = ReviewStatus.PUBLISHED
        review.wpid = wpid
        s.commit()


def test_a_published_review_links_to_the_finished_pathway_page(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        _offline_drafts(app, tmp_path)
        pr = _submit(client)["pr_number"]
        _publish(client, pr, 5423)
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text

    assert "https://sandbox.wikipathways.org/pathways/WP5423" in page
    assert "See the published pathway page" in page
    # The draft is gone by now, so offering it would be a link to a 404.
    assert "See the full draft page" not in page


def test_an_open_review_does_not_offer_a_published_page(tmp_path):
    """The finished page does not exist yet, and a link to a 404 is worse than no link."""
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        _offline_drafts(app, tmp_path)
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text

    assert "See the published pathway page" not in page
    assert "/pathways/WP" not in page


def test_a_published_review_with_no_wpid_offers_no_page_link(tmp_path):
    """PUBLISHED without an id is reachable — a marker comment that parsed but carried no wpid.

    Building the URL anyway yields `/pathways/WPNone`, which is a confident link to nothing.
    """
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        _offline_drafts(app, tmp_path)
        pr = _submit(client)["pr_number"]
        factory = client.app.state.session_factory
        with factory() as s:
            s.get(Review, pr).status = ReviewStatus.PUBLISHED
            s.commit()
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text

    assert "WPNone" not in page
    assert "See the published pathway page" not in page


def test_a_publish_workflow_that_never_started_is_visible_on_the_card(tmp_path):
    """The one publication failure nothing else in the app can see.

    When the publish workflow fails *while running* it posts its own marker comment and the review
    settles from that. When it fails to **start** — a malformed expression fails the whole workflow
    at parse time, which happened on 2026-07-30 — nothing is posted, nothing is labelled, and the
    review sits approved until the timeout with no trace anywhere the portal looks. Its runs carry
    no pull-request reference (the label dispatcher starts them with `workflow_dispatch`), so the
    most recent run in the repository is the only thing there is to show, and the card says so.
    """
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)
        client.post(f"/api/reviews/{pr}/approve")
        # Approved, and the repository's publish workflow died without announcing anything.
        fake.record_workflow_run(REPO, "3a_approved_pull_request.yml", conclusion="failure")
        page = client.get(f"/dashboard/{pr}").text

    assert "The last publish run in" in page
    assert "may belong to another pathway" in page  # hedged, because it cannot be tied to this PR


def test_a_successful_publish_run_is_not_reported_as_a_problem(tmp_path):
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        _pass_every_required_item(client, pr)
        client.post(f"/api/reviews/{pr}/approve")
        fake.record_workflow_run(REPO, "3a_approved_pull_request.yml", conclusion="success")
        page = client.get(f"/dashboard/{pr}").text

    assert "The last publish run in" not in page


def test_the_repositorys_own_verdict_is_shown_beside_the_apps_prediction(tmp_path):
    """The point of porting its three tests: two answers to the same question, side by side.

    The app answers before the pull request exists, from the uploaded GPML. The repository answers
    after its run, from its own diff. Where they disagree the ported thresholds have fallen behind,
    and showing both is the only way that is visible rather than silently wrong.
    """
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        fake = app.state._fake
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        # What workflow 1's `testing` job posts once the staged marker step is merged.
        fake.issue_comments.setdefault((REPO, pr), []).append(
            f'<!-- wikipathways-testing {{"pr": {pr}, "title": "review",'
            ' "description": "review", "nodes": "review"} -->'
        )
        fake.record_workflow_run(REPO, "1_on_pull_request.yml", conclusion="success")
        page = client.get(f"/dashboard/{pr}").text

    assert f"{REPO} said: review required" in page


def test_no_marker_means_the_card_shows_only_what_the_app_measured(tmp_path):
    """The ordinary case today — that workflow change is staged, not proposed."""
    app = _pipeline_app(tmp_path, curators=["marvinm2"])
    with TestClient(app) as client:
        pr = _submit(client)["pr_number"]
        _login(client, "marvinm2")
        page = client.get(f"/dashboard/{pr}").text

    assert "said: review required" not in page
    assert "Automated checks" in page  # the app's own findings are there regardless

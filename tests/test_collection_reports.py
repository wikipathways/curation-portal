"""The database's own per-pathway report, shown beside the portal's rules on an update."""
from __future__ import annotations

import httpx

from app.pipeline.collection import CollectionReports, parse_report

#: Shortened from reports/WP554.md as published on 2026-11-12.
WP554 = """---
title: WP554 curation report
---
# WikiPathways WP554

* Species: Homo sapiens
## Tests
* CASMetabolitesTests: all 2 tests OK!
* DataNodesTests
    * dataNodesWithoutIdentifier: .x we found 1 problem(s):
        * [The following DataNodes have no identifier: 2](#d2d32fa1)
    * unknownTypes: .. all OK!
* EnsemblTests: all 4 tests OK!
* InteractionTests
    * interactionsWithLabels: .x we found 1 problem(s):
        * [Interactions found that involve Labels: 1](#630d2678)
    * UnconnectedPoints: .. all OK!


## Summary

* Number of test classes: 22
* Number of tests: 155
* Number of assertions: 309
* Number of fails: 2

## Fails

* [The following DataNodes have no identifier: 2](#d2d32fa1)
"""


def test_the_report_is_read_into_problems_and_counts():
    report = parse_report(WP554)
    assert report["tests"] == 155 and report["fails"] == 2
    assert report["problems"] == [
        {
            "group": "DataNodesTests",
            "test": "dataNodesWithoutIdentifier",
            "text": "The following DataNodes have no identifier: 2",
        },
        {
            "group": "InteractionTests",
            "test": "interactionsWithLabels",
            "text": "Interactions found that involve Labels: 1",
        },
    ]


def _reports(tmp_path, handler):
    return CollectionReports(
        repo="wikipathways/wikipathways-collection",
        cache_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )


def test_a_report_is_fetched_once_a_day_and_read_from_disk(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=WP554)

    reports = _reports(tmp_path, handler)
    assert reports.cached(554) is None
    assert reports.refresh(554) is True
    assert reports.refresh(554) is False  # fresh on disk
    assert calls == [
        "https://raw.githubusercontent.com/wikipathways/wikipathways-collection/main/reports/WP554.md"
    ]
    cached = reports.cached(554)
    assert cached["fails"] == 2 and cached["url"].endswith("/reports/WP554.md")


def test_an_unpublished_pathway_is_remembered_as_having_no_report(tmp_path):
    reports = _reports(tmp_path, lambda request: httpx.Response(404))
    assert reports.refresh(9999) is True
    assert reports.cached(9999) is None
    assert reports.refresh(9999) is False


def test_a_server_error_writes_nothing(tmp_path):
    reports = _reports(tmp_path, lambda request: httpx.Response(503))
    assert reports.refresh(554) is False
    assert reports.cached(554) is None


def test_an_update_review_shows_the_database_checks(tmp_path):
    import io

    from fastapi.testclient import TestClient

    from app.github import FakeGitHubClient
    from tests.test_api import REV_GPML, _authed_app, _login, _settings

    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo = settings.content_repo
    path = "pathways/WP5636/WP5636.gpml"
    fake = FakeGitHubClient(
        default_branches={f"{repo}#main": "basesha"},
        existing_files={f"{repo}#{path}": "blob-v1"},
    )
    app, current = _authed_app(tmp_path, curators=["curator"], fake=fake)
    with TestClient(app) as c:
        app.state.collection = _reports(
            tmp_path / "collection", lambda request: httpx.Response(200, text=WP554)
        )
        app.state.worker._collection = app.state.collection
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5636/update",
            files={"file": ("rev.gpml", io.BytesIO(REV_GPML), "application/xml")},
        ).json()["pr_number"]
        app.state.worker.tick(github=fake)
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        assert "Database checks on the published WP5636" in page
        assert "Interactions found that involve Labels: 1" in page

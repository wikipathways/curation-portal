"""A slow GitHub call in one request must not stall every other request (2026-11 review).

The app runs as one uvicorn process with one event loop. Submit, update, revise, validate and
the webhook were ``async def`` handlers that called blocking code directly, so a submission
waiting on GitHub — forking alone can wait fifteen seconds — froze the whole portal, health
check included. Measured live in the review: a webhook whose mirror edit took 3 s held a
``GET /health`` due at 0.2 s until 3.02 s.
"""
from __future__ import annotations

import threading
import time

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

GPML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Event loop pathway" '
    b'Organism="Homo sapiens">\n'
    b'  <Graphics BoardWidth="200" BoardHeight="200"/>\n'
    b"</Pathway>\n"
)


class SlowFake(FakeGitHubClient):
    def put_file(self, *args, **kwargs):
        time.sleep(1.5)
        return super().put_file(*args, **kwargs)


def test_health_answers_while_a_submission_waits_on_github(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'reg.db'}",
        preview_cache_dir=str(tmp_path / "cache"),
        dev_wpid_floor=5636,
    )
    fake = SlowFake(
        default_branches={f"{settings.content_repo}#{settings.default_branch}": "basesha"}
    )
    app = build_app(settings)
    app.dependency_overrides[get_github_client] = lambda: fake
    app.dependency_overrides[get_bot_optional] = lambda: fake
    app.dependency_overrides[get_bot_client] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: "alice"

    with TestClient(app) as c:
        results: dict[str, object] = {}

        def submit() -> None:
            results["submit"] = c.post(
                "/api/submit", files={"file": ("p.gpml", GPML, "application/xml")}
            ).status_code

        worker = threading.Thread(target=submit)
        worker.start()
        time.sleep(0.3)  # the submission is now inside the slow put_file
        start = time.perf_counter()
        assert c.get("/health").status_code == 200
        waited = time.perf_counter() - start
        worker.join()

    assert results["submit"] == 201
    assert waited < 0.5, f"/health waited {waited:.2f}s behind a submission"

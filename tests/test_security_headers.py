from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_app, static_url
from app.security import build_csp, settings_site_host


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    with TestClient(build_app(settings)) as c:
        yield c


def test_pages_carry_a_report_only_csp_without_inline_script(client):
    r = client.get("/")
    csp = r.headers["content-security-policy-report-only"]
    script_src = csp.split("script-src")[1].split(";")[0]
    assert script_src.strip() == "'self'"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_json_responses_get_no_csp(client):
    r = client.get("/health")
    assert "content-security-policy-report-only" not in r.headers


def test_templates_have_no_inline_script_or_handlers():
    # script-src 'self' only works while no template carries an inline script body or on*= handler.
    from pathlib import Path

    for path in Path("templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), path
        assert not re.search(r"\son[a-z]+\s*=", text), path


def test_swagger_moved_and_openapi_stays(client):
    assert client.get("/api/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_static_url_is_content_hashed(client):
    url = static_url("app.js")
    assert re.fullmatch(r"/static/app\.js\?v=[0-9a-f]{8}", url)
    page = client.get("/").text
    assert url in page
    assert client.get(url).headers["cache-control"].startswith("public, max-age=31536000")


def test_site_host_only_from_https_settings():
    def host(url):
        return settings_site_host(Settings(_env_file=None, drafts_site_base_url=url))

    assert host("https://x.org/a") == "https://x.org"
    assert host("javascript:x") == ""
    assert "https://x.org" in build_csp("https://x.org")

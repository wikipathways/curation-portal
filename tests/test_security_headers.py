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


def test_pages_carry_an_enforced_csp_without_inline_script(client):
    r = client.get("/")
    assert "content-security-policy-report-only" not in r.headers
    csp = r.headers["content-security-policy"]
    script_src = csp.split("script-src")[1].split(";")[0]
    assert script_src.strip() == "'self'"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_json_responses_get_no_csp(client):
    r = client.get("/health")
    assert "content-security-policy" not in r.headers


def test_report_only_is_a_setting(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'r.db'}", csp_report_only=True
    )
    with TestClient(build_app(settings)) as c:
        r = c.get("/")
    assert "content-security-policy-report-only" in r.headers
    assert "content-security-policy" not in r.headers


def test_templates_have_no_inline_script_or_handlers():
    # script-src 'self' only works while no template carries an inline script body or on*= handler.
    from pathlib import Path

    for path in Path("templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), path
        assert not re.search(r"\son[a-z]+\s*=", text), path


def test_swagger_moved_and_openapi_stays(client):
    r = client.get("/api/docs")
    assert r.status_code == 200
    # Swagger UI loads from a CDN and runs inline; the enforced policy would leave it blank.
    assert "content-security-policy" not in r.headers
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


def test_static_url_follows_an_edited_file(tmp_path, monkeypatch):
    import os

    import app.main as main

    monkeypatch.setattr(main, "_STATIC_DIR", tmp_path)
    monkeypatch.setattr(main, "_STATIC_VERSIONS", {})
    f = tmp_path / "x.css"
    f.write_text("a{}")
    first = main.static_url("x.css")
    f.write_text("b{}")
    os.utime(f, ns=(1, f.stat().st_mtime_ns + 1_000_000))
    assert main.static_url("x.css") != first

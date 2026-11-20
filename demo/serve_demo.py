"""Run the app locally against a real GitHub fork, authenticated as you.

    submit a new pathway  ->  approve & merge in the dashboard  ->  update that same pathway

By default this talks to the **real** GitHub API as your own account and opens **real** pull
requests on your fork (``marvinm2/wikipathways-database``). Auth is your existing GitHub CLI
token; no OAuth App or GitHub App registration is needed. Because the app's OAuth login flow
needs a registered OAuth App, we skip it and inject your token directly — the identity, the API
calls, and the PRs are all real.

    .venv/bin/python demo/serve_demo.py         # real mode (opens real PRs on the fork)
    WPSUBMIT_DEMO_FAKE=1 .venv/bin/python demo/serve_demo.py   # offline, in-memory fake

Environment overrides:
    WPSUBMIT_DEMO_TOKEN   GitHub token to use (default: `gh auth token`)
    WPSUBMIT_DEMO_USER    your login          (default: `gh api user --jq .login`)
    WPSUBMIT_DEMO_REPO    target repo         (default: marvinm2/wikipathways-database)
    WPSUBMIT_DEMO_CURATOR 0 to sign in as a non-curator (default: you are a curator)

Real mode makes real changes on the fork (branches, PRs, and — on approve — a merge into the
fork's main). It is your fork, so that is fine; the README explains how to clean up afterwards.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import uvicorn
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import Settings
from app.github import FakeGitHubClient, HttpGitHubClient
from app.main import (
    build_app,
    get_bot_client,
    get_bot_optional,
    get_current_user,
    get_github_client,
)

BRANCH = "main"

# Stable state dir (registry + preview cache) so an auto-reload — which re-runs make_demo_app in a
# fresh worker — keeps the same DB and rendered previews instead of orphaning them in a new
# mkdtemp. Cleared once on an explicit launch (see __main__) for a fresh start.
_STATE_DIR = pathlib.Path(tempfile.gettempdir()) / "wpsubmit-demo-state"

# Repo root, so the auto-reloader's worker subprocess can import ``demo.serve_demo`` regardless of
# how the script was launched (PYTHONPATH propagates to the subprocess).
_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ["PYTHONPATH"] = str(_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")


def _gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()


_OAUTH_KEYS = (
    "WPSUBMIT_GITHUB_OAUTH_CLIENT_ID",
    "WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET",
    "WPSUBMIT_OAUTH_REDIRECT_URI",
)


def _load_oauth_from_env_file() -> None:
    """Pull just the OAuth keys from a local .env into the environment (the demo is otherwise
    hermetic). Lets a pre-registered OAuth App enable real login without exporting anything."""
    path = _ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in _OAUTH_KEYS and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


def _real_config():
    token = os.environ.get("WPSUBMIT_DEMO_TOKEN") or _gh("auth", "token")
    user = os.environ.get("WPSUBMIT_DEMO_USER") or _gh("api", "user", "--jq", ".login")
    repo = os.environ.get("WPSUBMIT_DEMO_REPO", "marvinm2/wikipathways-database")
    return token, user, repo


class _MergingFake(FakeGitHubClient):
    """Offline fallback: like the test fake, but a merge promotes the PR's file onto main so the
    update step afterwards finds the pathway on the base branch (real GitHub does this for free)."""

    def merge_pull_request(
        self, repo: str, pr_number: int, *, method: str = "squash", sha: str | None = None
    ) -> None:
        super().merge_pull_request(repo, pr_number, method=method, sha=sha)
        pr = next((p for p in self.pulls if p.number == pr_number), None)
        if pr is None:
            return
        for (r, branch, path), (content, _msg, sha) in list(self.files.items()):
            if r == repo and branch == pr.head_branch:
                self.existing_files[(r, path)] = sha
                self.existing_contents[(r, path)] = content

    def pr_preview_status(self, repo, pr_number, *, workflow_file, artifact_name):
        # Simulate a green PR-preview CI run so the merge gate (require_preview_check) passes in
        # the offline demo, matching what a real fork's pr-preview.yml does.
        return "ready"


def make_demo_app():
    tmp = _STATE_DIR
    tmp.mkdir(parents=True, exist_ok=True)
    fake_mode = os.environ.get("WPSUBMIT_DEMO_FAKE") == "1"

    if fake_mode:
        # WPSUBMIT_DEMO_USER is honoured here too, so that WPSUBMIT_DEMO_CURATOR=0 does not leave
        # you signed in as someone called "demo-curator" who is not one.
        user = os.environ.get("WPSUBMIT_DEMO_USER") or "demo-curator"
        repo, token = "wikipathways/wikipathways-database", None
    else:
        token, user, repo = _real_config()

    # Real GitHub login: if you register an OAuth App and pass its credentials, the demo uses the
    # actual "Log in with GitHub" flow (writes act as whoever logs in). Without them, a one-click
    # shim signs you in as the token owner so the demo still runs offline / without an OAuth App.
    if not fake_mode:
        _load_oauth_from_env_file()
    oauth_id = os.environ.get("WPSUBMIT_GITHUB_OAUTH_CLIENT_ID")
    oauth_secret = os.environ.get("WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET")
    real_login = bool(oauth_id and oauth_secret) and not fake_mode
    redirect_uri = (
        os.environ.get("WPSUBMIT_DEMO_OAUTH_REDIRECT")
        or os.environ.get("WPSUBMIT_OAUTH_REDIRECT_URI")
        or "http://localhost:8000/auth/callback"
    )

    settings = Settings(
        _env_file=None,  # ignore any local .env so the demo is self-contained
        database_url=f"sqlite:///{tmp / 'registry.db'}",
        content_repo=repo,
        default_branch=BRANCH,
        # In real mode the WPID floor is read from the fork's tree (a fresh, non-colliding id).
        # In fake mode there is no repo to read, so use a static floor.
        github_token=token if not fake_mode else None,
        dev_wpid_floor=5636,
        # A whitelist that exists but does not contain you, rather than an empty one: an empty list
        # also makes members() empty, which hides differences that only show up when there *is* a
        # curator somewhere and you are simply not them.
        curators=["another-curator"] if os.environ.get("WPSUBMIT_DEMO_CURATOR") == "0" else [user],
        preview_cache_dir=str(tmp / "preview-cache"),
        session_secret="demo-only-not-secret",
        github_oauth_client_id=oauth_id if real_login else None,
        github_oauth_client_secret=oauth_secret if real_login else None,
        oauth_redirect_uri=redirect_uri,
    )
    app = build_app(settings)

    if fake_mode:
        client = _MergingFake(default_branches={f"{repo}#{BRANCH}": "demobase0000000"})
        provider = lambda: client  # noqa: E731
    else:
        # A fresh real client per request (mirrors how the app builds one per user request).
        provider = lambda: HttpGitHubClient(token)  # noqa: E731

    # The bot identity (merge, mirror comment, review request) always runs as the token owner —
    # on your own fork you can merge your own PRs, so this stands in for the GitHub App.
    app.dependency_overrides[get_bot_optional] = provider
    app.dependency_overrides[get_bot_client] = provider

    if real_login:
        # Leave get_github_client / get_current_user to the real OAuth session, so writes are
        # attributed to whoever logs in with GitHub. The real /auth/login flow handles login.
        pass
    else:
        # Shim: one identity for everything, and make "Log in with GitHub" a one-click sign-in.
        app.dependency_overrides[get_github_client] = provider
        app.dependency_overrides[get_current_user] = lambda: user
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/auth/login"
        ]

        @app.get("/auth/login")
        def demo_auth_login(request: Request):
            request.session["gh_login"] = user
            return RedirectResponse("/", status_code=303)

    @app.get("/demo/login")
    def demo_login(request: Request):
        request.session["gh_login"] = user  # escape hatch for shim mode
        return RedirectResponse("/", status_code=303)

    @app.get("/demo/logout")
    def demo_logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    app.state._demo = {
        "user": user,
        "repo": repo,
        "fake": fake_mode,
        "real_login": real_login,
        "curator": settings.curators == [user],
    }
    return app


app = make_demo_app()

if __name__ == "__main__":
    shutil.rmtree(_STATE_DIR, ignore_errors=True)  # fresh registry + previews on an explicit start
    info = app.state._demo
    mode = "FAKE (offline, in-memory)" if info["fake"] else "REAL GitHub"
    print("\n  wikipathways-submit demo")
    print(f"  mode  : {mode}")
    print(f"  repo  : {info['repo']}")
    role = "curator" if info["curator"] else "NOT a curator (WPSUBMIT_DEMO_CURATOR=0)"
    print(f"  role  : {role}")
    if info["real_login"]:
        print("  login : REAL GitHub OAuth — click 'Log in with GitHub'")
        print("  open  : http://localhost:8000")
    else:
        print(f"  login : one-click shim as @{info['user']} (no OAuth App configured)")
        print("  open  : http://localhost:8000  (click 'Log in with GitHub')")
    if not info["fake"]:
        print("  NOTE  : submitting/approving opens and merges REAL pull requests on that fork.")
    print("  (auto-reloads on code changes — no stale-server surprises)\n")
    # Auto-reload: the worker re-imports demo.serve_demo (rebuilding a fresh app) whenever the
    # app/templates/static/demo sources change, so edits during a session can't run stale.
    uvicorn.run(
        "demo.serve_demo:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[str(_ROOT / d) for d in ("app", "templates", "static", "demo")],
    )

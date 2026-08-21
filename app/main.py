"""FastAPI application entry point.

Run locally: ``uvicorn app.main:app --reload``.

Wired: the transactional registry (WPID allocator + pathway lock), the new-pathway submission
flow, the update flow, and the curation dashboard (queue / checklist / assign / approve-merge).
Write paths (``/api/submit``, ``/api/pathways/{wpid}/update``, ``/api/reviews/{n}/approve``)
depend on a GitHub client via ``get_github_client`` and return 503 until the OAuth/App identities
(scaffolding-plan §3) are configured — read-only dashboard endpoints work without one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from app.auth import GitHubApp, GithubOAuth, OAuthError, TokenCipher, TokenCipherError
from app.config import Settings
from app.curators import make_curator_registry
from app.db import make_engine, make_session_factory
from app.github import (
    CredentialsRejected,
    GitHubClient,
    GitHubError,
    HttpGitHubClient,
    WriteDenied,
)
from app.locks import LockUnavailable, PathwayLockRegistry
from app.models import Base, ReviewStatus
from app.pipeline import DraftsReader
from app.preview import PreviewService
from app.preview.metadata import parse_curation_metadata
from app.ratelimit import RateLimited, SubmissionRateLimiter
from app.review.service import (
    TESTING_RULE_IDS,
    ChecklistIncomplete,
    CurationService,
    NotACurator,
    PreviewNotReady,
    ReviewNotActionable,
    ReviewNotFound,
)
from app.review.status import (
    ACTIONABLE,
    AWAITING_WPID,
    DECIDABLE,
    presentation,
    queue_tabs,
)
from app.submit import (
    InvalidGpml,
    NoPendingSubmission,
    SubmissionService,
    layout_paths,
    validate_gpml,
)
from app.submit.gpml import PLACEHOLDER_GPML_PATH
from app.submit.service import SubmissionMode
from app.submit.targets import (
    BotIdentityUnavailable,
    WriteTarget,
    bot_fallback_target,
    resolve_write_target,
)
from app.update import PathwayNotFound, UpdateService
from app.wpid import WpidAllocator
from app.wpid.github_floor import github_wpid_floor

_ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_ROOT / "templates"))

# Serve preview SVGs with script execution disabled (SVGs can carry <script>): a strict CSP plus
# the sandbox directive neutralises them even if a viewer opens the URL directly.
_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "public, max-age=300",
}
_PREVIEW_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 120" role="img" '
    b'aria-label="Preview unavailable"><rect width="200" height="120" fill="#f6f8f3"/>'
    b'<text x="100" y="63" text-anchor="middle" font-family="sans-serif" font-size="11" '
    b'fill="#7c8a82">Preview unavailable</text></svg>'
)


async def _read_upload(file: UploadFile, limit: int) -> bytes:
    """Read an uploaded file, refusing anything over ``limit`` with a 413 (issue #16).

    Reads in chunks and stops at the first one that crosses the limit, so an oversized body is
    never fully held in memory. ``Content-Length`` is deliberately not trusted as the authority —
    it is a claim by the client, absent under chunked encoding, and free to disagree with what
    actually arrives; the running total is what refuses.
    """
    chunk_size = 64 * 1024
    total = 0
    parts: list[bytes] = []
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"That file is larger than the {limit // (1024 * 1024)} MB limit. "
                    "A pathway this size is usually a sign the wrong file was chosen."
                ),
            )
        parts.append(chunk)
    return b"".join(parts)


#: A real WikiPathways identifier: at least one digit, never a leading zero. Declaring the path
#: parameter as ``int`` is not enough — FastAPI coerces "0001" to 1, so the placeholder this app
#: commits for a not-yet-published pathway silently addresses WP1, an unrelated real pathway.
_WPID_RE = re.compile(r"^[1-9][0-9]{0,5}$")

#: Reviews per page of the curation queue (issue #17). A card is not a row: it carries two
#: preview frames, a hotspot sidecar fetched per frame, the checklist, the data-node table and the
#: quality panel — and its pipeline section can cost three requests to the drafts site. The audit
#: this project came from counted 51 pull requests in three months, so on the real content repo
#: the Open tab is the ordinary working view rather than the tail.
QUEUE_PAGE_SIZE = 20


def _pager(request: Request, *, page: int, total: int) -> dict:
    """Where the reader is in the queue, and how to get to the rest of it (issue #17).

    Page numbers rather than a cursor: a curator works a queue, and wants to come back to the same
    place after acting on something. A cursor cannot say "page 3 of 7", and infinite scroll cannot
    be returned to at all.

    Out-of-range pages land on the last one instead of an empty list. The way this is reached is
    a bookmark or a back button after the queue shrank underneath it, and an empty page there
    reads as "everything is gone".
    """
    pages = max(1, -(-total // QUEUE_PAGE_SIZE))  # ceiling division
    page = min(max(page, 1), pages)
    offset = (page - 1) * QUEUE_PAGE_SIZE
    return {
        "page": page,
        "pages": pages,
        "offset": offset,
        "total": total,
        # 1-based inclusive span of this page, for "21-40 of 63".
        "first": offset + 1 if total else 0,
        "last": min(offset + QUEUE_PAGE_SIZE, total),
        # Built off the live query string so the status filter and ?mine=1 survive paging.
        "prev_url": _page_url(request, page - 1) if page > 1 else None,
        "next_url": _page_url(request, page + 1) if page < pages else None,
    }


def _page_url(request: Request, page: int) -> str:
    """This page's URL with ``page`` swapped, as a path — never absolute.

    Absolute would be wrong here rather than merely verbose. Nothing in the deployment tells
    uvicorn to trust ``X-Forwarded-Proto``, and Traefik terminates TLS and forwards plain HTTP, so
    ``request.url`` reports ``http`` on a site served over ``https`` and every pager link would
    point off the secure origin. Every other link in these templates is already relative.
    """
    url = request.url.include_query_params(page=page)
    return f"{url.path}?{url.query}" if url.query else url.path


def parse_wpid(raw: str) -> int:
    digits = raw[2:] if raw[:2].upper() == "WP" else raw
    if not _WPID_RE.fullmatch(digits):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{raw!r} is not a WikiPathways identifier. They are WP followed by digits, "
                "with no leading zero — WP0001 is the placeholder a submission carries until "
                "the database assigns its id, not a pathway."
            ),
        )
    return int(digits)


def get_current_user(request: Request) -> str:
    """The authenticated GitHub login from the session. 401 if not logged in.

    Identity comes from the OAuth session, never from a client-supplied form field — a submitter
    cannot act as someone else.
    """
    login = request.session.get("gh_login")
    if not login:
        raise HTTPException(status_code=401, detail="not authenticated. Log in at /auth/login")
    return login


def get_github_client(request: Request) -> GitHubClient:
    """Build a GitHub client acting as the logged-in user (their OAuth token). 401 if absent."""
    encrypted = request.session.get("gh_token")
    if not encrypted:
        raise HTTPException(status_code=401, detail="not authenticated. Log in at /auth/login")
    try:
        token = request.app.state.token_cipher.decrypt(encrypted)
    except TokenCipherError as exc:
        # Key rotated / tampered cookie: force a fresh login rather than 500.
        request.session.clear()
        raise HTTPException(
            status_code=401, detail="your session expired, log in again"
        ) from exc
    return HttpGitHubClient(token)


def get_bot_optional(request: Request) -> GitHubClient | None:
    """A GitHub client acting as the App (bot), or None if the App is not configured.

    Privileged, cross-cutting actions (merge, read-only mirror comment) run as the bot — never
    as a submitter's or curator's personal token (scaffolding-plan §3). Used where the bot is
    optional (mirror comments are best-effort); ``get_bot_client`` is the strict variant.
    """
    bot_app: GitHubApp | None = request.app.state.bot_app
    if bot_app is None:
        return None
    return HttpGitHubClient(bot_app.installation_token(), identity="bot")


def get_bot_client(request: Request) -> GitHubClient:
    """The bot GitHub client; 503 if the GitHub App identity is not configured."""
    client = get_bot_optional(request)
    if client is None:
        raise HTTPException(
            status_code=503, detail="GitHub App (bot) identity is not configured"
        )
    return client


def _write_target(
    settings: Settings, user_client: GitHubClient, bot: GitHubClient | None, submitter: str
) -> WriteTarget:
    """Whose credentials push the branch, and to which repository.

    On a shared target repo an ordinary submitter has no push access, so either the bot pushes
    for them (``bot``) or they push to their own fork and the pull request is cross-repository
    (``fork``). Where the submitter can push to the content repo directly — their own fork as
    the target, the demo — their own token against it is simplest.

    ``fork`` resolves the fork here, before the submission starts, so a failure falls back to the
    bot with nothing written. ``app.submit.targets`` has the reasoning for that boundary.
    """
    try:
        return resolve_write_target(
            identity=settings.submit_identity,
            user_client=user_client,
            bot_client=bot,
            content_repo=settings.content_repo,
            submitter=submitter,
        )
    except BotIdentityUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _writer_client_for_revise(
    settings: Settings,
    user_client: GitHubClient,
    bot: GitHubClient | None,
    head_repo: str | None,
) -> GitHubClient:
    """Who writes onto a branch that already exists — decided by *where that branch is*.

    Not by the configured identity, which is about opening submissions and can have changed since
    this one was opened. The rule that matters: **a GitHub App installation token cannot push to a
    submitter's personal fork**, because the App is not installed there. So a cross-repository
    submission has to be revised with the submitter's own token, whatever `submit_identity` says
    — including when it says `bot`, and including a submission that fell back to the bot and a
    later one that did not.

    With the branch on the content repo the old rule stands: the bot pushes wherever the
    submitter cannot.
    """
    if head_repo is not None:
        return user_client
    if settings.submit_identity in ("bot", "fork") and bot is not None:
        return bot
    return user_client


def _submitter_email(settings: Settings, submitter: str) -> str:
    """GitHub's noreply address, so a bot-pushed commit still shows the submitter as author."""
    return f"{submitter}@{settings.noreply_email_domain}"


def _host_of(url: str) -> str:
    """The bare hostname of a configured URL, for naming where an outbound link goes.

    Falls back to the input rather than raising or returning empty: this only ever feeds link
    text, and a misconfigured value showing up on screen is far better than a page that will
    not render. `urlparse` puts a scheme-less string in `path`, not `netloc`, so a value like
    `sandbox.wikipathways.org` would otherwise render as nothing at all.
    """
    return urlparse(url).netloc or url.strip("/") or ""


def _label_submission(
    settings: Settings, bot: GitHubClient | None, pr_number: int, *, kind: str
) -> None:
    """Tag the PR with the target repo's own new/edit vocabulary.

    Best-effort on purpose: these labels are descriptive, not the mechanism, so a repo that has
    not defined them (or a bot without Issues:write) must not cost anyone their submission.
    Contrast the `accepted` label, where a failure has to fail the call.
    """
    if bot is None or not settings.is_pipeline_mode:
        return
    label = (
        settings.label_new_submission if kind == "new" else settings.label_edited_submission
    )
    try:
        bot.add_labels(settings.content_repo, pr_number, [label])
    except (GitHubError, httpx.HTTPError):
        pass


def _make_bot_app(settings: Settings) -> GitHubApp | None:
    """Construct the GitHub App from settings, loading the private key from PEM or a secret file."""
    if not (settings.github_app_id and settings.github_app_installation_id):
        return None
    key = settings.github_app_private_key
    if not key and settings.github_app_private_key_path:
        # A configured-but-unreadable key must not take the whole service down. Everywhere else
        # an absent bot identity degrades to 503 on the routes that need it, and a deployment
        # that sets the path before creating the secret should behave the same way rather than
        # crash-looping on startup with the site already live.
        try:
            key = Path(settings.github_app_private_key_path).read_text()
        except OSError:
            logging.getLogger("wpsubmit.auth").error(
                "GitHub App private key is not readable at %s; the bot identity is disabled "
                "and the routes that need it will return 503",
                settings.github_app_private_key_path,
            )
            return None
    if not key:
        return None
    return GitHubApp(
        settings.github_app_id, key, settings.github_app_installation_id
    )


def _make_floor_provider(settings: Settings, bot_app: GitHubApp | None) -> Callable[[], int]:
    if settings.github_token:
        return lambda: github_wpid_floor(
            settings.content_repo_owner,
            settings.content_repo_name,
            settings.github_token,  # type: ignore[arg-type]
            branch=settings.default_branch,
        )
    if bot_app is not None:
        # The bot's installation token also reads the repo tree for the WPID floor (issue #3).
        return lambda: github_wpid_floor(
            settings.content_repo_owner,
            settings.content_repo_name,
            bot_app.installation_token(),
            branch=settings.default_branch,
        )
    # Local dev: no GitHub read, just a static floor the local reservations build on.
    return lambda: settings.dev_wpid_floor


def _configure_logging(settings: Settings) -> None:
    """Give the ``wpsubmit`` loggers somewhere to write. Without this they write nowhere.

    Found on 2026-08-03 by looking for one expected line after a deploy and finding that **no
    application log line had ever appeared in production**. Uvicorn configures only its own
    ``uvicorn*`` loggers, and nothing here ever called ``basicConfig``, so the root logger had no
    handler at all. Python's ``lastResort`` fallback then emits ``WARNING`` and above — bare, with
    no logger name or timestamp — and silently drops everything below it.

    The cost was not the missing line that started this. ``expire_stale`` and the WPID reclaim log
    how long a lock or reservation was actually held, at INFO, and that was built in the previous
    round precisely so the TTLs chosen from other people's data could be corrected against this
    deployment's own. It had been collecting nothing. Anything that decides quietly and logs why —
    the fork-mode fallback is the newest — has the same dependency.

    Attached to the ``wpsubmit`` parent rather than the root, so this says nothing about how
    anyone else's libraries log, and guarded so repeated ``build_app`` calls (every test that
    builds one) do not stack handlers.
    """
    logger = logging.getLogger("wpsubmit")
    logger.setLevel(settings.log_level.upper())
    if not any(getattr(h, "_wpsubmit", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
        handler._wpsubmit = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    # Uvicorn's access log already carries the request line; propagating would double every record.
    logger.propagate = False


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        # Dev convenience only. Production (Postgres) runs `alembic upgrade head` on deploy — see
        # docs/migrations.md — so we never auto-create tables outside SQLite.
        if settings.database_url.startswith("sqlite"):
            Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)
        # GitHub App (bot) identity — privileged merge/comment. None → those routes 503 (dev).
        bot_app = _make_bot_app(settings)
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.bot_app = bot_app
        app.state.token_cipher = TokenCipher(
            encryption_key=settings.token_encryption_key,
            session_secret=settings.session_secret,
        )

        def bot_provider() -> GitHubClient | None:
            if bot_app is None:
                return None
            return HttpGitHubClient(bot_app.installation_token(), identity="bot")

        # Server-side reads that happen outside a request — the curator team lookup and the
        # open-PR scan behind the pathway lock — go through this rather than through a FastAPI
        # dependency, because there is no request to hang one off. Kept on app.state so a test
        # can substitute a fake for it the way it substitutes the request-scoped clients.
        app.state.bot_client_provider = bot_provider

        # Curator whitelist: a GitHub Team if WPSUBMIT_CURATOR_TEAM is set, else the config list.
        app.state.curators = make_curator_registry(
            team=settings.curator_team,
            config_logins=settings.curators,
            bot_client_provider=lambda: app.state.bot_client_provider(),
        )
        # Pathway preview: the app draws before/after itself and caches it (issue #11).
        app.state.preview = PreviewService(cache_dir=settings.preview_cache_dir)
        # The target repo's own derived artifacts (pipeline mode). Read anonymously — the App is
        # not installed on the site repo and does not need to be, since those files are public.
        app.state.drafts = (
            DraftsReader(
                repo=settings.drafts_repo,
                branch=settings.drafts_branch,
                site_base_url=settings.drafts_site_base_url,
                cache_dir=str(Path(settings.preview_cache_dir) / "drafts"),
                ttl_seconds=settings.preview_cache_ttl_seconds,
            )
            if settings.is_pipeline_mode and settings.drafts_repo
            else None
        )
        # How many pull requests one account may open on the content repo in a window (issue
        # #21). Counted out of the review table rather than an in-memory bucket, which would
        # reset on every redeploy.
        app.state.rate_limiter = SubmissionRateLimiter(
            session_factory,
            limit=settings.submit_rate_limit,
            window=timedelta(minutes=settings.submit_rate_window_minutes),
        )
        app.state.allocator = WpidAllocator(
            session_factory,
            _make_floor_provider(settings, bot_app),
            ttl=timedelta(days=settings.wpid_reservation_ttl_days),
        )
        def open_pr_touching_pathway(wpid: int) -> bool:
            """Is there already an open pull request against this pathway, ours or anyone's?

            The lock's own table only knows about edits that went through this app. Someone with
            push access can open a raw pull request against the content repo at any time — on
            the deployed target most pull requests do arrive that way — and starting a second
            edit of the same GPML on top of one is exactly the unmergeable divergence the lock
            exists to prevent.

            Fails open. If GitHub is unreachable, refusing every update would be a worse outcome
            than the collision this guards against, which needs two editors at once to happen at
            all.
            """
            client = app.state.bot_client_provider()
            if client is None:
                return False
            try:
                return client.find_open_pr_touching(
                    settings.content_repo, f"pathways/WP{wpid}/"
                ) is not None
            except Exception:  # noqa: BLE001 — a blocked update costs more than a missed scan
                logging.getLogger("wpsubmit.locks").warning(
                    "could not scan %s for open pull requests touching WP%s; allowing the "
                    "check-out",
                    settings.content_repo,
                    wpid,
                    exc_info=True,
                )
                return False

        app.state.locks = PathwayLockRegistry(
            session_factory,
            ttl=timedelta(days=settings.pathway_lock_ttl_days),
            open_pr_scanner=open_pr_touching_pathway,
        )
        # Per-user OAuth (writes act as the submitter). None if unconfigured → auth routes 503.
        app.state.oauth = (
            GithubOAuth(
                settings.github_oauth_client_id,
                settings.github_oauth_client_secret,
            )
            if settings.github_oauth_client_id and settings.github_oauth_client_secret
            else None
        )
        yield

    def _curation(request: Request, github: GitHubClient | None = None) -> CurationService:
        # Review CRUD needs no GitHub client; only approve_and_merge does.
        st = request.app.state
        return CurationService(
            st.session_factory,
            github,
            repo=settings.content_repo,
            curators=st.curators,
            allocator=st.allocator,
            locks=st.locks,
            require_preview_check=settings.require_preview_check,
            preview_workflow_file=settings.preview_workflow_file,
            preview_artifact_name=settings.preview_artifact_name,
            app_base_url=settings.app_base_url,
            publish_mode=settings.publish_mode,
            default_branch=settings.default_branch,
            pipeline_workflow_file=settings.pipeline_workflow_file,
            publish_workflow_file=settings.publish_workflow_file,
            label_accepted=settings.label_accepted,
            label_rejected=settings.label_rejected,
            label_author_feedback=settings.label_author_feedback,
            publish_timeout=timedelta(minutes=settings.publish_timeout_minutes),
            close_rejected_after_timeout=settings.close_rejected_after_timeout,
            reconcile_min_interval=timedelta(
                seconds=settings.reconcile_min_interval_seconds
            ),
            drafts=st.drafts,
            previews=st.preview,
        )

    def _check_rate_limit(request: Request, submitter: str) -> None:
        """Refuse a submitter who is opening pull requests faster than anyone means to (#21).

        Called only where a *new* pull request would be created. Re-uploading to a pull request
        that already exists is not the thing being bounded — it opens nothing, notifies nobody
        new, and refusing it would punish the ordinary way a submitter answers a change request.
        """
        try:
            request.app.state.rate_limiter.check(submitter)
        except RateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

    def _fetch_base_gpml(github: GitHubClient, path: str) -> bytes | None:
        """The current base-``main`` version of ``path``, or None (best-effort — used as the
        update 'before' for both the render and the changed-since-base checklist scoping)."""
        try:
            return github.get_file_content(settings.content_repo, settings.default_branch, path)
        except Exception:  # noqa: BLE001 — a missing/unreadable base only costs the before-view
            return None

    def _render_preview(
        request: Request,
        *,
        pr_number: int,
        wpid: int,
        after_gpml: bytes,
        before_gpml: bytes | None = None,
        submitter_note: str | None = None,
    ) -> None:
        """Instantly render the before/after preview at PR-creation time (issue #11, 1a).

        Best-effort — a render failure only costs the preview, never the submission, so the whole
        thing is swallowed (the CI artifact / placeholder still covers the frame).
        """
        try:
            request.app.state.preview.render_local(
                pr_number,
                wpid,
                after_gpml=after_gpml,
                before_gpml=before_gpml,
                submitter_note=submitter_note,
            )
        except Exception:  # noqa: BLE001 — preview is cosmetic; never fail the write path on it
            logging.getLogger("wpsubmit.preview").warning(
                "local preview render failed for PR #%s", pr_number, exc_info=True
            )

    app = FastAPI(title="curation-portal", version="0.0.1", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.session_https_only,
        same_site="lax",
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(_ROOT / "static"), check_dir=False),
        name="static",
    )

    @app.exception_handler(CredentialsRejected)
    async def _credentials_rejected(request: Request, exc: CredentialsRejected) -> JSONResponse:
        """A revoked authorisation is the submitter's to fix, so say so and clear the session.

        Registered once rather than caught in each route: every write path has its own
        ``except GitHubError -> 502``, and a revoked token satisfies all of them. Adding a
        sibling clause to eight handlers would leave the ninth wrong. This is the one place the
        distinction is made, and it is made *before* those clauses see it because a handler for a
        specific type wins over the generic mapping.

        The session is cleared so the next request re-authenticates instead of retrying a token
        GitHub has already rejected — otherwise a submitter sees the same failure until the
        cookie ages out, with nothing telling them to sign in.

        Deliberately **not** 502: it was one, which is how issue #28 came to be filed as a
        token-refresh bug. There is nothing to refresh — an OAuth App token has no expiry and no
        refresh token — so the honest answer to a 401 is to ask for a fresh authorisation.
        """
        log = logging.getLogger("wpsubmit.auth")
        if getattr(exc, "identity", "user") == "bot":
            # The App's installation token was refused. Nothing the caller did, and signing in
            # would not touch it — the private key, the App id or the installation is wrong.
            log.error("the GitHub App's own credentials were rejected (%s)", exc)
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "The app's own GitHub credentials were rejected, so this action cannot "
                        "run. This is a server configuration problem, not something you can fix "
                        "by signing in. Please report it."
                    )
                },
            )
        log.info(
            "authorisation rejected for %s (%s); cleared the session",
            request.session.get("gh_login") or "an anonymous caller",
            exc,
        )
        request.session.clear()
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    "GitHub rejected your authorisation, which usually means it was revoked. "
                    "Sign in again at /auth/login. If writes still fail afterwards, revoke the "
                    "app at github.com/settings/applications and authorise it fresh."
                )
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- Pages -----------------------------------------------------------------------------

    def _page_ctx(request: Request) -> dict:
        login = request.session.get("gh_login")
        return {
            "request": request,
            "login": login,
            "is_curator": request.app.state.curators.is_curator(login),
            # Every page's footer names the target repo, so it belongs in the shared context
            # rather than in each route that happens to remember to pass it.
            "repo": settings.content_repo,
            # Changes what approval *means* on screen: a label handed to the repository, not a
            # merge this app performs.
            "pipeline_mode": settings.is_pipeline_mode,
            # Shown on every page when set, so a deployment pointed somewhere that cannot
            # publish says so before anyone spends an afternoon on a submission.
            "site_notice": settings.site_notice.strip(),
            # Just the host of the site the target repo publishes to, for naming the
            # destination of an outbound link. A bare hostname says "this leaves the portal"
            # in a way "the published page" does not, and on a fork it is the honest answer to
            # "published where?" — which is not wikipathways.org.
            "site_host": _host_of(settings.drafts_site_base_url),
        }

    # Order matters: this is the sentence, and it runs most to least review-relevant. A
    # re-annotation is the change a curator most needs told about, because the two boxes look
    # identical; "moved" comes last because it is usually noise.
    _DIFF_WORDS = (
        ("reannotated", "re-annotated"),
        ("added", "added"),
        ("removed", "removed"),
        ("relabelled", "relabelled"),
        ("moved", "moved"),
    )

    def _diff_summary(diff: dict | None) -> dict | None:
        """"3 added, 1 re-annotated" and the counts behind it, or None when there is nothing to
        compare (a new pathway) — and a truthful "no data nodes changed" when there is."""
        if not diff or not isinstance(diff.get("summary"), dict):
            return None
        counts = diff["summary"]
        parts = [f"{counts[k]} {word}" for k, word in _DIFF_WORDS if counts.get(k)]
        return {
            "counts": counts,
            "sentence": ", ".join(parts) if parts else "No data nodes were added, removed or "
            "re-annotated.",
            "changed": bool(parts),
        }

    def _review_view(request: Request, r) -> dict:
        """The per-review dict the templates consume (design §4.5) — enriched beyond the API model.

        ``preview`` is the app's own before/after render (issue #11): a cheap disk check here;
        the SVG bytes stream from ``/previews/...`` when the browser requests them. Every status
        gets it — a curator reading a change request, or checking what was merged, wants the
        diagram just as much as one reviewing an open PR.
        """
        pr_url = f"https://github.com/{settings.content_repo}/pull/{r.pr_number}"
        preview = None
        status = request.app.state.preview.status(r.pr_number)
        if status == "ready":
            preview = {
                "status": "ready",
                "before_svg_url": f"/previews/{r.pr_number}/before.svg",
                "after_svg_url": f"/previews/{r.pr_number}/after.svg",
                "datanodes_url": f"{pr_url}/files",
                "validation_url": f"{pr_url}/checks",
                # What changed, in words, above the two frames (issue #24). Server-rendered
                # because it is the sentence a curator reads *before* deciding whether to look at
                # the pictures at all — waiting on a fetch to say "nothing changed" defeats it.
                "diff": _diff_summary(request.app.state.preview.diff(r.pr_number)),
            }
        elif status == "failed":
            preview = {"status": "failed"}
        # 'pending' → leave None so the template shows the "generating" empty state
        shown = presentation(r.status.value)
        return {
            **_detail(r).model_dump(),
            "wpid_str": r.wpid_str,
            "pr_url": pr_url,
            # Everything the templates need to talk about this state in words rather than in
            # stored enum values.
            "status_label": shown.label,
            "status_blurb": shown.blurb,
            "status_tone": shown.tone,
            # Two different questions. "actionable" = the pull request is live and a revision
            # means something; "decidable" = a curator's approve/reject still applies, which is
            # also true of a publication that failed.
            "actionable": r.status.value in ACTIONABLE,
            "decidable": r.status.value in DECIDABLE,
            "awaiting_wpid": r.status.value in AWAITING_WPID,
            "preview": preview,
            # Parsed curation metadata (data nodes, references, description, ontology tags,
            # submitter note) cached at render time — a cheap disk read, None if not rendered.
            "metadata": request.app.state.preview.metadata(r.pr_number),
            "labels": r.github_labels or [],
            "decision_note": r.decision_note,
            "pipeline": _pipeline_view(request, r),
            # The app's own graded verdicts, cached beside the render. None where nothing was
            # rendered or the cache has been swept — the card falls back to saying nothing rather
            # than to an empty panel that reads as "no problems found".
            "quality": request.app.state.preview.quality(r.pr_number),
        }

    def _pipeline_view(request: Request, r) -> dict | None:
        """What the target repo's own pipeline has produced for this PR, if anything.

        The repo renders the pathway, resolves its identifiers and its references, and publishes
        a draft page, which is richer than anything the app makes for itself. It also fails more
        often than it succeeds: of the last 20 runs of its PR workflow, 5 succeeded and 14 failed
        (measured 2026-07-27). So every field here is optional, and the template has to read as
        "the app's own render still works" when all of them are absent.
        """
        drafts = request.app.state.drafts
        if drafts is None:
            return None
        slug = drafts.slug_for(kind=r.kind, wpid=r.wpid, pr_number=r.pr_number)
        artifacts = drafts.fetch(slug)
        run = r.pipeline_run or {}
        conclusion = run.get("conclusion")
        # The finished page, once the repo has published it. Kept separate from `draft_url`
        # because publication *moves* the drafts: the moment this is the right link to show,
        # every draft URL 404s and `available` goes false. Gated on the id actually existing,
        # since a new pathway has none until publication and a review can reach PUBLISHED with
        # the WPID recorded by hand.
        published_url = (
            drafts.published_url(r.wpid)
            if r.wpid and r.status == ReviewStatus.PUBLISHED
            else None
        )
        return {
            "slug": slug,
            "available": artifacts.available,
            "draft_url": artifacts.draft_url,
            "published_url": published_url,
            "svg_url": artifacts.svg_url,
            "thumb_url": artifacts.thumb_url,
            "datanode_count": len(artifacts.datanodes or []) or None,
            "reference_count": len(artifacts.bibliography or []) or None,
            "run_status": run.get("status"),
            "run_conclusion": conclusion,
            "run_url": run.get("url"),
            # The repository's own three verdicts, when its workflow has posted them, keyed by the
            # app rule that predicts each one so the card can show the pair without knowing the
            # mapping itself. Empty until that workflow change is proposed and merged.
            "testing": {
                rule_id: (run.get("testing") or {})[key]
                for rule_id, key in TESTING_RULE_IDS.items()
                if key in (run.get("testing") or {})
            },
            # The repository's most recent publish run, shown only while this review is waiting on
            # one. Not tied to this pull request — the publish workflow is dispatched by label and
            # carries no reference to one — so the card says whose run it might be, and does not
            # claim it is this pathway's.
            "publish_run": run.get("publish") or {},
            # The case worth naming: the repository ran and could not finish. Almost always it
            # could not read the GPML, which costs the submitter their metadata and preview and
            # is otherwise only visible several clicks into the Actions tab.
            "run_failed": conclusion not in (None, "success"),
        }

    @app.get("/robots.txt", response_class=Response)
    def robots() -> Response:
        """Keep crawlers out of everything but the landing page (issue #20).

        Not about the scanners already probing this host — they ignore it. It is that
        ``/auth/login`` is a real redirect into GitHub's OAuth flow, so a crawler following it
        mints authorization requests and the state entries that go with them, and that per-pull-
        request URLs (review pages, preview SVGs) have no business in a search index.

        Served from a route rather than ``static/`` so it does not depend on how that mount is
        configured.
        """
        body = (
            "User-agent: *\n"
            "Disallow: /dashboard\n"
            "Disallow: /previews\n"
            "Disallow: /auth\n"
            "Disallow: /api\n"
            "Disallow: /webhooks\n"
            "Allow: /$\n"
        )
        return Response(content=body, media_type="text/plain; charset=utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {**_page_ctx(request), "repo": settings.content_repo}
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        status: ReviewStatus | None = None,
        mine: bool = False,
        page: int = 1,
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # Terminalise any review whose PR was closed/merged outside the app before rendering the
        # queue, so the dashboard never shows a PR that no longer exists (issue #1).
        _curation(request, bot).reconcile_open_reviews()
        login = request.session.get("gh_login")
        # "Mine" is every state at once. A submitter is looking for one particular pathway and
        # does not know which state it reached — making them guess the tab is the same dead end
        # as making them remember a WPID they were never given.
        submitter = login if (mine and login) else None
        # Keyed on `submitter`, not on `mine`: ?mine=1 while logged out has nobody to filter by,
        # and leaving the status unset there would render the queue's empty state against a
        # status that is None.
        if submitter is not None:
            status = None
        elif status is None:
            status = ReviewStatus.OPEN
        curation = _curation(request)
        counts = curation.status_counts(submitter=submitter)
        # The page's own total, which is what the pager has to count against — the whole queue for
        # Mine (every status at once), one tab's worth otherwise.
        matching = sum(counts.values()) if status is None else counts.get(status.value, 0)
        pager = _pager(request, page=page, total=matching)
        reviews = [
            _review_view(request, r)
            for r in curation.list_queue(
                status=status,
                submitter=submitter,
                limit=QUEUE_PAGE_SIZE,
                offset=pager["offset"],
            )
        ]
        # The tab strip links to the unfiltered queue, so its numbers have to describe the
        # unfiltered queue. Scoping them to the viewer made every tab read zero from the Mine
        # page for anyone who has never submitted anything — which is most curators.
        tab_counts = curation.status_counts() if submitter is not None else counts
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                **_page_ctx(request),
                "reviews": reviews,
                "curators": sorted(request.app.state.curators.members()),
                "repo": settings.content_repo,
                "status": status.value if status is not None else None,
                "mine": bool(submitter),
                "counts": counts,
                "total_count": sum(counts.values()),
                "tabs": queue_tabs(counts=tab_counts, pipeline_mode=settings.is_pipeline_mode),
                "empty": presentation(status.value) if status is not None else None,
                "pager": pager,
            },
        )

    @app.get("/previews/{pr_number}/{side}.svg")
    def preview_svg(request: Request, pr_number: int, side: str):
        # Serve the app's cached before/after render (issue #11). SVGs are served with a
        # locked-down CSP + sandbox so a hostile SVG can't run script if opened directly; the
        # dashboard only ever loads them via <img> (which already can't run script).
        if side not in ("before", "after"):
            raise HTTPException(status_code=404, detail="unknown preview side")
        try:
            _curation(request).get(pr_number)  # unknown PR → 404, not a placeholder
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path = request.app.state.preview.svg_path(pr_number, side)
        if path is None:
            # Missing side (e.g. a new pathway has no "before", or the render is unavailable):
            # a placeholder keeps the frame intact instead of a broken-image icon.
            return Response(
                content=_PREVIEW_PLACEHOLDER, media_type="image/svg+xml", headers=_SVG_HEADERS
            )
        return FileResponse(path, media_type="image/svg+xml", headers=_SVG_HEADERS)

    @app.get("/previews/{pr_number}/{side}-nodes.json")
    def preview_nodes(request: Request, pr_number: int, side: str):
        """Hotspots for the clickable data-node overlay (issue #14).

        Separate from the SVG because the drawing is served into an ``<img>``, where its own
        markup is inert — the overlay is laid over the picture rather than built into it, which
        is also what keeps a hostile GPML's render unable to do anything.

        An empty list is a real answer ("drawn, no data nodes") and is served as such; a side with
        nothing on file 404s, so the client leaves the static image alone rather than rendering an
        overlay it cannot trust.
        """
        if side not in ("before", "after"):
            raise HTTPException(status_code=404, detail="unknown preview side")
        try:
            _curation(request).get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = request.app.state.preview.nodes(pr_number, side)
        if data is None:
            raise HTTPException(status_code=404, detail="no hotspots on file for this side")
        return JSONResponse(data)

    @app.get("/previews/{pr_number}/diff.json")
    def preview_diff(request: Request, pr_number: int):
        """What changed between the two sides, per node (issue #24).

        Index-aligned with each side's ``-nodes.json``, which is what lets the overlay colour a
        hotspot without a second identity scheme. 404 when there is nothing to compare — a new
        pathway has one side, and a cache written before this existed has no diff on file.
        """
        try:
            _curation(request).get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        data = request.app.state.preview.diff(pr_number)
        if data is None:
            raise HTTPException(status_code=404, detail="no diff on file for this pull request")
        return JSONResponse(data)

    @app.get("/dashboard/{pr_number}", response_class=HTMLResponse)
    def review_page(
        request: Request, pr_number: int, bot: GitHubClient | None = Depends(get_bot_optional)
    ):
        curation = _curation(request, bot)
        try:
            curation.get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # This page is reached directly as often as it is reached from the queue — a link in a
        # comment, a refresh after acting — so it cannot rely on a queue load having run.
        curation.reconcile_review(pr_number)
        # Fill in whatever the target repo's pipeline has already worked out, so the curator is
        # confirming answers rather than deriving them. Best-effort, and it never overwrites a
        # state a curator set by hand.
        meta = request.app.state.preview.metadata(pr_number)
        # The *cited* count, not the total: the target repo's generator only emits references a
        # <BiopaxRef> actually points at, so comparing its output against every PublicationXref
        # in the file reports a shortfall that is not one — and that item is required, so the
        # false FAIL would block approval and read as the submitter's fault.
        # `meta` is the parsed metadata.json, a plain dict — attribute access on it silently
        # yields None, which made the pipeline's reference check return early and do nothing.
        curation.refresh_pipeline_checks(
            pr_number,
            gpml_reference_count=meta.get("cited_reference_count") if meta else None,
        )
        # Read the row last. Both calls above write to it, and rendering the copy fetched before
        # them would show the page as it was one refresh ago.
        r = curation.get(pr_number)
        return templates.TemplateResponse(
            request,
            "review_detail.html",
            {
                **_page_ctx(request),
                "review": _review_view(request, r),
                "curators": sorted(request.app.state.curators.members()),
                "repo": settings.content_repo,
            },
        )

    # -- Auth (GitHub OAuth) ---------------------------------------------------------------

    def _oauth(request: Request) -> GithubOAuth:
        oauth = request.app.state.oauth
        if oauth is None:
            raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
        return oauth

    @app.get("/auth/login")
    def auth_login(request: Request):
        oauth = _oauth(request)
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state  # CSRF guard, verified on callback
        url = oauth.authorize_url(settings.oauth_redirect_uri, state, settings.oauth_scope)
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/callback")
    def auth_callback(request: Request, code: str, state: str):
        oauth = _oauth(request)
        if not state or state != request.session.get("oauth_state"):
            raise HTTPException(status_code=400, detail="OAuth state mismatch")
        request.session.pop("oauth_state", None)
        try:
            token = oauth.exchange_code(code, settings.oauth_redirect_uri)
            login = oauth.get_login(token)
        except OAuthError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Store the token encrypted at rest — the signed cookie is readable, encryption is not.
        request.session["gh_token"] = request.app.state.token_cipher.encrypt(token)
        request.session["gh_login"] = login
        return RedirectResponse("/", status_code=302)

    @app.get("/auth/me")
    def auth_me(request: Request) -> dict[str, object]:
        login = request.session.get("gh_login")
        return {
            "authenticated": bool(login),
            "login": login,
            "is_curator": request.app.state.curators.is_curator(login),
        }

    @app.post("/auth/logout")
    def auth_logout(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.post("/api/validate", response_model=ValidateResponse)
    async def validate(file: UploadFile) -> ValidateResponse:
        content = await _read_upload(file, settings.max_upload_bytes)
        try:
            meta = validate_gpml(content)
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        # The graded rules, run here rather than only at submit time: this is the last moment the
        # fix is free. Past this the file is on a branch, the pull request is open, and a warning
        # costs the submitter a re-upload.
        from app.quality import inspect_gpml

        return ValidateResponse(
            quality=inspect_gpml(content).as_dict(),
            name=meta.name,
            organism=meta.organism,
            embedded_wpid=meta.wpid,
            # In pipeline mode the file is committed under a placeholder and the target repo
            # renames it at publication, so promising "WP<assigned>" here would be a lie.
            will_layout_to=(
                PLACEHOLDER_GPML_PATH
                if settings.is_pipeline_mode
                else layout_paths(0)["gpml"].replace("WP0", "WP<assigned>")
            ),
        )

    @app.post("/api/submit", response_model=SubmitResponse, status_code=201)
    async def submit(
        request: Request,
        file: UploadFile,
        description: str = Form(""),
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        # Before the upload is read, let alone parsed or rendered: a refusal should cost this
        # process as little as it costs the content repo.
        _check_rate_limit(request, submitter)
        content = await _read_upload(file, settings.max_upload_bytes)
        target = _write_target(settings, github, bot, submitter)
        service = SubmissionService(
            request.app.state.allocator,
            target.client,
            repo=settings.content_repo,
            base_branch=settings.default_branch,
            mode=SubmissionMode(settings.publish_mode),
            target=target,
        )
        def _submit_with(svc: SubmissionService):
            return svc.submit_new_pathway(
                gpml=content,
                submitter=submitter,
                author_email=_submitter_email(settings, submitter),
                description=description,
            )

        try:
            try:
                result = _submit_with(service)
            except WriteDenied as denied:
                # The fork resolved and the *push* was refused, so nothing has been created and
                # the bot can take over cleanly. See ``bot_fallback_target``.
                retry = bot_fallback_target(
                    bot, settings.content_repo, submitter=submitter, reason=str(denied)
                )
                if retry is None or not target.is_cross_repo:
                    raise
                result = _submit_with(
                    SubmissionService(
                        request.app.state.allocator,
                        retry.client,
                        repo=settings.content_repo,
                        base_branch=settings.default_branch,
                        mode=SubmissionMode(settings.publish_mode),
                        target=retry,
                    )
                )
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except CredentialsRejected:
            # 401, not 502: handled app-wide as 'sign in again' (issue #28).
            raise
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        after_meta = parse_curation_metadata(content)
        # Rendered before the review is registered, not after. ``register`` posts the mirror
        # comment, and the mirror carries the quality report out of this cache — so registering
        # first published a comment with the table missing and only filled it in whenever a
        # curator next touched the review. Nothing in the render reads the review row, so the
        # order is free.
        _render_preview(
            request,
            pr_number=result.pr_number,
            wpid=result.wpid,
            after_gpml=content,
            before_gpml=None,  # new pathway has no base version
            submitter_note=description,
        )
        _curation(request, bot).register(
            pr_number=result.pr_number,
            wpid=result.wpid,
            submitter=submitter,
            kind="new",
            metadata=after_meta,  # pre-fills the checklist with auto-derived states
            head_branch=result.branch,
            head_repo=result.head_repo,
            submitter_note=description,
        )
        _label_submission(settings, bot, result.pr_number, kind="new")
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

    @app.post("/api/pathways/{wpid}/update", response_model=SubmitResponse, status_code=201)
    async def update(
        request: Request,
        wpid: str,
        file: UploadFile,
        description: str = Form(""),
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        wpid = parse_wpid(wpid)
        # The same gate the revise route has. Without it a submitter can push a new commit onto
        # an approved pull request that still carries the `accepted` label, so the repository
        # may publish a GPML no curator has looked at — while the review still reads "approved"
        # against a checklist silently rebuilt from the new file.
        existing = _curation(request).find_open_review_for_pathway(wpid)
        if existing is not None and existing.status.value not in ACTIONABLE:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"WP{wpid} has a pull request that is "
                    f"{presentation(existing.status.value).label.lower()} "
                    f"(#{existing.pr_number}); wait for it to finish before editing again."
                ),
            )
        if existing is None:
            # Only when this would open one. With a pull request already on the pathway the
            # update re-uploads onto it, which is the answer-a-change-request path.
            _check_rate_limit(request, submitter)
        content = await _read_upload(file, settings.max_upload_bytes)
        target = _write_target(settings, github, bot, submitter)
        writer = target.client
        service = UpdateService(
            request.app.state.locks,
            writer,
            repo=settings.content_repo,
            base_branch=settings.default_branch,
            target=target,
        )
        try:
            # Off the event loop. Checking out a pathway scans the target repo's open pull
            # requests for a foreign writer, which on a busy repo is dozens of sequential
            # requests — long enough to stall every other request in the process if it ran here.
            try:
                result = await run_in_threadpool(
                    service.update_pathway,
                    wpid=wpid,
                    gpml=content,
                    submitter=submitter,
                    author_email=_submitter_email(settings, submitter),
                    description=description,
                )
            except WriteDenied as denied:
                # Same boundary as the submit route: create_branch is the first mutating call, so
                # nothing has been written and the bot can take over. The update service releases
                # the pathway lock on any failure, so the retry re-acquires it cleanly.
                retry = bot_fallback_target(
                    bot, settings.content_repo, submitter=submitter, reason=str(denied)
                )
                if retry is None or not target.is_cross_repo:
                    raise
                result = await run_in_threadpool(
                    UpdateService(
                        request.app.state.locks,
                        retry.client,
                        repo=settings.content_repo,
                        base_branch=settings.default_branch,
                        target=retry,
                    ).update_pathway,
                    wpid=wpid,
                    gpml=content,
                    submitter=submitter,
                    author_email=_submitter_email(settings, submitter),
                    description=description,
                )
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except LockUnavailable as exc:
            raise HTTPException(
                status_code=409, detail={"reason": exc.reason, "held_by": exc.held_by}
            ) from exc
        except PathwayNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CredentialsRejected:
            # 401, not 502: handled app-wide as 'sign in again' (issue #28).
            raise
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Fetch the base version once — it is both the render 'before' and the baseline the
        # checklist uses to skip checks for things this update didn't change.
        before_gpml = _fetch_base_gpml(writer, result.path)
        after_meta = parse_curation_metadata(content)
        before_meta = parse_curation_metadata(before_gpml) if before_gpml else None
        # Before registering, so the mirror comment register posts carries the quality report.
        _render_preview(
            request,
            pr_number=result.pr_number,
            wpid=result.wpid,
            after_gpml=content,
            before_gpml=before_gpml,
            submitter_note=description,
        )
        _curation(request, bot).register(
            pr_number=result.pr_number,
            wpid=result.wpid,
            submitter=submitter,
            kind="update",
            metadata=after_meta,
            before_metadata=before_meta,
            head_branch=result.branch,
            head_repo=result.head_repo,
            submitter_note=description,
        )
        _label_submission(settings, bot, result.pr_number, kind="update")
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

    @app.post("/api/reviews/{pr_number}/revise", response_model=SubmitResponse, status_code=201)
    async def revise_review(
        request: Request,
        pr_number: int,
        file: UploadFile,
        description: str = Form(""),
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        """Commit a revised GPML onto an open new-pathway PR and re-open its review.

        Keyed by pull request, not by WPID: in pipeline mode a new submission has no id until
        the target repo publishes it, so there is nothing to look it up by until then.
        """
        content = await _read_upload(file, settings.max_upload_bytes)
        curation = _curation(request, bot)
        try:
            review = curation.get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if review.kind != "new":
            raise HTTPException(
                status_code=409,
                detail="this is an update, not a new submission; upload it as an update instead",
            )
        if review.status.value not in ACTIONABLE:
            # Committing onto an approved submission would put the review back to open while the
            # `accepted` label is still on the pull request and the repository may already be
            # publishing it — two venues telling different stories about the same pathway.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this submission is {presentation(review.status.value).label.lower()}, so it "
                    "can no longer be revised. Submit the change as a new upload instead."
                ),
            )
        if review.submitter != submitter and not request.app.state.curators.is_curator(submitter):
            raise HTTPException(
                status_code=403, detail="only the submitter or a curator can revise this submission"
            )
        # Revise writes onto a branch that already exists, wherever it is: the review row
        # records that in `head_repo` and it is passed below. Re-resolving a fork here would be
        # both redundant and wrong for a pull request opened under a different identity.
        service = SubmissionService(
            request.app.state.allocator,
            _writer_client_for_revise(settings, github, bot, review.head_repo),
            repo=settings.content_repo,
            base_branch=settings.default_branch,
            mode=SubmissionMode(settings.publish_mode),
        )
        try:
            result = service.revise_new_pathway(
                gpml=content,
                submitter=submitter,
                wpid=review.wpid,
                branch=review.head_branch,
                head_repo=review.head_repo,
                author_email=_submitter_email(settings, submitter),
            )
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except NoPendingSubmission as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CredentialsRejected:
            # 401, not 502: handled app-wide as 'sign in again' (issue #28).
            raise
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Re-rendered before the review is re-opened, so the mirror comment ``revise`` posts
        # describes the file that was just uploaded rather than the one it replaced.
        _render_preview(
            request,
            pr_number=result.pr_number,
            wpid=result.wpid,
            after_gpml=content,
            before_gpml=None,
            submitter_note=description,
        )
        # Re-open the review and rebuild its checklist from the revised content.
        curation.revise(
            result.pr_number,
            metadata=parse_curation_metadata(content),
            submitter_note=description,
        )
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

    @app.get("/api/pathways/{wpid}", response_model=PathwayInfo)
    def pathway_info(
        request: Request,
        wpid: str,
        github: GitHubClient = Depends(get_github_client),
    ) -> PathwayInfo:
        """Where does ``WP<wpid>`` live — on the base branch (an update), an open new-submission PR
        (a revise), or nowhere? Backs the update form's presence check and revise routing."""
        wpid = parse_wpid(wpid)
        wpid_str = f"WP{wpid}"
        path = layout_paths(wpid)["gpml"]
        try:
            content = github.get_file_content(
                settings.content_repo, settings.default_branch, path
            )
        except CredentialsRejected:
            # 401, not 502: handled app-wide as 'sign in again' (issue #28).
            raise
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if content is not None:
            return PathwayInfo(
                exists=True,
                state="on_main",
                wpid=wpid_str,
                name=parse_curation_metadata(content).name,
            )
        # Not on main — is there still an open new-submission PR to revise? Ask our own registry
        # first. It answers in both publish modes and costs no GitHub call, where the branch scan
        # below only ever finds a *direct*-mode submission: a pipeline branch carries a timestamp
        # and the pathway has no id to name it after in the first place.
        review = _curation(request).find_open_new_review(wpid)
        if review is not None:
            return PathwayInfo(
                exists=False, state="pending_new", wpid=wpid_str, pr_number=review.pr_number
            )
        try:
            pr = github.find_open_pr(settings.content_repo, f"submit/{wpid_str}")
        except GitHubError:
            pr = None
        if pr is not None:
            return PathwayInfo(
                exists=False, state="pending_new", wpid=wpid_str, pr_number=pr.number
            )
        return PathwayInfo(exists=False, state="absent", wpid=wpid_str)

    # -- Curation dashboard (MVP-4) --------------------------------------------------------

    @app.get("/api/reviews", response_model=list[ReviewSummary])
    def list_reviews(request: Request, status: ReviewStatus = ReviewStatus.OPEN):
        return [_summary(r) for r in _curation(request).list_queue(status=status)]

    @app.get("/api/reviews/{pr_number}", response_model=ReviewDetail)
    def get_review(request: Request, pr_number: int):
        try:
            r = _curation(request).get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/checklist", response_model=ReviewDetail)
    def update_checklist(
        request: Request,
        pr_number: int,
        key: str = Form(...),
        state: str = Form(...),
        # Omitted (the dashboard's state chips send no note) → the stored note is left alone.
        note: str | None = Form(None),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # Only curators mutate review state (design §4.5); non-curators get a read-only view.
        if not request.app.state.curators.is_curator(actor):
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).set_checklist_item(pr_number, key, state, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/assign", response_model=ReviewDetail)
    def assign_review(
        request: Request,
        pr_number: int,
        curator: str = Form(...),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        if not request.app.state.curators.is_curator(actor):
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).assign(pr_number, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/request-changes", response_model=ReviewDetail)
    def request_changes(
        request: Request,
        pr_number: int,
        note: str = Form(""),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        if not request.app.state.curators.is_curator(actor):
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).request_changes(pr_number, actor, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReviewNotActionable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/approve", response_model=ReviewDetail)
    def approve_review(
        request: Request,
        pr_number: int,
        curator: str = Depends(get_current_user),
        bot: GitHubClient = Depends(get_bot_client),
    ):
        # Approval runs as the bot (App installation token), never the curator's personal token
        # (scaffolding-plan §3) — merging that way satisfies branch protection, and labelling
        # that way works even for a curator without write access to the target repo.
        try:
            r = _curation(request, bot).approve(pr_number, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ChecklistIncomplete, ReviewNotActionable) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PreviewNotReady as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CredentialsRejected:
            # 401, not 502: handled app-wide as 'sign in again' (issue #28).
            raise
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/reject", response_model=ReviewDetail)
    def reject_review(
        request: Request,
        pr_number: int,
        note: str = Form(""),
        curator: str = Depends(get_current_user),
        bot: GitHubClient = Depends(get_bot_client),
    ):
        # Terminal, unlike request-changes: in pipeline mode this hands the PR to the target
        # repo's rejection workflow, which deletes the generated drafts and closes it.
        try:
            r = _curation(request, bot).reject(pr_number, curator, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ReviewNotActionable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CredentialsRejected:
            # 401, not 502: handled app-wide as 'sign in again' (issue #28).
            raise
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/published-wpid", response_model=ReviewDetail)
    def record_published_wpid(
        request: Request,
        pr_number: int,
        wpid: int = Form(...),
        curator: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # The manual way out of PUBLISH_FAILED: the target repo's publish workflow is the one
        # part of the loop the app does not control, so a curator has to be able to say what it
        # actually did.
        try:
            r = _curation(request, bot).record_published_wpid(pr_number, wpid, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/pathways/{wpid}/release")
    async def force_release(
        request: Request, wpid: str, curator: str = Depends(get_current_user)
    ) -> dict[str, bool]:
        # Curator override (design §4.3): restricted to the curator whitelist.
        if not request.app.state.curators.is_curator(curator):
            raise HTTPException(status_code=403, detail=f"{curator} is not a curator")
        released = request.app.state.locks.release(parse_wpid(wpid), curator, force=True)
        return {"released": released}

    # -- GitHub webhook (issue #8): release the lock when a PR is closed/merged outside the app --

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request, bot: GitHubClient | None = Depends(get_bot_optional)
    ) -> dict[str, object]:
        secret = settings.github_webhook_secret
        if not secret:
            raise HTTPException(status_code=503, detail="webhook secret is not configured")
        raw = await request.body()
        # Verify HMAC-SHA256 over the raw body before trusting anything in it.
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return {"ok": True, "pong": True}
        if event != "pull_request":
            return {"ok": True, "ignored": f"event:{event}"}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        action = payload.get("action")
        if action not in ("closed", "labeled", "unlabeled"):
            return {"ok": True, "ignored": f"action:{action}"}

        pr = payload.get("pull_request") or {}
        pr_number = payload.get("number") or pr.get("number")
        if pr_number is None:
            raise HTTPException(status_code=422, detail="no PR number in payload")
        curation = _curation(request, bot)

        if action in ("labeled", "unlabeled"):
            # A curator may well reach for the label on GitHub rather than the dashboard — those
            # labels are the target repo's own vocabulary. Recording it here is what keeps the
            # two venues from diverging.
            label = ((payload.get("label") or {}).get("name")) or ""
            actor = (payload.get("sender") or {}).get("login") or "github"
            review = curation.handle_label_event(
                int(pr_number), label, added=(action == "labeled"), actor=actor
            )
            return {
                "ok": True,
                "pr_number": int(pr_number),
                "label": label,
                "applied": review is not None,
            }

        merged = bool(pr.get("merged"))
        review = curation.handle_pr_closed(int(pr_number), merged=merged)
        return {
            "ok": True,
            "pr_number": int(pr_number),
            "merged": merged,
            "tracked": review is not None,
        }

    return app


class ValidateResponse(BaseModel):
    name: str | None
    organism: str | None
    embedded_wpid: str | None
    will_layout_to: str
    #: The graded pre-flight report (``app.quality``). Everything in here is advisory — a file
    #: that reaches this response has already cleared the blocking rules, which 422 instead.
    quality: dict = Field(default_factory=dict)


class SubmitResponse(BaseModel):
    wpid: str
    pr_number: int
    pr_url: str
    path: str


class PathwayInfo(BaseModel):
    exists: bool  # present on the base branch (an update target)
    wpid: str
    name: str | None = None
    state: str = "absent"  # "on_main" | "pending_new" | "absent"
    pr_number: int | None = None  # the open new-submission PR, when state == pending_new


class ReviewSummary(BaseModel):
    pr_number: int
    #: None until the target repo assigns one (pipeline mode); see Review.wpid.
    wpid: int | None = None
    submitter: str
    kind: str
    status: str
    assigned_curator: str | None


class ReviewDetail(ReviewSummary):
    checklist: list[dict]
    approved_by: str | None
    #: What the submitter said they changed. Also cached beside the render, but that copy is
    #: deleted at every terminal transition, so this is the one that outlives the review.
    submitter_note: str | None = None


def _summary(r) -> ReviewSummary:
    return ReviewSummary(
        pr_number=r.pr_number,
        wpid=r.wpid,
        submitter=r.submitter,
        kind=r.kind,
        status=r.status.value,
        assigned_curator=r.assigned_curator,
    )


def _detail(r) -> ReviewDetail:
    return ReviewDetail(
        **_summary(r).model_dump(),
        checklist=r.checklist,
        approved_by=r.approved_by,
        submitter_note=r.submitter_note,
    )


app = build_app()

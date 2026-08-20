"""Fork-per-submitter: the branch lives on the submitter's fork, the pull request is theirs.

Issue #22. The thing under test is not "does a fork get made" but the four places where a
cross-repository submission differs from a same-repository one, each of which is a way to get it
subtly wrong:

- the base commit is read from the **content repo**, never from the fork, which can be a year
  stale — cutting from the fork's default branch would silently revert everything merged upstream;
- the pull request's ``head`` is ``owner:branch``, not ``branch``;
- ``find_open_pr`` has to be told the head repo, or a fork branch is looked for on the base and
  found nowhere (which is how revise breaks and how an update opens a second pull request);
- a revise writes with the **submitter's** token, because a GitHub App installation token cannot
  push to a personal fork — the App is not installed there.

Plus the fallback, which is the whole reason fork mode is safe to turn on: anything that stops a
fork being had puts the submission back on the bot rather than failing it.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.github import (
    CredentialsRejected,
    FakeGitHubClient,
    GitHubError,
    HttpGitHubClient,
    WriteDenied,
)
from app.locks import PathwayLockRegistry
from app.submit import SubmissionService
from app.submit.service import SubmissionMode
from app.submit.targets import (
    BotIdentityUnavailable,
    WriteTarget,
    bot_fallback_target,
    resolve_write_target,
    same_repo_target,
)
from app.update import UpdateService

REPO = "wikipathways/sandbox-wp-db"
FORK = "alice/sandbox-wp-db"
FROZEN = datetime(2026, 8, 3, 18, 0, 0, tzinfo=UTC)

GOOD_GPML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Mitophagy" '
    'Organism="Homo sapiens"><Graphics BoardWidth="100" BoardHeight="100"/></Pathway>'
)


def _user_client(**kw) -> FakeGitHubClient:
    return FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"}, login="alice", **kw
    )


def _fork_target(user: FakeGitHubClient) -> WriteTarget:
    return resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=FakeGitHubClient(login="wikipathways-bot"),
        content_repo=REPO,
        submitter="alice",
    )


def _service(user: FakeGitHubClient, target: WriteTarget) -> SubmissionService:
    return SubmissionService(
        None,
        target.client,
        repo=REPO,
        mode=SubmissionMode.PIPELINE,
        clock=lambda: FROZEN,
        target=target,
    )


# ---- the target itself ------------------------------------------------------------------------


def test_head_is_owner_colon_branch_only_across_repos():
    same = same_repo_target(FakeGitHubClient(), REPO)
    assert same.head("my-branch") == "my-branch"
    assert same.head_repo is None
    assert same.is_cross_repo is False

    fork = WriteTarget(FakeGitHubClient(), branch_repo=FORK, content_repo=REPO, identity="fork")
    # Only the owner: a fork keeps its parent's name, and `alice/sandbox-wp-db:b` does not resolve.
    assert fork.head("my-branch") == "alice:my-branch"
    assert fork.head_repo == FORK
    assert fork.is_cross_repo is True


# ---- submitting -------------------------------------------------------------------------------


def test_submission_branches_on_the_fork_and_opens_a_cross_repo_pull_request():
    user = _user_client()
    target = _fork_target(user)
    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert (FORK, result.branch) in user.branches
    assert (REPO, result.branch) not in user.branches  # nothing was written to the content repo
    assert (FORK, result.branch, "pathways/WP0001/WP0001.gpml") in user.files
    assert user.pull_meta[result.pr_number]["head"] == f"alice:{result.branch}"
    assert user.pull_meta[result.pr_number]["base"] == "main"
    # Recorded off GitHub's answer, so revise can find the branch again.
    assert result.head_repo == FORK


def test_the_branch_is_cut_from_the_forks_own_head():
    """Reversed on 2026-08-05, and the reversal is the point (issue #29).

    This test used to assert the opposite — cut from upstream, never from the fork — on the
    reasoning that a stale fork would make every submission "a silent revert of everything merged
    since". Both halves of that were wrong. GitHub will not point a ref at a commit the repository
    does not hold, even one readable through the shared fork network: it answers **404**, which is
    also what it says to a write you may not make, which is why this cost a day. And a stale base
    does not revert anything, because a pull request's diff is computed against the merge base, so
    only the file actually changed appears in it.

    The fork's own head is native by definition, so the write is legal on any topology.
    """
    user = _user_client()
    target = _fork_target(user)
    # The fork has fallen behind: its own main points somewhere older.
    user.branches[(FORK, "main")] = "stale-fork-head"

    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert user.branches[(FORK, result.branch)] == "stale-fork-head"
    # Still opened against the content repo's base branch, so GitHub does the three-way work.
    assert user.pull_meta[result.pr_number]["base"] == "main"


def test_a_fork_that_could_not_be_synced_still_submits_as_the_submitter():
    """The regression test for issue #29, and the one the previous fake could not express.

    A fork of a fork cannot be synced: `merge-upstream` aims at the network source and a direct
    ref update is refused. So the fork stays behind, and the content repo's head is a commit it
    does not hold — readable through the shared network, but **not a legal ref target**. Cutting
    from it there is refused with 404, which sends a submission that should have been the
    submitter's own down the bot fallback.

    Cutting from the fork's own head is legal on any topology, which is what this pins. The
    assertion that matters is `identity == "fork"`: a bot fallback here would still produce a
    working pull request, so a test that only checked the submission succeeded would pass while
    the feature was broken — which is exactly how this survived unnoticed.
    """
    user = _user_client(fork_can_sync=False)
    # The fork holds only its own, older commit. The upstream head is nowhere in it.
    user.branches[(FORK, "main")] = "stale-fork-head"
    target = _fork_target(user)
    assert target.identity == "fork", "precondition: fork mode resolved"

    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert user.branches[(FORK, result.branch)] == "stale-fork-head"
    assert result.head_repo == FORK, "must stay the submitter's own PR, not fall back to the bot"


def test_the_fake_refuses_a_ref_at_a_commit_the_repo_does_not_hold():
    """Pins the fake's own fidelity, because the bug it now models is invisible without it.

    GitHub answers ref creation three ways and only one of them is an error the old fake could
    produce. This is the middle case: an object that exists elsewhere in the network but not here.
    """
    user = _user_client(fork_can_sync=False)
    user.branches[(FORK, "main")] = "stale-fork-head"

    with pytest.raises(WriteDenied) as exc:
        user.create_branch(FORK, "some-branch", "upstream-head")
    assert "not an object" in str(exc.value)

    user.create_branch(FORK, "fine-branch", "stale-fork-head")  # its own head: no raise


def test_a_same_repo_submission_still_cuts_from_the_content_repo():
    """`base_repo` is `branch_repo`, and outside fork mode those are the same repository — so the
    reversal above must be invisible to every other mode. Guards against 'fixed fork mode, moved
    the base out from under `user`/`bot`/the demo'."""
    user = _user_client()
    target = same_repo_target(user, REPO)

    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert user.branches[(REPO, result.branch)] == "upstream-head"


def test_the_fork_is_ensured_once_and_reused():
    user = _user_client()
    first = _fork_target(user)
    _service(user, first).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    second = _fork_target(user)
    _service(user, second).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    assert user.forks_created == [(REPO, FORK)]  # not once per submission
    assert second.branch_repo == FORK


# ---- the fallback -----------------------------------------------------------------------------


def test_a_fork_that_cannot_be_had_falls_back_to_the_bot():
    """The reason fork mode is safe to enable: a submission never dies because forking failed.

    An organisation can forbid forking and a token can be revoked between login and submission,
    so this is a routine outcome rather than an exceptional one.
    """
    user = _user_client(fail_on={"ensure_fork"})
    bot = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="wp-bot")

    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=bot,
        content_repo=REPO,
        submitter="alice",
    )

    assert target.identity == "bot"
    assert target.client is bot
    assert target.branch_repo == REPO
    assert target.head_repo is None  # an ordinary same-repo pull request

    result = _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    assert (REPO, result.branch) in bot.branches
    assert user.forks_created == []


def test_the_fallback_happens_before_anything_is_written():
    """Which is what makes it safe — there is no partial state to reconcile."""
    user = _user_client(fail_on={"ensure_fork"})
    bot = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="wp-bot")
    resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=bot,
        content_repo=REPO,
        submitter="alice",
    )
    assert user.branches == {(REPO, "main"): "upstream-head"}  # only what it was seeded with
    assert user.files == {}
    assert bot.files == {}


def test_fork_failure_with_no_bot_configured_uses_the_users_own_token():
    user = _user_client(fail_on={"ensure_fork"})
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=None,
        content_repo=REPO,
        submitter="alice",
    )
    # Not an exception: on a target the submitter *can* push to, this is simply correct, and where
    # they cannot, create_branch fails with a 403 that describes the real problem.
    assert target.identity == "user"
    assert target.client is user


def test_the_owner_of_the_content_repo_never_forks_it():
    """GitHub refuses to fork a repository into the account that owns it.

    This was the live configuration until 2026-08-20: the deployment targeted
    `marvinm2/sandbox-wp-db` and `marvinm2` was who tested it. Without this, every one of his
    submissions would have taken the bot fallback — a worse pull request than the one he can
    open directly, and one that would have made fork mode look broken while it was working.

    The target is `wikipathways/sandbox-wp-db` now, which he does not own, so his own
    submissions go down the ordinary fork path like anybody else's. The case stays tested
    because the rule is about ownership rather than about one deployment: the next target
    owned by whoever is submitting hits it again, and the fixtures here say so directly
    instead of borrowing a repository name that has to keep up.
    """
    user = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="marvinm2")
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=FakeGitHubClient(login="wp-bot"),
        content_repo="marvinm2/sandbox-wp-db",
        submitter="MarvinM2",  # case-insensitive: GitHub logins are
    )
    assert target.identity == "user"
    assert target.client is user
    assert target.head_repo is None
    assert user.forks_created == []


def test_bot_identity_without_a_bot_is_a_deployment_error():
    with pytest.raises(BotIdentityUnavailable):
        resolve_write_target(
            identity="bot",
            user_client=_user_client(),
            bot_client=None,
            content_repo=REPO,
            submitter="alice",
        )


# ---- updates ----------------------------------------------------------------------------------


def test_an_update_in_fork_mode_looks_for_its_pull_request_on_the_fork(session_factory):
    """`find_open_pr` defaults to the base owner, and a fork branch is not there.

    Reading None would make the re-upload path open a *second* pull request for one pathway —
    the divergence the check-out lock exists to prevent.
    """
    user = FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"},
        existing_files={f"{REPO}#pathways/WP554/WP554.gpml": "blob1"},
        login="alice",
    )
    target = _fork_target(user)
    service = UpdateService(
        PathwayLockRegistry(session_factory), target.client, repo=REPO, target=target
    )

    first = service.update_pathway(wpid=554, gpml=GOOD_GPML, submitter="alice")
    assert (FORK, first.branch) in user.branches
    assert first.head_repo == FORK

    # Re-uploading while still checked out must land on the same pull request, not a new one.
    second = service.update_pathway(wpid=554, gpml=GOOD_GPML, submitter="alice")
    assert second.pr_number == first.pr_number
    assert len(user.pulls) == 1


# ---- the real client --------------------------------------------------------------------------


def test_ensure_fork_reads_the_name_off_the_response_and_waits_for_readiness():
    """Two things the real API does that a naive implementation gets wrong.

    The fork may not be named after the parent — a submitter who already has a repository of that
    name gets something else, and guessing sends every later write to the wrong place. And a 202
    means accepted, not ready: creation is asynchronous, so the repository has to be probed.
    """
    calls: list[str] = []
    probes = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(202, json={"full_name": "alice/sandbox-wp-db-1"})
        if request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "upstream-head"}})
        if request.method == "PATCH":
            return httpx.Response(200, json={"object": {"sha": "upstream-head"}})
        probes["n"] += 1
        if probes["n"] < 3:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"full_name": "alice/sandbox-wp-db-1"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    # Patch the sleep out rather than waiting three real seconds.
    import app.github.client as client_module

    original = client_module.time.sleep
    client_module.time.sleep = lambda _s: None
    try:
        assert client.ensure_fork(REPO) == "alice/sandbox-wp-db-1"
    finally:
        client_module.time.sleep = original

    assert calls[0] == f"POST /repos/{REPO}/forks"
    # Three probes until it answers, then the fast-forward to its parent: read the parent's head,
    # then point the fork's own ref at it.
    assert calls[1:4] == ["GET /repos/alice/sandbox-wp-db-1"] * 3
    assert calls[4] == f"GET /repos/{REPO}/git/ref/heads/main"
    assert calls[5] == "PATCH /repos/alice/sandbox-wp-db-1/git/refs/heads/main"


def test_ensure_fork_raises_when_the_fork_never_becomes_readable():
    """Which the caller turns into a bot fallback rather than a failed submission."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"full_name": FORK})
        return httpx.Response(404, json={"message": "Not Found"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    import app.github.client as client_module

    original = client_module.time.sleep
    client_module.time.sleep = lambda _s: None
    try:
        with pytest.raises(GitHubError, match="did not become readable"):
            client.ensure_fork(REPO)
    finally:
        client_module.time.sleep = original


# ---- who writes a revise ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "identity,head_repo,expect_user",
    [
        # A branch on a personal fork can only be written by the person who owns it: a GitHub App
        # installation token cannot push there, because the App is not installed on their account.
        ("fork", FORK, True),
        ("bot", FORK, True),
        # A branch on the content repo follows the configured identity, as it always did.
        ("bot", None, False),
        ("fork", None, False),
        ("user", None, True),
    ],
)
def test_revise_writes_with_whoever_can_reach_the_branch(identity, head_repo, expect_user):
    from app.main import _writer_client_for_revise

    user = FakeGitHubClient(login="alice")
    bot = FakeGitHubClient(login="wp-bot")
    settings = Settings(submit_identity=identity, session_secret="x" * 32)

    chosen = _writer_client_for_revise(settings, user, bot, head_repo)
    assert (chosen is user) is expect_user


# ---- logging ------------------------------------------------------------------------------


def test_the_app_loggers_actually_have_somewhere_to_write(caplog):
    """No application log line had ever reached production before 2026-08-03.

    Uvicorn configures only its own loggers and nothing here called ``basicConfig``, so INFO
    records were dropped entirely — including the lock/reservation hold times added in the
    previous round specifically so the TTLs could be corrected against real behaviour, and now
    the fork-mode fallback's explanation of why it fell back.
    """
    import logging as _logging

    from app.main import _configure_logging

    _configure_logging(Settings(session_secret="x" * 32))
    logger = _logging.getLogger("wpsubmit.submit.targets")
    assert logger.getEffectiveLevel() <= _logging.INFO
    assert _logging.getLogger("wpsubmit").handlers, "no handler: INFO records go nowhere"


def test_configuring_logging_twice_does_not_stack_handlers():
    """Every test that builds an app calls this, and so does every worker reload."""
    import logging as _logging

    from app.main import _configure_logging

    settings = Settings(session_secret="x" * 32)
    _configure_logging(settings)
    before = len(_logging.getLogger("wpsubmit").handlers)
    _configure_logging(settings)
    _configure_logging(settings)
    assert len(_logging.getLogger("wpsubmit").handlers) == before


# ---- what the fake could not catch ------------------------------------------------------------


def test_open_pull_request_reads_the_head_off_githubs_answer_not_the_request():
    """Regression for the first two fork submissions this app ever opened (PRs #23/#24).

    `open_pull_request` echoed the `head` it was *asked for* and never parsed `head_repo`, so a
    cross-repository pull request was recorded as if its branch were on the base repo. Every later
    branch-side lookup then goes to the wrong repository and **revise raises NoPendingSubmission**
    — a curator requesting changes leaves the submitter with no way to answer.

    `FakeGitHubClient` parsed both correctly, which is exactly why the whole suite agreed while
    production did not. The only thing that catches this is asserting against a real-shaped API
    response.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "number": 23,
                "html_url": "https://github.com/marvinm2/sandbox-wp-db/pull/23",
                # GitHub answers with the branch alone in `head.ref`, and names the repository
                # separately — it does not echo the `owner:branch` string it was sent.
                "head": {
                    "ref": "WP0001_MadhushriMSV_20260804-072022",
                    "repo": {"full_name": "MadhushriMSV/sandbox-wp-db"},
                },
            },
        )

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    pr = client.open_pull_request(
        "marvinm2/sandbox-wp-db",
        head="MadhushriMSV:WP0001_MadhushriMSV_20260804-072022",
        base="main",
        title="t",
        body="b",
    )
    assert pr.head_repo == "MadhushriMSV/sandbox-wp-db"
    # The branch alone, with no owner prefix: it is used as a ref against the head repo.
    assert pr.head_branch == "WP0001_MadhushriMSV_20260804-072022"


def test_open_pull_request_leaves_head_repo_none_for_a_same_repo_pull_request():
    """None means "the content repo", which is what every non-fork submission must keep."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "number": 9,
                "html_url": "https://github.com/wikipathways/sandbox-wp-db/pull/9",
                "head": {"ref": "submit/WP5637", "repo": {"full_name": REPO}},
            },
        )

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    pr = client.open_pull_request(REPO, head="submit/WP5637", base="main", title="t", body="b")
    assert pr.head_repo is None
    assert pr.head_branch == "submit/WP5637"


def test_every_logger_is_under_the_wpsubmit_parent():
    """Logging is configured on `wpsubmit`, so a logger outside it writes nowhere.

    `app/review/service.py` used `__name__` and was therefore silent even after the handler was
    added — the fix for one silence leaving another in place. A convention that is only true by
    habit is one module away from being false, so it is asserted rather than remembered.
    """
    import ast
    from pathlib import Path

    offenders = []
    for path in sorted((Path(__file__).parent.parent / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getLogger"
                and node.args
            ):
                arg = node.args[0]
                name = arg.value if isinstance(arg, ast.Constant) else None
                if name != "wpsubmit" and not (
                    isinstance(name, str) and name.startswith("wpsubmit.")
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"loggers outside the wpsubmit tree write nowhere: {offenders}"


# ---- a submitter whose authorisation has lapsed ------------------------------------------------


def test_create_branch_names_the_repository_it_was_refused_on():
    """The only report of this failure that reached a human said `create_branch(update/WP5427)
    failed: 404` — no repository, so it was impossible to tell from the message alone whether the
    app had aimed at the fork or the base. GitHub answers a forbidden write with 404 rather than
    403, which removes the other clue."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    with pytest.raises(WriteDenied) as exc:
        client.create_branch(FORK, "update/WP5427", "sha1")
    assert FORK in str(exc.value)
    assert "cannot write there" in str(exc.value)


def test_a_refused_fork_push_falls_back_to_the_bot():
    """Real failure, 2026-08-04: a submitter's token still *read* fine — the fork resolved, the
    base was read — and then `POST /git/refs` came back 404. Her submission died with a 502 on
    work she had already done.

    `create_branch` is the first mutating call in the flow, so nothing exists yet and the bot can
    take over cleanly. A bot-authored pull request is worse than her own and much better than
    losing the upload.
    """
    user = FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"},
        login="alice",
        deny_writes_to={FORK},
    )
    bot = FakeGitHubClient(default_branches={f"{REPO}#main": "upstream-head"}, login="wp-bot")

    target = _fork_target(user)
    assert target.branch_repo == FORK  # the fork resolved fine; it is the *push* that is refused

    with pytest.raises(WriteDenied):
        _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")

    retry = bot_fallback_target(bot, REPO, submitter="alice", reason="denied")
    assert retry is not None and retry.identity == "bot"
    result = _service(user, retry).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    assert (REPO, result.branch) in bot.branches
    assert result.head_repo is None


def test_nothing_is_left_behind_on_the_fork_when_the_push_is_refused():
    """What makes retrying at this point sound rather than merely convenient."""
    user = FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"},
        login="alice",
        deny_writes_to={FORK},
    )
    target = _fork_target(user)
    with pytest.raises(WriteDenied):
        _service(user, target).submit_new_pathway(gpml=GOOD_GPML, submitter="alice")
    # The fork's own `main` is there because the fork exists, not because the submission got
    # anywhere. What must be absent is any *submission* branch, file or pull request.
    assert [b for (r, b) in user.branches if r == FORK] == ["main"]
    assert user.files == {}
    assert user.pulls == []


def test_no_bot_means_the_refusal_still_surfaces():
    """Falling back to nothing would turn a clear permission error into a silent no-op."""
    assert bot_fallback_target(None, REPO, submitter="alice", reason="denied") is None


def test_a_denied_write_reports_what_the_token_is_actually_allowed_to_do():
    """A write refused as 404 looks exactly like a missing repository, and the two have completely
    different answers. Asking GitHub what the token carries is the one question that separates
    them, and 2026-08-04 was spent unable to ask it: signing in again did not restore the write,
    so "the authorisation lapsed" stopped explaining anything.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(
                200, json={"login": "alice"}, headers={"x-oauth-scopes": "read:user"}
            )
        return httpx.Response(404, json={"message": "Not Found"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    with pytest.raises(WriteDenied) as exc:
        client.create_branch(FORK, "b", "sha1")
    assert "token scopes: read:user" in str(exc.value)


def test_the_scope_probe_never_replaces_the_error_it_is_describing():
    """A diagnostic that can fail would swallow the failure being reported."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            raise httpx.ConnectError("network gone")
        return httpx.Response(403, json={"message": "Forbidden"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    with pytest.raises(WriteDenied) as exc:
        client.create_branch(FORK, "b", "sha1")
    assert "token scopes: unknown" in str(exc.value)
    assert FORK in str(exc.value)


def test_a_denied_write_quotes_what_github_actually_said():
    """The status code alone is not enough, and finding that out cost an hour.

    The first report of this failure carried GitHub's body because it came through `_raise_for`.
    The "better" error that replaced it kept the repository and added the token scopes — and
    dropped the one field saying what GitHub objected to, which left a bare 404 to theorise
    against. An error that removes evidence is worse than the one it replaced.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={}, headers={"x-oauth-scopes": "public_repo"})
        return httpx.Response(404, json={"message": "Not Found", "status": "404"})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    with pytest.raises(WriteDenied) as exc:
        client.create_branch(FORK, "update/WP5427", "sha1")
    text = str(exc.value)
    assert "Not Found" in text          # what GitHub said
    assert FORK in text                 # where
    assert "public_repo" in text        # with what


def test_ensure_fork_brings_the_fork_level_with_its_parent():
    """A fork only holds the objects its parent had when it was created.

    Everything pushed upstream afterwards is readable through the shared network but is not the
    fork's own, and branching from such a commit is where 2026-08-04's `404 Not Found` came from —
    for a submitter whose token, scopes and ownership were all correct, and whose first submission
    minutes earlier had worked only because the fork was seconds old and still level. Syncing
    first removes the question instead of answering it.
    """
    calls: list[str] = []
    patched: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path.endswith("/forks"):
            return httpx.Response(
                202,
                json={
                    "full_name": FORK,
                    "parent": {"default_branch": "main"},
                    # Network root is a third repository, so this is a fork of a fork.
                    "source": {"full_name": "wikipathways/upstream-of-the-target"},
                },
            )
        if request.url.path == f"/repos/{REPO}/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "parent-head"}})
        if request.method == "PATCH":
            patched.update(json.loads(request.content))
            return httpx.Response(200, json={"object": {"sha": "parent-head"}})
        return httpx.Response(200, json={"full_name": FORK})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    assert client.ensure_fork(REPO) == FORK

    # It must move the fork's own ref to the *parent's* head. `merge-upstream` cannot do this job
    # (issue #29): it syncs against the network **source**, and a submitter's fork of an already-
    # forked content repo is a fork of a fork, so it aimed at a third repository the branch is
    # never cut from and had nothing to do.
    assert f"PATCH /repos/{FORK}/git/refs/heads/main" in calls
    assert not any("merge-upstream" in c for c in calls)
    assert patched == {"sha": "parent-head", "force": False}


def test_the_production_topology_syncs_with_merge_upstream():
    """When the fork's network source **is** the content repo — a submitter forking
    `wikipathways/wikipathways-database`, which is a root — `merge-upstream` is both correct and
    the only thing that works.

    A direct ref update cannot substitute: it can only point a ref at an object the fork already
    holds, and a commit merely readable through the shared network is refused with 404. So this
    is not a stylistic choice between two equivalent calls (issue #29)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path.endswith("/forks"):
            return httpx.Response(
                202,
                json={
                    "full_name": FORK,
                    "parent": {"default_branch": "main"},
                    "source": {"full_name": REPO},  # the content repo IS the network root
                },
            )
        return httpx.Response(200, json={"full_name": FORK})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    assert client.ensure_fork(REPO) == FORK
    assert f"POST /repos/{FORK}/merge-upstream" in calls
    assert not any("PATCH" in c for c in calls), "must not try the ref update in this topology"


def test_a_fork_that_cannot_fast_forward_still_submits():
    """A submitter's own commits on their default branch are theirs to keep. The sync is an
    optimisation of the *next* step, not a precondition, so it must never fail the submission.

    `force: False` is what enforces that — GitHub refuses the non-fast-forward itself — so the
    case to prove here is that its refusal is logged and swallowed, not raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/forks"):
            return httpx.Response(202, json={"full_name": FORK})
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "Update is not a fast forward"})
        if request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "parent-head"}})
        return httpx.Response(200, json={"full_name": FORK})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    assert client.ensure_fork(REPO) == FORK  # no raise


def test_a_sync_that_cannot_read_the_parent_still_submits():
    """The read is as best-effort as the write. A malformed or refused answer from the parent
    must degrade to "no sync" rather than take down a submission that would otherwise work."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/forks"):
            return httpx.Response(202, json={"full_name": FORK})
        if request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"unexpected": "shape"})
        return httpx.Response(200, json={"full_name": FORK})

    client = HttpGitHubClient(
        token="t", transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    assert client.ensure_fork(REPO) == FORK  # no raise


def test_an_inherited_update_branch_is_a_collision_not_a_refusal(session_factory):
    """A fork inherits every branch its parent had, so `update/WP<id>` is taken before the
    submitter has ever edited that pathway. GitHub reports the duplicate as 404 to a
    `public_repo` token, which made a routine collision look like a permission failure and sent
    two real update attempts to the bot fallback."""
    user = FakeGitHubClient(
        default_branches={f"{REPO}#main": "upstream-head"},
        existing_files={f"{REPO}#pathways/WP554/WP554.gpml": "blob1"},
        login="alice",
    )
    # The parent already carries an update branch for this pathway; the fork inherits it.
    user.branches[(REPO, "update/WP554")] = "someone-elses-old-edit"
    target = _fork_target(user)
    assert (FORK, "update/WP554") in user.branches, "the fake must inherit parent branches"

    result = UpdateService(
        PathwayLockRegistry(session_factory), target.client, repo=REPO, target=target
    ).update_pathway(wpid=554, gpml=GOOD_GPML, submitter="alice")

    # Stepped past the inherited name onto a fresh one, on the fork, as a cross-repo pull request.
    assert result.branch != "update/WP554"
    assert (FORK, result.branch) in user.branches
    assert result.head_repo == FORK


def test_a_revoked_authorisation_does_not_fall_back_to_the_bot():
    """Issue #28's real complaint, and the one part of it that was a genuine defect.

    Every other `ensure_fork` failure is transient or environmental, so a bot-authored pull
    request beats losing the upload. A revoked authorisation is neither: the fallback would
    reattribute that person's every future submission to the bot, silently and forever, with
    nothing anywhere telling them to sign in — which is exactly what fork mode exists to prevent.
    """
    user = _user_client(reject_credentials=True)
    with pytest.raises(CredentialsRejected):
        resolve_write_target(
            identity="fork",
            user_client=user,
            bot_client=FakeGitHubClient(login="wikipathways-bot"),
            content_repo=REPO,
            submitter="alice",
        )


def test_an_ordinary_fork_failure_still_falls_back_to_the_bot():
    """The contrast that makes the case above a decision rather than an accident."""
    user = _user_client(fail_on={"ensure_fork"})
    target = resolve_write_target(
        identity="fork",
        user_client=user,
        bot_client=FakeGitHubClient(login="wikipathways-bot"),
        content_repo=REPO,
        submitter="alice",
    )
    assert target.identity == "bot"

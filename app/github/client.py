"""GitHub client: the minimal surface the submission flow needs, plus a fake and an httpx impl.

The submission flow (open a PR that adds one GPML file) needs exactly four operations:
resolve a branch's head SHA, create a branch, create a file on it, and open a PR. Keeping the
interface this small makes the fake trivial and keeps the real client honest.
"""
from __future__ import annotations

import base64
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

_GITHUB_API = "https://api.github.com"

#: How long ``ensure_fork`` waits for an asynchronously-created fork to answer. GitHub documents
#: forking as taking up to five minutes; waiting that long inside a submission request would be
#: worse than falling back to the bot, so this covers the ordinary case (a fork is usually ready
#: in a second or two) and hands the slow tail to the fallback.
_FORK_READY_ATTEMPTS = 15
_FORK_READY_DELAY_SECONDS = 1.0


def _head_repo_of(pull: dict, base_repo: str) -> str | None:
    """``owner/name`` of a pull request's head, or None when it is the base repo itself.

    GitHub nulls ``head.repo`` once the fork behind a pull request is deleted, which is why the
    absent case is folded into "same as base" rather than raising: the only thing downstream does
    with it is scope a branch lookup, and a deleted fork has no branch to look up either way.
    """
    head = (pull.get("head") or {}).get("repo") or {}
    full_name = head.get("full_name")
    return full_name if full_name and full_name != base_repo else None


@dataclass(frozen=True)
class PullRequest:
    number: int
    html_url: str
    head_branch: str
    #: ``owner/name`` of the repository the head branch lives on, when that is *not* the base
    #: repository. None means the head is on the base repo, which is every pull request this app
    #: opens today. A branch name identifies an edit only within one repository, so anything that
    #: reads a head branch back — revise above all — has to carry this alongside it (issue #22).
    head_repo: str | None = None


@dataclass(frozen=True)
class PullRequestDetail:
    """Everything the publication handshake needs from one PR read.

    Deliberately a single fetch: reconciling a review against a target repo that publishes via
    its own Actions needs the state, the merge flag, the labels and the body together, and doing
    that as four calls would multiply the dashboard's request count by the size of the queue.
    """

    number: int
    html_url: str
    head_branch: str
    head_sha: str
    state: str  # "open" | "closed"
    merged: bool
    title: str
    body: str
    labels: list[str]
    author: str
    #: ``owner/name`` the head branch lives on, None meaning the base repo. Same meaning as on
    #: ``PullRequest``. Carried here so a reconcile can *repair* a review row whose head repo was
    #: never recorded — which is every cross-repository submission opened before 2026-08-04.
    head_repo: str | None = None


@dataclass(frozen=True)
class PrFile:
    """One file in a pull request's diff.

    ``status`` is GitHub's own word — "added", "modified", "removed", "renamed" — and it is worth
    carrying because it answers "is this pathway already on the base branch?" without a second
    read. It is a cross-check rather than an authority: the target repository classifies a
    submission by *filename*, so adoption does too, and a disagreement is recorded rather than
    acted on (``app.review.adopt``).
    """

    filename: str
    status: str
    previous_filename: str | None = None


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    html_url: str
    status: str  # "queued" | "in_progress" | "completed"
    conclusion: str | None  # "success" | "failure" | ... | None while running
    created_at: str


def _workflow_run(data: dict) -> WorkflowRun:
    return WorkflowRun(
        id=data["id"],
        html_url=data.get("html_url", ""),
        status=data.get("status", ""),
        conclusion=data.get("conclusion"),
        created_at=data.get("created_at", ""),
    )


class GitHubError(RuntimeError):
    """A GitHub operation failed."""


class BranchAlreadyExists(GitHubError):
    """The branch to be created already exists (e.g. a resubmission collided)."""


class CredentialsRejected(GitHubError):
    """GitHub refused the token itself — 401, not a permission or a missing object.

    For this app's user identity this means exactly one thing: **the authorisation is gone.**
    The user-facing credential is an OAuth App token, and those do not expire on a timer —
    expiring user tokens with refresh tokens are a *GitHub App* feature, and a GitHub App user
    token carries no scopes at all, whereas these report ``public_repo, read:user``. So the ways
    one dies are all revocations: the user revoked the app, GitHub revoked it after seeing the
    token in public, an admin revoked it, or it went a year unused.

    None of those is a server fault, which is why the write paths map it to **401** rather than
    the 502 every other ``GitHubError`` gets: telling a submitter the server is broken when the
    fix is "sign in again" sends them to the wrong person. Issue #28 filed this as a token-refresh
    problem; refreshing is not available for an OAuth App, and revocation is all that remains.

    It stays a ``GitHubError`` subclass on purpose. Roughly twenty best-effort call sites swallow
    ``GitHubError`` so a cosmetic action — a mirror comment, a reconcile — never fails the thing
    it decorates, and those should go on swallowing this too. Breaking the inheritance would turn
    every one of them into a login prompt.

    ``identity`` says **whose** credential was refused, because the answers differ completely: a
    submitter's OAuth token means "sign in again", while the bot's App installation token means
    the deployment is misconfigured and no amount of signing in will help.
    """

    def __init__(self, message: str, *, identity: str = "user") -> None:
        super().__init__(message)
        self.identity = identity


class WriteDenied(GitHubError):
    """The acting token may not write where it was asked to.

    Raised only from ``create_branch``, which is the **first** mutating call in every write path —
    so a caller catching this knows nothing has been created yet and may safely retry under a
    different identity. That is what makes the bot fallback sound here and unsound later on.

    Its own case: a token that genuinely may read a repository and not write it.

    **The 2026-08-04 incident this used to cite was not one.** "Reads fine, `POST /git/refs` 404,
    on a fork the submitter owns" was issue #29 — the ref named a commit the fork did not hold —
    and it was attributed here to an authorisation lapsing mid-session, which was wrong. A
    genuinely dead token answers **401** to everything including reads, and that is
    ``CredentialsRejected``, not this. Kept because the distinction is the useful part: this one
    is worth a bot fallback, that one is worth asking the person to sign in again.
    """


class GitHubClient(ABC):
    @abstractmethod
    def ensure_fork(self, repo: str) -> str:
        """Return ``owner/name`` of the acting user's fork of ``repo``, creating it if absent.

        This is how a submitter with no push access contributes: fork, push a branch there, and
        open a cross-repository pull request (issue #22). It needs no scope the app does not
        already hold — GitHub defines ``public_repo``, which the app requests today, as read/write
        to code on public repositories, and that covers creating a fork of one and pushing to it.

        Must be **idempotent**: called on every submission, and a submitter who has contributed
        before already has the fork. Creation is asynchronous on GitHub's side, so an
        implementation has to wait for the repository to become readable rather than assume the
        response means ready.

        Raises ``GitHubError`` if no fork can be had. That is a routine outcome, not an
        exceptional one — an organisation can forbid forking, GitHub can be down, a fork can still
        be materialising — so the caller falls back to the bot identity rather than failing the
        submission (``app.submit.targets``).

        ``CredentialsRejected`` is the one exception and is **not** absorbed: a revoked
        authorisation is permanent, so falling back would reattribute that person's every future
        submission to the bot without ever telling them why (issue #28).
        """

    @abstractmethod
    def get_branch_sha(self, repo: str, branch: str) -> str:
        """Return the head commit SHA of ``branch`` in ``owner/repo``."""

    @abstractmethod
    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        """Create ``new_branch`` at ``from_sha``; raises BranchAlreadyExists on conflict."""

    @abstractmethod
    def get_file_sha(self, repo: str, ref: str, path: str) -> str | None:
        """Return the blob SHA of ``path`` at ``ref`` (branch/sha), or None if absent."""

    @abstractmethod
    def get_file_content(self, repo: str, ref: str, path: str) -> bytes | None:
        """Return the raw bytes of ``path`` at ``ref`` (branch/sha), or None if absent."""

    @abstractmethod
    def put_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        """Create or update a text file on ``branch``.

        ``sha`` is the current blob SHA of the file and is **required to update** an existing
        file (GitHub rejects an update without it); omit it to create a new file.
        """

    @abstractmethod
    def delete_file(self, repo: str, branch: str, path: str, message: str, *, sha: str) -> None:
        """Remove ``path`` from ``branch``. ``sha`` is the blob SHA being deleted.

        Exists for exactly one caller: taking the submission placeholder back off the base
        branch after somebody merged a pipeline pull request. Nothing else in the app removes
        content from the content repository.
        """

    @abstractmethod
    def find_open_pr(
        self, repo: str, head_branch: str, *, head_repo: str | None = None
    ) -> PullRequest | None:
        """Return the open PR on ``repo`` whose head is ``head_branch``, or None.

        ``head_repo`` (``owner/name``) is where that branch lives. It defaults to ``repo``,
        which is every pull request this app opens today. GitHub's ``head`` filter is
        ``owner:branch``, so a cross-repository pull request — one from a contributor's fork,
        and 36 of the last 53 on the content repository are exactly that — is invisible to a
        query that assumes the base owner. Reading None there is not a harmless miss: revise
        raises ``NoPendingSubmission`` and an update opens a *second* pull request (issue #22).
        """

    @abstractmethod
    def find_open_pr_touching(
        self, repo: str, path_prefix: str, *, limit: int = 40
    ) -> int | None:
        """The number of an open PR that changes a file under ``path_prefix``, or None.

        This is what makes the pathway check-out lock more than an app-internal flag: a power
        user can open a raw pull request against the content repo without going near this app,
        and starting a second edit of the same GPML on top of that is the unmergeable-divergence
        failure the lock exists to prevent (design §4.3).

        There is no GitHub query for "open PRs touching this path", so this walks the open pull
        requests and reads each one's file list, newest first, stopping at ``limit``. That makes
        it too expensive for a page render; it belongs on the update write path, which happens a
        few times a day.
        """

    @abstractmethod
    def list_pr_files(self, repo: str, pr_number: int, *, limit: int = 300) -> list[PrFile]:
        """Every file a pull request changes, as ``PrFile`` records.

        Addressed to the **base** repository, which serves a fork's pull request as readily as its
        own — so this needs no access to a submitter's fork and keeps working after the fork is
        deleted.

        Raises ``GitHubError`` when the listing cannot be read. That matters to
        ``find_open_pr_touching``, whose whole job is to be sure: one unreadable pull request must
        not be read as "nothing touches this pathway".
        """

    @abstractmethod
    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        """Open a PR from ``head`` into ``base`` and return it."""

    @abstractmethod
    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        """Post a new comment on an issue/PR (not upserted) — e.g. a curator's change request."""

    @abstractmethod
    def request_pr_reviewer(self, repo: str, pr_number: int, reviewer: str) -> None:
        """Request ``reviewer`` as a reviewer on the PR (mirrors an app-side assignment).

        GitHub refuses to request a review from the PR author or a non-collaborator, so callers
        treat this as best-effort."""

    @abstractmethod
    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        """State of a PR: ``"open"``, ``"closed"``, ``"merged"``, or None if it does not exist.

        Used to reconcile review rows against PRs closed/merged outside the app (issue #1)."""

    @abstractmethod
    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        """Merge a PR. Raises GitHubError if the merge is not allowed."""

    @abstractmethod
    def upsert_issue_comment(
        self, repo: str, issue_number: int, body: str, *, marker: str
    ) -> None:
        """Post ``body`` as an issue/PR comment, or update the existing one carrying ``marker``.

        ``marker`` is a hidden token (an HTML comment) embedded in the body so the app keeps a
        single read-only *mirror* comment in sync instead of spamming the PR on every update.
        A privileged (bot) operation — the mirror must not be attributed to the submitter.
        """

    @abstractmethod
    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        """Return the logins of the members of ``org/team_slug`` (curator whitelist, issue #9)."""

    @abstractmethod
    def pr_preview_status(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> str:
        """State of the PR-preview render for ``pr_number`` (issue #11), *without* downloading it.

        One of: ``"pending"`` (workflow not run / still running), ``"ready"`` (completed
        successfully with the preview artifact present), ``"failed"`` (run failed / artifact
        missing), ``"absent"`` (no such PR). Cheap: no artifact bytes are transferred.
        """

    # --- Labels -------------------------------------------------------------------------
    # On a target repo that publishes through its own Actions, a label is not decoration: its
    # label dispatcher turns `accepted`/`rejected` into workflow runs. Applying one is the
    # approval, so these are write operations with real consequences.

    @abstractmethod
    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        """Add labels to an issue/PR, leaving existing ones in place."""

    @abstractmethod
    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        """Remove one label. A label that isn't there is not an error."""

    @abstractmethod
    def list_labels(self, repo: str, issue_number: int) -> list[str]:
        """Return the label names currently on an issue/PR."""

    # --- Pull-request reads -------------------------------------------------------------

    @abstractmethod
    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestDetail | None:
        """Full PR state in one call, or None if it does not exist."""

    @abstractmethod
    def list_issue_comments(self, repo: str, issue_number: int) -> list[str]:
        """Return the bodies of an issue/PR's comments, oldest first.

        The target repo's publish workflow announces the assigned WPID in a comment, which
        survives the PR-description rewrites its own pipeline performs.
        """

    @abstractmethod
    def close_pull_request(self, repo: str, pr_number: int) -> None:
        """Close a PR without merging."""

    # --- Actions (read-only) ------------------------------------------------------------

    @abstractmethod
    def latest_workflow_run_for_pr(
        self, repo: str, pr_number: int, *, workflow_file: str
    ) -> WorkflowRun | None:
        """Newest run of ``workflow_file`` against the PR's head SHA, or None if there is none."""

    @abstractmethod
    def recent_workflow_runs(
        self, repo: str, workflow_file: str, *, limit: int = 5
    ) -> list[WorkflowRun]:
        """Newest runs of ``workflow_file``, whatever triggered them.

        For display only. A ``workflow_dispatch`` run carries no head SHA or PR reference, so
        there is no reliable way to join it to a review — never drive state from this.
        """


class FakeGitHubClient(GitHubClient):
    """In-memory GitHubClient for tests. Records every mutation; can simulate failures.

    Pass ``fail_on={"open_pull_request"}`` to make that operation raise — used to prove the
    submission service rolls back the reserved WPID when the PR step fails.
    """

    def __init__(
        self,
        *,
        default_branches: dict[str, str] | None = None,
        existing_files: dict[str, str] | None = None,
        existing_contents: dict[str, str] | None = None,
        fail_on: set[str] | None = None,
        team_members: dict[str, list[str]] | None = None,
        previews: dict[int, dict] | None = None,
        login: str = "submitter",
        deny_writes_to: set[str] | None = None,
        fork_can_sync: bool = True,
        reject_credentials: bool = False,
        identity: str = "user",
    ) -> None:
        #: Models a revoked authorisation: GitHub answers 401 to *everything*, reads included,
        #: which is what separates it from ``deny_writes_to`` (a token that reads and cannot
        #: write). The distinction matters because only one of the two is fixed by signing in
        #: again, and the app has to tell the submitter which (issue #28).
        self.reject_credentials = reject_credentials
        #: "user" or "bot", mirroring ``HttpGitHubClient.identity``. Without it this fake reports
        #: every rejection as the user's even when standing in for the bot, and a test would then
        #: watch a deployment fault be dressed up as a login prompt and call it a pass.
        self.identity = identity
        #: Who this client is acting as, which is what ``ensure_fork`` names the fork after.
        self.login = login
        #: Whether ``ensure_fork`` manages to bring the fork level with its parent. False models
        #: the case the app cannot avoid — a fork of a fork, where `merge-upstream` aims at the
        #: network source and a direct ref update is refused (issue #29) — leaving the fork
        #: holding only the objects the test seeded on it. Without this the fake mirrors every
        #: parent branch onto the fork, so a fork is *never* observably behind and no test can
        #: reach the failure that took a day to find.
        self.fork_can_sync = fork_can_sync
        #: Repositories this identity may read but not write — a lapsed authorisation, or a token
        #: scoped narrower than the caller assumed. ``create_branch`` is where that first shows.
        self.deny_writes_to = set(deny_writes_to or ())
        #: [(repo, fork)] in call order, so a test can prove the fork was ensured once per
        #: submission and that a second submission reused it rather than asking again.
        self.forks_created: list[tuple[str, str]] = []
        # {(repo, branch): sha}
        self.branches: dict[tuple[str, str], str] = {}
        for key, sha in (default_branches or {}).items():
            repo, branch = key.split("#", 1)
            self.branches[(repo, branch)] = sha
        # Files already committed in the repo (visible from any branch cut off the base).
        # Seeded as {"repo#path": blob_sha}.
        self.existing_files: dict[tuple[str, str], str] = {}
        for key, sha in (existing_files or {}).items():
            repo, path = key.split("#", 1)
            self.existing_files[(repo, path)] = sha
        # Base-branch file *contents* (for get_file_content), seeded as {"repo#path": content}.
        self.existing_contents: dict[tuple[str, str], str] = {}
        for key, content in (existing_contents or {}).items():
            repo, path = key.split("#", 1)
            self.existing_contents[(repo, path)] = content
        # {(repo, branch, path): (content, message, sha)}
        self.files: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
        self.pulls: list[PullRequest] = []
        # {pr_number: {"title", "body", "base", "head"}} — the human-facing PR fields the API
        # sends. Captured so tests can assert on the title/body a reviewer actually sees.
        self.pull_meta: dict[int, dict[str, str]] = {}
        self.merged: set[int] = set()
        # PRs closed without merging (tests set this to simulate an out-of-band close).
        self.closed: set[int] = set()
        # {pr_number: [reviewer, ...]} — reviewers requested via request_pr_reviewer.
        self.review_requests: dict[int, list[str]] = {}
        # {(repo, issue_number): [body, ...]} — plain comments posted via create_issue_comment.
        self.issue_comments: dict[tuple[str, int], list[str]] = {}
        # {(repo, issue_number): {marker: body}} — one comment per marker (upsert semantics).
        self.comments: dict[tuple[str, int], dict[str, str]] = {}
        # {"org/team-slug": [login, ...]}
        self.team_members = dict(team_members or {})
        # {pr_number: {"status": str}} — PR-preview CI state, the merge gate (issue #11).
        self.previews = dict(previews or {})
        # {(repo, issue_number): {label, ...}}
        self.labels: dict[tuple[str, int], set[str]] = {}
        # [(repo, issue_number, "add"|"remove", label)] — ordered, so a test can assert that the
        # reason comment was posted *before* the label that triggers the repo's workflow.
        self.label_log: list[tuple[str, int, str, str]] = []
        # {(repo, workflow_file): [WorkflowRun, ...]}, newest last.
        self.workflow_runs: dict[tuple[str, str], list[WorkflowRun]] = {}
        # [(repo, branch, path, message)] — every delete_file, so a test can assert the repair
        # touched exactly the placeholder and nothing else.
        self.deleted: list[tuple[str, str, str, str]] = []
        #: {(repo, commit_sha, path): content} — contents addressed by a *commit*, which is how a
        #: pull request the app did not open is read. Without it ``get_file_content`` falls
        #: through to ``existing_contents``, which is keyed on (repo, path) alone: an adopted
        #: update would then render the base branch as both "before" and "after", the diff would
        #: report nothing changed, and every scoped checklist item would resolve N/A — with the
        #: whole suite green, because the fake had answered a question it cannot actually answer.
        self.commit_contents: dict[tuple[str, str, str], str] = {}
        #: {(repo, pr_number): [PrFile, ...]} — a pull request's diff, for ``list_pr_files``.
        self.pr_files: dict[tuple[str, int], list[PrFile]] = {}
        self.fail_on = fail_on or set()
        self._next_pr = 1
        self._next_run = 1000

    def _maybe_fail(self, op: str) -> None:
        if self.reject_credentials:
            raise CredentialsRejected(
                f'{op} failed: 401 {{"message":"Bad credentials"}}', identity=self.identity
            )
        if op in self.fail_on:
            raise GitHubError(f"simulated failure in {op}")

    def ensure_fork(self, repo: str) -> str:
        self._maybe_fail("ensure_fork")
        fork = f"{self.login}/{repo.split('/', 1)[1]}"
        if (repo, fork) not in self.forks_created:
            self.forks_created.append((repo, fork))
        # Stands in for the sync: the fork is brought level with its parent's default branch,
        # which is what makes a base commit the fork's own rather than merely visible through the
        # network. Note *parent*, not the network source — the distinction `merge-upstream` gets
        # wrong for a fork of a fork (issue #29).
        #
        # `fork_can_sync=False` models the topology where that cannot be done. Reachability is
        # NOT mirrored in that case, and deliberately: a fork does share its parent's object
        # database for *reads*, but a shared object is not a legal ref target, and treating the
        # two as equivalent is precisely the assumption that made this fake agree with a write
        # path GitHub rejects.
        if not self.fork_can_sync:
            return fork
        if (repo, "main") in self.branches:
            self.branches[(fork, "main")] = self.branches[(repo, "main")]
        for (r, branch), sha in list(self.branches.items()):
            if r == repo:
                self.branches.setdefault((fork, branch), sha)
        for (r, path), sha in list(self.existing_files.items()):
            if r == repo:
                self.existing_files.setdefault((fork, path), sha)
        for (r, path), content in list(self.existing_contents.items()):
            if r == repo:
                self.existing_contents.setdefault((fork, path), content)
        return fork

    def get_branch_sha(self, repo: str, branch: str) -> str:
        self._maybe_fail("get_branch_sha")
        try:
            return self.branches[(repo, branch)]
        except KeyError as exc:
            raise GitHubError(f"no such branch {branch} in {repo}") from exc

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        self._maybe_fail("create_branch")
        # Refuse writes to repositories this identity may not touch, so the fake can reproduce a
        # submitter whose authorisation has lapsed — the shape that took two real submissions down
        # on 2026-08-04 and that the previous fake could not express at all.
        if repo in self.deny_writes_to:
            raise WriteDenied(f"create_branch({new_branch}) on {repo}: denied")
        if (repo, new_branch) in self.branches:
            raise BranchAlreadyExists(f"{new_branch} already exists in {repo}")
        # A ref may only point at a commit *this* repository holds. A commit that is merely
        # readable through a shared fork network is refused with 404 — the failure that took a day
        # to identify in issue #29, and that this fake could not express at all, so 509 tests
        # agreed with a write path GitHub rejected. Sixth time the fake has been the more capable
        # of the two; modelling it here is what stops there being a seventh.
        held = {sha for (r, _), sha in self.branches.items() if r == repo}
        if held and from_sha not in held:
            raise WriteDenied(
                f"create_branch({new_branch}) on {repo}: 404 — {from_sha} is not an object "
                f"{repo} holds. A commit readable through the fork network is not a legal ref "
                f"target; cut from the branch repo's own head (WriteTarget.base_repo)."
            )
        self.branches[(repo, new_branch)] = from_sha

    def get_file_sha(self, repo: str, ref: str, path: str) -> str | None:
        self._maybe_fail("get_file_sha")
        entry = self.files.get((repo, ref, path))
        if entry is not None and entry[2] is not None:
            return entry[2]
        # Not written on this branch yet → fall back to what exists in the repo base.
        return self.existing_files.get((repo, path))

    def get_file_content(self, repo: str, ref: str, path: str) -> bytes | None:
        self._maybe_fail("get_file_content")
        entry = self.files.get((repo, ref, path))
        if entry is not None:
            return entry[0].encode("utf-8")
        # A commit ref is answered before the branch-agnostic fallback, or an adopted pull
        # request's head reads back as the base branch and every before/after comparison in a
        # test is comparing a file with itself.
        at_commit = self.commit_contents.get((repo, ref, path))
        if at_commit is not None:
            return at_commit.encode("utf-8")
        content = self.existing_contents.get((repo, path))
        return content.encode("utf-8") if content is not None else None

    def list_pr_files(self, repo: str, pr_number: int, *, limit: int = 300) -> list[PrFile]:
        self._maybe_fail("list_pr_files")
        return list(self.pr_files.get((repo, pr_number), []))[:limit]

    def seed_foreign_pr(
        self,
        repo: str,
        number: int,
        *,
        author: str,
        title: str,
        head_branch: str,
        head_sha: str,
        files: Sequence[tuple[str, str, str]],
        body: str = "",
        head_repo: str | None = None,
    ) -> None:
        """Seed a pull request this client did not open — the whole point of adoption.

        ``files`` is [(path, status, content)]. Contents are stored against the **base** repo at
        ``head_sha``, because that is where the app reads them: GitHub serves a fork's pull
        request from the base repository at the head commit, verified against the live API on
        2026-08-21 (identical blob sha from either side).
        """
        self.pulls.append(
            PullRequest(
                number=number,
                html_url=f"https://github.com/{repo}/pull/{number}",
                head_branch=head_branch,
                head_repo=head_repo,
            )
        )
        self.pull_meta[number] = {
            "title": title,
            "body": body,
            "base": "main",
            "head": f"{head_repo.split('/')[0]}:{head_branch}" if head_repo else head_branch,
            "author": author,
            "head_sha": head_sha,
        }
        self.pr_files[(repo, number)] = [
            PrFile(filename=path, status=status) for path, status, _ in files
        ]
        for path, _, content in files:
            self.commit_contents[(repo, head_sha, path)] = content
        self._next_pr = max(self._next_pr, number + 1)

    def put_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        self._maybe_fail("put_file")
        # GitHub answers 422 ("sha" wasn't supplied) when the contents API is asked to *create*
        # a file that is already there. Modelling that here is what stops a caller from
        # assuming a path is free: the placeholder path is shared by every new submission, so
        # one stray copy of it on the base branch would otherwise break the front door.
        if sha is None and self.get_file_sha(repo, branch, path) is not None:
            raise GitHubError(f'put_file({path}) failed: 422 "sha" wasn\'t supplied')
        # A new blob sha after the write (deterministic, for assertions).
        new_sha = f"sha-{branch}-{path}-{len(content)}"
        self.files[(repo, branch, path)] = (content, message, new_sha)

    def delete_file(self, repo: str, branch: str, path: str, message: str, *, sha: str) -> None:
        """Drops the file from both the branch map and the base — the app only ever deletes on
        the base branch, so modelling a branch-local tombstone would be fiction nothing uses."""
        self._maybe_fail("delete_file")
        if self.get_file_sha(repo, branch, path) is None:
            raise GitHubError(f"delete_file({path}) failed: 404 not found")
        self.files.pop((repo, branch, path), None)
        self.existing_files.pop((repo, path), None)
        self.existing_contents.pop((repo, path), None)
        self.deleted.append((repo, branch, path, message))

    def find_open_pr(
        self, repo: str, head_branch: str, *, head_repo: str | None = None
    ) -> PullRequest | None:
        self._maybe_fail("find_open_pr")
        for pr in reversed(self.pulls):
            # "open" is part of the contract: the real client filters on state=open. Without
            # this a revise would happily commit onto a branch whose PR the target repo's
            # publish workflow already closed.
            if pr.number in self.closed | self.merged or pr.head_branch != head_branch:
                continue
            # The head repo is half the identity: `submit/WP0001` exists in every fork at once,
            # so matching on the branch alone would hand a revise somebody else's pull request.
            # Both sides normalise None to `repo`, so a same-repo caller is unaffected.
            if (pr.head_repo or repo) == (head_repo or repo):
                return pr
        return None

    def find_open_pr_touching(
        self, repo: str, path_prefix: str, *, limit: int = 40
    ) -> int | None:
        self._maybe_fail("find_open_pr_touching")
        for pr in reversed(self.pulls[-limit:]):
            if pr.number in self.closed | self.merged:
                continue
            # A fork pull request's files live on the fork, and `GET /repos/{base}/pulls/{n}/
            # files` lists them all the same — so scoping this to the base repo would make every
            # cross-repository edit invisible to the lock, which is the one writer it most needs
            # to see.
            for (r, branch, path) in self.files:
                if (
                    r == (pr.head_repo or repo)
                    and branch == pr.head_branch
                    and path.startswith(path_prefix)
                ):
                    return pr.number
        return None

    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        self._maybe_fail("open_pull_request")
        # GitHub spells a cross-repository head `owner:branch`, naming only the owner — a fork
        # keeps the base repository's name, which is how the real API resolves it too. Splitting
        # it here is what lets a test build a fork pull request the way the app would open one.
        head_owner, _, head_branch = head.rpartition(":")
        pr = PullRequest(
            number=self._next_pr,
            html_url=f"https://github.com/{repo}/pull/{self._next_pr}",
            head_branch=head_branch,
            head_repo=f"{head_owner}/{repo.split('/', 1)[1]}" if head_owner else None,
        )
        self.pull_meta[pr.number] = {"title": title, "body": body, "base": base, "head": head}
        self._next_pr += 1
        self.pulls.append(pr)
        return pr

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self._maybe_fail("create_issue_comment")
        self.issue_comments.setdefault((repo, issue_number), []).append(body)

    def request_pr_reviewer(self, repo: str, pr_number: int, reviewer: str) -> None:
        self._maybe_fail("request_pr_reviewer")
        self.review_requests.setdefault(pr_number, []).append(reviewer)

    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        self._maybe_fail("get_pull_request_state")
        if pr_number in self.merged:
            return "merged"
        if pr_number in self.closed:
            return "closed"
        if any(pr.number == pr_number for pr in self.pulls):
            return "open"
        return None

    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        self._maybe_fail("merge_pull_request")
        self.merged.add(pr_number)

    def upsert_issue_comment(
        self, repo: str, issue_number: int, body: str, *, marker: str
    ) -> None:
        self._maybe_fail("upsert_issue_comment")
        self.comments.setdefault((repo, issue_number), {})[marker] = body

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        self._maybe_fail("list_team_members")
        return list(self.team_members.get(f"{org}/{team_slug}", []))

    def pr_preview_status(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> str:
        self._maybe_fail("pr_preview_status")
        return self.previews.get(pr_number, {}).get("status", "absent")

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        self._maybe_fail("add_labels")
        current = self.labels.setdefault((repo, issue_number), set())
        for label in labels:
            current.add(label)
            self.label_log.append((repo, issue_number, "add", label))

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self._maybe_fail("remove_label")
        self.labels.setdefault((repo, issue_number), set()).discard(label)
        self.label_log.append((repo, issue_number, "remove", label))

    def list_labels(self, repo: str, issue_number: int) -> list[str]:
        self._maybe_fail("list_labels")
        return sorted(self.labels.get((repo, issue_number), set()))

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestDetail | None:
        self._maybe_fail("get_pull_request")
        pr = next((p for p in self.pulls if p.number == pr_number), None)
        if pr is None:
            return None
        meta = self.pull_meta.get(pr_number, {})
        merged = pr_number in self.merged
        return PullRequestDetail(
            number=pr.number,
            html_url=pr.html_url,
            head_branch=pr.head_branch,
            # A seeded head sha wins over the branch table: a pull request opened from a fork has
            # its branch in the *fork*, so looking it up here answers for the wrong repository.
            head_sha=meta.get("head_sha")
            or self.branches.get((repo, pr.head_branch), f"sha-{pr.head_branch}"),
            state="closed" if merged or pr_number in self.closed else "open",
            merged=merged,
            title=meta.get("title", ""),
            body=meta.get("body", ""),
            labels=sorted(self.labels.get((repo, pr_number), set())),
            author=meta.get("author", ""),
            head_repo=pr.head_repo,
        )

    def list_issue_comments(self, repo: str, issue_number: int) -> list[str]:
        self._maybe_fail("list_issue_comments")
        plain = self.issue_comments.get((repo, issue_number), [])
        upserted = list(self.comments.get((repo, issue_number), {}).values())
        return [*plain, *upserted]

    def close_pull_request(self, repo: str, pr_number: int) -> None:
        self._maybe_fail("close_pull_request")
        self.closed.add(pr_number)

    def latest_workflow_run_for_pr(
        self, repo: str, pr_number: int, *, workflow_file: str
    ) -> WorkflowRun | None:
        self._maybe_fail("latest_workflow_run_for_pr")
        runs = self.workflow_runs.get((repo, workflow_file), [])
        return runs[-1] if runs else None

    def recent_workflow_runs(
        self, repo: str, workflow_file: str, *, limit: int = 5
    ) -> list[WorkflowRun]:
        self._maybe_fail("recent_workflow_runs")
        runs = self.workflow_runs.get((repo, workflow_file), [])
        return list(reversed(runs[-limit:]))

    # --- Simulating the target repo's own pipeline --------------------------------------
    # Modelled on wikipathways/sandbox-wp-db (docs/sandbox-pipeline.md). These exist because the
    # whole integration is a handshake with workflows we do not run: label goes on, PR gets
    # closed *unmerged*, and the assigned WPID comes back in a comment. Without a way to replay
    # that sequence there is nothing to test.

    def record_workflow_run(
        self, repo: str, workflow_file: str, *, conclusion: str | None = "success"
    ) -> WorkflowRun:
        run = WorkflowRun(
            id=self._next_run,
            html_url=f"https://github.com/{repo}/actions/runs/{self._next_run}",
            status="completed" if conclusion else "in_progress",
            conclusion=conclusion,
            created_at="2026-07-27T12:00:00Z",
        )
        self._next_run += 1
        self.workflow_runs.setdefault((repo, workflow_file), []).append(run)
        return run

    def simulate_workflow1(
        self,
        repo: str,
        pr_number: int,
        *,
        ok: bool = True,
        workflow_file: str = "1_on_pull_request.yml",
    ) -> None:
        """The target repo's PR processor: it rewrites the PR body wholesale, twice.

        The body clobbering is the point — it is why the app's durable record is a comment and
        not the description.
        """
        meta = self.pull_meta.setdefault(pr_number, {})
        meta["body"] = (
            f"## Pathway Information\n\n**WPID**: WP0__PR{pr_number}\n\n---\n"
            if ok
            else "\n## Pathway Information\n\nProcessing...\n"
        )
        self.record_workflow_run(repo, workflow_file, conclusion="success" if ok else "failure")

    def simulate_3a(
        self,
        repo: str,
        pr_number: int,
        *,
        wpid: int | None,
        marker: str = "<!-- wikipathways-publish ",
        branch: str = "main",
        write_file: bool = True,
        declares: str | None = None,
    ) -> None:
        """The target repo's publish workflow: announce the WPID, then close **unmerged**.

        ``wpid=None`` replays the failure this repo has actually shown — the PR gets closed with
        no announcement, so the app has to notice rather than assume success.

        ``write_file=False`` replays the other one: the announcement arrives before the push is
        visible, which the app must read as "early", not as a failure.

        ``declares`` is what the pushed file says about *itself*, in its root ``Version``
        attribute, and defaults to the truth. Until 2026-08-14 this fake announced a WPID and
        wrote nothing, so no test could tell a correct publication from the one that actually
        happened four times running: 3a renames ``WP0__PR<n>.gpml`` to ``WP<n>.gpml`` without
        opening it, so WP5426 through WP5429 all landed still declaring ``WP0001``. Pass
        ``declares="WP0001"`` to reproduce that.
        """
        if wpid is not None:
            self.create_issue_comment(
                repo,
                pr_number,
                f'{marker}{{"pr":{pr_number},"wpid":{wpid},"status":"published"}} -->\n'
                f"Published as WP{wpid}.",
            )
            if write_file:
                version = declares or f"WP{wpid}"
                gpml = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="p" '
                    f'Version="{version}_r20260813082819" Organism="Homo sapiens">\n'
                    '  <Graphics BoardWidth="100.0" BoardHeight="100.0"/>\n'
                    "</Pathway>\n"
                )
                key = (repo, branch, f"pathways/WP{wpid}/WP{wpid}.gpml")
                self.files[key] = (gpml, f"Add files for approved pathway WP{wpid}", "publishsha")
        self.closed.add(pr_number)

    def simulate_3b(self, repo: str, pr_number: int) -> None:
        """The target repo's rejection workflow: comment, then close unmerged."""
        self.create_issue_comment(repo, pr_number, "This pull request has been rejected.")
        self.closed.add(pr_number)

    def simulate_dispatcher_failure(self, repo: str, pr_number: int) -> None:
        """The label goes on and nothing happens. Historically the most likely outcome."""
        return None


@dataclass
class HttpGitHubClient(GitHubClient):
    """Real client over the GitHub REST API (httpx). Not exercised by the unit suite.

    ``token`` is the acting identity — a per-user OAuth token so the commit/PR is attributed to
    the submitter (scaffolding-plan §3). Construct one per request with the user's token.
    """

    token: str
    base_url: str = _GITHUB_API
    transport: httpx.BaseTransport | None = None  # injection seam for tests
    #: "user" or "bot" — only used to say whose credential was refused on a 401, because the
    #: remedies have nothing in common (sign in again vs fix the App configuration).
    identity: str = "user"
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.base_url,
            transport=self.transport,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def _raise_for(self, resp: httpx.Response, what: str) -> None:
        # 401 is the token being rejected outright, which is a revoked authorisation rather than a
        # server fault — separated here so it can reach the submitter as "sign in again" instead
        # of a 502 (issue #28). Checked before `is_error` so it cannot be swallowed by the general
        # case, and it carries GitHub's body like every other failure here does.
        if resp.status_code == 401:
            raise CredentialsRejected(
                f"{what} failed: 401 {resp.text.strip()}", identity=self.identity
            )
        if resp.is_error:
            raise GitHubError(f"{what} failed: {resp.status_code} {resp.text}")

    def ensure_fork(self, repo: str) -> str:
        """POST the fork, then wait until it is actually readable.

        ``POST /repos/{repo}/forks`` is idempotent in the way that matters here: where the fork
        already exists GitHub returns it rather than erroring, so there is no separate "does it
        exist" round trip and no race between checking and creating. The response carries
        ``full_name``, which is read rather than assembled — a submitter who already has a
        repository of that name gets a fork named something else, and guessing would send every
        subsequent write to the wrong repository.

        **202 means accepted, not ready.** Forking is asynchronous, and GitHub's own documentation
        says to allow up to five minutes. Creating a branch in a repository that does not yet
        answer is the failure this loop exists to prevent; it is also why the first submission by
        a new contributor is slower than their second.
        """
        resp = self._client.post(f"/repos/{repo}/forks")
        self._raise_for(resp, f"ensure_fork({repo})")
        payload = resp.json()
        full_name = payload.get("full_name")
        if not full_name:
            raise GitHubError(f"ensure_fork({repo}) returned no full_name")
        # The branch to bring level is the one the submission will be cut from, which is the
        # *parent's* default — read off the response rather than assumed to be `main`, since a
        # content repository is free to call it something else.
        branch = (payload.get("parent") or {}).get("default_branch") or "main"
        # Which sync method is even possible depends on the fork network's shape, so read where
        # GitHub says this fork's network root is. See ``_sync_fork``.
        source = (payload.get("source") or {}).get("full_name")
        for _ in range(_FORK_READY_ATTEMPTS):
            probe = self._client.get(f"/repos/{full_name}")
            if probe.status_code == 200:
                self._sync_fork(full_name, repo, branch, source=source)
                return full_name
            time.sleep(_FORK_READY_DELAY_SECONDS)
        raise GitHubError(
            f"ensure_fork({repo}) created {full_name} but it did not become readable within "
            f"{_FORK_READY_ATTEMPTS * _FORK_READY_DELAY_SECONDS:.0f}s"
        )

    def get_branch_sha(self, repo: str, branch: str) -> str:
        resp = self._client.get(f"/repos/{repo}/git/ref/heads/{branch}")
        self._raise_for(resp, f"get_branch_sha({branch})")
        return resp.json()["object"]["sha"]

    def _ref_exists(self, repo: str, branch: str) -> bool:
        """Whether ``refs/heads/{branch}`` is already there. Read-only; False if it cannot be asked.

        False on error rather than True: the caller uses this to *downgrade* a refusal to
        "already exists", and guessing that way round would relabel a genuine permission failure
        as a harmless collision and send the flow off looking for a pull request that is not there.
        """
        try:
            resp = self._client.get(f"/repos/{repo}/git/ref/heads/{branch}")
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - never mask the failure being classified
            return False

    def _sync_fork(self, fork: str, parent: str, branch: str, *, source: str | None = None) -> None:
        """Bring ``fork``'s ``branch`` level with ``parent``'s, best effort.

        A fork only holds the objects its parent had **at the moment it was created**. Everything
        pushed upstream afterwards is visible through the shared network for reads, but is not the
        fork's own — and branching from such a commit is where the trouble is: a submission cut
        from the content repo's current head is asking the fork to point a ref at an object it
        does not natively hold. On 2026-08-04 that came back as a bare ``404 Not Found`` for a
        submitter whose token, scopes and ownership were all correct, and whose *first* submission
        minutes earlier had worked precisely because the fork was seconds old and still level.

        Syncing first removes the question rather than answering it: after this the base commit is
        the fork's own, and ``create_branch`` is an ordinary write. It also settles the drift
        problem issue #22 raised from the other direction — a fork left alone for a year is now
        brought level on every submission instead of accumulating distance from the base its
        branches are cut from.

        Best effort on purpose. A fork with local commits on its default branch cannot
        fast-forward, and that is the submitter's repository to keep as they like; the submission
        should proceed and fail later with something specific if it is going to fail at all.

        **Not ``merge-upstream``, deliberately** (issue #29). That endpoint syncs against the
        *network source*, not the immediate parent, which it names back in ``base_branch``: asking
        it to sync ``mmarvinm2/sandbox-wp-db`` answers ``base_branch: wikipathways:main``, because
        the deployment's target is itself a fork and a submitter's fork is therefore a fork of a
        fork. Measured on the live API: that fork is one commit behind its parent — a clean
        fast-forward — and simultaneously 24 commits *ahead* of the source, so there was never a
        sync for ``merge-upstream`` to do. It could not bring the fork level with the repository
        the branch is actually cut from, whatever it returned.

        **Neither method works everywhere, so the topology picks one.**

        ``merge-upstream`` syncs against the network *source* and says so in ``base_branch``. When
        the source **is** the content repository — a submitter forking
        ``wikipathways/wikipathways-database``, which is a network root — that is exactly right,
        and it is the only method that works, because it happens server-side inside the network.

        Updating the ref directly names the parent, but can only point it at an object the fork
        already holds. A commit merely *readable* through the shared network is refused with
        **404** — distinct from 422 ``Object does not exist``, which is what a commit from another
        network entirely returns. Measured 2026-08-04. That is why a submission works for minutes
        after a fork is created (still level, so the base commit is the fork's own) and fails
        afterwards, for any account of any age.

        So: source == content repo, use ``merge-upstream``. Otherwise the fork is a fork of a
        fork — ``merge-upstream`` would aim at a third repository the branch is never cut from —
        and the ref update is the only thing left to try, knowing it may be refused.

        ``force`` stays false: GitHub then refuses a non-fast-forward itself, which is exactly the
        "their commits are theirs to keep" rule, enforced server-side rather than guessed at here.
        """
        log = logging.getLogger("wpsubmit.github")
        if source and source == parent:
            resp = self._client.post(f"/repos/{fork}/merge-upstream", json={"branch": branch})
            if resp.is_error:
                log.info(
                    "could not sync %s@%s from upstream (%s %s); continuing",
                    fork,
                    branch,
                    resp.status_code,
                    resp.text.strip(),
                )
            return
        if source:
            log.info(
                "%s is a fork of a fork (network source %s, parent %s); merge-upstream would "
                "sync against the wrong repository, falling back to a direct ref update",
                fork,
                source,
                parent,
            )
        try:
            head = self.get_branch_sha(parent, branch)
        except Exception as exc:  # noqa: BLE001 - a best-effort sync must never fail a submission
            log.info("could not read %s@%s to sync %s (%s); continuing", parent, branch, fork, exc)
            return
        resp = self._client.patch(
            f"/repos/{fork}/git/refs/heads/{branch}", json={"sha": head, "force": False}
        )
        if resp.is_error:
            # The body, not just the status. Logging only the code is what left the 422 in issue
            # #29 unexplained for a day — the same evidence-dropping that `create_branch` below
            # was fixed for hours earlier, still present in its sibling.
            log.info(
                "could not fast-forward %s@%s to %s@%s (%s %s); continuing",
                fork,
                branch,
                parent,
                head[:7],
                resp.status_code,
                resp.text.strip(),
            )

    def _token_scopes(self) -> str:
        """What GitHub says this token actually carries, for a denial message.

        A write refused as 404 is indistinguishable from a missing repository, and the two have
        completely different answers — so the one question worth asking on the way out is what the
        token is allowed to do. Asked only on the failure path, and never allowed to raise: a
        diagnostic that can itself fail would replace the real error with its own.

        Empty means GitHub returned the header blank, which for an OAuth token means *no scopes* —
        as distinct from ``unknown``, which means the question could not be asked.
        """
        try:
            resp = self._client.get("/user")
            return resp.headers.get("x-oauth-scopes", "").strip() or "none"
        except Exception:  # noqa: BLE001 - never mask the failure being reported
            return "unknown"

    def create_branch(self, repo: str, new_branch: str, from_sha: str) -> None:
        resp = self._client.post(
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{new_branch}", "sha": from_sha},
        )
        if resp.status_code == 422:
            raise BranchAlreadyExists(f"{new_branch} already exists in {repo}")
        # GitHub answers a write the caller may not make with **404**, not 403, so as not to
        # confirm the repository exists. On this endpoint that is indistinguishable from a typo
        # unless the repository is named — which it was not, so the only report of this failure
        # reaching a human read `create_branch(update/WP5427) failed: 404` and did not say *where*.
        if resp.status_code in (403, 404):
            # GitHub is ambiguous here, so ask rather than infer. A duplicate ref answers 422 to a
            # token with `repo` and **404 to one with only `public_repo`** — the
            # don't-confirm-what-you-may-not-see pattern — which makes "already exists" and
            # "you may not write" the same status for exactly the submitters fork mode is for.
            # It cost most of an afternoon: every `update/WP<id>` branch the content repo has ever
            # had is inherited by every fork at creation, so an update from a fork collides by
            # construction, and the collision arrived looking like a permission failure.
            if self._ref_exists(repo, new_branch):
                raise BranchAlreadyExists(f"{new_branch} already exists in {repo}")
            # GitHub's own message included, not just the status. Dropping it was a mistake worth
            # naming: the first report of this failure carried the body because it came through
            # `_raise_for`, and the "better" error that replaced it kept the repository and the
            # scopes and threw away the one field that says *what GitHub objected to* — which cost
            # an hour of theorising against a 404 that could have been read directly.
            raise WriteDenied(
                f"create_branch({new_branch}) on {repo}: {resp.status_code} {resp.text.strip()} "
                f"[token scopes: {self._token_scopes()}] — the acting token cannot write there. "
                f"On a submitter's own fork this usually means their GitHub authorisation has "
                f"lapsed or was granted a narrower scope than the app asks for."
            )
        self._raise_for(resp, f"create_branch({new_branch}) on {repo}")

    def get_file_sha(self, repo: str, ref: str, path: str) -> str | None:
        resp = self._client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_file_sha({path})")
        return resp.json()["sha"]

    def get_file_content(self, repo: str, ref: str, path: str) -> bytes | None:
        # The contents API inlines base64 for blobs up to 1 MB (GPML files are ~tens of KB).
        resp = self._client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_file_content({path})")
        body = resp.json()
        if body.get("encoding") != "base64" or "content" not in body:
            return None
        return base64.b64decode(body["content"])

    def put_file(
        self,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha is not None:
            payload["sha"] = sha  # required by GitHub to update an existing file
        if author_name and author_email:
            payload["author"] = {"name": author_name, "email": author_email}
        resp = self._client.put(f"/repos/{repo}/contents/{path}", json=payload)
        self._raise_for(resp, f"put_file({path})")

    def delete_file(self, repo: str, branch: str, path: str, message: str, *, sha: str) -> None:
        resp = self._client.request(
            "DELETE",
            f"/repos/{repo}/contents/{path}",
            # DELETE with a body: the contents API takes the sha and branch that way, and httpx
            # needs the explicit request() call because .delete() has no json= parameter.
            json={"message": message, "sha": sha, "branch": branch},
        )
        self._raise_for(resp, f"delete_file({path})")

    def find_open_pr(
        self, repo: str, head_branch: str, *, head_repo: str | None = None
    ) -> PullRequest | None:
        owner = (head_repo or repo).split("/", 1)[0]
        resp = self._client.get(
            f"/repos/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{head_branch}"},
        )
        self._raise_for(resp, "find_open_pr")
        items = resp.json()
        if not items:
            return None
        data = items[0]
        return PullRequest(
            number=data["number"],
            html_url=data["html_url"],
            head_branch=head_branch,
            head_repo=_head_repo_of(data, repo),
        )

    def find_open_pr_touching(
        self, repo: str, path_prefix: str, *, limit: int = 40
    ) -> int | None:
        resp = self._client.get(
            f"/repos/{repo}/pulls",
            params={"state": "open", "per_page": min(limit, 100), "sort": "long-running",
                    "direction": "desc"},
        )
        self._raise_for(resp, "find_open_pr_touching")
        pulls = resp.json()[:limit]
        # A pull request whose head branch is one of ours is one of ours. Skipping them first
        # keeps a submitter's own in-flight edit from reading as a foreign writer, and saves a
        # file listing per skipped pull request.
        #
        # "One of ours" requires the head to be on the base repo, because that is where this app
        # pushes. A branch name is unique only within a repository, so without that condition a
        # contributor's fork branch that happens to be called `update/WP123` would be skipped as
        # ours and the lock handed out over their genuine concurrent edit — the exact divergence
        # the lock exists to prevent (issue #22). Failing the other way merely costs a file
        # listing and refuses a lock that could have been granted.
        wpid = path_prefix.rstrip("/").rsplit("/", 1)[-1]
        ours = (f"update/{wpid}", f"submit/{wpid}")
        for pull in pulls:
            head = str((pull.get("head") or {}).get("ref", ""))
            if head.startswith(ours) and _head_repo_of(pull, repo) is None:
                continue
            # One unreadable pull request must not be read as "nothing touches this pathway",
            # but neither should it abort the scan — keep looking through the rest. The except
            # therefore stays *inside* the loop: hoisting it out would truncate the scan at the
            # first bad pull request and hand out a lock over a real concurrent edit.
            try:
                files = self.list_pr_files(repo, int(pull["number"]))
            except GitHubError:
                continue
            for entry in files:
                if entry.filename.startswith(path_prefix):
                    return int(pull["number"])
        return None

    def list_pr_files(self, repo: str, pr_number: int, *, limit: int = 300) -> list[PrFile]:
        out: list[PrFile] = []
        page = 1
        while len(out) < limit:
            resp = self._client.get(
                f"/repos/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            self._raise_for(resp, "list_pr_files")
            batch = resp.json()
            if not batch:
                break
            for entry in batch:
                out.append(
                    PrFile(
                        filename=str(entry.get("filename", "")),
                        status=str(entry.get("status", "")),
                        previous_filename=entry.get("previous_filename"),
                    )
                )
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    def open_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        resp = self._client.post(
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        self._raise_for(resp, "open_pull_request")
        data = resp.json()
        # Read both off GitHub's answer rather than echoing the request. ``head`` is what was
        # *asked for*, and for a cross-repository pull request that is ``owner:branch`` — so
        # echoing it stored an owner-prefixed string in ``head_branch`` and left ``head_repo``
        # permanently None, which is what happened to the first two fork submissions this app
        # ever opened (PRs #23/#24, 2026-08-04). ``head_repo`` being None means every later
        # branch-side lookup goes to the base repo, where a fork's branch does not exist, and
        # **revise raises NoPendingSubmission** — a curator requesting changes leaves the
        # submitter unable to answer, which is the loop this app exists to provide.
        #
        # ``FakeGitHubClient`` parsed both correctly all along, so the whole suite agreed while
        # production did not. Fifth instance of that pattern here; the MockTransport test beside
        # this one is the shape that catches it.
        head_data = data.get("head") or {}
        return PullRequest(
            number=data["number"],
            html_url=data["html_url"],
            head_branch=head_data.get("ref") or head.rpartition(":")[2],
            head_repo=_head_repo_of(data, repo),
        )

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        resp = self._client.post(
            f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
        )
        self._raise_for(resp, f"create_issue_comment({issue_number})")

    def request_pr_reviewer(self, repo: str, pr_number: int, reviewer: str) -> None:
        resp = self._client.post(
            f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
            json={"reviewers": [reviewer]},
        )
        # 422 = GitHub declined (author can't review, or not a collaborator). Surface as a
        # GitHubError so the caller can swallow it without failing the app-side assignment.
        self._raise_for(resp, f"request_pr_reviewer({reviewer})")

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequestDetail | None:
        resp = self._client.get(f"/repos/{repo}/pulls/{pr_number}")
        if resp.status_code == 404:
            return None
        self._raise_for(resp, f"get_pull_request({pr_number})")
        data = resp.json()
        return PullRequestDetail(
            number=data["number"],
            html_url=data.get("html_url", ""),
            head_branch=(data.get("head") or {}).get("ref", ""),
            head_sha=(data.get("head") or {}).get("sha", ""),
            state=data.get("state", ""),
            merged=bool(data.get("merged")),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=[label["name"] for label in data.get("labels") or []],
            author=(data.get("user") or {}).get("login", ""),
            head_repo=_head_repo_of(data, repo),
        )

    def get_pull_request_state(self, repo: str, pr_number: int) -> str | None:
        detail = self.get_pull_request(repo, pr_number)
        if detail is None:
            return None
        return "merged" if detail.merged else detail.state  # "open" | "closed"

    def list_issue_comments(self, repo: str, issue_number: int) -> list[str]:
        bodies: list[str] = []
        page = 1
        while True:
            resp = self._client.get(
                f"/repos/{repo}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page},
            )
            self._raise_for(resp, f"list_issue_comments({issue_number})")
            batch = resp.json()
            bodies.extend(c.get("body") or "" for c in batch)
            if len(batch) < 100:
                return bodies
            page += 1

    def close_pull_request(self, repo: str, pr_number: int) -> None:
        resp = self._client.patch(f"/repos/{repo}/pulls/{pr_number}", json={"state": "closed"})
        self._raise_for(resp, f"close_pull_request({pr_number})")

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        # Labels live on the Issues API even for a PR, so this needs Issues:write on the App.
        resp = self._client.post(
            f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": labels}
        )
        self._raise_for(resp, f"add_labels({labels})")

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        resp = self._client.delete(f"/repos/{repo}/issues/{issue_number}/labels/{label}")
        if resp.status_code == 404:
            return  # not on the PR — the caller's intent is already satisfied
        self._raise_for(resp, f"remove_label({label})")

    def list_labels(self, repo: str, issue_number: int) -> list[str]:
        resp = self._client.get(
            f"/repos/{repo}/issues/{issue_number}/labels", params={"per_page": 100}
        )
        self._raise_for(resp, f"list_labels({issue_number})")
        return [label["name"] for label in resp.json()]

    def latest_workflow_run_for_pr(
        self, repo: str, pr_number: int, *, workflow_file: str
    ) -> WorkflowRun | None:
        detail = self.get_pull_request(repo, pr_number)
        if detail is None:
            return None
        resp = self._client.get(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params={"head_sha": detail.head_sha, "per_page": 1},
        )
        self._raise_for(resp, "list_workflow_runs")
        items = resp.json().get("workflow_runs", [])
        return _workflow_run(items[0]) if items else None

    def recent_workflow_runs(
        self, repo: str, workflow_file: str, *, limit: int = 5
    ) -> list[WorkflowRun]:
        resp = self._client.get(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs",
            params={"per_page": limit},
        )
        self._raise_for(resp, "recent_workflow_runs")
        return [_workflow_run(run) for run in resp.json().get("workflow_runs", [])]

    def merge_pull_request(self, repo: str, pr_number: int, *, method: str = "squash") -> None:
        resp = self._client.put(
            f"/repos/{repo}/pulls/{pr_number}/merge", json={"merge_method": method}
        )
        self._raise_for(resp, f"merge_pull_request({pr_number})")

    def upsert_issue_comment(
        self, repo: str, issue_number: int, body: str, *, marker: str
    ) -> None:
        # Find our existing mirror comment (first page is plenty — the PR won't have hundreds).
        resp = self._client.get(
            f"/repos/{repo}/issues/{issue_number}/comments", params={"per_page": 100}
        )
        self._raise_for(resp, f"list_comments({issue_number})")
        existing_id: int | None = None
        for comment in resp.json():
            if marker in (comment.get("body") or ""):
                existing_id = comment["id"]
                break
        if existing_id is not None:
            resp = self._client.patch(
                f"/repos/{repo}/issues/comments/{existing_id}", json={"body": body}
            )
            self._raise_for(resp, f"update_comment({existing_id})")
        else:
            resp = self._client.post(
                f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
            )
            self._raise_for(resp, f"create_comment({issue_number})")

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        members: list[str] = []
        page = 1
        while True:
            resp = self._client.get(
                f"/orgs/{org}/teams/{team_slug}/members",
                params={"per_page": 100, "page": page},
            )
            self._raise_for(resp, f"list_team_members({org}/{team_slug})")
            batch = resp.json()
            if not batch:
                break
            members.extend(m["login"] for m in batch)
            if len(batch) < 100:
                break
            page += 1
        return members

    def _latest_preview_run(self, repo: str, pr_number: int, workflow_file: str):
        """Return (status, run_id) for the newest preview run on the PR's head SHA.

        status ∈ pending|success|failed|absent; run_id is None unless the run completed OK.
        """
        if self.get_pull_request(repo, pr_number) is None:
            return "absent", None
        run = self.latest_workflow_run_for_pr(repo, pr_number, workflow_file=workflow_file)
        if run is None or run.status != "completed":
            return "pending", None
        if run.conclusion != "success":
            return "failed", None
        return "success", run.id

    def _preview_artifact_id(self, repo: str, run_id: int, artifact_name: str) -> int | None:
        resp = self._client.get(f"/repos/{repo}/actions/runs/{run_id}/artifacts")
        self._raise_for(resp, f"list_run_artifacts({run_id})")
        for art in resp.json().get("artifacts", []):
            if art.get("name") == artifact_name and not art.get("expired"):
                return art["id"]
        return None

    def pr_preview_status(
        self, repo: str, pr_number: int, *, workflow_file: str, artifact_name: str
    ) -> str:
        status, run_id = self._latest_preview_run(repo, pr_number, workflow_file)
        if status != "success":
            return {"absent": "absent", "pending": "pending"}.get(status, "failed")
        return "ready" if self._preview_artifact_id(repo, run_id, artifact_name) else "failed"

"""New-pathway submission service (design §4.1).

Orchestrates the new-pathway flow end to end:

    validate GPML → reserve WPID (atomic) → write WPID into the GPML → create a branch →
    commit the file → open a PR → record the PR number on the reservation.

Two correctness properties:

- **A failed submission burns no WPID.** If any GitHub step fails after the WPID was reserved,
  the reservation is released (id returned to the pool) before the error propagates. (Reservations
  also expire by TTL, but immediate release keeps the sequence tight.)
- **Validation happens before allocation**, so a malformed upload never consumes an id at all.
"""
from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.github import BranchAlreadyExists, GitHubClient, GitHubError, PullRequest
from app.submit.gpml import (
    PLACEHOLDER_GPML_PATH,
    PLACEHOLDER_WPID_STR,
    assign_wpid,
    assign_wpid_str,
    layout_paths,
    validate_gpml,
)
from app.submit.targets import WriteTarget, same_repo_target
from app.wpid import WpidAllocator, format_wpid

#: How many times to step past a WPID whose ``submit/WP<id>`` branch already exists.
_BRANCH_COLLISION_RETRIES = 3

#: Characters a GitHub login cannot contain, so a branch name built from one stays valid.
_UNSAFE_IN_BRANCH = re.compile(r"[^A-Za-z0-9._-]")


class SubmissionMode(enum.StrEnum):
    #: The app owns publication: allocate a WPID up front, branch ``submit/WP<id>``, file at
    #: ``pathways/WP<id>/WP<id>.gpml``, and approving merges the PR.
    DIRECT = "direct"
    #: The target repo owns publication (docs/sandbox-pipeline.md): no WPID is claimed, the
    #: branch is named for the submitter and a timestamp, and the file carries a placeholder
    #: until the repo's own workflow assigns the real id at publication.
    PIPELINE = "pipeline"


class NoPendingSubmission(RuntimeError):
    """Revise targeted a WPID with no open new-pathway submission branch/PR to commit onto."""


@dataclass(frozen=True)
class SubmissionResult:
    #: None in pipeline mode — the id does not exist until the target repo publishes.
    wpid: int | None
    wpid_str: str
    branch: str
    path: str
    pr_number: int
    pr_url: str
    #: ``owner/name`` the branch lives on, None meaning the content repo. Read back off the pull
    #: request GitHub returned rather than assumed, so the day the app opens a cross-repository
    #: pull request the review row records where the branch really is (issue #22).
    head_repo: str | None = None
    #: The commit the app made on the branch — what the review is bound to at approval.
    head_sha: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SubmissionService:
    def __init__(
        self,
        allocator: WpidAllocator | None,
        github: GitHubClient,
        *,
        repo: str,
        base_branch: str = "main",
        mode: SubmissionMode = SubmissionMode.DIRECT,
        clock: Callable[[], datetime] = _utcnow,
        target: WriteTarget | None = None,
    ) -> None:
        self._allocator = allocator
        self._repo = repo
        self._base_branch = base_branch
        self._mode = mode
        # Injected so the timestamped branch name is deterministic under test.
        self._clock = clock
        # Where the branch goes. Defaulting to "the same repository the pull request is opened
        # on" keeps every existing caller and test unchanged; fork mode is the only arrangement
        # where the two differ (issue #22, ``app.submit.targets``).
        self._target = target or same_repo_target(github, repo)
        # Taken from the target rather than the argument, so there is exactly one answer to
        # "who is writing" — passing a client and a target that disagreed would be silent.
        self._github = self._target.client

    @property
    def _branch_repo(self) -> str:
        """Where branches and file writes go — the fork in fork mode, otherwise the content repo."""
        return self._target.branch_repo

    @property
    def _base_repo(self) -> str:
        """Where a new branch's base commit is read from. See ``WriteTarget.base_repo``."""
        return self._target.base_repo

    def submit_new_pathway(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
        if self._mode is SubmissionMode.PIPELINE:
            return self._submit_pipeline(
                gpml=gpml,
                submitter=submitter,
                author_email=author_email,
                description=description,
            )
        return self._submit_direct(
            gpml=gpml, submitter=submitter, author_email=author_email, description=description
        )

    def _submit_direct(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
        if self._allocator is None:
            raise RuntimeError("direct submission needs a WPID allocator")
        # 1. Validate first — a malformed upload must not consume a WPID.
        meta = validate_gpml(gpml)

        # 2. Reserve the WPID atomically and claim its branch. A PR closed unmerged leaves its
        # branch behind, so a free id can still have a taken branch name; the floor counts those
        # branches, and this loop steps past any that slipped through (e.g. a branch created
        # between the floor read and here). Colliding reservations are held until a clean id is
        # found — releasing one first would just hand the same id back — then returned to the pool.
        collided: list[int] = []
        try:
            for attempt in range(_BRANCH_COLLISION_RETRIES):
                wpid = self._allocator.allocate(submitter)

                # 3. Stamp the assigned WPID into the GPML (Version attribute), and the
                #    submitter as Author if the upload carries none.
                gpml_out = assign_wpid(gpml, wpid, author=submitter)
                path = layout_paths(wpid)["gpml"]
                wpid_str = format_wpid(wpid)
                branch = f"submit/{wpid_str}"

                # 4. Branch off the latest base.
                try:
                    # Base and branch both on the branch repo — a ref can only point at a commit
                    # that repository holds. See ``WriteTarget.base_repo``.
                    base_sha = self._github.get_branch_sha(self._base_repo, self._base_branch)
                    self._github.create_branch(self._branch_repo, branch, base_sha)
                except BranchAlreadyExists:
                    collided.append(wpid)
                    if attempt == _BRANCH_COLLISION_RETRIES - 1:
                        raise
                    continue
                except Exception:
                    self._allocator.release(wpid)
                    raise
                break

            try:
                # 5. Commit the file, 6. open the PR.
                head_sha = self._github.put_file(
                    self._branch_repo,
                    branch,
                    path,
                    gpml_out,
                    message=f"Add {wpid_str}: {meta.name}",
                    author_name=submitter,
                    author_email=author_email,
                )
                pr: PullRequest = self._github.open_pull_request(
                    self._repo,
                    head=self._target.head(branch),
                    base=self._base_branch,
                    title=f"New pathway {wpid_str}: {meta.name}",
                    body=_pr_body(wpid_str, meta.name, meta.organism, submitter, description),
                )
            except Exception:
                # Any GitHub failure → return the WPID to the pool before re-raising.
                self._allocator.release(wpid)
                raise
        finally:
            for burnt in collided:
                self._allocator.release(burnt)

        # 7. Record the PR number on the reservation.
        self._allocator.attach_pr(wpid, pr.number)

        return SubmissionResult(
            wpid=wpid,
            wpid_str=wpid_str,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
            head_repo=pr.head_repo,
            head_sha=head_sha,
        )

    def _pipeline_branch(self, submitter: str, *, suffix: int = 1) -> str:
        """``WP0001_<submitter>_<YYYYMMDD-HHMMSS>`` — placeholder id, who, and when.

        Id-then-user matches the branches the target repo already carries; the readable stamp
        beats an epoch-ms blob when a curator is scanning a branch list. The stamp is also what
        makes the name unique, so a leftover branch from a closed PR can no longer block the
        next submission the way a WPID-derived name did.
        """
        safe = _UNSAFE_IN_BRANCH.sub("-", submitter) or "user"
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
        base = f"{PLACEHOLDER_WPID_STR}_{safe}_{stamp}"
        return base if suffix == 1 else f"{base}-{suffix}"

    def _submit_pipeline(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
    ) -> SubmissionResult:
        """Open a PR carrying the placeholder, claiming no WPID.

        There is no rollback path here and nothing to roll back: no id is reserved, so a failed
        submission cannot burn one. That is the whole reason for assigning the id at publication.
        """
        meta = validate_gpml(gpml)

        # The target repo classifies new-vs-edit from the *basename* alone, so the placeholder
        # has to be a name its "existing WPID" pattern rejects. WP0001 does that (a leading
        # zero), which is why the id and the path are not free choices — see
        # tests/test_pipeline_submission.py, which pins that repo's regex.
        gpml_out = assign_wpid_str(gpml, PLACEHOLDER_WPID_STR, author=submitter)
        path = PLACEHOLDER_GPML_PATH

        # Read the base from the **branch repo**, which in fork mode is the submitter's fork. The
        # note that used to be here said the opposite, on the reasoning that a shared object
        # database makes the upstream head resolvable from a fork. It is resolvable — `GET
        # commits/<sha>` returns it — but it is not a legal ref target there, and GitHub says so
        # with a 404 that reads exactly like a permission failure. See ``WriteTarget.base_repo``.
        base_sha = self._github.get_branch_sha(self._base_repo, self._base_branch)
        for attempt in range(1, _BRANCH_COLLISION_RETRIES + 1):
            branch = self._pipeline_branch(submitter, suffix=attempt)
            try:
                self._github.create_branch(self._branch_repo, branch, base_sha)
                break
            except BranchAlreadyExists:
                # Same person, same second. Rare, and cheap to step past.
                if attempt == _BRANCH_COLLISION_RETRIES:
                    raise

        # Every new submission writes the *same* placeholder path, so unlike the direct mode's
        # per-WPID path it cannot be assumed free. It is free on a healthy base branch — the
        # target repo publishes by moving the placeholder aside and closes the PR unmerged — but
        # a pipeline PR merged by hand leaves the placeholder sitting on `main`, and from then on
        # a create-only write 422s for everyone. Overwriting is the right resolution: the
        # placeholder is a slot, not a pathway, and the repo still reads "new" off the basename.
        existing_sha = self._github.get_file_sha(self._branch_repo, branch, path)
        head_sha = self._github.put_file(
            self._branch_repo,
            branch,
            path,
            gpml_out,
            message=f"Add {meta.name or 'new pathway'}",
            sha=existing_sha,
            author_name=submitter,
            author_email=author_email,
        )
        pr: PullRequest = self._github.open_pull_request(
            self._repo,
            head=self._target.head(branch),
            base=self._base_branch,
            title=f"[NEW] {meta.name or 'Untitled pathway'} — submitted by @{submitter}",
            body=_pipeline_pr_body(meta.name, meta.organism, submitter, description),
        )
        return SubmissionResult(
            wpid=None,
            wpid_str=PLACEHOLDER_WPID_STR,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
            head_repo=pr.head_repo,
            head_sha=head_sha,
        )

    def revise_new_pathway(
        self,
        *,
        gpml: bytes | str,
        submitter: str,
        wpid: int | None = None,
        branch: str | None = None,
        head_repo: str | None = None,
        author_email: str | None = None,
    ) -> SubmissionResult:
        """Commit a revised GPML onto an **already-open** new-pathway PR (design: the revise loop).

        Unlike ``submit_new_pathway`` this reserves no WPID and opens no PR — it adds a commit to
        the existing branch, reusing the open PR. Used when a curator requested changes on a new
        submission (which, unlike an update, isn't on ``main`` yet). Raises
        ``NoPendingSubmission`` if there is no open submission branch/PR.

        ``branch`` is required in pipeline mode and optional in direct mode: a pipeline branch
        carries a timestamp, so it cannot be derived from anything and has to be read off the
        review row. Passing it in either mode is fine and is what the routes do.

        ``head_repo`` (``owner/name``) is the repository that branch lives on, None meaning the
        content repository — which is every submission the app opens today, since it pushes the
        branch there itself. Every branch-side read and write below is scoped to it rather than
        to the base repo, so a pull request from a contributor's fork revises onto the fork where
        its branch actually is (issue #22). The base repo is still what gets *queried* for the
        pull request; only the head moves.
        """
        if branch is None:
            if wpid is None:
                raise NoPendingSubmission("a revise needs either a branch or a WPID")
            branch = f"submit/{format_wpid(wpid)}"

        if self._mode is SubmissionMode.PIPELINE:
            wpid_str, path = PLACEHOLDER_WPID_STR, PLACEHOLDER_GPML_PATH
        else:
            if wpid is None:
                raise NoPendingSubmission("a direct-mode revise needs the assigned WPID")
            wpid_str = format_wpid(wpid)
            path = layout_paths(wpid)["gpml"]

        validate_gpml(gpml)

        branch_repo = head_repo or self._repo
        try:
            self._github.get_branch_sha(branch_repo, branch)
        except GitHubError as exc:
            raise NoPendingSubmission(
                f"no open submission branch for {wpid_str} to revise"
            ) from exc
        pr = self._github.find_open_pr(self._repo, branch, head_repo=head_repo)
        if pr is None:
            raise NoPendingSubmission(f"no open submission PR for {wpid_str} to revise")

        # Keep whatever id the submission already carries; a revise can't renumber.
        gpml_out = assign_wpid_str(gpml, wpid_str, author=submitter)
        branch_file_sha = self._github.get_file_sha(branch_repo, branch, path)
        head_sha = self._github.put_file(
            branch_repo,
            branch,
            path,
            gpml_out,
            message=f"Revise {wpid_str}",
            sha=branch_file_sha,
            author_name=submitter,
            author_email=author_email,
        )
        return SubmissionResult(
            wpid=wpid,
            wpid_str=wpid_str,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
            head_repo=pr.head_repo,
            head_sha=head_sha,
        )


def _pr_body(
    wpid_str: str,
    name: str | None,
    organism: str | None,
    submitter: str,
    description: str | None = None,
) -> str:
    body = (
        f"@{submitter} submitted this pathway through the WikiPathways curation portal.\n\n"
        f"- **Pathway:** {name or '(unnamed)'}\n"
        f"- **WPID:** {wpid_str}, assigned by the app\n"
        f"- **Organism:** {organism or '(unset)'}\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**Note from the submitter**\n\n{note}\n"
    body += "\nThe drawn pathway is in the curation dashboard; this pull request holds the GPML."
    return body


def _pipeline_pr_body(
    name: str | None,
    organism: str | None,
    submitter: str,
    description: str | None = None,
) -> str:
    """Short on purpose: the target repo's own workflow overwrites this description twice.

    It is worth writing anyway — it is what a person sees in the minute before that workflow
    starts, and it is all there is if the workflow never runs. The durable record is the mirror
    comment, which no workflow touches.
    """
    body = (
        f"@{submitter} submitted this pathway through the WikiPathways curation portal.\n\n"
        f"- **Pathway:** {name or '(unnamed)'}\n"
        f"- **Organism:** {organism or '(unset)'}\n"
        f"- **WPID:** not yet assigned — a curator's approval is what assigns it\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**Note from the submitter**\n\n{note}\n"
    body += (
        "\nThis description is regenerated by the repository's own pipeline. "
        "The curation record lives in the pinned comment below.\n"
    )
    return body

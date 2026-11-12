"""Update-an-existing-pathway flow.

    check out (lock) → validate revision → branch off *latest* main → commit → open PR

Three design guarantees converge here:

- **One editor at a time (§4.3).** Acquiring the pathway lock is step one; if someone else holds
  it, or an open GitHub PR already touches the pathway (the injected ``open_pr_scanner`` on the
  registry), the update is refused. This is what makes the #90 unmergeable-conflict impossible.
- **Branch off the latest main (§5).** The revision branch is always cut from the current base
  head, so a merged PR never leaves this one rebasing across a GPML (XML) conflict.
- **The lock is held until the PR resolves.** On success the PR number is attached to the lock;
  the lock is released by webhook on PR close/merge, or by TTL. On *failure* the lock is released
  immediately so a broken attempt never freezes the pathway.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.github import BranchAlreadyExists, GitHubClient, PullRequest
from app.locks import PathwayLockRegistry
from app.submit.gpml import assign_wpid, layout_paths, validate_gpml
from app.submit.targets import WriteTarget, same_repo_target
from app.wpid import format_wpid

#: How many leftover ``update/WP<id>-<n>`` branches to step past before giving up.
_STALE_BRANCH_RETRIES = 20


class PathwayNotFound(RuntimeError):
    """Update targeted a WPID that does not exist in the repo (use submit for new pathways)."""


@dataclass(frozen=True)
class UpdateResult:
    wpid: int
    wpid_str: str
    branch: str
    path: str
    pr_number: int
    pr_url: str
    #: ``owner/name`` the branch lives on, None meaning the content repo — see
    #: ``SubmissionResult.head_repo``.
    head_repo: str | None = None
    #: The commit the app made on the branch — see ``SubmissionResult.head_sha``.
    head_sha: str | None = None
    #: The GPML's blob sha on the base branch when this edit was made — the version it replaces.
    base_blob: str | None = None


class UpdateService:
    def __init__(
        self,
        locks: PathwayLockRegistry,
        github: GitHubClient,
        *,
        repo: str,
        base_branch: str = "main",
        target: WriteTarget | None = None,
    ) -> None:
        self._locks = locks
        self._repo = repo
        self._base_branch = base_branch
        # See ``app.submit.targets``. In every mode but fork the branch repo *is* the content
        # repo, so the two names below are the same string and nothing changes.
        self._target = target or same_repo_target(github, repo)
        self._github = self._target.client

    @property
    def _branch_repo(self) -> str:
        return self._target.branch_repo

    @property
    def _base_repo(self) -> str:
        """Where a new branch's base commit is read from. See ``WriteTarget.base_repo``."""
        return self._target.base_repo

    @property
    def _head_repo(self) -> str | None:
        """What ``find_open_pr`` must be told, so a fork branch is not searched for on the base."""
        return self._target.head_repo

    def _fresh_branch(self, wpid_str: str, base_sha: str) -> str:
        """A new ``update/WP<id>-<n>`` cut from the current base, stepping past leftovers.

        The suffix is a counter rather than a timestamp so the branch list stays readable: the
        second edit of WP554 is ``update/WP554-2``, and a curator can see at a glance which
        pathway it belongs to.
        """
        for suffix in range(2, 2 + _STALE_BRANCH_RETRIES):
            candidate = f"update/{wpid_str}-{suffix}"
            try:
                self._github.create_branch(self._branch_repo, candidate, base_sha)
                return candidate
            except BranchAlreadyExists:
                # Same rule as for the unsuffixed branch: if this one still has an open pull
                # request, that is the pull request this edit belongs on. Stepping past it would
                # open a second concurrent PR for one pathway — the divergence the check-out
                # lock exists to prevent, and which the lock cannot catch because the same
                # person holds it.
                if self._github.find_open_pr(
                    self._repo, candidate, head_repo=self._head_repo
                ) is not None:
                    return candidate
                continue
        raise BranchAlreadyExists(
            f"could not cut a fresh update branch for {wpid_str}: "
            f"update/{wpid_str}-2..{1 + _STALE_BRANCH_RETRIES} all exist"
        )

    def update_pathway(
        self,
        *,
        wpid: int,
        gpml: bytes | str,
        submitter: str,
        author_email: str | None = None,
        description: str | None = None,
        own_pr: int | None = None,
    ) -> UpdateResult:
        # Validate before taking the lock — a malformed revision shouldn't check out the pathway.
        validate_gpml(gpml)

        # Check out the pathway (may raise LockUnavailable → surfaced as 409 upstream).
        # ``own_pr`` is this submitter's open review on the pathway, if any: see acquire().
        self._locks.acquire(wpid, submitter, own_pr=own_pr)

        try:
            wpid_str = format_wpid(wpid)
            path = layout_paths(wpid)["gpml"]

            # The pathway must already exist; otherwise this is a submission, not an update. The
            # blob is kept: it is the version this edit replaces, and approval checks it is still
            # the one on the base branch (``CurationService`` / ``BaseMoved``).
            base_blob = self._github.get_file_sha(self._repo, self._base_branch, path)
            if base_blob is None:
                raise PathwayNotFound(
                    f"{wpid_str} does not exist on {self._base_branch}; use submit for new pathways"
                )

            # Force the WPID; an update can't renumber. Author only fills in when absent, so an
            # existing pathway keeps its original authorship.
            gpml_out = assign_wpid(gpml, wpid, author=submitter)
            branch = f"update/{wpid_str}"
            # From the branch repo — the fork in fork mode — because a ref can only point at a
            # commit that repository holds. See ``WriteTarget.base_repo``.
            base_sha = self._github.get_branch_sha(self._base_repo, self._base_branch)
            try:
                self._github.create_branch(self._branch_repo, branch, base_sha)
            except BranchAlreadyExists:
                # Re-uploading while still checked out: reuse the existing update branch/PR
                # instead of opening a second PR for the same pathway.
                #
                # Only while its pull request is still open, though. Nothing here or upstream
                # ever deletes a branch, and where the repository publishes through its own
                # Actions the pull request is closed rather than merged, so `update/WP<id>`
                # outlives every edit. Reusing it for the *next* edit would cut the diff against
                # a base that could be months old — the opposite of the branch-off-latest
                # guarantee this flow exists to provide, and of what the PR body claims.
                if self._github.find_open_pr(
                    self._repo, branch, head_repo=self._head_repo
                ) is None:
                    branch = self._fresh_branch(wpid_str, base_sha)

            # SHA of the file as it currently is on the update branch (falls back to base).
            branch_file_sha = self._github.get_file_sha(self._branch_repo, branch, path)
            head_sha = self._github.put_file(
                self._branch_repo,
                branch,
                path,
                gpml_out,
                message=f"Update {wpid_str}",
                sha=branch_file_sha,
                author_name=submitter,
                author_email=author_email,
            )
            pr: PullRequest | None = self._github.find_open_pr(
                self._repo, branch, head_repo=self._head_repo
            )
            if pr is None:
                pr = self._github.open_pull_request(
                    self._repo,
                    head=self._target.head(branch),
                    base=self._base_branch,
                    title=f"Update {wpid_str}",
                    body=_pr_body(wpid_str, submitter, description),
                )
        except Exception:
            # Any failure → free the pathway so it isn't stuck checked out.
            self._locks.release(wpid, submitter, force=True)
            raise

        # Keep the lock held until the PR resolves; record the PR number on it.
        self._locks.acquire(wpid, submitter, pr_number=pr.number)

        return UpdateResult(
            wpid=wpid,
            wpid_str=wpid_str,
            branch=branch,
            path=path,
            pr_number=pr.number,
            pr_url=pr.html_url,
            head_repo=pr.head_repo,
            head_sha=head_sha,
            base_blob=base_blob,
        )


def _pr_body(wpid_str: str, submitter: str, description: str | None = None) -> str:
    body = (
        f"@{submitter} edited **{wpid_str}** through the WikiPathways curation portal.\n\n"
        f"The branch was cut from the latest base, so this never rebases across a GPML "
        f"conflict.\n"
    )
    note = (description or "").strip()
    if note:
        body += f"\n**What changed**\n\n{note}\n"
    body += (
        "\nThe before and after drawings are in the curation dashboard; this pull request holds "
        "the GPML."
    )
    return body

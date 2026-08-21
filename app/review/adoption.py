"""Build a review around a pull request the portal did not open.

The portal's own submissions arrive as an upload: the app holds the bytes, so it can render,
grade and file them in one request. A pull request from the PathVisio plugin — or from anyone
with push access — arrives as a *number*. This service is the adapter between the two, and it is
deliberately the only new write path: everything after the fetch is the same
``PreviewService.render_local`` / ``build_checklist`` / ``CurationService.register`` the upload
route already uses, in the same order.

**Every read is addressed to the content repository, never to the submitter's fork.** GitHub
serves a fork's pull request from the base repository at the head commit — verified against the
live API on 2026-08-21, identical blob sha from either side — so one repository answers for
everything. That keeps working when the fork is deleted, and it means adoption needs no access
the app does not already have. (The bot *can* read a public fork; this is about not needing to.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.github import GitHubClient
from app.review.adopt import Adoption, Skipped, derive

logger = logging.getLogger("wpsubmit.adopt")


@dataclass(frozen=True)
class AdoptionOutcome:
    pr_number: int
    adopted: bool
    #: Why not, when ``adopted`` is False — always populated, so the log line can say. A pull
    #: request that quietly fails to appear in the queue is indistinguishable from one nobody
    #: opened, which is the failure mode this project keeps rediscovering.
    reason: str | None = None
    #: True when this call refreshed an existing adopted review rather than creating one.
    refreshed: bool = False


class AdoptionService:
    def __init__(
        self,
        *,
        github: GitHubClient,
        previews,
        curation,
        locks=None,
        content_repo: str,
        default_branch: str,
    ) -> None:
        self._github = github
        self._previews = previews
        self._curation = curation
        self._locks = locks
        self._repo = content_repo
        self._default_branch = default_branch

    def adopt(self, pr_number: int, *, announce: bool = True) -> AdoptionOutcome:
        """Adopt (or refresh) one pull request. Never raises for an ordinary refusal."""
        existing = self._existing(pr_number)
        if existing is not None and existing.origin == "portal":
            return self._skip(pr_number, "opened through the portal")

        try:
            detail = self._github.get_pull_request(self._repo, pr_number)
        except Exception as exc:  # noqa: BLE001 — a read failure must be reported, not raised
            return self._skip(pr_number, f"could not be read from GitHub: {exc}")
        if detail is None:
            return self._skip(pr_number, "does not exist")
        if detail.state != "open":
            # A closed pull request has nothing for a curator to do, and adopting one would put a
            # dead row in the queue. Note this is only about *fresh* adoption: a review already
            # adopted still settles through handle_pr_closed like any other.
            return self._skip(pr_number, f"is {detail.state}")

        if existing is not None and existing.head_sha == detail.head_sha:
            # `synchronize` also fires when the base branch moves under a pull request. Without
            # this guard a busy `main` re-renders every open review and re-posts every mirror
            # comment, on every push.
            return self._skip(pr_number, "head commit is unchanged")

        try:
            files = self._github.list_pr_files(self._repo, pr_number)
        except Exception as exc:  # noqa: BLE001
            return self._skip(pr_number, f"file list could not be read: {exc}")

        plan = derive(detail, files)
        if isinstance(plan, Skipped):
            return self._skip(pr_number, plan.reason)

        after = self._read(plan.head_sha, plan.primary_path)
        if after is None:
            return self._skip(
                pr_number, f"{plan.primary_path} could not be read at {plan.head_sha[:7]}"
            )
        before = (
            self._read(self._default_branch, plan.primary_path)
            if plan.kind == "update"
            else None
        )

        return self._record(plan, after=after, before=before, announce=announce, refresh=existing)

    # -- internals -------------------------------------------------------------------------

    def _existing(self, pr_number: int):
        try:
            return self._curation.get(pr_number)
        except Exception:  # noqa: BLE001 — ReviewNotFound, whose type lives in the service
            return None

    def _skip(self, pr_number: int, reason: str) -> AdoptionOutcome:
        logger.info("not adopting PR #%s: %s", pr_number, reason)
        return AdoptionOutcome(pr_number=pr_number, adopted=False, reason=reason)

    def _read(self, ref: str, path: str) -> bytes | None:
        try:
            return self._github.get_file_content(self._repo, ref, path)
        except Exception:  # noqa: BLE001
            logger.warning("could not read %s at %s from %s", path, ref, self._repo, exc_info=True)
            return None

    def _record(
        self,
        plan: Adoption,
        *,
        after: bytes,
        before: bytes | None,
        announce: bool,
        refresh,
    ) -> AdoptionOutcome:
        from app.preview.metadata import parse_curation_metadata

        after_meta = parse_curation_metadata(after)
        before_meta = parse_curation_metadata(before) if before else None

        # Render *before* register, and not as a matter of taste: the mirror comment reads the
        # quality report out of this cache, so registering first posts a comment with the
        # automated-checks table missing.
        try:
            self._previews.render_local(
                plan.pr_number,
                plan.wpid or 0,
                after_gpml=after,
                before_gpml=before,
            )
        except Exception:  # noqa: BLE001 — a render failure costs the picture, not the review
            logger.warning("preview render failed for PR #%s", plan.pr_number, exc_info=True)

        review = self._curation.register(
            pr_number=plan.pr_number,
            wpid=plan.wpid,
            submitter=plan.submitter,
            kind=plan.kind,
            metadata=after_meta,
            before_metadata=before_meta,
            head_branch=plan.head_branch,
            head_repo=plan.head_repo,
            origin="adopted",
            head_sha=plan.head_sha,
            pathway_paths=plan.paths,
            base_repo=self._repo,
            announce=announce,
        )
        self._take_lock(plan)
        if plan.note:
            logger.info("PR #%s: %s", plan.pr_number, plan.note)
        logger.info(
            "adopted PR #%s as a %s of %s by @%s (%s)",
            plan.pr_number,
            plan.kind,
            review.wpid_str,
            plan.submitter,
            plan.primary_path,
        )
        return AdoptionOutcome(
            pr_number=plan.pr_number, adopted=True, refreshed=refresh is not None
        )

    def _take_lock(self, plan: Adoption) -> None:
        """Record the check-out, best-effort and never stealing."""
        if self._locks is None or plan.wpid is None:
            return
        held = self._locks.adopt(plan.wpid, plan.submitter, pr_number=plan.pr_number)
        if held is None:
            logger.info(
                "PR #%s edits WP%s, which is already checked out by someone else — adopting the "
                "review without the lock",
                plan.pr_number,
                plan.wpid,
            )

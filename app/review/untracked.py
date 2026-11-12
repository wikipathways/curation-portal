"""Open pull requests on the content repository that the portal has no review for.

The portal's queue only ever showed rows it had written, so a pull request opened on GitHub — the
PathVisio plugin, a curator with push access, a contributor working in git — was invisible to it
however long it waited. On 2026-11-11 the queue was empty while nine open sandbox pull requests
touched ``pathways/``, one of them 36 days old.

This is the read side only. It lists, classifies with the same ``derive`` adoption uses, and says
why a pull request could or could not be adopted. Adopting stays a curator's explicit click: the
automatic flag is off on purpose, because adoption comments on the pull request.

Cost: one listing call, plus one file listing per untracked pull request whose head commit has not
been seen before. File lists are cached by ``(number, head_sha)``, so a quiet repository costs one
call per scan.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.github import GitHubClient, PrFile
from app.review.adopt import Adoption, derive

logger = logging.getLogger("wpsubmit.untracked")


@dataclass(frozen=True)
class UntrackedPr:
    number: int
    title: str
    author: str
    html_url: str
    created_at: str
    draft: bool
    #: What the pull request is, in words: "new pathway", "update of WP554", "4 pathways".
    summary: str
    #: Whether ``POST /api/reviews/{n}/adopt`` would build a review from it.
    adoptable: bool
    #: Why not, or a caveat worth reading first (several pathways in one pull request).
    reason: str | None = None


def _summarise(plan: Adoption) -> tuple[str, str | None]:
    if plan.multi_pathway:
        return (
            f"{len(plan.paths)} pathways",
            "Changes several pathways. It can be adopted and reviewed, but approval is blocked "
            "because the repository publishes one pathway per pull request.",
        )
    if plan.kind == "update":
        return f"update of WP{plan.wpid}", plan.note
    return f"new pathway ({plan.primary_path.rsplit('/', 1)[-1]})", plan.note


def scan_untracked(
    github: GitHubClient,
    repo: str,
    tracked: set[int],
    file_cache: dict[tuple[int, str], list[PrFile]],
) -> list[UntrackedPr]:
    """Every open pull request touching ``pathways/`` that has no review row, newest first.

    ``file_cache`` is read and pruned in place: entries for pull requests no longer open, or whose
    head has moved, are dropped. Raises what the listing raises; a single unreadable file list
    only drops that pull request from this scan.
    """
    pulls = github.list_open_pull_requests(repo)
    live_keys = {(p.number, p.head_sha) for p in pulls}
    for key in list(file_cache):
        if key not in live_keys:
            del file_cache[key]

    out: list[UntrackedPr] = []
    for detail in pulls:
        if detail.number in tracked:
            continue
        key = (detail.number, detail.head_sha)
        files = file_cache.get(key)
        if files is None:
            try:
                files = github.list_pr_files(repo, detail.number)
            except Exception:  # noqa: BLE001 — one unreadable pull request is not a failed scan
                logger.warning("could not list files of PR #%s", detail.number, exc_info=True)
                continue
            file_cache[key] = files
        if not any(f.filename.startswith("pathways/") for f in files):
            continue
        plan = derive(detail, files)
        if isinstance(plan, Adoption):
            summary, reason = _summarise(plan)
            adoptable = True
        else:
            summary, reason, adoptable = "no pathway to review", plan.reason, False
        out.append(
            UntrackedPr(
                number=detail.number,
                title=detail.title,
                author=detail.author,
                html_url=detail.html_url,
                created_at=detail.created_at,
                draft=detail.draft,
                summary=summary,
                adoptable=adoptable,
                reason=reason,
            )
        )
    return out

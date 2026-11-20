"""Re-render cached previews drawn by an older renderer.

A renderer upgrade changes nothing a curator sees until each review is drawn again, and a live
review is otherwise only redrawn when its pull request gets a new commit. This walks the live
reviews from the background tick and brings their cache up to ``RENDERER_VERSION`` (and the
comparison up to ``DIFF_VERSION``). Published and merged reviews keep their render for a while,
so they are brought up to date too, from the GPML on disk only.

Two sources, cheapest first. A cache written since renderer 2 keeps the GPML it was drawn from,
so it is redrawn from disk with no network at all. An older cache has only pictures, so its GPML
is fetched again: the "after" at the review's head commit, the "before" from the blob the update
was compared against (``Review.base_blob``) — not today's default branch, which may have moved on
and would turn the diff into a comparison nobody submitted. Only the fetching kind counts
against ``budget``, so one tick never spends more than a handful of GitHub calls on this.

Never raises and never posts anything: a preview is cosmetic, and the mirror comment is the
review's business, not the cache's.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("wpsubmit.preview")

#: How long a review whose GPML could not be fetched is left alone before the next attempt. The
#: usual cause (a deleted branch, a file moved) does not fix itself within a tick.
_RETRY_AFTER_SECONDS = 3600.0

#: ``Review.base_blob`` for an update whose pathway was not on the default branch.
_ABSENT_BLOB = "absent"


def _expected_path(review) -> str | None:
    """The GPML a review is about: its recorded path, else where its WPID puts it."""
    if review.pathway_paths:
        return review.pathway_paths[0]
    if review.wpid is not None:
        return f"pathways/WP{review.wpid}/WP{review.wpid}.gpml"
    from app.submit.gpml import PLACEHOLDER_GPML_PATH

    return PLACEHOLDER_GPML_PATH


def _live_reviews(session_factory) -> list:
    from sqlalchemy import select

    from app.models import Review
    from app.review.service import CurationService

    with session_factory() as s:
        rows = s.scalars(
            select(Review)
            .where(Review.status.notin_(CurationService._TERMINAL))
            .order_by(Review.pr_number.desc())
        ).all()
        s.expunge_all()
    return list(rows)


def _kept_reviews(session_factory) -> list:
    """Finished reviews whose render is kept (``CurationService._KEEPS_RENDER``), newest first.

    Their card still shows the change summary, so a comparison that learned something new
    (``DIFF_VERSION``) should reach them too. They are only ever redrawn from the GPML kept on
    disk: a finished review is not worth a GitHub call.
    """
    from sqlalchemy import select

    from app.models import Review
    from app.review.service import CurationService

    with session_factory() as s:
        rows = s.scalars(
            select(Review)
            .where(Review.status.in_(CurationService._KEEPS_RENDER))
            .order_by(Review.pr_number.desc())
        ).all()
        s.expunge_all()
    return list(rows)


#: How many finished reviews one tick redraws from disk. Each is a local render (milliseconds),
#: but a production history can hold many, and they can wait for the next tick.
_KEPT_BUDGET = 10


def refresh_stale_renders(
    preview, session_factory, github, *, content_repo: str, default_branch: str, budget: int = 5
) -> int:
    """Re-render up to ``budget`` live reviews whose render is stale; return how many were.

    The count covers every review redrawn; ``budget`` limits only those that needed GitHub.
    Finished reviews that keep their render are redrawn too, from disk only and at most
    ``_KEPT_BUDGET`` a tick.
    """
    try:
        reviews = _live_reviews(session_factory)
        live = {r.pr_number for r in reviews}
        kept = [r for r in _kept_reviews(session_factory) if r.pr_number not in live]
    except Exception:  # noqa: BLE001 — a cosmetic pass must never take the tick down
        logger.warning("render backfill could not list reviews", exc_info=True)
        return 0

    backoff: dict[int, float] = preview.__dict__.setdefault("_backfill_backoff", {})
    now = time.monotonic()
    done = 0
    fetched = 0
    for review in reviews:
        pr = review.pr_number
        try:
            if not preview.needs_rerender(pr):
                continue
            # Before either source: a redraw from disk that fails (or leaves no current stamp)
            # would otherwise be tried again, and logged again, on every tick.
            if backoff.get(pr, 0.0) > now:
                continue
            if preview.source_path(pr, "after") is not None:
                if (
                    preview.rerender_from_source(pr) is not None
                    and not preview.needs_rerender(pr)
                ):
                    done += 1
                    backoff.pop(pr, None)
                else:
                    backoff[pr] = now + _RETRY_AFTER_SECONDS
                continue
            if github is None or fetched >= budget or not review.head_sha:
                continue
            fetched += 1
            if _rerender_from_github(preview, github, review, content_repo, default_branch):
                done += 1
                backoff.pop(pr, None)
            else:
                backoff[pr] = now + _RETRY_AFTER_SECONDS
        except Exception:  # noqa: BLE001 — one bad review must not stop the others
            backoff[pr] = now + _RETRY_AFTER_SECONDS
            logger.warning("render backfill failed for PR #%s", pr, exc_info=True)
    redrawn_kept = 0
    for review in kept:
        if redrawn_kept >= _KEPT_BUDGET:
            break
        pr = review.pr_number
        try:
            if backoff.get(pr, 0.0) > now or not preview.needs_rerender(pr):
                continue
            if preview.source_path(pr, "after") is None:
                continue  # drawn before sources were kept; not worth fetching for a closed review
            redrawn_kept += 1
            if preview.rerender_from_source(pr) is not None and not preview.needs_rerender(pr):
                done += 1
                backoff.pop(pr, None)
            else:
                backoff[pr] = now + _RETRY_AFTER_SECONDS
        except Exception:  # noqa: BLE001
            backoff[pr] = now + _RETRY_AFTER_SECONDS
            logger.warning("render backfill failed for finished PR #%s", pr, exc_info=True)
    return done


def _rerender_from_github(preview, github, review, content_repo: str, default_branch: str) -> bool:
    path = _expected_path(review)
    if not path:
        return False
    after = github.get_file_content(content_repo, review.head_sha, path)
    if after is None:
        logger.info("render backfill: %s not found at %s for PR #%s",
                    path, review.head_sha[:7], review.pr_number)
        return False
    before = None
    if review.kind == "update":
        if review.base_blob and review.base_blob != _ABSENT_BLOB:
            before = github.get_blob(content_repo, review.base_blob)
        elif not review.base_blob:
            # Rows from before the stale-base guard recorded no blob; the default branch is the
            # best remaining guess at what the update was made against.
            before = github.get_file_content(content_repo, default_branch, path)
    stamp = preview.stamp(review.pr_number) or {}
    note = stamp.get("submitter_note")
    if note is None:
        note = (preview.metadata(review.pr_number) or {}).get("submitter_note")
    if note is None:
        note = review.submitter_note
    preview.render_local(
        review.pr_number,
        review.wpid or 0,
        after_gpml=after,
        before_gpml=before,
        submitter_note=note,
        origin=review.origin or "portal",
        head_sha=review.head_sha,
    )
    return True

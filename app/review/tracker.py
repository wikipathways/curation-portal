"""Where an approved pathway is on its way to the site: the publish tracker on the review card.

After a curator approves, four things happen that the portal does not do itself, and until now
the card said "approved, waiting on the repository" for all of them. A curator could not tell a
publication that was running from one that never started, or a published pathway from a page
that was still being built.

The stages, each read from what the portal already records:

1. **Accepted**: the curator's approval (``approved_at``); the ``accepted`` label went on.
2. **Publish workflow**: the repository's publish run (``pipeline_run["publish"]``), counted only
   if it started after the approval, since the run carries no pull-request reference.
3. **Published**: the repository's marker comment named a WPID (status PUBLISHED).
4. **Site live**: the published page answered (``pipeline_run["live_at"]``, from the background
   loop's probe).

Pure: no database, no network, so the template and the tests call it the same way.
"""
from __future__ import annotations

from datetime import UTC, datetime

#: Statuses the tracker is shown for. Anything else has not been approved or was never going to
#: publish (a rejection, a close).
TRACKED = frozenset({"approved", "published", "publish_failed"})

_RUNNING = frozenset({"requested", "queued", "in_progress", "waiting", "pending"})


def _when(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _status_value(review) -> str:
    status = getattr(review, "status", None)
    return getattr(status, "value", status) or ""


def publish_stages(review, pipeline_run: dict | None, now: datetime | None = None) -> list[dict]:
    """The four stages as ``{key, label, state, detail, at}``, or [] when not tracked.

    ``state`` is one of ``done``, ``active`` (happening now), ``waiting`` (not started),
    ``failed``, ``skipped`` (will not happen, because an earlier stage failed) and ``unknown``.
    """
    status = _status_value(review)
    approved_at = _when(getattr(review, "approved_at", None))
    if status not in TRACKED or approved_at is None:
        return []
    now = now or datetime.now(UTC)
    run = pipeline_run or {}
    publish = run.get("publish") or {}
    # A run that started before this approval belongs to another pathway, whatever it says.
    run_at = _when(publish.get("created_at"))
    ours = bool(publish) and (run_at is None or run_at >= approved_at.replace(microsecond=0))
    published_at = _when(getattr(review, "published_at", None))
    live_at = _when(run.get("live_at"))
    wpid = getattr(review, "wpid", None)
    is_update = getattr(review, "kind", None) == "update" and wpid is not None

    stages = [
        {
            "key": "accepted",
            "label": "Accepted",
            "state": "done",
            "detail": f"Approved by @{review.approved_by}" if getattr(review, "approved_by", None)
            else "Approved",
            "at": approved_at,
        }
    ]

    # 2. the repository's publish workflow
    if status == "published":
        workflow = {"state": "done", "detail": "The repository's publish workflow finished."}
    elif ours and publish.get("status") in _RUNNING:
        workflow = {"state": "active", "detail": "The repository's publish workflow is running."}
    elif ours and publish.get("conclusion") not in (None, "success"):
        workflow = {
            "state": "failed",
            "detail": f"The publish workflow finished as {publish.get('conclusion')}.",
        }
    elif ours and publish.get("conclusion") == "success":
        workflow = {
            "state": "done",
            "detail": "The publish workflow finished; waiting for its announcement.",
        }
    elif status == "publish_failed":
        workflow = {"state": "failed", "detail": "No publication was announced."}
    else:
        minutes = int((now - approved_at).total_seconds() // 60)
        workflow = {
            "state": "waiting" if minutes < 5 else "unknown",
            "detail": (
                "Waiting for the repository to start its publish workflow."
                if minutes < 5
                # No count of minutes: it would change the card every minute (and its ETag), and
                # the Accepted stage already says how long ago the approval was.
                else "No publish run has been seen since the approval."
            ),
        }
    stages.append(
        {
            "key": "workflow",
            "label": "Publish workflow",
            "url": publish.get("url") if ours else None,
            "at": run_at if ours else None,
            **workflow,
        }
    )

    # 3. the announcement
    if status == "published":
        published = {
            "state": "done",
            "detail": f"Published as WP{wpid}." if wpid else "Published.",
            "at": published_at,
        }
    elif status == "publish_failed":
        published = {
            "state": "failed",
            "detail": (
                "The repository did not announce the publication."
                if is_update
                else "The repository did not announce a WPID."
            ),
        }
    else:
        published = {
            "state": "waiting",
            # An update has its WPID already; only a new pathway gets one at publication.
            "detail": (
                f"It goes to WP{wpid} when it publishes."
                if is_update
                else "The WPID is assigned when it publishes."
            ),
        }
    stages.append({"key": "published", "label": "Published", "at": None, **published})

    # 4. the site
    if live_at is not None:
        site = {"state": "done", "detail": "The page is up.", "at": live_at}
    elif status == "published":
        waited = int((now - published_at).total_seconds() // 60) if published_at else 0
        site = {
            "state": "active" if waited < 30 else "unknown",
            "detail": (
                "The site is rebuilding. That takes a few minutes, and the CDN up to ten more."
                if waited < 30
                else "The page was not seen to update; open it to check."
            ),
        }
    elif status == "publish_failed":
        # Nothing was published, so nothing is coming; "Not yet" would say otherwise, and a
        # waiting stage would keep the card polling every few seconds for half an hour.
        site = {"state": "skipped", "detail": "Nothing was published, so the site has not changed."}
    else:
        site = {"state": "waiting", "detail": "After publication the site rebuilds."}
    stages.append({"key": "live", "label": "Site live", "at": None, **site})
    return stages


def publish_pending(review, pipeline_run: dict | None, now: datetime | None = None) -> bool:
    """True while something in the tracker is still expected to move soon. Drives the polling.

    Bounded to half an hour after the last step, so a publication that stalled does not keep a
    page polling every few seconds for the rest of the day.
    """
    now = now or datetime.now(UTC)
    stages = publish_stages(review, pipeline_run, now)
    if not any(s["state"] in ("active", "waiting") for s in stages):
        return False
    last = max((s["at"] for s in stages if s.get("at") is not None), default=None)
    return last is None or (now - last).total_seconds() < PENDING_WINDOW_SECONDS


#: How long after the latest stage the tracker counts as still moving.
PENDING_WINDOW_SECONDS = 30 * 60


#: How long after the publish workflow finished the portal waits for its announcement before
#: offering the hand-recording form. The marker comment arrives with the end of the run; ten
#: minutes past that, the automatic path has visibly not delivered.
WPID_FALLBACK_AFTER_SECONDS = 10 * 60


def wpid_fallback_due(review, pipeline_run: dict | None, now: datetime | None = None) -> bool:
    """Whether to offer the form that records a published WPID by hand.

    Not in the normal flow: the WPID arrives by itself from the repository's announcement a
    minute or so after the publish run, and a box asking for it before then reads as a step the
    curator has to take. Offered only once the automatic path has visibly failed:

    - the review is ``publish_failed``; or
    - it is ``approved``, the publish workflow that started after the approval has completed,
      and no WPID has arrived ``WPID_FALLBACK_AFTER_SECONDS`` after that run finished.

    "Finished" is the run's ``updated_at`` (GitHub moves it with every change, so for a completed
    run it is about the end). Not ``created_at``: a run that sat queued or ran long would offer
    the form the moment it completed, before its announcement had any time to arrive. A row
    recorded before ``updated_at`` was kept falls back to the start, which is the older rule.

    Reads only what the review row already records (``pipeline_run["publish"]`` is written by
    the background loop), so a page load costs no GitHub call. Same notion of "our run" as
    ``publish_stages``: one that started before the approval belongs to another pathway.
    """
    status = _status_value(review)
    if status == "publish_failed":
        return True
    # Still ``approved`` is "still no WPID": the announcement moves the review to published.
    if status != "approved":
        return False
    approved_at = _when(getattr(review, "approved_at", None))
    if approved_at is None:
        return False
    publish = (pipeline_run or {}).get("publish") or {}
    if not publish:
        return False
    run_at = _when(publish.get("created_at"))
    if run_at is not None and run_at < approved_at.replace(microsecond=0):
        return False
    completed = publish.get("status") == "completed" or publish.get("conclusion") is not None
    if not completed:
        return False
    now = now or datetime.now(UTC)
    finished = _when(publish.get("updated_at")) or run_at
    since = max(finished or approved_at, approved_at)
    return (now - since).total_seconds() >= WPID_FALLBACK_AFTER_SECONDS

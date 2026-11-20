"""The portal's background loop: keeping reviews in line with GitHub off the request path.

Until Wave 2 every ``/dashboard`` load — anonymous ones included — reconciled every open review
synchronously, at four or five GitHub calls each, and wrote checklists and mirror comments as a
side effect of a GET. Twenty open pull requests made a page load cost about eighty API calls.

Now one loop does that work on an interval, and webhook deliveries wake it early for the pull
request they name. Pages render from the database. The loop is single-flight in two ways: a
process-local lock stops two ticks overlapping, and ``CurationService`` claims each review with
a conditional UPDATE of ``last_checked_at`` before re-checking it, so a second replica or a
curator's on-demand refresh never re-checks the same review at the same moment.

``tick()`` is synchronous and safe to call directly, which is how the tests drive it.

A tick runs, in order: the reviews a curator is looking at right now (``request_review``, which
the review page calls instead of reconciling inline while the loop runs; without a loop the page
re-checks inline through ``recheck_now``, since nothing would take the request), the normal pass
over every live review, the re-render of previews an older renderer drew, the probe that tells
the publish tracker when a published page is up, and the curator team refresh. Each step is
isolated: one failing never skips the next.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app import obs
from app.github import GitHubClient, PrFile
from app.models import Review
from app.preview.backfill import refresh_stale_renders
from app.review.untracked import UntrackedPr, scan_untracked

logger = logging.getLogger("wpsubmit.background")


#: How long a re-check asked of the loop counts as "on its way". A card polls every 2 s while it
#: is; past this it falls back to its normal interval, so a loop that is stuck, slow or busy
#: with a long tick cannot keep every open tab asking twice a second.
PENDING_FRESH_SECONDS = 30.0

#: How long after publication the loop keeps asking whether the published page is up. The site
#: build takes two to four minutes and the CDN up to ten; past this the tracker stops waiting.
LIVE_PROBE_WINDOW = timedelta(minutes=30)


@dataclass
class TickResult:
    reconciled: int = 0
    refreshed: int = 0
    scanned: bool = False
    skipped: bool = False
    prioritised: int = 0
    rerendered: int = 0
    live: int = 0


@dataclass
class UntrackedState:
    """The last scan's answer. In memory: one replica, and a restart refills it on a tick."""

    items: list[UntrackedPr] = field(default_factory=list)
    checked_at: datetime | None = None
    error: str | None = None


class BackgroundWorker:
    def __init__(
        self,
        *,
        make_curation: Callable[[GitHubClient | None], object],
        bot_provider: Callable[[], GitHubClient | None],
        session_factory,
        preview,
        content_repo: str,
        untracked_interval_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
        collection=None,
        drafts=None,
        curators=None,
        default_branch: str = "main",
    ) -> None:
        self._collection = collection
        self._drafts = drafts
        self._curators = curators
        self._default_branch = default_branch
        #: Pull requests a curator has open right now, handled first on the next tick.
        self._priority: set[int] = set()
        #: The one being re-checked right now, so a card can keep polling briskly until it lands.
        self._in_flight: set[int] = set()
        #: When each pending request was first made (worker clock), for PENDING_FRESH_SECONDS.
        self._requested_at: dict[int, float] = {}
        #: True while ``run`` is looping. Without a loop (no App identity, or
        #: PORTAL_BACKGROUND_INTERVAL_SECONDS=0) nothing drains ``request_review``, so a page
        #: re-checks inline instead (main.py ``_ask_for_a_recheck``). A test that drives
        #: ``tick`` itself sets this to stand in for the loop.
        self.loop_running = False
        self._priority_lock = threading.Lock()
        self._make_curation = make_curation
        self._bot_provider = bot_provider
        self._session_factory = session_factory
        self._preview = preview
        self._repo = content_repo
        self._untracked_interval = untracked_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_scan: float | None = None
        self._scan_requested = True
        self._file_cache: dict[tuple[int, str], list[PrFile]] = {}
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.untracked = UntrackedState()

    # -- triggers ----------------------------------------------------------------------------

    def request_scan(self) -> None:
        """Re-list untracked pull requests on the next tick rather than at the next interval."""
        self._scan_requested = True

    def forget(self, pr_number: int) -> None:
        """Drop one pull request from the panel — it was just adopted, so it is tracked now."""
        self.untracked.items = [p for p in self.untracked.items if p.number != pr_number]

    def request_review(self, pr_number: int) -> None:
        """Re-check this review first, on a tick that starts now.

        What the review page does instead of reconciling inline: the page renders from the
        database at once and polls its card, which shows the answer a second or two later.
        """
        with self._priority_lock:
            self._priority.add(int(pr_number))
            self._requested_at.setdefault(int(pr_number), self._clock())
        self.poke()

    def priority_pending(self) -> set[int]:
        with self._priority_lock:
            return set(self._priority)

    def is_pending(self, pr_number: int) -> bool:
        """Asked for, not finished yet, and asked recently: queued or being re-checked now.

        False once the request is older than ``PENDING_FRESH_SECONDS`` even if it has not been
        handled, so the 2 s poll it drives is bounded.
        """
        with self._priority_lock:
            if pr_number not in self._priority and pr_number not in self._in_flight:
                return False
            asked = self._requested_at.get(pr_number)
            return asked is None or self._clock() - asked < PENDING_FRESH_SECONDS

    def _take_priority(self) -> int | None:
        with self._priority_lock:
            if not self._priority:
                return None
            pr_number = min(self._priority)
            self._priority.discard(pr_number)
            self._in_flight.add(pr_number)
        return pr_number

    def _done_priority(self, pr_number: int) -> None:
        with self._priority_lock:
            self._in_flight.discard(pr_number)
            if pr_number not in self._priority:
                self._requested_at.pop(pr_number, None)

    def recheck_now(self, pr_number: int) -> bool:
        """Re-check one review here and now, on the caller's thread. True if it ran.

        What a curator's page does when no loop is running to take ``request_review``: the
        same work as the priority pass (reconcile, then the repository's checks), with the same
        claim, so it cannot double up with a tick a test or a second replica runs.
        """
        curation = self._make_curation(self._bot())
        try:
            curation.reconcile_review(pr_number)
            self._refresh_one(curation, pr_number)
        except Exception:  # noqa: BLE001 - a failed re-check must not fail the page
            logger.exception("inline re-check of PR #%s failed", pr_number)
            return False
        return True

    def poke(self) -> None:
        """Run the next tick now. Safe from the event loop and from worker threads."""
        if self._wake is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:  # loop closed during shutdown
            pass

    # -- the work ----------------------------------------------------------------------------

    def _bot(self) -> GitHubClient | None:
        try:
            return self._bot_provider()
        except Exception:  # noqa: BLE001 — no bot this tick; reads that need it wait for the next
            logger.warning("background tick: no GitHub App token", exc_info=True)
            return None

    def tick(self, *, github: GitHubClient | None = None) -> TickResult:
        if not self._lock.acquire(blocking=False):
            return TickResult(skipped=True)
        try:
            with obs.collecting() as stats:
                result = self._tick(github if github is not None else self._bot())
            # The loop's own Server-Timing, since it is where the GitHub calls went.
            logger.info(
                "background tick: %.0fms priority=%d reconciled=%d refreshed=%d rerendered=%d "
                "live=%d %s",
                stats.total_ms(),
                result.prioritised,
                result.reconciled,
                result.refreshed,
                result.rerendered,
                result.live,
                stats.summary(),
            )
            return result
        finally:
            self._lock.release()

    def _tick(self, bot: GitHubClient | None) -> TickResult:
        result = TickResult()
        curation = self._make_curation(bot)
        try:
            result.prioritised = self._run_priority(curation)
        except Exception:  # noqa: BLE001 — one bad tick must not end the loop
            logger.exception("priority pass failed")
        try:
            result.reconciled = curation.reconcile()
        except Exception:  # noqa: BLE001 — one bad tick must not end the loop
            logger.exception("reconcile pass failed")
        try:
            result.refreshed = self._refresh_pipeline_checks(curation)
        except Exception:  # noqa: BLE001
            logger.exception("pipeline-check refresh failed")
        if self._collection is not None:
            try:
                self._refresh_collection(curation)
            except Exception:  # noqa: BLE001
                logger.exception("collection report refresh failed")
        try:
            result.rerendered = refresh_stale_renders(
                self._preview,
                self._session_factory,
                bot,
                content_repo=self._repo,
                default_branch=self._default_branch,
            )
        except Exception:  # noqa: BLE001
            logger.exception("stale render refresh failed")
        try:
            result.live = self._probe_live(curation)
        except Exception:  # noqa: BLE001
            logger.exception("published page probe failed")
        if self._curators is not None:
            try:
                self._curators.refresh_if_stale()
            except Exception:  # noqa: BLE001
                logger.exception("curator team refresh failed")
        if bot is not None and self._scan_due():
            self._scan(bot)
            result.scanned = True
        return result

    def _run_priority(self, curation) -> int:
        """The reviews a curator asked about, before anything else. Returns how many ran.

        The normal pass right after finds them freshly claimed and skips them, so asking costs
        no second check.
        """
        n = 0
        # One at a time, so a review asked for while this pass runs is still handled in it.
        while (pr_number := self._take_priority()) is not None:
            try:
                curation.reconcile_review(pr_number)
                self._refresh_one(curation, pr_number)
                n += 1
            except Exception:  # noqa: BLE001 — the next review still gets its turn
                logger.exception("priority re-check of PR #%s failed", pr_number)
            finally:
                self._done_priority(pr_number)
        return n

    def _refresh_one(self, curation, pr_number: int):
        if not getattr(curation, "is_pipeline_mode", False):
            return None
        meta = self._preview.metadata(pr_number)
        # The *cited* count, not the total: the target repo's generator only emits references
        # a <BiopaxRef> actually points at (see refresh_pipeline_checks).
        return curation.refresh_pipeline_checks(
            pr_number,
            gpml_reference_count=meta.get("cited_reference_count") if meta else None,
        )

    def _probe_live(self, curation) -> int:
        """Ask the site whether recently published pages are up, for the publish tracker."""
        # The reader itself, or a provider of it (the app passes one so a swapped reader is seen).
        drafts = self._drafts() if callable(self._drafts) else self._drafts
        if drafts is None or not hasattr(curation, "recently_published"):
            return 0
        n = 0
        for pr_number, wpid, kind, published_at in curation.recently_published(
            LIVE_PROBE_WINDOW
        ):
            # A new pathway's page did not exist before; an update's did, so it has to be newer.
            since = published_at if kind == "update" else None
            if drafts.page_live(wpid, since) is True:
                curation.mark_live(pr_number)
                n += 1
        return n

    def _refresh_pipeline_checks(self, curation) -> int:
        """Fill checklist items from the repository's generated files, for every live review.

        Used to run on each review page load, which made a GET write. ``refresh_pipeline_checks``
        reads through the drafts reader's own TTL cache, so this costs a network read per review
        per cache lifetime at most.
        """
        if not getattr(curation, "is_pipeline_mode", False):
            return 0
        n = 0
        for pr_number in curation.live_pr_numbers():
            if self._refresh_one(curation, pr_number) is not None:
                n += 1
        return n

    def _refresh_collection(self, curation) -> None:
        """The database's report for every pathway an open update targets (cached a day)."""
        live = set(curation.live_pr_numbers())
        if not live:
            return
        with self._session_factory() as s:
            wpids = {
                wpid
                for pr, wpid in s.execute(
                    select(Review.pr_number, Review.wpid).where(
                        Review.kind == "update", Review.wpid.is_not(None)
                    )
                ).all()
                if pr in live
            }
        for wpid in sorted(wpids):
            self._collection.refresh(wpid)

    def _scan_due(self) -> bool:
        if self._scan_requested or self._last_scan is None:
            return True
        return self._clock() - self._last_scan >= self._untracked_interval

    def _scan(self, bot: GitHubClient) -> None:
        self._scan_requested = False
        self._last_scan = self._clock()
        with self._session_factory() as s:
            tracked = set(s.execute(select(Review.pr_number)).scalars())
        try:
            items = scan_untracked(bot, self._repo, tracked, self._file_cache)
        except Exception as exc:  # noqa: BLE001 — keep the last answer, say it is stale
            logger.warning("untracked pull request scan failed", exc_info=True)
            self.untracked.error = f"The last scan of {self._repo} failed: {exc}"
            return
        self.untracked = UntrackedState(items=items, checked_at=datetime.now(UTC))

    # -- the loop ----------------------------------------------------------------------------

    async def run(self, interval_seconds: float, *, first_delay: float = 5.0) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.loop_running = True
        delay = first_delay
        try:
            while True:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                self._wake.clear()
                delay = interval_seconds
                try:
                    await run_in_threadpool(self.tick)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("background tick failed")
        finally:
            self.loop_running = False
            self._loop = None
            self._wake = None

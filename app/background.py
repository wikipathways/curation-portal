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
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.github import GitHubClient, PrFile
from app.models import Review
from app.review.untracked import UntrackedPr, scan_untracked

logger = logging.getLogger("wpsubmit.background")


@dataclass
class TickResult:
    reconciled: int = 0
    refreshed: int = 0
    scanned: bool = False
    skipped: bool = False


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
    ) -> None:
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
            return self._tick(github if github is not None else self._bot())
        finally:
            self._lock.release()

    def _tick(self, bot: GitHubClient | None) -> TickResult:
        result = TickResult()
        curation = self._make_curation(bot)
        try:
            result.reconciled = curation.reconcile()
        except Exception:  # noqa: BLE001 — one bad tick must not end the loop
            logger.exception("reconcile pass failed")
        try:
            result.refreshed = self._refresh_pipeline_checks(curation)
        except Exception:  # noqa: BLE001
            logger.exception("pipeline-check refresh failed")
        if bot is not None and self._scan_due():
            self._scan(bot)
            result.scanned = True
        return result

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
            meta = self._preview.metadata(pr_number)
            if curation.refresh_pipeline_checks(
                pr_number,
                gpml_reference_count=meta.get("cited_reference_count") if meta else None,
            ) is not None:
                n += 1
        return n

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
        delay = first_delay
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

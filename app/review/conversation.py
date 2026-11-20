"""A pull request's conversation, for the review page.

Curation happens in the pull request thread as much as in the portal: contributors answer there,
curators ask there, and the repository's workflows report there. Until Wave 2 none of it was
visible from the review page, so a curator deciding in the portal was deciding without the
discussion.

Read through a short per-PR cache, invalidated by the webhook for comments and reviews, so a page
refresh does not spend three GitHub calls and a fresh comment still shows at once.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.github import ConversationItem

#: Comments the portal itself posts and keeps up to date. Shown collapsed: the review page already
#: says everything they say.
PORTAL_MARKERS = ("<!-- wikipathways-submit:mirror -->", "<!-- wikipathways-submit:welcome -->")


@dataclass(frozen=True)
class Entry:
    items: list[ConversationItem]
    fetched_at: float


class ConversationCache:
    """Per pull request: fresh for ``ttl_seconds``, then served stale while it is re-read.

    Two things made the conversation slow to appear. Every reader whose entry had expired paid
    for three GitHub reads before the page could show anything, and two readers arriving together
    both paid. Now one fetch per pull request runs at a time (``_flight``), and an entry up to
    ``max_stale_seconds`` old is answered at once while a background task refreshes it
    (``get_or_stale``). The client asks again a moment later and gets the fresh copy.
    """

    def __init__(
        self,
        ttl_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        *,
        max_stale_seconds: float = 600,
    ):
        self._ttl = ttl_seconds
        self._max_stale = max_stale_seconds
        self._clock = clock
        self._entries: dict[int, Entry] = {}
        self._lock = threading.Lock()
        #: One lock per pull request: whoever holds it is the only one fetching that thread.
        self._flight: dict[int, threading.Lock] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def _peek(self, pr_number: int) -> Entry | None:
        with self._lock:
            return self._entries.get(pr_number)

    def _flight_lock(self, pr_number: int) -> threading.Lock:
        with self._lock:
            return self._flight.setdefault(pr_number, threading.Lock())

    def get(self, pr_number: int, fetch: Callable[[], list[ConversationItem]]) -> Entry:
        """A fresh entry, fetching if needed. Concurrent callers share one fetch."""
        entry = self._peek(pr_number)
        if entry is not None and self._clock() - entry.fetched_at < self._ttl:
            return entry
        return self.refresh(pr_number, fetch, only_if_older_than=self._ttl)

    def get_or_stale(
        self, pr_number: int, fetch: Callable[[], list[ConversationItem]]
    ) -> tuple[Entry, bool]:
        """``(entry, stale)``. A stale entry is returned without fetching; refresh it afterwards.

        Past ``max_stale_seconds`` (or with nothing cached) this fetches like ``get``.
        """
        entry = self._peek(pr_number)
        if entry is not None:
            age = self._clock() - entry.fetched_at
            if age < self._ttl:
                return entry, False
            if age < self._max_stale:
                return entry, True
        return self.get(pr_number, fetch), False

    def refresh(
        self,
        pr_number: int,
        fetch: Callable[[], list[ConversationItem]],
        *,
        only_if_older_than: float | None = 0,
    ) -> Entry:
        """Fetch now, unless another caller is already doing it, in which case wait for theirs.

        ``only_if_older_than``: after waiting for the lock, an entry this fresh is the answer the
        other caller just fetched, and is returned instead of fetching again.
        """
        with self._flight_lock(pr_number):
            entry = self._peek(pr_number)
            if (
                entry is not None
                and only_if_older_than is not None
                and self._clock() - entry.fetched_at < only_if_older_than
            ):
                return entry
            entry = Entry(items=fetch(), fetched_at=self._clock())
            now = entry.fetched_at
            with self._lock:
                self._entries[pr_number] = entry
                # Bounded by the number of reviews anyone has opened recently; drop what is too
                # old even to serve stale rather than letting the map grow for the life of the
                # process.
                for key in [
                    k for k, e in self._entries.items() if now - e.fetched_at >= self._max_stale
                ]:
                    if key != pr_number:
                        del self._entries[key]
                        self._flight.pop(key, None)
            return entry

    def invalidate(self, pr_number: int) -> None:
        with self._lock:
            self._entries.pop(pr_number, None)


def present(item: ConversationItem, *, curators: frozenset[str] | set[str]) -> dict:
    """One conversation entry as the page renders it. Bodies stay raw text; the page escapes."""
    portal_own = any(m in item.body for m in PORTAL_MARKERS)
    if portal_own:
        role = "portal"
    elif item.is_bot:
        role = "bot"
    elif item.author in curators:
        role = "curator"
    else:
        role = "person"
    verdict = {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "requested changes",
        "DISMISSED": "review dismissed",
        "COMMENTED": "reviewed",
    }.get(item.state or "")
    return {
        "kind": item.kind,
        "author": item.author,
        "body": item.body,
        "created_at": item.created_at,
        "url": item.html_url,
        "verdict": verdict,
        "path": item.path,
        "role": role,
        # Workflow reports and the portal's own comments are long and repetitive; the page shows
        # them folded so the people in the thread are what a curator reads first.
        "collapsed": role in ("portal", "bot"),
    }

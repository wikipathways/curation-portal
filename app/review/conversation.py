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
    def __init__(self, ttl_seconds: float = 60, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[int, Entry] = {}
        self._lock = threading.Lock()

    def get(self, pr_number: int, fetch: Callable[[], list[ConversationItem]]) -> Entry:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(pr_number)
        if entry is not None and now - entry.fetched_at < self._ttl:
            return entry
        entry = Entry(items=fetch(), fetched_at=now)
        with self._lock:
            self._entries[pr_number] = entry
            # Bounded by the number of reviews anyone has opened recently; drop the stale ones
            # rather than letting the map grow for the life of the process.
            for key in [k for k, e in self._entries.items() if now - e.fetched_at >= self._ttl]:
                if key != pr_number:
                    del self._entries[key]
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

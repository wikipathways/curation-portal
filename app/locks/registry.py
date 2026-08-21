"""Pathway check-out lock — one open edit per pathway at a time (design §4.3).

Acquiring a lock is what lets a submitter start an *update*; it structurally prevents the
#90 "two divergent GPMLs, unmergeable" failure rather than resolving it after the fact.

Two things make this more than a boolean flag:

1. **The lock cannot assume it is the only writer.** A power user can still open a raw GitHub
   PR directly, bypassing the app. So acquisition also runs an injected ``open_pr_scanner`` and
   refuses if an open PR already touches the pathway.
2. **Locks must not become permanent blocks.** They auto-expire (TTL) and a curator can
   force-release, so an abandoned check-out never silently freezes a pathway forever.

Atomicity mirrors the WPID allocator: the pathway id is the primary key, so two simultaneous
acquisitions of the same pathway resolve to exactly one winner via the uniqueness constraint.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import PathwayLock, utcnow

logger = logging.getLogger("wpsubmit.locks")

#: Callable ``(wpid) -> bool``: True if an open GitHub PR already touches this pathway.
OpenPrScanner = Callable[[int], bool]


class LockUnavailable(RuntimeError):
    """Raised when a pathway cannot be checked out."""

    def __init__(self, reason: str, *, held_by: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.held_by = held_by


class PathwayLockRegistry:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        ttl: timedelta = timedelta(days=3),
        open_pr_scanner: OpenPrScanner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._open_pr_scanner = open_pr_scanner

    def expire_stale(self) -> int:
        """Delete locks past their TTL. Returns the number released.

        Every expiry is logged with how long the lock was actually held, because the TTL is a
        guess and this is the only evidence that would ever correct it (issue #23). An expiry is
        not routine: it means a check-out outlived the window, so the pathway's protection fell
        back to the open-PR scanner — which fails open. A run of these is the signal that the TTL
        is too short for how people really work.
        """
        now = utcnow()
        with self._session_factory() as s:
            stale = list(
                s.execute(
                    select(PathwayLock).where(
                        PathwayLock.expires_at.is_not(None),
                        PathwayLock.expires_at < now,
                    )
                ).scalars()
            )
            for lock in stale:
                held = _held_for(lock, now)
                logger.info(
                    "lock on WP%s expired after being held %s by %s (pr=%s, ttl=%s)",
                    lock.wpid,
                    held,
                    lock.held_by,
                    lock.pr_number,
                    self._ttl,
                )
            if not stale:
                return 0
            result = s.execute(
                delete(PathwayLock).where(
                    PathwayLock.wpid.in_([lock.wpid for lock in stale])
                )
            )
            s.commit()
            return result.rowcount or 0

    def age_of(self, wpid: int) -> timedelta | None:
        """How long the lock on this pathway has been held, or None if it is not locked.

        The other half of making the timer observable: an over-long lock is invisible to everyone
        except the person it blocks, so the dashboard needs to be able to say how old one is.
        """
        lock = self.get(wpid)
        return None if lock is None else _held_for(lock, utcnow())

    def get(self, wpid: int) -> PathwayLock | None:
        with self._session_factory() as s:
            return s.get(PathwayLock, wpid)

    def is_locked(self, wpid: int) -> bool:
        self.expire_stale()
        return self.get(wpid) is not None

    def acquire(
        self, wpid: int, held_by: str, *, pr_number: int | None = None
    ) -> PathwayLock:
        """Check out a pathway for editing.

        Refuses if the pathway is already checked out by someone else, or if an open GitHub PR
        already touches it. Re-acquiring one you already hold refreshes the TTL.
        """
        self.expire_stale()

        now = utcnow()
        with self._session_factory() as s:
            existing = s.get(PathwayLock, wpid)
            if existing is not None:
                if existing.held_by == held_by:
                    # No scan on a refresh. The caller already holds this pathway and is usually
                    # coming back to record the pull request it just opened — a pull request the
                    # scan would find and read as a foreign writer, refusing the check-out its
                    # own holder is completing.
                    existing.expires_at = now + self._ttl
                    if pr_number is not None:
                        existing.pr_number = pr_number
                    s.commit()
                    return existing
                raise LockUnavailable(
                    f"pathway WP{wpid} is checked out by {existing.held_by}",
                    held_by=existing.held_by,
                )

        # Nobody holds it here — but a raw pull request opened outside the app is an external
        # writer this table cannot see, and starting a second edit on top of one is the
        # divergence the lock exists to prevent. Only worth the GitHub read on a fresh check-out.
        if self._open_pr_scanner is not None and self._open_pr_scanner(wpid):
            raise LockUnavailable(
                "an open GitHub PR already touches this pathway (opened outside the app)"
            )

        with self._session_factory() as s:
            lock = PathwayLock(
                wpid=wpid,
                held_by=held_by,
                acquired_at=now,
                expires_at=now + self._ttl,
                pr_number=pr_number,
            )
            s.add(lock)
            try:
                s.commit()
            except IntegrityError as exc:
                # Lost the race to a concurrent acquirer.
                raise LockUnavailable(
                    f"pathway WP{wpid} was checked out concurrently"
                ) from exc
            return lock

    def adopt(self, wpid: int, held_by: str, *, pr_number: int) -> PathwayLock | None:
        """Record that a pull request opened outside the app has this pathway checked out.

        **No scan**, and that is the whole reason this is not ``acquire``. The scan exists to find
        a writer this table cannot see; here that writer *is* the thing being recorded, so
        scanning would find the pull request being adopted and refuse the check-out it is
        completing — the same reasoning the same-holder refresh branch above already gives.

        Returns None, and takes nothing, when somebody else holds it. **Never steals.** A curator
        who checked WP1001 out in the portal and is mid-edit must not have it taken away by a
        pull request that appeared on GitHub; two live edits of one GPML is exactly the
        divergence the lock exists to prevent, and the honest response is to record that it has
        happened and show it, not to pick a winner. Several open pull requests on one pathway is
        not hypothetical on the live target — six of them touch WP1001 today.
        """
        self.expire_stale()

        now = utcnow()
        with self._session_factory() as s:
            existing = s.get(PathwayLock, wpid)
            if existing is not None:
                if existing.held_by != held_by:
                    return None
                existing.expires_at = now + self._ttl
                existing.pr_number = pr_number
                s.commit()
                return existing
            lock = PathwayLock(
                wpid=wpid,
                held_by=held_by,
                acquired_at=now,
                expires_at=now + self._ttl,
                pr_number=pr_number,
            )
            s.add(lock)
            try:
                s.commit()
            except IntegrityError:
                # Lost the race to a concurrent acquirer. Same answer as "somebody else holds
                # it", because that is now true.
                return None
            return lock

    def release_for_pr(self, wpid: int, pr_number: int) -> bool:
        """Release the lock **only** if this pull request is the one holding it.

        ``release(force=True)`` frees a lock whoever holds it, which was safe while one pathway
        could have only one review. Adoption ends that: closing one of the six open pull requests
        on WP1001 would otherwise free a lock another of them holds, and the next portal update
        would sail past the app's own table.
        """
        with self._session_factory() as s:
            lock = s.get(PathwayLock, wpid)
            if lock is None or lock.pr_number != pr_number:
                return False
            s.delete(lock)
            s.commit()
            return True

    def release(self, wpid: int, held_by: str, *, force: bool = False) -> bool:
        """Release a lock. ``force=True`` is the curator override (any holder).

        Returns True if a lock was released, False if none was held. Raises LockUnavailable if
        a non-force caller tries to release someone else's lock.
        """
        with self._session_factory() as s:
            lock = s.get(PathwayLock, wpid)
            if lock is None:
                return False
            if not force and lock.held_by != held_by:
                raise LockUnavailable(
                    f"pathway WP{wpid} is held by {lock.held_by}, not {held_by}",
                    held_by=lock.held_by,
                )
            s.delete(lock)
            s.commit()
            return True


def _held_for(lock: PathwayLock, now) -> timedelta:
    """How long a lock has been held, tolerating a naive timestamp.

    SQLite hands back naive datetimes where Postgres does not, and this is only ever used to
    describe a duration in a log line or on a page — never to decide anything — so it coerces
    rather than raising.
    """
    acquired = lock.acquired_at
    if acquired is None:
        return timedelta(0)
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=UTC)
    return now - acquired

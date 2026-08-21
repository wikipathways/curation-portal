"""How often one account may open a pull request on the content repo (issue #21).

Nothing limited this before, so a loop against ``/api/submit`` wrote to a repository the app does
not own at whatever rate the network allowed. The cost of that is not paid here: it is branches,
notifications to every watcher of ``wikipathways-database``, and one run of a full generation
pipeline per junk submission, cleaned up by hand by its maintainers. It does not take malice
either — a retry loop in a script, or a submitter double-clicking through a slow response, makes
the same thing at a smaller scale.

**Counted out of the ``review`` table, not a bucket in memory.** The app is a single replica whose
only shared store is the database, so an in-process counter resets on every redeploy — which is
both frequent and exactly when a limiter is least likely to be watched. The review row is already
the record of what somebody did, and it is created in the same request that opens the pull
request, so counting rows is counting pull requests with nothing to keep in step.

**Keyed on the GitHub login rather than the IP.** These endpoints are authenticated, so the login
is the identity that means anything: one person behind a shared address is not several submitters,
and one person on a phone is not several either. ``/api/validate`` takes no login and is therefore
not covered here at all — it wants a blunt outer bound at the proxy, which is the note in #21.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.models import Review, utcnow


class RateLimited(Exception):
    """Too many pull requests from one account inside the window.

    Carries ``retry_after`` in whole seconds so the route can answer with the header of the same
    name — a client that respects it stops hammering without anyone reading the message.
    """

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class SubmissionRateLimiter:
    def __init__(self, session_factory, *, limit: int, window: timedelta) -> None:
        self._session_factory = session_factory
        self._limit = limit
        self._window = window

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def check(self, submitter: str) -> None:
        """Raise ``RateLimited`` if this account has had its fill of the window.

        Called before the write rather than after, so a refusal costs nothing on GitHub. It is
        checked and not reserved: two requests racing can both pass and put the account one over,
        which is the right trade at this end of the scale — the limit is a bound on a runaway
        loop, not an accounting boundary, and a transaction per submission to enforce an
        off-by-one would cost more than it protects.
        """
        if not self.enabled:
            return
        since = utcnow() - self._window
        # Portal rows only. An *adopted* row records a pull request its author opened on GitHub,
        # without going near this app — counting those would spend somebody's portal quota on
        # submissions the portal did not accept, and the refusal would read as if they had done
        # something wrong. The limiter bounds what this app will open on a person's behalf.
        mine = (Review.submitter == submitter, Review.created_at >= since,
                Review.origin == "portal")
        with self._session_factory() as s:
            recent = (
                s.execute(select(func.count()).select_from(Review).where(*mine)).scalar()
                or 0
            )
            if recent < self._limit:
                return
            # When the window frees up: the oldest submission still inside it, plus the window.
            oldest = s.execute(select(func.min(Review.created_at)).where(*mine)).scalar()
        retry_after = int(self._window.total_seconds())
        if oldest is not None:
            freed = _aware(oldest) + self._window
            retry_after = int((freed - utcnow()).total_seconds())
        minutes = max(1, round(self._window.total_seconds() / 60))
        raise RateLimited(
            f"You have opened {recent} pull requests in the last {minutes} minutes, which is as "
            "many as this portal will open for one account in that time. Nothing is lost — wait "
            "for the window to clear and upload again. If you are submitting a batch of pathways, "
            "say so on an existing pull request and a curator can raise this.",
            retry_after=retry_after,
        )


def _aware(value):
    """SQLite hands back naive datetimes; Postgres does not."""
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

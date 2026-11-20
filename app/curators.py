"""Curator whitelist resolution (issue #9).

Who may approve-that-merges (~20 people, design §4.5). Two backends behind one interface so the
rest of the app never cares which is in use:

- **ConfigCurators** — a static list from ``WPSUBMIT_CURATORS`` (simple, no GitHub dependency).
- **GitHubTeamCurators** — membership of a GitHub Team (``WPSUBMIT_CURATOR_TEAM='org/slug'``),
  so curator management happens on GitHub (add/remove a team member) rather than in a redeploy.
  Resolved via the bot client and cached with a TTL; **fail-closed** — if GitHub can't be reached
  and we have no cached membership, nobody is a curator (safer than failing open on an authz gate).

Decision (2026-07-25): the GitHub Team is the intended mechanism; the config list stays as the
fallback when no team is set (and for tests / local dev without the bot).

With ``background=True`` (how the app builds it) an expired membership is served as it stands and
the background loop re-reads it (``refresh_if_stale``). Every page asks ``is_curator``, so the
read used to land on whichever request happened to arrive after the TTL, holding it for a token
exchange and a team listing. Only a cold start, with nothing cached at all, still reads inline.

Since 2026-11-13 the two **add up** when both are set. Setting a team used to drop the configured
list silently, so turning the team on (which needs an org owner to create it and the App to be
granted Members:read) would have left nobody able to approve until both had happened. Now the
configured logins stay curators throughout, and the list can be emptied once the team is trusted.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import timedelta

import httpx

from app.github import GitHubClient, GitHubError

#: How often a request may retry a team read that failed with nothing cached.
_COLD_RETRY_SECONDS = 60.0


class CuratorRegistry(ABC):
    @abstractmethod
    def members(self) -> set[str]:
        """The current curator logins."""

    def is_curator(self, user: str | None) -> bool:
        return bool(user) and user in self.members()

    def refresh_if_stale(self) -> bool:
        """Re-read the membership if it is due. Called by the background loop. True if it read."""
        return False


class ConfigCurators(CuratorRegistry):
    def __init__(self, logins: Iterable[str]) -> None:
        self._logins = {login for login in logins if login}

    def members(self) -> set[str]:
        return set(self._logins)


class GitHubTeamCurators(CuratorRegistry):
    """Curators = members of ``org/team_slug``, resolved via the bot client, cached for ``ttl``.

    ``client_provider`` returns a GitHubClient (the App/bot identity) or None when the bot is not
    configured. On a refresh error the last good membership is retained; if there is none yet, the
    set is empty (fail-closed).
    """

    def __init__(
        self,
        client_provider: Callable[[], GitHubClient | None],
        org: str,
        team_slug: str,
        *,
        ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], float] = time.monotonic,
        background: bool = False,
    ) -> None:
        self._background = background
        self._client_provider = client_provider
        self._org = org
        self._team_slug = team_slug
        self._ttl = ttl.total_seconds()
        self._clock = clock
        self._cache: set[str] | None = None
        self._fetched_at: float = 0.0
        #: When a read was last attempted, so a team that cannot be read at a cold start is not
        #: re-asked by every request in the meantime (background mode only).
        self._attempted_at: float | None = None

    def _stale(self) -> bool:
        return self._cache is None or self._clock() - self._fetched_at >= self._ttl

    def members(self) -> set[str]:
        if self._cache is not None and (self._background or not self._stale()):
            return set(self._cache)
        if (
            self._background
            and self._attempted_at is not None
            and self._clock() - self._attempted_at < _COLD_RETRY_SECONDS
        ):
            return set()  # fail-closed, and the loop retries
        return self._fetch()

    def refresh_if_stale(self) -> bool:
        if not self._stale():
            return False
        self._fetch()
        return True

    def _fetch(self) -> set[str]:
        now = self._clock()
        self._attempted_at = now
        try:
            client = self._client_provider()
        except Exception:  # noqa: BLE001 — minting the bot token failed; same as no bot
            client = None
        if client is None:
            # No bot configured: can't resolve. Keep any prior cache; else nobody is a curator.
            return set(self._cache) if self._cache is not None else set()
        try:
            logins = set(client.list_team_members(self._org, self._team_slug))
        except (GitHubError, httpx.HTTPError):
            # Transient failure: serve stale membership rather than lock everyone out.
            return set(self._cache) if self._cache is not None else set()
        self._cache = logins
        self._fetched_at = now
        return set(logins)


class CombinedCurators(CuratorRegistry):
    """Anyone in any of ``registries``. A team that cannot be read adds nobody; the rest stand."""

    def __init__(self, *registries: CuratorRegistry) -> None:
        self._registries = registries

    def members(self) -> set[str]:
        out: set[str] = set()
        for registry in self._registries:
            out |= registry.members()
        return out

    def refresh_if_stale(self) -> bool:
        read = False
        for registry in self._registries:
            read = registry.refresh_if_stale() or read
        return read


def make_curator_registry(
    *,
    team: str | None,
    config_logins: Iterable[str],
    bot_client_provider: Callable[[], GitHubClient | None],
    background: bool = False,
) -> CuratorRegistry:
    """The configured logins, plus the members of ``team`` ('org/slug') when one is set.

    ``background``: an expired team membership is re-read by the background loop rather than by
    the request that notices (see the module docstring).
    """
    configured = ConfigCurators(config_logins)
    if team:
        org, _, slug = team.partition("/")
        if org and slug:
            team_registry = GitHubTeamCurators(
                bot_client_provider, org, slug, background=background
            )
            if not configured.members():
                return team_registry
            return CombinedCurators(configured, team_registry)
    return configured

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
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import timedelta

import httpx

from app.github import GitHubClient, GitHubError


class CuratorRegistry(ABC):
    @abstractmethod
    def members(self) -> set[str]:
        """The current curator logins."""

    def is_curator(self, user: str | None) -> bool:
        return bool(user) and user in self.members()


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
    ) -> None:
        self._client_provider = client_provider
        self._org = org
        self._team_slug = team_slug
        self._ttl = ttl.total_seconds()
        self._clock = clock
        self._cache: set[str] | None = None
        self._fetched_at: float = 0.0

    def members(self) -> set[str]:
        now = self._clock()
        if self._cache is not None and now - self._fetched_at < self._ttl:
            return set(self._cache)
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


def make_curator_registry(
    *,
    team: str | None,
    config_logins: Iterable[str],
    bot_client_provider: Callable[[], GitHubClient | None],
) -> CuratorRegistry:
    """Pick the backend: a GitHub Team if ``team`` ('org/slug') is set, else the config list."""
    if team:
        org, _, slug = team.partition("/")
        if org and slug:
            return GitHubTeamCurators(bot_client_provider, org, slug)
    return ConfigCurators(config_logins)

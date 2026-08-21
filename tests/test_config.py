"""Settings resolution, and specifically the two env prefixes the rename left behind.

The project was `wikipathways-submit` until 2026-08-05, `pathway-portal` until 2026-08-21, and is
`curation-portal` now. Renaming the env prefix outright would have made each rename a flag day:
every variable on the live swarm service, plus its Docker secrets, would have to change in the
same breath as the image, and a typo inside that window takes the service down for a purely
cosmetic gain. Reading both prefixes turns that into something the deployment can do whenever it
likes, or never.
"""
from __future__ import annotations

from app.config import Settings


def test_the_legacy_prefix_is_still_read(monkeypatch):
    """A deployment that has not been touched since the rename must keep working untouched."""
    monkeypatch.delenv("PORTAL_CONTENT_REPO", raising=False)
    monkeypatch.setenv("WPSUBMIT_CONTENT_REPO", "legacy/repo")

    assert Settings(_env_file=None).content_repo == "legacy/repo"


def test_the_new_prefix_wins_when_both_are_set(monkeypatch):
    """The state a migration passes through, so it must not be a coin toss: during the cutover
    both are set, and the new one has to win or the change silently does nothing."""
    monkeypatch.setenv("WPSUBMIT_CONTENT_REPO", "legacy/repo")
    monkeypatch.setenv("PORTAL_CONTENT_REPO", "new/repo")

    assert Settings(_env_file=None).content_repo == "new/repo"


def test_the_new_prefix_works_alone(monkeypatch):
    """The end state, once the legacy variables are removed."""
    monkeypatch.delenv("WPSUBMIT_CONTENT_REPO", raising=False)
    monkeypatch.setenv("PORTAL_CONTENT_REPO", "new/repo")

    assert Settings(_env_file=None).content_repo == "new/repo"


def test_a_secret_carries_over_too(monkeypatch):
    """Not just the plain strings. The secrets are the part of a flag-day cutover that actually
    hurts, so the fallback has to cover them or it has not removed the risk it exists to remove."""
    monkeypatch.delenv("PORTAL_SESSION_SECRET", raising=False)
    monkeypatch.setenv("WPSUBMIT_SESSION_SECRET", "from-the-old-name")

    assert Settings(_env_file=None).session_secret == "from-the-old-name"

"""Curator whitelist resolution (issue #9): config list + GitHub Team with caching/fail-closed."""
from __future__ import annotations

from datetime import timedelta

from app.curators import (
    CombinedCurators,
    ConfigCurators,
    GitHubTeamCurators,
    make_curator_registry,
)
from app.github import FakeGitHubClient, GitHubError


def test_config_curators():
    reg = ConfigCurators(["alice", "bob"])
    assert reg.is_curator("alice")
    assert not reg.is_curator("carol")
    assert not reg.is_curator(None)
    assert reg.members() == {"alice", "bob"}


def test_team_curators_resolve_and_cache():
    gh = FakeGitHubClient(team_members={"wikipathways/curators": ["alice", "bob"]})
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return gh

    clock = {"t": 0.0}
    reg = GitHubTeamCurators(
        provider, "wikipathways", "curators", ttl=timedelta(minutes=5), clock=lambda: clock["t"]
    )
    assert reg.is_curator("alice") and not reg.is_curator("carol")
    assert reg.members() == {"alice", "bob"}
    # Within the TTL the team is not re-fetched.
    clock["t"] = 60.0
    assert reg.members() == {"alice", "bob"}
    assert calls["n"] == 1
    # After the TTL it refreshes and picks up a change.
    gh.team_members["wikipathways/curators"] = ["alice", "carol"]
    clock["t"] = 10 * 60.0
    assert reg.members() == {"alice", "carol"}
    assert calls["n"] == 2


def test_team_curators_fail_closed_then_serve_stale():
    failing = FakeGitHubClient(
        team_members={"wikipathways/curators": ["alice"]}, fail_on={"list_team_members"}
    )
    reg = GitHubTeamCurators(lambda: failing, "wikipathways", "curators")
    # No cache yet and GitHub errors → nobody is a curator (fail-closed).
    assert reg.members() == set()

    # Once a good fetch has populated the cache, a later failure serves the stale set.
    ok = FakeGitHubClient(team_members={"wikipathways/curators": ["alice", "bob"]})
    reg2 = GitHubTeamCurators(lambda: ok, "wikipathways", "curators", ttl=timedelta(seconds=0))
    assert reg2.members() == {"alice", "bob"}
    ok.fail_on = {"list_team_members"}
    assert reg2.members() == {"alice", "bob"}  # stale-but-safe rather than locking everyone out


def test_team_curators_no_bot_returns_empty():
    reg = GitHubTeamCurators(lambda: None, "org", "team")
    assert reg.members() == set()


def test_make_registry_picks_backend():
    assert isinstance(
        make_curator_registry(team=None, config_logins=["a"], bot_client_provider=lambda: None),
        ConfigCurators,
    )
    assert isinstance(
        make_curator_registry(
            team="org/team", config_logins=[], bot_client_provider=lambda: FakeGitHubClient()
        ),
        GitHubTeamCurators,
    )
    # Malformed team string falls back to config.
    assert isinstance(
        make_curator_registry(
            team="no-slash", config_logins=["a"], bot_client_provider=lambda: None
        ),
        ConfigCurators,
    )


def test_github_error_is_raised_type():
    # Sanity: the fake raises GitHubError for list_team_members when configured to fail.
    gh = FakeGitHubClient(fail_on={"list_team_members"})
    try:
        gh.list_team_members("o", "t")
    except GitHubError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected GitHubError")


def test_a_team_adds_to_the_configured_curators_rather_than_replacing_them():
    gh = FakeGitHubClient(team_members={"wikipathways/curators": ["bob"]})
    reg = make_curator_registry(
        team="wikipathways/curators", config_logins=["alice"], bot_client_provider=lambda: gh
    )
    assert isinstance(reg, CombinedCurators)
    assert reg.members() == {"alice", "bob"}
    assert reg.is_curator("alice") and reg.is_curator("bob")


def test_configured_curators_stand_when_the_team_cannot_be_read():
    gh = FakeGitHubClient(fail_on={"list_team_members"})
    reg = make_curator_registry(
        team="wikipathways/curators", config_logins=["alice"], bot_client_provider=lambda: gh
    )
    assert reg.members() == {"alice"}

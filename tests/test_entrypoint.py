"""The container entrypoint reads secrets and the database URL under both prefixes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


def _run(tmp_path: Path, env: dict[str, str], secrets: dict[str, str]) -> str:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    for name, value in secrets.items():
        (secrets_dir / name).write_text(value)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_alembic = bin_dir / "alembic"
    fake_alembic.write_text("#!/bin/sh\necho ALEMBIC-RAN \"$@\"\n")
    fake_alembic.chmod(0o755)
    base = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SECRETS_DIR": str(secrets_dir)}
    out = subprocess.run(
        ["sh", str(ENTRYPOINT), "env"],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.mark.parametrize("prefix", ["portal", "wpsubmit"])
def test_secrets_load_under_either_name(tmp_path, prefix):
    out = _run(tmp_path, {}, {f"{prefix}_session_secret": "s3", f"{prefix}_webhook_secret": "w"})
    upper = prefix.upper()
    assert f"{upper}_SESSION_SECRET=s3" in out
    assert f"{upper}_GITHUB_WEBHOOK_SECRET=w" in out


def test_a_set_variable_wins_over_the_secret_file(tmp_path):
    out = _run(tmp_path, {"PORTAL_SESSION_SECRET": "env"}, {"portal_session_secret": "file"})
    assert "PORTAL_SESSION_SECRET=env" in out


@pytest.mark.parametrize("var", ["PORTAL_DATABASE_URL", "WPSUBMIT_DATABASE_URL"])
def test_migrations_run_for_postgres_under_either_name(tmp_path, var):
    out = _run(tmp_path, {var: "postgresql://db/portal"}, {})
    assert "ALEMBIC-RAN upgrade head" in out


def test_migrations_run_when_the_url_comes_from_a_renamed_secret(tmp_path):
    out = _run(tmp_path, {}, {"portal_database_url": "postgresql://db/portal"})
    assert "ALEMBIC-RAN upgrade head" in out


def test_sqlite_skips_migrations(tmp_path):
    out = _run(tmp_path, {"PORTAL_DATABASE_URL": "sqlite:///x.db"}, {})
    assert "ALEMBIC-RAN" not in out

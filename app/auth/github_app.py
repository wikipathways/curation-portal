"""GitHub App (bot) identity — the second, privileged identity (scaffolding-plan §3).

Two identities, deliberately:

- **Per-user OAuth** (``github_oauth``) opens the branch / PR *as the submitter*, so authorship
  is real and attributed.
- **GitHub App** (this module) performs the privileged, cross-cutting actions the user token
  must not: merging on curator approval and posting the read-only PR-preview *mirror* comment.
  A stable bot identity is also what future webhooks (PR opened/closed → expire locks, issue #8)
  authenticate against.

Authenticating as the App is two-legged: sign a short-lived RS256 JWT with the App's private
key (proves "I am this App"), exchange it for a per-installation access token (valid ~1h, proves
"I am this App *on this repo*"), and cache that token until shortly before it expires so we don't
mint a new one on every call.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
import jwt

from app import obs

_GITHUB_API = "https://api.github.com"
#: Refresh the installation token this many seconds before its stated expiry (clock-skew slack).
_REFRESH_SLACK = 60
#: GitHub caps App JWT lifetime at 10 min; stay safely under it.
_JWT_TTL = 9 * 60


class GitHubAppError(RuntimeError):
    """Authenticating as the GitHub App failed."""


@dataclass
class GitHubApp:
    """Mints installation access tokens for a GitHub App installed on the content repo.

    ``private_key`` is the PEM contents of the App's private key (loaded from a Docker secret in
    production — never the repo). ``transport`` is an injection seam for tests.
    """

    app_id: str
    private_key: str
    installation_id: str
    base_url: str = _GITHUB_API
    transport: httpx.BaseTransport | None = None
    _token: str | None = field(default=None, init=False, repr=False)
    _token_exp: float = field(default=0.0, init=False, repr=False)

    def _app_jwt(self, now: float) -> str:
        # iat backdated for clock skew between us and GitHub; iss is the App (client) id.
        payload = {"iat": int(now) - _REFRESH_SLACK, "exp": int(now) + _JWT_TTL, "iss": self.app_id}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def installation_token(self, *, now: float | None = None) -> str:
        """Return a valid installation token, reusing the cached one until near expiry."""
        now = time.time() if now is None else now
        if self._token is not None and now < self._token_exp - _REFRESH_SLACK:
            return self._token

        try:
            app_jwt = self._app_jwt(now)
        except Exception as exc:  # bad/malformed private key
            raise GitHubAppError(f"could not sign App JWT: {exc}") from exc

        with httpx.Client(base_url=self.base_url, transport=self.transport, timeout=30.0) as client:
            obs.instrument_httpx(client, "gh")
            resp = client.post(
                f"/app/installations/{self.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if resp.is_error:
            raise GitHubAppError(
                f"installation-token exchange failed: {resp.status_code} {resp.text}"
            )
        self._token = resp.json()["token"]
        # Tokens live ~1h; we conservatively assume 1h and refresh _REFRESH_SLACK early. (GitHub
        # also returns an ISO expiry; the fixed TTL avoids a timezone-parse dependency here.)
        self._token_exp = now + 3600
        return self._token

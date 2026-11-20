"""Response security headers for the HTML pages.

The review page now inlines the pathway drawing into the DOM (the viewer), so a mistake in the
renderer's escaping would no longer be contained by ``<img>``. A Content-Security-Policy that
forbids inline script is the second line of defence. It starts as *Report-Only* so a policy that
blocks something legitimate shows up in the browser console instead of breaking the page. Enforced
since the browser pass of 2026-11-20 showed every page clean; ``PORTAL_CSP_REPORT_ONLY=true``
turns it back to report-only.

The templates carry no inline ``<script>`` bodies and no ``on*=`` handlers, which is what makes
``script-src 'self'`` possible at all. Keep it that way.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def settings_site_host(settings) -> str:
    """The published site's origin (``https://sandbox.wikipathways.org``), for ``img-src``."""
    base = getattr(settings, "drafts_site_base_url", "") or ""
    parts = urlsplit(base)
    if parts.scheme == "https" and parts.netloc:
        return f"https://{parts.netloc}"
    return ""


def build_csp(site_host: str = "") -> str:
    img = "'self' data: https://raw.githubusercontent.com"
    if site_host:
        img += f" {site_host}"
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            f"img-src {img}",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
            "form-action 'self' https://github.com",
        )
    )


class SecurityHeadersMiddleware:
    """Adds the CSP (report-only unless ``enforce``) and ``nosniff`` to ``text/html`` responses.

    Pure ASGI rather than ``BaseHTTPMiddleware``, so it neither buffers nor breaks streaming and
    background tasks.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        site_host: str = "",
        enforce: bool = False,
        exempt_paths: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        # FastAPI's Swagger page loads its scripts from a CDN and starts them inline, which this
        # policy forbids by design; it carries no user content, so it goes without the header.
        self.exempt_paths = exempt_paths
        self.header = (
            b"content-security-policy" if enforce else b"content-security-policy-report-only"
        )
        self.value = build_csp(site_host).encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html"):
                    names = {k.lower() for k, _ in headers}
                    if self.header not in names:
                        headers.append((self.header, self.value))
                    if b"x-content-type-options" not in names:
                        headers.append((b"x-content-type-options", b"nosniff"))
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

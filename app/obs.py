"""Where a request's time goes: GitHub, the raw-file host, the database, the render cache.

"The review page is slow" had no number behind it. Each request now carries a ``RequestStats``
in a context variable; the HTTP clients, the SQLAlchemy engine and the preview cache add to it,
and ``TimingMiddleware`` reports the result twice: as a ``Server-Timing`` header, which a browser's
network panel draws as a bar per category, and as one log line per request.

The stats object is mutable on purpose. ``run_in_threadpool`` runs a sync route in a *copy* of the
caller's context, so a ``ContextVar.set`` made inside the thread would never be seen by the
middleware; appending to the object the variable already points at is.

Nothing here may fail a request. Every hook is a few additions under a lock.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("wpsubmit.obs")

#: The categories, in the order the header and the log line list them. ``gh`` is the GitHub API
#: (both identities, and the App's token exchange), ``raw`` the anonymous reads of
#: raw.githubusercontent.com and the published site, ``db`` SQL statements, ``disk`` the render
#: cache under the preview directory.
KINDS = ("gh", "raw", "db", "disk")
_DESCRIPTIONS = {"gh": "GitHub", "raw": "raw files", "db": "SQL", "disk": "render cache"}


@dataclass
class RequestStats:
    """Counts and milliseconds per category for one request (or one background tick)."""

    started: float = field(default_factory=time.perf_counter)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    ms: dict[str, float] = field(default_factory=lambda: dict.fromkeys(KINDS, 0.0))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, seconds: float) -> None:
        with self._lock:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.ms[kind] = self.ms.get(kind, 0.0) + seconds * 1000.0

    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0

    def server_timing(self) -> str:
        parts = [f"total;dur={self.total_ms():.1f}"]
        for kind in KINDS:
            n = self.counts.get(kind, 0)
            if n:
                parts.append(
                    # "x", not a multiplication sign: header values are Latin-1 on the wire and
                    # several clients (Starlette's test client among them) decode them as ASCII.
                    f'{kind};desc="{_DESCRIPTIONS[kind]} x{n}";dur={self.ms[kind]:.1f}'
                )
        return ", ".join(parts)

    def summary(self) -> str:
        """``gh=3/412ms raw=0 db=14/9ms disk=4/1ms``: the log line's tail."""
        out = []
        for kind in KINDS:
            n = self.counts.get(kind, 0)
            out.append(f"{kind}={n}/{self.ms.get(kind, 0.0):.0f}ms" if n else f"{kind}=0")
        return " ".join(out)


_current: ContextVar[RequestStats | None] = ContextVar("wpsubmit_request_stats", default=None)


def current() -> RequestStats | None:
    return _current.get()


def record(kind: str, seconds: float) -> None:
    stats = _current.get()
    if stats is not None:
        stats.add(kind, seconds)


@contextlib.contextmanager
def timed(kind: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        record(kind, time.perf_counter() - t0)


@contextlib.contextmanager
def collecting() -> Iterator[RequestStats]:
    """A fresh ``RequestStats`` for the block, restored afterwards. For the background tick."""
    stats = RequestStats()
    token = _current.set(stats)
    try:
        yield stats
    finally:
        _current.reset(token)


# -- instrumentation ----------------------------------------------------------------------------

_T0 = "wpsubmit_t0"


def instrument_httpx(client, kind: str):
    """Count and time every request ``client`` sends, under ``kind``. Returns the client.

    Event hooks rather than a wrapping transport, because the GitHub clients share one transport
    across both identities and every request. The response hook fires when the headers are in,
    so the body read is not counted; for these small JSON and text replies that is most of it.
    """

    def on_request(request) -> None:
        request.extensions[_T0] = time.perf_counter()

    def on_response(response) -> None:
        t0 = response.request.extensions.get(_T0)
        if t0 is not None:
            record(kind, time.perf_counter() - t0)

    try:
        hooks = client.event_hooks
        hooks.setdefault("request", []).append(on_request)
        hooks.setdefault("response", []).append(on_response)
        client.event_hooks = hooks
    except Exception:  # noqa: BLE001 - observability must never break a client
        logger.debug("could not instrument an httpx client", exc_info=True)
    return client


def instrument_engine(engine) -> None:
    """Count every SQL statement the engine runs, and time it. Idempotent per engine."""
    from sqlalchemy import event

    if getattr(engine, "_wpsubmit_obs", False):
        return
    engine._wpsubmit_obs = True

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        conn.info.setdefault("wpsubmit_t0", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        stack = conn.info.get("wpsubmit_t0")
        if stack:
            record("db", time.perf_counter() - stack.pop())


def time_methods(obj, kind: str, names: tuple[str, ...]) -> None:
    """Wrap ``obj``'s named methods so each call is timed under ``kind``.

    Instance attributes shadow the class's methods, so the class itself is left alone and a test
    that builds its own instance sees the plain one. A name the object does not have is skipped.
    """
    for name in names:
        method = getattr(obj, name, None)
        if method is None or not callable(method):
            continue
        setattr(obj, name, _timed_call(method, kind))


def _timed_call(method: Callable, kind: str) -> Callable:
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return method(*args, **kwargs)
        finally:
            record(kind, time.perf_counter() - t0)

    wrapper.__wrapped__ = method  # type: ignore[attr-defined]
    wrapper.__name__ = getattr(method, "__name__", "wrapped")
    return wrapper


# -- the middleware -----------------------------------------------------------------------------

#: Paths not worth a log line: the container healthcheck polls every 30 seconds, and static files
#: are a 304 the browser asks for on every page.
_QUIET_PREFIXES = ("/static/", "/health")


class TimingMiddleware:
    """``Server-Timing`` on every response, and one log line per request.

    Pure ASGI, like ``SecurityHeadersMiddleware``: it must not buffer the body or break background
    tasks. Registered last so it is the outermost layer and its total covers the others.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        stats = RequestStats()
        token = _current.set(stats)
        status = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message.get("status", 0)
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", stats.server_timing().encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _current.reset(token)
            path = scope.get("path", "")
            level = logging.DEBUG if path.startswith(_QUIET_PREFIXES) else logging.INFO
            if logger.isEnabledFor(level):
                logger.log(
                    level,
                    "%s %s %s %.0fms %s",
                    scope.get("method", ""),
                    path,
                    status["code"],
                    stats.total_ms(),
                    stats.summary(),
                )

"""Every link the app writes into a GitHub comment has to resolve to a route it serves.

A comment is the one surface where a wrong URL is invisible from inside the app: nothing renders
it, nothing follows it, no test asserted on it, and the person who finds out is a submitter who
clicked it. So it went unnoticed that ``render_welcome_comment`` pointed at ``/reviews/{pr}`` —
a path that has never existed. ``/api/reviews/{pr}`` is JSON and the HTML page has always been
``/dashboard/{pr}``. Every submission since welcome comments were added carried a link returning
``{"detail":"Not Found"}``, and the mirror comment sitting directly beneath it had the right URL
the whole time, which is exactly why nobody spotted the difference.

Found by following the link in a browser on 2026-08-14, PR #42. Testing the *contents* of these
comments is not the fix — that only pins whatever string was there when the test was written.
The fix is checking the link against the app's own route table, so any future comment gets the
same guarantee for free.
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from app.config import Settings
from app.main import build_app
from app.models import Review, ReviewStatus
from app.review.service import render_mirror_comment, render_welcome_comment

BASE = "https://upload.wikipathways.org"

#: Matches a path in a URL we wrote, e.g. ``/dashboard/42`` out of the full link.
_URL = re.compile(re.escape(BASE) + r"(/[^\s,.)\]]*)")


def _served_paths() -> list[str]:
    app = build_app(Settings(_env_file=None, database_url="sqlite://"))
    return [r.path for r in app.routes if isinstance(r, APIRoute)] + [
        r.path for r in app.routes if not isinstance(r, APIRoute) and hasattr(r, "path")
    ]


def _resolves(path: str, served: list[str]) -> bool:
    """Does ``path`` match one of the app's route templates, ``{param}`` matching one segment?"""
    parts = path.strip("/").split("/")
    for template in served:
        tparts = template.strip("/").split("/")
        if len(tparts) != len(parts):
            continue
        if all(t.startswith("{") or t == p for t, p in zip(tparts, parts, strict=True)):
            return True
    return False


def _review() -> Review:
    return Review(
        pr_number=42,
        wpid=None,
        submitter="mmarvinm2",
        kind="new",
        status=ReviewStatus.OPEN,
        checklist=[],
    )


@pytest.mark.parametrize(
    "render",
    [
        pytest.param(lambda r: render_welcome_comment(r, base_url=BASE), id="welcome"),
        pytest.param(
            lambda r: render_mirror_comment(r, "owner/repo", base_url=BASE), id="mirror"
        ),
    ],
)
def test_links_in_comments_resolve_to_a_real_route(render):
    body = render(_review())
    paths = _URL.findall(body)
    assert paths, "the comment carried no link to this app at all — has base_url stopped working?"

    served = _served_paths()
    for path in paths:
        head = path.split("/")[1:2]
        nearby = sorted(p for p in served if p.split("/")[1:2] == head)
        assert _resolves(path, served), (
            f"{path} is written into a comment but the app serves no such route. "
            f"Closest by prefix: {nearby}"
        )


def test_the_matcher_would_actually_fail_on_the_bug_it_was_written_for():
    """Guard the guard: a test that cannot fail is worse than no test.

    ``/reviews/42`` is the exact string that shipped. If ``_resolves`` ever starts accepting it —
    because the matcher loosened, or because a ``/reviews`` route appeared and the check is now
    vacuous — this says so instead of the suite going quietly green.
    """
    served = _served_paths()
    assert not _resolves("/reviews/42", served)
    assert _resolves("/dashboard/42", served)

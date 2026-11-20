"""The browser's own wording for the update form, the approval toast and a card's ages.

The bugs these guard were in the JavaScript text, not the server: the update form said "Found
WP5430 … Uploading opens an update" over an open review of WP5430, the toast told an approved
update the repository assigns its WPID, and a finished card's "23 h ago" never moved on. The
server-side tests pass whatever static/ says, so each function is lifted out of its file and run
under node against stubs.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _function(path: str, name: str) -> str:
    """The source of ``function <name>(…) { … }`` in a static file, by brace matching.

    The functions lifted here have no braces inside strings or regular expressions; the
    ``node --check``-style failure of a bad slice would show up as a SyntaxError below.
    """
    src = (ROOT / path).read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1


def _node(script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


# A wpidStatus element and document.createElement just big enough for describeOpenReview.
_DOM = """
function el(tag) {
  return { tag: tag, className: '', textContent: '', href: '', children: [],
           appendChild: function (c) { this.children.push(c); } };
}
var document = { createElement: el };
var wpidStatus;
function describe(info) {
  wpidStatus = el('span');
  describeOpenReview(info);
  return { cls: wpidStatus.className, text: wpidStatus.textContent,
           link: wpidStatus.children.length ? wpidStatus.children[0].href : null };
}
"""


def _describe(info: dict) -> dict:
    fn = _function("static/app.js", "describeOpenReview")
    call = f"process.stdout.write(JSON.stringify(describe({json.dumps(info)})));"
    return _node(f"{_DOM}\n{fn}\n{call}")


def _open_review(**kw) -> dict:
    o = {
        "pr_number": 93, "kind": "update", "status": "changes_requested",
        "status_label": "Changes requested", "yours": True, "submitter": "alice",
        "held_by": None, "outcome": "revise",
    }
    o.update(kw)
    return {"wpid": "WP5430", "state": "on_main", "open_review": o}


def test_the_update_form_says_the_upload_goes_onto_your_own_review():
    r = _describe(_open_review())
    assert r["text"] == (
        "Your update #93 is open (changes requested). Uploading adds a new version to it. "
    )
    assert "Uploading opens an update" not in r["text"]
    assert r["link"] == "/dashboard/93"


def test_the_update_form_says_to_wait_for_an_approved_review():
    r = _describe(_open_review(status="approved", status_label="Approved", yours=False,
                               submitter="bob", outcome="wait"))
    assert r["text"] == (
        "The update #93 of WP5430 by @bob is approved. An upload is refused until it is "
        "finished; edit WP5430 again after that. "
    )
    assert r["cls"].endswith("--err")


def test_the_update_form_names_who_holds_the_pathway():
    r = _describe(_open_review(yours=False, submitter="bob", held_by="bob", outcome="locked",
                               status="open", status_label="Open"))
    assert r["text"] == (
        "WP5430 is being edited by @bob (pull request #93, open). An upload is refused until "
        "that edit is published or closed; comment on its pull request to coordinate. "
    )
    # A lock with no pull request recorded yet: no number, no link.
    r = _describe(_open_review(pr_number=None, kind=None, status=None, status_label=None,
                               yours=False, submitter="bob", held_by="bob", outcome="locked"))
    assert r["text"] == (
        "WP5430 is being edited by @bob. An upload is refused until that edit is published or "
        "closed; try again after that."
    )
    assert r["link"] is None


def _approval(review, pipeline: bool) -> str:
    fn = _function("static/app.js", "approvalMessage")
    return _node(
        f"{fn}\nprocess.stdout.write(JSON.stringify("
        f"approvalMessage({json.dumps(review)}, {json.dumps(pipeline)})));"
    )


def test_the_approval_toast_names_an_updates_wpid():
    assert _approval({"kind": "update", "wpid": 5430}, True) == (
        "Approved. The repository publishes it to WP5430 from here."
    )
    assert _approval({"kind": "new", "wpid": None}, True) == (
        "Approved. The repository assigns the WPID and publishes it from here."
    )
    assert _approval(None, True) == (
        "Approved. The repository assigns the WPID and publishes it from here."
    )
    assert _approval({"kind": "update", "wpid": 5430}, False) == "Approved and merged."


def test_refresh_words_every_age_as_the_server_does():
    """A finished card is never polled and its Refresh gets a 304, so refresh.js is the only
    thing that moves its "Published 23 h ago" on; past a day it must not give up."""
    from datetime import UTC, datetime, timedelta

    from app.main import _ago

    fn = _function("static/refresh.js", "ago")
    now = datetime.now(UTC)
    ages = [timedelta(seconds=10), timedelta(minutes=4), timedelta(hours=23),
            timedelta(hours=25), timedelta(days=3), timedelta(days=13), timedelta(days=20),
            timedelta(days=69), timedelta(days=100)]
    stamps = [(now - a).isoformat() for a in ages]
    words = _node(
        f"{fn}\nprocess.stdout.write(JSON.stringify({json.dumps(stamps)}.map(ago)));"
    )
    assert words == [_ago(now - a) for a in ages]
    assert words[3] == "yesterday" and words[-1] == "3 months ago"


def test_a_submitted_update_looks_the_pathway_up_again():
    # Sandbox #94 (2026-11-20): right after the update opened, the line under the WPID still said
    # "Uploading opens an update" although the next upload would go to #94.
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    success = js.index("disarm(updateForm, btn, 'Submitted', 'Submit update');")
    after = js[success: success + 400]
    assert "wpidInput.dispatchEvent(new Event('blur'))" in after

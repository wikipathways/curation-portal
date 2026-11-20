"""The change summary's description wording and the interaction diff (sandbox PR #93).

On #93 an edit added NQO1 to WP5430 with an interaction from ARNT whose target end was not
attached, and changed the description by one word. The card said "Description: rewritten
(51 → 52 words)" while the quality rule said the description was essentially unchanged, and the
card counted "1 added" for the node with nothing about the new interaction. The fixture is that
edit's first version; the "before" is the same file minus NQO1 (d009), the interaction (i007) and
the one-word change, which is what WP5430 was on main.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.github import FakeGitHubClient
from app.models import Review, ReviewStatus
from app.preview import PreviewService
from app.preview.backfill import refresh_stale_renders
from app.preview.diff import (
    DIFF_VERSION,
    describe_description_change,
    describe_edge_changes,
    diff_edges,
    diff_metadata,
    diff_nodes,
)
from app.preview.metadata import parse_curation_metadata
from app.preview.render import render_gpml_full
from app.quality import description_change, inspect_gpml
from tests.test_api import _authed_app, _login, _settings

FIXTURES = Path(__file__).parent / "fixtures" / "gpml"
V1 = (FIXTURES / "b_wp5430_nqo1_v1.gpml").read_text(encoding="utf-8")


def _before(v1: str = V1) -> str:
    out = re.sub(r"\s*<!-- NQO1:.*?</DataNode>\n", "\n", v1, count=1, flags=re.S)
    out = re.sub(r'\s*<Interaction GraphId="i007">.*?</Interaction>\n', "\n", out, count=1,
                 flags=re.S)
    return out.replace("CYP1A1, CYP1B1 and NQO1", "CYP1A1 and CYP1B1")


BEFORE = _before()
# The fix that followed on #93: the dangling end attached to NQO1.
V2 = V1.replace(
    '<Point X="680.0" Y="280.0" ArrowHead',
    '<Point X="680.0" Y="293.0" GraphRef="d009" RelX="0.0" RelY="-1.0" ArrowHead',
)


def _diff(before: str, after: str) -> dict:
    b, a = render_gpml_full(before.encode()), render_gpml_full(after.encode())
    bn = [h.as_dict() for h in b.hotspots]
    an = [h.as_dict() for h in a.hotspots]
    nodes = diff_nodes(bn, an)
    return diff_edges(b.graph["edges"], a.graph["edges"], nodes, bn, an)


def test_the_fixture_pair_is_the_edit_on_93():
    assert "d009" not in BEFORE and "i007" not in BEFORE
    assert V2 != V1
    b, a = parse_curation_metadata(BEFORE.encode()), parse_curation_metadata(V1.encode())
    assert (len(b.description.split()), len(a.description.split())) == (51, 52)


# -- the description: one measurement, one vocabulary -------------------------------------------


def test_the_card_and_the_rule_agree_on_the_edit_from_93():
    before = parse_curation_metadata(BEFORE.encode())
    entry = diff_metadata(before, parse_curation_metadata(V1.encode()))["description"]
    assert entry["word_delta"] == 1 and entry["rewritten"] is False
    assert describe_description_change(entry) == "edited (+1 word)"

    finding = inspect_gpml(V1, before=before, kind="update").by_id("content.description")
    assert finding.severity == "pass"
    assert finding.detail == (
        f"The description changed slightly (1 word, {entry['char_delta']} characters), under the "
        "repository's limit of 3 words or 10 characters."
    )


def test_a_rewrite_past_the_limits_is_rewritten_on_both():
    before = parse_curation_metadata(BEFORE.encode())
    long = V1.replace(
        "in human cells.",
        "in human cells, and in several other mammalian tissues studied since the "
        "nineteen-nineties.",
    )
    entry = diff_metadata(before, parse_curation_metadata(long.encode()))["description"]
    assert entry["rewritten"] is True
    assert describe_description_change(entry) == f"rewritten (51 → {entry['to_words']} words)"
    finding = inspect_gpml(long, before=before, kind="update").by_id("content.description")
    assert finding.severity == "warn"
    assert finding.detail.startswith(
        f"The description changed by {entry['word_delta']} words and {entry['char_delta']} "
        "characters."
    )


def test_description_wording_below_and_above_the_limits():
    def says(was: str, now: str) -> str:
        c = description_change(was, now)
        entry = {
            "from_words": len(was.split()), "to_words": len(now.split()),
            "word_delta": c["word_delta"], "char_delta": c["char_delta"], "rewritten": c["over"],
        }
        return describe_description_change(entry)

    base = "one two three four five six seven eight nine ten"
    assert says(base, base + " eleven") == "edited (+1 word)"
    assert says(base, "one two three four five six seven") == "rewritten (10 → 7 words)"
    assert says(base + " a b c", base) == "edited (−3 words)"
    assert says(base, base.replace("one", "uno")) == "edited (same length, reworded)"
    assert says(base, base.replace("one", "ones")) == "edited (+1 character)"
    assert says("", base) == "added (10 words)"
    assert says(base, "") == "removed (was 10 words)"
    # A cache written before the deltas were recorded keeps its old wording.
    assert describe_description_change({"from_words": 51, "to_words": 52}) == (
        "rewritten (51 → 52 words)"
    )


def test_the_rule_says_unchanged_and_reworded_plainly():
    before = parse_curation_metadata(V1.encode())
    same = inspect_gpml(V1, before=before, kind="update").by_id("content.description")
    assert (same.severity, same.detail) == ("pass", "The description is unchanged.")
    reworded = V1.replace("binds the aryl", "binds the ARYL")
    finding = inspect_gpml(reworded, before=before, kind="update").by_id("content.description")
    assert finding.severity == "pass"
    assert finding.detail == (
        "The description was reworded at the same length, under the repository's limit of 3 "
        "words or 10 characters."
    )


def test_the_limits_come_from_the_rule():
    c = description_change("a", "a b c d")
    assert (c["max_words"], c["max_chars"], c["over"]) == (3, 10, False)
    assert description_change("a", "a b c d e")["over"] is True
    assert description_change("a", "a" * 12)["over"] is True


# -- interactions -------------------------------------------------------------------------------


def test_the_new_interaction_on_93_is_reported_as_added():
    d = _diff(BEFORE, V1)
    assert d["summary"] == {
        "added": 1, "removed": 0, "retyped": 0, "reconnected": 0, "unchanged": 6,
    }
    assert d["after"]["e-i007"] == {"change": "added"}
    assert "e-i007" not in d["before"]
    assert {"before": "e-i005", "after": "e-i005", "change": "unchanged"} in d["pairs"]
    assert describe_edge_changes(d["summary"]) == "1 added"


def test_attaching_the_dangling_end_is_a_reconnection():
    d = _diff(V1, V2)
    assert d["after"]["e-i007"] == {"change": "reconnected"}
    assert d["before"]["e-i007"] == "reconnected"
    assert d["summary"]["reconnected"] == 1 and d["summary"]["added"] == 0


def test_a_removed_interaction_and_a_retyped_one():
    retyped = V1.replace(
        'GraphRef="d008" RelX="0.0" RelY="-1.0" ArrowHead="mim-transcription-translation"',
        'GraphRef="d008" RelX="0.0" RelY="-1.0" ArrowHead="mim-inhibition"',
    )
    d = _diff(V1, retyped)
    assert d["after"]["e-i005"] == {
        "change": "retyped", "was": {"type_label": "transcription translation"},
    }
    assert d["summary"]["retyped"] == 1

    d = _diff(V1, BEFORE)
    assert d["before"]["e-i007"] == "removed" and d["summary"]["removed"] == 1


def test_an_interaction_whose_graph_id_was_rewritten_matches_by_its_ends():
    renamed = V1.replace('<Interaction GraphId="i003">', '<Interaction GraphId="x003">')
    d = _diff(V1, renamed)
    assert {"before": "e-i003", "after": "e-x003", "change": "unchanged"} in d["pairs"]
    assert d["summary"]["added"] == d["summary"]["removed"] == 0
    # Same two ends, a different arrow, and no GraphId to go by: still the same interaction.
    retyped = renamed.replace(
        'GraphRef="d006" RelX="0.0" RelY="-1.0" ArrowHead="mim-transcription-translation"',
        'GraphRef="d006" RelX="0.0" RelY="-1.0" ArrowHead="mim-catalysis"',
    )
    d = _diff(V1, retyped)
    assert d["after"]["e-x003"]["change"] == "retyped"


def test_a_node_renamed_under_a_new_graph_id_keeps_its_lines():
    # The node pairing is reused for the line ends: ARNT under a new GraphId is still ARNT.
    moved = V1.replace('GraphId="d005"', 'GraphId="z005"').replace(
        'GraphRef="d005"', 'GraphRef="z005"'
    )
    d = _diff(V1, moved)
    assert d["summary"]["reconnected"] == 0 and d["summary"]["unchanged"] == 7


def test_graphical_lines_are_not_compared():
    nodes = diff_nodes([], [])
    line = {"id": "e-g1", "graph_id": "g1", "kind": "GraphicalLine", "type": "line",
            "source": None, "target": None}
    d = diff_edges([], [line], nodes, [], [])
    assert d["summary"]["added"] == 0 and d["after"] == {}


# -- the cache: diff.json, the stamp, the backfill ---------------------------------------------


def test_diff_json_carries_edges_and_keeps_version_2(tmp_path):
    svc = PreviewService(cache_dir=tmp_path / "cache")
    svc.render_local(93, 5430, after_gpml=V1.encode(), before_gpml=BEFORE.encode())
    d = svc.diff(93)
    assert d["version"] == 2 and d["minor"] == 1
    assert d["edges"]["after"]["e-i007"] == {"change": "added"}
    assert d["summary"]["added"] == 1  # the node keys are unchanged
    assert d["meta"]["description"]["rewritten"] is False
    assert svc.stamp(93)["diff"] == DIFF_VERSION and not svc.needs_rerender(93)


def test_a_cache_compared_before_interactions_is_redrawn_from_disk(tmp_path):
    from sqlalchemy import create_engine

    from app.db import make_session_factory
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(Review(pr_number=93, wpid=5430, submitter="alice", kind="update",
                     status=ReviewStatus.OPEN, head_sha="h93"))
        s.commit()

    svc = PreviewService(cache_dir=tmp_path / "cache")
    svc.render_local(93, 5430, after_gpml=V1.encode(), before_gpml=BEFORE.encode(), head_sha="h93")
    d = tmp_path / "cache" / "93"
    # What a cache written before this looks like: no edges, a stamp without "diff".
    old = json.loads((d / "diff.json").read_text())
    for k in ("edges", "minor"):
        old.pop(k)
    (d / "diff.json").write_text(json.dumps(old))
    stamp = json.loads((d / "render.json").read_text())
    stamp.pop("diff")
    (d / "render.json").write_text(json.dumps(stamp))
    assert svc.needs_rerender(93)
    stale_rev = PreviewService._rev(d)

    # The backfill brings it up to date with no GitHub at all.
    assert refresh_stale_renders(svc, sf, None, content_repo="o/r", default_branch="main") == 1
    assert svc.diff(93)["edges"]["after"]["e-i007"] == {"change": "added"}
    # A new rev, so a browser holding the old diff.json under ?rev= fetches the new one.
    assert svc.stamp(93)["rev"] != stale_rev
    assert not svc.needs_rerender(93)


def _update_app(tmp_path, before: str):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    path = "pathways/WP5430/WP5430.gpml"
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={f"{repo}#{path}": "oldsha"},
        existing_contents={f"{repo}#{path}": before},
    )
    return _authed_app(tmp_path, curators=["curator"], fake=fake)


def test_the_card_names_the_interaction_and_the_small_description_edit(tmp_path):
    app, current = _update_app(tmp_path, BEFORE)
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5430/update",
            files={"file": ("v1.gpml", io.BytesIO(V1.encode()), "application/xml")},
        ).json()["pr_number"]
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text

    assert "<strong>Data nodes:</strong> 1 added</p>" in page
    assert re.search(
        r'<p class="diff-summary__line diff-summary__line--edges"><strong>Interactions:</strong> '
        r"1 added</p>",
        page,
    )
    assert "<strong>Description:</strong> edited (+1 word).</p>" in page
    assert "rewritten" not in page.split('data-tour="diff-summary"')[1].split("</div>")[0]


def test_the_card_says_nothing_about_interactions_for_a_cache_that_never_compared_them(tmp_path):
    app, current = _update_app(tmp_path, BEFORE)
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5430/update",
            files={"file": ("v1.gpml", io.BytesIO(V1.encode()), "application/xml")},
        ).json()["pr_number"]
        cache = app.state.preview
        path = cache._cache_dir / str(pr) / "diff.json"
        old = json.loads(path.read_text())
        old.pop("edges")
        old["meta"]["description"] = {"from_words": 51, "to_words": 52}
        path.write_text(json.dumps(old))
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text

    assert "<strong>Data nodes:</strong> 1 added</p>" in page
    assert "Interactions:</strong>" not in page
    assert "<strong>Description:</strong> rewritten (51 → 52 words).</p>" in page

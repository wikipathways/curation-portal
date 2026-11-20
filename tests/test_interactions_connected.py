"""Interactions attached at both ends: the rule, the checklist hint, and where they show.

The case that started it is sandbox PR #93 (2026-11-20): an update of WP5430 added NQO1 and an
arrow from ARNT whose far end lay on NQO1 in the drawing but named no ``GraphRef``. The pre-flight,
every quality rule and the repository's pipeline passed it; only the viewer's legend showed
"Show unconnected line ends (1)". ``wp5430_nqo1_dangling.gpml`` is that upload,
``wp5430_nqo1_fixed.gpml`` the corrected one.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.github import FakeGitHubClient
from app.preview.metadata import CurationMetadata, parse_curation_metadata
from app.preview.render import render_gpml_full
from app.quality import Severity, inspect_gpml, inspect_metadata
from app.review.checklist import build_checklist, hint_text
from tests.test_api import _authed_app, _login, _settings

FIXTURES = Path(__file__).parent / "fixtures" / "gpml"
DANGLING = (FIXTURES / "wp5430_nqo1_dangling.gpml").read_bytes()
FIXED = (FIXTURES / "wp5430_nqo1_fixed.gpml").read_bytes()
RULE = "content.interactions_connected"


def _before() -> bytes:
    """WP5430 as published before PR #93: the dangling version without NQO1 and its arrow."""
    text = DANGLING.decode()
    text = re.sub(r'\s*<DataNode TextLabel="NQO1".*?</DataNode>', "", text, flags=re.S)
    text = re.sub(r'\s*<Interaction GraphId="i007">.*?</Interaction>', "", text, flags=re.S)
    assert "NQO1\"" not in text and "i007" not in text
    return text.encode()


def _finding(gpml, **kw):
    return inspect_gpml(gpml, **kw).by_id(RULE)


def _gpml_2013a(body: str) -> str:
    return (
        '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Connection test pathway" '
        'Organism="Homo sapiens"><Graphics BoardWidth="800" BoardHeight="600"/>'
        '<DataNode TextLabel="A" GraphId="a" Type="GeneProduct" GroupRef="grp">'
        '<Graphics CenterX="100" CenterY="100" Width="80" Height="20" ZOrder="1"/>'
        '<Xref Database="Ensembl" ID="ENSG1"/></DataNode>'
        '<DataNode TextLabel="B" GraphId="b" Type="GeneProduct">'
        '<Graphics CenterX="400" CenterY="100" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG2"/></DataNode>'
        '<DataNode TextLabel="C" GraphId="c" Type="GeneProduct">'
        '<Graphics CenterX="250" CenterY="300" Width="80" Height="20"/>'
        '<Xref Database="Ensembl" ID="ENSG3"/></DataNode>'
        '<Group GroupId="grp" GraphId="g1" Style="Complex"/>'
        f"{body}</Pathway>"
    )


def _line(tag: str, gid: str, p1: str, p2: str, extra: str = "") -> str:
    return (
        f'<{tag} GraphId="{gid}"><Graphics LineThickness="1.0">'
        f"<Point {p1}/><Point {p2}/>{extra}</Graphics></{tag}>"
    )


# ---- the rule ----------------------------------------------------------------------------------


def test_the_pr93_upload_flags_one_loose_end_from_arnt_near_nqo1():
    f = _finding(DANGLING)
    assert f.severity == Severity.WARN.value
    assert f.detail.startswith("1 interaction end is not attached to anything: ")
    assert "the arrow from ARNT ends near NQO1" in f.detail
    meta = parse_curation_metadata(DANGLING)
    assert meta.interaction_count == 7
    assert [(e.interaction, e.end, e.attached, e.near) for e in meta.loose_ends] == [
        ("i007", "target", "ARNT", "NQO1")
    ]


def test_the_fixed_upload_passes():
    f = _finding(FIXED)
    assert (f.severity, f.detail) == (
        Severity.PASS.value,
        "All 7 interactions are attached at both ends.",
    )


#: Ends on things the renderer does not draw, which no fixture has: a data node, label or shape
#: without placeable ``<Graphics>``, a state without them, a state on such a node, an anchor on a
#: line too short to draw. (name, body, loose ends expected)
_UNDRAWN = [
    (
        "node without Graphics",
        '<DataNode TextLabel="D" GraphId="d" Type="GeneProduct"/>'
        + _line("Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="300" Y="50" GraphRef="d"'),
        1,
    ),
    (
        "label without a width",
        '<Label TextLabel="L" GraphId="l">'
        '<Graphics CenterX="300" CenterY="50" Height="20"/></Label>'
        + _line("Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="300" Y="50" GraphRef="l"'),
        1,
    ),
    (
        "states placed and not",
        '<State GraphRef="b" GraphId="s1" TextLabel="P"><Graphics RelX="1" RelY="0" Width="10" '
        'Height="10"/></State><State GraphRef="b" GraphId="s2" TextLabel="P"/>'
        '<State GraphRef="nowhere" GraphId="s3" TextLabel="P"><Graphics RelX="1" RelY="0" '
        'Width="10" Height="10"/></State>'
        + "".join(
            _line(
                "Interaction", f"i{k}", 'X="140" Y="100" GraphRef="a"',
                f'X="440" Y="100" GraphRef="s{k}"',
            )
            for k in (1, 2, 3)
        ),
        2,
    ),
    (
        "anchor on a one-point line",
        '<Interaction GraphId="short"><Graphics><Point X="1" Y="1"/>'
        '<Anchor GraphId="anc" Position="0.5"/></Graphics></Interaction>'
        + _line("Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="1" Y="1" GraphRef="anc"'),
        1,
    ),
]


def test_the_count_agrees_with_the_viewer_legend():
    # The viewer's "Show unconnected line ends (N)" reads the renderer's count; the rule must
    # never say a different number about the same file.
    cases = [
        (path.name, path.read_bytes(), None)
        for path in sorted(FIXTURES.glob("*.gpml"))
        if not path.name.startswith("hostile")
    ] + [(name, _gpml_2013a(body).encode(), n) for name, body, n in _UNDRAWN]
    for name, data, expected in cases:
        stats = render_gpml_full(data).graph["stats"]
        loose = len(parse_curation_metadata(data).loose_ends)
        assert loose == stats["dangling_ends"], name
        if expected is not None:
            assert loose == expected, name


def test_an_end_on_an_anchor_or_a_group_is_attached():
    anchored = _line(
        "Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="360" Y="100" GraphRef="b"',
        '<Anchor GraphId="anc" Position="0.5" Shape="Circle"/>',
    )
    to_anchor = _line(
        "Interaction", "i2", 'X="250" Y="290" GraphRef="c"', 'X="250" Y="100" GraphRef="anc"'
    )
    to_group = _line(
        "Interaction", "i3", 'X="400" Y="110" GraphRef="b"', 'X="100" Y="110" GraphRef="g1"'
    )
    f = _finding(_gpml_2013a(anchored + to_anchor + to_group))
    assert (f.severity, f.detail) == (
        Severity.PASS.value,
        "All 3 interactions are attached at both ends.",
    )


def test_a_reference_to_nothing_is_as_loose_as_no_reference():
    body = _line(
        "Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="380" Y="110" GraphRef="zz"'
    )
    f = _finding(_gpml_2013a(body))
    assert f.severity == Severity.WARN.value
    assert "the arrow from A ends near B" in f.detail


def test_the_free_start_of_an_arrow_is_named_by_where_it_points():
    body = _line(
        "Interaction", "i1", 'X="250" Y="280"', 'X="380" Y="100" GraphRef="b" ArrowHead="Arrow"'
    )
    f = _finding(_gpml_2013a(body))
    assert "the arrow to B starts near C" in f.detail


def test_an_unfinished_arrow_is_not_said_to_end_near_a_distant_node():
    # A short arrow out of A that stops right beside A: B, 210px off, is not where it points.
    stub = _line("Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="150" Y="100"')
    f = _finding(_gpml_2013a(stub))
    assert "the arrow from A ends on nothing" in f.detail
    assert "near B" not in f.detail
    # Dropped short of B's box by 25px is near it; by 45px is not.
    short = _line("Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="335" Y="100"')
    assert "the arrow from A ends near B" in _finding(_gpml_2013a(short)).detail
    shorter = _line("Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="315" Y="100"')
    assert "the arrow from A ends on nothing" in _finding(_gpml_2013a(shorter)).detail


_NOISY_LABEL = (
    '<Label TextLabel="Line one | col&#10;line two @octocat, and a good deal more besides" '
    'GraphId="lab"><Graphics CenterX="400" CenterY="400" Width="300" Height="60"/></Label>'
)


def test_a_label_is_named_on_one_line_and_cut_short():
    body = _NOISY_LABEL + _line(
        "Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="400" Y="400"'
    )
    (end,) = parse_curation_metadata(_gpml_2013a(body)).loose_ends
    assert end.near == "Line one | col line two @octocat, and a\u2026"
    assert len(end.near) <= 40


def test_a_label_in_the_hint_cannot_break_the_mirror_table_or_ping_anyone():
    from app.review.service import render_mirror_comment

    body = _NOISY_LABEL + _line(
        "Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="400" Y="400"'
    )
    meta = parse_curation_metadata(_gpml_2013a(body))

    class _Review:
        pr_number = 3
        wpid_str = "WP0001"
        kind = "new"
        submitter = "bob"
        assigned_curator = None
        submitter_note = ""
        approved_by = None
        decision_note = None
        checklist = build_checklist(metadata=meta, kind="new")

        class status:
            value = "open"

    _Review.checklist[[i["key"] for i in _Review.checklist].index("interactions_connected")][
        "note"
    ] = "Asked @octocat |\nabout it; mail a@b.org"
    comment = render_mirror_comment(
        _Review(), "wikipathways/sandbox-wp-db", quality=inspect_gpml(_gpml_2013a(body))
    )
    row = next(x for x in comment.splitlines() if x.startswith("| Interactions are connected"))
    assert "@octocat" not in comment
    assert "@\u200boctocat" in row
    assert "Line one \\| col line two" in row
    assert "Asked @\u200boctocat \\| about it; mail a@b.org" in row  # an address is not a mention
    # Header, rule and one row per item: the label opened no extra column or row.
    table = [x for x in comment.splitlines() if x.startswith("| ") and "Notes" not in x]
    cells = [len(re.split(r"(?<!\\)\|", x)) for x in table[: len(_Review.checklist)]]
    assert set(cells) == {5}


def test_an_arrow_attached_at_neither_end_and_the_count_beyond_three():
    lines = "".join(
        _line("Interaction", f"i{k}", f'X="{90 + k}" Y="120"', f'X="{390 + k}" Y="120"')
        for k in range(5)
    )
    f = _finding(_gpml_2013a(lines))
    assert f.detail.startswith("10 interaction ends are not attached to anything: ")
    assert "the arrow i0 is attached at neither end, between A and B" in f.detail
    assert "; and 2 more interactions." in f.detail


def test_a_graphical_line_with_loose_ends_is_not_checked():
    body = _line("GraphicalLine", "gl1", 'X="10" Y="10"', 'X="50" Y="50"')
    assert _finding(_gpml_2013a(body)).severity == Severity.NA.value
    body += _line(
        "Interaction", "i1", 'X="140" Y="100" GraphRef="a"', 'X="360" Y="100" GraphRef="b"'
    )
    assert _finding(_gpml_2013a(body)).severity == Severity.PASS.value


def test_no_interactions_is_nothing_to_check():
    f = _finding(_gpml_2013a(""))
    assert (f.severity, f.detail) == (Severity.NA.value, "No interactions in this pathway.")


_GPML_2021 = """<Pathway xmlns="http://pathvisio.org/GPML/2021" title="Connection test pathway"
    organism="Homo sapiens">
  <Graphics boardWidth="800" boardHeight="600"/>
  <DataNodes>
    <DataNode elementId="d5" textLabel="ARNT" type="Protein">
      <Xref dataSource="Uniprot-TrEMBL" identifier="P27540"/>
      <Graphics centerX="560" centerY="230" width="120" height="34"/>
    </DataNode>
    <DataNode elementId="d9" textLabel="NQO1" type="GeneProduct">
      <Xref dataSource="Ensembl" identifier="ENSG00000181019"/>
      <Graphics centerX="680" centerY="310" width="120" height="34"/>
    </DataNode>
    <DataNode elementId="d6" textLabel="CYP1A1" type="GeneProduct" groupRef="grp">
      <Xref dataSource="Ensembl" identifier="ENSG00000140465"/>
      <Graphics centerX="480" centerY="400" width="120" height="34"/>
    </DataNode>
  </DataNodes>
  <Interactions>
    <Interaction elementId="i007">
      <Waypoints>
        <Point elementId="p1" x="620" y="230" elementRef="d5" relX="1.0" relY="0.0"/>
        <Point elementId="p2" x="680" y="280" {end}arrowHead="TranscriptionTranslation"/>
        <Anchor elementId="anc" position="0.5" shapeType="None"/>
      </Waypoints>
      <Graphics lineWidth="1.0"/>
    </Interaction>
    <Interaction elementId="i008">
      <Waypoints>
        <Point elementId="p3" x="480" y="383" elementRef="grp"/>
        <Point elementId="p4" x="650" y="255" elementRef="anc"/>
      </Waypoints>
      <Graphics lineWidth="1.0"/>
    </Interaction>
  </Interactions>
  <GraphicalLines>
    <GraphicalLine elementId="gl">
      <Waypoints><Point elementId="p5" x="1" y="1"/><Point elementId="p6" x="9" y="9"/></Waypoints>
    </GraphicalLine>
  </GraphicalLines>
  <Groups><Group elementId="grp" type="Complex"/></Groups>
</Pathway>
"""


def test_gpml2021_reads_the_same_way():
    # elementRef on a <Point> under <Waypoints>; anchors and groups named by elementId. A point's
    # own elementId ("p2") is not something an end can attach to.
    loose = _GPML_2021.format(end="")
    f = _finding(loose)
    assert f.severity == Severity.WARN.value
    assert f.detail.startswith("1 interaction end is not attached to anything: ")
    assert "the arrow from ARNT ends near NQO1" in f.detail
    on_point = _GPML_2021.format(end='elementRef="p1" ')
    assert _finding(on_point).severity == Severity.WARN.value
    fixed = _GPML_2021.format(end='elementRef="d9" ')
    assert _finding(fixed).detail == "All 2 interactions are attached at both ends."
    assert render_gpml_full(loose.encode()).graph["stats"]["dangling_ends"] == 1


def test_an_update_is_graded_on_the_loose_ends_it_adds():
    before = parse_curation_metadata(_before())
    assert before.loose_ends == []
    f = _finding(DANGLING, kind="update", before=before)
    assert f.severity == Severity.WARN.value
    assert "the arrow from ARNT ends near NQO1" in f.detail
    assert "already loose" not in f.detail


def test_a_loose_end_the_pathway_already_had_passes_but_is_mentioned():
    f = _finding(DANGLING, kind="update", before=parse_curation_metadata(DANGLING))
    assert f.severity == Severity.PASS.value
    assert f.detail.startswith("This edit leaves no new loose interaction ends. ")
    assert "1 interaction end is already loose in the version this edit replaces" in f.detail
    assert "the arrow from ARNT ends near NQO1" in f.detail


def test_metadata_cached_before_the_count_says_nothing():
    # A blob without the count is not "no loose ends": nobody looked.
    assert inspect_metadata(CurationMetadata(name="x")).by_id(RULE) is None


# ---- the checklist -----------------------------------------------------------------------------


def _item(checklist, key="interactions_connected"):
    return next(i for i in checklist if i["key"] == key)


def test_the_checklist_hint_suggests_fail_with_the_finding():
    item = _item(build_checklist(metadata=parse_curation_metadata(DANGLING), kind="new"))
    assert item["state"] == "pending"  # a hint, never an answer
    assert item["suggested"] == "fail"
    assert "the arrow from ARNT ends near NQO1" in item["hint"]
    assert hint_text(item).endswith("Suggests Fail.")


def test_the_checklist_hint_suggests_pass_when_all_are_attached():
    item = _item(build_checklist(metadata=parse_curation_metadata(FIXED), kind="new"))
    assert (item["suggested"], item["hint"]) == (
        "pass",
        "All 7 interactions are attached at both ends.",
    )


def test_the_hint_on_an_update_follows_the_edit_and_pipeline_mode_keeps_it():
    loose = parse_curation_metadata(DANGLING)
    added = _item(
        build_checklist(
            metadata=loose,
            before=parse_curation_metadata(_before()),
            kind="update",
            pipeline_mode=True,
        )
    )
    assert added["suggested"] == "fail"
    inherited = _item(
        build_checklist(metadata=loose, before=loose, kind="update", pipeline_mode=True)
    )
    assert inherited["suggested"] == "pass"
    assert "already loose" in inherited["hint"]


# ---- where it shows ----------------------------------------------------------------------------


def _update_app(tmp_path, base: bytes | None = None):
    settings = _settings(database_url=f"sqlite:///{tmp_path / 'reg.db'}")
    repo, branch = settings.content_repo, settings.default_branch
    path = f"{repo}#pathways/WP5430/WP5430.gpml"
    fake = FakeGitHubClient(
        default_branches={f"{repo}#{branch}": "basesha"},
        existing_files={path: "oldsha"},
        existing_contents={path: (base or _before()).decode()},
    )
    return _authed_app(tmp_path, curators=["curator"], fake=fake)


def _upload(c, current, gpml: bytes) -> int:
    current["user"] = "alice"
    r = c.post(
        "/api/pathways/5430/update",
        files={"file": ("rev.gpml", io.BytesIO(gpml), "application/xml")},
    )
    assert r.status_code == 201, r.text
    return r.json()["pr_number"]


def _ends_line(page: str) -> str:
    page = re.sub(r"\s+", " ", page)
    m = re.search(r"<strong>Unconnected line ends:</strong>(.*?)<button", page)
    assert m, "no 'Unconnected line ends' line on the card"
    return m.group(1).strip()


def test_the_preflight_shows_it_as_worth_a_look(tmp_path):
    # static/app.js lists every warn, fail and block finding; warn reads "Worth a look".
    app, _ = _authed_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/validate",
            files={"file": ("p.gpml", io.BytesIO(DANGLING), "application/xml")},
            data={"wpid": "WP5430"},
        )
    assert r.status_code == 200
    f = next(x for x in r.json()["quality"]["findings"] if x["id"] == RULE)
    assert f["severity"] == "warn"
    assert "the arrow from ARNT ends near NQO1" in f["detail"]
    # The form has the file but not the published version, which the review will compare
    # against: it says so rather than contradict the review over ends that were there before.
    assert f["detail"].endswith(
        "Without the published version to compare against, this check cannot tell which were "
        "there before; only ends this edit adds count."
    )


def test_the_preflight_for_a_new_pathway_does_not_mention_a_published_version(tmp_path):
    app, _ = _authed_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/validate", files={"file": ("p.gpml", io.BytesIO(DANGLING), "application/xml")}
        )
    f = next(x for x in r.json()["quality"]["findings"] if x["id"] == RULE)
    assert f["severity"] == "warn"
    assert "published version" not in f["detail"]


def test_an_update_card_counts_the_loose_ends_and_offers_to_show_them(tmp_path):
    app, current = _update_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "alice"
        r = c.post(
            "/api/pathways/5430/update",
            files={"file": ("rev.gpml", io.BytesIO(DANGLING), "application/xml")},
        )
        assert r.status_code == 201, r.text
        pr = r.json()["pr_number"]
        _login(c, "curator")
        page = re.sub(r"\s+", " ", c.get(f"/dashboard/{pr}").text)
        review = c.get(f"/api/reviews/{pr}").json()

    assert _ends_line(page) == "1"  # new, so nothing to add
    assert f'data-show-ends="viewer-{pr}-after"' in page
    assert 'aria-label="Show them: the unconnected line ends on the new version"' in page
    assert f'id="viewer-{pr}-after"' in page
    # The checklist the curator works from carries the same finding, as a suggestion.
    item = _item(review["checklist"])
    assert (item["state"], item["suggested"]) == ("pending", "fail")
    assert "the arrow from ARNT ends near NQO1" in item["hint"]


def test_a_card_with_every_end_attached_has_no_such_line(tmp_path):
    app, current = _update_app(tmp_path)
    with TestClient(app) as c:
        current["user"] = "alice"
        pr = c.post(
            "/api/pathways/5430/update",
            files={"file": ("rev.gpml", io.BytesIO(FIXED), "application/xml")},
        ).json()["pr_number"]
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
    assert "diff-summary" in page
    assert "Unconnected line ends" not in page and "data-show-ends" not in page


def test_the_viewer_exposes_show_ends_without_inline_handlers():
    js = (Path(__file__).parent.parent / "static" / "viewer.js").read_text(encoding="utf-8")
    assert "showEnds: showEnds" in js
    assert "closest('[data-show-ends]')" in js
    card = (Path(__file__).parent.parent / "templates" / "_review_card.html").read_text()
    line = next(x for x in card.splitlines() if "data-show-ends" in x)
    assert not re.search(r"\son[a-z]+=", line)


def test_an_update_card_says_when_the_loose_ends_were_there_before(tmp_path):
    # The published version already has the loose ARNT arrow and the edit leaves it: the
    # checklist suggests Pass, and the card must not read as if this edit made it.
    app, current = _update_app(tmp_path, base=DANGLING)
    with TestClient(app) as c:
        pr = _upload(c, current, DANGLING)
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
        review = c.get(f"/api/reviews/{pr}").json()
    assert _ends_line(page) == "1 (it was already in the published version)"
    assert _item(review["checklist"])["suggested"] == "pass"


def test_an_update_card_separates_new_loose_ends_from_old_ones(tmp_path):
    # The edit keeps the old loose arrow and detaches CYP1A1 from i003: one new, one old.
    detached = DANGLING.replace(
        b'GraphRef="d006" RelX="0.0" RelY="-1.0"', b'RelX="0.0" RelY="-1.0"'
    )
    assert detached != DANGLING
    app, current = _update_app(tmp_path, base=DANGLING)
    with TestClient(app) as c:
        pr = _upload(c, current, detached)
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
    assert _ends_line(page) == "2 (1 new, 1 already in the published version)"


def test_a_re_upload_re_derives_the_hint_and_keeps_the_curators_answer(tmp_path):
    # The update route reuses the pull request, so `register` re-merges the checklist with the
    # base metadata it re-reads: the fix must turn the suggestion round, and what the curator
    # answered by hand must survive it.
    app, current = _update_app(tmp_path)
    with TestClient(app) as c:
        pr = _upload(c, current, DANGLING)
        first = _item(c.get(f"/api/reviews/{pr}").json()["checklist"])
        assert (first["state"], first["suggested"]) == ("pending", "fail")
        current["user"] = "curator"
        r = c.post(
            f"/api/reviews/{pr}/checklist",
            data={"key": "interactions_connected", "state": "fail", "note": "NQO1 arrow loose"},
        )
        assert r.status_code == 200, r.text

        assert _upload(c, current, FIXED) == pr
        after = _item(c.get(f"/api/reviews/{pr}").json()["checklist"])
        _login(c, "curator")
        page = c.get(f"/dashboard/{pr}").text
    assert after["suggested"] == "pass"
    assert after["hint"] == "All 7 interactions are attached at both ends."
    assert (after["state"], after["note"]) == ("fail", "NQO1 arrow loose")
    assert "Unconnected line ends" not in page


def test_an_adopted_pull_request_gets_the_hint_and_re_derives_it_on_a_new_commit(
    session_factory, tmp_path
):
    from app.curators import ConfigCurators
    from app.preview import PreviewService
    from app.review.adoption import AdoptionService
    from app.review.service import CurationService

    repo, path = "wikipathways/sandbox-wp-db", "pathways/WP5430/WP5430.gpml"
    gh = FakeGitHubClient()
    gh.existing_contents[(repo, path)] = _before().decode()
    gh.seed_foreign_pr(
        repo,
        93,
        author="contributor",
        title="Contribution: Update WP5430",
        head_branch="WP5430_contributor_20261120-101500",
        head_sha="a" * 40,
        files=[(path, "modified", DANGLING.decode())],
    )
    previews = PreviewService(cache_dir=tmp_path / "preview-cache")
    curation = CurationService(
        session_factory,
        gh,
        repo=repo,
        curators=ConfigCurators({"curator"}),
        previews=previews,
        publish_mode="pipeline",
    )
    svc = AdoptionService(
        github=gh,
        previews=previews,
        curation=curation,
        content_repo=repo,
        default_branch="main",
    )

    assert svc.adopt(93).adopted
    item = _item(curation.get(93).checklist)
    assert (item["state"], item["suggested"]) == ("pending", "fail")
    assert "the arrow from ARNT ends near NQO1" in item["hint"]
    curation.set_checklist_item(93, "interactions_connected", "fail", "the NQO1 arrow")

    gh.pull_meta[93]["head_sha"] = "b" * 40
    gh.commit_contents[(repo, "b" * 40, path)] = FIXED.decode()
    assert svc.adopt(93).refreshed
    item = _item(curation.get(93).checklist)
    assert item["suggested"] == "pass"
    assert (item["state"], item["note"]) == ("fail", "the NQO1 arrow")

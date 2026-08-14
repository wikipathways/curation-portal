from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.preview.metadata import parse_curation_metadata
from app.quality import (
    Severity,
    blocking_reasons,
    checklist_result,
    inspect_gpml,
    inspect_metadata,
)
from app.quality.report import QualityReport, worst
from app.submit.gpml import InvalidGpml, validate_gpml

GOOD = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Insulin signalling in adipocytes"
         Organism="Homo sapiens" Author="[marvin]">
  <Graphics BoardWidth="480.0" BoardHeight="440.0" />
  <Comment Source="WikiPathways-description">A reasonably long description of the insulin
  signalling cascade as it runs in adipose tissue, written out so that it clears the fifteen
  word threshold the repository asks every new pathway to meet.</Comment>
  <DataNode TextLabel="INSR" Type="GeneProduct" GraphId="a1">
    <Xref Database="Ensembl" ID="ENSG00000171105"/></DataNode>
  <DataNode TextLabel="AKT1" Type="GeneProduct" GraphId="b2">
    <Xref Database="Ensembl" ID="ENSG00000142208"/></DataNode>
  <OntologyTerm Term="insulin signaling pathway" ID="PW:0000143" Ontology="Pathway Ontology"/>
  <BiopaxRef>ref1</BiopaxRef>
  <Biopax><bp:PublicationXref xmlns:bp="http://www.biopax.org/release/biopax-level3.owl#"
      xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" rdf:id="ref1">
    <bp:ID>12345678</bp:ID><bp:DB>PubMed</bp:DB>
    <bp:TITLE>Insulin signalling in adipose tissue</bp:TITLE>
  </bp:PublicationXref></Biopax>
</Pathway>
"""

# Short title, no description, no Author, one empty citation id, one unannotated node.
POOR = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Short" Organism="Homo sapiens">
  <DataNode TextLabel="MysteryGene" Type="GeneProduct"><Xref Database="" ID=""/></DataNode>
  <Biopax><bp:PublicationXref xmlns:bp="http://www.biopax.org/release/biopax-level3.owl#">
    <bp:ID></bp:ID></bp:PublicationXref></Biopax>
</Pathway>
"""

NOT_GPML = "<html><body>definitely not a pathway</body></html>"


def _severity(report: QualityReport, rule_id: str) -> str:
    finding = report.by_id(rule_id)
    assert finding is not None, f"{rule_id} emitted no finding"
    return finding.severity


# ---- the blocking subset --------------------------------------------------------------------


def test_the_four_blocking_reasons_are_word_for_word_what_validate_gpml_raises():
    """The strings reach a submitter through describeError, so they are interface, not prose."""
    missing_everything = '<?xml version="1.0"?><Pathway xmlns="http://pathvisio.org/GPML/2013a"/>'
    assert blocking_reasons(missing_everything) == [
        "root <Pathway> has no Name",
        "root <Pathway> has no Organism (required for metadata + render)",
    ]
    with pytest.raises(InvalidGpml) as exc:
        validate_gpml(missing_everything)
    assert exc.value.reasons == blocking_reasons(missing_everything)


def test_a_document_with_no_pathway_root_reports_only_that():
    """Everything downstream would be reporting on a document that is not a pathway."""
    reasons = blocking_reasons(NOT_GPML)
    assert reasons == ["no <Pathway> root element found"]
    report = inspect_gpml(NOT_GPML)
    assert [f.id for f in report.findings] == ["gpml.root"]


def test_a_good_pathway_blocks_on_nothing():
    assert blocking_reasons(GOOD) == []
    assert validate_gpml(GOOD).author == "[marvin]"


# ---- the checks ported from mvp1 -------------------------------------------------------------


def test_an_empty_citation_id_warns_because_it_kills_the_metadata_generator():
    assert _severity(inspect_gpml(POOR), "gpml.citation_ids") == Severity.WARN.value
    assert _severity(inspect_gpml(GOOD), "gpml.citation_ids") == Severity.PASS.value


def test_a_pathway_with_no_data_nodes_fails_without_being_refused():
    empty = (
        '<?xml version="1.0"?><Pathway xmlns="http://pathvisio.org/GPML/2013a"'
        ' Name="Empty pathway" Organism="Homo sapiens"/>'
    )
    report = inspect_gpml(empty)
    assert _severity(report, "gpml.datanodes") == Severity.FAIL.value
    # Graded, not refused: the submission still opens a pull request and a curator decides.
    assert report.blocking_reasons == []


def test_a_missing_author_warns_even_though_the_portal_repairs_it():
    assert _severity(inspect_gpml(POOR), "gpml.author") == Severity.WARN.value
    assert _severity(inspect_gpml(GOOD), "gpml.author") == Severity.PASS.value


def test_an_unannotated_data_node_fails():
    finding = inspect_gpml(POOR).by_id("content.datanode_annotation")
    assert finding.severity == Severity.FAIL.value
    assert "MysteryGene" in finding.detail


# ---- the checks ported from the repository's own testing job ---------------------------------


def test_a_short_title_predicts_the_repositorys_review_required():
    finding = inspect_gpml(POOR).by_id("content.title_length")
    assert finding.severity == Severity.WARN.value
    assert finding.predicts_repo is True
    assert _severity(inspect_gpml(GOOD), "content.title_length") == Severity.PASS.value


def test_a_new_pathway_needs_fifteen_words_of_description():
    thin = GOOD.replace(
        GOOD[GOOD.index("A reasonably") : GOOD.index("</Comment>")], "Too short."
    )
    assert _severity(inspect_gpml(thin), "content.description") == Severity.WARN.value
    assert _severity(inspect_gpml(GOOD), "content.description") == Severity.PASS.value


def test_a_pathway_with_no_description_at_all_fails():
    assert _severity(inspect_gpml(POOR), "content.description") == Severity.FAIL.value


def test_an_edit_that_rewrites_the_description_is_flagged():
    """On an edit the repository measures change, not length — the opposite test."""
    before = parse_curation_metadata(GOOD)
    rewritten = GOOD.replace(
        "word threshold the repository asks every new pathway to meet.",
        "word threshold, and then some entirely different subject matter nobody mentioned before.",
    )
    report = inspect_gpml(rewritten, before=before, kind="update")
    assert _severity(report, "content.description") == Severity.WARN.value


def test_an_edit_that_leaves_the_description_alone_passes():
    before = parse_curation_metadata(GOOD)
    report = inspect_gpml(GOOD, before=before, kind="update")
    assert _severity(report, "content.description") == Severity.PASS.value


def test_data_node_changes_are_classified_by_the_existing_diff():
    before = parse_curation_metadata(GOOD)
    dropped = GOOD.replace(
        '<DataNode TextLabel="AKT1" Type="GeneProduct" GraphId="b2">\n'
        '    <Xref Database="Ensembl" ID="ENSG00000142208"/></DataNode>',
        "",
    )
    report = inspect_gpml(dropped, before=before, kind="update")
    finding = report.by_id("content.datanode_changes")
    assert finding.severity == Severity.WARN.value
    assert "removed" in finding.detail


def test_an_update_that_touches_no_data_node_says_so():
    before = parse_curation_metadata(GOOD)
    report = inspect_gpml(GOOD, before=before, kind="update")
    assert _severity(report, "content.datanode_changes") == Severity.PASS.value


def test_a_new_pathway_has_nothing_to_diff_but_still_says_what_the_repo_will_report():
    finding = inspect_gpml(GOOD).by_id("content.datanode_changes")
    assert finding.severity == Severity.NA.value
    assert "review-required" in finding.detail


# ---- the render rule -------------------------------------------------------------------------


def test_the_render_rule_says_nothing_when_nobody_asked_the_renderer():
    assert inspect_gpml(GOOD).by_id("render.drawable") is None


def test_a_pathway_the_renderer_refused_is_reported_as_such():
    assert _severity(inspect_gpml(GOOD, drawable=False), "render.drawable") == Severity.FAIL.value
    assert _severity(inspect_gpml(GOOD, drawable=True), "render.drawable") == Severity.PASS.value


# ---- the rollup ------------------------------------------------------------------------------


def test_the_report_takes_the_worst_finding_as_its_status():
    assert inspect_gpml(POOR).status == Severity.FAIL.value
    assert inspect_gpml(GOOD).status == Severity.PASS.value
    assert inspect_gpml(NOT_GPML).status == Severity.BLOCK.value


def test_nothing_to_check_never_wins_the_rollup():
    """An all-na report is the report saying it has no complaint, not saying nothing."""
    assert worst([Severity.NA.value, Severity.NA.value]) == Severity.PASS.value
    assert worst([Severity.NA.value, Severity.WARN.value]) == Severity.WARN.value
    assert worst([]) == Severity.PASS.value


def test_the_markdown_table_names_every_finding():
    table = inspect_gpml(POOR).to_markdown()
    assert "| Status | Check | Detail |" in table
    assert "Title length" in table
    assert "**Overall: FAIL**" in table


def test_a_report_survives_a_round_trip_through_the_cache():
    report = inspect_gpml(POOR)
    assert QualityReport.from_dict(report.as_dict()) == report


def test_a_sidecar_written_before_quality_existed_reads_as_nothing():
    assert QualityReport.from_dict({}) is None
    assert QualityReport.from_dict(None) is None


# ---- the metadata-only subset ----------------------------------------------------------------


def test_rules_that_need_the_document_are_skipped_rather_than_answered_without_it():
    """With no text in hand, "no empty citation ids" would be a finding nobody looked for."""
    report = inspect_metadata(parse_curation_metadata(POOR))
    assert report.by_id("gpml.citation_ids") is None
    assert report.by_id("gpml.author") is None
    # What metadata alone can answer is still answered.
    assert report.by_id("content.datanode_annotation") is not None


def test_checklist_result_returns_none_for_an_item_no_rule_answers():
    assert checklist_result("render_ok", parse_curation_metadata(GOOD)) is None


# ---- the import constraint -------------------------------------------------------------------


def test_quality_imports_no_app_package_at_module_scope():
    """The cycle this guards: app.models → app.review.checklist → app.quality → app.submit →
    app.wpid → app.models. It surfaces as an ImportError at startup, not in a test, so the
    invariant is pinned here rather than discovered on a deploy."""
    package = Path(__file__).resolve().parents[1] / "app" / "quality"
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only — function-local imports are the escape hatch
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app."):
                if not (node.module or "").startswith("app.quality"):
                    offenders.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.") and not alias.name.startswith("app.quality"):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert offenders == [], offenders


def test_a_pathway_with_no_canvas_fails_because_the_repository_cannot_read_it():
    """Measured, not reasoned: two runs of the same pathway differing only in this element.

    Without it, run 30798868327's `metadata` job died with a NullPointerException out of
    GPML2013aReader.readPathway; with it, run 30800359486 succeeded. The portal's own renderer
    draws either one, which is why nothing caught it before.
    """
    finding = inspect_gpml(POOR).by_id("gpml.board")
    assert finding.severity == Severity.FAIL.value
    assert "BoardWidth" in finding.detail

    with_board = POOR.replace(
        'Organism="Homo sapiens">',
        'Organism="Homo sapiens">\n  <Graphics BoardWidth="480.0" BoardHeight="440.0" />',
    )
    assert _severity(inspect_gpml(with_board), "gpml.board") == Severity.PASS.value


def test_the_canvas_check_needs_the_document_so_metadata_alone_stays_quiet():
    """Otherwise every checklist auto-check would report a missing board it never looked for."""
    assert inspect_metadata(parse_curation_metadata(POOR)).by_id("gpml.board") is None


# ---- issue #26: interactions with no LineThickness -------------------------------------------

_INTERACTIONS = """<?xml version="1.0" encoding="UTF-8"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Lines" Organism="Homo sapiens">
  <Graphics BoardWidth="480.0" BoardHeight="440.0" />
  <Interaction GraphId="i1">
    <Graphics{thickness}>
      <Point X="240.0" Y="97.0" /><Point X="240.0" Y="183.0" ArrowHead="Arrow" />
    </Graphics>
  </Interaction>
</Pathway>
"""


def test_an_interaction_with_no_line_thickness_fails():
    """Measured the same way as the canvas rule: runs 30827814897 (absent, `metadata` died in
    readLineStyleProperty) and 30829825691 (present, all ten jobs green), one variable apart."""
    finding = inspect_gpml(_INTERACTIONS.format(thickness="")).by_id("gpml.line_thickness")
    assert finding.severity == Severity.FAIL.value
    assert "LineThickness" in finding.detail
    assert "Interaction i1" in finding.detail  # names the offender, not just a count

    good = _INTERACTIONS.format(thickness=' LineThickness="1.0"')
    assert _severity(inspect_gpml(good), "gpml.line_thickness") == Severity.PASS.value


def test_a_graphical_line_is_checked_too():
    """`readLineElement` is shared, so the same missing attribute reaches the same crash."""
    text = _INTERACTIONS.format(thickness="").replace("Interaction", "GraphicalLine")
    finding = inspect_gpml(text).by_id("gpml.line_thickness")
    assert finding.severity == Severity.FAIL.value
    assert "GraphicalLine i1" in finding.detail


def test_a_pathway_with_no_interactions_at_all_passes_the_line_check():
    """`na` would be defensible, but the rollup ranks it below pass and this is genuinely fine."""
    assert _severity(inspect_gpml(GOOD), "gpml.line_thickness") == Severity.PASS.value


def test_the_demo_fixtures_would_survive_the_repositorys_metadata_job():
    """The three walkthrough files all carried this defect (issue #26), and the walkthrough is
    the thing most likely to be run against a real pipeline by someone learning the portal."""
    demo = Path(__file__).resolve().parents[1] / "demo"
    for name in ("pathway_new.gpml", "pathway_revised.gpml", "pathway_update.gpml"):
        report = inspect_gpml((demo / name).read_text(encoding="utf-8"))
        assert _severity(report, "gpml.line_thickness") == Severity.PASS.value, name
        assert _severity(report, "gpml.board") == Severity.PASS.value, name


def test_a_reference_nothing_cites_does_not_count_as_a_reference():
    """A `<bp:PublicationXref>` with no `<BiopaxRef>` pointing at it reaches nothing downstream.

    PathVisio leaves these behind when an annotation is removed, and a hand-written file can
    simply forget the citation. Every generator in the target repository emits only the cited
    ones, so `refs.tsv` and `bibliography.tsv` come back empty — while this rule announced "1
    reference" and the checklist beside it, which has read the cited count since the pipeline
    check was wired, said the file declares none. Both on the same review page, disagreeing.
    Measured on PR #42, 2026-08-14.
    """
    orphaned = GOOD.replace("<BiopaxRef>ref1</BiopaxRef>", "")
    finding = inspect_gpml(orphaned).by_id("content.references")
    assert finding.severity == Severity.WARN.value
    # And it says *why*, because "no references" on a file that visibly contains one reads as
    # a portal bug rather than as the thing the submitter needs to fix.
    assert "no <BiopaxRef> cites" in finding.detail


def test_a_cited_reference_still_passes():
    """The guard against fixing the above by making the rule never pass."""
    assert _severity(inspect_gpml(GOOD), "content.references") == Severity.PASS.value

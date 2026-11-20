from __future__ import annotations

from app.preview.metadata import parse_curation_metadata
from app.review.checklist import (
    build_checklist,
    default_checklist,
    hint_text,
    is_complete,
    requirement_for,
)

# Two mapped data nodes, a title + description, an ontology tag, no references.
MAPPED = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <Comment>A demo pathway.</Comment>
  <DataNode TextLabel="INSR" Type="GeneProduct">
    <Xref Database="Ensembl" ID="ENSG00000171105"/></DataNode>
  <DataNode TextLabel="AKT1" Type="GeneProduct">
    <Xref Database="Ensembl" ID="ENSG00000142208"/></DataNode>
  <OntologyTerm Term="insulin signaling pathway" ID="PW:0000143" Ontology="Pathway Ontology"/>
</Pathway>
"""

# One node with no identifier.
UNMAPPED = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <DataNode TextLabel="MysteryGene" Type="GeneProduct"><Xref Database="" ID=""/></DataNode>
</Pathway>
"""


def _item(checklist, key):
    return next(i for i in checklist if i["key"] == key)


def _unanswered(item):
    """Every item starts like this: nobody has answered it, and the app never does."""
    return item["state"] == "pending" and item["auto"] is False and item["note"] == ""


def test_every_item_starts_pending_with_hints():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert all(_unanswered(i) for i in cl)
    # The automated findings are kept as suggestions beside the items.
    assert _item(cl, "datanodes_mapped")["suggested"] == "pass"
    assert _item(cl, "naming_ok")["suggested"] == "pass"
    onto = _item(cl, "ontology_tags")
    assert (onto["suggested"], onto["hint"]) == ("pass", "1 ontology tag.")
    # "Meaningful" stays a human judgement, so the description check never suggests pass — but it
    # does run, and its hint says how the description measures against what the repository asks.
    assert _item(cl, "description_ok")["suggested"] == "pending"
    assert _item(cl, "description_ok")["hint"]
    # "Interactions are connected" and the render cannot be read from parsed annotation: no hint.
    for key in ("interactions_connected", "render_ok"):
        assert (_item(cl, key)["suggested"], _item(cl, key)["hint"]) == (None, "")
    # No references suggests pending, not na (issue #27): the repository asks for at least one.
    refs = _item(cl, "references_valid")
    assert refs["suggested"] == "pending"
    assert refs["required"] is True
    assert "at least one" in refs["hint"]


def test_hint_text_names_the_finding_and_the_suggestion():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert hint_text(_item(cl, "ontology_tags")) == (
        "Automated check: 1 ontology tag. Suggests Pass."
    )
    # A pending suggestion is information without a verdict.
    assert "Suggests" not in hint_text(_item(cl, "description_ok"))
    assert hint_text(_item(cl, "description_ok")).startswith("Automated check: ")
    assert hint_text(_item(cl, "render_ok")) == ""


def test_well_annotated_submission_still_needs_every_answer():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert is_complete(cl) is False
    # A curator answers every required item, including the ones the automation would pass.
    for item in cl:
        if item["required"]:
            item["state"] = "pass"
    assert is_complete(cl) is True


def test_unmapped_datanode_suggests_fail_with_hint():
    cl = build_checklist(metadata=parse_curation_metadata(UNMAPPED), kind="new")
    item = _item(cl, "datanodes_mapped")
    assert _unanswered(item)
    assert item["suggested"] == "fail"
    assert "no identifier" in item["hint"]
    assert "MysteryGene" in item["hint"]
    assert hint_text(item).endswith("Suggests Fail.")


def test_default_checklist_is_all_pending():
    cl = default_checklist()
    assert all(_unanswered(i) for i in cl)
    # `one_pathway_per_pr` is derived from the pull request's file list, not from the GPML, so a
    # template built with no inputs has nothing to check: it suggests N/A, and stays required
    # until a curator agrees.
    one = _item(cl, "one_pathway_per_pr")
    assert (one["suggested"], one["required"]) == ("na", True)


def test_update_scoping_only_suggests_na():
    before = parse_curation_metadata(MAPPED)
    after = parse_curation_metadata(MAPPED)  # identical → nothing changed
    cl = build_checklist(metadata=after, before=before, kind="update")
    dn = _item(cl, "datanodes_mapped")
    assert _unanswered(dn)
    assert dn["required"] is True  # still asked: only the curator's N/A takes it off the gate
    assert (dn["suggested"], dn["scoped"]) == ("na", True)
    assert hint_text(dn) == "Automated check: This update did not touch it. Suggests N/A."
    # A check with no relevance rule (render) is always kept, with no hint.
    assert _item(cl, "render_ok")["required"] is True
    assert "scoped" not in _item(cl, "render_ok")
    # The curator follows the suggestions: N/A where suggested, pass on the rest.
    for i in cl:
        i["state"] = "na" if i["suggested"] == "na" else "pass"
        i["required"] = requirement_for(i["key"], i["state"])
    assert is_complete(cl) is True


def test_update_keeps_check_when_subject_changed():
    before = parse_curation_metadata(MAPPED)
    after = parse_curation_metadata(UNMAPPED)  # data nodes differ
    cl = build_checklist(metadata=after, before=before, kind="update")
    dn = _item(cl, "datanodes_mapped")
    assert dn["required"] is True
    assert _unanswered(dn)
    assert dn["suggested"] == "fail"  # relevant → the normal check gives the hint
    assert "scoped" not in dn


def test_pipeline_naming_and_file_list_hints():
    cl = build_checklist(
        metadata=parse_curation_metadata(MAPPED),
        kind="new",
        pipeline_mode=True,
        pathway_paths=["pathways/WP1/WP1.gpml", "pathways/WP2/WP2.gpml"],
    )
    assert all(_unanswered(i) for i in cl)
    naming = _item(cl, "naming_ok")
    assert naming["suggested"] == "pending"
    assert "assigns its WPID" in naming["hint"]
    one = _item(cl, "one_pathway_per_pr")
    assert one["suggested"] == "fail"
    assert "changes 2 pathways" in one["hint"]

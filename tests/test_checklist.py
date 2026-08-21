from __future__ import annotations

from app.preview.metadata import parse_curation_metadata
from app.review.checklist import build_checklist, default_checklist, is_complete

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


def test_auto_checks_prefill_states():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert _item(cl, "datanodes_mapped")["state"] == "pass"
    assert _item(cl, "datanodes_mapped")["auto"] is True
    assert _item(cl, "naming_ok")["state"] == "pass"
    assert _item(cl, "ontology_tags")["state"] == "pass"
    # "Meaningful" stays a human judgement, so the auto-check can never reach pass — but it does
    # run, and its note says how the description measures against what the repository asks for.
    assert _item(cl, "description_ok")["state"] == "pending"
    assert _item(cl, "description_ok")["auto"] is True
    # "Interactions are connected" is on the repository's reviewer checklist and cannot be
    # answered from parsed annotation, so it stays a blank human judgement.
    assert _item(cl, "interactions_connected")["state"] == "pending"
    assert _item(cl, "interactions_connected")["auto"] is False
    # No references is `pending`, not `na` (issue #27). The repository asks for at least one, so
    # a pathway with none has not answered the check — it is exactly the thing a curator should
    # be weighing, and `na` was quietly taking it off the approval gate instead.
    refs = _item(cl, "references_valid")
    assert refs["state"] == "pending"
    assert refs["required"] is True
    assert "at least one" in refs["note"]
    # The render check stays a human judgement — no auto state.
    assert _item(cl, "render_ok")["state"] == "pending"
    assert _item(cl, "render_ok")["auto"] is False


def test_well_annotated_submission_only_needs_human_checks():
    cl = build_checklist(metadata=parse_curation_metadata(MAPPED), kind="new")
    assert is_complete(cl) is False  # the human judgements are still pending
    # The structural checks auto-pass; only the human judgements remain. `references_valid` is
    # among them because MAPPED declares none and the repository asks for one.
    for key in ("render_ok", "description_ok", "interactions_connected", "references_valid"):
        assert _item(cl, key)["state"] == "pending"
        _item(cl, key)["state"] = "pass"
    assert is_complete(cl) is True


def test_unmapped_datanode_auto_fails_with_note():
    cl = build_checklist(metadata=parse_curation_metadata(UNMAPPED), kind="new")
    item = _item(cl, "datanodes_mapped")
    assert item["state"] == "fail"
    assert "no identifier" in item["note"]
    assert "MysteryGene" in item["note"]


def test_default_checklist_is_all_pending():
    cl = default_checklist()
    # `one_pathway_per_pr` is derived from the pull request's file list, not from the GPML, so a
    # template built with no inputs has nothing to check. It resolves to `na` rather than
    # `pending` on purpose: pending on a required item blocks approval, and a template must not
    # wedge the gate.
    checked = [i for i in cl if i["key"] != "one_pathway_per_pr"]
    assert all(i["state"] == "pending" and i["auto"] is False for i in checked)
    one = _item(cl, "one_pathway_per_pr")
    assert (one["state"], one["required"]) == ("na", False)


def test_update_scopes_unchanged_checks_to_na():
    before = parse_curation_metadata(MAPPED)
    after = parse_curation_metadata(MAPPED)  # identical → nothing changed
    cl = build_checklist(metadata=after, before=before, kind="update")
    dn = _item(cl, "datanodes_mapped")
    assert dn["state"] == "na"
    assert dn["required"] is False  # irrelevant → non-blocking
    assert "Not relevant" in dn["note"]
    # A check with no relevance rule (render) is always kept.
    assert _item(cl, "render_ok")["required"] is True
    # With every changeable subject unchanged and the always-kept human checks marked pass,
    # approval isn't blocked by the scoped-out items.
    for i in cl:
        if i["key"] in ("render_ok", "interactions_connected"):
            i["state"] = "pass"
    assert is_complete(cl) is True


def test_update_keeps_check_when_subject_changed():
    before = parse_curation_metadata(MAPPED)
    after = parse_curation_metadata(UNMAPPED)  # data nodes differ
    cl = build_checklist(metadata=after, before=before, kind="update")
    dn = _item(cl, "datanodes_mapped")
    assert dn["required"] is True  # relevant → normal auto-check runs
    assert dn["state"] == "fail"

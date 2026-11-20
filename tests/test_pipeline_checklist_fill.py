"""Checklist hints from the target repo's derived artifacts.

The service-side rules, as opposed to the mapping itself (tests/test_drafts_reader.py): the
pipeline's readings land beside the items as hints, and only a curator ever sets a state.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.curators import ConfigCurators
from app.github import FakeGitHubClient
from app.review.checklist import ChecklistState, is_complete
from app.review.service import CurationService

REPO = "wikipathways/sandbox-wp-db"


@dataclass
class _StubArtifacts:
    slug: str = "WP0__PR1"
    available: bool = True
    datanodes: list | None = None
    bibliography: list | None = None
    info: dict | None = None
    draft_url: str | None = None
    svg_url: str | None = None
    thumb_url: str | None = None


class _StubDrafts:
    """Stands in for DraftsReader so these tests pin the service's rules, not the TSV parsing."""

    def __init__(self, artifacts) -> None:
        self.artifacts = artifacts
        self.fetched: list[str] = []

    @staticmethod
    def slug_for(*, kind, wpid, pr_number):
        return f"WP0__PR{pr_number}" if kind == "new" else f"WP{wpid}__PR{pr_number}"

    def fetch(self, slug):
        self.fetched.append(slug)
        return self.artifacts


def _service(session_factory, drafts) -> CurationService:
    gh = FakeGitHubClient(default_branches={f"{REPO}#main": "basesha"})
    return CurationService(
        session_factory,
        gh,
        repo=REPO,
        curators=ConfigCurators(["marvinm2"]),
        publish_mode="pipeline",
        drafts=drafts,
    )


def _register(svc, pr_number=1, kind="new"):
    return svc.register(
        pr_number=pr_number, wpid=None, submitter="alice", kind=kind, head_branch="b"
    )


def _state(review, key):
    return next(i["state"] for i in review.checklist if i["key"] == key)


def _get(review, key):
    return next(i for i in review.checklist if i["key"] == key)


def test_refresh_never_sets_a_state(session_factory, monkeypatch):
    # Only a curator answers an item. Whatever the pipeline's readers say, including `na` on a
    # required item, lands beside the item as a hint and leaves the state pending and the item
    # on the approval gate. The readers are forced to return `na` so the rule is tested rather
    # than their current behaviour.
    artifacts = _StubArtifacts(
        datanodes=[], bibliography=[], info={"title": "T", "description": "D"}
    )
    svc = _service(session_factory, _StubDrafts(artifacts))
    _register(svc)

    def na_everything(*args, **kwargs):
        return (ChecklistState.NA.value, "nothing to check")

    monkeypatch.setattr("app.pipeline.drafts.datanode_check", na_everything)
    monkeypatch.setattr("app.pipeline.drafts.reference_check", na_everything)
    monkeypatch.setattr("app.pipeline.drafts.info_checks", lambda a: {})

    review = svc.refresh_pipeline_checks(1, gpml_reference_count=0)

    for key in ("datanodes_mapped", "references_valid"):
        item = _get(review, key)
        assert (item["state"], item["required"], item["auto"]) == ("pending", True, False)
        assert (item["suggested"], item["hint"]) == ("na", "nothing to check")
        assert item["note"] == ""


def test_pipeline_fail_is_a_hint_not_an_answer(session_factory):
    artifacts = _StubArtifacts(
        datanodes=[{"Identifier": "", "Label": "TP53"}],
        bibliography=[],
        info={"title": "", "description": ""},
    )
    svc = _service(session_factory, _StubDrafts(artifacts))
    _register(svc)

    review = svc.refresh_pipeline_checks(1, gpml_reference_count=2)

    assert all(i["state"] == "pending" for i in review.checklist)
    assert _get(review, "datanodes_mapped")["suggested"] == "fail"
    assert _get(review, "references_valid")["suggested"] == "fail"
    assert _get(review, "description_ok")["suggested"] == "fail"
    assert not is_complete(review.checklist)


def test_a_curators_own_answer_is_never_overwritten(session_factory):
    artifacts = _StubArtifacts(
        datanodes=[{"Identifier": ""}], bibliography=[], info={"title": "T", "description": "D"}
    )
    svc = _service(session_factory, _StubDrafts(artifacts))
    _register(svc)
    svc.set_checklist_item(1, "datanodes_mapped", ChecklistState.PASS.value, note="checked by me")

    svc.refresh_pipeline_checks(1)

    review = svc.get(1)
    assert _state(review, "datanodes_mapped") == ChecklistState.PASS.value
    assert next(i["note"] for i in review.checklist if i["key"] == "datanodes_mapped") == (
        "checked by me"
    )


def test_unavailable_artifacts_change_nothing(session_factory):
    svc = _service(session_factory, _StubDrafts(_StubArtifacts(available=False)))
    before = _register(svc)
    before_states = [(i["key"], i["state"]) for i in before.checklist]

    assert svc.refresh_pipeline_checks(1) is None
    after = svc.get(1)
    assert [(i["key"], i["state"]) for i in after.checklist] == before_states


def test_no_drafts_reader_is_a_no_op(session_factory):
    svc = _service(session_factory, None)
    _register(svc)

    assert svc.refresh_pipeline_checks(1) is None


def test_a_reader_that_explodes_does_not_break_the_page(session_factory):
    class Exploding(_StubDrafts):
        def fetch(self, slug):
            raise RuntimeError("raw.githubusercontent.com is having a day")

    svc = _service(session_factory, Exploding(_StubArtifacts()))
    _register(svc)

    assert svc.refresh_pipeline_checks(1) is None


def test_the_slug_follows_the_review_kind(session_factory):
    drafts = _StubDrafts(_StubArtifacts(available=False))
    svc = _service(session_factory, drafts)
    svc.register(pr_number=7, wpid=5636, submitter="alice", kind="update", head_branch="b")

    svc.refresh_pipeline_checks(7)

    assert drafts.fetched == ["WP5636__PR7"]


def test_a_hinted_review_can_still_reach_complete(session_factory):
    # The point of the hints: a curator confirms rather than derives. Answering every required
    # item Pass must open the gate, whatever the hints said.
    artifacts = _StubArtifacts(
        datanodes=[{"Identifier": "1234", "Label": "TP53"}],
        bibliography=[{"ID": "123"}],
        info={"title": "Mitophagy", "description": "A long enough description."},
    )
    svc = _service(session_factory, _StubDrafts(artifacts))
    _register(svc)
    svc.refresh_pipeline_checks(1, gpml_reference_count=1)

    review = svc.get(1)
    for item in review.checklist:
        if item["required"] and item["state"] != ChecklistState.PASS.value:
            svc.set_checklist_item(1, item["key"], ChecklistState.PASS.value)
    assert is_complete(svc.get(1).checklist)


def test_a_hint_is_re_derived_after_a_revision(session_factory):
    """A fixed submission must not keep a stale verdict beside it. PR #42, 2026-08-14.

    The first upload's reference was cited by no <BiopaxRef>, so the pipeline resolved none of
    it; the revision cited it and the pipeline agreed. The hint follows the newest reading.
    """
    drafts = _StubDrafts(
        _StubArtifacts(datanodes=None, bibliography=[], info={"title": "T", "description": "D"})
    )
    svc = _service(session_factory, drafts)
    _register(svc)

    svc.refresh_pipeline_checks(1, gpml_reference_count=1)
    refs = _get(svc.get(1), "references_valid")
    assert (refs["state"], refs["suggested"]) == ("pending", "fail")

    drafts.artifacts = _StubArtifacts(
        datanodes=None,
        bibliography=[{"ID": "12829793"}],
        info={"title": "T", "description": "D"},
    )
    svc.refresh_pipeline_checks(1, gpml_reference_count=1)

    refs = _get(svc.get(1), "references_valid")
    assert (refs["state"], refs["suggested"]) == ("pending", "pass")
    assert refs["hint"] == "The pipeline resolved all 1 reference."


def test_refresh_updates_the_hint_beside_a_curators_answer(session_factory):
    drafts = _StubDrafts(
        _StubArtifacts(datanodes=None, bibliography=[], info={"title": "T", "description": "D"})
    )
    svc = _service(session_factory, drafts)
    _register(svc)
    svc.set_checklist_item(1, "references_valid", ChecklistState.FAIL.value, note="cites nothing")

    svc.refresh_pipeline_checks(1, gpml_reference_count=1)

    refs = _get(svc.get(1), "references_valid")
    assert (refs["state"], refs["note"]) == ("fail", "cites nothing")
    assert refs["suggested"] == "fail"
    assert "resolved 0 of the 1" in refs["hint"]


def test_an_empty_pipeline_ontology_list_leaves_the_gpml_hint(session_factory):
    """PR #91, 2026-11-20: two <OntologyTerm> elements in the GPML, `"ontology-ids": []` in the
    pipeline's info.json (its extractor does not read them). The empty list is no opinion."""
    from app.preview.metadata import parse_curation_metadata

    gpml = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <OntologyTerm Term="insulin signaling pathway" ID="PW:0000143" Ontology="Pathway Ontology"/>
  <OntologyTerm Term="liver" ID="UBERON:0002107" Ontology="Uberon"/>
</Pathway>
"""
    artifacts = _StubArtifacts(
        info={"wpid": "WP0__PR1", "title": "T", "description": "D", "ontology-ids": []}
    )
    svc = _service(session_factory, _StubDrafts(artifacts))
    svc.register(
        pr_number=1,
        wpid=None,
        submitter="alice",
        kind="new",
        head_branch="b",
        metadata=parse_curation_metadata(gpml),
    )

    svc.refresh_pipeline_checks(1)

    onto = _get(svc.get(1), "ontology_tags")
    assert onto["state"] == "pending"
    assert (onto["suggested"], onto["hint"]) == ("pass", "2 ontology tags.")
    # The rest of info.json still counts: the naming hint came from it.
    assert _get(svc.get(1), "naming_ok")["hint"].startswith("Filed as WP0__PR1")


def test_a_non_empty_pipeline_ontology_list_is_a_reading(session_factory):
    artifacts = _StubArtifacts(info={"title": "T", "description": "D", "ontology-ids": ["PW:1"]})
    svc = _service(session_factory, _StubDrafts(artifacts))
    _register(svc)

    svc.refresh_pipeline_checks(1)

    onto = _get(svc.get(1), "ontology_tags")
    assert (onto["state"], onto["suggested"]) == ("pending", "pass")
    assert onto["hint"] == "1 ontology tag."


def test_refresh_leaves_an_update_scoped_hint_alone(session_factory):
    from app.preview.metadata import parse_curation_metadata

    gpml = """<?xml version="1.0"?>
<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Demo" Organism="Homo sapiens">
  <DataNode TextLabel="INSR" Type="GeneProduct"><Xref Database="Ensembl" ID="ENSG1"/></DataNode>
</Pathway>
"""
    meta = parse_curation_metadata(gpml)
    artifacts = _StubArtifacts(datanodes=[{"Identifier": "", "Label": "INSR"}])
    svc = _service(session_factory, _StubDrafts(artifacts))
    svc.register(
        pr_number=7, wpid=5636, submitter="alice", kind="update", head_branch="b",
        metadata=meta, before_metadata=meta,
    )

    svc.refresh_pipeline_checks(7)

    dn = _get(svc.get(7), "datanodes_mapped")
    assert (dn["state"], dn["suggested"], dn["scoped"]) == ("pending", "na", True)
    assert dn["hint"] == "This update did not touch it."


def test_re_deriving_still_leaves_a_curators_override_alone(session_factory):
    """The guard on the fix above: re-deriving must not become "overwrite everything".

    A curator disagreeing with the pipeline is the case the whole override exists for, and it
    is also the one a looser gate would silently undo on the next page load.
    """
    artifacts = _StubArtifacts(
        datanodes=[{"Identifier": ""}], bibliography=[], info={"title": "T", "description": "D"}
    )
    drafts = _StubDrafts(artifacts)
    svc = _service(session_factory, drafts)
    _register(svc)

    svc.refresh_pipeline_checks(1)
    svc.set_checklist_item(1, "datanodes_mapped", ChecklistState.PASS.value)

    svc.refresh_pipeline_checks(1)  # artifacts still say fail

    assert _state(svc.get(1), "datanodes_mapped") == ChecklistState.PASS.value


def _legacy_row(svc, pr_number=1):
    """A review written before the app stopped answering items: its state was the app's guess."""
    from app.models import Review

    with svc._session_factory() as s:
        review = s.get(Review, pr_number)
        checklist = [dict(i) for i in review.checklist]
        for item in checklist:
            if item["key"] == "references_valid":
                item.update(
                    state="fail",
                    auto=True,
                    note="The pipeline resolved 0 of the 1 reference in the GPML.",
                )
                for field in ("suggested", "hint"):
                    item.pop(field, None)
        review.checklist = checklist
        s.commit()


def test_overriding_an_old_rows_automated_verdict_marks_the_note_it_contradicts(
    session_factory,
):
    """PR #42 read "References resolve — pass" above "The pipeline resolved 0 of the 1 reference".

    Only rows from before the hint split carry an app-written state and note. The note is kept,
    because what the pipeline saw is evidence; only its standing changes.
    """
    svc = _service(session_factory, _StubDrafts(_StubArtifacts(available=False)))
    _register(svc)
    _legacy_row(svc)

    svc.set_checklist_item(1, "references_valid", ChecklistState.PASS.value)

    refs = _get(svc.get(1), "references_valid")
    assert (refs["state"], refs["auto"]) == ("pass", False)
    assert refs["note"].startswith("Overridden by a curator.")


def test_an_old_rows_states_survive_a_refresh(session_factory):
    """Existing rows keep their checklists: a refresh writes the hint and nothing else."""
    drafts = _StubDrafts(
        _StubArtifacts(datanodes=None, bibliography=[{"ID": "1"}], info={"title": "T"})
    )
    svc = _service(session_factory, drafts)
    _register(svc)
    _legacy_row(svc)

    svc.refresh_pipeline_checks(1, gpml_reference_count=1)

    refs = _get(svc.get(1), "references_valid")
    assert (refs["state"], refs["auto"], refs["suggested"]) == ("fail", True, "pass")


def test_agreeing_with_an_automated_hint_leaves_the_note_clean(session_factory):
    """Confirming what the pipeline suggested is not an override, and must not read as one."""
    svc = _service(
        session_factory,
        _StubDrafts(
            _StubArtifacts(
                datanodes=None,
                bibliography=[{"ID": "12829793"}],
                info={"title": "T", "description": "D"},
            )
        ),
    )
    _register(svc)
    svc.refresh_pipeline_checks(1, gpml_reference_count=1)
    assert _get(svc.get(1), "references_valid")["suggested"] == ChecklistState.PASS.value

    svc.set_checklist_item(1, "references_valid", ChecklistState.PASS.value)

    refs = _get(svc.get(1), "references_valid")
    assert refs["note"] == ""
    assert refs["hint"] == "The pipeline resolved all 1 reference."

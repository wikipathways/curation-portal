"""Filling the checklist from the target repo's derived artifacts.

The service-side rules, as opposed to the mapping itself (tests/test_drafts_reader.py): what may
be written over, and what must never be written at all.
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


def test_a_required_item_never_lands_on_na(session_factory, monkeypatch):
    # `na` on a required item is a state is_complete can never accept, so it would strand the
    # review: approval blocked, with nothing on screen explaining why. The mapping functions are
    # forced to return `na` here so the guard is tested rather than their current behaviour.
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

    assert _state(review, "datanodes_mapped") == ChecklistState.PENDING.value
    assert _state(review, "references_valid") == ChecklistState.PENDING.value
    # The note survives the downgrade, so the curator still sees the reasoning.
    note = next(i["note"] for i in review.checklist if i["key"] == "datanodes_mapped")
    assert note == "nothing to check"


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


def test_an_auto_filled_review_can_still_reach_complete(session_factory):
    # The point of the fill: a curator confirms rather than derives. It must be possible to end
    # up with every required item passed without any of them having been stranded on `na`.
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


def test_a_verdict_this_writer_made_is_re_derived_after_a_revision(session_factory):
    """The defect that let a fixed submission keep failing. PR #42, 2026-08-14.

    The gate used to be "only if still pending". Once this writer put a `fail` on an item the
    item was no longer pending, so it could never correct itself — a submitter fixing exactly
    what it complained about, and a fresh pipeline run agreeing, left the failure standing.

    `references_valid` is the item it actually happened to, and the one whose mapping can reach
    `pass`; `datanode_check` is documented never to.
    """
    drafts = _StubDrafts(
        _StubArtifacts(datanodes=None, bibliography=[], info={"title": "T", "description": "D"})
    )
    svc = _service(session_factory, drafts)
    _register(svc)

    # First upload: the GPML declares a reference no <BiopaxRef> cites, so the pipeline
    # resolves none of it.
    svc.refresh_pipeline_checks(1, gpml_reference_count=1)
    assert _state(svc.get(1), "references_valid") == ChecklistState.FAIL.value

    # The revision cites it and the pipeline's bibliography comes back with it in.
    drafts.artifacts = _StubArtifacts(
        datanodes=None,
        bibliography=[{"ID": "12829793"}],
        info={"title": "T", "description": "D"},
    )
    svc.refresh_pipeline_checks(1, gpml_reference_count=1)

    assert _state(svc.get(1), "references_valid") == ChecklistState.PASS.value


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


def test_overriding_an_automated_verdict_marks_the_note_it_contradicts(session_factory):
    """PR #42 read "References resolve — pass" above "The pipeline resolved 0 of the 1 reference".

    Two answers to one question. The note is kept, because what the pipeline saw is evidence and
    a curator overruling it is the interesting part of the record; only its standing changes.
    """
    artifacts = _StubArtifacts(
        datanodes=[{"Identifier": ""}], bibliography=[], info={"title": "T", "description": "D"}
    )
    svc = _service(session_factory, _StubDrafts(artifacts))
    _register(svc)
    svc.refresh_pipeline_checks(1)

    svc.set_checklist_item(1, "datanodes_mapped", ChecklistState.PASS.value)

    note = next(i["note"] for i in svc.get(1).checklist if i["key"] == "datanodes_mapped")
    assert note.startswith("Overridden by a curator.")


def test_agreeing_with_an_automated_verdict_leaves_its_note_clean(session_factory):
    """Confirming what the pipeline already said is not an override, and must not read as one."""
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
    assert _state(svc.get(1), "references_valid") == ChecklistState.PASS.value

    svc.set_checklist_item(1, "references_valid", ChecklistState.PASS.value)

    note = next(i["note"] for i in svc.get(1).checklist if i["key"] == "references_valid")
    assert "Overridden" not in note

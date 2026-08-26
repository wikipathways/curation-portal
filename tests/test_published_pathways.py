"""The quality ruleset, run against pathways the project has actually published.

Every fixture elsewhere in this suite is hand-written, which means it encodes what its author
already knew to include — so it can confirm the rules they thought of and nothing else. That has
now cost real time five separate times (see `tests/fixtures/published/README.md`), always the same
shape: a GPML the portal renders and validates happily that the content repository's own reader
refuses, or that is missing something the repository asks for.

These two files are the negative control. They are PathVisio output that survived curation and
publication, so a rule reporting `fail` or `block` on one of them is wrong about reality whatever
it says about the schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.preview.metadata import parse_curation_metadata
from app.quality import Severity, blocking_reasons, inspect_gpml
from app.review.checklist import build_checklist

_DIR = Path(__file__).parent / "fixtures" / "published"
_PUBLISHED = sorted(_DIR.glob("*.gpml"))

#: The warnings a real pathway may legitimately raise, and why each is left standing rather than
#: softened to fit. None of them is a prediction that the pipeline breaks.
#:
#: - ``content.datanode_annotation`` — 21 of 30 sampled pathways have at least one unannotated
#:   data node. Genuinely common, and genuinely worth a curator's attention on a *new* submission.
#: - ``content.references`` — 6 of 30 declare no literature reference at all. The repository's own
#:   reviewer checklist asks for one, so warning is honest even though a fifth of published
#:   content would trip it (issue #27).
#: - ``gpml.citation_ids`` — empty ``<bp:ID>`` elements, which the repository rewrites to ``NA``
#:   before running. A warning about the source file, by design.
#: - ``content.description`` / ``content.title_length`` — the repository's own thresholds, ported.
#:   Legacy content predates them.
_ALLOWED_WARNINGS = {
    "content.datanode_annotation",
    "content.references",
    "gpml.citation_ids",
    "content.description",
    "content.title_length",
    "gpml.author",
}


@pytest.mark.parametrize("path", _PUBLISHED, ids=lambda p: p.stem)
def test_no_rule_fails_a_published_pathway(path: Path):
    """The point of the whole file: nothing the project published may be reported broken.

    Both fixtures were picked from the six of thirty sampled pathways that came back entirely
    clean, so this is stricter than the parametrisation suggests — it also pins that they *stay*
    clean as rules are added. `gpml.line_thickness` (issue #26) fired on none of the thirty, which
    is the evidence that real PathVisio output always writes the attribute.
    """
    report = inspect_gpml(path.read_text(encoding="utf-8"))
    broken = [f for f in report.findings if f.severity in (Severity.FAIL, Severity.BLOCK)]
    assert broken == [], [(f.id, f.detail) for f in broken]
    assert report.status == Severity.PASS.value


@pytest.mark.parametrize("path", _PUBLISHED, ids=lambda p: p.stem)
def test_a_published_pathway_is_never_refused_at_upload(path: Path):
    """`validate_gpml` is the `block` subset, so this is the portal's front door."""
    assert blocking_reasons(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", _PUBLISHED, ids=lambda p: p.stem)
def test_a_published_pathway_raises_only_warnings_we_have_reasoned_about(path: Path):
    """A new warning on real content is a decision, not an accident — make it show up here.

    Widening `_ALLOWED_WARNINGS` should mean writing down why that warning is acceptable on a file
    the project already published.
    """
    report = inspect_gpml(path.read_text(encoding="utf-8"))
    surprising = [
        f.id
        for f in report.findings
        if f.severity == Severity.WARN and f.id not in _ALLOWED_WARNINGS
    ]
    assert surprising == []


@pytest.mark.parametrize("path", _PUBLISHED, ids=lambda p: p.stem)
def test_the_checklist_a_curator_sees_for_real_content_has_no_dead_items(path: Path):
    """Issue #27's invariant, checked against real metadata rather than a constructed case.

    A required item sitting at `na` is an approval gate nothing can open. The rule is that `na`
    and blocking are mutually exclusive, and the cheapest place to catch a regression is on files
    whose metadata nobody wrote to suit the test.
    """
    checklist = build_checklist(
        metadata=parse_curation_metadata(path.read_text(encoding="utf-8")), kind="new"
    )
    wedged = [i["key"] for i in checklist if i["required"] and i["state"] == "na"]
    assert wedged == []


def test_the_fixtures_are_actually_present():
    """A silent empty glob would make every parametrised test above vacuously pass."""
    assert len(_PUBLISHED) >= 2


@pytest.mark.parametrize("path", _PUBLISHED, ids=lambda p: p.stem)
def test_every_ontology_tag_a_published_pathway_declares_is_parsed(path: Path):
    """The review panel must count the tags the file actually carries.

    PathVisio writes ontology annotations as ``<bp:openControlledVocabulary>`` blocks inside
    ``<Biopax>``. The parser used to read only the ``<OntologyTerm>`` element, which real
    WikiPathways content never uses — so it reported zero tags for every published pathway,
    and the checklist's ``ontology_tags`` item resolved to ``na`` on files carrying several.
    It never blocked anything only because that item is not required.

    Counting the raw elements rather than asserting a fixed number keeps this honest if the
    fixture set grows.
    """
    raw = path.read_bytes()
    declared = raw.count(b"<bp:openControlledVocabulary") + raw.count(b"<OntologyTerm")

    parsed = parse_curation_metadata(raw).ontology_tags

    assert len(parsed) == declared, (
        f"{path.name} declares {declared} ontology tag(s), parser found {len(parsed)}"
    )
    # A tag the panel cannot render is no better than a missing one.
    for tag in parsed:
        assert tag.identifier, f"{path.name}: ontology tag with no identifier: {tag}"
        assert tag.term, f"{path.name}: ontology tag with no term: {tag}"

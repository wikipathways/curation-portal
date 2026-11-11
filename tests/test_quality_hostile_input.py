"""Crafted uploads must be refused or graded quickly, never ground through.

`/api/validate` is unauthenticated, and grading is pure Python holding the interpreter lock, so a
slow rule is a denial of service a threadpool cannot absorb. Before the readability gate, the
structural rules were text patterns that rescanned the rest of the document from every opening
tag they could not close: 80 KB of `<Interaction>` after a valid header took about five seconds in
`validate_gpml` and the same again in `inspect_gpml` — and was then reported as valid, because
nothing checked the file was XML at all. A well-formed file with a few thousand self-closing
`<Interaction/>` elements took three and a half.

Each input below is one pattern family from that defect, at the size that used to take seconds.
The bound is generous against today's timings (milliseconds) and far below the old ones, so it
fails on a regression rather than on a slow machine.
"""
from __future__ import annotations

import time

import pytest

from app.quality import Severity, blocking_reasons, inspect_gpml, inspect_metadata
from app.submit.gpml import InvalidGpml, validate_gpml
from tests.test_quality_rules import GOOD

_BOUND_SECONDS = 0.5
_SIZE = 80_000

_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Crafted pathway" '
    'Organism="Homo sapiens">'
)


def _repeat(unit: str, *, prefix: str = _HEADER) -> str:
    return prefix + unit * (_SIZE // len(unit))


def _timed(fn, *args):
    start = time.perf_counter()
    try:
        result = fn(*args)
    except InvalidGpml as exc:
        result = exc
    return result, time.perf_counter() - start


# Unterminated opening tags: the shape that drove each pattern to the end of the text.
_UNCLOSED = {
    "interaction": _repeat("<Interaction>"),
    "graphical-line": _repeat("<GraphicalLine x>"),
    "graphics": _repeat("<Graphics "),
    "citation-id": _repeat("<bp:ID "),
    "pathway-tag": _repeat("<Pathway ", prefix=""),
}


@pytest.mark.parametrize("name", sorted(_UNCLOSED))
def test_an_unclosed_tag_flood_is_refused_quickly_as_unreadable(name):
    text = _UNCLOSED[name]

    outcome, elapsed = _timed(validate_gpml, text)
    assert isinstance(outcome, InvalidGpml)
    assert elapsed < _BOUND_SECONDS, f"validate_gpml took {elapsed:.2f}s"
    assert len(outcome.reasons) == 1
    assert outcome.reasons[0].startswith("This file is not valid XML")

    report, elapsed = _timed(inspect_gpml, text)
    assert elapsed < _BOUND_SECONDS, f"inspect_gpml took {elapsed:.2f}s"
    # Nothing else runs on a file with no structure, so nothing else can be slow on one.
    assert [f.id for f in report.findings] == ["gpml.readable"]
    assert report.status == Severity.BLOCK.value


# Well-formed files the old patterns were still quadratic on.
_WELL_FORMED = {
    # Each self-closing element sent the line pattern looking for a closing tag to the end.
    "self-closing-interactions": _HEADER
    + '<Graphics BoardWidth="480.0" BoardHeight="440.0"/>'
    + "".join(f'<Interaction GraphId="i{i}"/>' for i in range(3500))
    + "</Pathway>",
    # Inside a comment nothing terminates a tag, so `<Graphics`/`<bp:ID` never found their `>`.
    "tags-inside-a-comment": _HEADER
    + "<!-- "
    + "<Graphics <bp:ID <Interaction " * (_SIZE // 30)
    + "--></Pathway>",
    # Deep nesting: a per-element subtree search would be quadratic here.
    "nested-interactions": _HEADER
    + "<Interaction>" * 3000
    + "</Interaction>" * 3000
    + "</Pathway>",
}


@pytest.mark.parametrize("name", sorted(_WELL_FORMED))
def test_a_large_well_formed_file_is_graded_quickly(name):
    text = _WELL_FORMED[name]
    assert len(text) > 50_000

    outcome, elapsed = _timed(validate_gpml, text)
    assert not isinstance(outcome, InvalidGpml), outcome
    assert elapsed < _BOUND_SECONDS, f"validate_gpml took {elapsed:.2f}s"

    report, elapsed = _timed(inspect_gpml, text)
    assert elapsed < _BOUND_SECONDS, f"inspect_gpml took {elapsed:.2f}s"
    assert report.by_id("gpml.line_thickness") is not None


def test_self_closing_interactions_are_still_each_named_as_offenders():
    """Faster must not mean blind: every one of them lacks a `<Graphics>` and is reported."""
    finding = inspect_gpml(_WELL_FORMED["self-closing-interactions"]).by_id("gpml.line_thickness")
    assert finding.severity == Severity.FAIL.value
    assert finding.detail.startswith("3500 line elements")
    assert "Interaction i0" in finding.detail


def test_a_commented_out_board_does_not_count_as_a_canvas():
    """The tree reads what the file declares; a comment declares nothing."""
    text = GOOD.replace(
        '<Graphics BoardWidth="480.0" BoardHeight="440.0" />',
        '<!-- <Graphics BoardWidth="480.0" BoardHeight="440.0" /> -->',
    )
    assert inspect_gpml(text).by_id("gpml.board").severity == Severity.FAIL.value


# ---- the gate itself -------------------------------------------------------------------------


def test_the_refusal_says_where_the_file_breaks_in_plain_words():
    broken = GOOD.replace("</Pathway>", "")
    reasons = blocking_reasons(broken)
    assert len(reasons) == 1
    assert "near line" in reasons[0] and "column" in reasons[0]
    # Written for a submitter: what to do about it, not what the parser called it.
    assert "PathVisio" in reasons[0]
    assert "ParseError" not in reasons[0] and "expat" not in reasons[0]


def test_the_line_number_counts_from_the_top_of_the_uploaded_file():
    """Leading blank lines are skipped before parsing and added back to the position."""
    broken = "\n\n" + GOOD.replace("</Pathway>", "<oops></Pathway>")
    expected = broken.splitlines().index("<oops></Pathway>") + 1
    assert f"near line {expected}," in blocking_reasons(broken)[0]


def test_entity_declarations_are_refused_before_any_parsing():
    """The billion-laughs shape. Refused on sight; nothing a pathway needs uses a DTD."""
    laughs = (
        '<?xml version="1.0"?>\n<!DOCTYPE Pathway [\n<!ENTITY a "aaaaaaaaaa">\n'
        + "".join(f'<!ENTITY {chr(98 + i)} "' + f"&{chr(97 + i)};" * 10 + '">\n' for i in range(9))
        + ']>\n<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="Laughing pathway" '
        'Organism="Homo sapiens">&j;</Pathway>'
    )
    outcome, elapsed = _timed(validate_gpml, laughs)
    assert isinstance(outcome, InvalidGpml)
    assert elapsed < _BOUND_SECONDS
    assert "<!DOCTYPE" in outcome.reasons[0]
    assert [f.id for f in inspect_gpml(laughs).findings] == ["gpml.readable"]


def test_whitespace_before_the_declaration_is_tolerated_because_the_portal_strips_it():
    """`assign_wpid_str` rewrites the declaration and drops what precedes it, so the committed
    file is valid. Refusing the upload would refuse something the portal repairs anyway."""
    assert blocking_reasons("\n  " + GOOD) == []


def test_a_byte_order_mark_is_not_a_reason_to_refuse():
    assert blocking_reasons(("﻿" + GOOD).encode("utf-8")) == []


def test_an_empty_upload_is_still_refused_for_having_no_pathway():
    """Unchanged: the root rule has always been what refuses an empty file."""
    assert blocking_reasons("") == ["no <Pathway> root element found"]
    assert blocking_reasons("   \n") == ["no <Pathway> root element found"]


def test_the_gate_needs_the_document_so_metadata_alone_stays_quiet():
    from app.preview.metadata import parse_curation_metadata

    assert inspect_metadata(parse_curation_metadata(GOOD)).by_id("gpml.readable") is None


def test_a_readable_file_gets_no_extra_row():
    """A precondition, not a grade: a clean report and the mirror comment keep their shape."""
    assert inspect_gpml(GOOD).by_id("gpml.readable") is None

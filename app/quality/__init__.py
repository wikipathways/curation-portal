"""One graded quality ruleset, shared by the submit form, the checklist and the mirror comment.

See ``app.quality.rules`` for what the rules are, where each came from, and the import constraint
this package works under. Everything below is the surface the rest of the app uses; nothing else
in ``app/`` should reach past it into ``rules``.
"""
from __future__ import annotations

from app.quality.report import EMPTY, Finding, QualityReport, Severity, worst
from app.quality.rules import RULES, Subject, build_subject, checklist_keys, evaluate

__all__ = [
    "EMPTY",
    "Finding",
    "QualityReport",
    "RULES",
    "Severity",
    "Subject",
    "blocking_reasons",
    "build_subject",
    "checklist_keys",
    "checklist_result",
    "evaluate",
    "inspect_gpml",
    "inspect_metadata",
    "worst",
]


def inspect_gpml(
    gpml: bytes | str,
    *,
    metadata: object | None = None,
    before: object | None = None,
    kind: str = "new",
    drawable: bool | None = None,
    origin: str = "portal",
    target_wpid: int | None = None,
) -> QualityReport:
    """Every rule, over an uploaded GPML. ``metadata`` is parsed from it when not supplied."""
    subject = build_subject(
        gpml,
        metadata=metadata,
        before=before,
        kind=kind,
        drawable=drawable,
        origin=origin,
        target_wpid=target_wpid,
    )
    return evaluate(subject, text_available=True)


def inspect_metadata(
    metadata: object, *, before: object | None = None, kind: str = "new"
) -> QualityReport:
    """The subset of rules that needs no raw GPML — what parsed metadata alone can answer.

    This is the shape a checklist auto-check runs in. The rules that read the document text are
    skipped rather than answered, because their absence branch would report a problem nobody
    looked for: with no text, every ``<bp:ID>`` is trivially absent.
    """
    subject = build_subject(None, metadata=metadata, before=before, kind=kind)
    return evaluate(subject, text_available=False)


def blocking_reasons(gpml: bytes | str) -> list[str]:
    """The reasons this upload must be refused, in rule order — or an empty list.

    ``app.submit.gpml.validate_gpml`` is defined in terms of this, so that the portal can never
    refuse a file for a reason its own pre-flight report called fine. Two lists of rules, one
    deciding the 422 and one shown to the submitter, is exactly how that happens.
    """
    return inspect_gpml(gpml).blocking_reasons


def checklist_result(
    key: str, metadata: object, *, before: object | None = None, kind: str = "new"
) -> tuple[str, str] | None:
    """``(state, note)`` for one checklist item, or None where no rule answers that key.

    The severity-to-state translation lives with the rule, not here, so an item whose honest
    ceiling is "pending" says so once — in the table — rather than in every caller.
    """
    from app.quality.rules import checklist_state_for

    finding = inspect_metadata(metadata, before=before, kind=kind).for_checklist(key)
    if finding is None:
        return None
    return (checklist_state_for(key, finding.severity), finding.detail)

"""The vocabulary every quality check in this app speaks.

Before this there were three, and they disagreed. ``app.submit.gpml.validate_gpml`` had two
outcomes (raise, or return); ``app.review.checklist`` had four states; ``mvp1/validate_pathway.py``
had three severities and a worst-wins rollup (removed 2026-11-13; see commit ``e96386d``). The
target repository's own ``testing`` job has a fourth vocabulary again ("PASS" / "REVIEW
REQUIRED"). A submitter could pass the first, be warned
by the second, and be sent back by the third, with nothing tying the three answers together.

The severities here are ``mvp1``'s three, plus the two it does not need:

- ``na`` — the rule had no subject. No references to count, no base version to diff against.
  Ranked *below* ``pass`` so it can never win a rollup: "there was nothing to check" is not a
  quality signal, and letting it rank as a pass would report an empty pathway as all-green.
- ``block`` — the submission is refused outright. This is the one distinction ``mvp1`` can do
  without and this module cannot: ``mvp1`` runs after the fact on a file already in the
  repository, so its worst verdict is still only a report. Here, four of the rules decide an
  HTTP 422. Collapsing ``block`` into ``fail`` would either start refusing submissions the portal
  accepts today (a pathway with no data nodes) or stop refusing ones it must (no Organism).
"""
from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass


class Severity(enum.StrEnum):
    NA = "na"
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    BLOCK = "block"


#: Rank for the worst-wins rollup. ``mvp1/validate_pathway.py`` calls this ``_RANK`` and orders the
#: middle three identically; a test pins that they stay in step.
_RANK: dict[str, int] = {
    Severity.NA.value: -1,
    Severity.PASS.value: 0,
    Severity.WARN.value: 1,
    Severity.FAIL.value: 2,
    Severity.BLOCK.value: 3,
}


def worst(severities: Iterable[str]) -> str:
    """The most severe of ``severities``, or ``pass`` for an empty run.

    An all-``na`` report rolls up to ``pass`` rather than to ``na``: a report is a statement about
    a submission, and "every check found nothing to look at" is the report saying it has no
    complaint. ``na`` survives on the individual findings, where it is informative.
    """
    top = Severity.PASS.value
    for severity in severities:
        if _RANK.get(severity, 0) > _RANK[top]:
            top = severity
    return top


#: An ``@`` that GitHub would read as a mention: not inside a word, as in an e-mail address.
_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9-])")


def table_cell(text: str) -> str:
    """Text made safe for one cell of a GitHub markdown table, read as it was written.

    On one line, with ``|`` escaped, or the table breaks. GPML element names are the subject of
    half the findings, and GitHub's markdown treats ``<Pathway>`` as an unknown HTML tag and drops
    it, leaving "The file has a root element.", so ``&``, ``<`` and ``>`` are escaped too. Escaped
    here rather than in the detail strings, because the same text is rendered as plain content on
    the dashboard, where a literal &lt; would be the bug instead. And an ``@handle`` gets a
    zero-width space after the ``@``: the text quotes labels from the submitted file, and the
    bot's comment must not ping whoever a label names.
    """
    text = " ".join(text.splitlines()).replace("|", "\\|")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MENTION.sub("@\u200b", text)


@dataclass(frozen=True)
class Finding:
    """One rule's verdict on one submission."""

    #: Stable and namespaced, never reused or renamed in place — the same contract checklist keys
    #: carry (``app.review.checklist``, "never reuse or rename one in place"). Three surfaces key
    #: off it: the submit form, the review card, and the mirror comment.
    id: str
    #: The short name in the "Check" column. Matches ``mvp1``'s wording wherever the check is
    #: shared, so a curator who has read the repository's table does not learn a second one.
    title: str
    severity: str
    #: One sentence a submitter can act on. Carries its own counts; nothing parses it.
    detail: str
    #: The checklist item this finding speaks to, or None. Lets the review card put a finding
    #: beside the item it explains instead of in a separate list the curator has to correlate.
    checklist_key: str | None = None
    #: True where the target repository's own ``testing`` job computes the same thing
    #: (``docs/sandbox-pipeline.md`` §1). Lets the UI say "the database will report this" rather
    #: than "we think this" — a different and more useful sentence, and the only one of the two
    #: that tells a submitter the finding will follow them onto the pull request.
    predicts_repo: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QualityReport:
    findings: tuple[Finding, ...] = ()

    @property
    def status(self) -> str:
        return worst(f.severity for f in self.findings)

    @property
    def blocking_reasons(self) -> list[str]:
        """The details of every ``block`` finding, in rule order.

        This is exactly what ``InvalidGpml.reasons`` used to be built from by hand, and it is a
        public list: ``app.main`` puts it in ``detail.errors`` and ``describeError`` in
        ``static/app.js`` renders it to the submitter. The strings are part of the interface.
        """
        return [f.detail for f in self.findings if f.severity == Severity.BLOCK.value]

    def by_id(self, rule_id: str) -> Finding | None:
        return next((f for f in self.findings if f.id == rule_id), None)

    def for_checklist(self, key: str) -> Finding | None:
        return next((f for f in self.findings if f.checklist_key == key), None)

    def counts(self) -> dict[str, int]:
        """How many findings at each severity — the pill row above the detail."""
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {"findings": [f.as_dict() for f in self.findings]}

    @classmethod
    def from_dict(cls, data: object) -> QualityReport | None:
        """Rebuild from a cached sidecar, or None if there is nothing usable there.

        Deliberately tolerant: the sidecar predates this field, so every render cached before
        this shipped has no ``quality`` key at all, and a report is an aid rather than a record.
        The same forgiveness ``PreviewService.diff`` already extends to a pre-issue-#24 cache.
        """
        if not isinstance(data, dict):
            return None
        raw = data.get("findings")
        if not isinstance(raw, list):
            return None
        findings = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                findings.append(
                    Finding(
                        id=str(item["id"]),
                        title=str(item["title"]),
                        severity=str(item["severity"]),
                        detail=str(item.get("detail", "")),
                        checklist_key=item.get("checklist_key") or None,
                        predicts_repo=bool(item.get("predicts_repo")),
                    )
                )
            except KeyError:
                continue
        return cls(findings=tuple(findings))

    def to_markdown(self, *, heading: str = "Automated checks") -> str:
        """The mirror comment's table.

        Reproduces ``mvp1.Report.to_markdown``'s three columns on purpose. A curator who has seen
        the repository's validation table on a pull request should not have to learn a second
        layout to read this one.
        """
        lines = [
            f"### {heading}",
            "",
            f"**Overall: {self.status.upper()}**",
            "",
            "| Status | Check | Detail |",
            "|---|---|---|",
        ]
        for f in self.findings:
            lines.append(f"| {f.severity.upper()} | {f.title} | {table_cell(f.detail)} |")
        lines.append("")
        lines.append(
            "These checks are automated and gate nothing. A curator decides."
        )
        return "\n".join(lines)


#: Exposed so callers can build an empty report without importing the dataclass.
EMPTY = QualityReport()

"""The user documentation at ``/docs``: how to submit, update, revise and curate a pathway.

The prose lives in ``templates/docs/``; the parts that describe the portal's own rules are built
here from the code that enforces them — the quality rules, the checklist, the status words and
the label names — so the help page cannot drift from what the portal does. A rule or checklist
item added without its ``explain`` / ``help`` text fails a test rather than shipping undocumented.

Public and anonymous: someone deciding whether to submit should be able to read it before they
sign in, and ``robots.txt`` lets crawlers in for the same reason.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.quality import RULES, Severity
from app.review.checklist import CURATION_CHECKLIST, build_checklist
from app.review.status import STATUS, queue_tabs

#: The rules that refuse a file outright, so it never becomes a pull request. Not declared on the
#: rule (a rule returns its severity per file), so it is listed here and a test holds it to what
#: the rules actually return on files built to trip each one.
REFUSING_RULES = frozenset(
    {"gpml.readable", "gpml.root", "gpml.namespace", "gpml.name", "gpml.organism"}
)

#: The severities in the words the review card and the submit form use, worst first, with what
#: each means for the person reading it.
SEVERITY_LADDER = (
    (
        Severity.BLOCK.value,
        "Blocked",
        "The file is refused and no pull request is opened. Fix it and choose the file again.",
    ),
    (
        Severity.FAIL.value,
        "Problem",
        "Something a curator will ask you to fix, or that will make the repository's own "
        "processing fail. The submission still goes through.",
    ),
    (
        Severity.WARN.value,
        "Worth a look",
        "Advice. Often fine as it is, but worth a moment before you submit.",
    ),
    (Severity.PASS.value, "Fine", "The check found nothing to report."),
    (
        Severity.NA.value,
        "Nothing to check",
        "The check does not apply, for example a comparison with an earlier version on a new "
        "pathway.",
    ),
)


def _rules() -> list[dict]:
    return [
        {
            "id": r.id,
            "title": r.title,
            "explain": r.explain,
            "predicts_repo": r.predicts_repo,
            "checklist_key": r.checklist.key if r.checklist else None,
            "refuses": r.id in REFUSING_RULES,
        }
        for r in RULES
    ]


def _checklist() -> list[dict]:
    # "Filled in automatically" read off what the checklist builder really does rather than
    # declared again: one item (one_pathway_per_pr) is answered from the pull request's file
    # list, not by an auto_check, and only build_checklist knows that.
    auto = {d.key for d in CURATION_CHECKLIST if d.auto_check is not None} | {
        item["key"]
        for item in build_checklist(pathway_paths=["pathways/WP1/WP1.gpml"], pipeline_mode=True)
        if item["auto"]
    }
    return [
        {
            "key": d.key,
            "label": d.label,
            "required": d.required,
            "auto": d.key in auto,
            "update_scoped": d.relevant_for_update is not None,
            "help": d.help,
        }
        for d in CURATION_CHECKLIST
    ]


def _statuses(pipeline_mode: bool) -> list[dict]:
    # The statuses this deployment can reach, in queue-tab order: the other mode's states would
    # describe things that never happen here.
    return [
        {
            "status": tab["status"],
            "label": tab["label"],
            "blurb": STATUS[tab["status"]].blurb,
        }
        for tab in queue_tabs(pipeline_mode=pipeline_mode, counts={})
    ]


def _labels(settings) -> list[dict]:
    """The pull request labels a submitter or curator will meet, with who sets each one."""
    return [
        {
            "name": settings.label_new_submission,
            "meaning": "Added by the portal to a pull request that adds a new pathway. "
            "Descriptive only.",
        },
        {
            "name": settings.label_edited_submission,
            "meaning": "Added by the portal to a pull request that changes a published pathway. "
            "Descriptive only.",
        },
        {
            "name": settings.label_accepted,
            "meaning": "Applied by the portal when a curator approves. It starts the "
            "repository's publish workflow.",
        },
        {
            "name": settings.label_rejected,
            "meaning": "Applied by the portal when a curator rejects. The repository then "
            "deletes the drafts and closes the pull request.",
        },
        {
            "name": "published",
            "meaning": "Set by the repository's publish workflow once it has published the "
            "pathway.",
        },
        {
            "name": "publish failed",
            "meaning": "Set by the publish workflow when it could not finish. A curator "
            "approves again to start a fresh run.",
        },
    ]


def docs_context(settings) -> dict:
    """Everything ``docs.html`` renders that comes from code rather than prose."""
    pipeline_mode = settings.is_pipeline_mode
    return {
        "rules": _rules(),
        "severities": [
            {"value": v, "word": w, "meaning": m} for v, w, m in SEVERITY_LADDER
        ],
        "checklist_items": _checklist(),
        "statuses": _statuses(pipeline_mode),
        "labels": _labels(settings) if pipeline_mode else [],
        # Who opens the pull request: "fork" (the submitter's fork), "bot" (the app's account) or
        # "user" (the submitter, straight on the repository). The prose about forks, and whether
        # the pull request is the submitter's own, depends on it.
        "submit_identity": settings.submit_identity,
    }


def register_docs(
    app: FastAPI,
    page_ctx: Callable[[Request], dict],
    *,
    templates,
    settings,
) -> None:
    """Add ``GET /docs``. Called once from ``build_app``, which owns the shared page context."""
    static = docs_context(settings)

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    def docs(request: Request):
        return templates.TemplateResponse(request, "docs.html", {**page_ctx(request), **static})

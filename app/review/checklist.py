"""The structured curation checklist (design §4.5) — modular, with automatable checks.

A reviewer works a fixed list of checks per pathway; approval requires every *required* item to be
marked ``pass``. Each item's state is stored per-review as JSON, so the template can change without
a schema migration.

Two kinds of automation, both **advisory** — a curator can always overrule an auto-derived state:

- ``auto_check`` — given the pathway's parsed metadata (``app.preview.metadata.CurationMetadata``),
  return a suggested state + explanatory note. E.g. "all data nodes have identifiers" → ``pass``.
  Items with an ``auto_check`` are pre-filled when the review is created and flagged ``auto`` so the
  UI shows they were machine-suggested.
- ``relevant_for_update`` — given (before, after) metadata, whether the check applies to *this*
  update. An update that didn't touch the title shouldn't ask the curator to re-check the title.
  Irrelevant items are auto-set to ``na`` and made non-blocking, with a note saying why.

## Adding or changing a check

1. Append a ``ChecklistItemDef(key, label, required=...)`` to ``CURATION_CHECKLIST``. ``key`` is a
   stable identifier the API uses to update the item — never reuse or rename one in place.
2. To automate it, pass ``auto_check=<fn(metadata) -> AutoResult>``. Keep it cheap and offline
   (no network) — it runs on every submission; verification that needs the network (does an id
   actually resolve?) belongs in a note with a link, not the auto_check.
3. To scope it to relevant updates, pass ``relevant_for_update=<fn(before, after) -> bool>``.
4. No migration is needed — the checklist is a JSON column. Existing open reviews keep the checklist
   they were created with; only new reviews pick up the change.
"""
from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass


class ChecklistState(enum.StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    NA = "na"


@dataclass(frozen=True)
class AutoResult:
    state: str
    note: str = ""


# Metadata is duck-typed (``app.preview.metadata.CurationMetadata``) to avoid an import cycle with
# the models, which import ``default_checklist`` from here.
MetaFn = Callable[[object], AutoResult]
RelevanceFn = Callable[[object, object], bool]


@dataclass(frozen=True)
class ChecklistItemDef:
    key: str
    label: str
    required: bool
    auto_check: MetaFn | None = None
    relevant_for_update: RelevanceFn | None = None


# ---- auto-check implementations (offline, cheap) -----------------------------------------
#
# Three of the four are adapters over ``app.quality``, which holds the same rules the submit form
# and the mirror comment run. Before that, the review card's "3 of 12 data nodes have no
# identifier" and the pull request's "Datanode annotation | WARN" were two separately-maintained
# descriptions of one problem, and only ever one of them got updated.


def _auto_from(key: str) -> MetaFn:
    """Turn the quality rule bound to ``key`` into a checklist auto-check.

    The threshold, the severity and the wording all live in ``app.quality``; this only translates
    the vocabulary, and the translation itself is declared on the rule so an item whose honest
    ceiling is "pending" says so once rather than in every caller.

    Imported inside the closure: ``app.models`` imports this module, so an eager import of
    ``app.quality`` would be reached from ``app.models`` and close a cycle through ``app.submit``.
    """

    def auto_check(m) -> AutoResult:
        from app.quality import checklist_result

        result = checklist_result(key, m)
        if result is None:
            return AutoResult(ChecklistState.PENDING.value, "")
        return AutoResult(*result)

    return auto_check


_auto_datanodes = _auto_from("datanodes_mapped")
_auto_references = _auto_from("references_valid")
_auto_ontology = _auto_from("ontology_tags")
#: New. "Meaningful" stays a human judgement — this can never return ``pass`` (see the binding in
#: ``app.quality.rules``) — but a missing description, or one below the length the repository asks
#: for, is a fact the app can state instead of leaving the curator to notice.
_auto_description = _auto_from("description_ok")


def _auto_naming(m) -> AutoResult:
    return AutoResult(ChecklistState.PASS.value, "WPID and file layout are assigned by the app.")


#: What the same item can honestly say where the *repository* names the file. The app commits a
#: placeholder there and the publish workflow assigns the id, so claiming the app got the naming
#: right is untrue — and, because ``refresh_pipeline_checks`` only fills items still pending, an
#: auto-pass here also locks out the check that can answer it: the repository's own record of
#: the slug, compared against the one the app predicted (``app.pipeline.drafts._naming_check``).
_PIPELINE_NAMING = AutoResult(
    ChecklistState.PENDING.value,
    "The repository files this pathway and assigns its WPID at publication. Its own record of "
    "the filename fills this in once its pipeline has run.",
)


# ---- update-relevance implementations ----------------------------------------------------


def _nodes_key(m) -> list:
    return sorted((n.label, n.database, n.identifier) for n in m.data_nodes)


def _refs_key(m) -> list:
    return sorted((r.database, r.identifier) for r in m.references)


def _onto_key(m) -> list:
    return sorted((t.ontology, t.identifier) for t in m.ontology_tags)


def _rel_datanodes(before, after) -> bool:
    return _nodes_key(before) != _nodes_key(after)


def _rel_description(before, after) -> bool:
    return (before.name, before.description) != (after.name, after.description)


def _rel_references(before, after) -> bool:
    return _refs_key(before) != _refs_key(after)


def _rel_ontology(before, after) -> bool:
    return _onto_key(before) != _onto_key(after)


# Ordered; keys are stable identifiers used by the API to update a single item.
#
# Deliberately aligned with the reviewer checklist the target repository appends to every pull
# request description (``1_on_pull_request.yml``, the ``Report on testing`` step). That list and
# this one used to overlap on four items and diverge on the rest, so a curator working in the
# portal and a curator working on the pull request were answering two different lists about one
# pathway. The mapping now:
#
#   repository says                                    | here
#   ---------------------------------------------------|--------------------------
#   Datanodes are annotated with database references    | datanodes_mapped
#   A description, 2-3 sentence overview of processes   | description_ok
#   At least one literature reference is provided       | references_valid
#   At least one pathway ontology term is included      | ontology_tags
#   Interactions are connected                          | interactions_connected
#   Pathway title conforms to the guidelines            | description_ok's note
#   ---------------------------------------------------|--------------------------
#   (not on the repository's list)                      | render_ok, naming_ok
#
# The last two stay because they are the portal's own business: the repository never sees the
# render, and in pipeline mode it owns the naming outright.
CURATION_CHECKLIST: list[ChecklistItemDef] = [
    # A human still eyeballs the rendered diagram — no auto_check.
    ChecklistItemDef("render_ok", "Rendered pathway displays correctly", required=True),
    ChecklistItemDef(
        "datanodes_mapped",
        "Data nodes are annotated with identifiers",
        required=True,
        auto_check=_auto_datanodes,
        relevant_for_update=_rel_datanodes,
    ),
    ChecklistItemDef(
        "references_valid",
        "References resolve",
        required=True,
        auto_check=_auto_references,
        relevant_for_update=_rel_references,
    ),
    ChecklistItemDef(
        "naming_ok", "WPID and file layout are correct", required=True, auto_check=_auto_naming
    ),
    # "Meaningful" is still a human judgement, and the auto-check cannot reach ``pass`` — but it
    # can say a description is missing or shorter than the repository will accept, which is a fact
    # rather than a judgement. Still scoped out of updates that leave both fields alone.
    ChecklistItemDef(
        "description_ok",
        "Title and description are meaningful",
        required=True,
        auto_check=_auto_description,
        relevant_for_update=_rel_description,
    ),
    # No auto_check, and there cannot be one: whether an interaction is *connected* is about the
    # graph the diagram draws, not about the annotation the parser reads. It is here because the
    # repository asks every reviewer for it and the portal was the only place not asking.
    ChecklistItemDef(
        "interactions_connected", "Interactions are connected", required=True
    ),
    ChecklistItemDef(
        "ontology_tags",
        "Ontology tags present",
        required=False,
        auto_check=_auto_ontology,
        relevant_for_update=_rel_ontology,
    ),
    # Derived from the pull request's file list rather than from the GPML, so it has no
    # ``auto_check`` (which takes metadata) and is filled in by ``build_checklist`` directly.
    # Always passes for a portal submission — the app commits one file — and exists for the
    # pull requests the app did not open, where changing several pathways at once is real.
    ChecklistItemDef(
        "one_pathway_per_pr", "One pathway per pull request", required=True
    ),
]

_VALID_KEYS = {item.key for item in CURATION_CHECKLIST}
_REQUIRED_BY_KEY = {item.key: item.required for item in CURATION_CHECKLIST}


def requirement_for(key: str, state: str) -> bool:
    """Whether an item in ``state`` blocks approval — the one place that decides it.

    The invariant is ``na`` never blocks. ``is_complete`` demands ``pass`` on every required
    item, so a required item sitting at ``na`` is an approval gate nothing can open: not by
    waiting, because ``na`` is already an answer, and not by re-uploading, because the new file
    re-derives the same ``na``. Issue #27 is what that costs — one required item auto-resolving
    to ``na`` disabled Approve on a pull request whose every other item passed, and the session
    that hit it before applied the repository's label by hand rather than finding out why.

    Three writers used to reach their own conclusion about this and only one of them was right,
    so this reads both ways: an item leaving ``na`` gets its declared requiredness back. Without
    that half, a curator clicking N/A and then Fail would leave a failed item not blocking
    anything, which is the same bug pointing the other way and considerably worse.
    """
    if state == ChecklistState.NA.value:
        return False
    return _REQUIRED_BY_KEY.get(key, False)


def _one_pathway_result(paths: list[str] | None) -> tuple[str, str]:
    """State and note for ``one_pathway_per_pr``.

    Unknown paths resolve to ``na`` — "nothing to check" — and **not** to pending. Pending on a
    required item blocks approval, which would wedge every review written before this item
    existed, and every row whose file list could not be read. ``na`` is the honest word and
    ``requirement_for`` already makes it non-blocking, restoring requiredness the moment it
    leaves ``na`` (issue #27).
    """
    if not paths:
        return (
            ChecklistState.NA.value,
            "Not checked: the pull request's file list was not read.",
        )
    if len(paths) == 1:
        return ChecklistState.PASS.value, ""
    named = ", ".join(f"`{p}`" for p in paths)
    return (
        ChecklistState.FAIL.value,
        f"This pull request changes {len(paths)} pathways ({named}). The repository publishes "
        f"one pathway per pull request — its own workflow greps a single GPML — so approving "
        f"this would publish at most one of them. Ask the author to split it.",
    )


def build_checklist(
    *,
    metadata=None,
    before=None,
    kind: str = "new",
    pipeline_mode: bool = False,
    pathway_paths: list[str] | None = None,
) -> list[dict]:
    """Build a review's checklist, applying auto-checks and (for updates) relevance scoping.

    ``metadata`` is the parsed *after* metadata; ``before`` the base-version metadata (updates
    only). With neither, every item is a blank ``pending`` — the plain template.

    ``pipeline_mode`` says the target repository owns naming and publication, which changes what
    one item can truthfully claim (see ``_PIPELINE_NAMING``).

    ``pathway_paths`` is every pathway GPML the pull request touches, known only where the file
    list was read — adoption. It is the one input here that does not come from the GPML.
    """
    items: list[dict] = []
    for d in CURATION_CHECKLIST:
        # Scope updates: an item whose subject didn't change is auto-N/A and non-blocking.
        if (
            kind == "update"
            and before is not None
            and metadata is not None
            and d.relevant_for_update is not None
            and not d.relevant_for_update(before, metadata)
        ):
            items.append(
                {
                    "key": d.key,
                    "label": d.label,
                    "required": requirement_for(d.key, ChecklistState.NA.value),
                    "state": ChecklistState.NA.value,
                    "note": "Not relevant: this update did not touch it.",
                    "auto": True,
                }
            )
            continue

        state, note, auto = ChecklistState.PENDING.value, "", False
        if d.key == "one_pathway_per_pr":
            state, note = _one_pathway_result(pathway_paths)
            auto = True
        elif d.key == "naming_ok" and pipeline_mode:
            state, note, auto = _PIPELINE_NAMING.state, _PIPELINE_NAMING.note, True
        elif metadata is not None and d.auto_check is not None:
            res = d.auto_check(metadata)
            state, note, auto = res.state, res.note, True
        items.append(
            {
                "key": d.key,
                "label": d.label,
                # An auto "N/A" means there is nothing to check, so it must not block approval.
                "required": requirement_for(d.key, state),
                "state": state,
                "note": note,
                "auto": auto,
            }
        )
    return items


def default_checklist() -> list[dict]:
    """The plain, all-pending checklist (SQLAlchemy column default; no metadata available)."""
    return build_checklist(kind="new", metadata=None)


def is_valid_key(key: str) -> bool:
    return key in _VALID_KEYS


def is_complete(checklist: list[dict]) -> bool:
    """True iff every required item is marked ``pass`` (non-required items may be anything)."""
    return all(
        item["state"] == ChecklistState.PASS.value
        for item in checklist
        if item.get("required")
    )

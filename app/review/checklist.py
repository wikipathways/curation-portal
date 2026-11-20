"""The structured curation checklist (design §4.5) — modular, with automated hints.

A reviewer works a fixed list of checks per pathway; approval requires every *required* item to be
marked ``pass``. Each item's state is stored per-review as JSON, so the template can change without
a schema migration.

**Only a curator sets an item's state.** Every item starts ``pending`` and stays there until a
curator answers it: not at creation, not when the repository's pipeline reports back, not when an
update is scoped. The app used to pre-fill states, and a live review (PR #91, 2026-11-20) showed
what that costs: ``ontology_tags`` sat at N/A on a pathway carrying two tags, because the
repository's own extractor had reported none and that reading overwrote the portal's. Even a
correct automated answer still has to be interpreted (are those tags the right ones?), so the
checklist now asks and the automation advises.

What the automation found is kept beside the state as a *hint*: ``suggested`` (the state it would
have chosen, or None) and ``hint`` (why). The card shows it as "Automated check: … Suggests Pass."
A later refresh may rewrite the hint; it never touches ``state``.

- ``auto_check`` — given the pathway's parsed metadata (``app.preview.metadata.CurationMetadata``),
  return a suggested state + explanatory note. E.g. "all data nodes have identifiers" → ``pass``.
- ``relevant_for_update`` — given (before, after) metadata, whether the check applies to *this*
  update. An item the update did not touch gets the hint "this update did not touch it" and the
  suggestion ``na``; a curator who agrees marks it N/A, which makes it non-blocking.

Item fields: ``key``, ``label``, ``required``, ``state``, ``note`` (the curator's own note),
``auto`` (False on every new row; True only on rows written before this rule, whose states the app
filled in), ``suggested``, ``hint`` and, for update-scoped items, ``scoped``.

## Adding or changing a check

1. Append a ``ChecklistItemDef(key, label, required=...)`` to ``CURATION_CHECKLIST``. ``key`` is a
   stable identifier the API uses to update the item — never reuse or rename one in place.
2. To give it a hint, pass ``auto_check=<fn(metadata) -> AutoResult>``. Keep it cheap and offline
   (no network) — it runs on every submission; verification that needs the network (does an id
   actually resolve?) belongs in a note with a link, not the auto_check.
3. To scope it to relevant updates, pass ``relevant_for_update=<fn(before, after) -> bool>``.
4. No migration is needed — the checklist is a JSON column. Existing reviews keep the checklist
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
#: ``(metadata, before=None, kind="new") -> AutoResult``. ``before`` is the base version's
#: metadata for an update, so a check can judge the change rather than the whole pathway.
MetaFn = Callable[..., AutoResult]
RelevanceFn = Callable[[object, object], bool]


@dataclass(frozen=True)
class ChecklistItemDef:
    key: str
    label: str
    required: bool
    auto_check: MetaFn | None = None
    relevant_for_update: RelevanceFn | None = None
    #: How a curator answers this item, in plain words. Shown on /docs beside the label; on the
    #: item rather than in the template so a new item cannot ship without one (a test requires it).
    help: str = ""


# ---- auto-check implementations (offline, cheap) -----------------------------------------
#
# All but the naming check are adapters over ``app.quality``, which holds the same rules the
# submit form and the mirror comment run. Before that, the review card's "3 of 12 data nodes have
# no identifier" and the pull request's "Datanode annotation | WARN" were two separately-maintained
# descriptions of one problem, and only ever one of them got updated.


def _auto_from(key: str) -> MetaFn:
    """Turn the quality rule bound to ``key`` into a checklist auto-check.

    The threshold, the severity and the wording all live in ``app.quality``; this only translates
    the vocabulary, and the translation itself is declared on the rule so an item whose honest
    ceiling is "pending" says so once rather than in every caller.

    Imported inside the closure: ``app.models`` imports this module, so an eager import of
    ``app.quality`` would be reached from ``app.models`` and close a cycle through ``app.submit``.
    """

    def auto_check(m, before=None, kind: str = "new") -> AutoResult:
        from app.quality import checklist_result

        result = checklist_result(key, m, before=before, kind=kind)
        if result is None:
            return AutoResult(ChecklistState.PENDING.value, "")
        return AutoResult(*result)

    return auto_check


_auto_datanodes = _auto_from("datanodes_mapped")
_auto_references = _auto_from("references_valid")
_auto_ontology = _auto_from("ontology_tags")
#: Whether every interaction's ends attach to something, read from the loose ends the metadata
#: parser lists. On an update only the ends the edit leaves loose count against it.
_auto_interactions = _auto_from("interactions_connected")
#: New. "Meaningful" stays a human judgement — this can never return ``pass`` (see the binding in
#: ``app.quality.rules``) — but a missing description, or one below the length the repository asks
#: for, is a fact the app can state instead of leaving the curator to notice.
_auto_description = _auto_from("description_ok")


def _auto_naming(m, before=None, kind: str = "new") -> AutoResult:
    return AutoResult(ChecklistState.PASS.value, "WPID and file layout are assigned by the app.")


#: What the same item can honestly say where the *repository* names the file. The app commits a
#: placeholder there and the publish workflow assigns the id, so suggesting the app got the naming
#: right is untrue. The hint that can answer it is the repository's own record of the slug,
#: compared against the one the app predicted (``app.pipeline.drafts._naming_check``), which
#: ``refresh_pipeline_checks`` writes once the pipeline has run.
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
    ChecklistItemDef(
        "render_ok",
        "Rendered pathway displays correctly",
        required=True,
        help=(
            "Look at the diagram on the review page. Are the nodes, labels and lines where you "
            "would expect, with nothing missing or piled on top of each other? The portal draws "
            "its own preview and can differ in detail from the published picture; when in doubt, "
            "compare with the repository's own render, linked once its pipeline has run."
        ),
    ),
    ChecklistItemDef(
        "datanodes_mapped",
        "Data nodes are annotated with identifiers",
        required=True,
        auto_check=_auto_datanodes,
        relevant_for_update=_rel_datanodes,
        help=(
            "Every data node carries a database identifier. The automated check counts the nodes "
            "without one; open the data node table and check that the identifiers point at the "
            "right genes, proteins and metabolites."
        ),
    ),
    ChecklistItemDef(
        "references_valid",
        "References resolve",
        required=True,
        auto_check=_auto_references,
        relevant_for_update=_rel_references,
        help=(
            "Open each listed reference and check that it exists and supports the pathway. "
            "The portal can count references in the file but not read them. Where the "
            "repository's own processing runs, the automated check says whether it resolved "
            "every reference the file declares."
        ),
    ),
    ChecklistItemDef(
        "naming_ok",
        "WPID and file layout are correct",
        required=True,
        auto_check=_auto_naming,
        help=(
            "Where the file sits and which identifier it gets. Where the repository publishes "
            "through its own workflows, it assigns both, and the automated check compares its "
            "record with what the portal expected once its pipeline has run; where the portal "
            "merges, the portal assigns them."
        ),
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
        help=(
            "Is the title specific, and does the description summarise the pathway in two or "
            "three sentences? The automated check can only say a description is missing or "
            "short; whether it is meaningful is your call."
        ),
    ),
    # It is here because the repository asks every reviewer for it and the portal was the only
    # place not asking. The hint comes from the parsed metadata, which lists every interaction end
    # that attaches to nothing (the same question the viewer's "Show unconnected line ends" asks
    # of the drawing). It cannot see an arrow attached to the wrong node, so the curator still
    # looks.
    ChecklistItemDef(
        "interactions_connected",
        "Interactions are connected",
        required=True,
        auto_check=_auto_interactions,
        help=(
            "Do the arrows really attach to the nodes they point at? A line that only ends near "
            "a node looks connected in the drawing but is not linked in the data, so the "
            "interaction is lost to anyone analysing the pathway. The automated check lists "
            "every interaction end attached to nothing, and for an update only the ones the edit "
            "adds count; whether each arrow is attached to the right node is your call."
        ),
    ),
    ChecklistItemDef(
        "ontology_tags",
        "Ontology tags present",
        required=False,
        auto_check=_auto_ontology,
        relevant_for_update=_rel_ontology,
        help=(
            "Optional. Whether the pathway carries suitable ontology tags (pathway, disease or "
            "cell type). The automated check counts them; whether they fit the pathway is your "
            "call. Never blocks approval."
        ),
    ),
    # Derived from the pull request's file list rather than from the GPML, so it has no
    # ``auto_check`` (which takes metadata) and its hint comes from ``build_checklist`` directly.
    # Always suggests pass for a portal submission — the app commits one file — and exists for
    # the pull requests the app did not open, where changing several pathways at once is real.
    ChecklistItemDef(
        "one_pathway_per_pr",
        "One pathway per pull request",
        required=True,
        help=(
            "The repository publishes one pathway per pull request. Always true for a "
            "submission made through the portal; a pull request opened on GitHub that changes "
            "several pathways fails here and has to be split."
        ),
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
    """Suggested state and hint for ``one_pathway_per_pr``.

    Unknown paths suggest ``na`` — "nothing to check" — since there is no file list to judge.
    The item itself still starts pending like every other: the curator decides, and marking it
    N/A takes it off the approval gate (``requirement_for``).
    """
    if not paths:
        return (
            ChecklistState.NA.value,
            "Not checked: the pull request's file list was not read.",
        )
    if len(paths) == 1:
        return ChecklistState.PASS.value, "The pull request changes one pathway."
    named = ", ".join(f"`{p}`" for p in paths)
    return (
        ChecklistState.FAIL.value,
        f"This pull request changes {len(paths)} pathways ({named}). The repository publishes "
        f"one pathway per pull request — its own workflow greps a single GPML — so approving "
        f"this would publish at most one of them. Ask the author to split it.",
    )


#: The hint on an update-scoped item the update left alone.
SCOPED_HINT = "This update did not touch it."


def _item(d: ChecklistItemDef, suggested: str | None, hint: str, *, scoped: bool = False) -> dict:
    """One fresh item: unanswered, with what the automation found beside it."""
    item = {
        "key": d.key,
        "label": d.label,
        "required": requirement_for(d.key, ChecklistState.PENDING.value),
        "state": ChecklistState.PENDING.value,
        "note": "",
        "auto": False,
        "suggested": suggested,
        "hint": hint,
    }
    if scoped:
        item["scoped"] = True
    return item


def build_checklist(
    *,
    metadata=None,
    before=None,
    kind: str = "new",
    pipeline_mode: bool = False,
    pathway_paths: list[str] | None = None,
) -> list[dict]:
    """Build a review's checklist: every item pending, with automated hints beside it.

    ``metadata`` is the parsed *after* metadata; ``before`` the base-version metadata (updates
    only). With neither, every item is a blank ``pending`` with no hint beyond what the file list
    says — the plain template.

    ``pipeline_mode`` says the target repository owns naming and publication, which changes what
    one item's hint can truthfully claim (see ``_PIPELINE_NAMING``).

    ``pathway_paths`` is every pathway GPML the pull request touches, known only where the file
    list was read — adoption. It is the one input here that does not come from the GPML.
    """
    items: list[dict] = []
    for d in CURATION_CHECKLIST:
        # Scope updates: an item whose subject didn't change suggests N/A. Only a suggestion: the
        # curator who agrees marks it N/A, and that is what takes it off the approval gate.
        if (
            kind == "update"
            and before is not None
            and metadata is not None
            and d.relevant_for_update is not None
            and not d.relevant_for_update(before, metadata)
        ):
            items.append(_item(d, ChecklistState.NA.value, SCOPED_HINT, scoped=True))
            continue

        suggested: str | None = None
        hint = ""
        if d.key == "one_pathway_per_pr":
            suggested, hint = _one_pathway_result(pathway_paths)
        elif d.key == "naming_ok" and pipeline_mode:
            suggested, hint = _PIPELINE_NAMING.state, _PIPELINE_NAMING.note
        elif metadata is not None and d.auto_check is not None:
            res = d.auto_check(metadata, before=before, kind=kind)
            suggested, hint = res.state, res.note
        items.append(_item(d, suggested, hint))
    return items


def hint_text(item: dict) -> str:
    """The automated hint for one item in words, or "" when there is none.

    "Automated check: 2 ontology tags. Suggests Pass." Shared by the review card and the mirror
    comment so both say the same sentence. A ``pending`` suggestion is information without a
    verdict, so it carries no "Suggests".
    """
    hint = (item.get("hint") or "").strip()
    suggested = item.get("suggested")
    word = _SUGGEST_WORD.get(suggested or "")
    if not hint and not word:
        return ""
    parts = ["Automated check:"]
    if hint:
        parts.append(hint if hint[-1] in ".!?" else hint + ".")
    if word:
        parts.append(f"Suggests {word}.")
    return " ".join(parts)


_SUGGEST_WORD = {
    ChecklistState.PASS.value: "Pass",
    ChecklistState.FAIL.value: "Fail",
    ChecklistState.NA.value: "N/A",
}


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

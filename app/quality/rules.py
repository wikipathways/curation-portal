"""One graded ruleset over an uploaded GPML, computed offline before the pull request exists.

The rules come from three places, and saying which is which is the point of the module:

1. **The four reasons the portal already refuses a file for** (``app.submit.gpml.validate_gpml``).
   They keep their exact wording — see ``_BLOCK_*`` below.
2. **The GPML-side checks from ``mvp1/validate_pathway.py``.** That validator grades thirteen
   things and rolls them up worst-wins, but it ships inside ``mvp1/pr-preview.yml`` to
   ``wikipathways-database``, and the live deployment targets a repository that runs its own
   numbered workflows and has no ``pr-preview.yml``. So the richest ruleset in the tree has never
   run on a real submission. About half of it needs only the uploaded file, and that half is here.
3. **The target repository's own ``testing`` job** (``docs/sandbox-pipeline.md`` §1) — title
   length, description length, and whether any data nodes changed. Its verdicts are written into
   the pull request *description*, which workflow 1 overwrites on every run and which this app
   deliberately never reads, so today they reach nobody. Ported here they become a *prediction*:
   the rules carry ``predicts_repo``, and the app can say what the repository is going to say
   before the submitter has waited for a run that fails more often than it succeeds.

Rule 3 is the one that can drift, because it is somebody else's rule reimplemented. That is why
the repository's own verdicts are also read back off a marker comment and compared: a mismatch is
the signal that these thresholds have fallen behind, and it is visible rather than silent.

## The import constraint

**Nothing in this package may import ``app.*`` at module scope.** There is a live cycle —
``app.models`` imports ``app.review.checklist`` for ``default_checklist``, ``app.review.checklist``
imports this package, and ``app.submit`` reaches ``app.models`` through the allocator. An eager
import closes it, and it surfaces as an ImportError at startup rather than in a test.

So metadata is duck-typed (the same device ``app.review.checklist`` and ``app.pipeline.drafts``
already use), and the one call into ``app.submit.gpml`` is function-local — the same deferral
``CurationService.refresh_pipeline_checks`` uses. ``test_quality_imports_no_app_package`` walks the
AST of every file here and fails if this slips.

## Adding a rule

Append a ``Rule`` to ``RULES``. ``id`` is stable and never renamed in place. If it answers a
checklist item, give it a ``ChecklistBinding`` rather than writing a second implementation in
``app.review.checklist`` — one description of a problem, in one place, is the whole point.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.quality.report import Finding, QualityReport, Severity

# ---- what the portal refuses, word for word -----------------------------------------------
#
# These four strings are interface, not prose. ``InvalidGpml.reasons`` is read into
# ``detail.errors`` by ``app.main`` and rendered to the submitter by ``describeError`` in
# ``static/app.js``. Rewording one changes what a person reads when their upload is rejected.
_BLOCK_ROOT = "no <Pathway> root element found"
_BLOCK_NAMESPACE = "missing GPML namespace (not a PathVisio/WikiPathways GPML file)"
_BLOCK_NAME = "root <Pathway> has no Name"
_BLOCK_ORGANISM = "root <Pathway> has no Organism (required for metadata + render)"

#: The namespace ``validate_gpml`` tests for by substring. Kept as a literal for the same reason it
#: was one there: GPML ships as 2013a and 2021 with different full namespace URIs, and this prefix
#: is what both share.
_GPML_NAMESPACE = "http://pathvisio.org/GPML"

#: An empty Biopax citation id. ``meta-data-action`` refuses a Citation with neither a valid xref
#: nor a url, so one of these kills the ``metadata`` job — and that job is where most of the target
#: repository's failed runs die, taking the submitter's tables and draft page with them. The
#: pipeline rewrites them to ``NA`` before running, so this is a warning about the source file.
#: Same pattern as ``mvp1/validate_pathway.py``'s, which mirrors the ``sed`` in the repo's own
#: ``on-gpml-change_local.sh``.
_EMPTY_BP_ID_RE = re.compile(r"<bp:ID\b[^>]*/>|<bp:ID\b[^>]*>\s*</bp:ID>")

#: The root ``<Graphics BoardWidth= BoardHeight= />`` that gives a pathway its canvas. Every GPML
#: PathVisio writes has one, and a hand-made file easily does not — the app's own renderer copes,
#: so nothing used to notice.
#:
#: Matched on ``BoardWidth`` rather than by walking to the root's ``<Graphics>`` child, because a
#: ``BoardWidth`` attribute appears nowhere else in the schema, and the cheap test is exact here.
_BOARD_RE = re.compile(r"<Graphics\b[^>]*\bBoardWidth=")

#: The line elements whose ``<Graphics>`` ``GPML2013aReader.readLineStyleProperty`` reads, and the
#: opening ``<Graphics>`` tag inside one. ``readLineElement`` is shared by both element types, so
#: both are checked — ``<Interaction>`` is the one measured (issue #26), ``<GraphicalLine>`` goes
#: through the identical call and is included rather than waiting for someone to hit it.
_LINE_ELEMENT_RE = re.compile(
    r"<(Interaction|GraphicalLine)\b([^>]*)>(.*?)</\1\s*>", re.DOTALL
)
_LINE_GRAPHICS_RE = re.compile(r"<Graphics\b([^>]*)>")
_GRAPH_ID_RE = re.compile(r'\bGraphId\s*=\s*"([^"]*)"')

# ---- the target repository's thresholds ----------------------------------------------------
#
# Read out of `1_on_pull_request.yml`'s `testing` job. Named constants rather than inline numbers
# because when that repository moves one, exactly one line here should move with it.
_MIN_TITLE_CHARS = 10
_MIN_DESCRIPTION_WORDS = 15
_EDIT_MAX_WORD_DIFF = 3
_EDIT_MAX_CHAR_DIFF = 10

#: How many offending labels a note names before it gives up and counts. Six is what
#: ``app.review.checklist`` has always shown; keeping it means the note wording does not change
#: for anyone as this takes over.
_MAX_NAMED = 6

# ---- checklist vocabulary -------------------------------------------------------------------
#
# The values of ``app.review.checklist.ChecklistState``, spelled here as plain strings because this
# package is a leaf and importing that module would close the cycle described above. They are kept
# in step by a test, not by an import — the same arrangement ``app.pipeline.drafts`` uses for the
# placeholder GPML filename.
_PENDING, _PASS, _FAIL, _NA = "pending", "pass", "fail", "na"

#: Severity → checklist state, the default. ``warn`` becomes ``pending``, not ``fail``: a warning is
#: advice, and ``fail`` would block approval on advice while ``pass`` would hide it. ``pending``
#: with the note attached says exactly what happened and leaves the decision where it belongs.
#: ``block`` is unreachable through this path — a blocking finding 422s before a review exists —
#: and is mapped anyway so the table is total.
_DEFAULT_STATE = {
    Severity.NA.value: _NA,
    Severity.PASS.value: _PASS,
    Severity.WARN.value: _PENDING,
    Severity.FAIL.value: _FAIL,
    Severity.BLOCK.value: _FAIL,
}


@dataclass(frozen=True)
class Subject:
    """Everything any rule may read, built once and passed to all of them.

    A struct rather than passing the GPML around: half the rules want the raw text and half want
    the parsed metadata, and building both once is what keeps a full evaluation to a single parse.
    That is the price of admission set by the checklist's own house rule, which requires auto-checks
    to be cheap and offline because they run on every submission.
    """

    text: str
    name: str | None = None
    organism: str | None = None
    author: str | None = None
    has_root: bool = False
    #: Duck-typed ``app.preview.metadata.CurationMetadata``.
    metadata: object | None = None
    #: The base version's metadata, for an update. None for a new pathway.
    before: object | None = None
    kind: str = "new"
    #: Whether the app's renderer could draw this file. None means nobody asked — see
    #: ``_check_drawable`` for why that is not the same as False.
    drawable: bool | None = None


@dataclass(frozen=True)
class ChecklistBinding:
    """How one rule reads as one checklist item.

    Declarative and per-rule rather than a branch in the mapping loop, because the exceptions are
    properties *of the item*: an optional item reads a warning as "nothing to check", and an item
    whose real answer needs the network can never exceed "pending" no matter how clean the file is.
    """

    key: str
    #: Overrides on ``_DEFAULT_STATE``, keyed by severity value.
    state_for: dict[str, str] = field(default_factory=dict)

    def state(self, severity: str) -> str:
        return self.state_for.get(severity, _DEFAULT_STATE.get(severity, _PENDING))


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    #: ``(Subject) -> (severity, detail)``, or None to emit no finding at all. Returning a pair
    #: rather than a Finding keeps id, title and binding declared once, in the table, instead of
    #: repeated in every body.
    check: Callable[[Subject], tuple[str, str] | None]
    predicts_repo: bool = False
    #: False where the rule reads only ``metadata`` / ``before`` / ``kind``, so it can run as a
    #: checklist auto-check in the places where no GPML text is in hand.
    needs_text: bool = True
    checklist: ChecklistBinding | None = None


# ---- helpers --------------------------------------------------------------------------------


def _nodes(subject: Subject) -> list:
    return list(getattr(subject.metadata, "data_nodes", []) or [])


def _named(labels: list[str]) -> str:
    shown = ", ".join(labels[:_MAX_NAMED])
    return shown + ("…" if len(labels) > _MAX_NAMED else "")


def _node_dicts(metadata: object) -> list[dict]:
    """A data-node list in the shape ``app.preview.diff.diff_nodes`` reads.

    ``diff_nodes`` takes plain dicts through ``.get()``, so the parsed metadata's nodes can be fed
    to it directly. It carries no geometry, which is exactly right here: ``_moved`` returns False
    without centres, so a pure metadata diff reports annotation and label changes and never claims
    something moved on evidence it does not have.
    """
    out = []
    for n in getattr(metadata, "data_nodes", []) or []:
        out.append(
            {
                "label": getattr(n, "label", "") or "",
                "type": getattr(n, "type", "") or "",
                "database": getattr(n, "database", "") or "",
                "identifier": getattr(n, "identifier", "") or "",
            }
        )
    return out


def _words(text: str | None) -> int:
    return len((text or "").split())


# ---- rule bodies ----------------------------------------------------------------------------


def _check_root(s: Subject) -> tuple[str, str] | None:
    if not s.has_root:
        return (Severity.BLOCK.value, _BLOCK_ROOT)
    return (Severity.PASS.value, "The file has a <Pathway> root element.")


def _check_namespace(s: Subject) -> tuple[str, str] | None:
    if _GPML_NAMESPACE not in s.text:
        return (Severity.BLOCK.value, _BLOCK_NAMESPACE)
    return (Severity.PASS.value, "Declared as a PathVisio/WikiPathways GPML document.")


def _check_name(s: Subject) -> tuple[str, str] | None:
    if not s.name:
        return (Severity.BLOCK.value, _BLOCK_NAME)
    return (Severity.PASS.value, f"Titled “{s.name}”.")


def _check_organism(s: Subject) -> tuple[str, str] | None:
    """Blocking here, where ``mvp1`` only warns.

    Deliberate, and the parity test excludes this rule because of it. ``mvp1`` runs after the fact
    on a file already committed, so refusing it would achieve nothing; this runs before the file
    exists anywhere, and both the renderer and ``meta-data-action`` key off Organism. Letting one
    through buys a broken run instead of a clear refusal.
    """
    if not s.organism:
        return (Severity.BLOCK.value, _BLOCK_ORGANISM)
    return (Severity.PASS.value, f"Organism is {s.organism}.")


def _check_author(s: Subject) -> tuple[str, str] | None:
    """A warning about the source file, not a prediction about this submission.

    ``meta-data-action`` dereferences the GPML's ``Author`` attribute unconditionally and crashes
    without it, and the ``metadata`` job is where most of the repository's failed runs die. The
    portal repairs it on the way out — ``assign_wpid`` fills in the submitter where the upload has
    none — so this never blocks. It is worth saying because the fix belongs in the file the
    submitter keeps, not only in the copy the portal commits.
    """
    if not s.author:
        return (
            Severity.WARN.value,
            "The GPML carries no Author attribute. The portal fills in your name when it "
            "commits, but the repository's metadata step crashes on a file that has none, so it "
            "is worth setting in your own copy.",
        )
    return (Severity.PASS.value, f"Author is {s.author}.")


def _check_citation_ids(s: Subject) -> tuple[str, str] | None:
    count = len(_EMPTY_BP_ID_RE.findall(s.text))
    if count:
        return (
            Severity.WARN.value,
            f"{count} empty <bp:ID> element{'' if count == 1 else 's'}. The repository rewrites "
            "these to NA so its metadata step can run, but they are worth fixing at the source — "
            "an empty citation id is the most common reason that step fails outright.",
        )
    return (Severity.PASS.value, "No empty citation ids.")


def _check_board(s: Subject) -> tuple[str, str] | None:
    """No root ``<Graphics>`` board, and the repository's metadata step dies on the file.

    Measured rather than reasoned, on 2026-08-03 against `marvinm2/sandbox-wp-db`. Two submissions
    of the same pathway, differing only in this one element:

    - without it, run 30798868327 — ``metadata`` failed with
      ``ConverterException: NullPointerException`` out of ``GPML2013aReader.readPathway``;
    - with it, run 30800359486 — ``metadata`` succeeded.

    ``fail`` rather than ``warn`` because the consequence is total for the submitter: ``metadata``
    is the job the whole downstream fan-out depends on, so they lose their identifier table, their
    bibliography and their draft page, and the only trace is a Java stack trace several clicks into
    the Actions tab. The portal's own renderer draws the file quite happily, which is exactly why
    nothing caught this before.
    """
    if not _BOARD_RE.search(s.text):
        return (
            Severity.FAIL.value,
            "The <Pathway> element has no <Graphics> board (BoardWidth / BoardHeight). The "
            "repository's metadata step cannot read a pathway without one and fails outright, "
            "which costs the identifier and reference tables and the draft page. PathVisio always "
            "writes it; a hand-built GPML often does not.",
        )
    return (Severity.PASS.value, "The pathway declares a canvas.")


def _check_line_thickness(s: Subject) -> tuple[str, str] | None:
    """An interaction whose ``<Graphics>`` has no ``LineThickness`` kills the same metadata job.

    Sibling of ``_check_board``, and measured the same way — one variable, two runs on
    `marvinm2/sandbox-wp-db`, 2026-08-03:

    - run 30827814897, `demo/pathway_new.gpml` as it stood — ``metadata`` failed with
      ``ConverterException: NullPointerException`` out of ``GPML2013aReader.readLineStyleProperty``;
    - run 30829825691, the same file with ``LineThickness="1.0"`` added to its two interaction
      ``<Graphics>`` elements and nothing else changed — all ten jobs green.

    ``fail`` for the reason ``gpml.board`` is: ``metadata`` is what the whole downstream fan-out
    depends on, so the submitter loses the identifier table, the bibliography, the draft page and
    the ``testing`` marker comment the portal reads its verdicts back from — and the only trace is
    a Java stack trace several clicks into the Actions tab.

    A line element with no ``<Graphics>`` child at all is counted too. The reader reaches the same
    property either way, and a file in that shape is broken for a superset of these reasons.
    """
    offenders: list[str] = []
    for position, match in enumerate(_LINE_ELEMENT_RE.finditer(s.text), start=1):
        tag, attrs, body = match.group(1), match.group(2), match.group(3)
        graphics = _LINE_GRAPHICS_RE.search(body)
        if graphics is not None and "LineThickness" in graphics.group(1):
            continue
        # Interactions carry no TextLabel, so a GraphId is the only name one can have — and a
        # hand-built GPML, which is the kind that has this defect, often sets neither. Falling
        # back to the position in document order beats repeating "an unnamed Interaction" once
        # per offender, which is what it said first and told nobody which ones to fix.
        found = _GRAPH_ID_RE.search(attrs)
        offenders.append(f"{tag} {found.group(1)}" if found else f"{tag} #{position}")
    if offenders:
        n = len(offenders)
        return (
            Severity.FAIL.value,
            f"{n} line element{'' if n == 1 else 's'} have a <Graphics> with no LineThickness "
            f"({_named(offenders)}). The repository's metadata step dereferences that attribute "
            "and fails outright without it, which costs the identifier and reference tables and "
            "the draft page. PathVisio always writes it; a hand-built GPML often does not.",
        )
    return (Severity.PASS.value, "Every interaction declares a line thickness.")


def _check_drawable(s: Subject) -> tuple[str, str] | None:
    """Whether the app's own renderer could draw this file.

    Takes the answer as a parameter instead of calling the renderer. Running it inside a rule would
    break the offline-and-cheap rule the checklist sets for auto-checks — it walks the whole tree,
    twice for an update — so the only caller that supplies it is ``render_local``, at the one
    moment the answer is already in hand and costs nothing.

    ``None`` means nobody asked, and the rule then says nothing at all rather than guessing. A
    dishonest "renderable" is worse than a missing line, because the render is the artifact the
    whole review is built around.
    """
    if s.drawable is None:
        return None
    if s.drawable:
        return (Severity.PASS.value, "The portal drew this pathway from its GPML.")
    return (
        Severity.FAIL.value,
        "The portal could not draw this pathway from its GPML. The review will have no diagram, "
        "which is the one thing a curator cannot work without.",
    )


def _check_datanodes_present(s: Subject) -> tuple[str, str] | None:
    """Counted from the parsed metadata, not by matching ``<DataNode`` in the text.

    ``mvp1`` counts occurrences of the tag because it refuses a full XML parse — it has to cope
    with 2013a and 2021 in one script. The app has already parsed. The two agree on every
    well-formed file and disagree only on a commented-out ``<DataNode``, where this one is right.
    """
    nodes = _nodes(s)
    if not nodes:
        return (Severity.FAIL.value, "The pathway contains no data nodes, so it is empty.")
    return (
        Severity.PASS.value,
        f"{len(nodes)} data node{'' if len(nodes) == 1 else 's'}.",
    )


def _check_datanode_annotation(s: Subject) -> tuple[str, str] | None:
    nodes = _nodes(s)
    if not nodes:
        return (Severity.NA.value, "No data nodes in this pathway.")
    unmapped = [
        getattr(n, "label", "") or "(unlabeled)"
        for n in nodes
        if not getattr(n, "identifier", "")
    ]
    if not unmapped:
        return (Severity.PASS.value, f"All {len(nodes)} data nodes carry an identifier.")
    # Any unmapped node fails, which is stricter than ``mvp1/validate_pathway.py`` — it warns
    # unless *nothing* resolved. The app's answer is kept because the app is in a position to be
    # strict: this runs before the submission exists, where an unannotated node is still cheap to
    # fix, and a curator can always overrule the state. The parity test excludes this rule.
    return (
        Severity.FAIL.value,
        f"{len(unmapped)} of {len(nodes)} data nodes have no identifier: {_named(unmapped)}",
    )


def _check_datanode_changes(s: Subject) -> tuple[str, str] | None:
    """The repository's third test: did any data nodes change?

    Reuses ``app.preview.diff.diff_nodes`` rather than re-deriving the comparison. That function
    is the app's answer to the same question for the before/after overlay, and having the review
    card's coloured hotspots and this line disagree about how many nodes changed would be worse
    than having neither.
    """
    if s.kind != "update" or s.before is None:
        # The repository reports "all nodes are new" as review-required on every new pathway. That
        # is worth telling a curator and useless to a submitter — there is nothing to fix — so it
        # is carried as the detail of an `na` rather than as a warning that would fire on every
        # single submission and teach people to skim past the list.
        return (
            Severity.NA.value,
            "Every node is new, so there is nothing to compare against. The repository reports "
            "this as review-required on any new pathway.",
        )
    from app.preview.diff import diff_nodes

    summary = diff_nodes(_node_dicts(s.before), _node_dicts(s.metadata)).summary
    changed = {k: v for k, v in summary.items() if k != "unchanged" and v}
    if not changed:
        return (Severity.PASS.value, "No data nodes were added, changed or removed.")
    parts = ", ".join(f"{v} {k}" for k, v in changed.items())
    return (
        Severity.WARN.value,
        f"Data nodes changed ({parts}). The repository flags any data-node change for review.",
    )


def _check_title_length(s: Subject) -> tuple[str, str] | None:
    if not s.name:
        return None  # already a blocking finding; saying it twice helps nobody
    if len(s.name) < _MIN_TITLE_CHARS:
        return (
            Severity.WARN.value,
            f"The title is {len(s.name)} characters. The repository asks for at least "
            f"{_MIN_TITLE_CHARS} and will flag this one for review.",
        )
    return (Severity.PASS.value, f"{len(s.name)} characters.")


def _check_description(s: Subject) -> tuple[str, str] | None:
    """The repository's second test, both arms.

    A new pathway wants at least fifteen words. An *edit* is measured the other way round — the
    repository flags a description that changed by more than three words or ten characters,
    because on an edit an unexplained rewrite of the description is the thing worth looking at.
    """
    description = getattr(s.metadata, "description", None)
    if not description:
        return (
            Severity.FAIL.value,
            "The pathway has no description. The repository asks for a two to three sentence "
            "overview of the processes it describes.",
        )
    if s.kind == "update" and s.before is not None:
        was = getattr(s.before, "description", "") or ""
        word_diff = abs(_words(description) - _words(was))
        char_diff = abs(len(description) - len(was))
        if word_diff > _EDIT_MAX_WORD_DIFF or char_diff > _EDIT_MAX_CHAR_DIFF:
            return (
                Severity.WARN.value,
                f"The description changed by {word_diff} words and {char_diff} characters. The "
                "repository flags an edit that rewrites it by more than "
                f"{_EDIT_MAX_WORD_DIFF} words or {_EDIT_MAX_CHAR_DIFF} characters.",
            )
        return (Severity.PASS.value, "The description is essentially unchanged by this edit.")
    count = _words(description)
    if count < _MIN_DESCRIPTION_WORDS:
        return (
            Severity.WARN.value,
            f"The description is {count} words. The repository asks for at least "
            f"{_MIN_DESCRIPTION_WORDS} and will flag this one for review.",
        )
    return (Severity.PASS.value, f"{count} words.")


def _check_references(s: Subject) -> tuple[str, str] | None:
    """Presence only. Whether each one *resolves* needs the network, so it is never claimed here.

    The checklist binding keeps this at ``pending`` even when it passes, for that reason: the
    review card lists every reference as a clickable link, and the honest instruction is to open
    them, not a green tick derived from counting.

    **No references is ``warn``, not ``na``.** It read ``na`` — "nothing to check" — until issue
    #27, which is the wrong word for it: the repository's own reviewer checklist asks for "at
    least one literature reference", so a pathway with none has not satisfied the check, it has
    failed to answer it. The practical consequence was worse than the wording. ``na`` makes the
    item non-blocking, so the one required item that a curator most needs to weigh was the one
    silently dropped from the approval gate.
    """
    declared = list(getattr(s.metadata, "references", []) or [])
    # The *cited* count, not the total. A GPML can carry a ``<bp:PublicationXref>`` that no
    # ``<BiopaxRef>`` anywhere points at — PathVisio leaves them behind when an annotation is
    # removed, and a hand-written file can simply forget the citation — and every downstream
    # generator emits only the cited ones, so the repository's ``refs.tsv`` and
    # ``bibliography.tsv`` both come back empty while this rule announced a reference.
    #
    # Both statements then appear on the same review page, disagreeing: "Open the 1 reference
    # listed above" under Automated checks, and "The GPML declares no literature references" on
    # the checklist, which has read the cited count since the pipeline check was wired. Measured
    # on PR #42, 2026-08-14, on a fixture whose reference had been *added to answer this very
    # rule* and was never cited — so the rule was validated against a file encoding the same
    # misunderstanding it contains.
    cited = getattr(s.metadata, "cited_reference_count", None)
    n = cited if isinstance(cited, int) else len(declared)
    if not n:
        uncited = (
            f" The file declares {len(declared)} but no <BiopaxRef> cites "
            f"{'it' if len(declared) == 1 else 'any of them'}, so nothing downstream will see "
            f"{'it' if len(declared) == 1 else 'them'}."
            if declared
            else ""
        )
        return (
            Severity.WARN.value,
            "No literature references. The repository asks for at least one, so this is for a "
            "curator to weigh rather than nothing to check." + uncited,
        )
    return (
        Severity.PASS.value,
        f"Open the {n} reference{'' if n == 1 else 's'} listed above and check "
        f"{'it resolves' if n == 1 else 'they resolve'}.",
    )


def _check_ontology(s: Subject) -> tuple[str, str] | None:
    tags = list(getattr(s.metadata, "ontology_tags", []) or [])
    if not tags:
        return (Severity.NA.value, "No ontology tags (optional).")
    n = len(tags)
    return (Severity.PASS.value, f"{n} ontology tag{'' if n == 1 else 's'}.")


# ---- the table -------------------------------------------------------------------------------
#
# Declaration order is presentation order: what refuses the file, then what breaks the
# repository's pipeline, then what a curator judges. The same most-to-least-urgent convention the
# diff summary follows.

RULES: tuple[Rule, ...] = (
    Rule("gpml.root", "GPML root element", _check_root),
    Rule("gpml.namespace", "GPML namespace", _check_namespace),
    Rule("gpml.name", "Title", _check_name),
    Rule("gpml.organism", "Organism", _check_organism),
    Rule("render.drawable", "Renderable", _check_drawable, needs_text=False),
    Rule("gpml.board", "Pathway canvas", _check_board),
    Rule("gpml.line_thickness", "Interaction line thickness", _check_line_thickness),
    Rule("gpml.author", "Authors", _check_author),
    Rule("gpml.citation_ids", "Empty citation IDs", _check_citation_ids),
    Rule("gpml.datanodes", "DataNodes in GPML", _check_datanodes_present, needs_text=False),
    Rule(
        "content.datanode_annotation",
        "Datanode annotation",
        _check_datanode_annotation,
        needs_text=False,
        checklist=ChecklistBinding("datanodes_mapped"),
    ),
    Rule(
        "content.title_length",
        "Title length",
        _check_title_length,
        predicts_repo=True,
    ),
    Rule(
        "content.description",
        "Description",
        _check_description,
        predicts_repo=True,
        needs_text=False,
        # Never better than pending, even on a clean file. ``refresh_pipeline_checks`` only writes
        # items still pending, so any state the app puts here pre-empts the repository's own
        # description check — and that one is strictly better, because it quotes what the
        # repository's extractor pulled out, which is the text that will appear on the published
        # page and can differ from the GPML comment. Failing is still worth saying: where there is
        # no description at all, both would say the same thing and the repository may never run.
        checklist=ChecklistBinding(
            "description_ok",
            state_for={Severity.PASS.value: _PENDING, Severity.WARN.value: _PENDING},
        ),
    ),
    Rule(
        "content.datanode_changes",
        "Data nodes changed",
        _check_datanode_changes,
        predicts_repo=True,
        needs_text=False,
    ),
    Rule(
        "content.references",
        "References",
        _check_references,
        needs_text=False,
        # Pass reads as pending for the reason in ``_check_references``: resolvability needs the
        # network, and counting is not checking.
        checklist=ChecklistBinding(
            "references_valid", state_for={Severity.PASS.value: _PENDING}
        ),
    ),
    Rule(
        "content.ontology",
        "Ontology tags",
        _check_ontology,
        needs_text=False,
        checklist=ChecklistBinding("ontology_tags"),
    ),
)

_BY_CHECKLIST_KEY: dict[str, Rule] = {
    r.checklist.key: r for r in RULES if r.checklist is not None
}


def build_subject(
    gpml: bytes | str | None = None,
    *,
    metadata: object | None = None,
    before: object | None = None,
    kind: str = "new",
    drawable: bool | None = None,
) -> Subject:
    """Parse once, into the struct every rule reads.

    ``metadata`` is parsed from ``gpml`` when not supplied. Both imports are function-local: see
    the module docstring for the cycle they would otherwise close.
    """
    text = ""
    if gpml is not None:
        text = gpml.decode("utf-8", errors="replace") if isinstance(gpml, bytes) else gpml

    name = organism = author = None
    has_root = False
    if text:
        from app.submit.gpml import InvalidGpml, parse_pathway_meta

        try:
            root = parse_pathway_meta(text)
        except InvalidGpml:
            root = None
        if root is not None:
            has_root = True
            name, organism, author = root.name, root.organism, root.author

    if metadata is None and text:
        from app.preview.metadata import parse_curation_metadata

        metadata = parse_curation_metadata(text)
    if metadata is not None and name is None:
        # A metadata-only subject still knows the title and organism; the root tag is simply not
        # in hand. Without this the title-length rule would report every such subject as untitled.
        name = getattr(metadata, "name", None)
        organism = getattr(metadata, "organism", None)

    return Subject(
        text=text,
        name=name,
        organism=organism,
        author=author,
        has_root=has_root,
        metadata=metadata,
        before=before,
        kind=kind,
        drawable=drawable,
    )


def evaluate(subject: Subject, *, text_available: bool = True) -> QualityReport:
    """Run every applicable rule over ``subject``.

    Two things are skipped rather than answered wrongly:

    - With no ``<Pathway>`` root, only that finding is emitted. Everything downstream would be
      reporting on a document that is not a pathway, and today ``validate_gpml`` raises from the
      root parse before it ever tests the namespace — so listing four reasons for a file of HTML
      would be a change in behaviour dressed up as extra helpfulness.
    - With no GPML text in hand (a checklist auto-check working from cached metadata), rules that
      read the raw document are skipped. They would all report their absence branch, and a warning
      derived from a file nobody looked at is a false one.
    """
    findings: list[Finding] = []
    for rule in RULES:
        if rule.needs_text and not text_available:
            continue
        outcome = rule.check(subject)
        if outcome is None:
            continue
        severity, detail = outcome
        findings.append(
            Finding(
                id=rule.id,
                title=rule.title,
                severity=severity,
                detail=detail,
                checklist_key=rule.checklist.key if rule.checklist else None,
                predicts_repo=rule.predicts_repo,
            )
        )
        if rule.id == "gpml.root" and severity == Severity.BLOCK.value:
            break
    return QualityReport(findings=tuple(findings))


def checklist_state_for(key: str, severity: str) -> str:
    """The checklist state one severity means for one item, honouring that item's binding."""
    rule = _BY_CHECKLIST_KEY.get(key)
    if rule is None or rule.checklist is None:
        return _DEFAULT_STATE.get(severity, _PENDING)
    return rule.checklist.state(severity)


def checklist_keys() -> tuple[str, ...]:
    """Every checklist item some rule can answer. Used by the cross-module consistency test."""
    return tuple(_BY_CHECKLIST_KEY)

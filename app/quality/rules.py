"""One graded ruleset over an uploaded GPML, computed offline before the pull request exists.

The rules come from three places, and saying which is which is the point of the module:

1. **The four reasons the portal already refuses a file for** (``app.submit.gpml.validate_gpml``).
   They keep their exact wording — see ``_BLOCK_*`` below.
2. **The GPML-side checks from ``mvp1/validate_pathway.py``.** That validator grades thirteen
   things and rolls them up worst-wins, but it ships inside ``mvp1/pr-preview.yml`` to
   ``wikipathways-database``, and the live deployment targets a repository that runs its own
   numbered workflows and has no ``pr-preview.yml``. So the richest ruleset in the tree has never
   run on a real submission. About half of it needs only the uploaded file, and that half is here.
   (``mvp1/`` itself was removed on 2026-11-13; it is in the history up to commit ``e96386d``.)
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
import xml.etree.ElementTree as ET
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

#: Why a file that is not readable XML is refused. Unlike the four above these are new, so they
#: are written for the person reading them in the upload form: no parser vocabulary, and the
#: one thing they can do about it. ``{where}`` is filled in from the parser's position.
_BLOCK_UNREADABLE = (
    "This file is not valid XML, so it cannot be read as a GPML pathway. The first problem is "
    "{where}. This usually means a tag that is opened but never closed, or a stray “<” or “&” "
    "character. Saving the pathway again from PathVisio writes a valid file."
)
_BLOCK_DOCTYPE = (
    "This file contains a document type declaration (<!DOCTYPE …> or <!ENTITY …>). GPML never "
    "uses one and the portal does not accept files that do. Remove it and upload the file again."
)

#: Markup that only a document type declaration can carry. Refused before parsing rather than
#: handed to the parser, because entity definitions are how a few hundred bytes of XML expand to
#: gigabytes, and nothing a pathway needs is expressed with them. A plain substring test, so it is
#: linear whatever the input looks like.
_DTD_MARKERS = ("<!DOCTYPE", "<!ENTITY")

#: An empty Biopax citation id. ``meta-data-action`` refuses a Citation with neither a valid xref
#: nor a url, so one of these kills the ``metadata`` job — and that job is where most of the target
#: repository's failed runs die, taking the submitter's tables and draft page with them. The
#: pipeline rewrites them to ``NA`` before running, so this is a warning about the source file.
#: Same pattern as ``mvp1/validate_pathway.py``'s, which mirrors the ``sed`` in the repo's own
#: ``on-gpml-change_local.sh`` — kept as text matching for that parity, the ``sed`` is text too.
#:
#: ``[^<>]*`` rather than ``[^>]*``: the two agree inside any real tag, since an attribute value
#: may not contain ``<``, but ``[^>]*`` let every unterminated ``<bp:ID`` (inside a comment, say)
#: scan on to the next ``>`` however far away, which made a crafted file quadratic. Stopping at
#: the next ``<`` bounds each attempt by the distance to the next tag.
_EMPTY_BP_ID_RE = re.compile(r"<bp:ID\b[^<>]*/>|<bp:ID\b[^<>]*>\s*</bp:ID>")

#: The line elements whose ``<Graphics>`` ``GPML2013aReader.readLineStyleProperty`` reads.
#: ``readLineElement`` is shared by both element types, so both are checked — ``<Interaction>`` is
#: the one measured (issue #26), ``<GraphicalLine>`` goes through the identical call and is
#: included rather than waiting for someone to hit it.
_LINE_ELEMENTS = frozenset({"Interaction", "GraphicalLine"})

# ---- the target repository's thresholds ----------------------------------------------------
#
# Read out of `1_on_pull_request.yml`'s `testing` job. Named constants rather than inline numbers
# because when that repository moves one, exactly one line here should move with it.
_MIN_TITLE_CHARS = 10
_MIN_DESCRIPTION_WORDS = 15
_EDIT_MAX_WORD_DIFF = 3
_EDIT_MAX_CHAR_DIFF = 10


def description_change(was: str | None, now: str | None) -> dict:
    """How an edit changed the description, measured the way the repository measures it.

    The repository compares lengths, not wording: the difference in word count and in character
    count, each against its own limit. ``word_delta``/``char_delta`` are signed (positive when the
    description grew); ``over`` is the repository's verdict, true when either difference passes its
    limit. One measurement for the rule and for the change summary on the card
    (``app.preview.diff.diff_metadata``), so the two cannot describe the same edit differently.
    """
    was = (was or "").strip()
    now = (now or "").strip()
    word_delta = _words(now) - _words(was)
    char_delta = len(now) - len(was)
    return {
        "identical": was == now,
        "word_delta": word_delta,
        "char_delta": char_delta,
        "over": abs(word_delta) > _EDIT_MAX_WORD_DIFF or abs(char_delta) > _EDIT_MAX_CHAR_DIFF,
        "max_words": _EDIT_MAX_WORD_DIFF,
        "max_chars": _EDIT_MAX_CHAR_DIFF,
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"

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
    #: The parsed document's root element, when ``text`` is readable XML. The rules that look at
    #: the document's structure walk this rather than pattern-matching the text.
    tree: ET.Element | None = None
    #: Why ``text`` could not be read as XML, in words for the submitter, or None if it could (or
    #: there is no text). See ``_read_tree``.
    unreadable: str | None = None
    #: ``portal`` for an upload, ``adopted`` for a pull request opened on GitHub. It changes what
    #: a missing attribute means: the portal repairs its own uploads before committing them, and
    #: nothing repairs an adopted pull request's file before the repository's workflows read it.
    origin: str = "portal"
    #: The root element's ``Version`` attribute, as written.
    version: str | None = None
    #: The WPID this file is being submitted as, when known: the pathway an update targets, or
    #: the id an adopted pull request's directory names. None for a new upload.
    target_wpid: int | None = None


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
    #: What the rule asks of a pathway and why, in words for the biologist reading /docs. Kept on
    #: the rule rather than in the template so a new rule cannot ship without its explanation (a
    #: test requires it) and the help page cannot describe a rule that no longer exists.
    explain: str = ""


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


def _localname(tag: object) -> str:
    """``{namespace}Name`` → ``Name``. GPML elements live in a default namespace that differs
    between 2013a and 2021, so every structural test here matches on the local name."""
    if not isinstance(tag, str):
        return ""  # comments and processing instructions carry a callable here
    return tag.rsplit("}", 1)[-1]


def _read_tree(text: str) -> tuple[ET.Element | None, str | None]:
    """Parse ``text`` once, returning ``(root, None)`` or ``(None, why it could not be read)``.

    This is the gate every structural rule sits behind. Before it, nothing checked that an upload
    was XML at all, and the rules that read structure did it with patterns that, on a file full of
    unclosed tags, rescanned the rest of the document from every one of them: 80 KB of
    ``<Interaction>`` took five seconds to grade, holding the interpreter the whole time, and was
    then reported as valid. A parser reads any input in one pass.

    Leading whitespace is skipped before parsing. Strictly an XML declaration must come first,
    but ``assign_wpid_str`` already strips that whitespace when it rewrites the declaration, so
    the committed file is valid — refusing the upload for it would refuse something the portal
    repairs anyway.
    """
    body = text.lstrip()
    if any(marker in body for marker in _DTD_MARKERS):
        return None, _BLOCK_DOCTYPE
    try:
        return ET.fromstring(body), None
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (0, 0))
        # The parser counts lines in what it was given; add back any stripped above so the
        # number matches what the submitter sees in their editor. Columns are 0-based.
        line += text[: len(text) - len(body)].count("\n")
        where = f"near line {line}, column {column + 1}" if line else "at the start of the file"
        return None, _BLOCK_UNREADABLE.format(where=where)


# ---- rule bodies ----------------------------------------------------------------------------


def _check_readable(s: Subject) -> tuple[str, str] | None:
    """The file must be readable XML before anything else is asked of it.

    ``block``, and first in the table, so ``evaluate`` stops here on an unreadable file and no
    other rule runs against text that has no structure to report on. Says nothing when the file
    is fine: it is a precondition rather than a grade, and every row after it already implies it
    passed. Emitting a ``pass`` would add a row to every report and mirror comment that tells a
    curator nothing.

    An empty upload is left to ``gpml.root``, which has always been what refuses it.
    """
    if s.unreadable:
        return (Severity.BLOCK.value, s.unreadable)
    return None


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
    if not s.author and s.origin == "adopted":
        return (
            Severity.FAIL.value,
            "The GPML carries no Author attribute. This pull request was opened on GitHub, so the "
            "portal never rewrites the file, and the repository's metadata step crashes on a "
            "pathway without one. Ask the author to set it and push again.",
        )
    if not s.author:
        return (
            Severity.WARN.value,
            "The GPML carries no Author attribute. The portal fills in your name when it "
            "commits, but the repository's metadata step crashes on a file that has none, so it "
            "is worth setting in your own copy.",
        )
    return (Severity.PASS.value, f"Author is {s.author}.")


_VERSION_RE = re.compile(r"^WP\d+_r\d+$")
_EMBEDDED_WPID_RE = re.compile(r"^WP([0-9]+)")


def _check_embedded_wpid(s: Subject) -> tuple[str, str] | None:
    """Does the file say it is the pathway it is being submitted as?

    A GPML carries its own identity in ``Version`` (``WP554_r…``). A contributor who picks the
    wrong file from a folder of downloads, or starts a new pathway from a copy of an old one, gets
    no warning otherwise: the portal renumbers an update to the target id and files a new one
    under a fresh id, either way silently.
    """
    match = _EMBEDDED_WPID_RE.match(s.version or "")
    if s.tree is None or not match or match.group(1).startswith("0"):
        # No identity in the file, or the WP0001 placeholder a submission carries before it has
        # an id. Nothing to compare.
        return None
    embedded = int(match.group(1))
    if s.target_wpid:
        if embedded == s.target_wpid:
            return (Severity.PASS.value, f"The file says it is WP{embedded}, as expected.")
        return (
            Severity.WARN.value,
            f"The file says it is WP{embedded} (Version {s.version}), but it is being submitted "
            f"as WP{s.target_wpid}. If it is the right file it is renumbered to WP"
            f"{s.target_wpid}; if not, choose the WP{s.target_wpid} file.",
        )
    if s.kind == "new":
        return (
            Severity.WARN.value,
            f"The file says it is WP{embedded} (Version {s.version}). Submitting it as a new "
            f"pathway gives it a new identifier. If you meant to change WP{embedded}, use "
            "Update existing instead.",
        )
    return None





def _check_version(s: Subject) -> tuple[str, str] | None:
    """What the published file's ``Version`` will say, predicted from what the upload carries.

    The repository's publish step (3A) keeps whatever follows the ``WP<n>`` part and sets
    ``Last-Modified`` from a 14-digit revision stamp, or from the publication time when there is
    none. So a ``Version`` of ``WP<n>_r<digits>`` publishes cleanly and a missing one gets a fresh
    stamp, but anything else — an old PathVisio ``Version="1"`` — is carried into the published
    file as ``WP<n>_1``. Not blocking: the pathway publishes either way.
    """
    if s.tree is None:
        return None
    if not s.version:
        return (
            Severity.PASS.value,
            "No Version yet. The repository stamps WP<id>_r<time> when it publishes.",
        )
    if _VERSION_RE.match(s.version):
        return (Severity.PASS.value, f"Version is {s.version}.")
    return (
        Severity.WARN.value,
        f"Version is “{s.version}”, which is not the WP<id>_r<revision> form. The repository keeps "
        f"what follows the id, so the published file would say WP<id>_{s.version} and its "
        "Last-Modified would be the publication time. Clearing the attribute avoids that.",
    )


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

    Read off the parsed tree: any ``<Graphics>`` carrying a ``BoardWidth`` attribute counts, rather
    than walking to the root's own ``<Graphics>`` child, because ``BoardWidth`` appears nowhere else
    in the schema. It was a text pattern until the pattern turned out to rescan the rest of the
    file from every unterminated ``<Graphics``.
    """
    if s.tree is None:
        return None  # an unreadable file is refused by ``gpml.readable`` before this runs
    has_board = any(
        _localname(el.tag) == "Graphics"
        and any(_localname(name) == "BoardWidth" for name in el.attrib)
        for el in s.tree.iter()
    )
    if not has_board:
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

    Walks the parsed tree, looking at each line element's own ``<Graphics>`` child — the one
    ``readLineElement`` reads. It used to pair each opening tag with the next closing one by
    pattern, which rescanned to the end of the file from every line element that had no closing
    tag (a self-closing ``<Interaction/>`` included), so a large file took seconds to grade.
    """
    if s.tree is None:
        return None  # an unreadable file is refused by ``gpml.readable`` before this runs
    offenders: list[str] = []
    position = 0
    for el in s.tree.iter():
        tag = _localname(el.tag)
        if tag not in _LINE_ELEMENTS:
            continue
        position += 1
        graphics = next((c for c in el if _localname(c.tag) == "Graphics"), None)
        if graphics is not None and any(
            _localname(name) == "LineThickness" for name in graphics.attrib
        ):
            continue
        # Interactions carry no TextLabel, so a GraphId is the only name one can have — and a
        # hand-built GPML, which is the kind that has this defect, often sets neither. Falling
        # back to the position in document order beats repeating "an unnamed Interaction" once
        # per offender, which is what it said first and told nobody which ones to fix.
        graph_id = el.get("GraphId")
        offenders.append(f"{tag} {graph_id}" if graph_id else f"{tag} #{position}")
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


def _check_group_zorder(s: Subject) -> tuple[str, str] | None:
    """A group none of whose members declares a ``ZOrder`` kills the repository's ``json-svg`` job.

    Third of the measured pipeline killers, and the first outside the ``metadata`` job. Found on
    `wikipathways/sandbox-wp-db` PR #87, 2026-11-19: a hand-built GPML with one complex (three
    grouped data nodes) and no ``ZOrder`` anywhere. ``metadata`` passed; ``json-svg`` died in
    ``gpml2pvjson`` (``toPvjson.ts``, ``getGroupDimensions``) with ``Got a non-finite value(s):
    {"zIndex": null}``. With no JSON there is no draft, so ``commit-outputs`` is skipped and
    workflow 3A later fails at ``get-wpid`` with "No draft file found".

    Reproduced locally with the converter version the repository pins (4.1.13), one variable at
    a time on that file: no ``ZOrder`` anywhere fails; ``ZOrder`` on the group members alone
    converts; ``ZOrder`` on every element but one group member converts. So a group is fine as
    soon as *one* member carries it, and an element outside any group never needs it. That is
    the condition tested here, so a file the converter accepts is never reported.

    Only plain groups. The same converter refuses a nested group outright ("Encountered nested
    Group"), with or without ``ZOrder``, so a ``<Group>`` member contributes nothing here and
    nesting is not this rule's question.
    """
    if s.tree is None:
        return None  # an unreadable file is refused by ``gpml.readable`` before this runs
    members: dict[str, list[ET.Element]] = {}
    for el in s.tree.iter():
        ref = el.get("GroupRef")
        if ref:
            members.setdefault(ref, []).append(el)
    if not members:
        return (Severity.PASS.value, "No grouped elements.")

    def has_zorder(el: ET.Element) -> bool:
        graphics = next((c for c in el if _localname(c.tag) == "Graphics"), None)
        return graphics is not None and any(
            _localname(name) == "ZOrder" for name in graphics.attrib
        )

    # Only groups with at least one non-<Group> member: a group holding nothing but other groups
    # is the nested case above, which this rule leaves alone.
    offenders = sorted(
        gid
        for gid, group in members.items()
        if any(_localname(m.tag) != "Group" for m in group)
        and not any(has_zorder(m) for m in group)
    )
    if offenders:
        n = len(offenders)
        named = []
        for gid in offenders:
            labels = [m.get("TextLabel") or m.get("GraphId") or "?" for m in members[gid]]
            named.append(f"{gid} ({', '.join(labels[:4])}{', …' if len(labels) > 4 else ''})")
        return (
            Severity.FAIL.value,
            f"{n} group{'' if n == 1 else 's'} where no member declares a ZOrder: "
            f"{'; '.join(named)}. The repository's converter cannot place such a group and its "
            "json-svg step fails, so no diagram or draft page is made and publishing fails later. "
            "PathVisio always writes ZOrder; add it to the members' <Graphics>.",
        )
    return (Severity.PASS.value, "Every group has a stacking order.")


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
    if s.kind == "update" and s.before is not None:
        return _relative_annotation(s, nodes, unmapped)
    # Any unmapped node fails, which is stricter than ``mvp1/validate_pathway.py`` — it warns
    # unless *nothing* resolved. The app's answer is kept because the app is in a position to be
    # strict: this runs before the submission exists, where an unannotated node is still cheap to
    # fix, and a curator can always overrule the state. The parity test excludes this rule.
    return (
        Severity.FAIL.value,
        f"{len(unmapped)} of {len(nodes)} data nodes have no identifier: {_named(unmapped)}",
    )


def _relative_annotation(s: Subject, nodes: list, unmapped: list[str]) -> tuple[str, str]:
    """An update is graded on what it changed, not on the pathway it inherited.

    1175 of 2218 published pathways have at least one data node with no identifier (measured
    2026-11-11). Failing every edit of those on the old debt told a contributor who improved a
    pathway that they had broken it. So: fail only when this edit leaves *more* unmapped nodes
    than the base had, naming the new ones; otherwise say the rest were there before.
    """
    before_unmapped = [
        getattr(n, "label", "") or "(unlabeled)"
        for n in _nodes(Subject(text="", metadata=s.before))
        if not getattr(n, "identifier", "")
    ]
    if len(unmapped) > len(before_unmapped):
        remaining = list(before_unmapped)
        new = []
        for label in unmapped:
            if label in remaining:
                remaining.remove(label)
            else:
                new.append(label)
        return (
            Severity.FAIL.value,
            f"This edit leaves {len(unmapped)} of {len(nodes)} data nodes without an identifier, "
            f"{len(unmapped) - len(before_unmapped)} more than before: {_named(new or unmapped)}",
        )
    fixed = len(before_unmapped) - len(unmapped)
    better = f" This edit annotates {fixed} that had none." if fixed else ""
    return (
        Severity.WARN.value,
        f"{len(unmapped)} of {len(nodes)} data nodes have no identifier, all of them already "
        f"unannotated before this edit: {_named(unmapped)}.{better}",
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
    if not description and s.kind == "update" and s.before is not None:
        if not getattr(s.before, "description", None):
            # Inherited, like unannotated nodes: 28 published pathways have none (2026-11-12).
            return (
                Severity.WARN.value,
                "The pathway has no description, and the version this edit replaces had none "
                "either. The repository asks for a two to three sentence overview; worth adding.",
            )
        return (
            Severity.FAIL.value,
            "This edit removes the pathway's description. The repository asks for a two to "
            "three sentence overview of the processes it describes.",
        )
    if not description:
        return (
            Severity.FAIL.value,
            "The pathway has no description. The repository asks for a two to three sentence "
            "overview of the processes it describes.",
        )
    if s.kind == "update" and s.before is not None:
        was = (getattr(s.before, "description", "") or "").strip()
        change = description_change(was, description)
        word_diff = abs(change["word_delta"])
        char_diff = abs(change["char_delta"])
        limit = (
            f"the repository's limit of {_EDIT_MAX_WORD_DIFF} words or "
            f"{_EDIT_MAX_CHAR_DIFF} characters"
        )
        if not was:
            # The card calls this "added" (app.preview.diff.describe_description_change); so
            # does the rule, rather than describing a first description as a rewrite.
            added = _plural(_words(description), "word")
            if change["over"]:
                return (
                    Severity.WARN.value,
                    f"This edit adds a description ({added}). The repository flags any change "
                    f"to the description of more than {_EDIT_MAX_WORD_DIFF} words or "
                    f"{_EDIT_MAX_CHAR_DIFF} characters.",
                )
            return (
                Severity.PASS.value,
                f"This edit adds a short description ({added}), under {limit}.",
            )
        if change["over"]:
            return (
                Severity.WARN.value,
                f"The description changed by {_plural(word_diff, 'word')} and "
                f"{_plural(char_diff, 'character')}. The repository flags an edit that rewrites "
                f"it by more than {_EDIT_MAX_WORD_DIFF} words or {_EDIT_MAX_CHAR_DIFF} "
                "characters.",
            )
        if change["identical"]:
            return (Severity.PASS.value, "The description is unchanged.")
        if not word_diff and not char_diff:
            return (
                Severity.PASS.value,
                f"The description was reworded at the same length, under {limit}.",
            )
        return (
            Severity.PASS.value,
            f"The description changed slightly ({_plural(word_diff, 'word')}, "
            f"{_plural(char_diff, 'character')}), under {limit}.",
        )
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
    if s.kind == "update" and s.before is not None:
        before_cited = getattr(s.before, "cited_reference_count", None)
        if not isinstance(before_cited, int):
            before_cited = len(list(getattr(s.before, "references", []) or []))
        if n < before_cited:
            return (
                Severity.FAIL.value,
                f"This edit drops {before_cited - n} of the {before_cited} cited literature "
                f"reference{'' if before_cited == 1 else 's'} the pathway had. If that was "
                "deliberate, a curator can mark this item; otherwise restore the citations.",
            )
        if not n:
            return (
                Severity.WARN.value,
                "No literature references, and the version this edit replaces had none either. "
                "The repository asks for at least one; worth adding, not caused by this edit.",
            )
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
    Rule(
        "gpml.readable",
        "Readable XML",
        _check_readable,
        explain=(
            "The file has to be well-formed XML: every tag closed and nothing cut off. A file "
            "saved by PathVisio always is; one edited by hand or truncated in a download may not "
            "be. Files that declare a DOCTYPE or ENTITY are refused too. An unreadable file is "
            "refused before anything else is checked."
        ),
    ),
    Rule(
        "gpml.root",
        "GPML root element",
        _check_root,
        explain=(
            "A GPML file has one <Pathway> element that holds everything else. Without it the "
            "file is not a pathway and is refused."
        ),
    ),
    Rule(
        "gpml.namespace",
        "GPML namespace",
        _check_namespace,
        explain=(
            "The <Pathway> element must declare the GPML namespace, which tells software the "
            "file is a PathVisio/WikiPathways pathway. PathVisio writes it for you. Refused "
            "without it."
        ),
    ),
    Rule(
        "gpml.name",
        "Title",
        _check_name,
        explain=(
            "The pathway needs a title, the Name attribute on <Pathway>. Refused without one."
        ),
    ),
    Rule(
        "gpml.organism",
        "Organism",
        _check_organism,
        explain=(
            "The pathway needs an organism (the Organism attribute, for example Homo sapiens). "
            "The drawing and the repository's metadata step both depend on it, so a file "
            "without one is refused."
        ),
    ),
    Rule(
        "render.drawable",
        "Renderable",
        _check_drawable,
        needs_text=False,
        explain=(
            "Whether the portal could draw a diagram from the file. Curators review the "
            "drawing, so a pathway that cannot be drawn is a problem. Reported once the pull "
            "request exists, on the review page."
        ),
    ),
    Rule(
        "gpml.board",
        "Pathway canvas",
        _check_board,
        explain=(
            "The pathway must declare its canvas size: a <Graphics> element with BoardWidth and "
            "BoardHeight directly under <Pathway>. Without it the repository's metadata step "
            "fails, which costs the identifier table, the reference list and the draft page. "
            "PathVisio always writes it; a hand-built file often does not."
        ),
    ),
    Rule(
        "gpml.line_thickness",
        "Interaction line thickness",
        _check_line_thickness,
        explain=(
            "Every interaction and graphical line needs a LineThickness on its <Graphics>. The "
            "repository's metadata step fails without it, with the same cost as a missing "
            "canvas. PathVisio always writes it."
        ),
    ),
    Rule(
        "gpml.group_zorder",
        "Group stacking order",
        _check_group_zorder,
        explain=(
            "In every group (a complex, for example) at least one member needs a ZOrder, the "
            "stacking order, on its <Graphics>. Without one the repository's converter cannot "
            "place the group, so no diagram or draft page is made and publishing fails later. "
            "PathVisio always writes it."
        ),
    ),
    Rule(
        "gpml.author",
        "Authors",
        _check_author,
        explain=(
            "Who made the pathway, the Author attribute. The portal fills in your GitHub name "
            "when the file has none, so for an upload this is only advice. A pull request "
            "opened directly on GitHub gets no such repair, and there it is a problem: the "
            "repository's metadata step crashes without an author."
        ),
    ),
    Rule(
        "gpml.version",
        "Version",
        _check_version,
        predicts_repo=True,
        explain=(
            "The Version attribute. The repository stamps it as WP<id>_r<date and time> when it "
            "publishes. Leave it empty or in that form; anything else, such as an old "
            "Version=\"1\", is carried into the published file."
        ),
    ),
    Rule(
        "gpml.embedded_wpid",
        "Pathway identifier",
        _check_embedded_wpid,
        explain=(
            "Whether the identifier written inside the file (in Version) matches the pathway you "
            "are submitting it as. It catches the wrong file picked from a downloads folder, or "
            "a new pathway started from a copy of an old one."
        ),
    ),
    Rule(
        "gpml.citation_ids",
        "Empty citation IDs",
        _check_citation_ids,
        explain=(
            "Literature references with an empty ID. The repository patches them so its "
            "metadata step can run, but an empty citation ID is the most common reason that "
            "step fails, so it is worth fixing in your own copy."
        ),
    ),
    Rule(
        "gpml.datanodes",
        "DataNodes in GPML",
        _check_datanodes_present,
        needs_text=False,
        explain=(
            "The pathway contains at least one data node: a gene, protein, metabolite and so on."
        ),
    ),
    Rule(
        "content.datanode_annotation",
        "Datanode annotation",
        _check_datanode_annotation,
        needs_text=False,
        checklist=ChecklistBinding("datanodes_mapped"),
        explain=(
            "Every data node should carry a database identifier (Ensembl, NCBI Gene, UniProt, "
            "ChEBI and so on) so it links out and can be used in analyses. For an update only "
            "the change counts: nodes left unannotated beyond those the pathway already had are "
            "a problem, the older ones are reported as worth a look."
        ),
    ),
    Rule(
        "content.title_length",
        "Title length",
        _check_title_length,
        predicts_repo=True,
        explain=(
            f"The repository's own check: a title of at least {_MIN_TITLE_CHARS} characters. "
            "Shorter titles are flagged for review."
        ),
    ),
    Rule(
        "content.description",
        "Description",
        _check_description,
        predicts_repo=True,
        needs_text=False,
        # Never suggests better than pending, even on a clean file: "meaningful" is the curator's
        # judgement, and the repository's own description reading (which ``refresh_pipeline_checks``
        # puts beside the item once it runs) quotes what its extractor pulled out, the text that
        # will appear on the published page. Failing is still worth saying: where there is no
        # description at all, both would say the same thing and the repository may never run.
        checklist=ChecklistBinding(
            "description_ok",
            state_for={Severity.PASS.value: _PENDING, Severity.WARN.value: _PENDING},
        ),
        explain=(
            "The repository asks for a two to three sentence description of the processes the "
            f"pathway shows, at least {_MIN_DESCRIPTION_WORDS} words for a new pathway. On an "
            f"update a rewrite of more than {_EDIT_MAX_WORD_DIFF} words or "
            f"{_EDIT_MAX_CHAR_DIFF} characters is flagged so a curator looks at it, and removing "
            "the description is a problem."
        ),
    ),
    Rule(
        "content.datanode_changes",
        "Data nodes changed",
        _check_datanode_changes,
        predicts_repo=True,
        needs_text=False,
        explain=(
            "Whether an update added, removed or changed data nodes. The repository flags any "
            "such change for review. It tells the curator where to look; it is not a fault."
        ),
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
        explain=(
            "At least one literature reference that is cited somewhere in the pathway (on the "
            "pathway itself, a node or an interaction). A reference listed in the file but cited "
            "nowhere does not count, because nothing downstream shows it. An update that drops "
            "citations is a problem. Whether each reference is correct is for the curator."
        ),
    ),
    Rule(
        "content.ontology",
        "Ontology tags",
        _check_ontology,
        needs_text=False,
        checklist=ChecklistBinding("ontology_tags"),
        explain=(
            "Ontology tags (pathway, disease, cell type) are optional. When present they help "
            "people find the pathway."
        ),
    ),
)

#: The refusals after which nothing else is worth saying: the file is not XML, or it is XML but
#: not a pathway.
_STOP_ON_BLOCK = frozenset({"gpml.readable", "gpml.root"})

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
    origin: str = "portal",
    target_wpid: int | None = None,
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
    tree: ET.Element | None = None
    unreadable: str | None = None
    if text.strip():
        tree, unreadable = _read_tree(text)
    if unreadable:
        # Nothing else is derived from a file that is not XML. ``evaluate`` stops at
        # ``gpml.readable``, and the root-tag pattern below is exactly the kind that rescans an
        # unterminated document from every opening tag — it is only cheap on readable XML, where
        # every ``<`` is eventually followed by a ``>``.
        return Subject(
            text=text,
            metadata=metadata,
            before=before,
            kind=kind,
            drawable=drawable,
            unreadable=unreadable,
            origin=origin,
        )
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
        tree=tree,
        origin=origin,
        version=tree.get("Version") if tree is not None else None,
        target_wpid=target_wpid,
    )


def evaluate(subject: Subject, *, text_available: bool = True) -> QualityReport:
    """Run every applicable rule over ``subject``.

    Three things are skipped rather than answered wrongly:

    - A file that is not readable XML gets only that finding. There is no structure to report on,
      and the other text rules would be pattern-matching a document that has no shape.
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
        if rule.id in _STOP_ON_BLOCK and severity == Severity.BLOCK.value:
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

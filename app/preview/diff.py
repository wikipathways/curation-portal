"""What changed between an update's before and after renders (issue #24).

The before/after viewer (issue #11) made the change *visible* and left working out *what* changed
to the curator's eye, across two images that can be a hundred nodes each. This closes the last
open part of the audit's problem #1 ("reviewers approve unreadable XML"): it is a comparison of
two lists the app already has on disk, not new extraction, because ``render_gpml_with_nodes``
returns each data node's identity, annotation and position in the same pass that draws it.

The categories, in the order a curator cares about them:

- **reannotated** — the same node, a different database or identifier. The most review-relevant
  and the least visible in a picture, because the box looks identical.
- **added** / **removed** — in one side and not the other.
- **relabelled** — the same annotation under a different name.
- **moved** — same node, different place. Usually noise, which is why the overlay can hide it.

Interactions are compared the same way (``diff_edges``), over the ``edges`` of the graph sidecar
the renderer writes beside each drawing: **added** / **removed**, **retyped** (the arrow changed,
so the interaction now means something else) and **reconnected** (it now starts or ends somewhere
else, including an end that was attached or let go). A line that only moved is not reported.

Nothing here is a substitute for reading the GPML when it matters; it is a summary that tells a
curator whether they need to.

## diff.json (contract K3)

``version`` stays 2, so a client written for it keeps working: ``before``/``after`` (index
arrays), ``summary``, ``pairs`` and ``ids`` for the data nodes, and ``meta`` for title,
description, references and tags. ``minor`` 1 adds ``edges``: ``summary`` (counts per change),
``pairs`` (``{before, after, change}`` by element id) and ``before``/``after`` (each id's change;
on the after side an object carrying ``was`` for a retyped interaction). A diff without ``edges``
is a cache written before it, and reads as "interactions not compared", never as "unchanged".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.quality import description_change

# Two nodes are in the same place unless their centres differ by more than this many GPML user
# units. A node box is typically 20-25 units tall, so this is well inside "did not move" while
# still absorbing the sub-unit noise a round-trip through PathVisio leaves behind.
_MOVE_TOLERANCE = 2.0

# Every classification the client knows how to colour. Kept here rather than in the template for
# the same reason ReviewStatus labels live in app/review/status.py: one list to add to.
CHANGES = ("added", "removed", "reannotated", "relabelled", "moved", "unchanged")

#: The same for interactions, in the order the card names them.
EDGE_CHANGES = ("added", "removed", "retyped", "reconnected", "unchanged")

#: Written into ``render.json`` as ``diff``. A cache stamped with less was compared by an older
#: version of this module, and ``PreviewService.needs_rerender`` sends it back through the
#: renderer (from the GPML on disk) so its ``diff.json`` gains what it lacks. 2: interactions.
DIFF_VERSION = 2


@dataclass
class PathwayDiff:
    """Per-side classifications, index-aligned with each side's ``<side>-nodes.json``.

    Index alignment is safe here in a way it would not be elsewhere: both arrays are built from
    the very lists the endpoint serves, in this one function. It is emphatically *not* the join
    that ``test_geometry_and_properties_cannot_drift_apart`` exists to forbid, which crossed the
    renderer's output with the metadata parser's — two passes that skip different elements. The
    client still checks the lengths agree before colouring anything.
    """

    before: list[dict] = field(default_factory=list)
    after: list[dict] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    #: The same answer keyed by element id (renderer 2), which is what the viewer colours by:
    #: ``pairs`` says which node on one side is which on the other, ``ids`` gives each id's
    #: change. Index arrays above stay for a client that still reads the v1 shape.
    pairs: list[dict] = field(default_factory=list)
    ids: dict[str, dict] = field(default_factory=lambda: {"before": {}, "after": {}})

    #: Interactions (``diff_edges``), or None when they were not compared.
    edges: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "version": 2,
            "before": self.before,
            "after": self.after,
            "summary": self.summary,
            "pairs": self.pairs,
            "ids": self.ids,
        }
        if self.edges is not None:
            out["minor"] = 1
            out["edges"] = self.edges
        return out


def _key_label(n: dict) -> tuple[str, str]:
    return ((n.get("label") or "").strip().casefold(), (n.get("type") or "").strip())


def _key_xref(n: dict) -> tuple[str, str]:
    return (
        (n.get("database") or "").strip().casefold(),
        (n.get("identifier") or "").strip().casefold(),
    )


def _pair_on(
    key,
    before: list[dict],
    after: list[dict],
    unmatched_b: set[int],
    unmatched_a: set[int],
    pairs: dict[int, int],
    *,
    require_all: bool = False,
    key_after=None,
) -> None:
    """Pair still-unmatched nodes whose ``key`` agrees, in document order within each group.

    ``key_after``, when given, is the key for the after side: an interaction's ends are named
    differently on each side and only agree once translated (``diff_edges``).

    Two nodes can legitimately share a key — a pathway may carry the same gene twice — so the
    groups are zipped rather than required to be singletons. Zipping in document order is a
    heuristic: it is right whenever the duplicates were not reordered, and when it is wrong it
    pairs two interchangeable copies of the same thing, which is a far smaller error than
    reporting every duplicate as one removal plus one addition.
    """
    groups_b: dict[tuple, list[int]] = {}
    groups_a: dict[tuple, list[int]] = {}
    for i in sorted(unmatched_b):
        k = key(before[i])
        if require_all and not all(k):
            continue
        groups_b.setdefault(k, []).append(i)
    key_a = key_after or key
    for j in sorted(unmatched_a):
        k = key_a(after[j])
        if require_all and not all(k):
            continue
        groups_a.setdefault(k, []).append(j)

    for k, bs in groups_b.items():
        # Uneven groups are the normal case (one copy added, two removed), so the shorter one
        # ending the zip is the intended behaviour, not an oversight.
        for i, j in zip(bs, groups_a.get(k, []), strict=False):
            pairs[i] = j
            unmatched_b.discard(i)
            unmatched_a.discard(j)


def _moved(b: dict, a: dict) -> bool:
    bx, by, ax, ay = b.get("cx"), b.get("cy"), a.get("cx"), a.get("cy")
    # A cache written before issue #24 has no user-unit centres. Percentages are on file, but
    # they are relative to each side's own board, so a resized board would read as every node
    # having moved. Saying nothing is the honest answer for those.
    if None in (bx, by, ax, ay):
        return False
    return abs(ax - bx) > _MOVE_TOLERANCE or abs(ay - by) > _MOVE_TOLERANCE  # type: ignore[operator]


def _classify(b: dict, a: dict) -> str:
    if _key_xref(b) != _key_xref(a):
        return "reannotated"
    if (b.get("label") or "").strip() != (a.get("label") or "").strip():
        return "relabelled"
    if _moved(b, a):
        return "moved"
    return "unchanged"


def _was(b: dict) -> dict:
    """What the counterpart used to say — enough for the panel to strike the old value through."""
    return {
        "label": b.get("label") or "",
        "database": b.get("database") or "",
        "identifier": b.get("identifier") or "",
    }


def diff_nodes(before: list[dict], after: list[dict]) -> PathwayDiff:
    """Classify every data node on both sides of an update.

    Matching runs from most to least trustworthy, each pass only over what is still unmatched:

    1. **GraphId** — GPML's own element id, which PathVisio carries across an edit.
    2. **label + type** — what a human means by "the same node" when the id was not preserved.
    3. **database + identifier** — catches a node that was renamed but still points at the same
       entity. Only over nodes carrying both, since every unannotated node would otherwise
       collide on the empty key.

    A node whose label *and* annotation both changed is genuinely a delete plus an add, and falls
    out of all three passes as exactly that.
    """
    unmatched_b = set(range(len(before)))
    unmatched_a = set(range(len(after)))
    pairs: dict[int, int] = {}

    # GraphId, but only where both sides have one — the empty id is not an identity.
    _pair_on(
        lambda n: ((n.get("graph_id") or "").strip(),),
        before, after, unmatched_b, unmatched_a, pairs, require_all=True,
    )
    _pair_on(_key_label, before, after, unmatched_b, unmatched_a, pairs)
    _pair_on(_key_xref, before, after, unmatched_b, unmatched_a, pairs, require_all=True)

    b_out: list[dict] = [{"change": "removed"} for _ in before]
    a_out: list[dict] = [{"change": "added"} for _ in after]
    for i, j in pairs.items():
        change = _classify(before[i], after[j])
        b_out[i] = {"change": change}
        entry: dict = {"change": change}
        # The panel shows the old value struck through beside the new one, which is the only way
        # a re-annotation is legible at all — the two boxes look identical.
        if change in ("reannotated", "relabelled"):
            entry["was"] = _was(before[i])
        a_out[j] = entry

    summary = {c: 0 for c in CHANGES}
    for e in a_out:
        summary[e["change"]] += 1
    summary["removed"] = sum(1 for e in b_out if e["change"] == "removed")

    # Keyed by element id. A node from a cache older than renderer 2 has none and is simply
    # absent here; the index arrays still describe it.
    pair_list = [
        {"before": before[i]["id"], "after": after[j]["id"], "change": a_out[j]["change"]}
        for i, j in sorted(pairs.items())
        if before[i].get("id") and after[j].get("id")
    ]
    ids: dict[str, dict] = {"before": {}, "after": {}}
    for n, e in zip(before, b_out, strict=True):
        if n.get("id"):
            ids["before"][n["id"]] = e["change"]
    for n, e in zip(after, a_out, strict=True):
        if n.get("id"):
            ids["after"][n["id"]] = dict(e)
    return PathwayDiff(
        before=b_out, after=a_out, summary=summary, pairs=pair_list, ids=ids
    )


def _endpoint_keys(diff: PathwayDiff, before: list[dict], after: list[dict]):
    """Two functions, one per side, turning a line's end (a ``data-id``) into something comparable
    across the sides.

    A data node's id is replaced by its pairing from ``diff_nodes``, so a line still counts as
    attached to the same node when the node's GraphId was rewritten. A data node with no
    counterpart (added or removed) gets a key only its own side can produce: a line to it is a
    line to something new. Anything else (a label, a group, a state, another line's anchor) is
    matched on its id, which comes from its GraphId and survives an edit the same way; an id the
    renderer had to make up (``auto``) means nothing across sides and is kept apart too.
    """
    paired_b = {p["before"]: p["after"] for p in diff.pairs}
    paired_a = set(paired_b.values())
    node_b = {n.get("id") for n in before if n.get("id")}
    node_a = {n.get("id") for n in after if n.get("id")}

    def key_b(did: str | None):
        if not did:
            return None
        if did in paired_b:
            return ("node", paired_b[did])
        if did in node_b or "auto" in did:
            return ("before", did)
        return ("id", did)

    def key_a(did: str | None):
        if not did:
            return None
        if did in paired_a:
            return ("node", did)
        if did in node_a or "auto" in did:
            return ("after", did)
        return ("id", did)

    return key_b, key_a


def diff_edges(
    before_edges: list[dict],
    after_edges: list[dict],
    node_diff: PathwayDiff,
    before_nodes: list[dict],
    after_nodes: list[dict],
) -> dict:
    """Classify every interaction on both sides of an update.

    ``*_edges`` are the ``edges`` of each side's graph sidecar; only ``kind == "Interaction"`` is
    compared, as only data nodes are among the boxes: a graphical line carries no meaning. The
    node lists and ``node_diff`` supply the pairing of the line ends.

    Matching, each pass over what is still unmatched:

    1. **GraphId**, where both sides have one.
    2. **source, target and type**: the same interaction redrawn under a new id.
    3. **source and target**: the same two ends with a different arrow, which is a retyping.

    A matched pair is **retyped** when the arrow changed and **reconnected** when either end now
    lands somewhere else (an end attached or let go counts); a change of both reads as
    reconnected, the larger of the two.
    """
    before = [e for e in before_edges if isinstance(e, dict) and e.get("kind") == "Interaction"]
    after = [e for e in after_edges if isinstance(e, dict) and e.get("kind") == "Interaction"]
    key_b, key_a = _endpoint_keys(node_diff, before_nodes, after_nodes)

    def ends_b(e: dict) -> tuple:
        return (key_b(e.get("source")), key_b(e.get("target")))

    def ends_a(e: dict) -> tuple:
        return (key_a(e.get("source")), key_a(e.get("target")))

    unmatched_b = set(range(len(before)))
    unmatched_a = set(range(len(after)))
    pairs: dict[int, int] = {}

    def graph_id(e: dict) -> tuple:
        return ((e.get("graph_id") or "").strip(),)

    _pair_on(graph_id, before, after, unmatched_b, unmatched_a, pairs, require_all=True)

    # A line attached at neither end has no identity beyond its GraphId: every such line would
    # share the key (None, None) and pair with any other.
    def by_ends(ends):
        def key(e: dict) -> tuple:
            t = ends(e)
            return t if any(t) else (("unattached", id(e)),)
        return key

    _pair_on(
        lambda e: (*by_ends(ends_b)(e), e.get("type")), before, after,
        unmatched_b, unmatched_a, pairs,
        key_after=lambda e: (*by_ends(ends_a)(e), e.get("type")),
    )
    _pair_on(
        by_ends(ends_b), before, after, unmatched_b, unmatched_a, pairs, key_after=by_ends(ends_a)
    )

    summary = {c: 0 for c in EDGE_CHANGES}
    summary["added"] = len(unmatched_a)
    summary["removed"] = len(unmatched_b)
    b_ids: dict[str, str] = {e["id"]: "removed" for e in before if e.get("id")}
    a_ids: dict[str, dict] = {e["id"]: {"change": "added"} for e in after if e.get("id")}
    pair_list: list[dict] = []
    for i, j in sorted(pairs.items()):
        b, a = before[i], after[j]
        if ends_b(b) != ends_a(a):
            change = "reconnected"
        elif (b.get("type") or "") != (a.get("type") or ""):
            change = "retyped"
        else:
            change = "unchanged"
        summary[change] += 1
        entry: dict = {"change": change}
        if change == "retyped":
            # The panel strikes the old arrow through beside the new one.
            entry["was"] = {"type_label": b.get("type_label") or b.get("type") or ""}
        if b.get("id"):
            b_ids[b["id"]] = change
        if a.get("id"):
            a_ids[a["id"]] = entry
        if b.get("id") and a.get("id"):
            pair_list.append({"before": b["id"], "after": a["id"], "change": change})
    return {"summary": summary, "pairs": pair_list, "before": b_ids, "after": a_ids}


def diff_metadata(before, after) -> dict:
    """What an update changed besides the data nodes: title, description, references, tags.

    ``before``/``after`` are ``CurationMetadata`` (duck-typed). Only what changed is returned, so
    an empty dict means none of these moved. References are keyed by database and identifier, tags
    by identifier; each entry carries a label for display.
    """
    out: dict = {}
    if (getattr(before, "name", None) or "") != (getattr(after, "name", None) or ""):
        out["title"] = {"from": getattr(before, "name", None), "to": getattr(after, "name", None)}
    b_desc = (getattr(before, "description", None) or "").strip()
    a_desc = (getattr(after, "description", None) or "").strip()
    if b_desc != a_desc:
        # Measured by the quality rule's own function, so the card and the rule agree on whether
        # this edit is a small one (app.quality.rules.description_change).
        change = description_change(b_desc, a_desc)
        out["description"] = {
            "from_words": len(b_desc.split()),
            "to_words": len(a_desc.split()),
            "word_delta": change["word_delta"],
            "char_delta": change["char_delta"],
            "rewritten": change["over"],
            "max_words": change["max_words"],
            "max_chars": change["max_chars"],
        }

    def refs(meta) -> dict[tuple[str, str], str]:
        return {
            (
                (getattr(r, "database", "") or "").casefold(),
                (getattr(r, "identifier", "") or "").strip(),
            ): (getattr(r, "title", "") or getattr(r, "identifier", "") or "")
            for r in (getattr(meta, "references", None) or [])
            if getattr(r, "identifier", "")
        }

    def tags(meta) -> dict[str, str]:
        return {
            (getattr(t, "identifier", "") or "").strip(): (
                getattr(t, "term", "") or getattr(t, "identifier", "")
            )
            for t in (getattr(meta, "ontology_tags", None) or [])
            if getattr(t, "identifier", "")
        }

    for key, read in (("references", refs), ("ontology_tags", tags)):
        b, a = read(before), read(after)
        added = [a[k] for k in a.keys() - b.keys()]
        removed = [b[k] for k in b.keys() - a.keys()]
        if added or removed:
            out[key] = {"added": sorted(added), "removed": sorted(removed)}
    return out


def _signed(n: int, word: str) -> str:
    sign = "+" if n > 0 else "\u2212"
    return f"{sign}{abs(n)} {word}{'' if abs(n) == 1 else 's'}"


def describe_description_change(entry: dict) -> str:
    """The card's words for ``diff_metadata``'s ``description`` entry, e.g. "edited (+1 word)".

    "Rewritten" only past the repository's limits, the line the quality rule draws too. A cache
    written before the deltas were recorded has only the two word counts, and keeps the old
    wording rather than guessing which side of the line it fell.
    """
    b, a = entry.get("from_words"), entry.get("to_words")
    if "rewritten" not in entry:
        return f"rewritten ({b} \u2192 {a} words)"
    if not b:
        return f"added ({a} word{'' if a == 1 else 's'})"
    if not a:
        return f"removed (was {b} word{'' if b == 1 else 's'})"
    if entry.get("rewritten"):
        return f"rewritten ({b} \u2192 {a} words)"
    words, chars = entry.get("word_delta") or 0, entry.get("char_delta") or 0
    if words:
        return f"edited ({_signed(words, 'word')})"
    if chars:
        return f"edited ({_signed(chars, 'character')})"
    return "edited (same length, reworded)"


def describe_edge_changes(summary: dict) -> str:
    """"1 added, 2 reconnected", or the sentence for no change at all."""
    parts = [
        f"{summary[k]} {k}" for k in ("added", "removed", "retyped", "reconnected")
        if summary.get(k)
    ]
    return ", ".join(parts) if parts else "No interactions were added, removed or changed."

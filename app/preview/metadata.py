"""Parse the curation-facing metadata out of a GPML pathway.

The review dashboard shows more than a picture: the data nodes and their identifiers, the
literature references, the pathway description, and the ontology tags. All of it lives in the
GPML the submitter uploaded, so we parse it once (at preview-render time) and cache it next to
the rendered SVGs, keeping the dashboard render a cheap disk read.

Parsing is namespace-tolerant (GPML uses a default namespace; the embedded Biopax block uses the
biopax-level3 namespace) via local-name matching, the same approach as ``app.preview.render``.
The attributes the panel reads are taken in either spelling, GPML2013a (``Name``, ``TextLabel``,
``Database``/``ID``) or GPML2021 (``title``, ``textLabel``, ``dataSource``/``identifier``), so a
2021 file the renderer can draw does not leave the panel beside it empty.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from app.preview.gpml import _f as _gpml_f
from app.preview.gpml import _rect as _gpml_rect
from app.preview.gpml import get as _gpml_get

#: ElementTree expands the rdf prefix, so the attribute arrives fully qualified.
_RDF_ID = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}id"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# GPML ``Database`` name (lower-cased) → identifiers.org prefix. identifiers.org is a universal
# resolver that redirects to the provider, so one link shape covers every data source. Databases
# not listed here simply get no link (the curator can still read the raw id).
_RESOLVER_PREFIX = {
    "ensembl": "ensembl",
    "entrez gene": "ncbigene",
    "ncbi gene": "ncbigene",
    "hgnc": "hgnc.symbol",
    "hgnc accession number": "hgnc",
    "uniprot": "uniprot",
    "uniprot-trembl": "uniprot",
    "uniprot/trembl": "uniprot",
    "chebi": "CHEBI",
    "hmdb": "hmdb",
    "pubchem-compound": "pubchem.compound",
    "kegg compound": "kegg.compound",
    "kegg genes": "kegg.genes",
    "chembl compound": "chembl.compound",
    "cas": "cas",
    "wikidata": "wikidata",
    "wikipathways": "wikipathways",
    "pubmed": "pubmed",
    "doi": "doi",
}


def resolver_url(database: str, identifier: str) -> str | None:
    """A resolvable identifiers.org URL for ``(database, identifier)``, or None if unmapped."""
    if not database or not identifier:
        return None
    prefix = _RESOLVER_PREFIX.get(database.strip().lower())
    if not prefix:
        return None
    ident = identifier.strip()
    if prefix == "CHEBI" and ident.upper().startswith("CHEBI:"):
        return f"https://identifiers.org/{ident}"  # id already carries the CHEBI: prefix
    return f"https://identifiers.org/{prefix}:{ident}"


def _either(el: ET.Element | None, *names: str) -> str | None:
    """The first of ``names`` present on ``el`` (2013a spelling first, then 2021)."""
    if el is None:
        return None
    for name in names:
        v = el.get(name)
        if v is not None:
            return v
    return None


def _find_child(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _localname(child.tag) == name:
            return child
    return None


@dataclass(frozen=True)
class DataNode:
    label: str
    type: str
    database: str
    identifier: str
    url: str | None = None


@dataclass(frozen=True)
class Reference:
    identifier: str
    database: str
    title: str
    url: str | None = None


@dataclass(frozen=True)
class OntologyTag:
    term: str
    identifier: str
    ontology: str


@dataclass(frozen=True)
class LooseEnd:
    """One end of an ``<Interaction>`` that is attached to nothing.

    ``end`` is ``source`` or ``target`` the way the viewer reads them: following the arrowhead, so
    a line whose only arrow sits on its first point points from its last point to its first.
    ``attached`` is the label of what the *other* end is attached to and ``near`` the label of the
    data node or label the free end lies on or close beside; either is "" when there is none.
    Both are on one line and shortened when long.
    """

    interaction: str
    end: str
    attached: str = ""
    near: str = ""


@dataclass(frozen=True)
class CurationMetadata:
    name: str | None = None
    organism: str | None = None
    description: str | None = None
    data_nodes: list[DataNode] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    ontology_tags: list[OntologyTag] = field(default_factory=list)
    #: How many of ``references`` are actually cited by a ``<BiopaxRef>`` somewhere in the
    #: pathway. A GPML can carry a PublicationXref nothing points at — PathVisio leaves them
    #: behind when an annotation is removed — and downstream generators only emit the cited ones.
    #: Comparing a generator's output against ``len(references)`` therefore reports a shortfall
    #: that is not one. Defaulted so a metadata blob cached before this field existed still loads.
    cited_reference_count: int = 0
    #: How many ``<Interaction>`` elements have both ends to look at, and which of their ends
    #: attach to nothing (``loose_ends``). ``None`` means not counted: a metadata blob cached
    #: before this existed, where "no loose ends" would be a claim nobody checked.
    interaction_count: int | None = None
    loose_ends: list[LooseEnd] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _dedupe_ontology_tags(tags: list[OntologyTag]) -> list[OntologyTag]:
    """Drop empty tags, and collapse a tag declared in both supported forms.

    A GPML is free to carry the same annotation as an ``<OntologyTerm>`` element and as a
    ``<bp:openControlledVocabulary>`` block — a round trip through a converter can leave both —
    and counting it twice would make the review panel disagree with the file. Order is the
    document's.
    """
    seen: set[tuple[str, str]] = set()
    kept: list[OntologyTag] = []
    for tag in tags:
        if not tag.term and not tag.identifier:
            continue  # an empty vocabulary block annotates nothing
        key = (tag.identifier.casefold(), tag.term.casefold())
        if key in seen:
            continue
        seen.add(key)
        kept.append(tag)
    return kept


def parse_curation_metadata(gpml: bytes | str) -> CurationMetadata:
    """Extract the review-panel metadata from a GPML document. Never raises on malformed input —
    returns whatever could be parsed (an empty metadata object for non-GPML)."""
    text = gpml.decode("utf-8", "replace") if isinstance(gpml, bytes) else gpml
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return CurationMetadata()
    if _localname(root.tag) != "Pathway":
        return CurationMetadata()

    name = _either(root, "Name", "title")
    organism = _either(root, "Organism", "organism")

    data_nodes: list[DataNode] = []
    references: list[Reference] = []
    ontology_tags: list[OntologyTag] = []
    comments: list[str] = []
    # rdf:id of each PublicationXref, in parse order, paired with the set of ids the pathway
    # actually cites — see CurationMetadata.cited_reference_count.
    reference_ids: list[str] = []
    cited_ids: set[str] = set()

    for el in root.iter():
        kind = _localname(el.tag)
        if kind == "DataNode":
            xref = _find_child(el, "Xref")
            database = _either(xref, "Database", "dataSource") or ""
            identifier = _either(xref, "ID", "identifier") or ""
            data_nodes.append(
                DataNode(
                    label=(_either(el, "TextLabel", "textLabel") or "").strip(),
                    type=_either(el, "Type", "type") or "Unknown",
                    database=database,
                    identifier=identifier,
                    url=resolver_url(database, identifier),
                )
            )
        elif kind == "OntologyTerm":
            ontology_tags.append(
                OntologyTag(
                    term=(el.get("Term") or "").strip(),
                    identifier=(el.get("ID") or "").strip(),
                    ontology=(el.get("Ontology") or "").strip(),
                )
            )
        elif kind == "openControlledVocabulary":
            # The form PathVisio actually writes. Real WikiPathways GPML carries ontology tags
            # here, inside <Biopax>, and never as the <OntologyTerm> element above — so reading
            # only that one reported zero tags for every genuine pathway (WP1487 declares one,
            # WP1518 two, and both parsed as none). Children are bp:TERM / bp:ID / bp:Ontology.
            ontology_tags.append(
                OntologyTag(
                    term=_text(_find_child(el, "TERM")),
                    identifier=_text(_find_child(el, "ID")),
                    ontology=_text(_find_child(el, "Ontology")),
                )
            )
        elif kind == "BiopaxRef":
            cited_ids.add((el.text or "").strip())
        elif kind == "PublicationXref":
            # Biopax literature reference: bp:ID / bp:DB / bp:TITLE children. The rdf:id
            # attribute is the handle a <BiopaxRef> elsewhere in the pathway points at.
            reference_ids.append(el.get(_RDF_ID) or el.get("id") or "")
            ref_id = _text(_find_child(el, "ID"))
            ref_db = _text(_find_child(el, "DB")) or "PubMed"
            references.append(
                Reference(
                    identifier=ref_id,
                    database=ref_db,
                    title=_text(_find_child(el, "TITLE")),
                    url=resolver_url(ref_db, ref_id),
                )
            )

    # Pathway description lives in top-level <Comment> children (skip nested ones on elements).
    for child in root:
        if _localname(child.tag) == "Comment" and (child.text or "").strip():
            comments.append(child.text.strip())

    description = "\n\n".join(comments) or None
    interaction_count, loose_ends = _loose_ends(root)
    return CurationMetadata(
        name=name,
        organism=organism,
        description=description,
        data_nodes=data_nodes,
        references=references,
        ontology_tags=_dedupe_ontology_tags(ontology_tags),
        cited_reference_count=sum(1 for rid in reference_ids if rid and rid in cited_ids),
        interaction_count=interaction_count,
        loose_ends=loose_ends,
    )


#: The elements an end may attach to, and the lines that have ends.
_WALKED = frozenset(
    {"DataNode", "Label", "Shape", "State", "Group", "Interaction", "GraphicalLine"}
)
#: What "ends near" may name: the boxes a reader sees labelled on the diagram.
_NEAR_KINDS = frozenset({"DataNode", "Label"})
#: How far outside a node's box a free end may lie and still be said to end "near" it. Further
#: than this the arrow is unfinished rather than dropped short of something, and naming the
#: closest node anyway tells the curator to attach it to a node it never pointed at.
_NEAR_MARGIN = 30.0
#: Labels are free text: a Label can run to a paragraph. A finding names it, it does not quote it.
_MAX_LABEL = 40


def _short_label(text: str | None) -> str:
    """A label as a finding names it: on one line, and cut short when long."""
    label = " ".join((text or "").split())
    return label if len(label) <= _MAX_LABEL else label[: _MAX_LABEL - 1].rstrip() + "\u2026"


def _coord(v: str | None) -> float | None:
    try:
        f = float(v) if v is not None else None
    except ValueError:
        return None
    return f if f is not None and math.isfinite(f) else None


def _points(el: ET.Element) -> tuple[ET.Element | None, list[ET.Element]]:
    """A line's point holder and its points, where the renderer finds them: under
    ``<Waypoints>`` in 2021, under ``<Graphics>`` in 2013a."""
    holder = _find_child(el, "Waypoints")
    if holder is None:
        holder = _find_child(el, "Graphics")
    points = [p for p in (holder if holder is not None else []) if _localname(p.tag) == "Point"]
    return holder, points


def _state_placed(el: ET.Element) -> bool:
    """Whether the renderer can place a state: the condition ``app.preview.gpml`` applies."""
    g = _find_child(el, "Graphics")
    rel_x, rel_y = _gpml_f(_gpml_get(g, "RelX")), _gpml_f(_gpml_get(g, "RelY"))
    w, h = _gpml_f(_gpml_get(g, "Width")), _gpml_f(_gpml_get(g, "Height"))
    return None not in (rel_x, rel_y, w, h) and w >= 0 and h >= 0  # type: ignore[operator]


def _loose_ends(root: ET.Element) -> tuple[int, list[LooseEnd]]:
    """Count the interactions and list every end of one that attaches to nothing.

    The question the viewer's "Show unconnected line ends" answers, asked of the GPML so a rule
    can ask it too: an end is attached when its point carries a ``GraphRef`` (2021:
    ``elementRef``) naming an element the renderer draws. A reference to an id nothing declares,
    or to an element the renderer cannot place (a data node without ``<Graphics>``), is as loose
    as no reference: the attachment targets are exactly the ones ``app.preview.render`` resolves
    a ``GraphRef`` against (``_ids``), so this count and the viewer's never disagree. Points are
    not among them: in GPML2021 they carry an ``elementId`` of their own, and a line ending on a
    point is not attached to anything. Both dialects are read through ``app.preview.gpml``'s
    alias table. Only ``<Interaction>``: a graphical line is decoration, and loose ends are what
    it is for. One walk of the tree already parsed, nothing reparsed.
    """
    from app.preview.render import canonical_arrow  # render imports this module

    targets: dict[str, str] = {}  # id -> its label, "" when it has none
    boxes: list[tuple[str, str, tuple[float, float, float, float]]] = []  # (id, label, rect)
    interactions: list[ET.Element] = []
    pending_states: list[tuple[str, str, str]] = []  # (id, label, the node it sits on)
    nodes: set[str] = set()  # ids of the data nodes, labels and shapes the renderer draws

    def declare(gid: str, label: str) -> None:
        if gid and gid not in targets:  # first declaration wins, as in the renderer
            targets[gid] = label

    for el in root.iter():
        kind = _localname(el.tag) if isinstance(el.tag, str) else ""
        if kind not in _WALKED:
            continue
        gid = (_gpml_get(el, "GraphId") or "").strip()
        label = _short_label(_gpml_get(el, "TextLabel"))
        if kind in ("DataNode", "Label", "Shape"):
            rect = _gpml_rect(_find_child(el, "Graphics"))
            if rect is None:
                continue  # not drawn, so nothing an end can attach to
            declare(gid, label)
            if gid:
                nodes.add(gid)
            if kind in _NEAR_KINDS and label:
                boxes.append((gid, label, rect))
            states = _find_child(el, "States") if kind == "DataNode" else None
            for s in states if states is not None else []:
                if _localname(s.tag) == "State" and _state_placed(s):
                    declare((_gpml_get(s, "GraphId") or "").strip(), "")
        elif kind == "State":
            # A 2013a state names its node; a nested 2021 one was declared with its node above.
            ref = (_gpml_get(el, "GraphRef") or "").strip()
            if ref and _state_placed(el):
                pending_states.append((gid, "", ref))
        elif kind == "Group":
            declare(gid, label)
        elif kind in ("Interaction", "GraphicalLine"):
            holder, points = _points(el)
            if len(points) < 2:
                continue  # the renderer draws no line, so neither it nor its anchors exist
            declare(gid, "")
            for a in holder if holder is not None else []:
                if _localname(a.tag) == "Anchor":
                    declare((_gpml_get(a, "GraphId") or "").strip(), "")
            if kind == "Interaction":
                interactions.append(el)
    for gid, label, ref in pending_states:
        if ref in nodes:  # a state is placed on its node, so that node must be drawn
            declare(gid, label)

    def near(p: ET.Element, skip: str) -> str:
        x, y = _coord(_gpml_get(p, "X")), _coord(_gpml_get(p, "Y"))
        if x is None or y is None:
            return ""
        best, best_d = "", _NEAR_MARGIN
        for gid, label, (bx, by, bw, bh) in boxes:
            if skip and gid == skip:
                continue
            # Distance from the point to the box, 0 inside it.
            d = math.hypot(max(bx - x, 0.0, x - bx - bw), max(by - y, 0.0, y - by - bh))
            if d <= best_d:
                best, best_d = label, d
        return best

    count = 0
    loose: list[LooseEnd] = []
    for el in interactions:
        _, points = _points(el)
        count += 1
        ends = []
        for p in (points[0], points[-1]):
            ref = (_gpml_get(p, "GraphRef") or "").strip()
            ends.append((p, ref if ref in targets else ""))
        # Direction follows the arrowhead, as in the viewer: an arrow only on the first point
        # makes the last point the source.
        reverse = bool(canonical_arrow(_gpml_get(points[0], "ArrowHead"))) and not (
            canonical_arrow(_gpml_get(points[-1], "ArrowHead"))
        )
        if reverse:
            ends.reverse()
        gid = (_gpml_get(el, "GraphId") or "").strip()
        for (p, ref), (_, other), role in zip(ends, ends[::-1], ("source", "target"), strict=True):
            if ref:
                continue
            loose.append(
                LooseEnd(
                    interaction=gid,
                    end=role,
                    attached=targets.get(other, "") if other else "",
                    near=near(p, other),
                )
            )
    return count, loose

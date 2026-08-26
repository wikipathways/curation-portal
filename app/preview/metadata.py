"""Parse the curation-facing metadata out of a GPML pathway.

The review dashboard shows more than a picture: the data nodes and their identifiers, the
literature references, the pathway description, and the ontology tags. All of it lives in the
GPML the submitter uploaded, so we parse it once (at preview-render time) and cache it next to
the rendered SVGs, keeping the dashboard render a cheap disk read.

Parsing is namespace-tolerant (GPML uses a default namespace; the embedded Biopax block uses the
biopax-level3 namespace) via local-name matching, the same approach as ``app.preview.render``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

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

    name = root.get("Name")
    organism = root.get("Organism")

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
            database = (xref.get("Database") if xref is not None else "") or ""
            identifier = (xref.get("ID") if xref is not None else "") or ""
            data_nodes.append(
                DataNode(
                    label=(el.get("TextLabel") or "").strip(),
                    type=el.get("Type") or "Unknown",
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
    return CurationMetadata(
        name=name,
        organism=organism,
        description=description,
        data_nodes=data_nodes,
        references=references,
        ontology_tags=_dedupe_ontology_tags(ontology_tags),
        cited_reference_count=sum(1 for rid in reference_ids if rid and rid in cited_ids),
    )

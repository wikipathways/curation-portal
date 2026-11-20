"""Parse GPML (2013a and 2021) into neutral dataclasses for the renderer.

Two dialects carry the same diagram under different spellings: 2013a writes ``CenterX``,
``GraphId``, ``GraphRef``, ``Database``/``ID`` and puts points under ``Graphics``; 2021 writes
``centerX``, ``elementId``, ``elementRef``, ``dataSource``/``identifier``, nests elements in
container lists and points under ``Waypoints``. ``ALIASES`` is the one table that maps them, so
the drawing code never asks which dialect it is looking at.

The parser is the security boundary for the input. A DOCTYPE or ENTITY declaration is refused
outright (no legitimate GPML has one, and internal entities are how a small file expands into a
large one), the element count is capped, and every number goes through ``_f``, which drops
non-finite and absurd values so a ``CenterX="1e300"`` element is skipped rather than drawn.
Values are kept raw otherwise: escaping is the SVG writer's job (``app.preview.svg``).
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

#: More elements than any real pathway has (the largest on WikiPathways is a few thousand);
#: beyond it the render is refused rather than attempted.
MAX_ELEMENTS = 20000

#: Coordinates beyond this are treated as missing (see ``svg.COORD_LIMIT``).
_COORD_LIMIT = 1e6


class RenderError(ValueError):
    """The input could not be parsed as GPML."""


#: canonical name → spellings, 2013a first.
ALIASES: dict[str, tuple[str, ...]] = {
    "GraphId": ("GraphId", "elementId"),
    "GraphRef": ("GraphRef", "elementRef"),
    "GroupRef": ("GroupRef", "groupRef"),
    "GroupId": ("GroupId", "elementId"),
    "TextLabel": ("TextLabel", "textLabel"),
    "Type": ("Type", "type"),
    "Database": ("Database", "dataSource"),
    "ID": ("ID", "identifier"),
    "CenterX": ("CenterX", "centerX"),
    "CenterY": ("CenterY", "centerY"),
    "Width": ("Width", "width"),
    "Height": ("Height", "height"),
    "X": ("X", "x"),
    "Y": ("Y", "y"),
    "RelX": ("RelX", "relX"),
    "RelY": ("RelY", "relY"),
    "ArrowHead": ("ArrowHead", "arrowHead"),
    "Position": ("Position", "position"),
    "AnchorShape": ("Shape", "shapeType"),
    "TextColor": ("Color", "textColor"),
    "BorderColor": ("Color", "borderColor"),
    "LineColor": ("Color", "lineColor"),
    "FillColor": ("FillColor", "fillColor"),
    "LineThickness": ("LineThickness", "lineWidth", "borderWidth"),
    "LineStyle": ("LineStyle", "lineStyle", "borderStyle"),
    "ShapeType": ("ShapeType", "shapeType"),
    "Rotation": ("Rotation", "rotation"),
    "ZOrder": ("ZOrder", "zOrder"),
    "FontSize": ("FontSize", "fontSize"),
    "FontWeight": ("FontWeight", "fontWeight"),
    "FontStyle": ("FontStyle", "fontStyle"),
    "FontName": ("FontName", "fontName"),
    "Align": ("Align", "hAlign"),
    "Valign": ("Valign", "vAlign"),
    "ConnectorType": ("ConnectorType", "connectorType"),
    "Name": ("Name", "title"),
    "Organism": ("Organism", "organism"),
    "BoardWidth": ("BoardWidth", "boardWidth"),
    "BoardHeight": ("BoardHeight", "boardHeight"),
    "GroupStyle": ("Style", "type"),
    "Href": ("Href", "href"),
}

_DOCTYPE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def get(el: ET.Element | None, key: str) -> str | None:
    """The attribute ``key`` under any of its spellings, or None."""
    if el is None:
        return None
    for name in ALIASES.get(key, (key,)):
        v = el.get(name)
        if v is not None:
            return v
    return None


def _f(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or abs(f) > _COORD_LIMIT:
        return None
    return f


def _s(v: str | None) -> str:
    return (v or "").strip()


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if localname(c.tag) == name:
            return c
    return None


def _comments(el: ET.Element) -> list[str]:
    return [(c.text or "").strip() for c in el
            if localname(c.tag) == "Comment" and (c.text or "").strip()]


def _xref(el: ET.Element) -> tuple[str, str]:
    x = _child(el, "Xref")
    return _s(get(x, "Database")), _s(get(x, "ID"))


@dataclass
class Style:
    text_color: str | None = None
    border_color: str | None = None
    fill_color: str | None = None
    line_width: float | None = None
    line_style: str | None = None
    double: bool = False
    shape_type: str | None = None
    rotation: float | None = None
    z: float | None = None
    font_size: float | None = None
    font_weight: str | None = None
    font_style: str | None = None
    font_name: str | None = None
    align: str | None = None
    valign: str | None = None
    connector: str | None = None


@dataclass
class NodeEl:
    """A DataNode, Label or Shape (``kind``): anything drawn as a box with text."""

    kind: str
    index: int
    graph_id: str
    label: str
    type: str
    rect: tuple[float, float, float, float]
    style: Style
    group_ref: str = ""
    database: str = ""
    identifier: str = ""
    comments: list[str] = field(default_factory=list)
    href: str = ""


@dataclass
class StateEl:
    index: int
    graph_id: str
    parent: int  # index into Pathway.nodes
    label: str
    type: str
    rel_x: float
    rel_y: float
    width: float
    height: float
    style: Style
    database: str = ""
    identifier: str = ""
    comments: list[str] = field(default_factory=list)


@dataclass
class GroupEl:
    index: int
    group_id: str  # what members' GroupRef names (2013a GroupId; 2021 elementId)
    graph_id: str  # what lines' GraphRef names (2013a GraphId; 2021 elementId)
    style_name: str
    label: str
    group_ref: str
    style: Style
    declared_rect: tuple[float, float, float, float] | None = None
    database: str = ""
    identifier: str = ""
    comments: list[str] = field(default_factory=list)


@dataclass
class PointEl:
    x: float | None
    y: float | None
    ref: str
    rel_x: float | None
    rel_y: float | None
    arrow: str


@dataclass
class AnchorEl:
    graph_id: str
    position: float
    shape: str


@dataclass
class EdgeEl:
    kind: str  # Interaction | GraphicalLine
    index: int
    graph_id: str
    points: list[PointEl]
    anchors: list[AnchorEl]
    style: Style
    group_ref: str = ""
    database: str = ""
    identifier: str = ""
    comments: list[str] = field(default_factory=list)


@dataclass
class Pathway:
    dialect: str
    name: str
    organism: str
    board: tuple[float, float] | None
    nodes: list[NodeEl] = field(default_factory=list)
    states: list[StateEl] = field(default_factory=list)
    groups: list[GroupEl] = field(default_factory=list)
    edges: list[EdgeEl] = field(default_factory=list)
    #: Elements carrying a ``Graphics`` block, i.e. that the source meant to be drawn, whether
    #: or not this parser could place them. Lets the caller tell "an empty pathway" from "a
    #: pathway we failed to draw", which look the same as SVG. An element with no Graphics at
    #: all is not counted: there is nothing there to have failed on.
    declared: int = 0


def _style(g: ET.Element | None, el: ET.Element, dialect: str) -> Style:
    st = Style()
    if g is None:
        g = ET.Element("Graphics")
    st.text_color = get(g, "TextColor")
    st.border_color = get(g, "BorderColor")
    is_line = localname(el.tag) in ("Interaction", "GraphicalLine")
    if dialect == "2021" and is_line:
        st.border_color = get(g, "LineColor")
    st.fill_color = get(g, "FillColor")
    st.line_width = _f(get(g, "LineThickness"))
    st.line_style = get(g, "LineStyle")
    st.shape_type = get(g, "ShapeType")
    st.rotation = _f(get(g, "Rotation"))
    st.z = _f(get(g, "ZOrder"))
    st.font_size = _f(get(g, "FontSize"))
    st.font_weight = get(g, "FontWeight")
    st.font_style = get(g, "FontStyle")
    st.font_name = get(g, "FontName")
    st.align = get(g, "Align")
    st.valign = get(g, "Valign")
    st.connector = get(g, "ConnectorType")
    # 2013a marks a double line with a PathVisio attribute rather than a line style.
    for a in el:
        if (localname(a.tag) == "Attribute" and a.get("Key") == "org.pathvisio.DoubleLineProperty"
                and (a.get("Value") or "").lower() == "double"):
            st.double = True
    if (st.line_style or "").lower() == "double":
        st.double = True
    return st


def _rect(g: ET.Element | None) -> tuple[float, float, float, float] | None:
    if g is None:
        return None
    cx, cy = _f(get(g, "CenterX")), _f(get(g, "CenterY"))
    w, h = _f(get(g, "Width")), _f(get(g, "Height"))
    if cx is None or cy is None or w is None or h is None or w < 0 or h < 0:
        return None
    return cx - w / 2, cy - h / 2, w, h


def detect_dialect(root: ET.Element) -> str:
    ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if ns.rstrip("/").endswith("2021"):
        return "2021"
    for el in root.iter():
        if el.get("centerX") is not None or el.get("elementId") is not None:
            return "2021"
    return "2013a"


def parse_pathway(gpml: bytes | str) -> Pathway:
    """Parse GPML into a ``Pathway``. Raises ``RenderError`` when the input is not GPML at all.

    An element that cannot be placed (no Graphics, missing or absurd coordinates) is skipped,
    never raised: a bad element costs that element, not the drawing.
    """
    text = gpml.decode("utf-8", "replace") if isinstance(gpml, bytes) else gpml
    if _DOCTYPE.search(text):
        raise RenderError("DOCTYPE and ENTITY declarations are not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RenderError(f"not parseable XML: {exc}") from exc
    if localname(root.tag) != "Pathway":
        raise RenderError("root element is not <Pathway>")
    count = 0
    for _ in root.iter():
        count += 1
        if count > MAX_ELEMENTS:
            raise RenderError(f"more than {MAX_ELEMENTS} elements")

    dialect = detect_dialect(root)
    pg = _child(root, "Graphics")
    bw, bh = _f(get(pg, "BoardWidth")), _f(get(pg, "BoardHeight"))
    pw = Pathway(
        dialect=dialect,
        name=_s(get(root, "Name")),
        organism=_s(get(root, "Organism")),
        board=(bw, bh) if bw and bh and bw > 0 and bh > 0 else None,
    )

    pending_states: list[tuple[ET.Element, str | None, int | None]] = []
    node_by_gid: dict[str, int] = {}
    index = 0

    for el in root.iter():
        kind = localname(el.tag)
        if kind not in ("DataNode", "Label", "Shape", "Interaction", "GraphicalLine", "Group",
                        "State"):
            continue
        index += 1
        if kind in ("DataNode", "Label", "Shape"):
            g = _child(el, "Graphics")
            pw.declared += g is not None
            rect = _rect(g)
            if rect is None:
                continue
            db, ident = _xref(el) if kind == "DataNode" else ("", "")
            node = NodeEl(
                kind=kind,
                index=index,
                graph_id=_s(get(el, "GraphId")),
                label=get(el, "TextLabel") or "",
                type=_s(get(el, "Type")) or ("Unknown" if kind == "DataNode" else ""),
                rect=rect,
                style=_style(g, el, dialect),
                group_ref=_s(get(el, "GroupRef")),
                database=db,
                identifier=ident,
                comments=_comments(el),
                href=_s(get(el, "Href")) if kind == "Label" else "",
            )
            pw.nodes.append(node)
            if node.graph_id and node.graph_id not in node_by_gid:
                node_by_gid[node.graph_id] = len(pw.nodes) - 1
            if kind == "DataNode":
                # 2021 nests a node's states inside it, so their parent is the enclosing node.
                states = _child(el, "States")
                for s in (states if states is not None else []):
                    if localname(s.tag) == "State":
                        pending_states.append((s, None, len(pw.nodes) - 1))
        elif kind == "State":
            if el.get("GraphRef") is not None or el.get("elementRef") is not None:
                pending_states.append((el, _s(get(el, "GraphRef")), None))
            # A nested 2021 State was queued by its DataNode above.
        elif kind in ("Interaction", "GraphicalLine"):
            g = _child(el, "Graphics")
            pw.declared += g is not None
            # Element truthiness is "has children", so this cannot be an ``or``.
            holder = _child(el, "Waypoints")
            if holder is None:
                holder = g
            points: list[PointEl] = []
            anchors: list[AnchorEl] = []
            if holder is not None:
                for p in holder:
                    pk = localname(p.tag)
                    if pk == "Point":
                        points.append(PointEl(
                            x=_f(get(p, "X")),
                            y=_f(get(p, "Y")),
                            ref=_s(get(p, "GraphRef")),
                            rel_x=_f(get(p, "RelX")),
                            rel_y=_f(get(p, "RelY")),
                            arrow=_s(get(p, "ArrowHead")),
                        ))
                    elif pk == "Anchor":
                        pos = _f(get(p, "Position"))
                        anchors.append(AnchorEl(
                            graph_id=_s(get(p, "GraphId")),
                            position=min(1.0, max(0.0, pos if pos is not None else 0.5)),
                            shape=_s(get(p, "AnchorShape")) or "None",
                        ))
            if len(points) < 2:
                continue
            db, ident = _xref(el)
            pw.edges.append(EdgeEl(
                kind=kind,
                index=index,
                graph_id=_s(get(el, "GraphId")),
                points=points,
                anchors=anchors,
                style=_style(g, el, dialect),
                group_ref=_s(get(el, "GroupRef")),
                database=db,
                identifier=ident,
                comments=_comments(el),
            ))
        elif kind == "Group":
            # Not counted: a 2013a group has no Graphics and is drawn only through its members.
            g = _child(el, "Graphics")
            db, ident = _xref(el)
            if dialect == "2021":
                gid = _s(el.get("elementId"))
                group_id, graph_id = gid, gid
            else:
                group_id, graph_id = _s(el.get("GroupId")), _s(el.get("GraphId"))
            pw.groups.append(GroupEl(
                index=index,
                group_id=group_id,
                graph_id=graph_id,
                style_name=_s(get(el, "GroupStyle")) or "None",
                label=get(el, "TextLabel") or "",
                group_ref=_s(get(el, "GroupRef")),
                style=_style(g, el, dialect),
                declared_rect=_rect(g),
                database=db,
                identifier=ident,
                comments=_comments(el),
            ))

    for s, ref, parent in pending_states:
        if parent is None:
            parent = node_by_gid.get(ref or "")
        if parent is None:
            continue
        g = _child(s, "Graphics")
        rel_x, rel_y = _f(get(g, "RelX")), _f(get(g, "RelY"))
        w, h = _f(get(g, "Width")), _f(get(g, "Height"))
        if None in (rel_x, rel_y, w, h) or w < 0 or h < 0:  # type: ignore[operator]
            continue
        db, ident = _xref(s)
        index += 1
        pw.states.append(StateEl(
            index=index,
            graph_id=_s(get(s, "GraphId")),
            parent=parent,
            label=get(s, "TextLabel") or "",
            type=_s(s.get("StateType") or s.get("type")),
            rel_x=rel_x,  # type: ignore[arg-type]
            rel_y=rel_y,  # type: ignore[arg-type]
            width=w,  # type: ignore[arg-type]
            height=h,  # type: ignore[arg-type]
            style=_style(g, s, dialect),
            database=db,
            identifier=ident,
            comments=_comments(s),
        ))
    return pw

"""Instant, dependency-free GPML -> SVG renderer for the before/after preview (issue #11, 1a).

The PR-preview *workflow* still produces the canonical artifacts (metadata, datanodes, refs,
validation, and the ``WP<id>.json`` pvjson), but the human-facing preview no longer waits on CI:
the app has the submitted GPML bytes at PR-creation time, so it renders the "after" here and the
"before" from the base-branch GPML, instantly.

Renderer 2 aims at what the WikiPathways site draws, closely enough that a curator reading the
portal's picture reads the same pathway: paint order from ``ZOrder``, Complex groups as octagons,
the MIM arrowheads, straight connectors as straight lines, wrapped and aligned text. It is split
in three: ``gpml`` parses both dialects into neutral dataclasses, ``geometry`` does the
arithmetic, ``svg`` owns the vocabulary and the escaping. This module joins them and keeps the
public surface the rest of the app already uses (``render_gpml``, ``render_gpml_with_nodes``,
``NodeHotspot``, ``RenderError``).

Alongside the SVG it produces the *graph* (``render_gpml_full(...).graph``): every element's id,
geometry and meaning, and what each line connects, with GraphRefs resolved. The viewer uses it
for selection, highlighting and search; it is built in the same pass as the drawing, so an id in
the graph is always an element in the picture.

``render_gpml`` never raises on a malformed *element* (it is skipped); it raises ``RenderError``
only when the input is not parseable GPML at all, so a bad upload degrades to the placeholder
rather than a 500.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.preview import geometry as geo
from app.preview.gpml import (
    EdgeEl,
    GroupEl,
    NodeEl,
    Pathway,
    RenderError,
    StateEl,
    parse_pathway,
)
from app.preview.metadata import resolver_url
from app.preview.svg import (
    Builder,
    UnsafeSvg,
    _num,
    assert_safe_svg,
    attr_text,
    clean_text,
    font_stack,
    safe_color,
)

__all__ = [
    "RENDERER_VERSION",
    "NodeHotspot",
    "RenderError",
    "RenderResult",
    "render_gpml",
    "render_gpml_full",
    "render_gpml_with_nodes",
]

#: Bumped whenever the drawing or the graph sidecar changes in a way an old cache should not keep
#: showing: ``PreviewService.needs_rerender`` compares a cache's stamp against it.
RENDERER_VERSION = 2

_PAD = 15.0  # space around the content when it reaches past (or there is no) declared board

#: PathVisio's font size when a GPML does not say. The site renders the same default.
DEFAULT_FONT_SIZE = 12.0
STATE_FONT_SIZE = 10.0

#: PathVisio's default z-order by element kind, for a GPML that does not set ``ZOrder``.
_DEFAULT_Z = {
    "DataNode": 0x8000,
    "Label": 0x7000,
    "Shape": 0x4000,
    "Interaction": 0x3000,
    "GraphicalLine": 0x3000,
    "Group": 0x1000,
}
# Ties in z are broken by kind, then document order: outlines under boxes under text.
_KIND_RANK = {"Group": 0, "Shape": 1, "Interaction": 2, "GraphicalLine": 2, "DataNode": 3,
              "Label": 4, "State": 5}

_ID_PREFIX = {"DataNode": "n-", "Label": "l-", "Shape": "s-", "Group": "g-", "Interaction": "e-",
              "GraphicalLine": "e-", "State": "st-", "Anchor": "a-"}
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

_GROUP_STROKE = "#808080"
_COMPLEX_FILL = "#b4b464"


# -- arrowheads --------------------------------------------------------------------------------

#: Every arrowhead spelling (2013a ``mim-*`` and ``Arrow``/``TBar``, 2021 ``Conversion`` and
#: friends), keyed by its letters alone, mapped onto one canonical set.
_ARROW_CANON = {
    "arrow": "arrow", "directed": "arrow",
    "conversion": "mim-conversion",
    "stimulation": "mim-stimulation",
    "necessarystimulation": "mim-necessary-stimulation",
    "catalysis": "mim-catalysis",
    "inhibition": "mim-inhibition",
    "tbar": "tbar",
    "binding": "mim-binding",
    "transcriptiontranslation": "mim-transcription-translation",
    "translation": "mim-transcription-translation",
    "translocation": "mim-translocation",
    "modification": "mim-modification",
    "cleavage": "mim-cleavage",
    "branchingleft": "mim-branching-left",
    "branchingright": "mim-branching-right",
    "gap": "mim-gap",
    "covalentbond": "mim-covalent-bond",
    "receptor": "receptor-round", "receptorround": "receptor-round",
    "receptorsquare": "receptor-square",
    "ligand": "ligand-round", "ligandround": "ligand-round", "ligandsquare": "ligand-square",
    "line": None, "undirected": None, "none": None, "": None,
}


def canonical_arrow(raw: str | None) -> str | None:
    """The canonical arrowhead for any GPML spelling; None for a plain line end.

    An unknown non-empty value draws as a plain arrow: it was meant to point somewhere.
    """
    key = "".join(ch for ch in (raw or "").lower() if ch.isalnum())
    if key.startswith("mim"):
        key = key[3:]
    if key in _ARROW_CANON:
        return _ARROW_CANON[key]
    return "arrow"


def type_label(canonical: str | None) -> str:
    if not canonical:
        return "line"
    return canonical.removeprefix("mim-").replace("-", " ")


@dataclass(frozen=True)
class _Marker:
    """An arrowhead drawn pointing along +x with its tip at ``x = width``.

    ``inset`` is how far the line is pulled back from the endpoint so it stops where the
    arrowhead's outline begins (an open triangle with a line through it reads as a different
    symbol). ``refX`` follows from it, so the tip still lands on the endpoint.
    """

    width: float
    height: float
    inset: float
    shapes: tuple  # ((tag, attrs, paint) ...) where paint is "fill" | "stroke" | "open"


_MARKERS: dict[str, _Marker] = {
    "arrow": _Marker(12, 12, 6, (("polygon", {"points": "0,1 12,6 0,11"}, "fill"),)),
    "mim-conversion": _Marker(12, 12, 6, (("polygon", {"points": "0,1 12,6 0,11"}, "fill"),)),
    "mim-translocation": _Marker(12, 12, 6, (("polygon", {"points": "0,1 12,6 0,11"}, "fill"),)),
    "mim-modification": _Marker(12, 12, 6, (("polygon", {"points": "1,1 12,6 1,11"}, "fill"),)),
    "mim-binding": _Marker(12, 12, 5, (("polygon", {"points": "0,0 12,6 0,12 7,6"}, "fill"),)),
    "mim-stimulation": _Marker(12, 12, 11, (("polygon", {"points": "1,1 12,6 1,11"}, "open"),)),
    "mim-necessary-stimulation": _Marker(16, 12, 9, (
        ("line", {"x1": "2", "y1": "0", "x2": "2", "y2": "12"}, "stroke"),
        ("polygon", {"points": "7,1 16,6 7,11"}, "open"),
    )),
    "mim-catalysis": _Marker(12, 12, 11, (
        ("circle", {"cx": "6", "cy": "6", "r": "5.3"}, "open"),)),
    # The bar sits a little short of the target, as on the site, so it is not lost under the
    # border of the box it points at.
    "mim-inhibition": _Marker(10, 20, 3, (
        ("line", {"x1": "7", "y1": "0", "x2": "7", "y2": "20", "stroke-width": "1.8"}, "stroke"),)),
    "tbar": _Marker(10, 20, 3, (
        ("line", {"x1": "7", "y1": "0", "x2": "7", "y2": "20", "stroke-width": "1.8"}, "stroke"),)),
    "mim-transcription-translation": _Marker(20, 24, 15, (
        ("path", {"d": "M5,12 L5,19 L12,19"}, "stroke"),
        ("polygon", {"points": "12,15 20,19 12,23"}, "open"),
    )),
    "mim-cleavage": _Marker(20, 30, 18, (("path", {"d": "M2,15 L2,0 L20,30"}, "stroke"),)),
    "mim-gap": _Marker(12, 12, 6, ()),
    "mim-covalent-bond": _Marker(12, 12, 11, (
        ("path", {"d": "M1,1 L11,1 L11,11 L1,11"}, "stroke"),)),
    "mim-branching-left": _Marker(12, 12, 0, (("path", {"d": "M12,6 L4,0"}, "stroke"),)),
    "mim-branching-right": _Marker(12, 12, 0, (("path", {"d": "M12,6 L4,12"}, "stroke"),)),
    "receptor-round": _Marker(12, 12, 6, (("path", {"d": "M12,1 A5,5 0 0 0 12,11"}, "stroke"),)),
    "receptor-square": _Marker(12, 12, 6, (("path", {"d": "M12,1 L6,1 L6,11 L12,11"}, "stroke"),)),
    "ligand-round": _Marker(12, 12, 6, (("circle", {"cx": "6", "cy": "6", "r": "5"}, "fill"),)),
    "ligand-square": _Marker(12, 12, 6, (
        ("rect", {"x": "1", "y": "1", "width": "10", "height": "10"}, "fill"),)),
}


class _MarkerRegistry:
    """One ``<marker>`` per (arrowhead, colour) actually used, so the defs stay small."""

    def __init__(self) -> None:
        self._used: dict[tuple[str, str], str] = {}

    def use(self, kind: str, color: str) -> str:
        key = (kind, color)
        if key not in self._used:
            suffix = "" if color == "#000000" else "-" + color.lstrip("#")
            self._used[key] = f"wp-m-{kind}{suffix}"
        return self._used[key]

    def defs(self) -> str:
        b = Builder()
        for (kind, color), mid in self._used.items():
            m = _MARKERS[kind]
            b.open("marker", {
                "id": mid,
                "viewBox": f"0 0 {_num(m.width)} {_num(m.height)}",
                "markerWidth": _num(m.width),
                "markerHeight": _num(m.height),
                "refX": _num(m.width - m.inset),
                "refY": _num(m.height / 2),
                # One marker serves both ends: at the start it is flipped to point backwards.
                "orient": "auto-start-reverse",
                "markerUnits": "userSpaceOnUse",
            })
            for tag, attrs, paint in m.shapes:
                style = {
                    "fill": {"fill": color, "stroke": "none"},
                    "open": {"fill": "#ffffff", "stroke": color, "stroke-width": "1"},
                    "stroke": {"fill": "none", "stroke": color, "stroke-width": "1"},
                }[paint]
                b.leaf(tag, {**style, **attrs})
            b.close("marker")
        return b.markup()


# -- public types ------------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeHotspot:
    """One clickable data node: where it sits on the drawing, and what to say about it.

    Geometry is a **percentage of the viewBox**, not user units, so the overlay lines up at any
    size without the client knowing the coordinate system.

    Built in the same pass that draws the node, from the same element. An earlier sketch joined
    ``render`` geometry to ``metadata`` properties by list index; that only holds while both
    passes skip exactly the same elements, and they do not — the renderer drops a DataNode with
    no ``Graphics``, the metadata parser keeps it.
    """

    left: float
    top: float
    width: float
    height: float
    label: str
    type: str
    database: str
    identifier: str
    url: str | None
    comment: str
    # Identity and position for the before/after diff (issue #24), not for the overlay.
    #
    # ``graph_id`` is GPML's own per-element id. PathVisio preserves it across an edit, which
    # makes it the one reliable way to say "this is the same node"; it is absent or rewritten
    # often enough that the diff cannot rely on it alone.
    #
    # ``cx``/``cy`` are the centre in *user units*, deliberately not the percentages above: a
    # submitter who changes BoardWidth moves every percentage without moving anything on the
    # diagram, so a diff computed from them would report the whole pathway as relocated.
    graph_id: str = ""
    cx: float | None = None
    cy: float | None = None
    #: The node's ``data-id`` in the SVG and the graph sidecar (``n-<GraphId>``).
    id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RenderResult:
    svg: bytes
    hotspots: list[NodeHotspot]
    graph: dict
    #: Elements actually drawn. Zero with ``declared`` > 0 means the file has content this
    #: renderer could not place, which the cache records as a failed render, not a blank one.
    element_count: int
    dialect: str
    declared: int = 0
    warnings: list[str] = field(default_factory=list)


def render_gpml(gpml: bytes | str) -> bytes:
    """Render a GPML pathway to a standalone SVG (bytes). Raises RenderError on non-GPML input."""
    return render_gpml_full(gpml).svg


def render_gpml_with_nodes(gpml: bytes | str) -> tuple[bytes, list[NodeHotspot]]:
    """Render to SVG *and* return the clickable data-node hotspots (issue #14)."""
    r = render_gpml_full(gpml)
    return r.svg, r.hotspots


def render_gpml_full(gpml: bytes | str) -> RenderResult:
    """Render to SVG, hotspots and the graph sidecar in one pass.

    Raises only ``RenderError``. Anything else escaping the drawing is a renderer bug that some
    file found, and the callers' contract (a failed preview, never a crash) must hold for it too.
    """
    try:
        pw = parse_pathway(gpml)
        result = _Renderer(pw).run()
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001 — the safety net; see the docstring
        raise RenderError(f"renderer failed: {type(exc).__name__}") from exc
    try:
        assert_safe_svg(result.svg)
    except UnsafeSvg as exc:  # a renderer bug, never the input's fault — but never serve it
        raise RenderError(f"renderer produced unsafe SVG: {exc}") from exc
    return result


# -- the drawing -------------------------------------------------------------------------------

def _slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out[:40] or "unknown"


def _safe_https(url: str | None) -> str | None:
    u = (url or "").strip()
    if re.match(r"^https://[^\s<>\"']+$", u):
        return u
    return None


def _assign_ids(entries: list[tuple[object, str, str]]) -> dict[object, str]:
    """``data-id`` for every element: prefix + its own id when that is safe and unique, else
    ``auto<i>``. Done in two passes so an ``auto`` id can never shadow a real one."""
    taken: set[str] = set()
    out: dict[object, str] = {}
    later: list[tuple[object, str]] = []
    for key, prefix, raw in entries:
        cand = prefix + raw if raw and _SAFE_ID.fullmatch(raw) else None
        if cand and cand not in taken:
            taken.add(cand)
            out[key] = cand
        else:
            later.append((key, prefix))
    i = 0
    for key, prefix in later:
        while True:
            i += 1
            cand = f"{prefix}auto{i}"
            if cand not in taken:
                break
        taken.add(cand)
        out[key] = cand
    return out


def _width(v: float | None) -> float:
    """A stroke width: GPML's value, defaulting to 1 and kept within reason (a hostile 1e5 would
    paint the whole board)."""
    return 1.0 if v is None else max(0.0, min(v, 20.0))


def _font_size(v: float | None, default: float) -> float:
    return default if not v or v <= 0 else max(2.0, min(v, 72.0))


def _group_style(name: str) -> str:
    """complex | group | none | pathway. 2021's ``Transparent`` is 2013a's ``None``; anything
    unrecognised is drawn as a plain group, the most neutral outline."""
    s = _slug(name)
    if s == "transparent":
        return "none"
    return s if s in ("complex", "group", "none", "pathway") else "group"


class _Renderer:
    def __init__(self, pw: Pathway) -> None:
        self.pw = pw
        self.markers = _MarkerRegistry()
        self.items: list[tuple[float, int, int, str]] = []  # (z, rank, doc index, markup)
        self.extent: list[geo.Rect] = []
        self.drawn = 0

    # identity ----------------------------------------------------------------------------
    def _ids(self) -> None:
        pw = self.pw
        entries: list[tuple[object, str, str]] = []
        for n in pw.nodes:
            entries.append((("node", n.index), _ID_PREFIX[n.kind], n.graph_id))
        for g in pw.groups:
            entries.append((("group", g.index), "g-", g.group_id or g.graph_id))
        for e in pw.edges:
            entries.append((("edge", e.index), "e-", e.graph_id))
            for k, a in enumerate(e.anchors):
                entries.append((("anchor", e.index, k), "a-", a.graph_id))
        for s in pw.states:
            entries.append((("state", s.index), "st-", s.graph_id))
        self.did = _assign_ids(entries)

        # GraphRef targets: anything with a GraphId. First declaration wins on a duplicate.
        self.by_ref: dict[str, str] = {}
        self.group_by_key: dict[str, str] = {}
        self.anchor_edge: dict[str, str] = {}

        def ref(raw: str, did: str) -> None:
            if raw and raw not in self.by_ref:
                self.by_ref[raw] = did

        for n in pw.nodes:
            ref(n.graph_id, self.did[("node", n.index)])
        for s in pw.states:
            ref(s.graph_id, self.did[("state", s.index)])
        for g in pw.groups:
            gdid = self.did[("group", g.index)]
            ref(g.graph_id, gdid)
            if g.group_id and g.group_id not in self.group_by_key:
                self.group_by_key[g.group_id] = gdid
        for e in pw.edges:
            edid = self.did[("edge", e.index)]
            ref(e.graph_id, edid)
            for k, a in enumerate(e.anchors):
                adid = self.did[("anchor", e.index, k)]
                ref(a.graph_id, adid)
                self.anchor_edge[adid] = edid

    def _group_of(self, raw: str) -> str | None:
        if not raw:
            return None
        if raw in self.group_by_key:
            return self.group_by_key[raw]
        hit = self.by_ref.get(raw)
        return hit if hit and hit.startswith("g-") else None

    # paint helpers -----------------------------------------------------------------------
    def _add(self, z: float, kind: str, index: int, markup: str) -> None:
        self.items.append((z, _KIND_RANK[kind], index, markup))
        self.drawn += 1

    @staticmethod
    def _line_attrs(color: str | None, width: float, style: str | None) -> dict:
        attrs: dict = {"stroke": color or "none", "stroke-width": _num(width)}
        if (style or "").lower() in ("broken", "dashed", "dotted"):
            dash = max(1.0, width) * 4
            attrs["stroke-dasharray"] = f"{_num(dash)},{_num(dash)}"
        return attrs

    def _shape(self, b: Builder, shape_type: str | None, rect: geo.Rect, *, stroke: str | None,
               fill: str | None, width: float, line_style: str | None, double: bool,
               cls: str) -> None:
        elements = geo.shape_elements(shape_type, rect, num=_num)
        line = self._line_attrs(stroke, width, line_style)
        if double and stroke:
            # Two strokes with white between: a wide coloured one, then a narrow white one.
            for tag, attrs in elements:
                b.leaf(tag, {"class": cls, "fill": fill or "none", **line,
                             "stroke-width": _num(width * 3), **attrs})
            for tag, attrs in elements:
                b.leaf(tag, {"fill": "none", **attrs, "stroke": "#ffffff",
                             "stroke-width": _num(width)})
            return
        for tag, attrs in elements:
            b.leaf(tag, {"class": cls, "fill": fill or "none", **line, **attrs})

    def _text(self, b: Builder, text: str, rect: geo.Rect, *, color: str, font_size: float,
              weight: str | None, style: str | None, font: str | None, align: str | None,
              valign: str | None, cls: str, clip: bool = True) -> None:
        text = clean_text(text)
        if not text.strip():
            return
        x, y, w, h = rect
        bold = (weight or "").lower() == "bold"
        lines = geo.wrap_text(text, w, h, font_size, bold=bold, clip=clip)
        lh = geo.LINE_HEIGHT * font_size
        block = lh * len(lines)
        va = (valign or "Middle").lower()
        if va == "top":
            top = y + 2
        elif va == "bottom":
            top = y + h - 2 - block
        else:
            top = y + h / 2 - block / 2
        al = (align or "Center").lower()
        if al == "left":
            tx, anchor = x + 2, "start"
        elif al == "right":
            tx, anchor = x + w - 2, "end"
        else:
            tx, anchor = x + w / 2, "middle"
        b.open("text", {
            "class": cls,
            "x": _num(tx),
            "text-anchor": anchor,
            "font-family": font_stack(font),
            "font-size": _num(font_size),
            "font-weight": "bold" if bold else None,
            "font-style": "italic" if (style or "").lower() == "italic" else None,
            "fill": color,
        })
        for i, line in enumerate(lines):
            # Baseline from the line's centre: renderers disagree on dominant-baseline, and a
            # fixed 0.35em drop centres cap height closely enough everywhere.
            baseline = top + lh * (i + 0.5) + 0.35 * font_size
            b.leaf("tspan", {"x": _num(tx), "y": _num(baseline)}, line)
        b.close("text")

    # elements ----------------------------------------------------------------------------
    def _node_z(self, n: NodeEl) -> float:
        return n.style.z if n.style.z is not None else _DEFAULT_Z[n.kind]

    def _draw_node(self, n: NodeEl) -> None:
        did = self.did[("node", n.index)]
        st = n.style
        rect = n.rect
        x, y, w, h = rect
        self.extent.append(rect)
        # Colours: 2013a's Color is both text and border; 2021 splits them. DataNodes keep
        # PathVisio's white fill; Labels and Shapes are see-through unless a fill is set.
        text_color = safe_color(st.text_color, "#000000") or "#000000"
        border = safe_color(st.border_color, "#000000")
        if n.kind == "DataNode":
            fill = safe_color(st.fill_color, "#ffffff")
            shape_type = st.shape_type or "Rectangle"
        elif n.kind == "Shape":
            fill = safe_color(st.fill_color, None)
            shape_type = st.shape_type or "Rectangle"
        else:  # Label: no outline unless one was asked for
            fill = safe_color(st.fill_color, None)
            shape_type = st.shape_type or "None"
        width = _width(st.line_width)
        group = self._group_of(n.group_ref)

        b = Builder()
        if n.kind == "DataNode":
            cls = f"wp-node wp-node--{_slug(n.type)}"
        elif n.kind == "Shape":
            cls = f"wp-shape wp-shape--{_slug(shape_type)}"
        else:
            cls = "wp-label"
        b.open("g", {
            "class": cls,
            "data-id": did,
            "data-graph-id": n.graph_id if _SAFE_ID.fullmatch(n.graph_id or "") else None,
            "data-group": group,
            "transform": geo.rotation_transform(st.rotation, x + w / 2, y + h / 2, _num),
        })
        part = {"DataNode": "wp-node", "Shape": "wp-shape", "Label": "wp-label"}[n.kind]
        if geo.shape_key(shape_type) != "none":
            self._shape(b, shape_type, rect, stroke=border, fill=fill, width=width,
                        line_style=st.line_style, double=st.double, cls=f"{part}__shape")
        elif n.kind == "DataNode":
            # No outline, but still something to point at: an invisible box over the node.
            b.leaf("rect", {"class": f"{part}__shape", **geo._rect(rect, _num),
                            "fill": "#ffffff", "fill-opacity": "0", "stroke": "none"})
        self._text(b, n.label, rect, color=text_color,
                   font_size=_font_size(st.font_size, DEFAULT_FONT_SIZE), weight=st.font_weight,
                   style=st.font_style, font=st.font_name, align=st.align, valign=st.valign,
                   cls=f"{part}__label", clip=n.kind == "DataNode")
        b.close("g")
        self._add(self._node_z(n), n.kind, n.index, b.markup())

    def _state_rect(self, s: StateEl) -> geo.Rect:
        px, py, pw_, ph = self.pw.nodes[s.parent].rect
        cx, cy = geo.attach_xy((px, py, pw_, ph), s.rel_x, s.rel_y)
        return cx - s.width / 2, cy - s.height / 2, s.width, s.height

    def _draw_state(self, s: StateEl) -> None:
        parent = self.pw.nodes[s.parent]
        rect = self._state_rect(s)
        self.extent.append(rect)
        st = s.style
        b = Builder()
        b.open("g", {
            "class": "wp-state",
            "data-id": self.did[("state", s.index)],
            "data-parent": self.did[("node", parent.index)],
        })
        self._shape(b, st.shape_type or "Rectangle", rect,
                    stroke=safe_color(st.border_color, "#000000"),
                    fill=safe_color(st.fill_color, "#ffffff"),
                    width=_width(st.line_width),
                    line_style=st.line_style, double=st.double, cls="wp-state__shape")
        self._text(b, s.label, rect, color=safe_color(st.text_color, "#000000") or "#000000",
                   font_size=_font_size(st.font_size,
                                        min(STATE_FONT_SIZE, max(4.0, s.height * 0.8))),
                   weight=st.font_weight, style=st.font_style, font=st.font_name,
                   align="Center", valign="Middle", cls="wp-state__label")
        b.close("g")
        self._add(self._node_z(parent) + 0.1, "State", s.index, b.markup())

    # groups ------------------------------------------------------------------------------
    def _groups(self) -> None:
        pw = self.pw
        members: dict[str, list[str]] = {self.did[("group", g.index)]: [] for g in pw.groups}
        rects: dict[str, geo.Rect] = {}
        zs: dict[str, float] = {}
        for n in pw.nodes:
            did = self.did[("node", n.index)]
            rects[did] = n.rect
            zs[did] = self._node_z(n)
            gid = self._group_of(n.group_ref)
            if gid in members:
                members[gid].append(did)
        self.group_parent: dict[str, str | None] = {}
        for g in pw.groups:
            gdid = self.did[("group", g.index)]
            parent = self._group_of(g.group_ref)
            if parent == gdid:
                parent = None
            self.group_parent[gdid] = parent
            if parent in members:
                members[parent].append(gdid)
        for g in pw.groups:
            gdid = self.did[("group", g.index)]
            if not members[gdid] and g.declared_rect is not None:
                rects[gdid] = g.declared_rect
                members[gdid] = [gdid + "#declared"]
                rects[gdid + "#declared"] = g.declared_rect
        self.group_members = {k: [m for m in v if "#" not in m] for k, v in members.items()}
        self.group_bbox = geo.group_bboxes(members, rects)

        # A group without its own ZOrder sits just under its lowest member. Folded without
        # recursion, like the outlines: nesting depth is the file's to choose.
        derived_z = geo.fold_groups(
            self.group_members, zs.get, lambda vals: (min(vals) - 1) if vals else None
        )

        for g in pw.groups:
            gdid = self.did[("group", g.index)]
            box = self.group_bbox.get(gdid)
            if box is None:
                continue
            z = g.style.z
            if z is None:
                z = derived_z.get(gdid)
            if z is None:
                z = _DEFAULT_Z["Group"]
            self._draw_group(g, gdid, box, z)

    def _draw_group(self, g: GroupEl, gdid: str, box: geo.Rect, z: float) -> None:
        self.extent.append(box)
        style = _group_style(g.style_name)
        b = Builder()
        b.open("g", {
            "class": f"wp-group wp-group--{style}",
            "data-id": gdid,
            "data-graph-id": g.graph_id if _SAFE_ID.fullmatch(g.graph_id or "") else None,
            "data-parent": self.group_parent.get(gdid),
        })
        if style == "complex":
            b.leaf("path", {"class": "wp-group__outline", "d": geo.octagon_d(box, _num),
                            "fill": _COMPLEX_FILL, "fill-opacity": "0.1",
                            "stroke": _GROUP_STROKE, "stroke-width": "1"})
        else:
            x, y, w, h = box
            d = f"M{_num(x)},{_num(y)} H{_num(x + w)} V{_num(y + h)} H{_num(x)} Z"
            attrs = {"class": "wp-group__outline", "d": d}
            if style == "group":
                attrs.update({"fill": "#ffffff", "fill-opacity": "0", "stroke": _GROUP_STROKE,
                              "stroke-width": "1", "stroke-dasharray": "4,3"})
            elif style == "pathway":
                attrs.update({"fill": "#00a000", "fill-opacity": "0.06", "stroke": _GROUP_STROKE,
                              "stroke-width": "1"})
            else:  # None: invisible, but still a target for the pointer
                attrs.update({"fill": "#ffffff", "fill-opacity": "0", "stroke": "none"})
            b.leaf("path", attrs)
        b.close("g")
        self._add(z, "Group", g.index, b.markup())

    # edges -------------------------------------------------------------------------------
    def _resolve_rect(self, did: str | None) -> geo.Rect | None:
        if not did:
            return None
        return self._rects.get(did)

    def _edges(self) -> None:
        pw = self.pw
        self._rects: dict[str, geo.Rect] = {}
        for n in pw.nodes:
            self._rects[self.did[("node", n.index)]] = n.rect
        for s in pw.states:
            self._rects[self.did[("state", s.index)]] = self._state_rect(s)
        self._rects.update(self.group_bbox)
        self.anchor_xy: dict[str, geo.Point] = {}
        self.edge_out: dict[int, dict] = {}

        pending = list(pw.edges)
        # Lines can end on another line's anchor, which only has a position once that line is
        # routed — so route in passes until nothing more resolves (cycles stay unresolved).
        for _ in range(4):
            if not pending:
                break
            later = []
            for e in pending:
                if not self._route_edge(e, final=False):
                    later.append(e)
            if len(later) == len(pending):
                break
            pending = later
        for e in pending:
            self._route_edge(e, final=True)

    def _endpoint(self, p, did: str | None) -> geo.Point | None:
        if p.x is not None and p.y is not None:
            return p.x, p.y
        if did and did.startswith("a-"):
            return self.anchor_xy.get(did)
        rect = self._resolve_rect(did)
        if rect is not None:
            return geo.attach_xy(rect, p.rel_x or 0.0, p.rel_y or 0.0)
        return None

    def _route_edge(self, e: EdgeEl, *, final: bool) -> bool:
        refs = [self.by_ref.get(p.ref) if p.ref else None for p in e.points]
        pts: list[geo.Point] = []
        for p, did in zip(e.points, refs, strict=False):
            xy = self._endpoint(p, did)
            if xy is None:
                if did and did.startswith("a-") and not final:
                    return False  # wait for the anchor's line
                if p is e.points[0] or p is e.points[-1]:
                    if final:
                        return True  # an end with no place at all: nothing to draw
                    return False
                continue  # an unplaceable waypoint is just skipped
            pts.append(xy)
        first, last = e.points[0], e.points[-1]
        route = geo.route_points(
            e.style.connector, pts,
            start_rel=(first.rel_x, first.rel_y) if first.ref else (None, None),
            end_rel=(last.rel_x, last.rel_y) if last.ref else (None, None),
        )
        if len(route) < 2:
            return True
        edid = self.did[("edge", e.index)]
        anchors = []
        for k, a in enumerate(e.anchors):
            adid = self.did[("anchor", e.index, k)]
            ax, ay = geo.anchor_xy(route, a.position)
            self.anchor_xy[adid] = (ax, ay)
            anchors.append((adid, a, ax, ay))
        self._draw_edge(e, edid, route, refs, anchors)
        return True

    def _draw_edge(self, e: EdgeEl, edid: str, route: list[geo.Point], refs: list,
                   anchors: list) -> None:
        st = e.style
        color = safe_color(st.border_color, "#000000") or "#000000"
        width = _width(st.line_width)
        start_arrow = canonical_arrow(e.points[0].arrow)
        end_arrow = canonical_arrow(e.points[-1].arrow)
        drawn = geo.shorten(
            route,
            start=_MARKERS[start_arrow].inset if start_arrow else 0.0,
            end=_MARKERS[end_arrow].inset if end_arrow else 0.0,
        )
        curved = (st.connector or "").lower() == "curved"
        d = geo.path_d(drawn, curved=curved, num=_num)
        for x, y in route:
            self.extent.append((x, y, 0.0, 0.0))

        source, target = refs[0], refs[-1]
        # Direction follows the arrowhead: a line drawn with its only arrow on the first point
        # points from the last point to the first.
        reverse = bool(start_arrow) and not end_arrow
        if reverse:
            source, target = target, source
        kind_arrow = (start_arrow if reverse else end_arrow)
        cls_type = kind_arrow or "line"

        b = Builder()
        b.open("g", {
            "class": f"wp-edge wp-edge--{cls_type}",
            "data-id": edid,
            "data-graph-id": e.graph_id if _SAFE_ID.fullmatch(e.graph_id or "") else None,
            "data-source": source,
            "data-target": target,
            "data-group": self._group_of(e.group_ref),
        })
        line = self._line_attrs(color, width * (3 if st.double else 1), st.line_style)
        b.leaf("path", {
            "class": "wp-edge__line", "d": d, "fill": "none", **line,
            "marker-start": (f"url(#{self.markers.use(start_arrow, color)})"
                             if start_arrow else None),
            "marker-end": f"url(#{self.markers.use(end_arrow, color)})" if end_arrow else None,
        })
        if st.double:
            b.leaf("path", {"d": d, "fill": "none", "stroke": "#ffffff",
                            "stroke-width": _num(width)})
        for adid, a, ax, ay in anchors:
            if (a.shape or "").lower() == "circle":
                b.leaf("circle", {"class": "wp-anchor", "data-id": adid, "cx": _num(ax),
                                  "cy": _num(ay), "r": "3", "fill": color})
        b.close("g")
        z = st.z if st.z is not None else _DEFAULT_Z[e.kind]
        self._add(z, e.kind, e.index, b.markup())

        def end_info(did):
            return did, (self.anchor_edge.get(did) if did and did.startswith("a-") else None)

        s_did, s_edge = end_info(source)
        t_did, t_edge = end_info(target)
        dangling = []
        if not s_did:
            dangling.append("source")
        if not t_did:
            dangling.append("target")
        self.edge_out[e.index] = {
            "id": edid,
            "graph_id": e.graph_id,
            "kind": e.kind,
            "type": kind_arrow or "line",
            "type_label": type_label(kind_arrow),
            "type_raw": (e.points[0].arrow if reverse else e.points[-1].arrow) or "",
            "source": s_did,
            "target": t_did,
            "source_edge": s_edge,
            "target_edge": t_edge,
            "points": [[round(x, 1), round(y, 1)] for x, y in route],
            "anchors": [{"id": adid, "position": round(a.position, 4), "x": round(ax, 1),
                         "y": round(ay, 1)} for adid, a, ax, ay in anchors],
            "dangling": dangling,
            "comment": "\n\n".join(clean_text(c) for c in e.comments),
            "database": clean_text(e.database),
            "identifier": clean_text(e.identifier),
            "group": self._group_of(e.group_ref),
        }

    # assembly ----------------------------------------------------------------------------
    def run(self) -> RenderResult:
        pw = self.pw
        self._ids()
        self._groups()
        for n in pw.nodes:
            self._draw_node(n)
        for s in pw.states:
            self._draw_state(s)
        self._edges()

        # viewBox: the declared board, grown to take in anything drawn beyond it.
        content = geo.union(self.extent)
        boxes: list[geo.Rect] = []
        if pw.board:
            boxes.append((0.0, 0.0, pw.board[0], pw.board[1]))
        if content is not None:
            cx, cy, cw, ch = content
            boxes.append((cx - _PAD, cy - _PAD, cw + 2 * _PAD, ch + 2 * _PAD))
        vb = geo.union(boxes) or (0.0, 0.0, 100.0, 100.0)
        vb_x, vb_y, vb_w, vb_h = vb
        if vb_w <= 0 or vb_h <= 0:
            vb_x, vb_y, vb_w, vb_h = 0.0, 0.0, 100.0, 100.0

        self.items.sort(key=lambda t: (t[0], t[1], t[2]))
        name = clean_text(pw.name) or "Untitled pathway"
        b = Builder()
        b.open("svg", {
            "xmlns": "http://www.w3.org/2000/svg",
            "class": "wp-pathway",
            "data-renderer": str(RENDERER_VERSION),
            "data-dialect": pw.dialect,
            "viewBox": f"{_num(vb_x)} {_num(vb_y)} {_num(vb_w)} {_num(vb_h)}",
            "width": f"{max(1.0, vb_w):.0f}",
            "height": f"{max(1.0, vb_h):.0f}",
            "role": "img",
            "aria-label": attr_text(f"Pathway: {name}"),
        })
        b.leaf("title", {}, name)
        b.raw(f"<defs>{self.markers.defs()}</defs>")
        b.leaf("rect", {"class": "wp-board", "x": _num(vb_x), "y": _num(vb_y),
                        "width": _num(vb_w), "height": _num(vb_h), "fill": "#ffffff"})
        for _z, _r, _i, markup in self.items:
            b.raw(markup)
        b.close("svg")
        svg = (b.markup() + "\n").encode("utf-8")

        hotspots, graph = self._graph(vb)
        return RenderResult(
            svg=svg,
            hotspots=hotspots,
            graph=graph,
            element_count=self.drawn,
            dialect=pw.dialect,
            declared=pw.declared,
        )

    def _graph(self, vb: geo.Rect) -> tuple[list[NodeHotspot], dict]:
        pw = self.pw
        vb_x, vb_y, vb_w, vb_h = vb
        states_by_parent: dict[int, list[dict]] = {}
        for s in pw.states:
            states_by_parent.setdefault(s.parent, []).append({
                "id": self.did[("state", s.index)],
                "label": clean_text(s.label),
                "database": clean_text(s.database),
                "identifier": clean_text(s.identifier),
            })

        def r1(v: float) -> float:
            return round(v, 1)

        nodes: list[dict] = []
        hotspots: list[NodeHotspot] = []
        labels: list[dict] = []
        shapes: list[dict] = []
        unannotated = 0
        for idx, n in enumerate(pw.nodes):
            did = self.did[("node", n.index)]
            x, y, w, h = n.rect
            bbox = [r1(x), r1(y), r1(w), r1(h)]
            if n.kind == "Label":
                labels.append({"id": did, "text": clean_text(n.label), "bbox": bbox,
                               "href": _safe_https(n.href)})
                continue
            if n.kind == "Shape":
                shapes.append({"id": did, "type": n.style.shape_type or "Rectangle",
                               "label": clean_text(n.label), "bbox": bbox})
                continue
            database, identifier = clean_text(n.database), clean_text(n.identifier)
            if not database or not identifier:
                unannotated += 1
            url = _safe_https(resolver_url(database, identifier))
            comment = "\n\n".join(clean_text(c) for c in n.comments)
            label = clean_text(n.label).strip()
            nodes.append({
                "id": did,
                "graph_id": n.graph_id,
                "kind": "DataNode",
                "type": n.type or "Unknown",
                "label": label,
                "database": database,
                "identifier": identifier,
                "url": url,
                "comment": comment,
                "bbox": bbox,
                "cx": r1(x + w / 2),
                "cy": r1(y + h / 2),
                "group": self._group_of(n.group_ref),
                "shape": n.style.shape_type or "Rectangle",
                "states": states_by_parent.get(idx, []),
            })
            if vb_w > 0 and vb_h > 0:
                left = (x - vb_x) / vb_w * 100.0
                top = (y - vb_y) / vb_h * 100.0
                hotspots.append(NodeHotspot(
                    left=round(max(0.0, left), 3),
                    top=round(max(0.0, top), 3),
                    width=round(min(w / vb_w * 100.0, 100.0 - max(0.0, left)), 3),
                    height=round(min(h / vb_h * 100.0, 100.0 - max(0.0, top)), 3),
                    label=label,
                    type=n.type or "Unknown",
                    database=database,
                    identifier=identifier,
                    url=url,
                    comment=comment,
                    graph_id=n.graph_id,
                    cx=x + w / 2,
                    cy=y + h / 2,
                    id=did,
                ))

        groups = []
        for g in pw.groups:
            gdid = self.did[("group", g.index)]
            box = self.group_bbox.get(gdid)
            groups.append({
                "id": gdid,
                "group_id": g.group_id,
                "graph_id": g.graph_id,
                "style": _group_style(g.style_name),
                "members": list(self.group_members.get(gdid, [])),
                "parent": self.group_parent.get(gdid),
                "bbox": [r1(v) for v in box] if box else None,
            })

        edges = [self.edge_out[e.index] for e in pw.edges if e.index in self.edge_out]
        graph = {
            "version": 2,
            "renderer": RENDERER_VERSION,
            "dialect": pw.dialect,
            "name": clean_text(pw.name),
            "viewBox": [r1(vb_x), r1(vb_y), r1(vb_w), r1(vb_h)],
            "nodes": nodes,
            "edges": edges,
            "groups": groups,
            "labels": labels,
            "shapes": shapes,
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "dangling_ends": sum(len(e["dangling"]) for e in edges
                                     if e["kind"] == "Interaction"),
                "unannotated": unannotated,
                "groups": len(groups),
            },
        }
        return hotspots, graph

"""Renderer 2: fidelity to the site's drawing, the graph sidecar, and the SVG safety contract.

Structural, not pixel, tests: the output is parsed and its elements counted, because the exact
bytes are expected to change whenever the drawing is tuned. Every render in here also goes
through ``assert_safe_svg``, the same check the viewer's client-side sanitiser mirrors.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.preview import geometry as geo
from app.preview.gpml import MAX_ELEMENTS, parse_pathway
from app.preview.render import (
    RENDERER_VERSION,
    RenderError,
    canonical_arrow,
    render_gpml_full,
    render_gpml_with_nodes,
)
from app.preview.svg import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_ELEMENTS,
    UnsafeSvg,
    _num,
    assert_safe_svg,
    attr_text,
    safe_color,
    safe_font,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gpml"
SVG = "{http://www.w3.org/2000/svg}"
ALL_FIXTURES = sorted(FIXTURES.glob("*.gpml"))


def _render(name: str):
    r = render_gpml_full((FIXTURES / name).read_bytes())
    assert_safe_svg(r.svg)
    return r, ET.fromstring(r.svg)


def _groups(root, cls: str) -> list[ET.Element]:
    return [g for g in root.iter(f"{SVG}g") if cls in (g.get("class") or "").split()]


def _by_id(root) -> dict[str, ET.Element]:
    return {g.get("data-id"): g for g in root.iter(f"{SVG}g") if g.get("data-id")}


# -- every fixture -----------------------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.name)
def test_every_fixture_renders_to_the_allowed_vocabulary(path):
    r = render_gpml_full(path.read_bytes())
    assert r.svg.startswith(b"<svg")  # no XML declaration: tests/test_api.py relies on it
    assert_safe_svg(r.svg)
    root = ET.fromstring(r.svg)
    assert root.get("data-renderer") == str(RENDERER_VERSION)
    assert root.get("role") == "img"
    # Every data-id is unique, and DOM ids exist only on markers (the client prefixes them).
    ids = [el.get("data-id") for el in root.iter() if el.get("data-id")]
    assert len(ids) == len(set(ids))
    assert all(el.tag == f"{SVG}marker" for el in root.iter() if el.get("id"))
    # nodes.json and graph.json list the same nodes in the same order (K2).
    assert [h.id for h in r.hotspots] == [n["id"] for n in r.graph["nodes"]]


def test_the_allowlist_is_what_the_contract_names():
    # K1: the client's sanitiser is built from these; widening them is a contract change.
    assert {"script", "style", "foreignObject", "use", "image", "a"}.isdisjoint(ALLOWED_ELEMENTS)
    assert not any("href" in a or a.startswith("on") for a in ALLOWED_ATTRIBUTES)
    assert {"svg", "marker", "path", "text", "tspan"} <= ALLOWED_ELEMENTS


# -- fidelity against the site render of the AhR revision (WP5429) -----------------------------

def test_complex_group_is_an_octagon_round_its_members_like_the_site():
    r, root = _render("2_revision_AhR_dioxin.gpml")
    (cpx,) = _groups(root, "wp-group--complex")
    assert cpx.get("data-id") == "g-grp1" and cpx.get("data-graph-id") == "g001"
    outline = cpx.find(f"{SVG}path")
    assert outline.get("class") == "wp-group__outline"
    assert outline.get("d").count("L") == 7  # eight corners
    # The site draws this group at translate(118,201), 144 x 126: members' box grown by 12.
    (g,) = r.graph["groups"]
    assert g["bbox"] == [118.0, 201.0, 144.0, 126.0]
    assert g["members"] == ["n-d002", "n-d003", "n-d004"]
    assert g["style"] == "complex"
    # Members carry their group; the group paints below them.
    order = [e.get("data-id") for e in root if e.get("data-id")]
    assert order.index("g-grp1") < order.index("n-d002")
    assert _by_id(root)["n-d002"].get("data-group") == "g-grp1"


def test_mim_arrowheads_get_one_marker_each():
    _, root = _render("2_revision_AhR_dioxin.gpml")
    markers = {m.get("id") for m in root.iter(f"{SVG}marker")}
    assert markers == {
        "wp-m-mim-binding", "wp-m-mim-transcription-translation", "wp-m-mim-inhibition",
    }
    edges = _by_id(root)
    line = edges["e-i003"].find(f"{SVG}path")
    assert line.get("marker-end") == "url(#wp-m-mim-transcription-translation)"
    assert "wp-edge--mim-binding" in edges["e-i001"].get("class")
    assert edges["e-i001"].get("data-source") == "n-d001"
    assert edges["e-i001"].get("data-target") == "n-d002"


def test_straight_connectors_ignore_intermediate_points():
    # i006 has a waypoint at (560,480); the site draws first -> last only (M380,480L560,247).
    r, root = _render("2_revision_AhR_dioxin.gpml")
    e = next(e for e in r.graph["edges"] if e["id"] == "e-i006")
    assert e["points"] == [[380.0, 480.0], [560.0, 247.0]]
    d = _by_id(root)["e-i006"].find(f"{SVG}path").get("d")
    assert d.count("L") == 1 and d.startswith("M380.0,480.0")


def test_paint_order_follows_zorder_and_text_is_the_datanode_colour():
    _, root = _render("2_revision_AhR_dioxin.gpml")
    order = [e.get("class").split()[0] for e in root if e.get("data-id")]
    # Lines (ZOrder 12288) under labels (28672) under nodes (32768).
    assert order.index("wp-edge") < order.index("wp-label") < order.index("wp-node")
    tcdd = _by_id(root)["n-d001"]
    assert tcdd.find(f"{SVG}rect").get("stroke") == "#0000ff"
    assert tcdd.find(f"{SVG}text").get("fill") == "#0000ff"
    # Metabolites are plain boxes on the site; the blue comes from Color, not from the type.
    assert tcdd.find(f"{SVG}rect").get("rx") is None


def test_the_viewbox_is_the_board():
    _, root = _render("2_revision_AhR_dioxin.gpml")
    assert root.get("viewBox") == "0.0 0.0 760.0 560.0"


def test_a_catalysis_onto_an_anchor_names_the_line_it_sits_on():
    r, _ = _render("3_update_WP5427_EGFR_RAS.gpml")
    edges = {e["id"]: e for e in r.graph["edges"]}
    cat = edges["e-e007"]
    assert cat["type"] == "mim-catalysis" and cat["type_label"] == "catalysis"
    assert cat["target"] == "a-a001" and cat["target_edge"] == "e-e006"
    assert edges["e-e006"]["anchors"] == [
        {"id": "a-a001", "position": 0.5, "x": 520.0, "y": 560.0}
    ]
    assert r.graph["stats"]["dangling_ends"] == 0


# -- the feature zoo, in both dialects ---------------------------------------------------------

def test_2021_renders_the_same_diagram_as_2013a():
    a, root_a = _render("zoo_2013a.gpml")
    b, root_b = _render("zoo_2021.gpml")
    assert (a.dialect, b.dialect) == ("2013a", "2021")
    assert root_b.get("data-dialect") == "2021"
    assert a.graph["viewBox"] == b.graph["viewBox"]
    for key in ("nodes", "labels", "shapes"):
        assert [n["id"] for n in a.graph[key]] == [n["id"] for n in b.graph[key]]
        assert [n["bbox"] for n in a.graph[key]] == [n["bbox"] for n in b.graph[key]]
    strip = ("graph_id", "type_raw")
    assert [{k: v for k, v in e.items() if k not in strip} for e in a.graph["edges"]] == [
        {k: v for k, v in e.items() if k not in strip} for e in b.graph["edges"]
    ]
    assert [(g["id"], g["style"], g["members"], g["bbox"]) for g in a.graph["groups"]] == [
        (g["id"], g["style"], g["members"], g["bbox"]) for g in b.graph["groups"]
    ]
    assert [n["states"] for n in a.graph["nodes"]] == [n["states"] for n in b.graph["nodes"]]
    assert a.graph["stats"] == b.graph["stats"]
    assert [e.get("data-id") for e in root_a if e.get("data-id")] == [
        e.get("data-id") for e in root_b if e.get("data-id")
    ]


def test_group_styles_and_nesting():
    r, root = _render("zoo_2013a.gpml")
    groups = {g["id"]: g for g in r.graph["groups"]}
    assert groups["g-cpx"]["style"] == "complex" and groups["g-cpx"]["parent"] == "g-outer"
    assert groups["g-outer"]["members"] == ["n-nc", "g-cpx"]
    # The outer group's box contains the inner one's.
    ox, oy, ow, oh = groups["g-outer"]["bbox"]
    ix, iy, iw, ih = groups["g-cpx"]["bbox"]
    assert ox < ix and oy < iy and ox + ow > ix + iw and oy + oh > iy + ih
    el = _by_id(root)
    assert el["g-outer"].find(f"{SVG}path").get("stroke-dasharray")  # Group: dashed
    inv = el["g-inv"].find(f"{SVG}path")  # None: invisible, still a pointer target
    assert inv.get("stroke") == "none" and inv.get("fill-opacity") == "0"
    assert "wp-group--pathway" in el["g-pwg"].get("class")
    order = [e.get("data-id") for e in root if e.get("data-id")]
    assert order.index("g-outer") < order.index("g-cpx") < order.index("n-na")


def test_states_sit_on_their_parent():
    r, root = _render("zoo_2013a.gpml")
    node = next(n for n in r.graph["nodes"] if n["id"] == "n-ne")
    assert node["states"] == [
        {"id": "st-st1", "label": "P", "database": "PSI-MOD", "identifier": "MOD:00046"},
        {"id": "st-st2", "label": "U", "database": "", "identifier": ""},
    ]
    st = _by_id(root)["st-st1"]
    assert st.get("data-parent") == "n-ne"
    shape = st.find(f"{SVG}ellipse")
    # RelX=1, RelY=-1 on a box centred (100,300) sized 80x25: the top-right corner.
    assert (shape.get("cx"), shape.get("cy")) == ("140.0", "287.5")
    assert shape.get("fill") == "#ffff00"


def test_line_styles_routes_and_ends():
    r, root = _render("zoo_2013a.gpml")
    el = _by_id(root)
    edges = {e["id"]: e for e in r.graph["edges"]}
    # Broken -> dashed; Elbow -> axis-aligned, leaving EEE's right side horizontally.
    elbow = el["e-ielbow"].find(f"{SVG}path")
    assert elbow.get("stroke-dasharray")
    pts = edges["e-ielbow"]["points"]
    assert pts == [[140.0, 300.0], [400.0, 300.0], [400.0, 387.5]]
    # Curved uses quadratic segments, in the line's own colour, with a marker of that colour.
    curve = el["e-icurve"].find(f"{SVG}path")
    assert " Q" in curve.get("d") and curve.get("stroke") == "#cc0000"
    assert curve.get("marker-end") == "url(#wp-m-mim-necessary-stimulation-cc0000)"
    # Segmented goes through every point.
    assert len(edges["e-iseg"]["points"]) == 3
    # Double: two strokes, the inner one white.
    paths = el["e-idouble"].findall(f"{SVG}path")
    assert len(paths) == 2 and paths[1].get("stroke") == "#ffffff"
    # An arrow only on the first point: direction is reversed, and the marker sits at the start.
    back = edges["e-ibackwards"]
    assert (back["source"], back["target"]) == ("n-np", "n-nd")
    assert el["e-ibackwards"].find(f"{SVG}path").get("marker-start")
    # A line into a group targets the group.
    assert edges["e-itogroup"]["target"] == "g-cpx"
    # Free ends are reported, not invented.
    assert edges["e-gl1"]["dangling"] == ["source", "target"]
    assert edges["e-gl1"]["kind"] == "GraphicalLine"
    assert edges["e-itt"]["dangling"] == ["target"]
    # A Circle anchor is drawn; the catalysis onto it resolves to the conversion line.
    assert el["e-iconv"].find(f"{SVG}circle").get("data-id") == "a-anc1"
    assert edges["e-icat"]["target_edge"] == "e-iconv"


def test_text_rotation_shapes_and_labels():
    r, root = _render("zoo_2013a.gpml")
    el = _by_id(root)
    # The long label wraps into several tspans inside its 120-wide box.
    tspans = el["n-ng"].findall(f"{SVG}text/{SVG}tspan")
    assert len(tspans) >= 3
    assert el["n-nc"].find(f"{SVG}text").get("font-weight") == "bold"
    assert el["n-nc"].find(f"{SVG}rect").get("rx")  # RoundedRectangle
    # Rotation is radians in GPML, degrees in SVG.
    assert el["n-nr"].get("transform") == "rotate(45.0 700.0 500.0)"
    lab = el["l-lab1"].find(f"{SVG}text")
    assert lab.get("text-anchor") == "start" and lab.get("font-style") == "italic"
    assert [t.text for t in lab.findall(f"{SVG}tspan")] == ["Left aligned", "second line"]
    # Shapes are see-through unless filled; a filled one keeps its colour.
    assert el["s-sh3"].find(f"{SVG}path").get("fill") == "none"
    assert el["s-sh2"].find(f"{SVG}ellipse").get("fill") == "#ffcc00"
    assert len(el["s-sh4"].findall(f"{SVG}rect")) == 2  # organelle: double outline
    labels = {lb["id"]: lb for lb in r.graph["labels"]}
    assert labels["l-lab1"]["href"] == "https://www.wikipathways.org/"
    assert labels["l-lab2"]["href"] is None  # javascript: is not a link
    # The off-board node is drawn: the viewBox grows past the 900 x 700 board to reach it.
    x, y, w, h = r.graph["viewBox"]
    assert x + w >= 1020 + 15 and y + h >= 772.5 + 15


def test_unannotated_and_pathway_nodes_in_the_graph():
    r, _ = _render("zoo_2013a.gpml")
    nodes = {n["id"]: n for n in r.graph["nodes"]}
    assert nodes["n-nc"]["database"] == "" and r.graph["stats"]["unannotated"] == 1
    assert nodes["n-np"]["url"] == "https://identifiers.org/wikipathways:WP554"
    assert nodes["n-na"]["group"] == "g-cpx"


# -- hostile input -----------------------------------------------------------------------------

def test_hostile_values_never_reach_a_live_position():
    r, root = _render("hostile_2013a.gpml")
    text = r.svg.decode()
    for payload in ("<script", "onload=", "onclick=", "onmouseover=", "expression("):
        assert payload not in text
    for el in root.iter():
        for k, v in el.attrib.items():
            assert "javascript:" not in v.lower(), (k, v)
            if k in ("fill", "stroke"):
                assert v == "none" or v.startswith("#") or v.isalpha(), (k, v)
            if k == "font-family":
                assert v in ("Arial, sans-serif", "Helvetica, sans-serif", "Times, sans-serif",
                             "Courier, sans-serif", "sans-serif")
    # Unsafe GraphIds and duplicates fall back to auto ids; references still resolve.
    nodes = r.graph["nodes"]
    assert [n["id"] for n in nodes] == ["n-auto1", "n-dup", "n-auto2"]
    # NaN, infinite and absurd coordinates are skipped, not drawn at the edge of the universe.
    assert [n["label"] for n in nodes] == ["<script>alert(3)</script>",
                                           "controlcharshere\ttab", "duplicate id"]
    x, y, w, h = r.graph["viewBox"]
    assert max(abs(x), abs(y), w, h) < 1000
    # A GroupRef cycle neither recurses nor loses the groups.
    assert {g["id"] for g in r.graph["groups"]} == {"g-loopA", "g-loopB", "g-self"}
    # Hrefs that are not https never reach the graph.
    assert all(lb["href"] is None for lb in r.graph["labels"])


def test_control_characters_are_stripped_from_text():
    _, root = _render("hostile_2013a.gpml")
    labels = [t.text for t in root.iter(f"{SVG}tspan")]
    assert "controlcharshere tab" in labels or "controlcharshere\ttab" in labels
    assert not any(ch in "".join(labels) for ch in "\x85\x9f")


@pytest.mark.parametrize("doc", [
    '<?xml version="1.0"?><!DOCTYPE Pathway [<!ENTITY x "boom">]>'
    '<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="&x;"/>',
    '<!DOCTYPE Pathway SYSTEM "file:///etc/passwd"><Pathway Name="p"/>',
    '<Pathway Name="p"><!ENTITY x "y"></Pathway>',
])
def test_doctype_and_entities_are_refused(doc):
    with pytest.raises(RenderError):
        render_gpml_full(doc)


def test_more_than_max_elements_is_refused():
    body = "<Label/>" * (MAX_ELEMENTS + 1)
    with pytest.raises(RenderError):
        render_gpml_full(f'<Pathway xmlns="http://pathvisio.org/GPML/2013a">{body}</Pathway>')


def test_a_big_but_legal_pathway_renders():
    nodes = "".join(
        f'<DataNode TextLabel="N{i}" GraphId="n{i}" Type="GeneProduct">'
        f'<Graphics CenterX="{(i % 50) * 100}" CenterY="{(i // 50) * 40}" Width="80" '
        f'Height="25"/></DataNode>'
        for i in range(2000)
    )
    r = render_gpml_full(f'<Pathway xmlns="http://pathvisio.org/GPML/2013a">{nodes}</Pathway>')
    assert r.element_count == 2000 and len(r.hotspots) == 2000
    assert_safe_svg(r.svg)


# -- units -------------------------------------------------------------------------------------

def test_num_is_finite_and_clamped():
    assert _num(1.25) == "1.2" or _num(1.25) == "1.3"
    assert _num(float("nan")) == "0.0" and _num(float("inf")) == "0.0"
    assert _num(1e300) == "1000000.0" and _num(-1e300) == "-1000000.0"
    assert _num(-0.01) == "0.0"


@pytest.mark.parametrize(("raw", "out"), [
    ("ff0000", "#ff0000"), ("#00FF00", "#00ff00"), ("0000ff80", "#0000ff"),
    ("00000000", None), ("Transparent", None), ("red", "red"),
    ('red" onload="x', "#123456"), ("url(#x)", "#123456"), ("", "#123456"), (None, "#123456"),
])
def test_safe_color(raw, out):
    assert safe_color(raw, "#123456") == out


@pytest.mark.parametrize(("raw", "out"), [
    ("Arial", "Arial"), ("Liberation Sans", "sans-serif"), ("Times New Roman", "Times"),
    ("Courier New", "Courier"), ("Helvetica", "Helvetica"), ("</style><script>", "sans-serif"),
    (None, "sans-serif"),
])
def test_safe_font(raw, out):
    assert safe_font(raw) == out


@pytest.mark.parametrize(("raw", "canon"), [
    ("Arrow", "arrow"), ("mim-conversion", "mim-conversion"), ("Conversion", "mim-conversion"),
    ("TBar", "tbar"), ("mim-inhibition", "mim-inhibition"), ("Inhibition", "mim-inhibition"),
    ("TranscriptionTranslation", "mim-transcription-translation"),
    ("mim-necessary-stimulation", "mim-necessary-stimulation"), ("ReceptorRound", "receptor-round"),
    ("LigandRound", "ligand-round"), ("Line", None), ("", None), ("Undirected", None),
    ("something-new", "arrow"),
])
def test_arrowheads_of_both_dialects_map_to_one_set(raw, canon):
    assert canonical_arrow(raw) == canon


def test_assert_safe_svg_catches_what_it_should():
    ok = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0" marker-end="url(#m)"/></svg>'
    assert_safe_svg(ok)
    for bad in (
        '<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(http://evil)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
        '<rect x:href="#a"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect data-x="javascript:alert(1)"/></svg>',
        '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    ):
        with pytest.raises(UnsafeSvg):
            assert_safe_svg(bad)


def test_elbow_routes():
    # Both ends horizontal: H-V-H through the midpoint.
    assert geo.elbow_route([(0, 0), (100, 50)], start_rel=(1, 0), end_rel=(-1, 0)) == [
        (0, 0), (50, 0), (50, 50), (100, 50)]
    # Horizontal out, vertical in: an L.
    assert geo.elbow_route([(0, 0), (100, 50)], start_rel=(1, 0), end_rel=(0, -1)) == [
        (0, 0), (100, 0), (100, 50)]
    # A waypoint fixes the middle segment (PathVisio's elbow waypoints).
    assert geo.elbow_route([(216, 208), (100, 364), (453, 520)],
                           start_rel=(-1, 0), end_rel=(-1, 0)) == [
        (216, 208), (100, 208), (100, 520), (453, 520)]


def test_anchor_position_is_by_length():
    route = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    assert geo.anchor_xy(route, 0.5) == (100.0, 0.0)
    assert geo.anchor_xy(route, 0.75) == (100.0, 50.0)
    assert geo.anchor_xy(route, 2) == (100.0, 100.0)


def test_group_bboxes_survive_cycles():
    boxes = geo.group_bboxes({"a": ["n1", "b"], "b": ["n2", "a"]},
                             {"n1": (0, 0, 10, 10), "n2": (20, 0, 10, 10)}, margin=0)
    assert boxes["a"] == (0, 0, 30, 10)


def test_fold_groups_walks_any_depth_without_recursion():
    depth = 5000  # far beyond the interpreter's recursion limit
    members = {f"g{i}": [f"g{i + 1}"] for i in range(depth)}
    members[f"g{depth}"] = ["n"]
    boxes = geo.group_bboxes(members, {"n": (0, 0, 10, 10)}, margin=1)
    assert boxes["g0"] == (-(depth + 1), -(depth + 1), 10 + 2 * (depth + 1), 10 + 2 * (depth + 1))
    z = geo.fold_groups(members, {"n": 5.0}.get, lambda v: (min(v) - 1) if v else None)
    assert z["g0"] == 5.0 - (depth + 1)


def _deeply_nested_groups(n: int) -> bytes:
    parts = ['<Pathway xmlns="http://pathvisio.org/GPML/2013a" Name="deep" Organism="x">',
             '<DataNode TextLabel="A" GraphId="n0" Type="GeneProduct" GroupRef="g0">'
             '<Graphics CenterX="50" CenterY="50" Width="40" Height="20"/></DataNode>']
    for i in range(n):
        ref = f' GroupRef="g{i + 1}"' if i < n - 1 else ""
        parts.append(f'<Group GroupId="g{i}" GraphId="gg{i}" Style="Group"{ref}/>')
    parts.append("</Pathway>")
    return "".join(parts).encode()


def test_groups_nested_thousands_deep_render():
    # ~100 KB, passes validation, and used to raise RecursionError out of the renderer, which
    # left the preview "pending" for good and the backfill retrying it every tick.
    r = render_gpml_full(_deeply_nested_groups(1500))
    assert_safe_svg(r.svg)
    assert len(r.graph["groups"]) == 1500


def test_an_unexpected_renderer_error_is_a_render_error(monkeypatch):
    from app.preview import render

    def boom(self):
        raise ZeroDivisionError("a renderer bug")

    monkeypatch.setattr(render._Renderer, "run", boom)
    with pytest.raises(RenderError):
        render_gpml_full((FIXTURES / "1_new_AhR_dioxin.gpml").read_bytes())


@pytest.mark.parametrize("raw", [
    "javajavascript:script: pathway", "uurl(rl(x", "JAVAjavascript:SCRIPT:x",
    "jajavascript:vascript:",
])
def test_attr_text_leaves_no_payload_behind(raw):
    out = attr_text(raw).lower()
    assert "javascript:" not in out and "url(" not in out


def test_a_pathway_with_a_nested_payload_in_its_name_still_renders():
    gpml = (FIXTURES / "1_new_AhR_dioxin.gpml").read_bytes()
    gpml = gpml.replace(b'Name="', b'Name="javajavascript:script: uurl(rl(x ', 1)
    r = render_gpml_full(gpml)
    assert_safe_svg(r.svg)


def test_wrap_text():
    assert geo.wrap_text("short", 100, 20, 10) == ["short"]
    assert geo.wrap_text("one\ntwo", 100, 40, 10) == ["one", "two"]
    lines = geo.wrap_text("a b c d e f g h i j k l m n o p", 30, 100, 10)
    assert len(lines) > 1 and all(len(ln) <= 6 for ln in lines)
    cut = geo.wrap_text("a\nb\nc\nd", 100, 26, 10)  # room for two lines
    assert cut == ["a", "b…"]
    assert geo.wrap_text("a\nb\nc\nd", 100, 26, 10, clip=False) == ["a", "b", "c", "d"]


def test_rotation_transform():
    assert geo.rotation_transform(math.pi / 2, 10, 20, _num) == "rotate(90.0 10.0 20.0)"
    assert geo.rotation_transform(0.0, 10, 20, _num) is None
    assert geo.rotation_transform(None, 10, 20, _num) is None


def test_parse_reads_both_spellings():
    a = parse_pathway((FIXTURES / "zoo_2013a.gpml").read_bytes())
    b = parse_pathway((FIXTURES / "zoo_2021.gpml").read_bytes())
    assert (a.name, a.organism, a.board) == (b.name, b.organism, b.board)
    # Sorted: 2021 lists Labels before Shapes, the 2013a fixture the other way round.
    assert sorted((n.kind, n.label, n.database, n.identifier, n.rect) for n in a.nodes) == sorted(
        (n.kind, n.label, n.database, n.identifier, n.rect) for n in b.nodes)


def test_hotspots_keep_their_shape_and_gain_an_id():
    svg, hotspots = render_gpml_with_nodes((FIXTURES / "1_new_AhR_dioxin.gpml").read_bytes())
    assert hotspots[0].id == "n-d001" and hotspots[0].graph_id == "d001"
    assert set(hotspots[0].as_dict()) >= {"left", "top", "width", "height", "label", "id"}

"""Pure geometry for the renderer: line routes, anchors, group outlines, shape outlines, text wrap.

No SVG and no GPML here, only numbers, so each rule can be tested on its own and the drawing code
in ``render.py`` stays a translation of these results into markup.

The routing rules follow what PathVisio draws, as far as a reviewer can tell the difference:

- **Straight** joins the first and last point only. GPML can carry intermediate points on a
  straight line (left behind when the connector type was changed), and PathVisio ignores them;
  drawing through them made the portal's picture disagree with the published one.
- **Segmented** goes through every point.
- **Elbow** is axis-aligned. Each end leaves along the axis of the side it is attached to
  (``RelX = ±1`` is a left/right side, so horizontal), and each intermediate point fixes the
  position of one segment, the way PathVisio stores elbow waypoints.
- **Curved** is the elbow route with every corner rounded by a quadratic segment.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # x, y, w, h

H, V = "h", "v"

#: Space between a group's members and its outline. Matches the WikiPathways site render (a
#: Complex of 120 x 34 nodes gets a 144-wide outline).
GROUP_MARGIN = 12.0

LINE_HEIGHT = 1.2


# -- endpoints and routes ----------------------------------------------------------------------

def attach_xy(rect: Rect, rel_x: float, rel_y: float) -> Point:
    """Where a line attached at ``(relX, relY)`` meets ``rect`` (relative to its centre)."""
    x, y, w, h = rect
    return x + w / 2 + rel_x * w / 2, y + h / 2 + rel_y * h / 2


def _axis_from_rel(rel_x: float | None, rel_y: float | None) -> str | None:
    if rel_x is None or rel_y is None:
        return None
    if abs(abs(rel_x) - 1.0) < 1e-6 and abs(rel_x) >= abs(rel_y):
        return H
    if abs(abs(rel_y) - 1.0) < 1e-6:
        return V
    return None


def _axis_between(a: Point, b: Point) -> str:
    return H if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else V


def elbow_route(
    pts: list[Point],
    *,
    start_rel: tuple[float | None, float | None] = (None, None),
    end_rel: tuple[float | None, float | None] = (None, None),
) -> list[Point]:
    """Axis-aligned route through ``pts`` (first and last are the ends, the rest waypoints).

    Each segment is pinned by one coordinate: the first by the start point, one per waypoint
    (the waypoint lies on it), and the last by the end point. Corners are where consecutive
    segments meet. With no waypoints that gives an L when the two ends leave on different axes
    and an H-V-H (or V-H-V) through the midpoint when they share one.
    """
    if len(pts) < 2:
        return list(pts)
    start, end = pts[0], pts[-1]
    waypoints = pts[1:-1]
    s_axis = _axis_from_rel(*start_rel) or _axis_between(start, waypoints[0] if waypoints else end)
    e_axis = _axis_from_rel(*end_rel) or _axis_between(waypoints[-1] if waypoints else start, end)

    def fixed_of(axis: str, p: Point) -> float:
        return p[1] if axis == H else p[0]

    segs: list[tuple[str, float]] = [(s_axis, fixed_of(s_axis, start))]
    axis = s_axis
    for w in waypoints:
        axis = V if axis == H else H
        segs.append((axis, fixed_of(axis, w)))
    if axis == e_axis:
        # The last segment so far runs along the axis the end needs, but on the wrong line:
        # step across at the midpoint, then come in along the end's axis.
        across = V if axis == H else H
        prev = waypoints[-1] if waypoints else start
        mid = (fixed_of(across, prev) + fixed_of(across, end)) / 2
        segs.append((across, mid))
    segs.append((e_axis, fixed_of(e_axis, end)))

    route: list[Point] = [start]
    for (a1, v1), (_a2, v2) in zip(segs, segs[1:], strict=False):
        route.append((v2, v1) if a1 == H else (v1, v2))
    route.append(end)
    return _dedupe(route)


def _dedupe(route: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in route:
        if not out or abs(out[-1][0] - p[0]) > 1e-6 or abs(out[-1][1] - p[1]) > 1e-6:
            out.append(p)
    if len(out) == 1 and len(route) > 1:
        out.append(route[-1])
    return out


def route_points(
    connector: str | None,
    pts: list[Point],
    *,
    start_rel: tuple[float | None, float | None] = (None, None),
    end_rel: tuple[float | None, float | None] = (None, None),
) -> list[Point]:
    """The polyline a connector of this type follows (Curved: the polyline it smooths)."""
    kind = (connector or "Straight").strip().lower()
    if len(pts) < 2:
        return list(pts)
    if kind == "segmented":
        return list(pts)
    if kind in ("elbow", "curved"):
        return elbow_route(pts, start_rel=start_rel, end_rel=end_rel)
    return [pts[0], pts[-1]]


def path_d(route: list[Point], *, curved: bool, num: Callable[[float], str]) -> str:
    """SVG path data for a route; ``num`` formats each coordinate."""
    if not route:
        return ""

    def p(pt: Point) -> str:
        return f"{num(pt[0])},{num(pt[1])}"

    if not curved or len(route) < 3:
        return "M" + " L".join(p(pt) for pt in route)
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(route, route[1:], strict=False)]
    d = f"M{p(route[0])} L{p(mids[0])}"
    for corner, nxt in zip(route[1:-1], mids[1:], strict=False):
        d += f" Q{p(corner)} {p(nxt)}"
    d += f" L{p(route[-1])}"
    return d


def shorten(route: list[Point], *, start: float = 0.0, end: float = 0.0) -> list[Point]:
    """Pull the route's ends back so a line stops where its arrowhead's outline begins.

    Open arrowheads (stimulation, catalysis) would otherwise show the line running through them.
    A segment too short to give up that much is left alone: a stub with no line is worse.
    """
    out = list(route)
    if len(out) < 2:
        return out

    def pull(tip: Point, toward: Point, by: float) -> Point:
        dx, dy = toward[0] - tip[0], toward[1] - tip[1]
        length = math.hypot(dx, dy)
        if by <= 0 or length <= by * 1.2:
            return tip
        return tip[0] + dx / length * by, tip[1] + dy / length * by

    if end:
        out[-1] = pull(out[-1], out[-2], end)
    if start:
        out[0] = pull(out[0], out[1], start)
    return out


def anchor_xy(route: list[Point], position: float) -> Point:
    """The point ``position`` (0..1) of the way along ``route``, by length."""
    if not route:
        return 0.0, 0.0
    if len(route) == 1:
        return route[0]
    position = min(1.0, max(0.0, position))
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:], strict=False)]
    total = sum(lengths)
    if total <= 0:
        return route[0]
    target = total * position
    for (a, b), seg in zip(zip(route, route[1:], strict=False), lengths, strict=False):
        if target <= seg and seg > 0:
            t = target / seg
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        target -= seg
    return route[-1]


# -- groups ------------------------------------------------------------------------------------

def union(rects: Iterable[Rect]) -> Rect | None:
    rs = list(rects)
    if not rs:
        return None
    x0 = min(r[0] for r in rs)
    y0 = min(r[1] for r in rs)
    x1 = max(r[0] + r[2] for r in rs)
    y1 = max(r[1] + r[3] for r in rs)
    return x0, y0, x1 - x0, y1 - y0


def group_bboxes(
    members: dict[str, list[str]],
    rects: dict[str, Rect],
    *,
    margin: float = GROUP_MARGIN,
) -> dict[str, Rect]:
    """Outline of every group: its members' union, grown by ``margin``, nested groups included.

    ``members`` maps a group id to member ids (element or group ids); ``rects`` holds the
    elements' own boxes. A GroupRef cycle (a group that contains itself, directly or not) is
    cut where it closes, so a hostile file cannot recurse the renderer to death.
    """
    out: dict[str, Rect] = {}

    def box(gid: str, visiting: set[str]) -> Rect | None:
        if gid in out:
            return out[gid]
        if gid in visiting:
            return None
        visiting.add(gid)
        parts: list[Rect] = []
        for m in members.get(gid, []):
            if m in members:
                r = box(m, visiting)
            else:
                r = rects.get(m)
            if r is not None:
                parts.append(r)
        visiting.discard(gid)
        u = union(parts)
        if u is None:
            return None
        x, y, w, h = u
        out[gid] = (x - margin, y - margin, w + 2 * margin, h + 2 * margin)
        return out[gid]

    for gid in members:
        box(gid, set())
    return out


# -- shapes ------------------------------------------------------------------------------------

#: Shape types drawn as an organelle (a double-stroked rounded box). PathVisio draws each of
#: these with its own artwork; a reviewer needs to see "compartment", not the cristae.
ORGANELLES = frozenset({
    "mitochondria", "nucleus", "cell", "organelle", "endoplasmicreticulum",
    "sarcoplasmicreticulum", "golgiapparatus", "extracellularregion", "vesicle",
    "cellularcomponent",
})


def shape_key(shape_type: str | None) -> str:
    return "".join(ch for ch in (shape_type or "").lower() if ch.isalnum())


def _polygon(points: list[Point], num: Callable[[float], str]) -> str:
    return "M" + " L".join(f"{num(x)},{num(y)}" for x, y in points) + " Z"


def shape_elements(
    shape_type: str | None, rect: Rect, *, num: Callable[[float], str]
) -> list[tuple[str, dict]]:
    """``[(tag, attrs)]`` outlining ``shape_type`` inside ``rect`` (style is the caller's).

    Unknown types fall back to a rectangle, which is what PathVisio does too.
    """
    x, y, w, h = rect
    key = shape_key(shape_type)
    cx, cy = x + w / 2, y + h / 2
    if key in ("none", ""):
        return [] if key == "none" else [("rect", _rect(rect, num))]
    if key == "rectangle":
        return [("rect", _rect(rect, num))]
    if key == "roundedrectangle":
        r = min(10.0, w / 4, h / 4)
        return [("rect", {**_rect(rect, num), "rx": num(r), "ry": num(r)})]
    if key in ("oval", "ellipse", "circle"):
        return [("ellipse", {"cx": num(cx), "cy": num(cy), "rx": num(w / 2), "ry": num(h / 2)})]
    if key == "triangle":
        return [("path", {"d": _polygon([(x, y), (x + w, cy), (x, y + h)], num)})]
    if key == "pentagon":
        pts = [(x + w * 0.0, y + h * 0.2), (x + w * 0.6, y), (x + w, cy),
               (x + w * 0.6, y + h), (x + w * 0.0, y + h * 0.8)]
        return [("path", {"d": _polygon(pts, num)})]
    if key == "hexagon":
        pts = [(x + w * 0.25, y), (x + w * 0.75, y), (x + w, cy), (x + w * 0.75, y + h),
               (x + w * 0.25, y + h), (x, cy)]
        return [("path", {"d": _polygon(pts, num)})]
    if key == "octagon":
        return [("path", {"d": octagon_d(rect, num)})]
    if key == "arc":
        d = (f"M{num(x)},{num(cy)} A{num(w / 2)},{num(h / 2)} 0 0 1 "
             f"{num(x + w)},{num(cy)}")
        return [("path", {"d": d})]
    if key == "brace":
        q = w / 4
        d = (f"M{num(x)},{num(y + h)} Q{num(x)},{num(cy)} {num(x + q)},{num(cy)} "
             f"L{num(cx - q / 2)},{num(cy)} Q{num(cx)},{num(cy)} {num(cx)},{num(y)} "
             f"Q{num(cx)},{num(cy)} {num(cx + q / 2)},{num(cy)} L{num(x + w - q)},{num(cy)} "
             f"Q{num(x + w)},{num(cy)} {num(x + w)},{num(y + h)}")
        return [("path", {"d": d})]
    if key == "mimdegradation":
        r = min(w, h) / 2
        return [
            ("ellipse", {"cx": num(cx), "cy": num(cy), "rx": num(r), "ry": num(r)}),
            ("line", {"x1": num(cx - r * 0.7), "y1": num(cy + r * 0.7),
                      "x2": num(cx + r * 0.7), "y2": num(cy - r * 0.7)}),
        ]
    if key in ORGANELLES:
        r = min(15.0, w / 4, h / 4)
        inset = min(4.0, w / 6, h / 6)
        inner = (x + inset, y + inset, max(0.0, w - 2 * inset), max(0.0, h - 2 * inset))
        return [
            ("rect", {**_rect(rect, num), "rx": num(r), "ry": num(r)}),
            ("rect", {**_rect(inner, num), "rx": num(max(0.0, r - inset)),
                      "ry": num(max(0.0, r - inset)), "fill": "none"}),
        ]
    return [("rect", _rect(rect, num))]


def octagon_d(rect: Rect, num: Callable[[float], str], cut: float = 0.25) -> str:
    """An octagon with its corners cut at ``cut`` of each side, like the site's Complex outline."""
    x, y, w, h = rect
    cx, cy = w * cut, h * cut
    pts = [(x, y + cy), (x + cx, y), (x + w - cx, y), (x + w, y + cy), (x + w, y + h - cy),
           (x + w - cx, y + h), (x + cx, y + h), (x, y + h - cy)]
    return _polygon(pts, num)


def _rect(rect: Rect, num: Callable[[float], str]) -> dict:
    x, y, w, h = rect
    return {"x": num(x), "y": num(y), "width": num(w), "height": num(h)}


def rotation_transform(radians: float | None, cx: float, cy: float,
                       num: Callable[[float], str]) -> str | None:
    """``rotate(deg cx cy)`` for a GPML rotation (radians), or None when there is none."""
    if not radians or not math.isfinite(radians):
        return None
    deg = math.degrees(radians) % 360.0
    if abs(deg) < 0.05 or abs(deg - 360.0) < 0.05:
        return None
    return f"rotate({num(deg)} {num(cx)} {num(cy)})"


# -- text --------------------------------------------------------------------------------------

def text_width(s: str, font_size: float, *, bold: bool = False) -> float:
    """A rough rendered width: no font metrics server-side, so an average glyph advance."""
    return len(s) * font_size * (0.6 if bold else 0.55)


def wrap_text(
    text: str, width: float, height: float, font_size: float, *, bold: bool = False,
    clip: bool = True,
) -> list[str]:
    """Lines of ``text`` to draw in a ``width`` x ``height`` box.

    Explicit newlines are kept. A line is only broken further when it would clearly overflow,
    because a guessed width that wraps a label that would have fitted looks worse than a label
    that runs a little past its box. With ``clip``, lines that do not fit vertically are dropped,
    with an ellipsis on the last one kept; without it (free labels, which PathVisio lets
    overflow) every line is kept, since a label cut short loses words nothing else carries.
    """
    fs = max(1.0, font_size)
    avail = max(1.0, width - 4)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = [" ".join(p.split()) for p in text.split("\n")]
    while len(paras) > 1 and not paras[-1]:
        paras.pop()
    lines: list[str] = []
    for para in paras:
        if text_width(para, fs, bold=bold) <= avail or " " not in para:
            lines.append(para)
            continue
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}" if cur else word
            if cur and text_width(trial, fs, bold=bold) > avail:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
    if not clip:
        return lines
    max_lines = max(1, math.floor((height - 2) / (LINE_HEIGHT * fs)))
    if len(lines) > max_lines >= len(paras):
        # Wrapping is a guess from an average glyph width; when the guess would cost words to
        # the ellipsis, a line that runs past its box (which PathVisio also draws) loses less.
        lines = paras
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines

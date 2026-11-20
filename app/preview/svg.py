"""The SVG vocabulary the renderer may emit, and the escaping every value goes through.

The drawing used to reach the browser only through an ``<img>``, where its markup is inert, so a
colour string passed through verbatim was harmless. The viewer now inlines it, which makes every
byte of it page markup: a ``Color="red&quot; onload=..."`` in a submitted GPML would be script.
So nothing here interpolates a GPML value directly. Numbers go through ``_num`` (finite, clamped),
colours through ``safe_color`` (hex or a fixed set of names), fonts through ``safe_font`` (a
fixed set), text through ``_text`` and attributes through ``_attr``. ``Builder`` refuses any
element or attribute outside ``ALLOWED_ELEMENTS`` / ``ALLOWED_ATTRIBUTES``, so the allowlist the
client-side sanitiser enforces (``static/viewer.js``) is also what the server can produce.

``assert_safe_svg`` re-checks a finished document against the same lists. The renderer runs it on
its own output, and the tests run it on every fixture, hostile ones included.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SVG_NS = "http://www.w3.org/2000/svg"

ALLOWED_ELEMENTS = frozenset({
    "svg", "title", "defs", "marker", "g", "path", "rect", "ellipse", "circle", "polygon",
    "polyline", "line", "text", "tspan",
})

ALLOWED_ATTRIBUTES = frozenset({
    "xmlns", "viewBox", "width", "height", "id", "class", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "d", "points", "transform", "fill", "fill-opacity", "stroke",
    "stroke-width", "stroke-dasharray", "stroke-opacity", "stroke-linejoin", "stroke-linecap",
    "marker-start", "marker-end", "markerWidth", "markerHeight", "refX", "refY", "orient",
    "markerUnits", "text-anchor", "dominant-baseline", "font-family", "font-size", "font-weight",
    "font-style", "opacity", "role", "aria-label",
})

#: The largest coordinate the renderer writes. GPML in the wild stays within a few thousand
#: units; anything past this is either an error or an attempt to make the client do silly
#: arithmetic, and clamping keeps the output a sane size either way.
COORD_LIMIT = 1e6

_DATA_ATTR = re.compile(r"data-[a-z0-9-]{1,40}")
_ATTR_HOSTILE = re.compile(r"(?i)javascript:|url\(")
_HEX = re.compile(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")
# Control characters XML 1.0 cannot carry at all, plus the C1 range, which it can but which never
# means anything in a label. Tab and newline are folded to spaces by the caller.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￾￿]")

#: CSS colour names GPML files actually use. Anything else that is not hex falls back to the
#: default rather than being passed through: the set is small enough to list, and listing it is
#: what makes the value impossible to break out of.
NAMED_COLORS = frozenset({
    "black", "white", "red", "green", "blue", "yellow", "orange", "purple", "gray", "grey",
    "brown", "pink", "cyan", "magenta", "lime", "navy", "teal", "olive", "maroon", "silver",
    "aqua", "fuchsia", "darkgreen", "darkblue", "darkred", "lightgray", "lightgrey",
})

_FONTS = (
    ("courier", "Courier"),
    ("mono", "Courier"),
    ("times", "Times"),
    ("serif", "Times"),  # after "sans" is ruled out below
    ("helvetica", "Helvetica"),
    ("arial", "Arial"),
)


def _num(v: float | int | None, default: float = 0.0) -> str:
    """A coordinate as text: finite only, clamped, one decimal."""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        f = default
    if not math.isfinite(f):
        f = default
    f = max(-COORD_LIMIT, min(COORD_LIMIT, f))
    out = f"{f:.1f}"
    return "0.0" if out == "-0.0" else out


def parse_color(raw: str | None) -> tuple[str | None, bool]:
    """``(css colour, explicit)`` for a GPML colour value.

    ``explicit`` is False when the value is missing or unusable, so the caller can apply its own
    default; a recognised "Transparent" is explicit and yields None (no paint). An eight-digit
    hex carries alpha: fully transparent reads as None, anything else keeps the colour.
    """
    if raw is None:
        return None, False
    s = raw.strip()
    if not s:
        return None, False
    low = s.lower()
    if low in ("transparent", "none"):
        return None, True
    m = _HEX.fullmatch(s)
    if m:
        if m.group(2) is not None and m.group(2) == "00":
            return None, True
        return "#" + m.group(1).lower(), True
    if low in NAMED_COLORS:
        return low, True
    return None, False


def safe_color(raw: str | None, default: str | None = "#000000") -> str | None:
    """A colour safe to put in an attribute, or ``default``. None means "no paint"."""
    color, explicit = parse_color(raw)
    return color if explicit else default


def safe_font(raw: str | None) -> str:
    """One of Arial, Helvetica, Times, Courier or sans-serif, whatever the GPML asked for."""
    low = (raw or "").lower()
    if "sans" in low:
        return "Arial" if "arial" in low else "sans-serif"
    for needle, family in _FONTS:
        if needle in low:
            return family
    return "sans-serif"


def font_stack(raw: str | None) -> str:
    family = safe_font(raw)
    return family if family == "sans-serif" else f"{family}, sans-serif"


def _text(s: str | None) -> str:
    """Element text: control characters removed, then XML-escaped."""
    return escape(_CONTROL.sub("", s or ""))


def clean_text(s: str | None) -> str:
    """The same cleaning without escaping, for values that leave as JSON."""
    return _CONTROL.sub("", s or "")


def attr_text(s: str | None, limit: int = 300) -> str:
    """Free text bound for an attribute (``aria-label``): cleaned, shortened, and without the two
    substrings the client sanitiser treats as an attack — which would otherwise strip the label
    from a pathway that merely has an odd name.

    Repeated until nothing is left to remove: one pass turns ``javajavascript:script:`` into
    ``javascript:``, which ``assert_safe_svg`` then refuses, failing the whole preview."""
    out = clean_text(s)
    while True:
        cut = _ATTR_HOSTILE.sub("", out)
        if cut == out:
            break
        out = cut
    return out.strip()[:limit]


def _attr(v: object) -> str:
    """An attribute value, quoted and escaped (quoteattr picks the quote and escapes the rest)."""
    return quoteattr(_CONTROL.sub("", str(v)))


class Builder:
    """Accumulates SVG markup, refusing anything outside the allowlist.

    The check runs at construction time, so a renderer change that reaches for ``<image>`` or an
    ``href`` fails in the test suite the first time it runs rather than in a browser.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []

    @staticmethod
    def _attrs(attrs: dict) -> str:
        out = []
        for k, v in attrs.items():
            if v is None:
                continue
            if k not in ALLOWED_ATTRIBUTES and not _DATA_ATTR.fullmatch(k):
                raise ValueError(f"attribute {k!r} is not in the SVG allowlist")
            out.append(f" {k}={_attr(v)}")
        return "".join(out)

    def open(self, tag: str, attrs: dict | None = None) -> None:
        self._check(tag)
        self.parts.append(f"<{tag}{self._attrs(attrs or {})}>")

    def close(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def leaf(self, tag: str, attrs: dict | None = None, text: str | None = None) -> None:
        self._check(tag)
        a = self._attrs(attrs or {})
        if text is None:
            self.parts.append(f"<{tag}{a}/>")
        else:
            self.parts.append(f"<{tag}{a}>{_text(text)}</{tag}>")

    def raw(self, markup: str) -> None:
        """Markup another Builder produced (already checked)."""
        self.parts.append(markup)

    @staticmethod
    def _check(tag: str) -> None:
        if tag not in ALLOWED_ELEMENTS:
            raise ValueError(f"element {tag!r} is not in the SVG allowlist")

    def markup(self) -> str:
        return "".join(self.parts)


class UnsafeSvg(AssertionError):
    """The document contains something outside the SVG allowlist."""


def assert_safe_svg(svg: bytes | str) -> None:
    """Raise ``UnsafeSvg`` unless every element and attribute is on the allowlist.

    Also refuses a DOCTYPE (entity tricks), any namespace but SVG's, any ``href``, and any value
    carrying ``javascript:`` or a ``url(`` that is not a same-document ``url(#id)`` reference.
    """
    text = svg.decode("utf-8") if isinstance(svg, bytes) else svg
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise UnsafeSvg("DOCTYPE/ENTITY in output")
    if not text.lstrip().startswith("<svg"):
        raise UnsafeSvg("output must begin with <svg")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise UnsafeSvg(f"not well-formed: {exc}") from exc
    for el in root.iter():
        tag = el.tag
        if not tag.startswith("{" + SVG_NS + "}"):
            raise UnsafeSvg(f"element outside the SVG namespace: {tag}")
        local = tag.split("}", 1)[1]
        if local not in ALLOWED_ELEMENTS:
            raise UnsafeSvg(f"element not allowed: {local}")
        for k, v in el.attrib.items():
            if k.startswith("{"):
                raise UnsafeSvg(f"namespaced attribute not allowed: {k}")
            if k not in ALLOWED_ATTRIBUTES and not _DATA_ATTR.fullmatch(k):
                raise UnsafeSvg(f"attribute not allowed: {k}")
            if k.lower().startswith("on") or "href" in k.lower():
                raise UnsafeSvg(f"attribute not allowed: {k}")
            low = v.lower()
            if "javascript:" in low:
                raise UnsafeSvg(f"javascript: in {k}")
            for m in re.finditer(r"url\(([^)]*)\)", low):
                if not re.fullmatch(r"#[a-z0-9_.:-]+", m.group(1).strip()):
                    raise UnsafeSvg(f"non-local url() in {k}")
            if "url(" in low and not re.search(r"url\(#", low):
                raise UnsafeSvg(f"url( in {k}")

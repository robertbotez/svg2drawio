#!/usr/bin/env python3
"""
svg2drawio — convert SVG diagrams into native draw.io / diagrams.net files.

draw.io imports an SVG as one opaque image. This tool instead turns every
shape, line and label of the SVG into a first-class draw.io cell, so the
diagram can be edited (moved, restyled, re-labelled, re-routed) in draw.io.

It understands the subset of SVG that hand-written or generated block
diagrams use: rectangles (also written as paths, with rounded corners),
ellipses, lines / polylines / open paths (with arrowheads), text with
positioned tspans, groups with transforms, inline CSS classes and hatch
patterns. SVG produced by dvisvgm from TikZ (DVI) is supported, including
reconstruction of word spacing and the mapping of TeX fonts.

Usage:
    python svg2drawio.py input.svg                 # writes input.drawio
    python svg2drawio.py input.svg -o out.drawio
    python svg2drawio.py *.svg                     # one .drawio per input
    python svg2drawio.py input.svg --scale 2       # enlarge 2x in draw.io
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _local_tag(elem: ET.Element) -> str:
    """Return the tag name without XML namespace."""
    t = elem.tag
    return t.split("}", 1)[-1] if "}" in t else t


def _num(value, default: float = 0.0) -> float:
    """Parse a possibly unit-bearing SVG number (e.g. '12', '12px', '1.5em')."""
    if value is None:
        return default
    m = _NUM_RE.search(str(value))
    return float(m.group(0)) if m else default


def _get_attr(elem: ET.Element, name: str, default=None):
    """Get an SVG attribute, also looking inside an inline `style=` block."""
    v = elem.get(name)
    if v not in (None, ""):
        return v
    style = elem.get("style") or ""
    for kv in style.split(";"):
        if ":" in kv:
            k, val = kv.split(":", 1)
            if k.strip() == name:
                return val.strip()
    return default


def _has_marker_end(elem: ET.Element) -> bool:
    me = _get_attr(elem, "marker-end")
    return me not in (None, "", "none")


def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _norm_color(c: str | None) -> str | None:
    """Expand #rgb to #rrggbb; leave everything else untouched."""
    if c is None:
        return None
    c = c.strip()
    m = re.fullmatch(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])", c)
    if m:
        return "#" + "".join(ch * 2 for ch in m.groups()).lower()
    return c


# ---------------------------------------------------------------------------
# 2-D affine matrices  (a, b, c, d, e, f):  x' = a x + c y + e,  y' = b x + d y + f
# ---------------------------------------------------------------------------

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mmul(m1: Matrix, m2: Matrix) -> Matrix:
    """m1 ∘ m2 (m2 is applied to the point first)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def mapply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def mscale(m: Matrix) -> float:
    """Uniform scale factor of the matrix (sqrt of |det|)."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0


def parse_transform(s: str | None) -> Matrix:
    m = IDENTITY
    for name, args in re.findall(
            r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", s or ""):
        v = [float(t) for t in _NUM_RE.findall(args)]
        if name == "matrix" and len(v) == 6:
            t: Matrix = (v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            sx = v[0]
            sy = v[1] if len(v) > 1 else sx
            t = (sx, 0, 0, sy, 0, 0)
        elif name == "rotate" and v:
            ang = math.radians(v[0])
            cs, sn = math.cos(ang), math.sin(ang)
            t = (cs, sn, -sn, cs, 0, 0)
            if len(v) == 3:
                cx, cy = v[1], v[2]
                t = mmul(mmul((1, 0, 0, 1, cx, cy), t), (1, 0, 0, 1, -cx, -cy))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = mmul(m, t)
    return m


# ---------------------------------------------------------------------------
# Inherited rendering context
# ---------------------------------------------------------------------------

@dataclass
class Ctx:
    m: Matrix = IDENTITY
    fill: str | None = None          # None = not specified anywhere
    stroke: str | None = None
    stroke_width: float | None = None
    dash: str | None = None
    font_family: str | None = None
    font_size: float | None = None
    font_weight: str | None = None
    font_style: str | None = None
    text_anchor: str | None = None

    def child(self, elem: ET.Element, css: dict[str, dict[str, str]]) -> "Ctx":
        """Context for `elem`: inherit, then apply the element's own attributes."""
        props: dict[str, str] = {}
        for cls in (elem.get("class") or "").split():
            props.update(css.get(cls, {}))
        for name in ("fill", "stroke", "stroke-width", "stroke-dasharray",
                     "font-family", "font-size", "font-weight", "font-style",
                     "text-anchor"):
            v = _get_attr(elem, name)
            if v is not None and name not in props:
                props[name] = v
        c = replace(self)
        c.m = mmul(self.m, parse_transform(elem.get("transform")))
        if "fill" in props:
            c.fill = props["fill"]
        if "stroke" in props:
            c.stroke = props["stroke"]
        if "stroke-width" in props:
            c.stroke_width = _num(props["stroke-width"], 1.0)
        if "stroke-dasharray" in props:
            da = props["stroke-dasharray"]
            c.dash = None if da.strip() in ("", "none") else da
        if "font-family" in props:
            c.font_family = props["font-family"]
        if "font-size" in props:
            c.font_size = _num(props["font-size"], 12.0)
        if "font-weight" in props:
            c.font_weight = props["font-weight"]
        if "font-style" in props:
            c.font_style = props["font-style"]
        if "text-anchor" in props:
            c.text_anchor = props["text-anchor"]
        return c


def parse_css(text: str) -> dict[str, dict[str, str]]:
    """Very small CSS parser: `sel, sel { prop: val; ... }` → {class: {prop: val}}."""
    rules: dict[str, dict[str, str]] = {}
    text = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    text = text.replace("<![CDATA[", "").replace("]]>", "")
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        props: dict[str, str] = {}
        for kv in body.split(";"):
            if ":" in kv:
                k, v = kv.split(":", 1)
                props[k.strip()] = v.strip()
        for sel in sels.split(","):
            sel = sel.strip()
            m = re.search(r"\.([A-Za-z_][\w-]*)$", sel)
            if m:
                rules.setdefault(m.group(1), {}).update(props)
    return rules


# ---------------------------------------------------------------------------
# Embedded SVG fonts (as written by dvisvgm) — used only to measure text
# ---------------------------------------------------------------------------

@dataclass
class SvgFont:
    family: str
    units_per_em: float
    advances: dict[str, float]

    def width(self, text: str, font_size: float) -> float:
        total = 0.0
        for ch in text:
            adv = self.advances.get(ch)
            if adv is None:
                adv = 0.5 * self.units_per_em
            total += adv
        return total * font_size / self.units_per_em


def parse_fonts(root: ET.Element) -> dict[str, SvgFont]:
    fonts: dict[str, SvgFont] = {}
    for f in root.iter():
        if _local_tag(f) != "font":
            continue
        fid = f.get("id") or ""
        family = fid
        upem = 1000.0
        adv: dict[str, float] = {}
        for ch in f:
            t = _local_tag(ch)
            if t == "font-face":
                family = ch.get("font-family") or family
                upem = _num(ch.get("units-per-em"), 1000.0)
            elif t == "glyph":
                u = ch.get("unicode")
                if u:
                    adv[u] = _num(ch.get("horiz-adv-x"), 0.0)
        font = SvgFont(family, upem, adv)
        fonts[fid] = font
        fonts.setdefault(family, font)
    return fonts


def parse_patterns(root: ET.Element) -> dict[str, str]:
    """pattern id → a representative colour (first fill/stroke found inside)."""
    out: dict[str, str] = {}
    for p in root.iter():
        if _local_tag(p) != "pattern":
            continue
        pid = p.get("id")
        if not pid:
            continue
        color = None
        for ch in p.iter():
            c = _get_attr(ch, "fill") or _get_attr(ch, "stroke")
            if c and c != "none" and not c.startswith("url("):
                color = c
                break
        out[pid] = color or "#000000"
    return out


# ---------------------------------------------------------------------------
# Font mapping (TeX families → something draw.io can render)
# ---------------------------------------------------------------------------

def map_font(family: str | None, serif: str, mono: str) -> tuple[str | None, bool, bool]:
    """→ (draw.io font family, bold, italic)."""
    if not family:
        return None, False, False
    first = family.split(",")[0].strip().strip('"').strip("'")
    f = first.lower()
    bold = italic = False
    if re.match(r"^(cm|ec|lm|sf|cs)[a-z]*\d*$", f) or f.startswith(("cmr", "cmbx", "cmti", "cmtt", "cmsy", "cmmi", "cmss", "cmsl", "cmex")):
        if f.startswith(("cmtt", "cmitt", "cmsltt", "ectt", "lmtt")):
            fam = mono
        else:
            fam = serif
        if any(tag in f for tag in ("bx", "cmb", "bold", "ssbx")):
            bold = True
        if any(tag in f for tag in ("ti", "mi", "sl", "it", "bxti")):
            italic = True
        return fam, bold, italic
    return first, False, False


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

@dataclass
class Cell:
    cid: str
    value: str
    style: str
    kind: str  # "vertex" | "edge"
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    src: tuple[float, float] | None = None       # for edges
    dst: tuple[float, float] | None = None       # for edges
    points: list[tuple[float, float]] = field(default_factory=list)  # waypoints
    text_meta: dict | None = None   # set for text cells; used by the merge pass


@dataclass
class ArrowHead:
    """A small filled shape that is probably the tip of an arrow."""
    pts: list[tuple[float, float]]
    size: float


def _style_str(d: dict) -> str:
    out = []
    for k, v in d.items():
        if v == "":
            out.append(k)
        else:
            out.append(f"{k}={v}")
    return ";".join(out) + ";"


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


# ---------------------------------------------------------------------------
# Path data parsing
# ---------------------------------------------------------------------------

@dataclass
class SubPath:
    pts: list[tuple[float, float]]
    curved: list[bool]     # curved[i] → segment pts[i]→pts[i+1] was a curve
    closed: bool


_PATH_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def parse_path(d: str) -> list[SubPath]:
    toks = _PATH_TOKEN.findall(d or "")
    subs: list[SubPath] = []
    cur: SubPath | None = None
    cx = cy = sx = sy = 0.0
    cmd = None
    i = 0
    n = len(toks)

    def nums(k: int) -> list[float]:
        nonlocal i
        vals = [float(t) for t in toks[i:i + k]]
        i += k
        return vals

    def add(x: float, y: float, curved: bool) -> None:
        nonlocal cx, cy, cur
        if cur is None:
            cur = SubPath([(cx, cy)], [], False)
            subs.append(cur)
        cur.pts.append((x, y))
        cur.curved.append(curved)
        cx, cy = x, y

    while i < n:
        t = toks[i]
        if re.match(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur is not None:
                    cur.closed = True
                    cx, cy = sx, sy
                cur = None
                continue
        if cmd is None:
            i += 1
            continue
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c == "M":
                x, y = nums(2)
                if rel:
                    x, y = cx + x, cy + y
                cx, cy, sx, sy = x, y, x, y
                cur = SubPath([(x, y)], [], False)
                subs.append(cur)
                cmd = "l" if rel else "L"     # implicit line-to after move-to
            elif c == "L":
                x, y = nums(2)
                if rel:
                    x, y = cx + x, cy + y
                add(x, y, False)
            elif c == "H":
                (x,) = nums(1)
                add(cx + x if rel else x, cy, False)
            elif c == "V":
                (y,) = nums(1)
                add(cx, cy + y if rel else y, False)
            elif c == "C":
                v = nums(6)
                x, y = v[4], v[5]
                if rel:
                    x, y = cx + x, cy + y
                add(x, y, True)
            elif c in ("S", "Q"):
                v = nums(4)
                x, y = v[2], v[3]
                if rel:
                    x, y = cx + x, cy + y
                add(x, y, True)
            elif c == "T":
                x, y = nums(2)
                if rel:
                    x, y = cx + x, cy + y
                add(x, y, True)
            elif c == "A":
                v = nums(7)
                x, y = v[5], v[6]
                if rel:
                    x, y = cx + x, cy + y
                add(x, y, True)
            else:
                i += 1
        except (ValueError, IndexError):
            break
    # drop degenerate sub-paths (lone move-to's)
    out = []
    for sp in subs:
        pts = [sp.pts[0]]
        curved = []
        for p, cv in zip(sp.pts[1:], sp.curved):
            if abs(p[0] - pts[-1][0]) > 1e-6 or abs(p[1] - pts[-1][1]) > 1e-6:
                pts.append(p)
                curved.append(cv)
        if len(pts) >= 2:
            out.append(SubPath(pts, curved, sp.closed))
    return out


# ---------------------------------------------------------------------------
# Geometry helpers for classification
# ---------------------------------------------------------------------------

def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _is_axis_rect(pts, tol: float) -> tuple[bool, float]:
    """(is rectangle, corner radius) for a closed point list."""
    if len(pts) < 4:
        return False, 0.0
    x0, y0, x1, y1 = _bbox(pts)
    w, h = x1 - x0, y1 - y0
    if w < 2 * tol or h < 2 * tol:
        return False, 0.0
    for x, y in pts:
        on_edge = (abs(x - x0) < tol or abs(x - x1) < tol
                   or abs(y - y0) < tol or abs(y - y1) < tol)
        if not on_edge:
            return False, 0.0
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    radius = 0.0
    for c in corners:
        dmin = min(_dist(c, p) for p in pts)
        if dmin > 0.3 * min(w, h) + tol:
            return False, 0.0
        radius = max(radius, dmin)
    return True, (0.0 if radius < tol else radius)


def _line_intersection(p1, p2, p3, p4):
    d = (p1[0] - p2[0]) * (p3[1] - p4[1]) - (p1[1] - p2[1]) * (p3[0] - p4[0])
    if abs(d) < 1e-9:
        return None
    t = ((p1[0] - p3[0]) * (p3[1] - p4[1]) - (p1[1] - p3[1]) * (p3[0] - p4[0])) / d
    return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))


def _simplify_polyline(pts, curved, tol: float) -> tuple[list, bool]:
    """Collapse rounded corners back to the corner point and drop collinear
    waypoints. Returns (points, had_rounded_corner)."""
    pts = list(pts)
    curved = list(curved)
    rounded = False
    # 1. rounded corners: straight – curve – straight → corner point
    i = 0
    while i < len(curved):
        if curved[i] and 0 < i < len(curved) - 1 and not curved[i - 1] and not curved[i + 1]:
            chord = _dist(pts[i], pts[i + 1])
            x = _line_intersection(pts[i - 1], pts[i], pts[i + 1], pts[i + 2])
            if x is not None and _dist(x, pts[i]) < 4 * chord + tol and _dist(x, pts[i + 1]) < 4 * chord + tol:
                pts[i] = x
                del pts[i + 1]
                del curved[i]
                curved[i - 1] = False
                rounded = True
                continue
        i += 1
    # 2. drop near-duplicate and collinear interior points
    out = [pts[0]]
    for j in range(1, len(pts)):
        if _dist(pts[j], out[-1]) < tol:
            if j == len(pts) - 1:
                out[-1] = pts[j]
            continue
        out.append(pts[j])
    k = 1
    while k < len(out) - 1:
        a, b, c = out[k - 1], out[k], out[k + 1]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) / max(_dist(a, c), 1e-9) < tol:
            del out[k]
        else:
            k += 1
    return out, rounded


_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}
_OPERATORS = "\u00b7\u22c5\u2192\u2194\u2190\u2264\u2265\u2260\u00d7"


def _clean_text(t: str) -> str:
    """Normalise extracted text: ligatures, ". . ." → …, spaces around
    binary operators, collapsed whitespace."""
    for k, v in _LIGATURES.items():
        t = t.replace(k, v)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"(?:\.\s?){3}", "\u2026", t)
    t = re.sub(r"\s*([" + _OPERATORS + r"])\s*", r" \1 ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------

@dataclass
class Options:
    scale: float = 1.0
    serif_font: str = "Times New Roman"
    mono_font: str = "Courier New"
    merge_text: bool = True


def convert(svg_path: Path, options: Options | None = None) -> str:
    opt = options or Options()
    tree = ET.parse(str(svg_path))
    root = tree.getroot()

    vb_attr = root.get("viewBox")
    if vb_attr:
        parts = vb_attr.replace(",", " ").split()
        vx, vy, vw, vh = (float(p) for p in parts[:4])
    else:
        vx = vy = 0.0
        vw = _num(root.get("width"), 800)
        vh = _num(root.get("height"), 600)

    css: dict[str, dict[str, str]] = {}
    for st in root.iter():
        if _local_tag(st) == "style":
            css.update(parse_css("".join(st.itertext())))
    fonts = parse_fonts(root)
    patterns = parse_patterns(root)

    # geometric tolerances in user units
    tol = max(0.35, 0.0012 * max(vw, vh))
    arrow_max = max(8.0, 0.035 * max(vw, vh))

    cells: list[Cell] = []
    arrowheads: list[ArrowHead] = []
    warnings: dict[str, int] = {}
    next_id = [2]

    def warn(msg: str) -> None:
        warnings[msg] = warnings.get(msg, 0) + 1

    def new_id() -> str:
        i = next_id[0]
        next_id[0] += 1
        return str(i)

    # --- style helpers -----------------------------------------------------
    def shape_fill_stroke(ctx: Ctx, default_fill: str, default_stroke: str) -> dict:
        style: dict = {}
        fill = ctx.fill if ctx.fill is not None else default_fill
        stroke = ctx.stroke if ctx.stroke is not None else default_stroke
        if isinstance(fill, str) and fill.startswith("url("):
            pid = re.sub(r"url\(#?([^)]*)\)", r"\1", fill).strip("'\"")
            color = _norm_color(patterns.get(pid, "#000000"))
            style.update({"fillColor": color, "sketch": "1", "fillStyle": "hatch",
                          "hachureGap": _fmt(4 * opt.scale), "jiggle": "0", "curveFitting": "1"})
        else:
            style["fillColor"] = "none" if fill == "none" else _norm_color(fill)
        style["strokeColor"] = "none" if stroke == "none" else _norm_color(stroke)
        if ctx.stroke_width is not None:
            style["strokeWidth"] = _fmt(ctx.stroke_width * mscale(ctx.m) * opt.scale)
        if ctx.dash:
            style["dashed"] = "1"
            style["dashPattern"] = " ".join(_fmt(_num(v) * opt.scale) for v in ctx.dash.replace(",", " ").split())
        return style

    def edge_style(ctx: Ctx, elem: ET.Element, rounded: bool) -> dict:
        style: dict = {"html": "1", "rounded": "1" if rounded else "0",
                       "startArrow": "none",
                       "endArrow": "classic" if _has_marker_end(elem) else "none",
                       "endFill": "1"}
        stroke = ctx.stroke if ctx.stroke is not None else "#000000"
        style["strokeColor"] = "none" if stroke == "none" else _norm_color(stroke)
        if ctx.stroke_width is not None:
            style["strokeWidth"] = _fmt(ctx.stroke_width * mscale(ctx.m) * opt.scale)
        if ctx.dash:
            style["dashed"] = "1"
            style["dashPattern"] = " ".join(_fmt(_num(v) * opt.scale) for v in ctx.dash.replace(",", " ").split())
        return style

    def add_rect(ctx: Ctx, elem: ET.Element, x0, y0, x1, y1, radius: float) -> None:
        w, h = x1 - x0, y1 - y0
        style: dict = {"whiteSpace": "wrap", "html": "1"}
        if radius > 0 and min(w, h) > 0:
            style["rounded"] = "1"
            style["arcSize"] = str(max(5, min(50, int(round(200 * radius / min(w, h))))))
        else:
            style["rounded"] = "0"
        style.update(shape_fill_stroke(ctx, "#ffffff", "#000000"))
        cells.append(Cell(new_id(), "", _style_str(style), "vertex", x0, y0, w, h))

    def add_edge(ctx: Ctx, elem: ET.Element, pts, rounded: bool, closed: bool = False) -> None:
        if closed:
            pts = list(pts) + [pts[0]]
        style = edge_style(ctx, elem, rounded)
        cells.append(Cell(new_id(), "", _style_str(style), "edge",
                          src=pts[0], dst=pts[-1], points=list(pts[1:-1])))

    def add_polygon_or_rect(ctx: Ctx, elem: ET.Element, gpts, curved) -> None:
        is_rect, radius = _is_axis_rect(gpts, tol)
        if is_rect:
            x0, y0, x1, y1 = _bbox(gpts)
            add_rect(ctx, elem, x0, y0, x1, y1, radius)
            return
        fill = ctx.fill if ctx.fill is not None else "#000000"
        x0, y0, x1, y1 = _bbox(gpts)
        size = math.hypot(x1 - x0, y1 - y0)
        if fill != "none" and size <= arrow_max:
            arrowheads.append(ArrowHead(list(gpts), size))
            return
        simp, rounded = _simplify_polyline(gpts, curved, tol)
        if len(simp) >= 2:
            warn("closed non-rectangular shape emitted as a closed polyline (fill dropped)")
            add_edge(ctx, elem, simp, rounded, closed=True)

    # --- text --------------------------------------------------------------
    def text_runs(elem: ET.Element, fs: float, font: SvgFont | None):
        """→ (lines, local start x, local end x, line gap): one entry per
        line (a tspan with a new `y` starts a new line); spaces are
        reconstructed from the horizontal gaps between runs."""
        x0 = _num(elem.get("x"))
        y0 = _num(elem.get("y"))
        space_gap = 0.16 * fs
        lines: list[list[str]] = [[]]
        baselines: list[float] = [y0]
        cursor = x0
        start = None
        end = x0

        def measure(t: str) -> float:
            if font is not None:
                return font.width(t, fs)
            return len(t) * fs * 0.55

        def emit(t: str, x_abs: float | None, y_abs: float | None) -> None:
            nonlocal cursor, start, end
            if not t:
                return
            if y_abs is not None and abs(y_abs - baselines[-1]) > 0.3 * fs:
                lines.append([])
                baselines.append(y_abs)
                cursor = x_abs if x_abs is not None else x0
                if x_abs is not None and start is not None:
                    start = min(start, x_abs)
            elif x_abs is not None:
                if start is not None and x_abs - cursor > space_gap and lines[-1] and not lines[-1][-1].endswith(" "):
                    lines[-1].append(" ")
                cursor = x_abs
            if start is None:
                start = cursor
            lines[-1].append(t)
            cursor += measure(t)
            end = max(end, cursor)

        def walk_text(e: ET.Element, first: bool) -> None:
            t = re.sub(r"\s+", " ", e.text or "")
            if first:
                emit(t.strip(), x0, None)
            else:
                xa = e.get("x")
                ya = e.get("y")
                emit(t if t.strip() else "", _num(xa) if xa is not None else None,
                     _num(ya) if ya is not None else None)
            for ch in e:
                if _local_tag(ch) in ("tspan", "textPath"):
                    walk_text(ch, False)
                tail = re.sub(r"\s+", " ", ch.tail or "")
                emit(tail if tail.strip() else "", None, None)

        walk_text(elem, True)
        out_lines = [_clean_text("".join(parts)) for parts in lines]
        out_lines = [ln for ln in out_lines if ln]
        gap = (baselines[1] - baselines[0]) if len(baselines) > 1 else 1.2 * fs
        return out_lines, (start if start is not None else x0), end, abs(gap)

    def add_text(ctx: Ctx, elem: ET.Element) -> None:
        fs_local = ctx.font_size if ctx.font_size is not None else 12.0
        fam_attr = ctx.font_family
        font = None
        for cls in (elem.get("class") or "").split():
            if cls in fonts:
                font = fonts[cls]
                break
        if font is None and fam_attr:
            font = fonts.get(fam_attr.split(",")[0].strip().strip("'\""))
        lines, lx0, lx1, line_gap = text_runs(elem, fs_local, font)
        if not lines:
            return
        text = "\n".join(lines)
        nlines = len(lines)
        anchor = ctx.text_anchor or "start"
        width_local = max(lx1 - lx0, 0.0)
        if anchor == "middle":
            lx0 -= width_local / 2
        elif anchor == "end":
            lx0 -= width_local
        ly = _num(elem.get("y"))
        s = mscale(ctx.m)
        fs = fs_local * s
        w = max(width_local * s, fs * 0.6)
        h = fs * 1.4 + (nlines - 1) * line_gap * s
        # text direction / rotation from the matrix (y-down coordinates)
        p0 = mapply(ctx.m, lx0, ly)
        p1 = mapply(ctx.m, lx0 + max(width_local, 1.0), ly)
        angle = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
        # centre of the box in local coordinates, then to global
        c_local = (lx0 + width_local / 2, ly - 0.3 * fs_local + (nlines - 1) * line_gap / 2)
        cxg, cyg = mapply(ctx.m, *c_local)
        family, bold, italic = map_font(fam_attr, opt.serif_font, opt.mono_font)
        fw = (ctx.font_weight or "").strip()
        if fw in ("bold", "bolder", "600", "700", "800", "900"):
            bold = True
        if (ctx.font_style or "") == "italic":
            italic = True
        color = None
        # colour: own/inherited fill, or the first tspan's fill
        fill = ctx.fill
        for ch in elem.iter():
            if ch is not elem and _get_attr(ch, "fill"):
                fill = _get_attr(ch, "fill")
                break
        if fill and fill != "none" and not fill.startswith("url("):
            color = _norm_color(fill)
        style: dict = {"text": "", "html": "1", "strokeColor": "none", "fillColor": "none",
                       "align": "center", "verticalAlign": "middle", "overflow": "visible",
                       "spacing": "0", "spacingLeft": "0", "spacingRight": "0",
                       "fontSize": str(int(round(fs * opt.scale)))}
        flag = (1 if bold else 0) | (2 if italic else 0)
        if flag:
            style["fontStyle"] = str(flag)
        if color:
            style["fontColor"] = color
        if family:
            style["fontFamily"] = family
        if abs(angle) > 0.5:
            style["rotation"] = _fmt(angle)
        meta = {"fs": fs, "angle": angle, "baseline_y": p0[1], "x0": p0[0],
                "x1": p0[0] + w, "color": color, "family": family, "flag": flag,
                "lines": nlines, "last_baseline": p0[1] + (nlines - 1) * line_gap * s,
                "cx": cxg}
        cells.append(Cell(new_id(), text, _style_str(style), "vertex",
                          cxg - w / 2, cyg - h / 2, w, h, text_meta=meta))

    # --- tree walk ---------------------------------------------------------
    def walk(elem: ET.Element, parent_ctx: Ctx) -> None:
        tag = _local_tag(elem)
        if tag in ("defs", "style", "clipPath", "pattern", "font", "marker",
                   "metadata", "title", "desc", "linearGradient", "radialGradient", "mask"):
            return
        ctx = parent_ctx.child(elem, css)
        if tag in ("svg", "g", "a", "switch"):
            for ch in elem:
                walk(ch, ctx)
            return
        if tag == "rect":
            x = _num(elem.get("x"))
            y = _num(elem.get("y"))
            w = _num(elem.get("width"))
            h = _num(elem.get("height"))
            if w <= 0 or h <= 0:
                return
            corners = [mapply(ctx.m, x, y), mapply(ctx.m, x + w, y),
                       mapply(ctx.m, x + w, y + h), mapply(ctx.m, x, y + h)]
            x0, y0, x1, y1 = _bbox(corners)
            rx = _num(elem.get("rx")) * mscale(ctx.m)
            add_rect(ctx, elem, x0, y0, x1, y1, rx)
            return
        if tag in ("ellipse", "circle"):
            cx = _num(elem.get("cx"))
            cy = _num(elem.get("cy"))
            if tag == "circle":
                rx = ry = _num(elem.get("r"))
            else:
                rx = _num(elem.get("rx"))
                ry = _num(elem.get("ry"))
            pts = [mapply(ctx.m, cx - rx, cy - ry), mapply(ctx.m, cx + rx, cy - ry),
                   mapply(ctx.m, cx + rx, cy + ry), mapply(ctx.m, cx - rx, cy + ry)]
            x0, y0, x1, y1 = _bbox(pts)
            style: dict = {"ellipse": "", "whiteSpace": "wrap", "html": "1"}
            style.update(shape_fill_stroke(ctx, "#ffffff", "#000000"))
            cells.append(Cell(new_id(), "", _style_str(style), "vertex", x0, y0, x1 - x0, y1 - y0))
            return
        if tag == "line":
            p1 = mapply(ctx.m, _num(elem.get("x1")), _num(elem.get("y1")))
            p2 = mapply(ctx.m, _num(elem.get("x2")), _num(elem.get("y2")))
            add_edge(ctx, elem, [p1, p2], False)
            return
        if tag in ("polyline", "polygon"):
            nums = [float(t) for t in _NUM_RE.findall(elem.get("points") or "")]
            pts = [mapply(ctx.m, nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
            if len(pts) < 2:
                return
            if tag == "polygon":
                add_polygon_or_rect(ctx, elem, pts, [False] * (len(pts) - 1))
            else:
                simp, _ = _simplify_polyline(pts, [False] * (len(pts) - 1), tol)
                add_edge(ctx, elem, simp, False)
            return
        if tag == "path":
            subs = parse_path(elem.get("d") or "")
            for sp in subs:
                gpts = [mapply(ctx.m, x, y) for x, y in sp.pts]
                closed = sp.closed or (len(gpts) > 3 and _dist(gpts[0], gpts[-1]) < tol)
                if closed and _dist(gpts[0], gpts[-1]) < tol and len(gpts) > 2:
                    gpts = gpts[:-1]
                    curved = sp.curved[:-1]
                else:
                    curved = sp.curved
                if closed:
                    add_polygon_or_rect(ctx, elem, gpts, curved)
                else:
                    simp, rounded = _simplify_polyline(gpts, curved, tol)
                    if len(simp) >= 2:
                        add_edge(ctx, elem, simp, rounded)
            return
        if tag == "text":
            add_text(ctx, elem)
            return
        if tag == "use":
            warn("skipping <use> (glyph outlines / symbols are not supported; "
                 "for TeX figures convert the DVI with dvisvgm to keep real text)")
            return
        warn(f"skipping <{tag}> (unsupported)")

    walk(root, Ctx())

    # --- attach arrowheads to the nearest edge end-point -------------------
    for ah in arrowheads:
        best = None
        for c in cells:
            if c.kind != "edge":
                continue
            for end in ("src", "dst"):
                pt = c.src if end == "src" else c.dst
                d = min(_dist(pt, q) for q in ah.pts)
                if d <= 1.5 * ah.size + tol and (best is None or d < best[0]):
                    best = (d, c, end)
        if best is None:
            warn("small filled shape near no line end-point dropped (arrowhead?)")
            continue
        _, c, end = best
        # apex = the tip point that lies furthest along the line direction
        if end == "dst":
            prev_pt = c.points[-1] if c.points else c.src
            tip_end = c.dst
        else:
            prev_pt = c.points[0] if c.points else c.dst
            tip_end = c.src
        dxv, dyv = tip_end[0] - prev_pt[0], tip_end[1] - prev_pt[1]
        norm = math.hypot(dxv, dyv) or 1.0
        apex = max(ah.pts, key=lambda q: ((q[0] - tip_end[0]) * dxv + (q[1] - tip_end[1]) * dyv) / norm)
        style = c.style
        if end == "dst":
            c.dst = apex
            style = re.sub(r"endArrow=[^;]*;", "endArrow=classic;", style)
        else:
            c.src = apex
            style = re.sub(r"startArrow=[^;]*;", "startArrow=classic;startFill=1;", style)
        c.style = style

    # --- merge adjacent text cells: runs on one baseline (a label split at
    #     kerns or font changes) and stacked lines of one multi-line label ---
    if opt.merge_text:
        merged: list[Cell] = []
        for c in cells:
            prev = merged[-1] if merged else None
            if (c.kind == "vertex" and c.text_meta and prev is not None and prev.kind == "vertex"
                    and prev.text_meta and abs(prev.text_meta["angle"]) < 0.5
                    and abs(c.text_meta["angle"]) < 0.5):
                pm, cm = prev.text_meta, c.text_meta
                fs = max(pm["fs"], cm["fs"])
                gap = cm["x0"] - pm["x1"]
                same_line = (pm["lines"] == 1 and cm["lines"] == 1
                             and abs(pm["baseline_y"] - cm["baseline_y"]) < 0.35 * fs
                             and -0.2 * fs < gap < 0.8 * fs
                             and pm["color"] == cm["color"])
                dy = cm["baseline_y"] - pm["last_baseline"]
                same_font = pm["family"] == cm["family"] and pm["flag"] == cm["flag"]
                centred = abs(cm["cx"] - pm["cx"]) < 0.6 * fs
                stacked = (cm["lines"] == 1
                           and abs(pm["fs"] - cm["fs"]) < 0.5
                           and pm["color"] == cm["color"]
                           and 0.8 * fs < dy < 1.9 * fs
                           and ((same_font and (centred or abs(cm["x0"] - pm["x0"]) < 0.6 * fs))
                                or (centred and abs(dy - 1.2 * fs) < 0.35 * fs)))
                if same_line or stacked:
                    if same_line:
                        sep = " " if gap > 0.15 * fs else ""
                        if len(c.value) > pm.get("nchars", len(prev.value)):
                            # the longer run decides font family / bold / italic
                            prev.style = c.style
                            pm["family"], pm["flag"] = cm["family"], cm["flag"]
                            pm["nchars"] = len(c.value)
                        prev.value = _clean_text(prev.value + sep + c.value)
                        pm["x1"] = max(pm["x1"], cm["x1"])
                    else:
                        prev.value = prev.value + "\n" + c.value
                        pm["lines"] += 1
                        pm["last_baseline"] = cm["baseline_y"]
                        if abs(cm["cx"] - pm["cx"]) >= 0.6 * fs:
                            prev.style = prev.style.replace("align=center;", "align=left;")
                        pm["x0"] = min(pm["x0"], cm["x0"])
                        pm["x1"] = max(pm["x1"], cm["x1"])
                    x0 = min(prev.x, c.x)
                    x1 = max(prev.x + prev.w, c.x + c.w)
                    y0 = min(prev.y, c.y)
                    y1 = max(prev.y + prev.h, c.y + c.h)
                    prev.x, prev.y, prev.w, prev.h = x0, y0, x1 - x0, y1 - y0
                    pm["cx"] = (pm["x0"] + pm["x1"]) / 2
                    if cm["fs"] > pm["fs"]:
                        pm["fs"] = cm["fs"]
                        prev.style = re.sub(r"fontSize=\d+;", f"fontSize={int(round(cm['fs'] * opt.scale))};", prev.style)
                    continue
            merged.append(c)
        cells = merged

    for msg, n in warnings.items():
        print(f"[svg2drawio] warning: {msg} (x{n})", file=sys.stderr)

    return _serialise(cells, vx, vy, vw, vh, svg_path.stem, opt.scale)


def _label_value(c: Cell) -> str:
    """Label text for the XML attribute. draw.io labels with html=1 are HTML,
    so the text is HTML-escaped first (a literal "<" must survive), new lines
    become <br>, and the result is XML-escaped once more."""
    if c.text_meta is None:
        return _xml_escape(c.value)
    html_text = "<br>".join(
        v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for v in c.value.split("\n"))
    return _xml_escape(html_text)


def _serialise(cells: list[Cell], vx: float, vy: float, vw: float, vh: float,
               diagram_name: str, scale: float = 1.0) -> str:
    pad = 20
    page_w = int(vw * scale + 2 * pad)
    page_h = int(vh * scale + 2 * pad)

    def X(x: float) -> str:
        return _fmt((x - vx) * scale + pad)

    def Y(y: float) -> str:
        return _fmt((y - vy) * scale + pad)

    head = (
        f'<mxfile host="app.diagrams.net" agent="svg2drawio" type="device" '
        f'modified="{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}">\n'
        f'  <diagram id="{_xml_escape(diagram_name)}" '
        f'name="{_xml_escape(diagram_name)}">\n'
        f'    <mxGraphModel dx="{page_w}" dy="{page_h}" grid="1" gridSize="10" '
        f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
        f'pageScale="1" pageWidth="{page_w}" pageHeight="{page_h}" '
        f'math="0" shadow="0">\n'
        '      <root>\n'
        '        <mxCell id="0" />\n'
        '        <mxCell id="1" parent="0" />\n'
    )

    body_lines: list[str] = []
    for c in cells:
        if c.kind == "vertex":
            body_lines.append(
                f'        <mxCell id="{c.cid}" value="{_label_value(c)}" '
                f'style="{_xml_escape(c.style)}" vertex="1" parent="1">\n'
                f'          <mxGeometry x="{X(c.x)}" y="{Y(c.y)}" '
                f'width="{_fmt(c.w * scale)}" height="{_fmt(c.h * scale)}" as="geometry" />\n'
                f'        </mxCell>\n'
            )
        else:  # edge
            sx, sy = c.src  # type: ignore[misc]
            dx, dy = c.dst  # type: ignore[misc]
            pts = ""
            if c.points:
                pts = ('            <Array as="points">\n'
                       + "".join(f'              <mxPoint x="{X(px)}" y="{Y(py)}" />\n' for px, py in c.points)
                       + '            </Array>\n')
            body_lines.append(
                f'        <mxCell id="{c.cid}" value="{_xml_escape(c.value)}" '
                f'style="{_xml_escape(c.style)}" edge="1" parent="1">\n'
                '          <mxGeometry relative="1" as="geometry">\n'
                f'            <mxPoint x="{X(sx)}" y="{Y(sy)}" as="sourcePoint" />\n'
                f'            <mxPoint x="{X(dx)}" y="{Y(dy)}" as="targetPoint" />\n'
                + pts +
                '          </mxGeometry>\n'
                '        </mxCell>\n'
            )

    tail = (
        '      </root>\n'
        '    </mxGraphModel>\n'
        '  </diagram>\n'
        '</mxfile>\n'
    )
    return head + "".join(body_lines) + tail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert SVG diagrams to native draw.io files.",
        epilog="See the README for the list of supported SVG features.",
    )
    p.add_argument("inputs", nargs="+", help="Path(s) to .svg file(s).")
    p.add_argument("-o", "--output",
                   help="Output path (only valid for a single input).")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Do not print per-file conversion log.")
    p.add_argument("--scale", type=float, default=1.0,
                   help="Multiply all coordinates, sizes and fonts (default 1).")
    p.add_argument("--serif-font", default="Times New Roman",
                   help="draw.io font used for TeX serif families (cmr, cmbx, ...).")
    p.add_argument("--mono-font", default="Courier New",
                   help="draw.io font used for TeX typewriter families (cmtt).")
    p.add_argument("--no-merge-text", action="store_true",
                   help="Keep every <text> element as its own cell.")
    args = p.parse_args(argv)

    if args.output and len(args.inputs) != 1:
        print("error: --output can only be used with a single input file.",
              file=sys.stderr)
        return 2

    opt = Options(scale=args.scale, serif_font=args.serif_font,
                  mono_font=args.mono_font, merge_text=not args.no_merge_text)
    rc = 0
    for inp in args.inputs:
        ipath = Path(inp)
        if not ipath.exists():
            print(f"error: {ipath} not found", file=sys.stderr)
            rc = 1
            continue
        opath = Path(args.output) if args.output else ipath.with_suffix(".drawio")
        try:
            xml = convert(ipath, opt)
            opath.write_text(xml, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"error: failed to convert {ipath}: {exc}", file=sys.stderr)
            rc = 1
            continue
        if not args.quiet:
            print(f"{ipath.name} → {opath.name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

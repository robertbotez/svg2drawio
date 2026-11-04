#!/usr/bin/env python3
"""
preview_drawio — rough PNG preview of a .drawio file (no draw.io needed).

Draws rectangles, ellipses, text cells and edges (with waypoints, dashes and
arrowheads) from an uncompressed draw.io file so that a conversion can be
sanity-checked from the command line. It is an approximation of draw.io's
rendering (fonts, arrow sizes and hatch fills differ), not a replacement.

Requires matplotlib (optional dependency; the converter itself does not).

Usage
-----
    python preview_drawio.py diagram.drawio             # writes diagram.png
    python preview_drawio.py diagram.drawio -o out.png --dpi 150
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_style(s: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in (s or "").split(";"):
        if not kv:
            continue
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
        else:
            out[kv] = ""
    return out


def render(path: Path, out: Path, dpi: int = 150) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse, FancyBboxPatch, Rectangle

    root = ET.parse(str(path)).getroot()
    model = root.find(".//mxGraphModel")
    pw = float(model.get("pageWidth", 800))
    ph = float(model.get("pageHeight", 600))
    fig, ax = plt.subplots(figsize=(pw / 72, ph / 72))
    ax.set_xlim(0, pw)
    ax.set_ylim(ph, 0)
    ax.set_aspect("equal")
    ax.axis("off")

    for cell in root.findall(".//mxCell"):
        st = parse_style(cell.get("style"))
        geo = cell.find("mxGeometry")
        if geo is None:
            continue
        if cell.get("vertex") == "1":
            x, y = float(geo.get("x", 0)), float(geo.get("y", 0))
            w, h = float(geo.get("width", 0)), float(geo.get("height", 0))
            fill = st.get("fillColor", "#ffffff")
            stroke = st.get("strokeColor", "#000000")
            lw = float(st.get("strokeWidth", 1))
            dashed = st.get("dashed") == "1"
            if "text" in st:
                pass  # text-only cell: drawn below
            elif "ellipse" in st:
                ax.add_patch(Ellipse((x + w / 2, y + h / 2), w, h,
                                     facecolor="none" if fill == "none" else fill,
                                     edgecolor="none" if stroke == "none" else stroke,
                                     linewidth=lw, linestyle="--" if dashed else "-"))
            else:
                hatch = "////" if st.get("fillStyle") == "hatch" else None
                if st.get("rounded") == "1":
                    r = min(w, h) * float(st.get("arcSize", 20)) / 200
                    patch = FancyBboxPatch((x + r, y + r), w - 2 * r, h - 2 * r,
                                           boxstyle=f"round,pad={r}",
                                           facecolor="none" if fill == "none" else (fill if not hatch else "none"),
                                           edgecolor="none" if stroke == "none" else stroke,
                                           linewidth=lw, linestyle="--" if dashed else "-",
                                           hatch=hatch)
                    if hatch:
                        patch.set_edgecolor(fill)
                    ax.add_patch(patch)
                else:
                    ax.add_patch(Rectangle((x, y), w, h,
                                           facecolor="none" if fill == "none" else (fill if not hatch else "none"),
                                           edgecolor=(fill if hatch else ("none" if stroke == "none" else stroke)),
                                           linewidth=lw, linestyle="--" if dashed else "-", hatch=hatch))
            raw = cell.get("value") or ""          # HTML (draw.io html=1 labels)
            if raw:
                text = re.sub(r"<br\s*/?>", "\n", raw)
                text = re.sub(r"</?(div|span|b|i|u|font|p)[^>]*>", "", text)
                text = html.unescape(text)
                fs = float(st.get("fontSize", 12))
                color = st.get("fontColor", "#000000")
                weight = "bold" if int(st.get("fontStyle", 0)) & 1 else "normal"
                style = "italic" if int(st.get("fontStyle", 0)) & 2 else "normal"
                fam = st.get("fontFamily", "serif").lower()
                if "courier" in fam or "mono" in fam:
                    family = "monospace"
                elif any(k in fam for k in ("helvetica", "arial", "sans", "verdana", "roboto")):
                    family = "sans-serif"
                else:
                    family = "serif"
                rot = -float(st.get("rotation", 0))
                align = st.get("align", "center")
                ha = {"left": "left", "right": "right"}.get(align, "center")
                tx = {"left": x, "right": x + w}.get(align, x + w / 2)
                ax.text(tx, y + h / 2, text, fontsize=fs * 72 / 96 * 1.0, color=color,
                        ha=ha, va="center", rotation=rot, rotation_mode="anchor",
                        family=family, weight=weight, style=style, linespacing=1.15)
        elif cell.get("edge") == "1":
            sp = geo.find("mxPoint[@as='sourcePoint']")
            tp = geo.find("mxPoint[@as='targetPoint']")
            if sp is None or tp is None:
                continue
            pts = [(float(sp.get("x")), float(sp.get("y")))]
            for p in geo.findall("Array/mxPoint"):
                pts.append((float(p.get("x")), float(p.get("y"))))
            pts.append((float(tp.get("x")), float(tp.get("y"))))
            stroke = st.get("strokeColor", "#000000")
            lw = float(st.get("strokeWidth", 1))
            dashed = st.get("dashed") == "1"
            xs, ys = zip(*pts)
            ax.plot(xs, ys, color=stroke if stroke != "none" else "none", linewidth=lw,
                    linestyle="--" if dashed else "-", solid_capstyle="butt")
            for which in ("endArrow", "startArrow"):
                if st.get(which, "none") not in ("none", ""):
                    a, b = (pts[-2], pts[-1]) if which == "endArrow" else (pts[1], pts[0])
                    # only the head: start the arrow patch a hair before the tip
                    d = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 or 1.0
                    a = (b[0] - (b[0] - a[0]) / d, b[1] - (b[1] - a[1]) / d)
                    ax.annotate("", xy=b, xytext=a,
                                arrowprops=dict(arrowstyle="-|>", color=stroke, lw=lw,
                                                mutation_scale=8 + 3 * lw, shrinkA=0, shrinkB=0))
    fig.savefig(str(out), dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render a rough PNG preview of a .drawio file.")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--dpi", type=int, default=150)
    a = p.parse_args(argv)
    src = Path(a.input)
    out = Path(a.output) if a.output else src.with_suffix(".png")
    render(src, out, a.dpi)
    print(f"{src.name} → {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

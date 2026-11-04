#!/usr/bin/env python3
"""
tikz2drawio — TikZ / standalone .tex → editable .drawio, via DVI + dvisvgm.

Why DVI and not PDF: every PDF→SVG converter (pdf2svg, pdftocairo, Inkscape)
turns glyphs into outlines, so the text is lost for editing. `dvisvgm` run on
a DVI file keeps `<text>` elements and emits the TikZ shapes as `<path>`s,
which svg2drawio can classify into rectangles, edges and arrowheads.

The .tex file is compiled with `latex` after injecting

    \\def\\pgfsysdriver{pgfsys-dvisvgm.def}

in front of \\documentclass (and the `dvisvgm` option for the standalone
class), so the TikZ picture is written with the dvisvgm driver. Multi-page
documents (standalone `multi=page`) give one .drawio per page.

Requirements: latex + dvisvgm (both in TeX Live / MacTeX), Python 3.9+.

Usage
-----
    python tikz2drawio.py figure.tex                     # figure.drawio (page 1)
    python tikz2drawio.py figs.tex -o out/ --names a b c # one file per page
    python tikz2drawio.py figs.tex --scale 2             # enlarge 2x in draw.io
    python tikz2drawio.py figure.svg                     # an existing dvisvgm SVG
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from svg2drawio import Options, convert  # noqa: E402

DRIVER_LINE = "\\def\\pgfsysdriver{pgfsys-dvisvgm.def}\n"


def inject_driver(tex: str) -> str:
    if "pgfsys-dvisvgm.def" in tex:
        return tex
    m = re.search(r"\\documentclass(\[[^\]]*\])?\{standalone\}", tex)
    if m:
        opts = (m.group(1) or "[]")[1:-1]
        parts = [o.strip() for o in opts.split(",") if o.strip()]
        if "dvisvgm" not in parts:
            parts.append("dvisvgm")
        new = "\\documentclass[" + ",".join(parts) + "]{standalone}"
        tex = tex[:m.start()] + new + tex[m.end():]
    m = re.search(r"\\documentclass", tex)
    if not m:
        raise SystemExit("no \\documentclass found")
    return tex[:m.start()] + DRIVER_LINE + tex[m.start():]


def run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
        raise SystemExit(f"command failed: {' '.join(cmd)}\n" + "\n".join(tail))


def tex_to_svgs(tex_path: Path, workdir: Path) -> list[Path]:
    for tool in ("latex", "dvisvgm"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH (install TeX Live / MacTeX)")
    src = tex_path.read_text(encoding="utf-8")
    job = workdir / (tex_path.stem + "_dvisvgm.tex")
    job.write_text(inject_driver(src), encoding="utf-8")
    # copy sibling inputs (\input, images) next to the job file
    for f in tex_path.parent.iterdir():
        if f.is_file() and f.suffix.lower() in (".tex", ".sty", ".png", ".jpg", ".pdf", ".csv", ".dat") and f != tex_path:
            try:
                shutil.copy(f, workdir / f.name)
            except OSError:
                pass
    run(["latex", "-interaction=nonstopmode", "-halt-on-error", job.name], workdir)
    dvi = job.with_suffix(".dvi")
    if not dvi.exists():
        raise SystemExit("latex produced no DVI file")
    # one SVG per page: <stem>-N.svg (dvisvgm numbers from 1)
    run(["dvisvgm", "--page=1-", "--bbox=min", "-o", f"{job.stem}-%p.svg", dvi.name], workdir)
    svgs = sorted(workdir.glob(f"{job.stem}-*.svg"),
                  key=lambda p: int(re.search(r"-(\d+)\.svg$", p.name).group(1)))
    return svgs


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TikZ (.tex) or dvisvgm SVG → native .drawio")
    p.add_argument("input", help=".tex (compiled with latex + dvisvgm) or .svg")
    p.add_argument("-o", "--outdir", default=None, help="output directory (default: next to the input)")
    p.add_argument("--names", nargs="+", default=None, help="output file names, one per page")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--serif-font", default="Times New Roman")
    p.add_argument("--mono-font", default="Courier New")
    p.add_argument("--keep-svg", action="store_true", help="also copy the intermediate SVGs")
    a = p.parse_args(argv)

    src = Path(a.input).resolve()
    outdir = Path(a.outdir).resolve() if a.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)
    opt = Options(scale=a.scale, serif_font=a.serif_font, mono_font=a.mono_font)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if src.suffix.lower() == ".svg":
            svgs = [src]
        else:
            svgs = tex_to_svgs(src, work)
        if not svgs:
            raise SystemExit("no SVG pages produced")
        for i, svg in enumerate(svgs):
            if a.names and i < len(a.names):
                name = a.names[i]
            elif len(svgs) == 1:
                name = src.stem
            else:
                name = f"{src.stem}_p{i + 1}"
            out = outdir / (name + ".drawio")
            out.write_text(convert(svg, opt), encoding="utf-8")
            if a.keep_svg:
                shutil.copy(svg, outdir / (name + ".svg"))
            print(f"page {i + 1}: {svg.name} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for svg2drawio.

Run with:
    python3 tests/test_basic.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from svg2drawio import Options, convert  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

MINIMAL_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"
     font-family="Arial" font-size="13">
  <rect x="10" y="10" width="80" height="40" rx="6"
        fill="#dbeafe" stroke="#1f6feb" stroke-width="1.5"/>
  <text x="50" y="35" text-anchor="middle" font-weight="bold">Hello</text>
  <line x1="90" y1="30" x2="180" y2="30"
        stroke="#374151" stroke-width="1.4" marker-end="url(#arr)"/>
  <ellipse cx="180" cy="60" rx="15" ry="10"
           fill="#fef3c7" stroke="#d97706"/>
</svg>
"""


def to_dom(svg_text: str, options: Options | None = None) -> ET.Element:
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(svg_text)
        tmp = Path(f.name)
    try:
        return ET.fromstring(convert(tmp, options))
    finally:
        tmp.unlink()


def cells(dom: ET.Element) -> list[ET.Element]:
    return dom.findall(".//mxCell")


def vertices(dom: ET.Element) -> list[ET.Element]:
    return [c for c in cells(dom) if c.get("vertex") == "1"]


def edges(dom: ET.Element) -> list[ET.Element]:
    return [c for c in cells(dom) if c.get("edge") == "1"]


def texts(dom: ET.Element) -> list[ET.Element]:
    return [c for c in vertices(dom) if (c.get("style") or "").startswith("text;")]


class BasicElements(unittest.TestCase):

    def test_cell_counts(self):
        dom = to_dom(MINIMAL_SVG)
        # 2 wrapper cells + rect + text + edge + ellipse
        self.assertEqual(len(cells(dom)), 6)
        self.assertEqual(len(vertices(dom)), 3)
        self.assertEqual(len(edges(dom)), 1)

    def test_marker_end_gives_arrow(self):
        dom = to_dom(MINIMAL_SVG)
        self.assertIn("endArrow=classic", edges(dom)[0].get("style"))

    def test_text_inherits_root_font(self):
        dom = to_dom(MINIMAL_SVG)
        cell = next(c for c in texts(dom) if c.get("value") == "Hello")
        self.assertIn("fontSize=13", cell.get("style"))
        self.assertIn("fontFamily=Arial", cell.get("style"))
        self.assertIn("fontStyle=1", cell.get("style"))

    def test_rounded_rect(self):
        dom = to_dom(MINIMAL_SVG)
        rect = next(c for c in vertices(dom) if "rounded=1" in (c.get("style") or ""))
        self.assertIn("arcSize=", rect.get("style"))

    def test_unsupported_element_is_skipped(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
               '<image href="x.png" width="5" height="5"/></svg>')
        self.assertEqual(len(cells(to_dom(svg))), 2)

    def test_scale_option(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 50 50">'
               '<rect x="0" y="0" width="10" height="10"/></svg>')
        g = vertices(to_dom(svg, Options(scale=3.0)))[0].find("mxGeometry")
        self.assertAlmostEqual(float(g.get("width")), 30, places=1)


class Paths(unittest.TestCase):

    def test_rounded_rect_path(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">'
               '<path d="M 90 50 L 12 50 C 11 50 10 49 10 48 L 10 12 C 10 11 11 10 12 10 '
               'L 90 10 C 91 10 92 11 92 12 L 92 48 C 92 49 91 50 90 50 Z" fill="#cfc" stroke="#000"/>'
               '</svg>')
        rect = vertices(to_dom(svg))[0]
        self.assertIn("rounded=1", rect.get("style"))
        self.assertIn("fillColor=#ccffcc", rect.get("style"))
        g = rect.find("mxGeometry")
        self.assertAlmostEqual(float(g.get("width")), 82, places=1)
        self.assertAlmostEqual(float(g.get("height")), 40, places=1)

    def test_polyline_edge_with_waypoint(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
               '<path d="M 10 10 L 10 50 L 80 50" fill="none" stroke="#333" stroke-dasharray="3,3"/>'
               '</svg>')
        edge = edges(to_dom(svg))[0]
        self.assertIn("dashed=1", edge.get("style"))
        self.assertEqual(len(edge.findall("mxGeometry/Array/mxPoint")), 1)

    def test_arrowhead_shape_attached_to_line(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
               '<path d="M 10 50 L 70 50" fill="none" stroke="#000"/>'
               '<path d="M 76 50 L 70 52 L 70 48 Z" fill="#000"/>'
               '</svg>')
        dom = to_dom(svg)
        self.assertEqual(len(edges(dom)), 1)
        edge = edges(dom)[0]
        self.assertIn("endArrow=classic", edge.get("style"))
        tp = edge.find("mxGeometry/mxPoint[@as='targetPoint']")
        self.assertAlmostEqual(float(tp.get("x")), 76 + 20, places=1)   # apex, plus page padding

    def test_pattern_fill_becomes_hatch(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
               '<defs><pattern id="hp"><g fill="#ff4c4c"><rect width="2" height="2"/></g></pattern></defs>'
               '<path d="M 10 10 L 10 40 L 60 40 L 60 10 Z" fill="url(#hp)" stroke="#b30000"/></svg>')
        rect = vertices(to_dom(svg))[0]
        self.assertIn("fillStyle=hatch", rect.get("style"))
        self.assertIn("fillColor=#ff4c4c", rect.get("style"))


class GroupsAndText(unittest.TestCase):

    def test_group_transform_and_inheritance(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
               '<g transform="translate(100,0) scale(2)" fill="#fee" stroke="#f00">'
               '<rect x="5" y="5" width="10" height="10"/></g></svg>')
        rect = vertices(to_dom(svg))[0]
        g = rect.find("mxGeometry")
        self.assertAlmostEqual(float(g.get("x")), 110 + 20, places=1)
        self.assertAlmostEqual(float(g.get("width")), 20, places=1)
        self.assertIn("fillColor=#ffeeee", rect.get("style"))
        self.assertIn("strokeColor=#ff0000", rect.get("style"))

    def test_tspan_spacing_and_css_font(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 50">'
               '<style>text.f7 {font-family:cmbx12;font-size:12px}</style>'
               '<text class="f7" x="10" y="30">MEC<tspan x="40">host</tspan></text></svg>')
        cell = texts(to_dom(svg))[0]
        self.assertEqual(cell.get("value"), "MEC host")
        self.assertIn("fontFamily=Times New Roman", cell.get("style"))
        self.assertIn("fontStyle=1", cell.get("style"))      # cmbx → bold
        self.assertIn("fontSize=12", cell.get("style"))

    def test_multiline_text(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 60" font-size="10">'
               '<text x="10" y="20">one<tspan x="10" y="32">two</tspan></text></svg>')
        self.assertEqual(texts(to_dom(svg))[0].get("value"), "one<br>two")

    def test_label_html_escaping(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 20">'
               '<text x="5" y="15">a &lt;b&gt; &amp; c</text></svg>')
        self.assertEqual(texts(to_dom(svg))[0].get("value"), "a &lt;b&gt; &amp; c")

    def test_rotated_text(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" font-size="10">'
               '<text x="50" y="50" transform="rotate(-90 50 50)">up</text></svg>')
        self.assertIn("rotation=-90", texts(to_dom(svg))[0].get("style"))


class Fixtures(unittest.TestCase):
    """End-to-end on real files: a hand-written SVG in the documented profile
    and a TikZ figure exported with latex + dvisvgm."""

    def test_pipeline_example(self):
        dom = ET.fromstring(convert(ROOT / "examples" / "pipeline.svg"))
        values = {c.get("value") for c in texts(dom)}
        self.assertEqual(len(vertices(dom)) - len(texts(dom)), 4)   # 3 rects + 1 ellipse
        self.assertEqual(len(edges(dom)), 4)
        self.assertTrue(all("endArrow=classic" in e.get("style") for e in edges(dom)))
        self.assertIn("Tests pass?", values)
        self.assertIn("no: fix and rebuild", values)

    def test_tikz_dvisvgm_sample(self):
        dom = ET.fromstring(convert(FIXTURES / "tikz_sample.svg"))
        values = {c.get("value") for c in texts(dom)}
        self.assertEqual(values, {"Client", "Server", "Data store", "two-line<br>label",
                                  "request", "Sample flow", "10.0.0.1"})
        shapes = [c for c in vertices(dom) if c not in texts(dom)]
        self.assertEqual(len(shapes), 5)                   # 4 nodes + hatched rectangle
        self.assertTrue(any("fillStyle=hatch" in s.get("style") for s in shapes))
        self.assertTrue(any("rounded=1" in s.get("style") for s in shapes))
        arrows = [e for e in edges(dom) if "Arrow=classic" in e.get("style")]
        self.assertEqual(len(arrows), 3)
        self.assertTrue(any("dashed=1" in e.get("style") for e in edges(dom)))
        bent = [e for e in edges(dom) if e.findall("mxGeometry/Array/mxPoint")]
        self.assertEqual(len(bent), 1)                     # the |- connector, one waypoint
        mono = next(c for c in texts(dom) if c.get("value") == "10.0.0.1")
        self.assertIn("fontFamily=Courier New", mono.get("style"))


if __name__ == "__main__":
    unittest.main()

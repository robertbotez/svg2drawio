# Writing SVG that svg2drawio can convert

This file is for AI coding agents (Claude Code, Cursor, Codex, …) and for
people who generate diagrams programmatically. If you follow the profile
below, `svg2drawio.py` turns the SVG into a draw.io file in which every
box, connector and label is a separate, editable cell. If you don't, parts
of the drawing are skipped with a warning.

## The profile

Root element:

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 W H" width="W" height="H"
     font-family="Helvetica, Arial, sans-serif" font-size="14">
```

- `viewBox` starts at `0 0`; coordinates are pixels; keep the drawing
  inside roughly 1200 × 800.
- Put the default font on the root; override per `<text>` when needed.

Shapes — one element per visual object:

```xml
<rect x="40" y="100" width="140" height="56" rx="8"
      fill="#e8f1fb" stroke="#1f6feb" stroke-width="1.5"/>
<ellipse cx="530" cy="128" rx="70" ry="30" fill="#fff4d6" stroke="#d98c00"/>
<circle cx="100" cy="100" r="20" fill="#ffffff" stroke="#333333"/>
```

- Colours as `#rrggbb` (or `none`). No gradients, no `opacity`.
- `rx` on a `<rect>` gives rounded corners.
- `stroke-dasharray="6 4"` gives a dashed outline.

Connectors — straight or orthogonal, arrowheads via one shared marker:

```xml
<defs>
  <marker id="arrow" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
    <polygon points="0 0, 10 3.5, 0 7" fill="#333333"/>
  </marker>
</defs>
<line x1="180" y1="128" x2="250" y2="128" stroke="#333333" stroke-width="1.5" marker-end="url(#arrow)"/>
<polyline points="560,158 560,244 580,244" fill="none" stroke="#333333" stroke-width="1.5" marker-end="url(#arrow)"/>
```

- `<line>` for straight connectors, `<polyline>` (with `fill="none"`) for
  bends; each bend becomes a draw.io waypoint.
- `marker-end` makes the connector an arrow (`marker-start` is not read; put
  the arrow at the end, or draw the line in the other direction).
- Start and end connectors at the box border, not at its centre.
- Dashed connector: `stroke-dasharray="6 4"`.

Labels — plain `<text>`, one per label, never inside a shape element:

```xml
<text x="110" y="133" text-anchor="middle">Source</text>
<text x="320" y="125" text-anchor="middle" font-weight="bold">Build</text>
<text x="320" y="143" text-anchor="middle" font-size="11" fill="#555555">compile + lint</text>
```

- To centre a label in a box, use `text-anchor="middle"` at the box's
  centre `x` and set `y` to the centre plus about a third of the font size
  (SVG `y` is the baseline).
- Multi-line label: separate `<text>` elements on consecutive baselines
  (they are merged into one draw.io label when centred on the same `x`),
  or `<tspan x=".." y="..">` children with an explicit `y` per line.
- Escape `<`, `>` and `&` as `&lt;`, `&gt;`, `&amp;`.
- Rotated label: `transform="rotate(-90 x y)"` on the `<text>`.

Grouping is fine:

```xml
<g transform="translate(300,0)" fill="#ffffff" stroke="#333333"> … </g>
```

`transform` (translate / scale / rotate / matrix), `fill`, `stroke`,
`stroke-width`, `stroke-dasharray` and the `font-*` attributes are
inherited by the children.

## Do not use

- `<path>` for things that have a simpler element. Paths are supported
  (closed axis-aligned → rectangle, open → connector, small filled shape at
  a line end → arrowhead), but curves are flattened to their anchor points.
- `<use>`, `<symbol>`, `<image>`, `<foreignObject>`, nested `<svg>`,
  `<textPath>`, gradients, filters, masks, `opacity`.
- Text as outlines (every "convert text to path" export does this); the
  text must be real `<text>`.
- Shape-specific CSS in `<style>` beyond simple `.class { … }` rules.
- Diagrams drawn with a single `<path>` per node (icon fonts, hand-drawn
  styles).

## Workflow for an agent

1. Write the SVG following the profile (the example in
   `examples/pipeline.svg` is a good template).
2. Convert: `python3 svg2drawio.py figure.svg` — the run must print no
   `warning:` lines; a warning names the element that was skipped.
3. Check the geometry: `python3 preview_drawio.py figure.drawio` writes a
   PNG (needs matplotlib) — look at it, or open `figure.drawio` in draw.io.
4. Fix the SVG, not the `.drawio`: the `.drawio` is regenerated each time.

## TikZ / LaTeX figures

Do not go through PDF (text becomes outlines). Use
`python3 tikz2drawio.py figure.tex`, which compiles with `latex` and
`dvisvgm` so the text survives, then converts. TeX fonts are mapped to
Times New Roman / Courier New; the figure keeps its layout.

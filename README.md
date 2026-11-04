# svg2drawio

Turn an SVG diagram into a **native, editable draw.io file**.

The intended workflow: let an AI assistant (Claude, Cursor, ChatGPT, …) or a
script draft a diagram as SVG, run `svg2drawio`, then finish the figure by
hand in [draw.io / diagrams.net](https://app.diagrams.net) — move boxes,
re-route arrows, fix labels — instead of re-drawing it from scratch.
draw.io's own SVG import gives you one opaque image; this tool gives you
one editable cell per box, connector and label.

The SVG has to follow a small, explicit profile for this to work (plain
`rect` / `ellipse` / `line` / `polyline` / `text` elements, real text, no
outlines). [`AGENTS.md`](AGENTS.md) describes that profile so that an AI
agent can generate conforming SVG directly; `examples/pipeline.svg` is a
template. TikZ figures are supported through `latex` + `dvisvgm`.

## Install

Python 3.9+, standard library only.

```bash
git clone https://github.com/robertbotez/svg2drawio.git
cd svg2drawio
```

Optional: `matplotlib` for `preview_drawio.py`; `latex` and `dvisvgm`
(TeX Live / MacTeX) for `tikz2drawio.py`.

## Use

```bash
python3 svg2drawio.py diagram.svg                 # writes diagram.drawio
python3 svg2drawio.py diagram.svg -o out.drawio
python3 svg2drawio.py figures/*.svg               # one .drawio per input
python3 svg2drawio.py diagram.svg --scale 2       # enlarge 2x in draw.io
```

Open the `.drawio` in the draw.io desktop app or at app.diagrams.net
(File → Open From → Device).

Preview without draw.io (approximate rendering):

```bash
python3 preview_drawio.py diagram.drawio          # writes diagram.png
```

TikZ / `standalone` figures (one `.drawio` per page):

```bash
python3 tikz2drawio.py figure.tex
python3 tikz2drawio.py figures.tex -o out/ --names a b c --scale 2
```

Options: `--serif-font` / `--mono-font` choose the draw.io fonts used for
TeX families (defaults *Times New Roman* / *Courier New*);
`--no-merge-text` keeps every `<text>` element as its own cell.

## Example

`examples/pipeline.svg` (hand-written in the profile) converts to
`examples/pipeline.drawio`: 4 shapes, 4 arrows, 8 labels, all editable.

```bash
python3 svg2drawio.py examples/pipeline.svg
```

## What is converted

| SVG                                  | draw.io                                              |
|--------------------------------------|------------------------------------------------------|
| `<rect>` (with `rx`)                 | rectangle, rounded if `rx > 0`                       |
| `<ellipse>`, `<circle>`              | ellipse                                              |
| `<line>`, `<polyline>`               | edge; `marker-end` → `endArrow=classic`              |
| `<polygon>`                          | rectangle if axis-aligned, else closed polyline      |
| `<path>` closed, axis-aligned        | rectangle (`rounded=1` when the corners are curves)  |
| `<path>` open                        | edge with waypoints (rounded corners collapsed)      |
| `<path>` small, filled, at a line end| arrowhead on that line                               |
| `<path>` other closed                | closed polyline (fill dropped, warning)              |
| `<text>` + `<tspan>`                 | text cell; spaces from `tspan x`, lines from `tspan y` |
| `<g>`                                | transform / fill / stroke / font inheritance         |
| `<style>` `.class { … }`             | applied to elements with that class                  |
| `<pattern>` fill                     | draw.io sketch hatch fill                            |
| `<defs>`, `<clipPath>`, `<font>`     | ignored (embedded fonts are only used to measure text) |

Adjacent text runs on one baseline are merged into one label, and stacked
centred lines into one multi-line label.

### SVG from dvisvgm (TikZ)

`dvisvgm` keeps real text and names fonts after the TeX families (`cmr12`,
`cmbx12`, `cmtt8`, …). draw.io cannot render Computer Modern, so these are
mapped to the serif / monospace font of your choice, with bold / italic
derived from the family name. Word spacing is reconstructed from the glyph
metrics embedded in the SVG; ligatures and `\ldots` are normalised.

## Not supported

- `<use>` / `<symbol>` (glyph outlines written by pdf2svg, pdftocairo,
  Inkscape's PDF import): the shapes are converted, the text is lost. For
  TeX, export from DVI with `dvisvgm` (or use `tikz2drawio.py`).
- Curves are flattened to their anchor points; gradients, filters, masks,
  `opacity`, `<image>`, `<foreignObject>`, nested `<svg>`.
- `<marker>` definitions are not read; any `marker-end` becomes a classic
  arrowhead.
- Labels are separate cells, not attached to the shape or edge they sit on.

## Roadmap

The profile is deliberately narrow. The goal is to widen it step by step
(attached labels, curved edges, `<use>` for shapes, marker shapes) while
keeping the output clean: one draw.io cell per visual object, never a
cloud of micro-paths.

## Tests

```bash
python3 tests/test_basic.py
```

## License

MIT — see `LICENSE`.

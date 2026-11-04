# svg2drawio

Converter from SVG to native draw.io XML. To generate SVG that converts
cleanly, follow `AGENTS.md`.

- `svg2drawio.py` — the converter, standard library only (keep it so).
  Pipeline in `convert()`: parse CSS / embedded fonts / patterns → walk the
  tree with an inherited `Ctx` (transform matrix, fill, stroke, fonts) →
  classify each element into a rectangle, ellipse, edge, arrowhead or text
  cell → attach arrowheads to edges → merge split text runs → serialise.
- `tikz2drawio.py` — `latex` + `dvisvgm` front end for TikZ figures.
- `preview_drawio.py` — approximate PNG rendering of a `.drawio` (matplotlib).
- `tests/test_basic.py` — unit tests plus two end-to-end fixtures
  (`examples/pipeline.svg`, `tests/fixtures/tikz_sample.svg`).

Run tests with `python3 tests/test_basic.py`. Keep the output deterministic
except for the `modified=` timestamp; warnings go to stderr.

# Local dev harness (no Home Assistant)

Fetches DMI's live radar composite, decodes the HDF5 grid, reprojects and
colorizes it, and serves it to a plain browser page — for inspecting the
new DMI-based pipeline before it goes anywhere near Home Assistant.

## Setup

```
python -m venv .venv
```
Windows:
```
.venv\Scripts\pip install -r dev/requirements.txt
```
macOS/Linux:
```
.venv/bin/pip install -r dev/requirements.txt
```

## Run

Windows:
```
.venv\Scripts\python dev/server.py
```
macOS/Linux:
```
.venv/bin/python dev/server.py
```

Then open **http://localhost:8765/** in your browser.

## What you're looking at

- `dev/render.py` — the actual pipeline: decode ODIM_H5 (DMI's radar
  format), reproject from its native stereographic grid onto a plain
  lon/lat box via `pyproj`, colorize dBZ values, output PNG. This is the
  part that will get ported into the real HA integration later.
- `dev/nowcast.py` — the ~90 min forecast: a self-computed advection
  nowcast (piecewise per-tile FFT phase correlation, extrapolated forward
  in 10-min steps) instead of a DMI forecast product, which isn't exposed
  via Open Data. See [`CLAUDE.md`](../CLAUDE.md) for how this evolved and why.
- `dev/server.py` — a plain-stdlib local HTTP server. Polls DMI's
  `composite` collection (filtered to `scanType=fullRange` for consistent
  national coverage), caches rendered frames in `dev/cache/` (gitignored)
  so repeat requests don't re-download/re-render, builds the forecast tail
  via `nowcast.py`, and serves everything at `/api/frame/<id>.png`.
- `dev/index.html` — a minimal standalone timeline UI (play/scrub through
  ~2h observed + ~90min forecast, with the track split and
  Observeret/Seneste/Prognose labeling), with the same real Natural Earth
  coastline basemap as the shipped card (`dev/_gen_coastline.py` generates
  it; see the comment above `coastPolys` in either file).

Alignment was verified by overlaying known city coordinates on a rendered
frame and confirming they land in geographically correct positions relative
to the radar data (Skagen north of Aalborg, Aarhus/Odense/Esbjerg roughly
in a line, København to the east, etc.) — the stereographic reprojection
math is correct, not just visually plausible.

## Known gaps

- The forecast is a real approximation, not DMI's own (private,
  unavailable) nowcast product — accuracy degrades toward the end of the
  90-min window. See [`CLAUDE.md`](../CLAUDE.md) for how the motion
  estimation itself evolved.

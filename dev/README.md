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
- `dev/nowcast.py` — the ~90 min forecast: estimates a single global motion
  vector between the last two observed frames (FFT phase correlation) and
  extrapolates it forward in 10-min steps. See "Forecast: resolved" in
  `PLAN-DMI-MIGRATION.md` for why this exists instead of using a DMI
  forecast product directly.
- `dev/server.py` — a plain-stdlib local HTTP server. Polls DMI's
  `composite` collection (filtered to `scanType=fullRange` for consistent
  national coverage), caches rendered frames in `dev/cache/` (gitignored)
  so repeat requests don't re-download/re-render, builds the forecast tail
  via `nowcast.py`, and serves everything at `/api/frame/<id>.png`.
- `dev/index.html` — a minimal standalone timeline UI (play/scrub through
  ~2h observed + ~90min forecast, with the track split and
  Observeret/Seneste/Prognose labeling matching the original TV2-based
  card), with a rough hand-drawn coastline overlay purely as an alignment
  sanity check — **not** the final basemap (that's a separate step in
  `PLAN-DMI-MIGRATION.md`).

Alignment was verified by overlaying known city coordinates on a rendered
frame and confirming they land in geographically correct positions relative
to the radar data (Skagen north of Aalborg, Aarhus/Odense/Esbjerg roughly
in a line, København to the east, etc.) — the stereographic reprojection
math is correct, not just visually plausible.

## Known gaps (tracked in PLAN-DMI-MIGRATION.md)

- The forecast is a self-computed advection nowcast (single global motion
  vector, no growth/decay modeling) — a real approximation, not DMI's own
  (private, unavailable) nowcast product. Accuracy degrades toward the end
  of the 90-min window.
- The coastline in `index.html` is a rough approximation for alignment
  checking, not the real basemap.

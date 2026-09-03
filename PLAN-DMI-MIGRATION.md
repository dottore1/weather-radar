# Plan: Migrate from TV2's CDN to DMI Open Data (zero-risk data source)

Supersedes the TV2-based data source used by `radar.html` / `weather-radar-card.js`.
Goal: eliminate the copyright/ToS risk from hotlinking TV2's undocumented CDN
(`gfx.tv2a.dk`, `radar-cdn.weather.tv2api.dk`) by switching to DMI's official
Open Data program, which is explicitly licensed for this kind of reuse.

This plan only *replaces the data source and rendering pipeline*; the visual
design (map + colored radar overlay + scrub/play timeline) stays the same
goal. See `PLAN.md` for the (separate, already-implemented) HACS
packaging work — this plan changes what that packaging wraps.

## Researched facts (verified directly, not just from docs)

- **License**: DMI's Terms of Use (`dmi.dk/friedata/dokumentation/terms-of-use`)
  is a CC BY 4.0-equivalent: redistribution, modification, and commercial use
  are explicitly permitted, on the condition of attribution ("give appropriate
  credit, provide a link to the license, indicate if changes were made").
  **This means we now *must* credit DMI in the UI/README — the opposite of
  the earlier request to drop TV2 attribution, which was about not
  implicating TV2's undocumented CDN, not about avoiding attribution in
  general.**
- **Radar Data API**: `https://opendataapi.dmi.dk/v1/radardata`
  - `collections/composite/items` — STAC-style GeoJSON listing, combined
    national radar composite (what we want; also `pseudoCappi` and `volume`
    exist per-station, not needed here).
  - Each item has `datetime`, `created` (~8 min lag observed — better than
    TV2's ~15-25 min), `bbox` in WGS84, `scanType` (`fullRange` = 240km
    radius/full country coverage, `doppler` = 120km; items alternate, so we
    must filter to `scanType=fullRange` for consistent national coverage —
    still one every 10 min).
  - Actual file: `.../download/<id>.h5` — **HDF5 binary** (confirmed via
    direct download: 84KB, valid HDF5 header), not an image. This is the
    standard European radar exchange format (ODIM_H5) — a reflectivity grid
    plus georeferencing/projection metadata, not simple RGBA pixels.
  - No API key was required for anything tested (items listing, file
    download) — contradicts nothing in the docs, which don't mention a key
    either.
  - **CORS is blocked**: an `OPTIONS` preflight from an arbitrary origin gets
    `403 Invalid CORS request`, and the items/download endpoints do the same
    when an `Origin` header is present. DMI's API is evidently allow-listed
    for their own site, not open for arbitrary third-party browser calls.
    **This is the fact that forces an architecture change.**
- **Basemap**: TV2's basemap image (`gfx.tv2a.dk/weather/radar_map_medium.png`)
  is equally a TV2 copyright concern and needs replacing too — not just the
  radar overlay. Not covered by DMI's data at all.

## Architecture change this forces

Browsers can't `fetch()` DMI's API directly (CORS), and even if they could,
turning a raw HDF5 reflectivity grid into a correctly-projected, colored PNG
in-browser is a real geospatial processing job, not a decode-and-display one.
So the "pure static card that just points `<img>` tags at a live CDN" design
doesn't carry over. The new pipeline needs a server-side step:

```
DMI API (HDF5, server-to-server — no CORS issue)
   -> parse HDF5 (reflectivity grid + geolocation/projection)
   -> colorize (dBZ -> RGBA, our own legend, since we're not reusing TV2's)
   -> reproject/crop to match our basemap's extent
   -> render PNG
   -> serve same-origin to the browser (plain <img>, no CORS involved here)
```

- [x] **1. Local dev/test harness (what you asked to run and inspect first,
      no HA involved)** — `dev/render.py` + `dev/server.py` + `dev/index.html`,
      see `dev/README.md` for setup/run. Verified end-to-end (HDF5 decode →
      stereographic reprojection via `pyproj` → colorize → serve), including
      confirming geographic alignment by overlaying known city coordinates
      on a rendered frame.
  - A small local script/server (Python, using `h5py` + `numpy` + `Pillow`;
    all well-trodden for ODIM_H5) that:
    - Polls `collections/composite/items?scanType=fullRange` for the latest
      frames (mirrors the current anchor/history logic, just server-side).
    - Downloads and decodes each HDF5 file server-side (no CORS issue,
      since this isn't a browser request).
    - Colorizes and renders each frame to a PNG, matching our chosen basemap
      extent.
    - Serves those PNGs over plain HTTP on `localhost` (e.g. Python's
      built-in `http.server` or a two-route Flask app: `/frames/latest`,
      `/frames/<timestamp>.png`).
  - A `dev.html` page (plain HTML/JS, no HA/customElements dependency) that
    points `<img>` tags at `http://localhost:<port>/...` and reuses the
    existing timeline/scrub/play UI — run locally, open directly in your
    browser, no Home Assistant required. This is the same harness we'd use
    to visually verify colorization and alignment before it ever touches HA.

- [ ] **2. Basemap replacement**
  - Generate a static Denmark basemap once, offline, from public-domain
    data (Natural Earth — explicitly public domain, no attribution even
    required) at the same bbox DMI's composite reports
    (`[4.379, 52.294, 20.735, 59.828]`), so radar-grid alignment is exact
    instead of the eyeballed-pixel-match we had to do for TV2's PNGs.
  - Commit the generated static image/SVG to the repo — zero ongoing
    third-party dependency for the map layer at all, unlike before.

- [ ] **3. Colorization/legend design**
  - Pick our own dBZ -> color ramp (can't reuse TV2's exact palette; not
    that we'd want to — it's their branding). Standard meteorological radar
    palettes are well precedented (e.g. NWS-style blue->yellow->red, or a
    single-hue intensity ramp) — pick one and document the dBZ breakpoints.

- [ ] **4. Server-side rendering component for real HA deployment**
  - Once the dev harness proves the pipeline out, port it into a proper HA
    **custom_component** (Python integration — this is a HACS category
    change from the current "Plugin/Lovelace" packaging in `PLAN.md`, since
    the fetch+decode+render step must run somewhere with server-side
    network access, which a frontend-only card architecturally can't do).
  - The integration periodically renders frames and exposes them at an
    HA-registered same-origin URL (e.g. via a custom view, or writing to
    `www/` on a timer) that `weather-radar-card.js` then just points
    `<img>` tags at, same as it does today — the frontend card mostly stays
    as-is, it just points at our own backend instead of TV2's CDN.

- [ ] **5. Update packaging/docs**
  - `hacs.json`/`README.md` updated for an Integration-category install
    (or hybrid integration+card) instead of pure Plugin.
  - Add the required DMI attribution (license link + "if changes were
    made" note, since we do recolor/reproject the data) to the README and
    somewhere visible in the UI.

## Forecast: resolved

Investigated three options for the ~90 min forecast (matching what DMI's own
`friedata` docs describe: *"en fremskrivning af, hvor nedbøren forventes at
bevæge sig hen de kommende 90 minutter"*):

1. **DMI's own nowcast** (whatever powers their consumer radar page) — not
   exposed via the Open Data API (confirmed: the Radar Data API's
   `collections` list is only `composite`/`pseudoCappi`/`volume`, and the
   dataset's official ISO metadata record lists no image/forecast
   distribution format). Reverse-engineering it would put us right back in
   undocumented-private-API territory, just against DMI instead of TV2 —
   defeats the point of this migration.
2. **HARMONIE NWP precipitation forecast** (`total-precipitation` /
   `rain-precipitation-rate` parameters, confirmed present in the Forecast
   Data EDR API) — real DMI Open Data, but hourly-stepped only, and a test
   `cube` grid query over all of Denmark returned just 3×2 data points
   (~130km+ spacing) — far too coarse to render as a map. Would need more
   exploratory work against a sparsely-documented, rate-limited endpoint to
   determine if/how a usable-resolution grid is obtainable.
3. **Self-computed advection nowcast (chosen)** — estimate a single global
   motion vector between the last two observed composite frames via FFT
   phase correlation, then extrapolate that motion forward in 10-min steps.
   Standard "Lagrangian persistence" nowcasting technique, exactly what
   short-lead-time forecasting is normally done with anyway (more accurate
   than NWP at 0-2h lead times) — and circumstantial evidence suggests this
   is close to what DMI/TV2's own nowcast actually is: TV2's forecast
   images match the observed ones' resolution/10-min cadence exactly, which
   is the signature of echo extrapolation, not a coarse NWP model like
   HARMONIE.

- [x] **Implemented**: `dev/nowcast.py` (motion estimation + extrapolation,
  sign convention verified via a synthetic-shift self-test) and wired into
  `dev/server.py` (`build_forecast()`, `/api/frames` now returns observed +
  9 forecast entries spanning 90 min, `/api/frame/forecast-<ts>.png`).
  `dev/index.html` shows the observed/forecast split on the timeline track
  (matching the original TV2-card design) with distinct "Observeret /
  Seneste / Prognose" labeling. Verified end-to-end against live data —
  motion estimate on a real frame pair was physically plausible (~9.75
  km/10min ≈ 58 km/h eastward, consistent with typical frontal-system
  speeds), and forecast frames render correctly through the full HTTP path.
- Known limitation, inherent to the technique: single global motion vector
  (no per-cell/optical-flow motion field, no growth/decay modeling) — fine
  for a first pass, degrades toward the end of the 90-min window, and
  won't match DMI/TV2's own (unknown, private) nowcast algorithm exactly.

## Suggested order of work

Start with step 1 (local harness) — it's the part you explicitly want to
poke at yourself in a browser, it proves out the HDF5-parsing/colorizing/
alignment logic in isolation, and steps 2-5 build directly on it without
needing HA at any point until step 4.

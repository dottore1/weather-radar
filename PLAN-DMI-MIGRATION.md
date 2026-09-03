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

- [x] **2. Basemap replacement**
  - Done via `dev/index.html`'s `coastPolys` — real coastline (Denmark plus
    Sweden/Germany slivers for context) from Natural Earth's public-domain
    10m admin-0-countries dataset, clipped to our display bbox and
    simplified with shapely (`dev/_gen_coastline.py`, a one-off generation
    script kept in the repo for reproducibility — not a runtime dependency;
    the resulting coordinates are baked directly into `index.html`).
  - Hit a real data-quality trap worth remembering: the first source tried
    (`ne_10m_land.geojson` on the same GitHub mirror) turned out to be an
    oddly pre-merged ~11-feature version that fused Scandinavia into
    mainland Europe with no sea gap at all — LOOKED like a bug (a giant
    blob swallowing the whole Kattegat/Baltic) but was actually bad input
    data. Confirmed the *real* per-country dataset
    (`ne_10m_admin_0_countries.geojson`) has correct topology by checking
    `Denmark.distance(Sweden)` directly with shapely (~5.7km, no overlap)
    rather than trusting a low-contrast preview render alone — the dark
    fill/outline/background palette made a real, correctly-separated gap
    visually indistinguishable from a bug in a quick monochrome check.
  - Still using the render.py/nowcast.py-generated composite bbox
    (`[7.0, 16.0, 54.3, 58.2]`), not DMI's full composite bbox — fine,
    since coastline and radar frames already share that same box by
    construction (`OUT_LON_MIN`/etc.), so alignment is exact by
    definition, not eyeballed.

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
- ~~Known limitation, inherent to the technique: single global motion vector~~
  **Superseded** — see "Piecewise motion field" below. Still true regardless
  of motion-estimation approach: no growth/decay modeling, degrades toward
  the end of the 90-min window, won't match DMI/TV2's own (unknown, private)
  nowcast algorithm exactly.

### Piecewise (tile-based) motion field

The single-global-vector nowcast above visibly looked like "the whole sky
slides in one direction" — because that's literally what it did. Real
weather doesn't move as one rigid sheet (a front over Jylland can move
differently than a cell over Sjælland). Replaced with a spatially-varying
motion field:

- `estimate_motion_field()` (`dev/nowcast.py`) runs the same per-tile FFT
  phase correlation independently across overlapping 180px tiles (60px
  overlap), then interpolates the sparse per-tile vectors into a smooth
  per-pixel field via bilinear upsampling (PIL, no new dependency).
- `warp_by_field()` does semi-Lagrangian backward sampling — each output
  pixel looks up its *own local* velocity and pulls content from where it
  came from, instead of one rigid shift for the whole frame.
- Verified via a synthetic self-test with two cells moving in *opposite*
  directions (something a global vector fundamentally cannot represent — it
  collapsed to picking up only one cell's motion and got the other
  completely wrong); the field-based estimate recovered both correctly.
- **Needed real tuning to be usable**, not just "more sophisticated in
  theory": individual small tiles are noisier than a full-frame correlation
  and can lock onto spurious/aliased peaks, which extrapolation over 9 steps
  amplified into visible streak artifacts on real data. Fixed with (1) a
  magnitude clamp on implausible single-tile displacements (~120 km/h
  ceiling) and (2) a NaN-aware neighborhood median filter over the tile
  grid — applied *before* filling gaps with the global-vector fallback,
  since smoothing a real measurement against neighboring fallback
  placeholders (rather than other real measurements) was actively wrong and
  broke the opposing-cells self-test the first time around. A faint minor
  streak artifact still remains in one small area on real data — better,
  not perfect; a candidate for further tuning (larger filter neighborhood,
  or per-tile confidence weighting) if it's still noticeable in practice.
- Cost: field estimation + warping adds real time to the (still
  background-refreshed, per PLAN-PERFORMANCE.md) forecast computation —
  measured ~1.1s added on top of the ~0.35s global-vector version on real
  data. Doesn't affect warm `/api/frames` latency, only how long the
  background refresh takes to complete after new DMI data appears.
- Other options considered but not implemented (still available if this
  needs to go further): dense optical flow (e.g. OpenCV Farneback — a real
  per-pixel field, better at rotation/shear, heavier dependency), pySTEPS
  (a proper nowcasting library with growth/decay modeling and ensemble
  uncertainty — closest to "how the field actually does it", biggest lift).
- **Follow-up bug: forecast looked like the sky "expanding" rather than
  moving.** Cause: measured tiles and fallback (no-signal) tiles could sit
  right next to each other with very different vectors — a real tile inside
  a storm's own independent measurement next to a neighboring tile that had
  no signal and jumped straight to the (possibly quite different) global
  fallback. Warping by a spatially *inconsistent* field doesn't translate a
  rain area as one piece; region boundaries where the field disagrees with
  itself stretch instead. Fixed by blending every measured tile *toward*
  the global vector (`TILE_BLEND_ALPHA = 0.6`) rather than trusting it
  outright, so the whole field shares one common drift with only a bounded
  local deviation layered on top — real per-region character survives
  (confirmed via a re-run of the opposing-cells self-test, now checking
  that the two regions land on either side of the global vector rather than
  hitting their exact true values, which blending intentionally trades
  away), but the system visibly moves as a whole again. Verified against
  real data: now/+30min/+90min frames show the pattern clearly relocating
  across frames, not just diffusing in place.
- **Follow-up: large central masses still looked like they were "expanding"
  while smaller edge features moved correctly.** More precise diagnosis
  than the first blend fix addressed: verified empirically (a per-tile
  peak-to-mean correlation-confidence scan against real data) that tiles
  deep inside a large, heavily-raining mass genuinely produce *weaker*
  correlation peaks than small tiles right at a sharp rain/no-rain edge —
  a large, broad, locally self-similar interior gives phase correlation
  multiple almost-equally-plausible shifts to choose from, while a
  distinctive edge locks on confidently. The fixed 60/40 blend didn't
  distinguish a confidently-measured tile from a noisy one. Confound found
  in the same scan: a tile with only a tiny sliver of rain can score an
  artificially *high* confidence purely because it has almost no content
  to disagree with itself about — so confidence alone isn't a safe signal.
  Fixed with two multiplied trust factors per tile — `compute_motion`'s
  peak-to-mean ratio (`TILE_CONF_LOW`/`HIGH` = 7-25, calibrated against the
  real-data scan) and how much valid data the tile actually had
  (`TILE_DATA_SATURATE_PX` = 3000) — so a tile only gets close to full
  `TILE_BLEND_ALPHA` trust when it's *both* confidently- and
  substantially-measured; a big ambiguous interior tile or a barely-there
  sliver both get pulled back toward the shared global drift instead.
  Verified against real data: the large mass now shows directional
  streaking/stretching consistent with the rest of the field's motion,
  not uniform growth in place.
- **Follow-up: "expanding" came back on a later real-data check.** Not a
  regression — the confidence-weighting code was untouched and still
  correct — but a different, previously-unaddressed mechanism. Diagnosed
  directly: the global (whole-frame) vector was exactly `(0, 0)` for that
  moment's real data (a legitimate outcome — no strong overall
  precipitation drift right then, not a bug), yet individual tiles were
  still earning full trust (confidence scores 30-59, comfortably above
  `TILE_CONF_LOW`) with quite different raw vectors — e.g. `(-20,22)`,
  `(-13,0)`, `(-8,28)`, `(7,42)`. `TILE_MAX_DISPLACEMENT_PX` only rejects a
  tile whose *absolute* motion is implausible; it does nothing when several
  tiles are each individually plausible but disagree with each other and
  with the (near-zero) shared drift. Confidently-measured-but-mutually-
  divergent tiles with nothing to anchor them together is exactly what
  reads as "expanding" rather than translating.
  - Fixed by adding `TILE_MAX_DEVIATION_PX = 15`: caps how far *any* tile's
    raw vector is allowed to differ from the global vector, applied before
    the trust-weighted blend and regardless of that tile's own trust score.
    Real per-region variation stays visible but bounded, instead of a
    handful of confident tiles running off in unrelated directions with no
    larger context to validate them.
  - This directly conflicts with the earlier opposing-cells self-test's
    exact-recovery assertions (deviation of 24px there, above the new
    15px cap) — that's an intentional trade-off, not a test regression: the
    test now passes `max_deviation_px=999` for the `blend_alpha=1.0` case
    specifically to keep verifying the underlying per-tile *mechanism*
    still works, while the default-parameter case verifies the new,
    intentionally more conservative real behavior instead.
  - Verified against the exact real data that exposed the issue: re-ran
    the same diagnostic after the fix (dx field range narrowed from
    `[-3.5, 15.5]` to `[-3.5, 9.0]`) and visually confirmed the forecast
    sequence now shows subtle, bounded local evolution instead of
    sprawling outward. Given the global vector genuinely was zero, a
    forecast that looks mostly stable is the *honest* answer here —
    inventing dramatic movement the data doesn't support would be less
    accurate, not more, even though it looks less dynamic.
- **Bug found and fixed: the last forecast frame jumped noticeably more
  than the smooth progression of the rest.** Cause: forecast PNGs were
  cached to disk keyed only by absolute future timestamp
  (`forecast-YYYYMMDDHHmm.png`), with `if not cache_path.exists(): write`
  — appropriate for observed frames (genuinely immutable once published)
  but wrong for forecast frames. A given absolute future timestamp gets
  computed as a *different* step number (different accumulated
  displacement, from a different/fresher motion field) across successive
  refreshes as `curr` advances — e.g. today's +90min step is tomorrow's
  +80min step once `curr` catches up 10 min. Skip-if-exists meant whichever
  refresh computed a given timestamp *first* won permanently; it silently
  reused that older, larger-displacement render forever after, sitting as
  a discontinuity at the end of an otherwise-fresh sequence. Fixed by
  always overwriting in `_compute_forecast_sync` — safe because that
  function only runs when the higher-level `(baseline_id, curr_id)`-keyed
  cache in `get_forecast_entries` has already decided a recompute is
  warranted, so every step is authoritative at that point. Verified the
  fix directly (not just by inspection): called the compute function twice
  in a row and confirmed the same output file's mtime actually advances on
  the second call, where before the fix it wouldn't have.
- **Bug found and fixed** (frames weren't moving at all): a naive whole-field
  FFT phase correlation on two real consecutive composites returned exactly
  `(0, 0)` even though the frames genuinely differed. Root cause: radar
  composites have a fixed coverage-boundary edge (data → no-data) that's
  geographically static between frames — a hard edge is a very strong,
  perfectly-stationary broadband signal that dominates phase correlation and
  pulls the estimate toward zero shift regardless of real (smaller-amplitude)
  rain motion. Fixed by (1) tapering the field with a 2D Hann window before
  the FFT (standard fix for edge artifacts in phase correlation) and (2)
  cropping to the active echo region first. Also switched the motion
  baseline from the immediately-preceding 10-min frame pair to a 20-min gap
  (`items[-3]` → `items[-1]`, halved) — a short baseline is noise-dominated
  and prone to reading as ~0 even with the edge-artifact fix in place.
  Verified against real data: forecast frames now visibly/numerically differ
  from each other (confirmed via pixel diff and side-by-side inspection).
- **Second bug found and fixed** (forecast frames looked "too small"/shrinking
  toward +90min): `shift_grid` correctly leaves revealed edge pixels
  transparent (no known data moved in from outside the frame) — but at
  large accumulated shifts near the end of the 90-min window, that reveals
  a real, growing margin, which read as the image shrinking against the
  page's dark background. Fixed by reprojecting onto a larger *padded*
  working box for the forecast pipeline only (`render.py`'s
  `dbz_grid_for_work`/`crop_to_display`, ~150-190km margin) so a shift
  reveals real DMI data pulled from the padding instead of blank space,
  then cropping back down to the standard display size before output —
  observed and forecast PNGs stay pixel-aligned. Verified: the +90min frame
  now has full edge-to-edge coverage, no blank margin.

## Suggested order of work

Start with step 1 (local harness) — it's the part you explicitly want to
poke at yourself in a browser, it proves out the HDF5-parsing/colorizing/
alignment logic in isolation, and steps 2-5 build directly on it without
needing HA at any point until step 4.

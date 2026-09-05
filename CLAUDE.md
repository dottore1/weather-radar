# DMI Vejrradar — project notes

A Home Assistant integration + Lovelace card showing Danish precipitation
radar: ~2h of observed history plus a ~90min self-computed forecast, on a
real Denmark basemap, rendered entirely on the user's own HA instance.

This file consolidates the intent, findings, and major decisions that used
to live in `PLAN-DMI-MIGRATION.md` / `PLAN-HA-COMPONENT.md` /
`PLAN-PERFORMANCE.md` / `PLAN.md` (now removed — they were execution logs
for work that's since landed; git history has the blow-by-blow if it's ever
needed). Read this instead of expecting those files to exist.

## Why this shape

- **Original design was a pure Lovelace card hotlinking TV2's CDN** directly
  from the browser. Abandoned for two reasons: TV2's CDN is undocumented and
  a copyright/ToS risk, and once the data source moved to DMI's official
  Open Data (CC BY 4.0, requires attribution — see README), DMI's API blocks
  CORS for arbitrary origins and the raw format is HDF5 (ODIM_H5), not an
  image. Turning that into a colored, correctly-projected PNG is real
  geospatial processing, not a decode-and-display job — a browser-only card
  architecturally can't do it. That's why this is an **integration**
  (`custom_components/weather_radar_dmi/`) that runs its own pipeline, with
  a **card** as a thin client, not a card alone.
- **The integration runs the full pipeline inside the user's own HA
  instance** — install via HACS, add the integration, add the card, nothing
  else to host. Not a card pointed at an externally-hosted server.
- **`dev/` is a permanent local iteration harness**, not a relic — a plain
  stdlib HTTP server with no Home Assistant involved, used to build and
  tune `render.py`/`nowcast.py` against real DMI data before touching the
  shipped integration. `dev/render.py` and `dev/nowcast.py` are kept
  framework-free specifically so they can be ported near-verbatim into
  `custom_components/weather_radar_dmi/`.
  - **These two pairs of files must be kept in sync**:
    `dev/render.py` ↔ `custom_components/weather_radar_dmi/render.py`,
    `dev/nowcast.py` ↔ `custom_components/weather_radar_dmi/nowcast.py`.
    `tests/` runs the same assertions against both copies (a parametrized
    `pipeline_impl` fixture) specifically so drift between them fails a
    named test instead of going unnoticed. When you change one, change the
    other the same way in the same commit.
  - Everything else under `custom_components/weather_radar_dmi/`
    (`coordinator.py`, `http.py`, `config_flow.py`, `image.py`, `__init__.py`,
    `www/weather-radar-dmi-card.js`) is HA-specific and has no `dev/`
    equivalent — `dev/server.py` reimplements just enough of the same ideas
    (caching, forecast scheduling) to run standalone.

## The nowcast: current shape and why

The forecast is a **self-computed advection nowcast** (DMI's own nowcast
algorithm isn't exposed via Open Data, and a NWP-based alternative
(HARMONIE precipitation fields) was tested and found far too coarse-grained
to render usefully). This went through several real iterations before
landing on the current design — each one fixed a genuine failure mode found
against real data, not a hypothetical one:

1. **Single global FFT phase-correlation vector**, extrapolated forward —
   the simplest version. Visibly wrong on real weather: the whole sky slides
   as one rigid sheet, which real weather doesn't do (a front over Jylland
   can move differently than a cell over Sjælland).
2. **Piecewise tile-based motion field** (`estimate_motion_field`): the same
   phase correlation run independently per overlapping tile, bilinearly
   upsampled into a dense per-pixel field, applied via semi-Lagrangian
   warping (`warp_by_field`) instead of one rigid shift. Needed real tuning
   to be usable, not just "more sophisticated in theory" — individual tiles
   are noisy and prone to locking onto spurious/aliased peaks. Confidence
   weighting (`TILE_CONF_LOW/HIGH`, `TILE_DATA_SATURATE_PX`) distinguishes a
   sharp, data-rich tile from a broad, ambiguous, or barely-measured one.
3. **The anchor tiles blend toward** used to be a single whole-frame FFT
   correlation. Found live (2026-09-04) to be a real design flaw: on a day
   with broad, near-domain-filling precipitation, whole-frame correlation
   goes genuinely ambiguous (weak internal texture, content entering/exiting
   at the domain edges) and reads near-zero even when individual tiles
   *do* measure real, mutually-consistent motion — this is what produced
   "the forecast looks frozen / shrinks and expands in place instead of
   moving." Fixed by deriving the anchor from the tiles' own measurements
   instead: a **geometric median** (`_geometric_median`, Weiszfeld's
   algorithm) of the trustworthy tiles — not two independent per-axis
   medians, which was tried first and is a real bug in its own right: tiles
   agree on speed more often than direction, and per-axis medians let
   positive/negative components cancel, collapsing the resultant magnitude
   even when every tile measured fast real motion (measured live: a 7px
   per-axis-median anchor against a 23px true per-tile median magnitude).
4. **Isolated single-tile aliasing** (a lone tile reading e.g. 59px in an
   unrelated direction among neighbors otherwise agreeing on ~25px) was
   still polluting the anchor. Fixed by running the existing NaN-aware
   neighborhood median filter (`_median_filter_grid`) on the raw tile grid
   *before* computing the anchor, not just after — smooths out spatially
   isolated outliers without erasing genuine per-region variation (tiles
   still deviate from the anchor up to `_tile_max_deviation`, which scales
   with the anchor's own magnitude via `TILE_DEVIATION_FLOOR/FRACTION/
   CEILING_PX`).
5. **Even a fixed pair's tiles can be genuinely multi-modal** on a complex
   weather day (verified live: several comparably-confident tile clusters
   pointing in substantially different real directions at once, not noise
   around one true value) — no aggregation of *that one pair alone* can
   invent a coherent single answer the data doesn't have. Fixed by
   `estimate_consensus_anchor`: pool tile measurements across **every
   consecutive pair in the full ~13-frame observed history** (~2h at DMI's
   ~10-min cadence), not just the current baseline/curr pair — a real,
   persistent drift reinforces across many independent measurements while
   any single pair's disagreement gets diluted. **Recency-weighted**
   (`CONSENSUS_RECENCY_HALF_LIFE_STEPS`, currently ~20min half-life):
   without this, a system that's accelerating gets dragged down by older,
   slower pairs right at the observed-to-forecast transition — reported
   live as the forecast visibly starting slower than the observed frames
   even after the consensus fix landed.
6. The tile-level cohesion knobs (`TILE_BLEND_ALPHA`, the
   `TILE_DEVIATION_*` constants) were tuned down once live feedback said
   tiles were moving too independently of the shared drift. If tuning
   these again: validate against real, current DMI data (a small script
   fetching the live `composite` items and calling `estimate_motion_field`/
   `estimate_consensus_anchor` directly — see "Release workflow" below),
   not just unit tests — the unit tests use synthetic, deliberately simple
   scenarios and won't catch a regression in real-world cohesion or speed.

`MOTION_BASELINE_STEPS` (`const.py`) is the baseline gap the *spatial field
itself* is measured over (20min, i.e. 2 steps) — independent of the
consensus anchor's own pooling window, which always uses the full available
history regardless of this constant.

## Rendering decisions

- **Color ramp** (`render.py`'s `COLOR_STOPS`): real DMI composites include
  valid-but-tiny/negative dBZ (observed down to -31.5 dBZ) from radar noise
  floor / clear-air return, not real precipitation. The ramp has a **hard
  cutoff at 20 dBZ** (not a gradual fade from 0) — anything below is fully
  transparent regardless of its `valid` flag. This value was chosen by A/B
  testing several thresholds rendered from the *identical* real composite,
  compared against a DMI reference screenshot of the same timestamp; 20 dBZ
  reproduced DMI's actual dry/wet contrast. Don't lower this without
  re-verifying against a live DMI comparison — a lower threshold reintroduces
  noise-floor haze that reads as "raining everywhere."
- **Coastline** (`www/weather-radar-dmi-card.js`'s `coastPolys`): real
  Natural Earth 10m admin-0-countries data, clipped and simplified — not a
  placeholder. Must share the exact same lon/lat bbox as `render.py`'s
  `OUT_LON_MIN/MAX`/etc. for alignment.
- **Forecast padding**: the forecast pipeline reprojects onto a *padded*
  working box (`WORK_*` in `render.py`) before warping, then crops back to
  the display box. Without the padding, large accumulated shifts near the
  end of the 90-min window would reveal blank (transparent) margin instead
  of real DMI data — this was a real, fixed bug ("forecast frames shrink
  toward +90min").
- **The map/card chrome is theme-aware**: background, coastline, city
  dots/labels, and the timestamp stamp all bind to HA's CSS custom
  properties (`--card-background-color`, `--secondary-background-color`,
  `--primary-text-color`, `--secondary-text-color`) instead of a fixed dark
  palette — these variables cross the shadow DOM boundary automatically
  (standard CSS custom property inheritance), so no JS theme-change
  listener is needed. The **home-location marker is the deliberate
  exception**: fixed red/white regardless of theme, since its entire job is
  to always stand out. The colorized radar PNGs themselves are also
  deliberately fixed — a data-encoding legend, not UI chrome.
- **The default displayed frame is whichever frame's own timestamp is
  closest to real wall-clock time** (`_closestToNowIdx()`), not the latest
  *observed* frame (`_nowIdx`, which is the real data-provenance boundary
  used for "Nu" mark placement and Seneste/Prognose/Observeret labeling).
  DMI's processing lag (typically 15-25 min) means these two differ in
  practice — showing the latest observed frame by default was showing
  stale data when a closer-to-now forecast frame was available.

## Performance: dev harness vs. the real integration

`dev/server.py` serves concurrent HTTP requests on demand, so it needed
real request-time caching to stay responsive: skip forecast recomputation
when the `(baseline_id, curr_id)` pair hasn't changed, decode each HDF5
file once and reproject to whichever output size(s) are actually needed
(not once per endpoint), make forecast computation non-blocking for the
frame list (a background thread — see `get_forecast_entries`'s docstring),
HTTP caching headers on immutable observed-frame PNGs, diff `<img>`
elements client-side instead of rebuilding them every poll, prioritize the
initially-visible frame's fetch. This work dropped warm `/api/frames` from
~1.35s to ~0.1s.

**`coordinator.py` doesn't face the same problem by construction** — it's a
`DataUpdateCoordinator` that pre-renders on its own poll schedule
independent of whether any dashboard is open, so the HTTP views just serve
already-rendered bytes; there's no request-time computation to cache in the
first place. Its own caching (`_decoded_cache`, PNG cache pruning) solves a
different problem: avoiding redundant work within one poll cycle, and
preventing unbounded disk growth (found live: unpruned forecast/observed
PNGs grew at a rate that projected to ~35GB/year before
`_prune_png_cache`/`prune_png_cache` was added) — not request-time latency.

**Resource usage**, measured against real DMI data: memory plateaus around
~370-515MB during active computation (budget ~400-500MB of headroom on
constrained hardware, e.g. a Raspberry Pi-class HA install); CPU is ~3-4s
of compute per poll when new data actually arrived (~2-3% of one core on
average). Two follow-up changes target *idle-time* footprint specifically:
float32/complex64 instead of float64/complex128 in the FFT motion
estimation (peak-finding on a correlation surface isn't precision-sensitive,
and this was the single biggest transient allocation — ~93% reduction in
that stage's own memory delta), and `_trim_memory()` (see "Known platform
gotchas" below). Combined, measured idle RSS between polls dropped from
~325MB (pinned at peak, never released back to the OS) to ~78MB.

## Card registration: one path only

The card is registered as a Lovelace resource via
`hass.data["lovelace"].resources` (`__init__.py`) — the same mechanism HACS
itself uses for user-installed cards. **Do not also register it via
`add_extra_js_url`.** That was tried (on the theory that "ES modules dedupe
by URL, so registering both ways is a harmless hedge") and caused a real,
hard-to-diagnose bug: racing a `<script type=module src=URL>` tag against a
dynamic `import(URL)` of the identical URL can hit a genuine browser-level
collision (`customElements.define` throwing "already been used with this
registry" for the loser), and under the wrong interleaving on a cold fetch,
*neither* loader ends up defining the tag — a permanent "Configuration
error" that no amount of waiting fixes. Confirmed live by deliberately
reproducing the exact collision. One registration path only.

The card's static file is served with `cache_headers=True`
(`_async_register_static_path`), which is HA's *cache-forever* mode
(31-day max-age) intended for content-hashed, truly-immutable filenames
like HA's own `frontend_latest/*.js` chunks. Ours isn't content-hashed —
same filename every release — so the URL is cache-busted with
`?v=<manifest.json version>` (`_card_resource_url`), and the Lovelace
resource entry is **updated in place** on a version bump (matched by path,
ignoring the query string) rather than only checked for an exact-URL match
— the latter would silently never find the previous entry and pile up
stale duplicate resources across releases.

## Card config

`getConfigForm()` (even with an empty schema) is what flips HA's edit
dialog from "doesn't support the visual editor — YAML only" to a real
Configuration tab; `getGridOptions()` (an instance method, unlike the
static `getConfigForm`) is the separate mechanism that makes the card
drag-resizable in **sections-view** dashboards specifically — without it
HA defaults to full-width with no resize affordance. `getCardSize()` stays
as the unrelated masonry-view column-balancing hint.

Boolean options (`autoplay`, `show_home_marker`) are read as
`this._config.<key> !== false` — **not** `=== true` — so an existing config
that predates the option (the key is simply absent) reproduces prior
behavior (on) rather than silently flipping to off. Keep this pattern for
any future boolean option.

## Known platform gotchas

- **musl vs glibc**: `coordinator.py`'s `_trim_memory()` calls
  `ctypes.CDLL("libc.so.6").malloc_trim(0)` to return freed heap arenas to
  the OS between polls (glibc holds them otherwise, keeping RSS pinned at
  its peak for the ~2min idle gap between polls — this dropped measured
  idle-time RSS from ~325MB to ~78MB on real data). On musl-based libc
  (HAOS's container), the symbol doesn't exist and raises `AttributeError`
  (not `OSError`) — both are caught and it's a deliberate no-op there, not
  a workaround-in-progress: musl's allocator (`mallocng`) routes larger
  allocations through `mmap`, which the kernel reclaims immediately on
  free, so `gc.collect()` alone already gets the full benefit on musl.
  Verified directly (not assumed) with a repeated alloc/free test in an
  Alpine container.
- **Windows test environment**: `pytest-homeassistant-custom-component`
  pulls in `homeassistant`, which pins `lru-dict==1.3.0` — no prebuilt
  wheel for Python 3.13 on any platform, needs a C compiler. Resolved via
  WSL2 + `uv` with a version override (`tests/uv-overrides.txt`) instead of
  installing MSVC Build Tools — see `tests/README.md` for the exact
  commands. **Always run the test suite through the WSL venv
  (`.venv-test-wsl`)**, not a native Windows Python.
- **No `gh` CLI in this environment.** Creating a git tag is not enough for
  HACS to see a new version — HACS tracks actual **GitHub Releases**, not
  bare tags, and falls back to tracking the branch by commit SHA (showing a
  short hash instead of a version) if no Release exists. After pushing a
  tag, the user needs to manually create the Release from it on GitHub
  (Releases → Draft a new release → pick the tag → publish, not marked as
  prerelease). This was missed for several releases in a row before being
  diagnosed — don't assume a pushed tag alone is sufficient.
- Once, on Microsoft Edge specifically, `customElements.get(...)` stayed
  `undefined` even though the script fetched correctly and `import()`
  resolved without error — switching to Chrome fixed it immediately. Never
  root-caused; noted in case it recurs.

## Release workflow

1. Bump `version` in `custom_components/weather_radar_dmi/manifest.json`.
2. Run the full test suite via the WSL venv.
3. For anything touching `nowcast.py` or the card's visual behavior,
   validate against **real, current DMI data** before shipping — either a
   throwaway script under the scratchpad directory that fetches live
   `composite` items and calls the pipeline functions directly, or by
   patching the change into the *already-loaded* card in a live browser tab
   (`customElements.get('weather-radar-dmi-card')`, reachable through
   nested shadow roots) to preview before a real release. Synthetic unit
   tests alone have repeatedly missed real-world regressions in this
   codebase (motion cohesion/speed, timestamp staleness, etc.).
4. Commit, tag (`vX.Y.Z`, annotated), push both.
5. Give the user paste-ready release notes, and remind them to manually
   create the GitHub Release from the tag (see gotcha above) and to fully
   **restart** Home Assistant after updating via HACS, not just reload —
   some fixes (e.g. the `add_extra_js_url` removal) only take effect after
   a real restart because HA keeps some registrations in memory for the
   life of the process.

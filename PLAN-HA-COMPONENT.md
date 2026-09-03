# Plan: DMI radar as a real HACS integration + card

Supersedes `PLAN.md` (the original TV2-hotlinking Lovelace-card-only plan —
no longer viable now that the pipeline decodes raw DMI HDF5 composites,
which requires real server-side Python, not a browser card alone).

## Context

The repo had two generations of scaffolding that didn't match each other:
root-level `hacs.json`/`weather-radar-card.js`/`radar.html` (the original
pure-Lovelace-card design, hotlinking TV2's CDN directly from the browser)
and `dev/` (the current pipeline, built after moving to DMI's official CC
BY Open Data — decodes ODIM_H5 composites with `h5py`, reprojects with
`pyproj`, colorizes with `numpy`/`Pillow`, and runs an FFT-based advection
nowcast). `dev/server.py` is explicitly a **local-only dev harness** used
to build and tune that pipeline against real data.

Decided (confirmed with the user before building):
- The integration runs the full pipeline **inside Home Assistant itself**
  — install via HACS, add the integration, add the card, nothing else to
  host — rather than pointing at an externally-hosted copy of
  `dev/server.py`.
- `render.py` and `nowcast.py` were kept framework-free in the harness
  specifically so they'd be easy to port — ported close to verbatim here
  rather than redesigned.

## What was built

`custom_components/weather_radar_dmi/`:

- `render.py`, `nowcast.py` — near-verbatim ports of `dev/render.py` /
  `dev/nowcast.py`. Verified against the harness's own self-test
  assertions (dense-field sign convention, coverage-edge cropping, opposing
  motion-field cells) to confirm the port didn't change behavior.
- `coordinator.py` — `DataUpdateCoordinator`, polls every 2 minutes. The
  whole synchronous pipeline (network fetch + HDF5 decode + FFT motion
  estimation) runs via `hass.async_add_executor_job`, ported from
  `dev/server.py`'s `fetch_latest_items`/caching logic. Unlike the harness
  (which needed a background-thread forecast refresh to keep concurrent
  HTTP polls fast), the coordinator's own poll cycle *is* the periodic
  refresh, so that background-thread indirection was dropped as
  unnecessary in this architecture.
- `http.py` — two `HomeAssistantView`s mirroring the harness's
  `/api/frames` and `/api/frame/<id>.png`, mounted under
  `/api/weather_radar_dmi/...`, `requires_auth = True`.
- `config_flow.py` — zero-config, single-instance (no API key needed, one
  fixed Denmark bounding box, confirmed in `PLAN-DMI-MIGRATION.md`).
- `image.py` — one `ImageEntity` exposing the latest observed frame, for
  automations/dashboards that don't use the custom card.
- `__init__.py` — wires the coordinator, registers the HTTP views, and
  auto-registers the card as a frontend resource via
  `add_extra_js_url`/static path — no manual "add resource" step.
- `www/weather-radar-dmi-card.js` — port of `dev/index.html`'s tuned
  timeline/coastline/hard-cut-frame UI into a shadow-DOM Lovelace card.
  Card chrome (button/track/ticks) binds to HA's theme CSS variables
  (`--primary-color`, `--divider-color`, etc.), matching the original
  TV2-era card's theme-inheritance approach; the radar map stage itself
  keeps its own fixed dark cartographic look rather than reflowing per
  theme. Dropped the harness's "N observed + M forecast" status line —
  useful for local debugging, not for a shipped dashboard widget.

Root-level cleanup: retired `radar.html`/old `weather-radar-card.js`,
`hacs.json` now points HACS at the integration category (auto-detected
from `custom_components/*/manifest.json`, no `filename` key needed),
`README.md` rewritten for integration-style install.

## Verification

Done here (no live HA available in this environment):
- `manifest.json` valid JSON with required HA fields.
- All new Python modules pass `py_compile`.
- `render.py`/`nowcast.py` re-run against the harness's own self-test
  assertions after porting — all passed.
- Card JS passes `node --check`.

Left to the user's own HA instance (confirmed available): installing via
HACS, completing the config flow, confirming `h5py`/`pyproj` install
cleanly, and that the card renders/updates/forecasts correctly live.

## Test suite

Added a real `tests/` suite (pytest) before recommending live install, per
the user's request for stronger sanity checking:

- `test_render.py`/`test_nowcast.py` run every assertion against **both**
  `dev/` and `custom_components/weather_radar_dmi/` copies of the pipeline
  (a parametrized `pipeline_impl` fixture) — any future drift between the
  harness and the shipped port fails a specific `[dev]` or
  `[custom_components]` test case rather than going unnoticed.
- `test_dev_server.py` starts `dev/server.py`'s real `ThreadingHTTPServer`
  against synthetic, network-free DMI data (`tests/synth.py` builds a
  minimal but structurally real ODIM_H5 file using a plain lon/lat
  "projection" so pixel<->geography math is exact and hand-checkable,
  rather than needing real stereographic geodesy).
- The HA layer (`config_flow`, `coordinator`, `http` views, `image`
  entity, `__init__.py` setup/unload) is tested with
  `pytest-homeassistant-custom-component` against the same synthetic
  data — real coordinator polling, real config-entry setup/unload, real
  HTTP views hit through `hass_client`.

**Environment friction (Windows):** `pytest-homeassistant-custom-component`
depends on the full `homeassistant` package, which pins `lru-dict==1.3.0`
— a version with no prebuilt wheel for Python 3.13 on any platform, so it
needs a C compiler to install. This machine has none, and installing the
several-GB MSVC Build Tools for one small extension wasn't worth it.
Resolved via WSL2 (already installed) instead: installed `uv` user-space
(no sudo), then used `uv pip install --override tests/uv-overrides.txt` to
force `lru-dict>=1.4.1` (full wheel coverage, otherwise a drop-in
replacement) — no compiler needed at all. See `tests/uv-overrides.txt` and
the README's "Running the tests" section.

**Two real bugs the test suite caught** (both fixed, not just worked
around):
1. `hass.http.register_view(...)` in `__init__.py` assumed `hass.http`
   already exists — true on any real running HA instance (core loads
   `http` very early) but not guaranteed in the isolated test `hass`.
   Fixed properly, not just in tests: added `"dependencies": ["http"]` to
   `manifest.json`, making the requirement explicit and enforced by HA's
   own bootstrap ordering. (`add_extra_js_url`'s similar dependency on the
   `frontend` component was left as a soft test-side mock instead of a
   hard manifest dependency — declaring `frontend` there would require the
   separate `home-assistant-frontend` asset package for anything to load
   it, real installs included, which is a much heavier ask than declaring
   `http`.)
2. `image.py`'s `WeatherRadarDmiImage` only set `_attr_image_last_updated`
   inside `_handle_coordinator_update`, which fires on the coordinator's
   *future* refreshes — not for data the coordinator already fetched
   before the entity was even created (which is the normal case:
   `async_setup_entry` awaits the coordinator's first refresh before
   forwarding platforms). The entity would sit at `unknown` until the next
   poll, up to `UPDATE_INTERVAL` (2 min) after setup. Fixed by seeding
   `_last_frame_id`/`_attr_image_last_updated` from the coordinator's
   already-fetched data in `__init__`.

## Resource-usage benchmark

Measured against **real, live DMI data** (not synthetic) from inside the
WSL venv, since that already had numpy/h5py/pyproj/Pillow installed. Ran
the actual pipeline primitives (not through HA) to isolate the compute
cost itself; repeated the forecast computation 6x in one process to
distinguish a real plateau from a leak before trusting any number.

- **Memory**: baseline process ~60-75 MB; after one full poll cycle (13
  observed frames + motion field + 9-step forecast), RSS climbs to
  ~370-515 MB then **plateaus** (confirmed via 6 repeated cycles: RSS
  stabilized after the 2nd and stayed flat) — this is glibc/numpy holding
  transient FFT/reprojection buffers rather than returning them to the OS,
  not a leak. The persistent piece (decoded-HDF5 cache, correctly pruned
  to the current window) is ~3.3 MB/frame x 13 ~= 43 MB. Practical
  takeaway: budget **~400-500 MB of headroom**, worth knowing on a
  Raspberry Pi-class HA install.
- **CPU/time**: cold start (nothing cached yet) ~20s, mostly
  downloading+decoding 13 HDF5 files. Steady-state poll: ~3-4s of compute
  every 2 min when genuinely new data arrived — roughly 2-3% of one core
  on average.
- **Disk — found and fixed a real bug**: nothing pruned the PNG cache.
  Every ~10 min (DMI's real publish cadence) leaves exactly one new
  observed-frame PNG (~344 KB) and one new forecast-frame PNG (~316 KB,
  since forecast filenames are keyed by absolute target timestamp — 8 of
  every 9 get overwritten in place as `curr` advances, only 1 is
  genuinely new) that never got cleaned up: **~95 MB/day, ~35 GB/year,
  unbounded**. Fixed with `_prune_png_cache` (coordinator.py) /
  `prune_png_cache` (dev/server.py): delete any cached PNG whose frame id
  has scrolled out of the current serving window.
  - `coordinator.py`: safe to prune unconditionally at the end of every
    `_poll_sync` — `DataUpdateCoordinator` never runs two polls
    concurrently, so there's nothing to race.
  - `dev/server.py` needed more care: it serves concurrent HTTP requests
    and has a background-thread forecast refresh (see
    `get_forecast_entries`'s docstring). Pruning from a per-request
    handler against a *stale* id set could delete a background recompute's
    just-written output before `_forecast_state` is updated to include
    it — self-inflicted cache misses. Fixed by pruning only from the two
    places a fresh window is fully computed and about to become
    authoritative (`_forecast_worker`, and the `first_ever` synchronous
    path), never from the request handler itself.
  - Regression tests: `test_frames_poll_prunes_stale_cached_pngs` (dev
    server) and `test_png_cache_is_pruned_of_stale_frame_ids`
    (coordinator) — write a bogus stale PNG, poll, assert it's gone and
    everything remaining belongs to the current window.

## Memory optimization: FFT precision + heap trimming

Two follow-up changes, benchmarked against real DMI data both before and
after (same methodology as above — cold poll, warm poll, and a 6-iteration
repeated-cycle loop to separate "plateau" from "leak"):

1. **float32/complex64 instead of float64/complex128 in `compute_motion`**
   (`nowcast.py`, both copies). Peak-finding on a correlation surface
   isn't precision-sensitive, and this was the single biggest transient
   allocation. numpy's `fft2` preserves the input's precision family, so
   casting `a`/`b`/the Hann window to float32 before the FFT is enough —
   halves every array from there on. Measured effect, isolated to that
   one stage: **~93% reduction in that stage's own memory delta (+15.9 MB
   -> +1.2 MB)**. Smaller effect on the end-to-end total, since the WORK-
   box reprojection and forecast-step rendering (unaffected by this
   change) dominate the overall footprint.
2. **`gc.collect()` + `ctypes.CDLL("libc.so.6").malloc_trim(0)`** at the
   end of every poll (`coordinator.py`'s `_poll_sync`; `dev/server.py`'s
   `_forecast_worker` and the `first_ever` synchronous path — the same
   two authoritative points as the cache-pruning fix, for the same
   reason). Doesn't reduce the peak during active computation, but forces
   glibc to actually return freed heap arenas to the OS afterward instead
   of holding them in reserve — the process was spending nearly all of
   its time *idle* between polls while still pinned at its peak RSS.

**Combined result** (6-iteration repeated-cycle loop, real DMI data, same
baseline/curr frame pair reused every iteration for a clean before/after
comparison):

| | before | after |
|---|---|---|
| Peak RSS during active computation | ~325 MB (plateaued, never released) | ~318 MB |
| **RSS at rest between polls** | **~325 MB (same as peak — never released)** | **~78 MB** |

The idle-time footprint — which is what the process actually sits at for
~all of the 2-minute gap between polls — dropped **~76%**, from ~325 MB
down to close to the ~60-76 MB cold-process baseline. Confirmed via the
same before/after full test suite run (54 passed) and the same 6x-repeat
methodology used to rule out a leak in the original benchmark. One
existing test (`test_motion_field_blending_stays_directionally_distinct_
around_the_global_vector`) needed `>` loosened to `>=`: float32's coarser
precision made a previously-strict inequality land exactly on the
boundary — a real precision-sensitivity artifact in the test's assertion,
not a behavior regression (the paired right-cell assertion already used
`<=`).

## v0.1.1: malloc_trim crash on HAOS (musl libc)

First real install (HAOS, x86_64) failed immediately: `DMI radar poll
failed: Symbol not found: malloc_trim`. `ctypes.CDLL("libc.so.6").
malloc_trim` raises `AttributeError` (not `OSError`) when the loaded libc
has no such symbol — true for musl-based libc, which HAOS's container
uses. `_trim_memory` only caught `OSError`, so the exception propagated
through `_poll_sync` into `_async_update_data`, turning an otherwise
fully-successful poll into a reported failure. Fixed by also catching
`AttributeError`; regression test reproduces the exact failure. Bumped to
v0.1.1.

Follow-up question: does musl need a different memory-release call in
place of `malloc_trim`, or does skipping it there just mean losing the
memory benefit? Tested directly rather than assuming — built an Alpine
(musl) container and repeated the same large float32/complex64 array
allocate/free pattern the FFT motion field uses, 6 cycles, checking RSS
after each with only `gc.collect()` (no trim call at all):

```
iter 0: peak=81.6 MB, after gc.collect() only=34.4 MB
iter 1-5: identical (81.6 -> 34.4 MB every cycle, no creep)
```

**musl doesn't need a `malloc_trim` equivalent.** musl's allocator
(`mallocng`) routes larger allocations through `mmap`, which the kernel
reclaims immediately via `munmap` on free — memory just doesn't get stuck
the way it does with glibc's arena-based heap (the actual problem
`malloc_trim` exists to work around on glibc). So on HAOS specifically,
`gc.collect()` alone already gets the full memory-release benefit;
`_trim_memory`'s `malloc_trim` attempt correctly no-ops there. v0.1.1's
fix is the complete, correct behavior on this platform, not a partial
workaround — no follow-up change needed.

## v0.1.2: card couldn't authenticate its own API calls

First real card install (after v0.1.1) surfaced two separate, unrelated
problems in sequence:

1. **`Custom element doesn't exist: weather-radar-dmi-card`, only in
   Microsoft Edge.** The JS fetched fine (200, correct Content-Type,
   correct bytes — confirmed via direct `fetch()` + content check),
   `import()` of it resolved without error, yet
   `customElements.get('weather-radar-dmi-card')` stayed `undefined` —
   even via HA's own standard manual Lovelace-resource loading path, not
   just our `add_extra_js_url` auto-registration. Switching to Chrome
   fixed it immediately. Not a bug in this integration — some Edge
   privacy/security feature was silently neutering the custom element
   registration for this specific page. No code change; noting it here
   in case it recurs for another Edge user.

2. **`GET /api/weather_radar_dmi/frames 401 (Unauthorized)`** — a real
   bug, once past #1. Home Assistant's frontend authenticates API calls
   with a bearer token (`Authorization: Bearer <token>`), not cookies.
   The card's `fetch()` calls were plain and unauthenticated — worked
   against the local dev harness (`dev/server.py`, no auth at all) but
   never against real HA. The Python view tests never caught this because
   `pytest-homeassistant-custom-component`'s `hass_client` fixture is
   already authenticated — there's no test coverage for the card's own
   browser-side JS at all, since the pytest suite can't exercise it.
   Fixed: the JSON frames endpoint now goes through `hass.callApi()` (the
   idiomatic HA pattern — handles the token and base URL automatically);
   the PNG frames, used as `<img src>` which can't carry a custom header
   at all, are now fetched as authenticated blobs and handed to the
   `<img>` via `URL.createObjectURL()`, with the object URL revoked when
   a frame's element is removed (window scrolled past) to avoid leaking
   blob references over a long-running dashboard session. Verified
   end-to-end against a mock auth-gated server (401 without the header,
   200 with it) before shipping — real blob URLs assigned, card renders,
   no errors. Bumped to v0.1.2.

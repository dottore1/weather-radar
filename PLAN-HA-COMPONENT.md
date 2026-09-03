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

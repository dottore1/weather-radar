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

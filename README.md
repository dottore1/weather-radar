# DMI Vejrradar

A Home Assistant integration showing live Danish precipitation radar: the
last ~2 hours of observed rainfall and up to ~90 minutes of forecast (an
FFT-based advection nowcast), on a detailed Denmark basemap, with a
scrub/play timeline.

Built on DMI's official Open Data radar composites (CC BY 4.0) — decoded,
reprojected, colorized and extrapolated entirely on your own Home
Assistant instance. No external server to host: install the integration
and its matching Lovelace card comes with it.

## Installation

### HACS (custom repository)

This isn't in HACS's default store, so add it as a custom repository:

1. HACS → the "⋮" menu → **Custom repositories**.
2. Repository: `https://github.com/dottore1/weather-radar`, category **Integration**.
3. Install **DMI Vejrradar**, then restart Home Assistant.
4. Settings → Devices & Services → **Add Integration** → search for
   "DMI Vejrradar". No configuration needed — it covers Denmark as a whole,
   and DMI's radar Open Data needs no API key.

The matching Lovelace card is registered automatically once the
integration is set up — no manual "add resource" step.

### Manual

Copy `custom_components/weather_radar_dmi/` into `<config>/custom_components/`,
restart Home Assistant, then add the integration as above.

## Usage

Add a card with:

```yaml
type: custom:weather-radar-dmi-card
```

No configuration options — the card fetches from this integration's own
endpoints (same-origin, using your existing HA session), which are backed
by a background poll every 2 minutes.

## Running the tests

```
tests/test_render.py, tests/test_nowcast.py   # framework-free pipeline, both dev/ and
                                                # custom_components/ copies (parametrized)
tests/test_dev_server.py                       # dev/server.py, a real local HTTP server
tests/test_manifest.py                         # manifest.json structural checks
tests/test_config_flow.py, test_coordinator.py,
tests/test_http_views.py, test_image.py, test_init.py   # the HA integration layer
```

The pipeline/dev-server/manifest tests only need `dev/requirements.txt` +
`requirements-test.txt`'s `pytest`. The HA-layer tests additionally need
`pytest-homeassistant-custom-component`, which pulls in the full
`homeassistant` package — and one of its pinned dependencies
(`lru-dict==1.3.0`) has no prebuilt wheel for Python 3.13 on **any**
platform, so installing it needs either a C compiler or the version
override below. **On Windows, run the HA-layer tests via WSL** (a native
Windows compiler toolchain is a much bigger ask than WSL, which most
already have):

```
wsl --install                              # if not already set up
wsl -d Ubuntu -- bash -lc "curl -LsSf https://astral.sh/uv/install.sh | sh"
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Dev/weather-map && \
  ~/.local/bin/uv venv .venv-test-wsl --python 3.13 && \
  ~/.local/bin/uv pip install --python .venv-test-wsl \
    -r dev/requirements.txt -r requirements-test.txt \
    --override tests/uv-overrides.txt"
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Dev/weather-map && .venv-test-wsl/bin/python -m pytest -v"
```

On Linux/Mac, drop the `wsl -d Ubuntu --` prefix and run the same `uv`/
`pytest` commands directly — no override needed if a compiler is present,
but it's harmless either way.

## Notes

- Radar data typically lags real time by 15–25 minutes; that's DMI's own
  processing delay, not a bug.
- `h5py` and `pyproj` (needed to decode DMI's raw HDF5 composites and
  reproject them) are less common Home Assistant dependencies than
  `numpy`/`Pillow` and pull in compiled extensions — should install cleanly
  from wheels on standard HAOS/Container images, but this is worth knowing
  if setup ever fails on an unusual platform.
- `dev/` is a separate local-only test harness (no Home Assistant involved)
  used to build and tune the rendering/nowcast pipeline before it was
  ported into `custom_components/weather_radar_dmi/`. See
  `PLAN-DMI-MIGRATION.md` and `PLAN-HA-COMPONENT.md` for the history.

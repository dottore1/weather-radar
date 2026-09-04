"""Shared pytest fixtures for the weather-radar test suite.

Covers three layers:
- The framework-free pipeline (render.py/nowcast.py), tested against BOTH
  dev/ (the harness) and custom_components/weather_radar_dmi/ (the ported
  copy) via the `pipeline_impl` fixture — any drift between the two shows
  up as a per-implementation test failure, not silent divergence.
- dev/server.py, a plain-stdlib local HTTP server — integration-tested
  directly against a real (but ephemeral, localhost-only) HTTP server.
- custom_components/weather_radar_dmi's Home Assistant layer, tested with
  pytest-homeassistant-custom-component's `hass` fixture.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """pytest-homeassistant-custom-component blocks loading anything under
    custom_components/ by default (a safety guard for HA's own core test
    suite); this integration IS the thing under test, so allow it."""
    yield


def load_module(unique_name: str, path: Path):
    """Loads a module directly by file path, bypassing normal package
    import machinery. Used for the two files that exist as near-duplicate
    copies (dev/ and custom_components/weather_radar_dmi/) so both can be
    loaded side by side under distinct names without colliding."""
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


class PipelineImpl:
    """Bundles one implementation's render/nowcast modules plus a label,
    so a test failure clearly says which copy (dev vs. the shipped
    integration) regressed."""

    def __init__(self, label: str, base: Path) -> None:
        self.label = label
        self.render = load_module(f"_test_{label}_render", base / "render.py")
        self.nowcast = load_module(f"_test_{label}_nowcast", base / "nowcast.py")

    def __repr__(self) -> str:
        return f"PipelineImpl({self.label})"


_IMPL_BASES = {
    "dev": REPO_ROOT / "dev",
    "custom_components": REPO_ROOT / "custom_components" / "weather_radar_dmi",
}


@pytest.fixture(params=list(_IMPL_BASES))
def pipeline_impl(request) -> PipelineImpl:
    return PipelineImpl(request.param, _IMPL_BASES[request.param])


@pytest.fixture
async def setup_http(hass):
    """Loads Home Assistant's core `http` component. A real running HA
    instance always has this ready before any integration's
    async_setup_entry runs (manifest.json declares it as a dependency),
    but the isolated test `hass` fixture does not load it automatically —
    needed by any test that touches hass.http directly, or indirectly via
    a config entry setup that bypasses manifest dependency resolution."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    await hass.async_block_till_done()

"""Tests for WeatherRadarDmiCoordinator: the actual fetch/decode/render/
nowcast pipeline, run against synthetic DMI data via a monkeypatched
urllib.request.urlopen — no real network access."""
from __future__ import annotations

import urllib.request

from custom_components.weather_radar_dmi import coordinator as coordinator_module
from custom_components.weather_radar_dmi.coordinator import WeatherRadarDmiCoordinator

from tests import synth


def _patch_dmi(monkeypatch, tmp_path):
    frames, hrefs = synth.make_default_dataset(tmp_path)
    fake_urlopen = synth.make_fake_urlopen(
        coordinator_module.ITEMS_URL, synth.make_items_payload(frames), hrefs)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


async def test_first_refresh_renders_observed_and_forecast_frames(hass, monkeypatch, tmp_path):
    _patch_dmi(monkeypatch, tmp_path)
    coord = WeatherRadarDmiCoordinator(hass)

    await coord.async_refresh()

    assert coord.last_update_success
    observed = [f for f in coord.data if not f["forecast"]]
    forecast = [f for f in coord.data if f["forecast"]]
    assert len(observed) == 3
    assert len(forecast) == coordinator_module.FCST_FRAMES
    assert coord.latest_frame_id == "frame-2"
    assert coord.frame_png_path("frame-2").exists()
    assert coord.frame_png_path(forecast[0]["id"]).exists()


async def test_second_refresh_reuses_the_cached_forecast_when_key_unchanged(hass, monkeypatch, tmp_path):
    _patch_dmi(monkeypatch, tmp_path)
    coord = WeatherRadarDmiCoordinator(hass)

    await coord.async_refresh()
    first_data = coord.data
    await coord.async_refresh()

    assert coord.data == first_data


async def test_decoded_cache_is_pruned_to_the_current_item_window(hass, monkeypatch, tmp_path):
    _patch_dmi(monkeypatch, tmp_path)
    coord = WeatherRadarDmiCoordinator(hass)
    await coord.async_refresh()

    assert set(coord._decoded_cache) <= {"frame-0", "frame-1", "frame-2"}


async def test_png_cache_is_pruned_of_stale_frame_ids(hass, monkeypatch, tmp_path):
    """A PNG left over from a frame id no longer in the current serving
    window should get deleted on the next poll (see _prune_png_cache's
    docstring — without this, disk usage grows unbounded, ~35 GB/year on
    real data)."""
    _patch_dmi(monkeypatch, tmp_path)
    coord = WeatherRadarDmiCoordinator(hass)
    await coord.async_refresh()

    stale_path = coord.frame_png_path("stale-frame-from-an-old-window")
    stale_path.write_bytes(b"not a real png, just needs to exist")

    await coord.async_refresh()

    assert not stale_path.exists()
    current_ids = {entry["id"] for entry in coord.data}
    remaining = {p.stem for p in coord.cache_dir.glob("*.png")}
    assert remaining <= current_ids


class _LibcMissingMallocTrim:
    """Stands in for a loaded libc (e.g. musl, as used by some container
    base images) that has no malloc_trim symbol. ctypes raises
    AttributeError for that — not OSError — which is exactly what
    _trim_memory failed to catch in production (see
    "DMI radar poll failed: Symbol not found: malloc_trim")."""

    def __getattr__(self, name):
        raise AttributeError(name)


def test_trim_memory_survives_a_libc_without_malloc_trim(monkeypatch):
    monkeypatch.setattr(coordinator_module.ctypes, "CDLL", lambda name: _LibcMissingMallocTrim())
    coordinator_module._trim_memory()  # must not raise


async def test_poll_still_succeeds_when_malloc_trim_is_unavailable(hass, monkeypatch, tmp_path):
    """End-to-end regression test for the actual reported failure: the
    whole poll was reported as failed even though every bit of real work
    (fetch/render/forecast) had already succeeded, purely because
    _trim_memory's AttributeError propagated up through _poll_sync."""
    _patch_dmi(monkeypatch, tmp_path)
    monkeypatch.setattr(coordinator_module.ctypes, "CDLL", lambda name: _LibcMissingMallocTrim())
    coord = WeatherRadarDmiCoordinator(hass)

    await coord.async_refresh()

    assert coord.last_update_success
    assert coord.data

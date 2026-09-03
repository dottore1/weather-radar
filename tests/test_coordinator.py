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

"""Tests for the two HTTP views (frame list JSON, frame PNG bytes)."""
from __future__ import annotations

import urllib.request

from custom_components.weather_radar_dmi import coordinator as coordinator_module
from custom_components.weather_radar_dmi.coordinator import WeatherRadarDmiCoordinator
from custom_components.weather_radar_dmi.http import WeatherRadarFrameImageView, WeatherRadarFramesView

from tests import synth


async def _ready_coordinator(hass, monkeypatch, tmp_path):
    frames, hrefs = synth.make_default_dataset(tmp_path)
    fake_urlopen = synth.make_fake_urlopen(
        coordinator_module.ITEMS_URL, synth.make_items_payload(frames), hrefs)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    coord = WeatherRadarDmiCoordinator(hass)
    await coord.async_refresh()
    return coord


async def test_frames_view_returns_the_coordinator_data(hass, setup_http, hass_client, monkeypatch, tmp_path):
    coord = await _ready_coordinator(hass, monkeypatch, tmp_path)
    hass.http.register_view(WeatherRadarFramesView(coord))
    client = await hass_client()

    resp = await client.get("/api/weather_radar_dmi/frames")
    assert resp.status == 200
    payload = await resp.json()
    assert payload == coord.data


async def test_frame_image_view_serves_a_cached_observed_png(hass, setup_http, hass_client, monkeypatch, tmp_path):
    coord = await _ready_coordinator(hass, monkeypatch, tmp_path)
    hass.http.register_view(WeatherRadarFrameImageView(hass, coord))
    client = await hass_client()

    resp = await client.get(f"/api/weather_radar_dmi/frame/{coord.latest_frame_id}.png")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    body = await resp.read()
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


async def test_frame_image_view_serves_a_cached_forecast_png(hass, setup_http, hass_client, monkeypatch, tmp_path):
    coord = await _ready_coordinator(hass, monkeypatch, tmp_path)
    hass.http.register_view(WeatherRadarFrameImageView(hass, coord))
    client = await hass_client()

    forecast_id = next(f["id"] for f in coord.data if f["forecast"])
    resp = await client.get(f"/api/weather_radar_dmi/frame/{forecast_id}.png")
    assert resp.status == 200
    body = await resp.read()
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


async def test_frame_image_view_404s_for_an_unknown_frame(hass, setup_http, hass_client, monkeypatch, tmp_path):
    coord = await _ready_coordinator(hass, monkeypatch, tmp_path)
    hass.http.register_view(WeatherRadarFrameImageView(hass, coord))
    client = await hass_client()

    resp = await client.get("/api/weather_radar_dmi/frame/does-not-exist.png")
    assert resp.status == 404

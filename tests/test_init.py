"""Tests for the integration's __init__.py: full setup/unload wiring —
coordinator creation, HTTP view + static path + frontend resource
registration, and platform forwarding."""
from __future__ import annotations

import urllib.request

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.weather_radar_dmi import CARD_URL
from custom_components.weather_radar_dmi import coordinator as coordinator_module
from custom_components.weather_radar_dmi.const import DOMAIN

from tests import synth


def _patch_dmi(monkeypatch, tmp_path):
    frames, hrefs = synth.make_default_dataset(tmp_path)
    fake_urlopen = synth.make_fake_urlopen(
        coordinator_module.ITEMS_URL, synth.make_items_payload(frames), hrefs)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


async def test_setup_entry_wires_up_views_static_path_and_image_platform(
    hass, setup_http, mock_add_extra_js_url, hass_client, monkeypatch, tmp_path
):
    _patch_dmi(monkeypatch, tmp_path)

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]

    client = await hass_client()

    resp = await client.get("/api/weather_radar_dmi/frames")
    assert resp.status == 200

    resp = await client.get(CARD_URL)
    assert resp.status == 200
    body = await resp.text()
    assert "weather-radar-dmi-card" in body


async def test_unload_entry_removes_coordinator_state(hass, setup_http, mock_add_extra_js_url, monkeypatch, tmp_path):
    _patch_dmi(monkeypatch, tmp_path)

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]

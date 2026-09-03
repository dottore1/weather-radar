"""Tests for the latest-observed-frame image entity."""
from __future__ import annotations

import urllib.request

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.weather_radar_dmi import coordinator as coordinator_module
from custom_components.weather_radar_dmi.const import DOMAIN

from tests import synth


async def test_image_entity_is_created_and_available_after_setup(
    hass, setup_http, mock_add_extra_js_url, monkeypatch, tmp_path
):
    frames, hrefs = synth.make_default_dataset(tmp_path)
    fake_urlopen = synth.make_fake_urlopen(
        coordinator_module.ITEMS_URL, synth.make_items_payload(frames), hrefs)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("image", DOMAIN, f"{entry.entry_id}_latest_frame")
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state not in (None, "unknown", "unavailable")

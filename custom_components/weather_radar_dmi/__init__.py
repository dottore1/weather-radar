"""The DMI Vejrradar integration.

Installing this is meant to be the whole setup: it runs the fetch/decode/
render/nowcast pipeline itself (see coordinator.py), serves the results
over HA's own HTTP server (see http.py), and auto-registers the matching
Lovelace card as a frontend resource — no separate server to host, no
manual "add resource" step.
"""
from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import WeatherRadarDmiCoordinator
from .http import WeatherRadarFrameImageView, WeatherRadarFramesView

PLATFORMS = ["image"]

CARD_URL = "/weather_radar_dmi/weather-radar-dmi-card.js"
CARD_PATH = Path(__file__).parent / "www" / "weather-radar-dmi-card.js"


async def _async_register_static_path(hass: HomeAssistant) -> None:
    try:
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=True)]
        )
    except ImportError:
        # Older HA core without the async static-path API (added 2024.7).
        hass.http.register_static_path(CARD_URL, str(CARD_PATH), cache_headers=True)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = WeatherRadarDmiCoordinator(hass)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Views/static path/frontend resource are process-wide, not per-entry —
    # only register once. config_flow.py caps this integration at a single
    # entry, so this is really just a defensive guard.
    if len(hass.data[DOMAIN]) == 1:
        hass.http.register_view(WeatherRadarFramesView(coordinator))
        hass.http.register_view(WeatherRadarFrameImageView(hass, coordinator))
        await _async_register_static_path(hass)
        add_extra_js_url(hass, CARD_URL)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded

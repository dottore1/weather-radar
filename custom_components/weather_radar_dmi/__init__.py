"""The DMI Vejrradar integration.

Installing this is meant to be the whole setup: it runs the fetch/decode/
render/nowcast pipeline itself (see coordinator.py), serves the results
over HA's own HTTP server (see http.py), and auto-registers the matching
Lovelace card as a frontend resource — no separate server to host, no
manual "add resource" step.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import WeatherRadarDmiCoordinator
from .http import WeatherRadarFrameImageView, WeatherRadarFramesView

_LOGGER = logging.getLogger(__name__)

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


async def _async_register_lovelace_resource(hass: HomeAssistant) -> None:
    """Registers the card as a standard Lovelace resource — the same
    mechanism HACS itself uses for user-installed cards. add_extra_js_url
    (called alongside this, not instead of it) turned out to be
    unreliable in practice: on a real instance, its injected inline
    `import(url)` sometimes never actually fires on first page load, while
    manually re-running the identical import() always worked — see
    PLAN-HA-COMPONENT.md's investigation. The standard resources path
    every other HACS card already uses (real `<script src>` tags) doesn't
    have that problem. Keeping add_extra_js_url too is harmless: ES
    modules dedupe by resolved URL, so a module registered both ways
    still only executes once.

    This uses hass.data["lovelace"].resources, an internal API with no
    public contract for third-party use, and one that only exists for
    storage-mode dashboards (not YAML-mode Lovelace configs) — so this is
    entirely best-effort. Any failure is logged, never raised: the
    integration's actual data pipeline works regardless, and the user can
    always add the resource manually (Settings > Dashboards > Resources)
    if this doesn't take on their setup."""
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data is None:
        return
    try:
        resources = lovelace_data.resources
        for item in resources.async_items():
            if item.get("url") == CARD_URL:
                return
        await resources.async_create_item({"res_type": "module", "url": CARD_URL})
    except Exception:  # noqa: BLE001 - best-effort; see docstring
        _LOGGER.warning(
            "Could not auto-register the DMI Vejrradar Lovelace resource; "
            "add it manually (Settings > Dashboards > Resources, type "
            "JavaScript Module) if the card doesn't load: %s",
            CARD_URL,
        )


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
        await _async_register_lovelace_resource(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded

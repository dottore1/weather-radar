"""A single `image` entity exposing the latest observed radar frame.

The Lovelace card (www/weather-radar-dmi-card.js) is what most people will
actually look at — this entity is a small addition on top so automations
and dashboards that don't use the custom card still get something (e.g. a
"rain now" picture in a notification), reusing the exact same cached PNG.
"""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .coordinator import WeatherRadarDmiCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WeatherRadarDmiCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeatherRadarDmiImage(hass, coordinator, entry)])


class WeatherRadarDmiImage(CoordinatorEntity[WeatherRadarDmiCoordinator], ImageEntity):
    _attr_has_entity_name = True
    _attr_name = "Seneste radarbillede"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, coordinator: WeatherRadarDmiCoordinator, entry: ConfigEntry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{entry.entry_id}_latest_frame"
        # The coordinator's first refresh already ran (async_setup_entry
        # awaits it before forwarding platforms), so latest_frame_id may
        # already be populated by the time this entity is created —
        # _handle_coordinator_update only fires on *future* refreshes, so
        # without this the entity would sit at "unknown" until the
        # coordinator's next poll, up to UPDATE_INTERVAL later.
        self._last_frame_id: str | None = coordinator.latest_frame_id
        if self._last_frame_id:
            self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        frame_id = self.coordinator.latest_frame_id
        if frame_id and frame_id != self._last_frame_id:
            self._last_frame_id = frame_id
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        frame_id = self.coordinator.latest_frame_id
        if not frame_id:
            return None
        path = self.coordinator.frame_png_path(frame_id)

        def _read() -> bytes | None:
            return path.read_bytes() if path.exists() else None

        return await self.hass.async_add_executor_job(_read)

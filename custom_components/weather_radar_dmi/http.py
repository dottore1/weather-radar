"""HTTP views exposing the coordinator's cached frame list/PNGs to the
Lovelace card — the same two-endpoint shape dev/server.py serves locally
(/api/frames, /api/frame/<id>.png), mounted under HA's own HTTP server.

requires_auth = True: these run behind the same authenticated frontend
session the card itself loads in (same pattern HA's own camera proxy views
use), not a public endpoint.
"""
from __future__ import annotations

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import FRAME_CACHE_SECONDS
from .coordinator import WeatherRadarDmiCoordinator


class WeatherRadarFramesView(HomeAssistantView):
    url = "/api/weather_radar_dmi/frames"
    name = "api:weather_radar_dmi:frames"
    requires_auth = True

    def __init__(self, coordinator: WeatherRadarDmiCoordinator) -> None:
        self._coordinator = coordinator

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(self._coordinator.data or [])


class WeatherRadarFrameImageView(HomeAssistantView):
    url = "/api/weather_radar_dmi/frame/{frame_id}"
    name = "api:weather_radar_dmi:frame"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, coordinator: WeatherRadarDmiCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator

    async def get(self, request: web.Request, frame_id: str) -> web.Response:
        frame_id = frame_id.removesuffix(".png")
        cache_path = self._coordinator.frame_png_path(frame_id)

        def _read() -> bytes | None:
            return cache_path.read_bytes() if cache_path.exists() else None

        png = await self._hass.async_add_executor_job(_read)
        if png is None:
            return web.Response(status=404, text="frame not found or expired")
        return web.Response(
            body=png,
            content_type="image/png",
            headers={"Cache-Control": f"public, max-age={FRAME_CACHE_SECONDS}"},
        )

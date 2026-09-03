"""Config flow for DMI Vejrradar.

Zero-config on purpose: the radar composite covers Denmark as a whole (no
per-location setup) and DMI's Open Data radar endpoint needs no API key
(confirmed during the dev-harness migration — see PLAN-DMI-MIGRATION.md).
Single-instance only, since a second entry would just poll the same data.
"""
from __future__ import annotations

from homeassistant import config_entries

from .const import DOMAIN


class WeatherRadarDmiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="DMI Vejrradar", data={})
        return self.async_show_form(step_id="user")

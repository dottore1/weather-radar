"""DataUpdateCoordinator for the DMI Vejrradar integration.

Runs the same fetch -> decode -> reproject -> colorize -> nowcast pipeline
as dev/server.py (the local dev harness this was ported from), adapted to
run inside Home Assistant: the whole synchronous pipeline (network I/O and
CPU-bound HDF5/FFT work alike) runs in HA's executor via
async_add_executor_job, never on the event loop.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, FCST_FRAMES, HIST_FRAMES, ITEMS_URL, MOTION_BASELINE_STEPS, UPDATE_INTERVAL
from .nowcast import estimate_motion_field, forecast_steps_from_field
from .render import (
    OUT_HEIGHT,
    OUT_LAT_MAX,
    OUT_LAT_MIN,
    OUT_LON_MAX,
    OUT_LON_MIN,
    OUT_WIDTH,
    WORK_HEIGHT,
    WORK_LAT_MAX,
    WORK_LAT_MIN,
    WORK_LON_MAX,
    WORK_LON_MIN,
    WORK_WIDTH,
    crop_to_display,
    decode_h5,
    png_from_grid,
    reproject_to_dbz_grid,
)

_LOGGER = logging.getLogger(__name__)


def _fetch_latest_items() -> list[dict]:
    """Server-side call to DMI's API — no API key required for the
    composite radar collection (see PLAN-DMI-MIGRATION.md)."""
    with urllib.request.urlopen(ITEMS_URL, timeout=15) as resp:
        data = json.load(resp)
    # DMI alternates scanType between 'fullRange' (240km, full national
    # coverage) and 'doppler' (120km) roughly every other scan; only
    # fullRange gives consistent whole-country coverage.
    items = [f for f in data["features"] if f["properties"]["scanType"] == "fullRange"]
    items.sort(key=lambda f: f["properties"]["datetime"])
    return items[-HIST_FRAMES:]


def _download(href: str) -> bytes:
    with urllib.request.urlopen(href, timeout=20) as resp:
        return resp.read()


class WeatherRadarDmiCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls DMI, renders frames, and caches everything on disk so the HTTP
    views (http.py) can serve PNGs without redoing this work per-request.

    coordinator.data is the same JSON-shaped frame list dev/server.py's
    /api/frames endpoint returns: [{id, time, forecast}, ...]. Rendered PNG
    bytes live on disk under self.cache_dir, keyed by frame id.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.cache_dir = Path(hass.config.path(".storage", DOMAIN, "cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Decoded-HDF5 cache: the two motion-baseline frames used by the
        # forecast pipeline are almost always among the same observed items
        # already downloaded for their own image endpoint — this avoids
        # downloading+decoding them twice per cycle.
        self._decoded_cache: dict[str, dict] = {}
        self._decoded_lock = threading.Lock()
        self._forecast_state: dict = {"key": None, "entries": []}
        self.latest_frame_id: str | None = None  # for image.py

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self.hass.async_add_executor_job(self._poll_sync)
        except Exception as err:  # noqa: BLE001 - surfaced to HA as UpdateFailed
            raise UpdateFailed(f"DMI radar poll failed: {err}") from err

    def frame_png_path(self, frame_id: str) -> Path:
        return self.cache_dir / f"{frame_id}.png"

    # --- decoded-HDF5 cache -------------------------------------------------
    def _get_decoded(self, item: dict) -> dict:
        frame_id = item["id"]
        with self._decoded_lock:
            cached = self._decoded_cache.get(frame_id)
        if cached is not None:
            return cached
        h5_bytes = _download(item["asset"]["data"]["href"])
        decoded = decode_h5(io.BytesIO(h5_bytes))
        with self._decoded_lock:
            self._decoded_cache[frame_id] = decoded
        return decoded

    def _prune_decoded_cache(self, current_ids: set[str]) -> None:
        with self._decoded_lock:
            for stale_id in list(self._decoded_cache):
                if stale_id not in current_ids:
                    del self._decoded_cache[stale_id]

    def _get_or_render(self, item: dict) -> None:
        cache_path = self.frame_png_path(item["id"])
        if cache_path.exists():
            return
        decoded = self._get_decoded(item)
        dbz, valid = reproject_to_dbz_grid(
            decoded, OUT_WIDTH, OUT_HEIGHT, OUT_LON_MIN, OUT_LON_MAX, OUT_LAT_MIN, OUT_LAT_MAX
        )
        cache_path.write_bytes(png_from_grid(dbz, valid))

    def _get_grid_work(self, item: dict):
        decoded = self._get_decoded(item)
        return reproject_to_dbz_grid(
            decoded, WORK_WIDTH, WORK_HEIGHT, WORK_LON_MIN, WORK_LON_MAX, WORK_LAT_MIN, WORK_LAT_MAX
        )

    # --- forecast ------------------------------------------------------------
    def _compute_forecast_sync(self, items: list[dict]) -> list[dict]:
        if len(items) <= MOTION_BASELINE_STEPS:
            return []
        baseline_item, curr_item = items[-1 - MOTION_BASELINE_STEPS], items[-1]
        dbz_base, valid_base = self._get_grid_work(baseline_item)
        dbz_curr, valid_curr = self._get_grid_work(curr_item)
        # Piecewise (tile-based) motion field instead of one global vector —
        # see nowcast.py and PLAN-DMI-MIGRATION.md for why.
        dy_field_baseline, dx_field_baseline = estimate_motion_field(dbz_base, valid_base, dbz_curr, valid_curr)
        dy_field = dy_field_baseline / MOTION_BASELINE_STEPS
        dx_field = dx_field_baseline / MOTION_BASELINE_STEPS
        steps = forecast_steps_from_field(dbz_curr, valid_curr, dy_field, dx_field, FCST_FRAMES)

        base_time = datetime.fromisoformat(curr_item["properties"]["datetime"].replace("Z", "+00:00"))
        entries = []
        for i, (dbz_work, valid_work) in enumerate(steps, start=1):
            dbz, valid = crop_to_display(dbz_work, valid_work)
            t = base_time + timedelta(minutes=10 * i)
            fid = "forecast-" + t.strftime("%Y%m%d%H%M")
            # Always overwrite, never skip-if-exists: a given absolute
            # future timestamp is computed as a *different* step number
            # (different accumulated displacement, from a fresher motion
            # field) across successive polls as curr advances — see
            # dev/server.py's _compute_forecast_sync docstring for the full
            # history of this fix.
            self.frame_png_path(fid).write_bytes(png_from_grid(dbz, valid))
            entries.append({
                "id": fid,
                "time": t.strftime("%Y-%m-%dT%H:%M:00Z"),
                "forecast": True,
            })
        return entries

    def _get_forecast_entries(self, items: list[dict]) -> list[dict]:
        """Unlike dev/server.py (which serves many concurrent HTTP polls and
        needed a background-thread refresh to keep those fast), this runs
        once per UPDATE_INTERVAL on the coordinator's own executor job —
        there's no separate fast-path caller to protect, so it's fine to
        just recompute in place whenever the (baseline_id, curr_id) key
        changes."""
        if len(items) <= MOTION_BASELINE_STEPS:
            return []
        baseline_item, curr_item = items[-1 - MOTION_BASELINE_STEPS], items[-1]
        key = (baseline_item["id"], curr_item["id"])
        if self._forecast_state["key"] == key:
            return list(self._forecast_state["entries"])
        entries = self._compute_forecast_sync(items)
        self._forecast_state["key"] = key
        self._forecast_state["entries"] = entries
        return entries

    def _poll_sync(self) -> list[dict]:
        items = _fetch_latest_items()
        self._prune_decoded_cache({it["id"] for it in items})
        for item in items:
            self._get_or_render(item)
        payload = [
            {"id": it["id"], "time": it["properties"]["datetime"], "forecast": False}
            for it in items
        ]
        if items:
            self.latest_frame_id = items[-1]["id"]
        payload.extend(self._get_forecast_entries(items))
        self._prune_png_cache({entry["id"] for entry in payload})
        return payload

    def _prune_png_cache(self, current_ids: set[str]) -> None:
        """Deletes cached PNGs whose frame id has scrolled out of the
        current serving window. Without this, every ~10-min DMI publish
        leaves one new observed-frame PNG and one new forecast-frame PNG
        that never get cleaned up — unbounded growth (~35 GB/year measured
        against live DMI data; see PLAN-HA-COMPONENT.md's resource-usage
        benchmark). Safe to call unconditionally at the end of every poll:
        DataUpdateCoordinator never runs two _poll_sync calls concurrently,
        so there's no in-flight recompute this could race (contrast with
        dev/server.py's prune_png_cache, which has to be more careful about
        exactly when it runs because of that file's background-thread
        refresh path)."""
        for path in self.cache_dir.glob("*.png"):
            if path.stem not in current_ids:
                path.unlink(missing_ok=True)

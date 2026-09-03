"""Shared constants for the DMI Vejrradar integration."""
from datetime import timedelta

DOMAIN = "weather_radar_dmi"

ITEMS_URL = (
    "https://opendataapi.dmi.dk/v1/radardata/collections/composite/items"
    "?limit=40&sortorder=datetime,DESC"
)

HIST_FRAMES = 13  # ~2h at 10-min steps
FCST_FRAMES = 9   # 90 min ahead at 10-min steps (advection nowcast — see nowcast.py)
MOTION_BASELINE_STEPS = 2  # estimate motion over a 20-min gap, not the noisy 10-min pair

UPDATE_INTERVAL = timedelta(minutes=2)  # DMI publishes ~every 10 min; polling faster is
                                         # cheap thanks to the id-keyed caches in coordinator.py

FRAME_CACHE_SECONDS = 86400  # rendered frame content is immutable once generated for a given id

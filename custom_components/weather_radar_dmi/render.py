"""Decode a DMI ODIM_H5 radar composite file and render it as a colorized,
reprojected PNG aligned to a plain equirectangular (lon/lat) grid.

Ported near-verbatim from dev/render.py (the local dev harness's pipeline) —
kept dependency-light and framework-free so it drops in here unchanged. See
dev/render.py for the harness this was developed and tuned against.
"""
from __future__ import annotations

import io

import h5py
import numpy as np
from PIL import Image
from pyproj import Transformer

# Output frame: plain equirectangular lon/lat box around Denmark, with a
# little margin beyond the coastline so weather approaching from the west/
# south is visible before it arrives.
OUT_LON_MIN, OUT_LON_MAX = 7.0, 16.0
OUT_LAT_MIN, OUT_LAT_MAX = 54.3, 58.2
OUT_WIDTH, OUT_HEIGHT = 1000, 700

# Padded working box used only by the forecast pipeline (see
# dbz_grid_for_work / crop_to_display below): advecting the field forward
# reveals edge pixels with no known data. Reprojecting a larger area than
# what's displayed lets those reveals pull in real DMI data (already
# present in the source composite, just outside our display box) instead
# of leaving a growing blank/transparent margin as forecast lead time
# increases. Sized generously (~150-190km) to comfortably cover a fast
# frontal system over 90 min; if actual motion ever exceeds this, the
# margin still degrades gracefully to the old blank-edge behavior rather
# than failing outright.
FCST_MARGIN_LON, FCST_MARGIN_LAT = 2.5, 1.8
WORK_LON_MIN, WORK_LON_MAX = OUT_LON_MIN - FCST_MARGIN_LON, OUT_LON_MAX + FCST_MARGIN_LON
WORK_LAT_MIN, WORK_LAT_MAX = OUT_LAT_MIN - FCST_MARGIN_LAT, OUT_LAT_MAX + FCST_MARGIN_LAT
_DEG_PER_PX_LON = (OUT_LON_MAX - OUT_LON_MIN) / OUT_WIDTH
_DEG_PER_PX_LAT = (OUT_LAT_MAX - OUT_LAT_MIN) / OUT_HEIGHT
MARGIN_PX_X = round(FCST_MARGIN_LON / _DEG_PER_PX_LON)
MARGIN_PX_Y = round(FCST_MARGIN_LAT / _DEG_PER_PX_LAT)
WORK_WIDTH = OUT_WIDTH + 2 * MARGIN_PX_X
WORK_HEIGHT = OUT_HEIGHT + 2 * MARGIN_PX_Y

# dBZ -> RGBA color ramp. Rough intensity convention (light blue = light
# rain, through purple/magenta = heavy) — our own choice, not reused from
# any third party. Values below 20 dBZ are fully transparent.
#
# The `valid` mask (see reproject_to_dbz_grid) only excludes the source
# file's own nodata/undetect sentinels — it does not mean "this pixel is
# real precipitation." Real DMI composites carry valid-but-tiny and even
# negative dBZ values (observed as low as -31.5 dBZ against live data) from
# noise floor / clear-air return, not rain. The previous first stop at 5.0
# painted a lot of that as faint-but-visible, washing out real dry regions
# into a near-continuous haze. A/B tested several thresholds against DMI's
# own reference rendering for the same real composite (see CLAUDE.md); 20
# dBZ was the one that reproduced DMI's actual
# dry/wet contrast (e.g. a genuinely dry central Jutland showing as dry,
# not lightly shaded) rather than one continuous smear.
COLOR_STOPS = [
    (0.0,   (191, 230, 251, 0)),
    (20.0,  (191, 230, 251, 0)),
    (20.01, (191, 230, 251, 170)),
    (25.0,  (58, 111, 216, 215)),
    (35.0,  (90, 58, 176, 225)),
    (45.0,  (167, 38, 192, 235)),
    (55.0,  (233, 30, 140, 245)),
]

_STOP_VALUES = np.array([s[0] for s in COLOR_STOPS], dtype=np.float32)
_STOP_COLORS = np.array([s[1] for s in COLOR_STOPS], dtype=np.float32)


def colorize(dbz: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """dbz: float32 grid of reflectivity values. valid: bool mask (True = has
    a real, in-range measurement; False = no-data/undetect -> transparent).
    Returns an (H, W, 4) uint8 RGBA array."""
    out = np.zeros(dbz.shape + (4,), dtype=np.uint8)
    for ch in range(4):
        out[..., ch] = np.clip(
            np.interp(dbz, _STOP_VALUES, _STOP_COLORS[:, ch]), 0, 255
        ).astype(np.uint8)
    out[~valid, 3] = 0
    return out


def decode_h5(path_or_bytes) -> dict:
    with h5py.File(path_or_bytes, "r") as f:
        raw = f["dataset1/data1/data"][:]
        what = dict(f["what"].attrs)
        where = dict(f["where"].attrs)
    return {
        "raw": raw,
        "gain": float(what["gain"]),
        "offset": float(what["offset"]),
        "nodata": float(what["nodata"]),
        "undetect": float(what["undetect"]),
        "projdef": where["projdef"].decode() if isinstance(where["projdef"], bytes) else where["projdef"],
        "ul_lon": float(np.asarray(where["UL_lon"]).ravel()[0]),
        "ul_lat": float(np.asarray(where["UL_lat"]).ravel()[0]),
        "xscale": float(np.asarray(where["xscale"]).ravel()[0]),
        "yscale": float(np.asarray(where["yscale"]).ravel()[0]),
    }


def reproject_to_dbz_grid(decoded: dict, out_w: int, out_h: int,
                           lon_min: float, lon_max: float,
                           lat_min: float, lat_max: float) -> tuple[np.ndarray, np.ndarray]:
    """Resample the source stereographic grid onto a plain equirectangular
    output grid via nearest-neighbor lookup. Returns (dbz, valid_mask)."""
    raw = decoded["raw"]
    src_h, src_w = raw.shape

    to_proj = Transformer.from_crs("EPSG:4326", decoded["projdef"], always_xy=True)
    ul_x, ul_y = to_proj.transform(decoded["ul_lon"], decoded["ul_lat"])

    lons = np.linspace(lon_min, lon_max, out_w)
    lats = np.linspace(lat_max, lat_min, out_h)  # row 0 = top = max lat
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    x, y = to_proj.transform(lon_grid, lat_grid)

    col = (x - ul_x) / decoded["xscale"]
    row = (ul_y - y) / decoded["yscale"]
    col_i = np.round(col).astype(np.int32)
    row_i = np.round(row).astype(np.int32)

    in_bounds = (col_i >= 0) & (col_i < src_w) & (row_i >= 0) & (row_i < src_h)
    col_c = np.clip(col_i, 0, src_w - 1)
    row_c = np.clip(row_i, 0, src_h - 1)
    sampled = raw[row_c, col_c]

    has_echo = sampled != decoded["undetect"]
    has_data = sampled != decoded["nodata"]
    valid = in_bounds & has_data & has_echo

    dbz = sampled.astype(np.float32) * decoded["gain"] + decoded["offset"]
    dbz[~valid] = 0.0
    return dbz, valid


def dbz_grid_for(path_or_bytes) -> tuple:
    """Decode + reproject an HDF5 composite straight to our output dbz/valid
    grid — the shared representation both the observed-frame renderer and
    the advection nowcast (nowcast.py) work with."""
    decoded = decode_h5(path_or_bytes)
    return reproject_to_dbz_grid(
        decoded, OUT_WIDTH, OUT_HEIGHT,
        OUT_LON_MIN, OUT_LON_MAX, OUT_LAT_MIN, OUT_LAT_MAX,
    )


def dbz_grid_for_work(path_or_bytes) -> tuple:
    """Same as dbz_grid_for, but reprojected onto the padded working box
    (see FCST_MARGIN_* above) instead of the display box. Used by the
    forecast pipeline so that shifting the field forward reveals real data
    from the padding instead of a blank edge."""
    decoded = decode_h5(path_or_bytes)
    return reproject_to_dbz_grid(
        decoded, WORK_WIDTH, WORK_HEIGHT,
        WORK_LON_MIN, WORK_LON_MAX, WORK_LAT_MIN, WORK_LAT_MAX,
    )


def crop_to_display(dbz: np.ndarray, valid: np.ndarray) -> tuple:
    """Crop a padded working-box grid back down to the standard OUT_WIDTH x
    OUT_HEIGHT display box, so forecast PNGs line up pixel-for-pixel with
    observed ones."""
    y0, y1 = MARGIN_PX_Y, MARGIN_PX_Y + OUT_HEIGHT
    x0, x1 = MARGIN_PX_X, MARGIN_PX_X + OUT_WIDTH
    return dbz[y0:y1, x0:x1], valid[y0:y1, x0:x1]


def png_from_grid(dbz, valid) -> bytes:
    rgba = colorize(dbz, valid)
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_png_bytes(path_or_bytes) -> bytes:
    dbz, valid = dbz_grid_for(path_or_bytes)
    return png_from_grid(dbz, valid)

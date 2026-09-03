"""Synthetic DMI-shaped test fixtures: builds small, self-consistent
ODIM_H5 byte strings and a matching DMI /items API JSON payload, so the
test suite never depends on real network access or real DMI data.

A plain lon/lat "projection" (no actual projection at all) makes the
source grid's pixel <-> geographic-coordinate math trivial to reason about
by hand in tests, while still exercising the exact same pyproj.Transformer
code path reproject_to_dbz_grid uses for a real stereographic composite.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import h5py
import numpy as np

PROJDEF = "+proj=longlat +datum=WGS84 +no_defs"
GAIN, OFFSET = 0.5, -32.5
NODATA, UNDETECT = 255.0, 0.0

# Geometry shared by the synthetic composite: a 12deg x 6deg box (UL corner
# at 5E/59N, 0.01deg/px) comfortably covering render.py's OUT bbox
# (7-16E, 54.3-58.2N) with margin on every side.
UL_LON, UL_LAT = 5.0, 59.0
XSCALE, YSCALE = 0.01, 0.01
RAW_W, RAW_H = 1200, 600


def dbz_to_raw(dbz: float) -> int:
    return round((dbz - OFFSET) / GAIN)


def blank_raw(w: int = RAW_W, h: int = RAW_H) -> np.ndarray:
    return np.full((h, w), UNDETECT, dtype=np.uint8)


def place_signal(raw: np.ndarray, row: int, col: int, size: int, dbz: float) -> None:
    r0, r1 = max(0, row - size // 2), row + size // 2
    c0, c1 = max(0, col - size // 2), col + size // 2
    raw[r0:r1, c0:c1] = dbz_to_raw(dbz)


def lonlat_to_rawpx(lon: float, lat: float) -> tuple[int, int]:
    col = round((lon - UL_LON) / XSCALE)
    row = round((UL_LAT - lat) / YSCALE)
    return row, col


def make_h5_bytes(tmp_path: Path, name: str, ul_lon: float, ul_lat: float,
                   xscale: float, yscale: float, raw: np.ndarray) -> bytes:
    """Writes a minimal but structurally real ODIM_H5 composite (the exact
    groups/attrs render.decode_h5 reads) and returns its bytes."""
    path = tmp_path / name
    with h5py.File(path, "w") as f:
        f.create_dataset("dataset1/data1/data", data=raw.astype(np.uint8))
        what = f.create_group("what")
        what.attrs["gain"] = GAIN
        what.attrs["offset"] = OFFSET
        what.attrs["nodata"] = NODATA
        what.attrs["undetect"] = UNDETECT
        where = f.create_group("where")
        where.attrs["projdef"] = PROJDEF.encode()
        where.attrs["UL_lon"] = ul_lon
        where.attrs["UL_lat"] = ul_lat
        where.attrs["xscale"] = xscale
        where.attrs["yscale"] = yscale
    return path.read_bytes()


def make_composite_h5_bytes(tmp_path: Path, name: str, signal_lon: float, signal_lat: float,
                             signal_dbz: float = 30.0, signal_size: int = 24) -> bytes:
    raw = blank_raw()
    row, col = lonlat_to_rawpx(signal_lon, signal_lat)
    place_signal(raw, row, col, signal_size, signal_dbz)
    return make_h5_bytes(tmp_path, name, UL_LON, UL_LAT, XSCALE, YSCALE, raw)


def make_items_payload(frames: list[dict]) -> bytes:
    """frames: [{"id": ..., "datetime": ..., "href": ...}, ...]"""
    features = [
        {
            "id": f["id"],
            "properties": {"datetime": f["datetime"], "scanType": f.get("scanType", "fullRange")},
            "asset": {"data": {"href": f["href"]}},
        }
        for f in frames
    ]
    return json.dumps({"features": features}).encode()


class FakeResponse:
    """Enough of urllib's response object for decode_h5/json.load to work:
    a readable, context-manager-compatible byte source."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def make_fake_urlopen(items_url: str, items_bytes: bytes, hrefs: dict[str, bytes], strict: bool = True):
    """Drop-in replacement for urllib.request.urlopen: serves `items_bytes`
    for the items-list URL and each href's bytes for that href.

    strict=True (the default, for coordinator/HA-layer tests where only
    DMI calls are ever legitimate): raises on anything else, so an
    unexpected real-network call fails loudly instead of hanging or
    silently reaching the internet.

    strict=False (for dev/server.py's own integration tests): falls
    through to the real urlopen for anything unrecognized. Those tests
    patch this at process/module scope (urllib.request.urlopen), which
    also covers the test's *own* client calls to the real local test
    server it starts — those need to actually reach it, not be faked.
    """
    real_urlopen = urllib.request.urlopen

    def _fake_urlopen(url, timeout=None):  # noqa: ANN001 - matches urlopen's loose signature
        if url == items_url:
            return FakeResponse(items_bytes)
        if url in hrefs:
            return FakeResponse(hrefs[url])
        if strict:
            raise AssertionError(f"unexpected URL requested in test: {url}")
        return real_urlopen(url, timeout=timeout)
    return _fake_urlopen


# Three observed frames 10 minutes apart, with a signal that drifts east
# over time (lon increasing) — gives the motion-field pipeline a real,
# non-zero, predictable-direction motion to find. Shared by every test
# that needs a ready-to-serve synthetic DMI dataset (dev/server.py and the
# HA coordinator/views/setup tests alike).
DEFAULT_FRAME_TIMES = ["2026-01-01T11:40:00Z", "2026-01-01T11:50:00Z", "2026-01-01T12:00:00Z"]
DEFAULT_SIGNAL_LONS = [10.0, 10.3, 10.6]


def make_default_dataset(tmp_path: Path) -> tuple[list[dict], dict[str, bytes]]:
    frames, hrefs = [], {}
    for i, (t, lon) in enumerate(zip(DEFAULT_FRAME_TIMES, DEFAULT_SIGNAL_LONS)):
        frame_id = f"frame-{i}"
        href = f"https://fake.dmi.test/{frame_id}.h5"
        hrefs[href] = make_composite_h5_bytes(tmp_path, f"{frame_id}.h5", lon, 56.0, 30.0)
        frames.append({"id": frame_id, "datetime": t, "href": href})
    return frames, hrefs

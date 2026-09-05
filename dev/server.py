"""Local dev/test harness for the DMI radar pipeline. No Home Assistant
involved: run this, open dev/index.html (served by this same process) in a
browser, and inspect the rendered/aligned radar frames directly.

    python dev/server.py

Then visit http://localhost:8765/

See CLAUDE.md for the load-time optimizations implemented here.
"""
from __future__ import annotations

import ctypes
import gc
import io
import json
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from render import (  # noqa: E402
    decode_h5, reproject_to_dbz_grid, crop_to_display, png_from_grid,
    OUT_WIDTH, OUT_HEIGHT, OUT_LON_MIN, OUT_LON_MAX, OUT_LAT_MIN, OUT_LAT_MAX,
    WORK_WIDTH, WORK_HEIGHT, WORK_LON_MIN, WORK_LON_MAX, WORK_LAT_MIN, WORK_LAT_MAX,
)
from nowcast import estimate_consensus_anchor, estimate_motion_field, forecast_steps_from_field  # noqa: E402

PORT = 8765
ITEMS_URL = (
    "https://opendataapi.dmi.dk/v1/radardata/collections/composite/items"
    "?limit=40&sortorder=datetime,DESC"
)
HIST_FRAMES = 13  # ~2h at 10-min steps, matching the previous TV2-based design
FCST_FRAMES = 9   # 90 min ahead at 10-min steps (advection nowcast — see dev/nowcast.py)
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
FRAME_CACHE_SECONDS = 86400  # rendered frame content is immutable once generated for a given id


def fetch_latest_items() -> list[dict]:
    """Server-side call to DMI's API — no CORS involved, this isn't a browser."""
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


# --- decoded-HDF5 cache -----------------------------------------------------
# Observed frames get downloaded+decoded once for their own image endpoint,
# and the two motion-baseline frames used by the forecast pipeline are
# *also* almost always among those same observed items. Without this cache
# each of those files was being downloaded and HDF5-decoded twice (once at
# display resolution, once at the padded WORK resolution) every cycle.
_decoded_cache: dict[str, dict] = {}
_decoded_lock = threading.Lock()


def get_decoded(item: dict) -> dict:
    frame_id = item["id"]
    with _decoded_lock:
        cached = _decoded_cache.get(frame_id)
    if cached is not None:
        return cached
    h5_bytes = _download(item["asset"]["data"]["href"])
    decoded = decode_h5(io.BytesIO(h5_bytes))
    with _decoded_lock:
        _decoded_cache[frame_id] = decoded
    return decoded


def prune_decoded_cache(current_ids: set[str]) -> None:
    with _decoded_lock:
        for stale_id in list(_decoded_cache):
            if stale_id not in current_ids:
                del _decoded_cache[stale_id]


def get_or_render(item: dict) -> bytes:
    frame_id = item["id"]
    cache_path = CACHE_DIR / f"{frame_id}.png"
    if cache_path.exists():
        return cache_path.read_bytes()
    decoded = get_decoded(item)
    dbz, valid = reproject_to_dbz_grid(decoded, OUT_WIDTH, OUT_HEIGHT, OUT_LON_MIN, OUT_LON_MAX, OUT_LAT_MIN, OUT_LAT_MAX)
    png_bytes = png_from_grid(dbz, valid)
    cache_path.write_bytes(png_bytes)
    return png_bytes


def get_grid_work(item: dict):
    """Padded-working-box grid for the forecast pipeline — see
    dbz_grid_for_work / FCST_MARGIN_* in render.py. Reuses the decoded-HDF5
    cache instead of downloading/decoding again."""
    decoded = get_decoded(item)
    return reproject_to_dbz_grid(decoded, WORK_WIDTH, WORK_HEIGHT, WORK_LON_MIN, WORK_LON_MAX, WORK_LAT_MIN, WORK_LAT_MAX)


MOTION_BASELINE_STEPS = 2  # estimate motion over a 20-min gap (items[-3] -> items[-1]),
                           # not the immediately-preceding 10-min pair — a short baseline
                           # is noise-dominated and prone to spuriously reading as ~0


def _compute_forecast_sync(items: list[dict]) -> list[dict]:
    """The actual forecast work: downloads (unless already cached — see
    get_decoded), padded reprojections, an FFT motion estimate pooled
    across the whole observed history (see estimate_consensus_anchor),
    and 9 shift+render steps. Expensive (~1.3s+) — see
    get_forecast_entries() below for how callers avoid paying this on
    every poll."""
    if len(items) <= MOTION_BASELINE_STEPS:
        return []

    # The anchor driving the field's overall direction/speed comes from
    # pooling tile measurements across the *whole* observed history
    # (items, ~13 frames / ~2h at DMI's ~10-min cadence) rather than just
    # the baseline/curr pair below — a single pair can show genuinely
    # conflicting tile motion on a complex weather day that no
    # aggregation of *that pair alone* can resolve; see
    # estimate_consensus_anchor's docstring. estimate_consensus_anchor
    # returns per-single-step (~10-min) units; scale up by
    # MOTION_BASELINE_STEPS to match the baseline/curr pair's own
    # duration, which is what estimate_motion_field's per-tile deviation
    # clamping is calibrated against.
    history_frames = [get_grid_work(it) for it in items]
    dbz_base, valid_base = history_frames[-1 - MOTION_BASELINE_STEPS]
    dbz_curr, valid_curr = history_frames[-1]
    curr_item = items[-1]
    anchor = estimate_consensus_anchor(history_frames)
    anchor_override = (
        (anchor[0] * MOTION_BASELINE_STEPS, anchor[1] * MOTION_BASELINE_STEPS)
        if anchor is not None else None
    )

    # Piecewise (tile-based) motion field instead of one global vector — see
    # dev/nowcast.py and CLAUDE.md for why: a single vector can only slide
    # the whole frame rigidly, and can't represent different regions of the
    # sky moving differently.
    dy_field_baseline, dx_field_baseline = estimate_motion_field(
        dbz_base, valid_base, dbz_curr, valid_curr, anchor_override=anchor_override)
    dy_field = dy_field_baseline / MOTION_BASELINE_STEPS
    dx_field = dx_field_baseline / MOTION_BASELINE_STEPS
    steps = forecast_steps_from_field(dbz_curr, valid_curr, dy_field, dx_field, FCST_FRAMES)

    base_time = datetime.fromisoformat(curr_item["properties"]["datetime"].replace("Z", "+00:00"))
    entries = []
    for i, (dbz_work, valid_work) in enumerate(steps, start=1):
        dbz, valid = crop_to_display(dbz_work, valid_work)
        t = base_time + timedelta(minutes=10 * i)
        fid = "forecast-" + t.strftime("%Y%m%d%H%M")
        cache_path = CACHE_DIR / f"{fid}.png"
        # Always overwrite, never skip-if-exists: unlike observed frames,
        # a given absolute future timestamp gets computed as a *different*
        # step number (different accumulated displacement, from a
        # different/fresher motion field) across successive refreshes as
        # curr advances -- e.g. today's +90min step becomes tomorrow's
        # +80min step once curr catches up by 10 min. This function only
        # runs when get_forecast_entries has already decided a recompute is
        # warranted, so every step here is authoritative and must replace
        # whatever an older batch previously guessed for the same fid.
        # (Skip-if-exists here previously left one stale, larger-looking
        # step sitting at the end of an otherwise-fresh sequence.)
        cache_path.write_bytes(png_from_grid(dbz, valid))
        entries.append({
            "id": fid,
            "time": t.strftime("%Y-%m-%dT%H:%M:00Z"),
            "forecast": True,
        })
    return entries


# --- forecast result cache + background refresh -----------------------------
# Keyed on (baseline_item.id, curr_item.id): as long as that pair hasn't
# changed, the previous result is still correct (DMI hasn't published
# anything new), so repeat /api/frames polls — which happen every 60s while
# new data only shows up every ~10min — return instantly instead of redoing
# the ~1.3s of work above. The very first computation still blocks (so the
# first response actually has forecast data); every later change recomputes
# in a background thread while continuing to serve the last-good result.
_forecast_lock = threading.Lock()
_forecast_state = {"key": None, "entries": []}
_forecast_computing_key = None


def prune_png_cache(current_ids: set[str]) -> None:
    """Deletes cached PNGs whose frame id has scrolled out of the current
    serving window. Without this, every ~10-min DMI publish leaves one new
    observed-frame PNG and one new forecast-frame PNG that never get
    cleaned up — unbounded growth (~35 GB/year measured against live DMI
    data; see CLAUDE.md).

    Only ever called right after a fresh (items, forecast) window has been
    fully computed and is about to become authoritative (see the
    first_ever branch and _forecast_worker below) — never from a
    per-request handler. Pruning against a *stale* id set while a
    background recompute is still in flight would delete that recompute's
    just-written output before _forecast_state is updated to include it,
    turning into self-inflicted cache misses."""
    for path in CACHE_DIR.glob("*.png"):
        if path.stem not in current_ids:
            path.unlink(missing_ok=True)


def _trim_memory() -> None:
    """Returns freed heap arenas to the OS after a forecast recompute's
    FFT/reprojection work. CPython/glibc otherwise keep that memory
    reserved for reuse within the process rather than releasing it — fine
    while actively computing, but it means RSS stays at its peak between
    polls too (see CLAUDE.md). Best-effort: quietly does nothing on
    non-glibc platforms."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass  # libc.so.6 itself not found (e.g. not Linux)
    except AttributeError:
        pass  # loaded a libc without the malloc_trim symbol (e.g. musl)


def _forecast_worker(items: list[dict], key: tuple) -> None:
    global _forecast_computing_key
    try:
        entries = _compute_forecast_sync(items)
        with _forecast_lock:
            _forecast_state["key"] = key
            _forecast_state["entries"] = entries
        prune_png_cache({it["id"] for it in items} | {e["id"] for e in entries})
        _trim_memory()
    except Exception as e:
        sys.stderr.write(f"[dev-server] background forecast refresh failed: {e}\n")
    finally:
        with _forecast_lock:
            _forecast_computing_key = None


def get_forecast_entries(items: list[dict]) -> list[dict]:
    global _forecast_computing_key
    if len(items) <= MOTION_BASELINE_STEPS:
        return []
    baseline_item, curr_item = items[-1 - MOTION_BASELINE_STEPS], items[-1]
    key = (baseline_item["id"], curr_item["id"])

    with _forecast_lock:
        if _forecast_state["key"] == key:
            return list(_forecast_state["entries"])
        first_ever = _forecast_state["key"] is None
        already_computing = _forecast_computing_key == key

    if first_ever:
        # Nothing to serve yet at all — block so the first response has data.
        entries = _compute_forecast_sync(items)
        with _forecast_lock:
            _forecast_state["key"] = key
            _forecast_state["entries"] = entries
        prune_png_cache({it["id"] for it in items} | {e["id"] for e in entries})
        _trim_memory()
        return entries

    if not already_computing:
        with _forecast_lock:
            _forecast_computing_key = key
        threading.Thread(target=_forecast_worker, args=(items, key), daemon=True).start()

    with _forecast_lock:
        return list(_forecast_state["entries"])  # stale-but-fast, refreshing in the background


INDEX_HTML_PATH = Path(__file__).parent / "index.html"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[dev-server] " + (fmt % args) + "\n")

    def _send(self, status, content_type, body: bytes, cache_seconds: int | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_seconds is not None:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                self._send(200, "text/html; charset=utf-8", INDEX_HTML_PATH.read_bytes())
            elif path == "/api/frames":
                items = fetch_latest_items()
                prune_decoded_cache({it["id"] for it in items})
                payload = [
                    {"id": it["id"], "time": it["properties"]["datetime"], "forecast": False}
                    for it in items
                ]
                payload.extend(get_forecast_entries(items))
                self._send(200, "application/json", json.dumps(payload).encode())
            elif path.startswith("/api/frame/"):
                frame_id = path[len("/api/frame/"):].removesuffix(".png")
                if frame_id.startswith("forecast-"):
                    cache_path = CACHE_DIR / f"{frame_id}.png"
                    if not cache_path.exists():
                        self._send(404, "text/plain", b"forecast frame expired (recompute via /api/frames)")
                        return
                    self._send(200, "image/png", cache_path.read_bytes(), cache_seconds=FRAME_CACHE_SECONDS)
                    return
                items = fetch_latest_items()
                match = next((it for it in items if it["id"] == frame_id), None)
                if not match:
                    self._send(404, "text/plain", b"frame not found (outside current window)")
                    return
                png = get_or_render(match)
                self._send(200, "image/png", png, cache_seconds=FRAME_CACHE_SECONDS)
            else:
                self._send(404, "text/plain", b"not found")
        except Exception as e:  # dev harness: surface errors plainly
            self._send(500, "text/plain", f"error: {e}".encode())


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Weather radar dev harness: http://127.0.0.1:{PORT}/")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

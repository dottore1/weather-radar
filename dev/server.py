"""Local dev/test harness for the DMI radar pipeline. No Home Assistant
involved: run this, open dev/index.html (served by this same process) in a
browser, and inspect the rendered/aligned radar frames directly.

    python dev/server.py

Then visit http://localhost:8765/
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from render import render_png_bytes, dbz_grid_for, png_from_grid  # noqa: E402
from nowcast import estimate_motion, forecast_steps_from_motion  # noqa: E402

PORT = 8765
ITEMS_URL = (
    "https://opendataapi.dmi.dk/v1/radardata/collections/composite/items"
    "?limit=40&sortorder=datetime,DESC"
)
HIST_FRAMES = 13  # ~2h at 10-min steps, matching the previous TV2-based design
FCST_FRAMES = 9   # 90 min ahead at 10-min steps (advection nowcast — see dev/nowcast.py)
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


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


def get_or_render(item: dict) -> bytes:
    frame_id = item["id"]
    cache_path = CACHE_DIR / f"{frame_id}.png"
    if cache_path.exists():
        return cache_path.read_bytes()
    import io
    h5_bytes = _download(item["asset"]["data"]["href"])
    png_bytes = render_png_bytes(io.BytesIO(h5_bytes))
    cache_path.write_bytes(png_bytes)
    return png_bytes


def get_grid(item: dict):
    import io
    h5_bytes = _download(item["asset"]["data"]["href"])
    return dbz_grid_for(io.BytesIO(h5_bytes))


MOTION_BASELINE_STEPS = 2  # estimate motion over a 20-min gap (items[-3] -> items[-1]),
                           # not the immediately-preceding 10-min pair — a short baseline
                           # is noise-dominated and prone to spuriously reading as ~0


def build_forecast(items: list[dict]) -> list[dict]:
    """Advection nowcast — see dev/nowcast.py. Returns frame-list entries in
    the same shape as the observed ones, plus caches each forecast PNG to
    disk."""
    if len(items) <= MOTION_BASELINE_STEPS:
        return []
    baseline_item, curr_item = items[-1 - MOTION_BASELINE_STEPS], items[-1]
    dbz_base, valid_base = get_grid(baseline_item)
    dbz_curr, valid_curr = get_grid(curr_item)
    dy_baseline, dx_baseline = estimate_motion(dbz_base, valid_base, dbz_curr, valid_curr)
    # per-10-min-step motion: the baseline spans MOTION_BASELINE_STEPS steps
    dy = round(dy_baseline / MOTION_BASELINE_STEPS)
    dx = round(dx_baseline / MOTION_BASELINE_STEPS)
    steps = forecast_steps_from_motion(dbz_curr, valid_curr, dy, dx, FCST_FRAMES)

    base_time = datetime.fromisoformat(curr_item["properties"]["datetime"].replace("Z", "+00:00"))
    entries = []
    for i, (dbz, valid) in enumerate(steps, start=1):
        t = base_time + timedelta(minutes=10 * i)
        fid = "forecast-" + t.strftime("%Y%m%d%H%M")
        cache_path = CACHE_DIR / f"{fid}.png"
        if not cache_path.exists():
            cache_path.write_bytes(png_from_grid(dbz, valid))
        entries.append({
            "id": fid,
            "time": t.strftime("%Y-%m-%dT%H:%M:00Z"),
            "forecast": True,
        })
    return entries


INDEX_HTML_PATH = Path(__file__).parent / "index.html"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[dev-server] " + (fmt % args) + "\n")

    def _send(self, status, content_type, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                self._send(200, "text/html; charset=utf-8", INDEX_HTML_PATH.read_bytes())
            elif path == "/api/frames":
                items = fetch_latest_items()
                payload = [
                    {"id": it["id"], "time": it["properties"]["datetime"], "forecast": False}
                    for it in items
                ]
                payload.extend(build_forecast(items))
                self._send(200, "application/json", json.dumps(payload).encode())
            elif path.startswith("/api/frame/"):
                frame_id = path[len("/api/frame/"):].removesuffix(".png")
                if frame_id.startswith("forecast-"):
                    cache_path = CACHE_DIR / f"{frame_id}.png"
                    if not cache_path.exists():
                        self._send(404, "text/plain", b"forecast frame expired (recompute via /api/frames)")
                        return
                    self._send(200, "image/png", cache_path.read_bytes())
                    return
                items = fetch_latest_items()
                match = next((it for it in items if it["id"] == frame_id), None)
                if not match:
                    self._send(404, "text/plain", b"frame not found (outside current window)")
                    return
                png = get_or_render(match)
                self._send(200, "image/png", png)
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

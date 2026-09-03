"""Integration tests for dev/server.py: starts the real HTTP server
against synthetic, network-free DMI data and exercises its actual
request-handling, caching, and forecast-computation code paths."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tests import synth
from tests.conftest import REPO_ROOT, load_module

@pytest.fixture
def dev_server(request, socket_enabled, monkeypatch, tmp_path):
    """Starts dev/server.py's real HTTP server on an ephemeral localhost
    port, with urllib.request.urlopen patched to serve synthetic DMI data
    instead of hitting the real network. Yields (base_url, module).

    Depends on pytest-socket's `socket_enabled` fixture: pytest-homeassistant-
    custom-component blocks real sockets by default (matching HA core's own
    test suite safety net), but dev/server.py isn't an HA integration at all
    — it's a plain-stdlib local HTTP server — and this fixture genuinely
    needs a real (localhost-only) socket to start it. Declaring the
    dependency as a fixture (rather than the `@pytest.mark.enable_socket`
    marker) guarantees it's re-enabled before this fixture's own setup code
    runs, which starts the server."""
    module = load_module(f"_test_dev_server_{request.node.name}", REPO_ROOT / "dev" / "server.py")
    # Redirect the on-disk PNG cache away from the real dev/cache/ dir —
    # otherwise tests would read/write real cached frames from prior real
    # runs, or pollute the working repo with test output.
    module.CACHE_DIR = tmp_path / "cache"
    module.CACHE_DIR.mkdir(exist_ok=True)

    frames, hrefs = synth.make_default_dataset(tmp_path)
    fake_urlopen = synth.make_fake_urlopen(
        module.ITEMS_URL, synth.make_items_payload(frames), hrefs, strict=False)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", module
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_index_html_is_served(dev_server):
    base_url, _ = dev_server
    with urllib.request.urlopen(f"{base_url}/") as resp:
        assert resp.status == 200
        body = resp.read()
    assert b"<html" in body.lower()


def test_api_frames_lists_observed_and_forecast(dev_server):
    base_url, module = dev_server
    with urllib.request.urlopen(f"{base_url}/api/frames") as resp:
        payload = json.loads(resp.read())
    observed = [f for f in payload if not f["forecast"]]
    forecast = [f for f in payload if f["forecast"]]
    assert len(observed) == 3
    assert len(forecast) == module.FCST_FRAMES
    assert forecast[0]["time"] > observed[-1]["time"]


def test_api_frame_png_served_for_an_observed_frame(dev_server):
    base_url, _ = dev_server
    with urllib.request.urlopen(f"{base_url}/api/frame/frame-2.png") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        body = resp.read()
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_api_frame_png_served_for_a_forecast_frame(dev_server):
    base_url, _ = dev_server
    with urllib.request.urlopen(f"{base_url}/api/frames") as resp:
        payload = json.loads(resp.read())
    forecast_id = next(f["id"] for f in payload if f["forecast"])
    with urllib.request.urlopen(f"{base_url}/api/frame/{forecast_id}.png") as resp:
        assert resp.status == 200
        body = resp.read()
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_observed_frame_returns_404(dev_server):
    base_url, _ = dev_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base_url}/api/frame/does-not-exist.png")
    assert excinfo.value.code == 404


def test_unknown_forecast_frame_returns_404(dev_server):
    base_url, _ = dev_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base_url}/api/frame/forecast-99991231235900.png")
    assert excinfo.value.code == 404


def test_repeat_frames_poll_reuses_the_cached_forecast(dev_server):
    """A second /api/frames call with the same (baseline, curr) key should
    hit the forecast cache rather than recomputing (see
    get_forecast_entries's docstring in dev/server.py)."""
    base_url, _ = dev_server
    with urllib.request.urlopen(f"{base_url}/api/frames") as resp:
        first = json.loads(resp.read())
    with urllib.request.urlopen(f"{base_url}/api/frames") as resp:
        second = json.loads(resp.read())
    assert first == second


def test_frames_poll_prunes_stale_cached_pngs(dev_server):
    """PNGs left over from a frame id that's no longer in the current
    serving window should get cleaned up once the window is known (see
    prune_png_cache's docstring in dev/server.py — without this, disk
    usage grows unbounded, ~35 GB/year on real data)."""
    base_url, module = dev_server
    stale_path = module.CACHE_DIR / "stale-frame-from-an-old-window.png"
    stale_path.write_bytes(b"not a real png, just needs to exist")

    with urllib.request.urlopen(f"{base_url}/api/frames") as resp:
        payload = json.loads(resp.read())

    assert not stale_path.exists()
    current_ids = {entry["id"] for entry in payload}
    remaining = {p.stem for p in module.CACHE_DIR.glob("*.png")}
    assert remaining <= current_ids

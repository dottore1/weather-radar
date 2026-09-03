"""Simple advection nowcast: estimate a single global motion vector between
two consecutive reflectivity grids (FFT phase correlation) and extrapolate
the most recent grid forward by repeating that motion.

This is Lagrangian persistence — assumes the rain keeps moving the way it's
currently moving, with no growth/decay modeling. Good enough for ~90 min,
degrades toward the end of that window. Computed entirely from DMI's own
licensed composite data (see PLAN-DMI-MIGRATION.md).
"""
from __future__ import annotations

import numpy as np


def _hann2d(shape: tuple[int, int]) -> np.ndarray:
    wy = np.hanning(shape[0])
    wx = np.hanning(shape[1])
    return np.outer(wy, wx)


def compute_motion(dbz_prev: np.ndarray, dbz_curr: np.ndarray) -> tuple[int, int]:
    """Returns (dy, dx) in pixels: the shift such that dbz_curr ~= dbz_prev
    shifted by (dy, dx). I.e. applying this same shift again extrapolates
    one more step forward."""
    a = dbz_prev.astype(np.float64)
    b = dbz_curr.astype(np.float64)
    a = a - a.mean()
    b = b - b.mean()

    # Radar composites have a fixed coverage-boundary edge (data -> no-data)
    # that's geographically static between frames. A hard edge is a very
    # strong, perfectly-stationary signal that can dominate phase
    # correlation and pull the estimate toward zero shift even when the
    # actual rain clearly moved. Taper toward the array edges to suppress
    # that (standard fix for edge artifacts in phase correlation).
    window = _hann2d(a.shape)
    a = a * window
    b = b * window

    fa = np.fft.fft2(a)
    fb = np.fft.fft2(b)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-8
    corr = np.abs(np.fft.ifft2(r))
    corr = np.fft.fftshift(corr)

    h, w = corr.shape
    py, px = np.unravel_index(np.argmax(corr), corr.shape)
    # Empirically verified against a synthetic known-shift test (see
    # __main__ below) — the raw peak position is the negative of the shift
    # that takes prev -> curr, hence the sign flip.
    dy = -(py - h // 2)
    dx = -(px - w // 2)
    return int(dy), int(dx)


def shift_grid(dbz: np.ndarray, valid: np.ndarray, dy: int, dx: int) -> tuple[np.ndarray, np.ndarray]:
    """Shift (dbz, valid) by integer (dy, dx) pixels. Area revealed at the
    leading edge (nothing known moved in from outside the frame) is left as
    no-data/transparent rather than wrapping or extrapolating garbage."""
    h, w = dbz.shape
    out_dbz = np.zeros_like(dbz)
    out_valid = np.zeros_like(valid)

    src_y0, src_y1 = max(0, -dy), min(h, h - dy)
    dst_y0, dst_y1 = max(0, dy), min(h, h + dy)
    src_x0, src_x1 = max(0, -dx), min(w, w - dx)
    dst_x0, dst_x1 = max(0, dx), min(w, w + dx)

    if src_y1 > src_y0 and src_x1 > src_x0:
        out_dbz[dst_y0:dst_y1, dst_x0:dst_x1] = dbz[src_y0:src_y1, src_x0:src_x1]
        out_valid[dst_y0:dst_y1, dst_x0:dst_x1] = valid[src_y0:src_y1, src_x0:src_x1]
    return out_dbz, out_valid


def _echo_bbox(valid_a: np.ndarray, valid_b: np.ndarray, margin: int = 20):
    """Bounding box (y0, y1, x0, x1) around wherever either frame has an
    echo, padded by margin. Most of a composite grid is empty (no
    precipitation) — a plain whole-field phase correlation lets that huge
    flat background dominate and pulls the estimate toward zero shift, even
    when the (smaller) rain area clearly moved. Cropping to just the active
    region fixes that."""
    combined = valid_a | valid_b
    if not combined.any():
        return None
    ys, xs = np.where(combined)
    h, w = combined.shape
    y0, y1 = max(0, ys.min() - margin), min(h, ys.max() + margin + 1)
    x0, x1 = max(0, xs.min() - margin), min(w, xs.max() + margin + 1)
    return y0, y1, x0, x1


def estimate_motion(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                     dbz_curr: np.ndarray, valid_curr: np.ndarray) -> tuple[int, int]:
    bbox = _echo_bbox(valid_prev, valid_curr)
    if bbox is None:
        return 0, 0  # no echo in either frame — nothing to track
    y0, y1, x0, x1 = bbox
    return compute_motion(dbz_prev[y0:y1, x0:x1], dbz_curr[y0:y1, x0:x1])


def forecast_steps_from_motion(dbz_curr: np.ndarray, valid_curr: np.ndarray,
                                dy: int, dx: int, n_steps: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Returns n_steps forecast (dbz, valid) grids, each shifted by one more
    (dy, dx) than the last, starting from dbz_curr."""
    out = []
    dbz, valid = dbz_curr, valid_curr
    for _ in range(n_steps):
        dbz, valid = shift_grid(dbz, valid, dy, dx)
        out.append((dbz, valid))
    return out


def forecast_steps(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                    dbz_curr: np.ndarray, valid_curr: np.ndarray,
                    n_steps: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Convenience wrapper: estimate motion directly from one prev/curr pair
    and extrapolate. See build_forecast() in dev/server.py for the version
    used in practice, which estimates motion over a longer (20-min) baseline
    for a less noise-dominated per-step vector."""
    dy, dx = estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)
    return forecast_steps_from_motion(dbz_curr, valid_curr, dy, dx, n_steps)


if __name__ == "__main__":
    # Self-test 1: dense field (no empty background), confirms sign convention.
    rng = np.random.default_rng(0)
    base = rng.random((80, 100))
    true_dy, true_dx = 3, -5
    shifted, _ = shift_grid(base, np.ones_like(base, dtype=bool), true_dy, true_dx)
    est_dy, est_dx = compute_motion(base, shifted)
    print(f"dense field: true=({true_dy},{true_dx}) estimated=({est_dy},{est_dx})")
    assert (est_dy, est_dx) == (true_dy, true_dx), "sign convention mismatch"

    # Self-test 2: a static "coverage boundary" (a fixed-position hard edge,
    # identical in both frames — exactly what a real radar composite's
    # data/no-data edge looks like) plus a small storm cell that actually
    # moves. A hard edge is a very strong, perfectly-stationary broadband
    # signal that dominates naive phase correlation and pulls the estimate
    # toward zero shift, even though the smaller rain area clearly moved.
    h, w = 200, 200
    true_dy, true_dx = 4, 6
    coverage = np.zeros((h, w), dtype=bool)
    coverage[20:180, 20:180] = True  # static rectangle, identical in both frames

    field_prev = np.zeros((h, w))
    field_prev[coverage] = 5.0  # baseline in-coverage value
    field_prev[80:100, 80:100] = 30.0  # storm cell
    field_curr = np.zeros((h, w))
    field_curr[coverage] = 5.0
    field_curr[80 + true_dy:100 + true_dy, 80 + true_dx:100 + true_dx] = 30.0  # moved
    valid_prev, valid_curr = coverage.copy(), coverage.copy()

    naive_dy, naive_dx = compute_motion(field_prev, field_curr)
    est_dy, est_dx = estimate_motion(field_prev, valid_prev, field_curr, valid_curr)
    print(f"coverage-edge field: true=({true_dy},{true_dx}) naive={naive_dy,naive_dx} cropped+windowed={est_dy,est_dx}")
    assert (est_dy, est_dx) == (true_dy, true_dx), "cropped+windowed estimate should recover the true shift"

    print("OK")

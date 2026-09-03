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


def compute_motion(dbz_prev: np.ndarray, dbz_curr: np.ndarray) -> tuple[int, int]:
    """Returns (dy, dx) in pixels: the shift such that dbz_curr ~= dbz_prev
    shifted by (dy, dx). I.e. applying this same shift again extrapolates
    one more step forward."""
    a = dbz_prev.astype(np.float64)
    b = dbz_curr.astype(np.float64)
    a = a - a.mean()
    b = b - b.mean()

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


def forecast_steps(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                    dbz_curr: np.ndarray, valid_curr: np.ndarray,
                    n_steps: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Returns n_steps forecast (dbz, valid) grids, each one motion-step
    further ahead than the last, starting from dbz_curr."""
    dy, dx = compute_motion(dbz_prev, dbz_curr)
    out = []
    dbz, valid = dbz_curr, valid_curr
    for _ in range(n_steps):
        dbz, valid = shift_grid(dbz, valid, dy, dx)
        out.append((dbz, valid))
    return out


if __name__ == "__main__":
    # Self-test: synthetic field with a known shift, confirm sign convention.
    rng = np.random.default_rng(0)
    base = rng.random((80, 100))
    true_dy, true_dx = 3, -5
    shifted, _ = shift_grid(base, np.ones_like(base, dtype=bool), true_dy, true_dx)
    est_dy, est_dx = compute_motion(base, shifted)
    print(f"true=({true_dy},{true_dx}) estimated=({est_dy},{est_dx})")
    assert (est_dy, est_dx) == (true_dy, true_dx), "sign convention mismatch"
    print("OK")

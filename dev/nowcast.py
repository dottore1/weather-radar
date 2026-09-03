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
from PIL import Image


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
    and extrapolate. Superseded by forecast_steps_field() below (a single
    global vector can't represent different parts of the sky moving
    differently) — kept because it's simpler and the self-tests below still
    exercise the shared compute_motion()/estimate_motion() internals."""
    dy, dx = estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)
    return forecast_steps_from_motion(dbz_curr, valid_curr, dy, dx, n_steps)


# --- piecewise (tile-based) motion field -------------------------------
# A single global vector can only represent the whole sky translating
# rigidly in one direction. Real weather doesn't: a front over Jylland can
# move differently than a cell over Sjælland, flow can rotate/shear, cells
# grow/decay/split. This estimates a *spatially-varying* motion field by
# running the same per-tile phase correlation independently across
# overlapping tiles, then interpolating the sparse per-tile vectors into a
# smooth per-pixel field, and warps each output pixel by its own local
# velocity (semi-Lagrangian) instead of shifting the whole frame rigidly.

TILE_SIZE = 180
TILE_OVERLAP = 60
TILE_MIN_VALID_PX = 400  # ~12% of a 180x180 tile; below this a tile's own
                          # correlation is unreliable and we fall back instead
# A single small tile is much noisier than the full-frame correlation (less
# data, more prone to locking onto a spurious/aliased peak), and those
# outliers get amplified 9x by extrapolation and show up as long streak
# artifacts. Clamp to a generous but physically bounded ceiling (~120 km/h
# over the 20-min baseline, in our ~0.57 km/px working-grid resolution)
# before smoothing, on top of the neighborhood median filter below.
TILE_MAX_DISPLACEMENT_PX = 70
# How much of a measured tile's own vector to trust vs. the global (whole-
# frame) vector: 1.0 = fully independent per-tile motion (can look like the
# sky "expanding" rather than moving — see estimate_motion_field's
# docstring), 0.0 = collapses back to one rigid global shift. 0.6 keeps
# real per-region variation visible while anchoring everything to a shared
# overall drift.
TILE_BLEND_ALPHA = 0.6


def _tile_starts(total: int, tile_size: int, step: int) -> list[int]:
    if total <= tile_size:
        return [0]
    starts = list(range(0, total - tile_size + 1, step))
    if starts[-1] != total - tile_size:
        starts.append(total - tile_size)  # make sure the far edge is covered
    return starts


def _median_filter_grid(grid: np.ndarray, size: int = 3) -> np.ndarray:
    """NaN-aware neighborhood median filter over the small tile-vector grid.
    Must run *before* fallback-filling: fallback values are placeholders,
    not real measurements, so smoothing a real tile against a neighborhood
    full of fallback placeholders would just overwrite genuine signal with
    the fallback (this is what broke the opposing-cells self-test the first
    time around). Using nanmedian and only padding with NaN means a tile
    only gets pulled toward *actually measured* neighbors — an isolated real
    measurement with no real neighbors passes through unchanged, while a
    genuine single-tile noise spike surrounded by other real, agreeing
    measurements gets corrected. The tile grid is tiny (order ~10x15), so a
    plain Python double loop is plenty fast — not worth a scipy dependency
    for this."""
    h, w = grid.shape
    pad = size // 2
    padded = np.pad(grid, pad, mode="constant", constant_values=np.nan)
    out = np.empty_like(grid)
    for i in range(h):
        for j in range(w):
            neighborhood = padded[i:i + size, j:j + size]
            out[i, j] = np.nan if np.isnan(neighborhood).all() else np.nanmedian(neighborhood)
    return out


def _upsample_field(grid: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """Smoothly upsample a small (n_tiles_y, n_tiles_x) grid of vectors to a
    dense (out_h, out_w) per-pixel field via bilinear interpolation. Uses
    PIL (already a dependency) instead of adding scipy just for this."""
    if grid.shape == (1, 1):
        return np.full((out_h, out_w), grid[0, 0], dtype=np.float32)
    img = Image.fromarray(grid.astype(np.float32), mode="F")
    resized = img.resize((out_w, out_h), resample=Image.BILINEAR)
    return np.asarray(resized, dtype=np.float32)


def estimate_motion_field(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                           dbz_curr: np.ndarray, valid_curr: np.ndarray,
                           tile_size: int = TILE_SIZE, overlap: int = TILE_OVERLAP,
                           min_valid_px: int = TILE_MIN_VALID_PX,
                           blend_alpha: float = TILE_BLEND_ALPHA) -> tuple[np.ndarray, np.ndarray]:
    """Returns (dy_field, dx_field): dense per-pixel arrays, same shape as
    the input, giving a local motion vector at every pixel."""
    h, w = dbz_prev.shape
    step = tile_size - overlap
    y_starts = _tile_starts(h, tile_size, step)
    x_starts = _tile_starts(w, tile_size, step)

    # The global (cropped+windowed, whole-frame) estimate anchors the field:
    # every tile — measured or not — gets pulled toward this shared drift
    # (see blend step below), so the *whole* system visibly translates
    # together instead of the sky looking like it's expanding in place.
    fallback_dy, fallback_dx = estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)

    dy_grid = np.full((len(y_starts), len(x_starts)), np.nan)
    dx_grid = np.full((len(y_starts), len(x_starts)), np.nan)
    for iy, y0 in enumerate(y_starts):
        for ix, x0 in enumerate(x_starts):
            y1, x1 = y0 + tile_size, x0 + tile_size
            tv_prev = valid_prev[y0:y1, x0:x1]
            tv_curr = valid_curr[y0:y1, x0:x1]
            if int(tv_prev.sum()) + int(tv_curr.sum()) < min_valid_px:
                continue  # too little signal in this tile to trust a correlation
            tdy, tdx = compute_motion(dbz_prev[y0:y1, x0:x1], dbz_curr[y0:y1, x0:x1])
            if (tdy * tdy + tdx * tdx) ** 0.5 > TILE_MAX_DISPLACEMENT_PX:
                continue  # implausible spike — treat like "no reliable signal"
            dy_grid[iy, ix], dx_grid[iy, ix] = tdy, tdx

    # Smooth out single-tile noise (a tile can pass the magnitude clamp and
    # still be a spurious/wrong-direction outlier relative to its neighbors)
    # while the grid still distinguishes "real measurement" from "no data"
    # (NaN) — see _median_filter_grid's docstring for why this must happen
    # before the blend/fallback steps below, not after.
    dy_grid = _median_filter_grid(dy_grid)
    dx_grid = _median_filter_grid(dx_grid)

    # Blend measured tiles *toward* the global vector rather than trusting
    # them outright. Without this, a tile with a strong independent
    # measurement sitting next to a tile that has none (and would otherwise
    # jump straight to a possibly very different fallback value) creates a
    # spatially inconsistent field — and warping by an inconsistent field
    # doesn't translate a rain area as one piece, it stretches it (looks
    # like the sky "expanding" instead of moving). Blending keeps every
    # tile anchored to the same baseline drift, with only a bounded local
    # deviation layered on top — real per-region character, but the whole
    # field still moves together. blend_alpha=1 is the old "trust the tile
    # outright" behavior; 0 collapses back to the single global vector.
    dy_grid = np.where(np.isnan(dy_grid), np.nan, blend_alpha * dy_grid + (1 - blend_alpha) * fallback_dy)
    dx_grid = np.where(np.isnan(dx_grid), np.nan, blend_alpha * dx_grid + (1 - blend_alpha) * fallback_dx)

    # Whatever's still NaN (no real measurement anywhere in that tile's
    # neighborhood) gets the plain global vector — same as a fully-blended
    # measured tile would if it agreed exactly with the global estimate.
    dy_grid = np.where(np.isnan(dy_grid), fallback_dy, dy_grid)
    dx_grid = np.where(np.isnan(dx_grid), fallback_dx, dx_grid)

    return _upsample_field(dy_grid, h, w), _upsample_field(dx_grid, h, w)


def warp_by_field(dbz: np.ndarray, valid: np.ndarray,
                   dy_field: np.ndarray, dx_field: np.ndarray,
                   _yyxx: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Semi-Lagrangian backward sample: for each output pixel, look up the
    *local* velocity there and pull content from where it came from. Unlike
    shift_grid (one rigid shift for the whole frame), different pixels can
    sample from different displacement amounts — spatially-varying motion.
    _yyxx lets a caller doing this repeatedly (see forecast_steps_from_field)
    pass in a precomputed pixel-coordinate grid instead of rebuilding it —
    identical every call for a given shape, so recomputing it per-step is
    pure waste."""
    h, w = dbz.shape
    yy, xx = _yyxx if _yyxx is not None else np.mgrid[0:h, 0:w]
    src_y = np.round(yy - dy_field).astype(np.int32)
    src_x = np.round(xx - dx_field).astype(np.int32)
    in_bounds = (src_y >= 0) & (src_y < h) & (src_x >= 0) & (src_x < w)
    src_y_c = np.clip(src_y, 0, h - 1)
    src_x_c = np.clip(src_x, 0, w - 1)
    out_valid = in_bounds & valid[src_y_c, src_x_c]
    out_dbz = np.where(out_valid, dbz[src_y_c, src_x_c], 0.0)
    return out_dbz, out_valid


def forecast_steps_from_field(dbz_curr: np.ndarray, valid_curr: np.ndarray,
                               dy_field: np.ndarray, dx_field: np.ndarray,
                               n_steps: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """n_steps forecast (dbz, valid) grids from a per-pixel velocity field
    already scaled to "per 10-min step". Each step k re-warps from the
    *original* curr frame by k*field rather than iterating from the previous
    step's output, to avoid compounding resampling error over 9 steps
    (standard practice for semi-Lagrangian extrapolation)."""
    yyxx = np.mgrid[0:dbz_curr.shape[0], 0:dbz_curr.shape[1]]
    return [warp_by_field(dbz_curr, valid_curr, k * dy_field, k * dx_field, _yyxx=yyxx)
            for k in range(1, n_steps + 1)]


def forecast_steps_field(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                          dbz_curr: np.ndarray, valid_curr: np.ndarray,
                          n_steps: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Convenience wrapper, piecewise-motion version of forecast_steps()."""
    dy_field, dx_field = estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    return forecast_steps_from_field(dbz_curr, valid_curr, dy_field, dx_field, n_steps)


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

    # Self-test 3: two storm cells moving in *opposite* directions — exactly
    # the case a single global vector cannot represent (the best it can do
    # is average the two, getting both regions wrong), which is the whole
    # reason for the tile-based motion field.
    h, w = 600, 600
    left_dy, left_dx = 0, 12     # left cell moves right
    right_dy, right_dx = 0, -12  # right cell moves left — opposite direction

    dbz_prev = np.zeros((h, w))
    valid_prev = np.zeros((h, w), dtype=bool)
    dbz_prev[250:350, 80:180] = 30.0     # left cell
    dbz_prev[250:350, 420:520] = 30.0    # right cell
    valid_prev[250:350, 80:180] = True
    valid_prev[250:350, 420:520] = True

    dbz_curr = np.zeros((h, w))
    valid_curr = np.zeros((h, w), dtype=bool)
    dbz_curr[250 + left_dy:350 + left_dy, 80 + left_dx:180 + left_dx] = 30.0
    dbz_curr[250 + right_dy:350 + right_dy, 420 + right_dx:520 + right_dx] = 30.0
    valid_curr[250 + left_dy:350 + left_dy, 80 + left_dx:180 + left_dx] = True
    valid_curr[250 + right_dy:350 + right_dy, 420 + right_dx:520 + right_dx] = True

    global_dy, global_dx = estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)
    # blend_alpha=1: pure per-tile measurement, no shrinkage toward the
    # global vector — this is what "should the two regions actually
    # disagree at all" tests, independent of the blend feature itself.
    dy_field_pure, dx_field_pure = estimate_motion_field(
        dbz_prev, valid_prev, dbz_curr, valid_curr, blend_alpha=1.0)
    field_dx_left_pure = dx_field_pure[300, 130]
    field_dx_right_pure = dx_field_pure[300, 470]
    print(f"opposing cells (blend_alpha=1): global=({global_dy},{global_dx}) "
          f"field@left_dx={field_dx_left_pure:.1f} (true {left_dx}) "
          f"field@right_dx={field_dx_right_pure:.1f} (true {right_dx})")
    assert abs(field_dx_left_pure - left_dx) <= 2, "field should recover the left cell's own motion"
    assert abs(field_dx_right_pure - right_dx) <= 2, "field should recover the right cell's own motion"
    assert field_dx_left_pure > 0 and field_dx_right_pure < 0, "the two regions should disagree in sign, unlike a global vector"

    # Default blend_alpha (<1): local measurements get pulled toward the
    # global vector rather than trusted outright (see TILE_BLEND_ALPHA) —
    # exact magnitude recovery is intentionally traded away for a field
    # that stays anchored to one consistent overall drift, so what matters
    # here is that the two regions *still* end up on opposite sides of the
    # global estimate, not that they hit their true value exactly.
    dy_field, dx_field = estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    field_dx_left = dx_field[300, 130]
    field_dx_right = dx_field[300, 470]
    print(f"opposing cells (blend_alpha={TILE_BLEND_ALPHA}): "
          f"field@left_dx={field_dx_left:.1f} field@right_dx={field_dx_right:.1f}")
    assert field_dx_left > global_dx, "blended left-cell vector should still lean toward its own (rightward) motion"
    assert field_dx_right <= global_dx, "blended right-cell vector should stay at/below the global drift"

    print("OK")

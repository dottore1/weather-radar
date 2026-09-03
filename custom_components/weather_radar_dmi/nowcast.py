"""Simple advection nowcast: estimate a spatially-varying motion field
between two consecutive reflectivity grids (FFT phase correlation, tile-
based) and extrapolate the most recent grid forward along it.

Ported near-verbatim from dev/nowcast.py (the local dev harness's pipeline,
where this was developed and tuned against real DMI data — see
PLAN-DMI-MIGRATION.md there for the calibration history). Run
`python dev/nowcast.py` in that harness to exercise the self-tests; they're
not duplicated here since this module's behavior is meant to stay identical
to the harness copy it was ported from.
"""
from __future__ import annotations

import numpy as np
from PIL import Image


def _hann2d(shape: tuple[int, int]) -> np.ndarray:
    wy = np.hanning(shape[0])
    wx = np.hanning(shape[1])
    return np.outer(wy, wx)


def compute_motion(dbz_prev: np.ndarray, dbz_curr: np.ndarray,
                    return_confidence: bool = False):
    """Returns (dy, dx) in pixels: the shift such that dbz_curr ~= dbz_prev
    shifted by (dy, dx). I.e. applying this same shift again extrapolates
    one more step forward. With return_confidence=True, also returns a
    peak-to-mean ratio of the correlation surface: how much the winning
    shift stands out versus a flat/ambiguous surface. Low inside a large,
    broad, relatively self-similar rain mass (many shifts look almost
    equally plausible), high at a sharp, distinctive edge — see
    estimate_motion_field's confidence-weighted blending, which is why
    this exists."""
    # float32/complex64 rather than the numpy FFT default of float64/
    # complex128: this is peak-finding on a correlation surface, not
    # precision-sensitive science, and this array is the single biggest
    # transient allocation in a poll cycle (see PLAN-HA-COMPONENT.md's
    # resource-usage benchmark) — halving it directly halves that peak.
    # numpy's fft2 preserves the input's precision family (float32 in ->
    # complex64 out), so casting here is enough to keep everything below
    # at half width.
    a = dbz_prev.astype(np.float32)
    b = dbz_curr.astype(np.float32)
    a = a - a.mean()
    b = b - b.mean()

    # Radar composites have a fixed coverage-boundary edge (data -> no-data)
    # that's geographically static between frames. A hard edge is a very
    # strong, perfectly-stationary signal that can dominate phase
    # correlation and pull the estimate toward zero shift even when the
    # actual rain clearly moved. Taper toward the array edges to suppress
    # that (standard fix for edge artifacts in phase correlation).
    window = _hann2d(a.shape).astype(np.float32)  # np.hanning is float64 by
                                                    # default; cast or the
                                                    # multiply below upcasts
                                                    # a/b straight back to
                                                    # float64.
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
    # dev/nowcast.py's __main__) — the raw peak position is the negative of
    # the shift that takes prev -> curr, hence the sign flip.
    dy = -(py - h // 2)
    dx = -(px - w // 2)
    if return_confidence:
        confidence = float(corr[py, px] / (corr.mean() + 1e-8))
        return int(dy), int(dx), confidence
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
# frame) vector, at most: 1.0 = fully independent per-tile motion (can look
# like the sky "expanding" rather than moving — see estimate_motion_field's
# docstring), 0.0 = collapses back to one rigid global shift. 0.6 keeps
# real per-region variation visible while anchoring everything to a shared
# overall drift. This is a ceiling — the *actual* per-tile trust used is
# this scaled down further by that tile's own confidence (see below).
TILE_BLEND_ALPHA = 0.6

# Confidence weighting: a tile deep inside a large, broad rain mass has weak
# internal texture (many candidate shifts look almost equally plausible —
# low peak-to-mean ratio from compute_motion), while a tile right at a
# sharp rain/no-rain edge locks onto a confident, distinctive peak. Blending
# every tile by the same fixed ratio doesn't distinguish these — verified
# empirically (see PLAN-DMI-MIGRATION.md): large heavily-covered tiles
# scored ~7-10, small sharp-edge tiles scored 45-190+. But a tiny sliver of
# a tile (barely above TILE_MIN_VALID_PX) can also score artificially high
# just because there's almost nothing for it to disagree with itself about
# — so confidence alone isn't enough; trust also needs to scale with how
# much actual data went into the estimate.
TILE_CONF_LOW, TILE_CONF_HIGH = 7.0, 25.0   # peak/mean range: noise-floor -> clearly-confident
TILE_DATA_SATURATE_PX = 3000  # combined (prev+curr) valid pixels at which the data-volume factor maxes out

# TILE_MAX_DISPLACEMENT_PX above only rejects a tile whose *absolute* motion
# is implausible. It does nothing when several tiles are each individually
# plausible (pass the confidence+data trust check) but point in noticeably
# different directions from each other and from the global vector -- which
# happens whenever the global (whole-frame) vector is small or ~zero (a
# real, legitimate situation: scattered/weakly-organized precipitation with
# no strong overall drift, not a bug). Confidently-measured-but-divergent
# tiles with nothing to anchor them together is exactly what reads as the
# sky "expanding" rather than translating, even though each tile's own
# vector may be individually correct. Cap how far *any* tile is allowed to
# deviate from the shared global drift, regardless of its own trust score --
# real per-region variation stays visible, but bounded, instead of letting
# a handful of confident tiles run off in unrelated directions.
#
# A *fixed* pixel cap turned out to be the wrong shape for this: it's a
# reasonable absolute allowance when the global vector is small (the case
# it was built for), but the same fixed number becomes a much tighter
# *angular* leash as the global vector grows -- e.g. a 15px cap against a
# 34px global vector only allows ~20-25 degrees of directional swing,
# which squeezed out almost all real per-region variation on a day with a
# strong overall drift and made everything look like it was sliding in one
# direction again (the original complaint the whole tiling approach exists
# to fix). So the effective cap now scales with the global vector's own
# magnitude: floor (handles near-zero global), a fraction of the global
# magnitude (preserves real angular variation when there's a strong shared
# drift), ceiling (still bounded even for a very fast-moving system).
TILE_DEVIATION_FLOOR_PX = 15
TILE_DEVIATION_FRACTION = 0.6
TILE_DEVIATION_CEILING_PX = 40


def _tile_max_deviation(global_dy: float, global_dx: float) -> float:
    magnitude = (global_dy * global_dy + global_dx * global_dx) ** 0.5
    return min(TILE_DEVIATION_CEILING_PX, max(TILE_DEVIATION_FLOOR_PX, TILE_DEVIATION_FRACTION * magnitude))


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
    the fallback. Using nanmedian and only padding with NaN means a tile
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
                           blend_alpha: float = TILE_BLEND_ALPHA,
                           max_deviation_px: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Returns (dy_field, dx_field): dense per-pixel arrays, same shape as
    the input, giving a local motion vector at every pixel.

    max_deviation_px: how far any tile's vector may differ from the global
    one (see TILE_DEVIATION_FLOOR_PX/FRACTION/CEILING). Leave as None (the
    default, used in production) to scale it with the global vector's own
    magnitude; pass an explicit number to override — mainly for tests that
    want to isolate the underlying per-tile mechanism from this cap."""
    h, w = dbz_prev.shape
    step = tile_size - overlap
    y_starts = _tile_starts(h, tile_size, step)
    x_starts = _tile_starts(w, tile_size, step)

    # The global (cropped+windowed, whole-frame) estimate anchors the field:
    # every tile — measured or not — gets pulled toward this shared drift
    # (see blend step below), so the *whole* system visibly translates
    # together instead of the sky looking like it's expanding in place.
    fallback_dy, fallback_dx = estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)
    if max_deviation_px is None:
        max_deviation_px = _tile_max_deviation(fallback_dy, fallback_dx)

    dy_grid = np.full((len(y_starts), len(x_starts)), np.nan)
    dx_grid = np.full((len(y_starts), len(x_starts)), np.nan)
    trust_grid = np.zeros((len(y_starts), len(x_starts)))  # per-tile blend weight, see below
    for iy, y0 in enumerate(y_starts):
        for ix, x0 in enumerate(x_starts):
            y1, x1 = y0 + tile_size, x0 + tile_size
            tv_prev = valid_prev[y0:y1, x0:x1]
            tv_curr = valid_curr[y0:y1, x0:x1]
            valid_px = int(tv_prev.sum()) + int(tv_curr.sum())
            if valid_px < min_valid_px:
                continue  # too little signal in this tile to trust a correlation
            tdy, tdx, conf = compute_motion(dbz_prev[y0:y1, x0:x1], dbz_curr[y0:y1, x0:x1],
                                             return_confidence=True)
            if (tdy * tdy + tdx * tdx) ** 0.5 > TILE_MAX_DISPLACEMENT_PX:
                continue  # implausible spike — treat like "no reliable signal"
            # Bound how far even a plausible, confidently-measured tile can
            # differ from the shared global drift (see max_deviation_px /
            # _tile_max_deviation above) — this is a *different* check than
            # the absolute-magnitude one above: it catches a tile that's
            # individually reasonable but disagrees with everything else,
            # which the magnitude check alone can't (a tile pointing a
            # plausible-looking 30px in a direction nothing else agrees
            # with isn't "implausible", just inconsistent with its
            # neighbors and the overall picture).
            tdy = fallback_dy + np.clip(tdy - fallback_dy, -max_deviation_px, max_deviation_px)
            tdx = fallback_dx + np.clip(tdx - fallback_dx, -max_deviation_px, max_deviation_px)
            dy_grid[iy, ix], dx_grid[iy, ix] = tdy, tdx
            # Two independent trust factors, multiplied: how sharply this
            # tile's correlation peak stood out (low inside a broad, self-
            # similar rain mass; see TILE_CONF_LOW/HIGH), and how much
            # actual data it was measured from (guards against a tiny
            # sliver of a tile scoring a spuriously sharp peak just because
            # it has almost nothing to disagree with itself about).
            conf_factor = np.clip((conf - TILE_CONF_LOW) / (TILE_CONF_HIGH - TILE_CONF_LOW), 0.0, 1.0)
            data_factor = np.clip(valid_px / TILE_DATA_SATURATE_PX, 0.0, 1.0)
            trust_grid[iy, ix] = conf_factor * data_factor

    # Smooth out single-tile noise (a tile can pass the magnitude clamp and
    # still be a spurious/wrong-direction outlier relative to its neighbors)
    # while the grid still distinguishes "real measurement" from "no data"
    # (NaN) — see _median_filter_grid's docstring for why this must happen
    # before the blend/fallback steps below, not after. trust_grid isn't
    # smoothed — it's a per-tile confidence weight, not a value that needs
    # spatial consistency with its neighbors.
    dy_grid = _median_filter_grid(dy_grid)
    dx_grid = _median_filter_grid(dx_grid)

    # Blend measured tiles *toward* the global vector rather than trusting
    # them outright, weighted by each tile's own trust score. Without this,
    # a confidently-measured tile sitting next to a low-trust tile (which
    # would otherwise jump straight to a possibly very different fallback
    # value) creates a spatially inconsistent field — and warping by an
    # inconsistent field doesn't translate a rain area as one piece, it
    # stretches it (looks like the sky "expanding" instead of moving).
    # Blending keeps every tile anchored to the same baseline drift, with
    # only a bounded, trust-scaled local deviation layered on top: a sharp,
    # data-rich edge tile gets to show close to its own independent motion,
    # while a noisy/ambiguous interior tile mostly just follows the global
    # drift like everything else. blend_alpha=1 + full trust reproduces the
    # old "trust every tile outright" behavior; 0 collapses to one rigid
    # global shift.
    alpha_grid = blend_alpha * trust_grid
    dy_grid = np.where(np.isnan(dy_grid), np.nan, alpha_grid * dy_grid + (1 - alpha_grid) * fallback_dy)
    dx_grid = np.where(np.isnan(dx_grid), np.nan, alpha_grid * dx_grid + (1 - alpha_grid) * fallback_dx)

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

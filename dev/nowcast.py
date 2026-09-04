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
    # __main__ below) — the raw peak position is the negative of the shift
    # that takes prev -> curr, hence the sign flip.
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


# Same physical ceiling as TILE_MAX_DISPLACEMENT_PX below (~120 km/h over
# the current 20-min production baseline), applied to the global vector
# itself. Found live: an A/B test across baseline lengths against real DMI
# data produced one reading of (dy=51, dx=177) at a 40-min baseline — 177px
# in 40 min is ~150 km/h, physically implausible for this radar's coverage
# area, and almost certainly phase correlation locking onto a spurious peak
# rather than real motion. The global vector is whole-active-region data
# (more than any single tile gets), so it's *less* prone to this than a
# tile — but nothing was rejecting it if it happened anyway. Treat an
# implausible global vector the same way an implausible tile is treated:
# no reliable signal, fall back to (0, 0) rather than trust the spike.
GLOBAL_MAX_DISPLACEMENT_PX = 70


def estimate_motion(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                     dbz_curr: np.ndarray, valid_curr: np.ndarray) -> tuple[int, int]:
    bbox = _echo_bbox(valid_prev, valid_curr)
    if bbox is None:
        return 0, 0  # no echo in either frame — nothing to track
    y0, y1, x0, x1 = bbox
    dy, dx = compute_motion(dbz_prev[y0:y1, x0:x1], dbz_curr[y0:y1, x0:x1])
    if (dy * dy + dx * dx) ** 0.5 > GLOBAL_MAX_DISPLACEMENT_PX:
        return 0, 0  # implausible spike — treat like "no reliable signal"
    return dy, dx


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
# frame) vector, at most: 1.0 = fully independent per-tile motion (can look
# like the sky "expanding" rather than moving — see estimate_motion_field's
# docstring), 0.0 = collapses back to one rigid global shift. 0.4 (lowered
# from an initial 0.6 — even a confidently-measured tile still visibly
# lagged the shared drift, reported live as the field not looking like it
# was moving "together enough") keeps some real per-region variation
# visible while leaning noticeably more on the shared overall drift. This
# is a ceiling — the *actual* per-tile trust used is this scaled down
# further by that tile's own confidence (see below).
TILE_BLEND_ALPHA = 0.4

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
#
# Floor/fraction lowered a second time (15/0.6/40 -> 8/0.4/25) after the
# TILE_BLEND_ALPHA reduction above still wasn't enough on its own -- at the
# anchor magnitudes actually seen in practice (order 10px), the floor was
# the binding constraint, not the fraction, so that's the one that needed
# to move to visibly tighten how far tiles could wander from the shared
# drift.
TILE_DEVIATION_FLOOR_PX = 8
TILE_DEVIATION_FRACTION = 0.4
TILE_DEVIATION_CEILING_PX = 25

# The global vector anchoring the field above was, until now, always the
# separate whole-frame correlation (estimate_motion). That's a real design
# flaw on a day with broad, near-domain-filling precipitation (confirmed
# live 2026-09-04 against DMI's own radar, which showed a real, if modest,
# eastward drift): whole-frame phase correlation goes genuinely ambiguous
# when rain covers nearly the whole crop with weak internal texture and
# content entering/exiting at the domain edges, reading near-zero even
# though individual tiles — sharp local edges, real texture — often *do*
# measure real, mutually-consistent motion. Anchoring everything to that
# bad near-zero global vector produced exactly the reported symptom: tiles
# only wobble within their small deviation allowance around zero, which
# reads as the sky shrinking/expanding in place rather than translating.
#
# Fix: derive the anchor from the tiles' own measurements — a *geometric*
# median (not two independent per-axis medians, and not the mean; see
# _geometric_median's docstring for why) across tiles trustworthy enough
# to count — falling back to the old whole-frame correlation only when
# there isn't enough trustworthy tile signal to form a consensus (a
# genuinely quiet/scattered sky with few measurable tiles).
TILE_CONSENSUS_MIN_TRUST = 0.15  # same 0-1 trust score used for blending; low bar, just excludes noise
TILE_CONSENSUS_MIN_TILES = 3     # below this, a median isn't more trustworthy than the whole-frame estimate

# Pooling across the whole observed history (estimate_consensus_anchor)
# is far more robust than any single pair, but weighting every pair
# equally has its own failure mode: a real system that's accelerating or
# intensifying over the ~2h window makes older, slower pairs drag the
# pooled average below the *current* speed — reported live as forecast
# motion visibly decelerating right at the observed-to-forecast
# transition, even though the direction was already correct. Recency
# weighting fixes this without giving up the robustness pooling exists
# for: each pair's tiles get scaled by this halving every
# CONSENSUS_RECENCY_HALF_LIFE_STEPS steps back from the most recent
# pair, so the last ~20 minutes dominate while the fuller ~2h history
# still contributes real weight rather than being ignored outright.
#
# Lowered once already (4 -> 2, i.e. ~40min -> ~20min half-life) after
# live feedback that the forecast still looked slower than the observed
# frames even with recency weighting in place — verified against real
# data that shortening the half-life further does keep raising the
# anchor's magnitude (a lower half-life leans more on the most recent,
# fastest-moving pairs), without collapsing the anchor's direction.
# Going much lower than this starts trading away the robustness pooling
# exists for in the first place — at half_life=1 the older 90% of the
# 2h history barely counts at all, close to just trusting the single
# most recent pair again (the original single-pair-noise problem this
# whole mechanism exists to avoid).
CONSENSUS_RECENCY_HALF_LIFE_STEPS = 2


def _geometric_median(points: np.ndarray, weights: np.ndarray | None = None,
                       max_iter: int = 50, eps: float = 1e-3) -> tuple[float, float]:
    """The 2D point minimizing total (weighted) Euclidean distance to
    every point in `points` (an (N, 2) array of (dy, dx) tile vectors) —
    Weiszfeld's algorithm. This is *not* the same thing as taking
    median(dy) and median(dx) separately, and the difference matters a
    lot here: tiles routinely agree reasonably well on *speed* while
    disagreeing somewhat on *direction* (real wind has angular spread) —
    two independent per-axis medians let positive and negative
    components partially cancel, collapsing the resultant magnitude even
    though every individual tile measured real, fast motion. Confirmed
    live (2026-09-04): a per-axis median produced a 7px anchor against a
    measured per-tile median *magnitude* of 23px — a >3x understatement
    that read as "the forecast moves too slowly." The geometric median
    doesn't have this failure mode: it operates on the actual 2D points,
    not their axis-wise projections, so it stays close to the cloud of
    real vectors instead of an artifact of decomposing them into x/y.

    weights: optional per-point weight (e.g. each tile's own trust
    score) — a higher-weighted point pulls the result toward itself more
    strongly. Defaults to equal weight for every point."""
    if weights is None:
        weights = np.ones(len(points))
    y = np.average(points, axis=0, weights=weights)
    for _ in range(max_iter):
        dists = np.linalg.norm(points - y, axis=1)
        dists = np.maximum(dists, eps)  # avoid dividing by ~0 at an exact point match
        w = weights / dists
        y_new = np.average(points, axis=0, weights=w)
        if np.linalg.norm(y_new - y) < eps:
            y = y_new
            break
        y = y_new
    return float(y[0]), float(y[1])


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


def _measure_tiles(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                    dbz_curr: np.ndarray, valid_curr: np.ndarray,
                    tile_size: int, overlap: int, min_valid_px: int
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Runs per-tile phase correlation independently across overlapping
    tiles for one frame pair, with no anchor/deviation-clamping applied
    yet. Returns (dy_raw, dx_raw, trust_grid): NaN-filled (n_tiles_y,
    n_tiles_x) arrays (NaN wherever a tile had too little signal or an
    implausible spike to trust) and a 0-1 trust score per measured tile
    (see estimate_motion_field's confidence-weighting comment for what
    trust means). Shared by estimate_motion_field (measures the single
    current baseline/curr pair) and estimate_consensus_anchor (pools
    this across every pair in a longer observed history)."""
    h, w = dbz_prev.shape
    step = tile_size - overlap
    y_starts = _tile_starts(h, tile_size, step)
    x_starts = _tile_starts(w, tile_size, step)
    dy_raw = np.full((len(y_starts), len(x_starts)), np.nan)
    dx_raw = np.full((len(y_starts), len(x_starts)), np.nan)
    trust_grid = np.zeros((len(y_starts), len(x_starts)))
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
            dy_raw[iy, ix], dx_raw[iy, ix] = tdy, tdx
            # Two independent trust factors, multiplied: how sharply this
            # tile's correlation peak stood out (low inside a broad, self-
            # similar rain mass; see TILE_CONF_LOW/HIGH), and how much
            # actual data it was measured from (guards against a tiny
            # sliver of a tile scoring a spuriously sharp peak just because
            # it has almost nothing to disagree with itself about).
            conf_factor = np.clip((conf - TILE_CONF_LOW) / (TILE_CONF_HIGH - TILE_CONF_LOW), 0.0, 1.0)
            data_factor = np.clip(valid_px / TILE_DATA_SATURATE_PX, 0.0, 1.0)
            trust_grid[iy, ix] = conf_factor * data_factor
    return dy_raw, dx_raw, trust_grid


def estimate_consensus_anchor(frames: list[tuple[np.ndarray, np.ndarray]],
                               tile_size: int = TILE_SIZE, overlap: int = TILE_OVERLAP,
                               min_valid_px: int = TILE_MIN_VALID_PX) -> tuple[float, float] | None:
    """Pools tile measurements across *every consecutive pair* in a
    chronological list of observed (dbz, valid) frames — e.g. the full
    ~2h observed history the coordinator already has on hand, not just
    the single most-recent baseline/curr pair — and returns one
    geometric-median anchor in "per frame-to-frame step" units (e.g. per
    DMI's ~10-min publish cadence). Scale the result up before using it
    as estimate_motion_field's anchor_override if the pair you're
    building a field from spans more than one such step (e.g. by
    MOTION_BASELINE_STEPS).

    Why this exists: a single baseline pair can show genuinely
    conflicting, multi-modal tile motion on a complex weather day —
    several comparably-confident tile clusters pointing in substantially
    different directions (verified live, 2026-09-04: real, differently-
    directed high-trust clusters across the domain, not just noise
    around one true value). No aggregation of *that one pair's* tiles —
    median, geometric median, trimming — can manufacture a coherent
    single answer the underlying data doesn't have. Pooling across many
    consecutive pairs instead lets a persistent, real advection
    direction reinforce itself across repeated independent
    measurements, while any single pair's transient/local disagreement
    gets diluted by the rest rather than dominating the whole estimate.

    Returns None if there isn't enough pooled trustworthy signal across
    the whole history to trust a consensus — the caller should fall back
    to whatever it would otherwise use (estimate_motion_field's own
    single-pair fallback if anchor_override isn't passed at all)."""
    pairs = list(zip(frames, frames[1:]))
    all_points, all_trust = [], []
    for pair_index, ((dbz_prev, valid_prev), (dbz_curr, valid_curr)) in enumerate(pairs):
        dy_raw, dx_raw, trust_grid = _measure_tiles(
            dbz_prev, valid_prev, dbz_curr, valid_curr, tile_size, overlap, min_valid_px)
        measured = ~np.isnan(dy_raw)
        if not measured.any():
            continue
        # See CONSENSUS_RECENCY_HALF_LIFE_STEPS: steps_back=0 for the most
        # recent pair (pair_index == len(pairs)-1), growing for older ones.
        steps_back = (len(pairs) - 1) - pair_index
        recency_weight = 0.5 ** (steps_back / CONSENSUS_RECENCY_HALF_LIFE_STEPS)
        all_points.append(np.stack([dy_raw[measured], dx_raw[measured]], axis=1))
        all_trust.append(trust_grid[measured] * recency_weight)
    if not all_points:
        return None
    points = np.concatenate(all_points, axis=0)
    trust = np.concatenate(all_trust, axis=0)
    trustworthy = trust >= TILE_CONSENSUS_MIN_TRUST
    if int(trustworthy.sum()) < TILE_CONSENSUS_MIN_TILES:
        return None
    return _geometric_median(points[trustworthy], weights=trust[trustworthy])


def estimate_motion_field(dbz_prev: np.ndarray, valid_prev: np.ndarray,
                           dbz_curr: np.ndarray, valid_curr: np.ndarray,
                           tile_size: int = TILE_SIZE, overlap: int = TILE_OVERLAP,
                           min_valid_px: int = TILE_MIN_VALID_PX,
                           blend_alpha: float = TILE_BLEND_ALPHA,
                           max_deviation_px: float | None = None,
                           anchor_override: tuple[float, float] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Returns (dy_field, dx_field): dense per-pixel arrays, same shape as
    the input, giving a local motion vector at every pixel.

    max_deviation_px: how far any tile's vector may differ from the global
    one (see TILE_DEVIATION_FLOOR_PX/FRACTION/CEILING). Leave as None (the
    default, used in production) to scale it with the global vector's own
    magnitude; pass an explicit number to override — mainly for tests that
    want to isolate the underlying per-tile mechanism from this cap.

    anchor_override: use this (dy, dx) as the shared-drift anchor instead
    of computing one from dbz_prev/dbz_curr alone — see
    estimate_consensus_anchor, which is what production actually uses to
    build this. Must already be scaled to the same baseline duration
    dbz_prev/dbz_curr spans (estimate_consensus_anchor returns per-step
    units; the caller scales up by however many steps the baseline pair
    covers). Per-tile measurement and the spatial field itself still come
    from dbz_prev/dbz_curr as normal — only the anchor they get compared
    and blended against changes."""
    h, w = dbz_prev.shape
    dy_raw, dx_raw, trust_grid = _measure_tiles(
        dbz_prev, valid_prev, dbz_curr, valid_curr, tile_size, overlap, min_valid_px)

    # Smooth out single-tile noise *before* it can pollute the anchor below
    # — a tile can pass both trust checks and still be a spurious outlier
    # relative to its neighbors (individual ~180px tiles are small enough,
    # relative to a fast-moving system's true displacement, to be at real
    # risk of phase-correlation aliasing: locking onto a wrong, sometimes
    # near-opposite-looking peak). Verified live (2026-09-04): among tiles
    # otherwise agreeing on a real ~25px eastward drift, isolated tiles
    # measured 59px in unrelated directions — physically implausible for a
    # single coherently-advecting system, and exactly the shape of an
    # aliasing artifact rather than real local variation. Confirmed via
    # standard-deviation check that these weren't just flat/textureless
    # tiles trivially "agreeing with themselves". Computing the anchor from
    # such unsmoothed raw tiles let isolated artifacts drag the *aggregate*
    # direction/speed away from what the combined observed radar actually
    # shows, even though most tiles individually had it right. See
    # _median_filter_grid's docstring for why this must run before any
    # fallback-value filling — dy_raw/dx_raw still have real measurement-
    # vs-no-data (NaN) semantics at this point, unlike a later grid that's
    # partly filled with placeholder values. trust_grid isn't smoothed —
    # it's a per-tile confidence weight, not a value that needs spatial
    # consistency with its neighbors.
    dy_smooth = _median_filter_grid(dy_raw)
    dx_smooth = _median_filter_grid(dx_raw)

    # The anchor: every tile — measured or not — gets pulled toward this
    # shared drift (see blend step below), so the *whole* system visibly
    # translates together, in the same overall direction and speed as the
    # combined observed radar, instead of the sky looking like it's
    # shrinking/expanding in place.
    #
    # anchor_override (production always passes one — see
    # estimate_consensus_anchor) takes priority over anything derivable
    # from just this one pair: pooling across the fuller observed history
    # is strictly more robust than any aggregation of a single pair's
    # tiles can be, for the same reason _geometric_median's docstring
    # explains for per-axis medians — a single pair can lack a coherent
    # answer the data simply doesn't have. Without an override (tests
    # exercising this function in isolation, or too little pooled
    # history), fall back to a geometric median of this pair's own
    # trustworthy (now spatially-smoothed) tiles when there are enough of
    # them to count, or the old whole-frame correlation for a genuinely
    # quiet/scattered sky with too few measurable tiles either way.
    if anchor_override is not None:
        fallback_dy, fallback_dx = anchor_override
    else:
        trustworthy = (trust_grid >= TILE_CONSENSUS_MIN_TRUST) & ~np.isnan(dy_smooth)
        if int(trustworthy.sum()) >= TILE_CONSENSUS_MIN_TILES:
            points = np.stack([dy_smooth[trustworthy], dx_smooth[trustworthy]], axis=1)
            fallback_dy, fallback_dx = _geometric_median(points, weights=trust_grid[trustworthy])
        else:
            fallback_dy, fallback_dx = estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)
    if max_deviation_px is None:
        max_deviation_px = _tile_max_deviation(fallback_dy, fallback_dx)

    # Pass 2: bound how far even a plausible, confidently-measured tile can
    # differ from the shared anchor (see max_deviation_px /
    # _tile_max_deviation above) — this is a *different* check than the
    # absolute-magnitude one in pass 1: it catches a tile that's
    # individually reasonable but disagrees with everything else, which the
    # magnitude check alone can't (a tile pointing a plausible-looking 30px
    # in a direction nothing else agrees with isn't "implausible", just
    # inconsistent with its neighbors and the overall picture). Built from
    # the smoothed grid, not the raw one — real per-tile variation still
    # comes through (the clamp still allows every tile up to
    # max_deviation_px of its own character), just without a lone aliased
    # tile's full, uncorrected magnitude.
    dy_grid = np.where(np.isnan(dy_smooth), np.nan,
                        fallback_dy + np.clip(dy_smooth - fallback_dy, -max_deviation_px, max_deviation_px))
    dx_grid = np.where(np.isnan(dx_smooth), np.nan,
                        fallback_dx + np.clip(dx_smooth - fallback_dx, -max_deviation_px, max_deviation_px))

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
    # blend_alpha=1 + a relaxed deviation cap: pure per-tile measurement, no
    # shrinkage toward the global vector and no bound on how far a tile can
    # differ from it — this is what "should the two regions actually
    # disagree at all" tests, independent of the blend/deviation-cap
    # features that (by design) constrain this in the default call below.
    dy_field_pure, dx_field_pure = estimate_motion_field(
        dbz_prev, valid_prev, dbz_curr, valid_curr, blend_alpha=1.0, max_deviation_px=999)
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

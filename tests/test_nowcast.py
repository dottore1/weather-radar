"""Unit tests for nowcast.py, run against both the dev harness copy and
the shipped custom_components copy (see tests/conftest.py's pipeline_impl
fixture). These mirror the assertions in dev/nowcast.py's own __main__
self-test block, formalized as real pytest tests."""
from __future__ import annotations

import numpy as np
import pytest


def test_compute_motion_recovers_a_known_shift(pipeline_impl):
    nowcast = pipeline_impl.nowcast
    rng = np.random.default_rng(0)
    base = rng.random((80, 100))
    true_dy, true_dx = 3, -5
    shifted, _ = nowcast.shift_grid(base, np.ones_like(base, dtype=bool), true_dy, true_dx)
    est_dy, est_dx = nowcast.compute_motion(base, shifted)
    assert (est_dy, est_dx) == (true_dy, true_dx)


def test_estimate_motion_ignores_a_static_coverage_edge(pipeline_impl):
    """A fixed coverage-boundary edge (identical in both frames) shouldn't
    drag the estimate toward zero shift even though it's a strong,
    perfectly-stationary signal — the cropped+windowed estimate should
    still recover the real cell's motion."""
    nowcast = pipeline_impl.nowcast
    h, w = 200, 200
    true_dy, true_dx = 4, 6
    coverage = np.zeros((h, w), dtype=bool)
    coverage[20:180, 20:180] = True
    field_prev = np.zeros((h, w))
    field_prev[coverage] = 5.0
    field_prev[80:100, 80:100] = 30.0
    field_curr = np.zeros((h, w))
    field_curr[coverage] = 5.0
    field_curr[80 + true_dy:100 + true_dy, 80 + true_dx:100 + true_dx] = 30.0
    valid_prev, valid_curr = coverage.copy(), coverage.copy()

    est_dy, est_dx = nowcast.estimate_motion(field_prev, valid_prev, field_curr, valid_curr)
    assert (est_dy, est_dx) == (true_dy, true_dx)


def _opposing_cells_grids():
    """Two storm cells moving in opposite directions — the case a single
    global vector can't represent (the whole reason the tile-based motion
    field exists)."""
    h, w = 600, 600
    left_dy, left_dx = 0, 12
    right_dy, right_dx = 0, -12
    dbz_prev = np.zeros((h, w))
    valid_prev = np.zeros((h, w), dtype=bool)
    dbz_prev[250:350, 80:180] = 30.0
    dbz_prev[250:350, 420:520] = 30.0
    valid_prev[250:350, 80:180] = True
    valid_prev[250:350, 420:520] = True
    dbz_curr = np.zeros((h, w))
    valid_curr = np.zeros((h, w), dtype=bool)
    dbz_curr[250 + left_dy:350 + left_dy, 80 + left_dx:180 + left_dx] = 30.0
    dbz_curr[250 + right_dy:350 + right_dy, 420 + right_dx:520 + right_dx] = 30.0
    valid_curr[250 + left_dy:350 + left_dy, 80 + left_dx:180 + left_dx] = True
    valid_curr[250 + right_dy:350 + right_dy, 420 + right_dx:520 + right_dx] = True
    return dbz_prev, valid_prev, dbz_curr, valid_curr, left_dx, right_dx


def test_motion_field_recovers_independent_opposing_cells(pipeline_impl):
    nowcast = pipeline_impl.nowcast
    dbz_prev, valid_prev, dbz_curr, valid_curr, left_dx, right_dx = _opposing_cells_grids()
    dy_field, dx_field = nowcast.estimate_motion_field(
        dbz_prev, valid_prev, dbz_curr, valid_curr, blend_alpha=1.0, max_deviation_px=999)
    assert dx_field[300, 130] == pytest.approx(left_dx, abs=2)
    assert dx_field[300, 470] == pytest.approx(right_dx, abs=2)
    assert dx_field[300, 130] > 0 and dx_field[300, 470] < 0


def test_motion_field_blending_stays_directionally_distinct(pipeline_impl):
    """Two cells with no coherent overall drift between them (opposing,
    equal-and-opposite motion) should still blend toward *distinct*
    per-tile outputs — pulled toward each cell's own true direction, not
    collapsed to one shared value — even though the anchor itself (now a
    consensus of the tiles, see TILE_CONSENSUS_MIN_TRUST/TILE_CONSENSUS_
    MIN_TILES) is honestly near zero for this symmetric case."""
    nowcast = pipeline_impl.nowcast
    dbz_prev, valid_prev, dbz_curr, valid_curr, _, _ = _opposing_cells_grids()
    dy_field, dx_field = nowcast.estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    assert dx_field[300, 130] > 0  # pulled toward the left cell's true +dx
    assert dx_field[300, 470] < 0  # pulled toward the right cell's true -dx
    assert dx_field[300, 130] > dx_field[300, 470]


def test_motion_field_anchor_prefers_tile_consensus_over_a_bad_whole_frame_reading(pipeline_impl, monkeypatch):
    """The real-world bug this fixes: broad, near-domain-filling
    precipitation can make the whole-frame correlation read near-zero
    even when individual tiles clearly agree on real, consistent motion
    (confirmed live against DMI's own radar, 2026-09-04 — see nowcast.py's
    TILE_CONSENSUS_MIN_TRUST/TILE_CONSENSUS_MIN_TILES comment). Force
    estimate_motion (the whole-frame fallback) to return an obviously
    wrong (0, 0) and verify the field still reflects what several
    independently-agreeing, confidently-measured tiles found, instead of
    collapsing everything toward the bad global reading."""
    nowcast = pipeline_impl.nowcast
    h, w = 700, 700
    true_dy, true_dx = 0, 8
    dbz_prev = np.zeros((h, w))
    valid_prev = np.zeros((h, w), dtype=bool)
    dbz_curr = np.zeros((h, w))
    valid_curr = np.zeros((h, w), dtype=bool)
    rng = np.random.default_rng(1)
    # Several separated, textured (non-uniform) patches sharing the same
    # true shift -- textured so each tile's own correlation is confident,
    # not just a flat block with a weak/ambiguous peak.
    for (y0, x0) in [(80, 80), (80, 380), (380, 80), (380, 380), (380, 500)]:
        patch = 20.0 + rng.random((160, 160)) * 15.0
        dbz_prev[y0:y0 + 160, x0:x0 + 160] = patch
        valid_prev[y0:y0 + 160, x0:x0 + 160] = True
        dbz_curr[y0 + true_dy:y0 + 160 + true_dy, x0 + true_dx:x0 + 160 + true_dx] = patch
        valid_curr[y0 + true_dy:y0 + 160 + true_dy, x0 + true_dx:x0 + 160 + true_dx] = True

    monkeypatch.setattr(nowcast, "estimate_motion", lambda *a, **k: (0, 0))
    dy_field, dx_field = nowcast.estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    assert dx_field[150, 150] == pytest.approx(true_dx, abs=2)


def test_motion_field_falls_back_to_whole_frame_when_too_few_tiles_are_trustworthy(pipeline_impl, monkeypatch):
    """The consensus anchor only kicks in with enough trustworthy tiles
    (TILE_CONSENSUS_MIN_TILES) — a quiet/scattered sky with too little
    tile signal should still fall back to the whole-frame estimate,
    exactly like before this change."""
    nowcast = pipeline_impl.nowcast
    h, w = 150, 150  # <= TILE_SIZE: a single tile, per _tile_starts
    # No echo anywhere -- every tile fails TILE_MIN_VALID_PX, so there's
    # zero tile signal to form a consensus from and this must fall back
    # to whichever value estimate_motion returns.
    dbz_prev = np.zeros((h, w))
    valid_prev = np.zeros((h, w), dtype=bool)
    dbz_curr = np.zeros((h, w))
    valid_curr = np.zeros((h, w), dtype=bool)

    monkeypatch.setattr(nowcast, "estimate_motion", lambda *a, **k: (7, 9))
    dy_field, dx_field = nowcast.estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    assert dy_field[0, 0] == pytest.approx(7, abs=1)
    assert dx_field[0, 0] == pytest.approx(9, abs=1)


def test_tile_max_deviation_scales_with_global_magnitude(pipeline_impl):
    nowcast = pipeline_impl.nowcast
    assert nowcast._tile_max_deviation(0, 0) == nowcast.TILE_DEVIATION_FLOOR_PX
    small = nowcast._tile_max_deviation(0, 10)
    large = nowcast._tile_max_deviation(0, 100)
    assert nowcast.TILE_DEVIATION_FLOOR_PX <= small <= large <= nowcast.TILE_DEVIATION_CEILING_PX


def test_forecast_steps_from_field_produces_the_requested_step_count(pipeline_impl):
    nowcast = pipeline_impl.nowcast
    dbz_prev, valid_prev, dbz_curr, valid_curr, _, _ = _opposing_cells_grids()
    dy_field, dx_field = nowcast.estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    steps = nowcast.forecast_steps_from_field(dbz_curr, valid_curr, dy_field, dx_field, 4)
    assert len(steps) == 4
    for dbz_step, valid_step in steps:
        assert dbz_step.shape == dbz_curr.shape
        assert valid_step.shape == valid_curr.shape


def test_warp_by_field_leaves_revealed_edges_invalid(pipeline_impl):
    """warp_by_field should never invent data at an edge the shift reveals
    — a semi-Lagrangian sample that lands outside the source frame must
    come back invalid, not wrap or extrapolate."""
    nowcast = pipeline_impl.nowcast
    dbz = np.full((50, 50), 20.0)
    valid = np.ones((50, 50), dtype=bool)
    dy_field = np.full((50, 50), 0.0)
    dx_field = np.full((50, 50), 40.0)  # shift everything 40px right
    out_dbz, out_valid = nowcast.warp_by_field(dbz, valid, dy_field, dx_field)
    assert not out_valid[:, :40].any()
    assert out_valid[:, 40:].all()

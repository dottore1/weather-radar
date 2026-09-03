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


def test_motion_field_blending_stays_directionally_distinct_around_the_global_vector(pipeline_impl):
    nowcast = pipeline_impl.nowcast
    dbz_prev, valid_prev, dbz_curr, valid_curr, _, _ = _opposing_cells_grids()
    global_dy, global_dx = nowcast.estimate_motion(dbz_prev, valid_prev, dbz_curr, valid_curr)
    dy_field, dx_field = nowcast.estimate_motion_field(dbz_prev, valid_prev, dbz_curr, valid_curr)
    assert dx_field[300, 130] > global_dx
    assert dx_field[300, 470] <= global_dx


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

"""Unit + integration tests for render.py, run against both the dev
harness copy and the shipped custom_components copy (see
tests/conftest.py's pipeline_impl fixture)."""
from __future__ import annotations

import io

import numpy as np
import pytest

from tests import synth


def test_colorize_transparent_below_first_stop(pipeline_impl):
    render = pipeline_impl.render
    dbz = np.array([[-5.0, 0.0]], dtype=np.float32)
    valid = np.array([[True, True]])
    rgba = render.colorize(dbz, valid)
    assert rgba[0, 0, 3] == 0  # below the lowest color stop -> alpha 0


def test_colorize_hides_noise_floor_reflectivity_not_just_negative_values(pipeline_impl):
    """Regression test for the A/B-tested color threshold fix: real DMI
    composites carry valid-but-tiny dBZ (observed down to -31.5 dBZ live)
    from radar noise floor / clear-air return, not actual precipitation.
    Marked `valid=True` doesn't mean "draw it" — only >=20 dBZ should ever
    become visible (see COLOR_STOPS's docstring in render.py)."""
    render = pipeline_impl.render
    dbz = np.array([[0.0, 10.0, 19.9, 20.5, 30.0]], dtype=np.float32)
    valid = np.ones((1, 5), dtype=bool)
    rgba = render.colorize(dbz, valid)
    assert list(rgba[0, :3, 3]) == [0, 0, 0], "0/10/19.9 dBZ (noise-floor-range) must stay invisible"
    assert rgba[0, 3, 3] > 0, "20.5 dBZ (real light rain) should be visible"
    assert rgba[0, 4, 3] > 0, "30.0 dBZ (real rain) should be visible"


def test_colorize_invalid_pixel_is_transparent_regardless_of_value(pipeline_impl):
    render = pipeline_impl.render
    dbz = np.array([[40.0]], dtype=np.float32)
    valid = np.array([[False]])
    rgba = render.colorize(dbz, valid)
    assert rgba[0, 0, 3] == 0


def test_png_from_grid_produces_a_valid_png(pipeline_impl):
    render = pipeline_impl.render
    dbz = np.zeros((10, 10), dtype=np.float32)
    valid = np.zeros((10, 10), dtype=bool)
    dbz[3:6, 3:6] = 25.0
    valid[3:6, 3:6] = True
    png_bytes = render.png_from_grid(dbz, valid)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_decode_h5_reads_expected_fields(pipeline_impl, tmp_path):
    render = pipeline_impl.render
    raw = synth.blank_raw(w=20, h=10)
    h5_bytes = synth.make_h5_bytes(tmp_path, "sample.h5", ul_lon=5.0, ul_lat=59.0,
                                    xscale=0.01, yscale=0.01, raw=raw)
    decoded = render.decode_h5(io.BytesIO(h5_bytes))
    assert decoded["gain"] == synth.GAIN
    assert decoded["offset"] == synth.OFFSET
    assert decoded["nodata"] == synth.NODATA
    assert decoded["undetect"] == synth.UNDETECT
    assert decoded["projdef"] == synth.PROJDEF
    assert decoded["ul_lon"] == 5.0
    assert decoded["ul_lat"] == 59.0
    assert decoded["xscale"] == 0.01
    assert decoded["yscale"] == 0.01
    assert decoded["raw"].shape == (10, 20)


def test_reproject_places_signal_at_the_right_geographic_location(pipeline_impl, tmp_path):
    render = pipeline_impl.render
    signal_lon, signal_lat, signal_dbz = 10.0, 56.0, 30.0
    h5_bytes = synth.make_composite_h5_bytes(tmp_path, "composite.h5", signal_lon, signal_lat, signal_dbz)
    dbz, valid = render.dbz_grid_for(io.BytesIO(h5_bytes))
    assert dbz.shape == (render.OUT_HEIGHT, render.OUT_WIDTH)

    out_col = round((signal_lon - render.OUT_LON_MIN) / (render.OUT_LON_MAX - render.OUT_LON_MIN) * render.OUT_WIDTH)
    out_row = round((render.OUT_LAT_MAX - signal_lat) / (render.OUT_LAT_MAX - render.OUT_LAT_MIN) * render.OUT_HEIGHT)

    window = 6
    r0, r1 = max(0, out_row - window), out_row + window
    c0, c1 = max(0, out_col - window), out_col + window
    window_valid = valid[r0:r1, c0:c1]
    window_dbz = dbz[r0:r1, c0:c1]
    assert window_valid.any(), "the synthetic signal should show up near its expected output pixel"
    assert window_dbz.max() == pytest.approx(signal_dbz, abs=synth.GAIN)


def test_no_signal_produces_an_entirely_invalid_grid(pipeline_impl, tmp_path):
    render = pipeline_impl.render
    raw = synth.blank_raw()  # all UNDETECT
    h5_bytes = synth.make_h5_bytes(tmp_path, "blank.h5", synth.UL_LON, synth.UL_LAT,
                                    synth.XSCALE, synth.YSCALE, raw)
    _, valid = render.dbz_grid_for(io.BytesIO(h5_bytes))
    assert not valid.any()


def test_crop_to_display_matches_direct_out_box(pipeline_impl, tmp_path):
    """dbz_grid_for_work cropped back to the display box should line up
    with dbz_grid_for on the same source file — the forecast pipeline
    depends on this (see render.py's crop_to_display docstring)."""
    render = pipeline_impl.render
    h5_bytes = synth.make_composite_h5_bytes(tmp_path, "composite.h5", 10.0, 56.0, 30.0)
    direct_dbz, direct_valid = render.dbz_grid_for(io.BytesIO(h5_bytes))
    work_dbz, work_valid = render.dbz_grid_for_work(io.BytesIO(h5_bytes))
    cropped_dbz, cropped_valid = render.crop_to_display(work_dbz, work_valid)

    assert cropped_valid.shape == direct_valid.shape
    agreement = (cropped_valid == direct_valid).mean()
    assert agreement > 0.99, f"cropped work-box valid mask should closely match the direct out-box one (got {agreement:.4f})"
    both_valid = cropped_valid & direct_valid
    assert np.allclose(cropped_dbz[both_valid], direct_dbz[both_valid])


def test_render_png_bytes_end_to_end(pipeline_impl, tmp_path):
    render = pipeline_impl.render
    h5_bytes = synth.make_composite_h5_bytes(tmp_path, "composite.h5", 10.0, 56.0, 30.0)
    png_bytes = render.render_png_bytes(io.BytesIO(h5_bytes))
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

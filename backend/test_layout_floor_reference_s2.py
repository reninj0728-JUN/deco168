# -*- coding: utf-8 -*-
"""Deterministic transverse-floor evidence tests."""
from pathlib import Path

from PIL import Image

import layout_floor_reference_s2 as floor_ref


E72_LIVING_FLOOR_YX1000 = [
    [990, 12],
    [578, 436],
    [617, 617],
    [995, 988],
]


def test_e72_detects_observed_horizontal_transverse_floor_reference():
    photo = Path(__file__).parent / "_phase0_layout_contract" / "fixtures" / "E72F4ADB.jpg"

    result = floor_ref.estimate_transverse_floor_reference(
        photo, E72_LIVING_FLOOR_YX1000,
    )

    assert result["status"] == "observed"
    assert result["support_count"] >= 10
    assert abs(result["angle_degrees"]) <= 3.0
    assert result["direction_xy"][0] > 0.99


def test_textureless_floor_returns_missing_instead_of_guessing(tmp_path):
    photo = tmp_path / "blank.jpg"
    Image.new("RGB", (1200, 800), "white").save(photo)

    result = floor_ref.estimate_transverse_floor_reference(
        photo,
        [[950, 20], [350, 420], [350, 580], [950, 980]],
    )

    assert result["status"] == "missing"
    assert result["direction_xy"] is None
    assert "MISSING_TRANSVERSE_FLOOR_REFERENCE" in result["unsafe_codes"]

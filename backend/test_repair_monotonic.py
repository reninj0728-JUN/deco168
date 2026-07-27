# -*- coding: utf-8 -*-
"""Z3 候選不得以新增硬傷的版本覆蓋目前較佳版本。"""
from pathlib import Path

import pytest

import api


def _v(**over):
    base = {
        "hard_fail": False,
        "camera_axis_preserved": True,
        "passage_openings_preserved": True,
        "main_window_region_match": True,
        "sofa_focal_face_each_other": True,
        "product_sofa_seating_match": True,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("flag", api._Z3_REGRESSION_FLAGS)
def test_new_hard_failure_rejects_z3_candidate(flag):
    reason = api._z3_candidate_regression_reason(_v(), _v(**{flag: True}))
    assert reason is not None
    assert flag in reason


def test_edd_pair_repair_cannot_reintroduce_door_block():
    prev = _v(hard_fail=True, focal_anchor_misaligned_with_sofa=True,
              furniture_blocks_door=False)
    candidate = _v(hard_fail=True, focal_anchor_misaligned_with_sofa=True,
                   furniture_blocks_door=True)
    reason = api._z3_candidate_regression_reason(prev, candidate)
    assert reason is not None
    assert "furniture_blocks_door" in reason


def test_existing_fault_is_not_mistaken_for_new_regression():
    prev = _v(hard_fail=True, furniture_blocks_door=True)
    candidate = _v(hard_fail=True, furniture_blocks_door=True)
    assert api._z3_candidate_regression_reason(prev, candidate) is None


@pytest.mark.parametrize("field", [
    "camera_axis_preserved",
    "passage_openings_preserved",
    "main_window_region_match",
    "sofa_focal_face_each_other",
    "product_sofa_seating_match",
])
def test_passed_negative_polarity_field_cannot_regress(field):
    reason = api._z3_candidate_regression_reason(_v(), _v(**{field: False}))
    assert reason is not None
    assert field in reason


def test_validation_outage_candidate_is_never_adopted():
    reason = api._z3_candidate_regression_reason(
        _v(hard_fail=True), _v(hard_fail=True, validation_outage=True))
    assert reason == "candidate validation unavailable"


def test_new_unclassified_hard_fail_is_rejected():
    assert api._z3_candidate_regression_reason(
        _v(hard_fail=False), _v(hard_fail=True)) == "candidate introduced hard_fail"


def test_improved_candidate_without_new_fault_is_allowed():
    prev = _v(hard_fail=True, focal_anchor_misaligned_with_sofa=True)
    candidate = _v(hard_fail=False, focal_anchor_misaligned_with_sofa=False)
    assert api._z3_candidate_regression_reason(prev, candidate) is None


def test_z3_guard_runs_before_candidate_replaces_current_version():
    source = Path(api.__file__).read_text(encoding="utf-8")
    z3 = source.split("# ── Z3:", 1)[1].split("# 統計", 1)[0]
    assert z3.index("_z3_candidate_regression_reason(") < z3.index("final[idx] = new_r")
    assert "if regression_reason:" in z3
    assert "break" in z3[z3.index("if regression_reason:"):z3.index("final[idx] = new_r")]

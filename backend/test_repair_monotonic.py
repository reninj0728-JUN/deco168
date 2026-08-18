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


def test_becf_product_fail_after_door_cleared_is_not_z3_regression():
    """BECF1DA5：門距修好、只新增商品保真 → 不得當回歸丢掉。"""
    prev = _v(hard_fail=True, furniture_blocks_door=True)
    cand = _v(hard_fail=True, furniture_blocks_door=False,
              product_visibility_fail=True)
    assert api._z3_candidate_regression_reason(prev, cand) is None


def test_product_fail_while_door_still_blocked_is_still_rejected():
    prev = _v(hard_fail=True, furniture_blocks_door=True)
    cand = _v(hard_fail=True, furniture_blocks_door=True,
              product_visibility_fail=True)
    reason = api._z3_candidate_regression_reason(prev, cand)
    assert reason is not None
    assert "product_visibility_fail" in reason


def test_product_fail_after_partial_geom_clear_is_still_rejected():
    prev = _v(hard_fail=True, furniture_blocks_door=True, sofa_on_wrong_side=True)
    cand = _v(hard_fail=True, furniture_blocks_door=False, sofa_on_wrong_side=True,
              product_visibility_fail=True)
    reason = api._z3_candidate_regression_reason(prev, cand)
    assert reason is not None
    assert "product_visibility_fail" in reason


def test_becf_cleared_door_routes_to_product_only_not_sofa_align(tmp_path):
    """放行後必須走既有商品修，不能再走沙發位移。"""
    prev = {
        "hard_fail": True,
        "furniture_blocks_door": True,
        "product_visibility_fail": False,
        "sofa_facing_entrance_door": False,
        "sofa_faces_walkway": False,
        "furniture_blocks_walkway": False,
        "sofa_on_wrong_side": False,
        "sofa_outside_living_zone": False,
        "sofa_back_against_window": False,
        "sofa_facing_window": False,
        "focal_anchor_misaligned_with_sofa": False,
        "focal_anchor_past_door_in_depth": False,
        "camera_axis_preserved": True,
        "passage_openings_preserved": True,
        "render_bboxes": {
            "sofa": [530, 287, 750, 442],
            "entrance_door": [260, 115, 840, 258],
            "focal_anchor": [570, 621, 792, 826],
        },
    }
    cand = dict(prev)
    cand["furniture_blocks_door"] = False
    cand["product_visibility_fail"] = True
    cand["focal_anchor_past_door_in_depth"] = True
    cand["render_bboxes"] = {
        "sofa": [528, 301, 730, 442],
        "entrance_door": [338, 118, 860, 258],
        "focal_anchor": [572, 626, 788, 827],
    }
    render = tmp_path / "z3.jpg"
    render.write_bytes(b"x")
    r = {"render_path": str(render)}
    assert api._z3_candidate_regression_reason(prev, cand) is None
    assert api._sofa_alignment_edit_base(cand, r, "living") is None
    assert api._product_only_edit_base(cand, r, "living") == str(render)


def test_z3_retry_calls_product_only_before_generate():
    source = Path(api.__file__).read_text(encoding="utf-8")
    z3 = source.split("# ── Z3:", 1)[1].split("# 統計", 1)[0]
    assert "_product_only_edit_base(" in z3
    assert z3.index("_product_only_edit_base(") < z3.index(
        'failed_stage = "z3_retry_generate_renders"')

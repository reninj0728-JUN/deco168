# -*- coding: utf-8 -*-
"""Production S2 geometry verification/correction loop. No network calls."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

import layout_geometry_s2 as geometry_s2
import layout_geometry_verifier_s2 as verifier_s2
from PIL import Image
from test_layout_geometry_s2 import _safe_geometry


HARD_PASS = {
    "right_wall_floor_alignment": "pass",
    "left_wall_floor_alignment": "pass",
    "sofa_back_contact": "pass",
    "tv_wall_contact": "pass",
    "axis_clear_of_entrance": "pass",
    "walkway_connected": "pass",
    "source_entrance_retained": "pass",
    "cross_axis_matches_floor_transverse": "pass",
    "overall": "pass",
    "unsafe_codes": [],
    "notes": "verified",
}

FLOATING_PASS = {
    "tv_intended_wall_contact": "pass",
    "floating_sofa_inside_living_floor": "pass",
    "floating_sofa_rear_clear": "pass",
    "floating_sofa_clear_of_entrance_walkway": "pass",
    "axis_clear_of_entrance": "pass",
    "walkway_connected": "pass",
    "source_entrance_retained": "pass",
    "shared_floor_depth_axis": "pass",
    "overall": "pass",
    "unsafe_codes": [],
    "notes": "floating verified",
}

OBSERVED_FLOOR_REFERENCE = {
    "status": "observed",
    "confidence": "high",
    "direction_xy": [1.0, 0.0],
    "angle_degrees": 0.0,
    "support_count": 20,
    "unsafe_codes": [],
}


def _observed_floor_reference(*_args):
    return copy.deepcopy(OBSERVED_FLOOR_REFERENCE)


def test_verify_attempt_configuration_has_absolute_hard_cap():
    assert verifier_s2._bounded_verify_attempts("999") == verifier_s2.S2_VERIFY_HARD_CAP
    assert verifier_s2._bounded_verify_attempts("1") == 2
    assert verifier_s2._bounded_verify_attempts("invalid") == 4


def test_strict_parser_rejects_trailing_non_json_tokens():
    text = json.dumps(HARD_PASS) + "\n}"

    try:
        verifier_s2.parse_verifier_response(text)
    except verifier_s2.VerifierResponseError as exc:
        assert "trailing" in str(exc).lower() or "json" in str(exc).lower()
    else:
        raise AssertionError("malformed verifier output must fail closed")


def test_gemini_verifier_retries_one_malformed_json_response(tmp_path):
    photo = tmp_path / "room.jpg"
    guide = tmp_path / "guide.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    Image.new("RGB", (1000, 700), "white").save(guide)

    class FakeModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            text = "{broken" if self.calls == 1 else json.dumps(HARD_PASS)
            return SimpleNamespace(text=text)

    models = FakeModels()
    verdict = verifier_s2.verify_s2_guide_gemini(
        photo, guide, 1, client=SimpleNamespace(models=models),
    )

    assert verdict == HARD_PASS
    assert models.calls == 2


def test_gemini_verifier_uses_floating_parser_for_candidate_f(tmp_path):
    photo = tmp_path / "room.jpg"
    guide = tmp_path / "guide.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    Image.new("RGB", (1000, 700), "white").save(guide)

    class FakeModels:
        def __init__(self):
            self.contents = None

        def generate_content(self, **kwargs):
            self.contents = kwargs["contents"]
            return SimpleNamespace(text=json.dumps(FLOATING_PASS))

    plan = {
        "chosen_candidate_id": "s2_f_free_test",
        "candidates": [{
            "candidate_id": "s2_f_free_test",
            "candidate_type": "F",
            "sofa_side": "free",
            "tv_side": "right",
        }],
    }
    models = FakeModels()

    verdict = verifier_s2.verify_s2_guide_gemini(
        photo, guide, 1, plan=plan,
        client=SimpleNamespace(models=models),
    )

    assert verifier_s2.verification_passes(verdict) is True
    assert verdict["floating_audit"] == {
        key: FLOATING_PASS[key]
        for key in (
            "tv_intended_wall_contact",
            "floating_sofa_inside_living_floor",
            "floating_sofa_rear_clear",
            "floating_sofa_clear_of_entrance_walkway",
            "axis_clear_of_entrance",
            "walkway_connected",
            "source_entrance_retained",
            "shared_floor_depth_axis",
        )
    }
    prompt = models.contents[-1]
    assert "floating_sofa_inside_living_floor" in prompt
    assert "Do not require the GREEN sofa to touch either wall" in prompt
    assert "visible RIGHT wall/floor seam" in prompt


def test_hard_pass_is_required_for_every_verifier_field():
    assert verifier_s2.verification_passes(HARD_PASS) is True
    uncertain = copy.deepcopy(HARD_PASS)
    uncertain["cross_axis_matches_floor_transverse"] = "uncertain"
    uncertain["overall"] = "fail"

    assert verifier_s2.verification_passes(uncertain) is False


def test_first_pass_verified_plan_writes_final_guide(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    calls = []

    def fake_verifier(photo_path, guide_path, attempt_number, plan=None):
        calls.append((photo_path, guide_path, attempt_number))
        return copy.deepcopy(HARD_PASS)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=fake_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["disposition"] == "SAFE_FOR_GENERATION"
    assert result["plan"]["geometry_verification"]["status"] == "pass"
    assert result["plan"]["geometry_verification"]["attempt_count"] == 1
    assert result["guide_artifact"]["path"].endswith("layout_guide_s2_final.jpg")
    assert len(calls) == 1


def test_wall_correction_extends_observed_line_across_original_usable_depth():
    raw = _safe_geometry()
    original_wall = raw["elements"]["left_wall_floor"]["segment_yx1000"]
    usable = next(
        item for item in raw["usable_wall_segments"] if item["side"] == "left"
    )
    expected_near_y = original_wall[0][0] + (
        original_wall[1][0] - original_wall[0][0]
    ) * usable["t_start"]
    expected_deep_y = original_wall[0][0] + (
        original_wall[1][0] - original_wall[0][0]
    ) * usable["t_end"]
    short_observed_segment = [[700, 189], [650, 224]]

    corrected, changed = verifier_s2._apply_wall_corrections(raw, {
        "left_wall_floor_alignment": "fail",
        "corrected_left_wall_floor_segment_yx1000": short_observed_segment,
    })

    corrected_line = corrected["elements"]["left_wall_floor"]["segment_yx1000"]
    assert changed is True
    assert abs(corrected_line[0][0] - expected_near_y) < 1e-6
    assert abs(corrected_line[1][0] - expected_deep_y) < 1e-6
    assert abs(corrected_line[0][0] - corrected_line[1][0]) > 4 * abs(
        short_observed_segment[0][0] - short_observed_segment[1][0]
    )
    corrected_element = corrected["elements"]["left_wall_floor"]
    corrected_usable = next(
        item for item in corrected["usable_wall_segments"] if item["side"] == "left"
    )
    assert corrected_element["status"] == "verifier_corrected"
    assert corrected_element["confidence"] == "medium"
    assert corrected_element["visibility"] == "partial"
    assert corrected_usable["status"] == "verifier_corrected"
    assert corrected_usable["confidence"] == "medium"
    replanned = geometry_s2.build_s2_plan(
        corrected,
        width=1000,
        height=700,
        expected_source_photo_index=0,
        trusted_verifier_corrections=True,
    )
    corrected_geometry = [
        item for item in replanned["geometry"]
        if item.get("source_name") in ("left_wall_floor", "left_deep-verifier-corrected")
    ]
    assert corrected_geometry
    assert all(item["evidence_mode"] == "verifier_corrected" for item in corrected_geometry)


def test_wall_correction_updates_living_floor_corner_when_usable_wall_reaches_deep_end():
    """71DC312E｜左牆深端修正後，living_floor 不得仍保留錯的舊深角。"""
    raw = _safe_geometry()
    raw["elements"]["living_floor"]["polygon_yx1000"] = [
        [980, 0], [980, 1000], [615, 620], [580, 435],
    ]
    raw["elements"]["left_wall_floor"]["segment_yx1000"] = [
        [980, 0], [580, 435],
    ]
    left_usable = next(
        item for item in raw["usable_wall_segments"] if item["side"] == "left"
    )
    left_usable["t_start"] = 0.35
    left_usable["t_end"] = 1.0

    corrected, changed = verifier_s2._apply_wall_corrections(raw, {
        "left_wall_floor_alignment": "fail",
        "corrected_left_wall_floor_segment_yx1000": [[700, 260], [580, 382]],
    })

    assert changed is True
    floor = corrected["elements"]["living_floor"]["polygon_yx1000"]
    assert [580.0, 382.0] in floor
    assert [580, 435] not in floor
    assert [980, 0] in floor, "未被修正證據覆蓋的近端地板角必須保留"
    corrected_line = corrected["elements"]["left_wall_floor"]["segment_yx1000"]
    for endpoint in corrected_line:
        boundary_distance = min(
            geometry_s2._point_segment_distance(
                endpoint, floor[index], floor[(index + 1) % len(floor)],
            )
            for index in range(len(floor))
        )
        assert boundary_distance <= 1e-6, "修正牆段兩端都必須落在 living_floor 邊界"
    replanned = geometry_s2.build_s2_plan(
        corrected,
        width=1000,
        height=700,
        expected_source_photo_index=0,
        transverse_direction_xy=[1.0, 0.0],
        trusted_verifier_corrections=True,
    )
    assert "GEOM_NOT_ELIGIBLE" not in replanned["unsafe_codes"]


def test_wall_side_check_requires_both_endpoints_to_stay_on_side():
    assert verifier_s2._segment_stays_on_side([[700, 100], [650, 550]], "left") is False
    assert verifier_s2._segment_stays_on_side([[700, 450], [650, 900]], "right") is False


def test_uncertain_or_missing_alignment_never_applies_correction():
    raw = _safe_geometry()
    segment = [[700, 189], [650, 224]]
    for alignment in ("uncertain", None, "invalid"):
        verdict = {"corrected_left_wall_floor_segment_yx1000": segment}
        if alignment is not None:
            verdict["left_wall_floor_alignment"] = alignment
        corrected, changed = verifier_s2._apply_wall_corrections(raw, verdict)
        assert changed is False
        assert corrected == raw


@pytest.mark.parametrize("unsafe_segment", [
    [[700, 800], [650, 850]],  # crosses to the opposite side
    [[700, 400], [650, 450]],  # too far from the observed wall seam
    [[700, 200], [650, 205]],  # implausible orientation change
    [[650, 225], [640, 235]],  # excessive extrapolation from a tiny segment
])
def test_wall_correction_rejects_unbounded_or_cross_side_segments(unsafe_segment):
    raw = _safe_geometry()

    corrected, changed = verifier_s2._apply_wall_corrections(raw, {
        "left_wall_floor_alignment": "fail",
        "corrected_left_wall_floor_segment_yx1000": unsafe_segment,
    })

    assert changed is False
    assert corrected == raw


def test_failed_wall_alignment_applies_one_correction_and_reverifies(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    corrected_left = [[820, 90], [357, 430]]
    responses = [
        {
            **HARD_PASS,
            "left_wall_floor_alignment": "fail",
            "tv_wall_contact": "fail",
            "overall": "fail",
            "unsafe_codes": ["LEFT_WALL_ALIGNMENT_FAIL"],
            "corrected_left_wall_floor_segment_yx1000": corrected_left,
            "corrected_right_wall_floor_segment_yx1000": None,
        },
        copy.deepcopy(HARD_PASS),
    ]
    calls = []

    def fake_verifier(photo_path, guide_path, attempt_number, plan=None):
        calls.append((guide_path, attempt_number))
        return responses.pop(0)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=fake_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["disposition"] == "SAFE_FOR_GENERATION"
    assert result["plan"]["geometry_verification"]["status"] == "pass"
    assert result["plan"]["geometry_verification"]["corrected"] is True
    corrected_geometry = result["raw_geometry"]["elements"]["left_wall_floor"]["segment_yx1000"]
    assert corrected_geometry != corrected_left
    assert corrected_geometry[0][0] > corrected_geometry[1][0]
    assert len(calls) == 2
    assert calls[0][1] == 1 and calls[1][1] == 2


def test_uncertain_verdict_retries_same_plan_and_second_pass_is_accepted(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    uncertain = copy.deepcopy(HARD_PASS)
    uncertain["sofa_back_contact"] = "uncertain"
    uncertain["overall"] = "fail"
    uncertain["unsafe_codes"] = ["SOFA_WALL_CONTACT_UNCERTAIN"]
    responses = [uncertain, copy.deepcopy(HARD_PASS)]
    calls = []

    def fake_verifier(_photo, _guide, attempt_number, _plan=None):
        calls.append(attempt_number)
        return responses.pop(0)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=fake_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    verification = result["plan"]["geometry_verification"]
    assert result["plan"]["disposition"] == "SAFE_FOR_GENERATION"
    assert calls == [1, 2]
    assert verification["attempt_count"] == 2
    assert verification["corrected"] is False
    assert verification["retry_reason"] == "uncertain_verdict"
    assert [item["outcome"] for item in result["verification_history"]] == [
        "uncertain", "pass",
    ]


def test_uncertain_verifier_result_blocks_only_the_verified_candidate(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    uncertain = copy.deepcopy(HARD_PASS)
    uncertain["sofa_back_contact"] = "uncertain"
    uncertain["overall"] = "fail"
    uncertain["unsafe_codes"] = ["SOFA_WALL_CONTACT_UNCERTAIN"]

    calls = []

    def fake_verifier(_photo, _guide, attempt_number, _plan=None):
        calls.append(attempt_number)
        return copy.deepcopy(uncertain)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=fake_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["disposition"] == "BLOCKED"
    assert result["plan"]["pre_generation_eligible"] is False
    assert "GEOM_NOT_ELIGIBLE" in result["plan"]["unsafe_codes"]
    # uncertain 一路 uncertain 仍然擋死（uncertain is not pass），只是多問幾次
    assert len(calls) == verifier_s2.S2_VERIFY_MAX_ATTEMPTS
    candidates = result["plan"]["candidates"]
    failed_ids = set(result["plan"]["geometry_verification"]["failed_candidate_ids"])
    assert failed_ids
    assert all(
        candidate["eligible"] is False
        for candidate in candidates if candidate["candidate_id"] in failed_ids
    )
    assert any(
        candidate["eligible"] is True
        for candidate in candidates if candidate["candidate_id"] not in failed_ids
    ), "未展示給判官的候選不得被連坐判死"
    assert result["guide_artifact"] is None
    assert calls == list(range(1, verifier_s2.S2_VERIFY_MAX_ATTEMPTS + 1))
    verification = result["plan"]["geometry_verification"]
    assert verification["attempt_count"] == verifier_s2.S2_VERIFY_MAX_ATTEMPTS
    assert verification["corrected"] is False
    assert verification["failed_fields"] == {"sofa_back_contact": "uncertain"}
    assert len(result["verification_history"]) == verifier_s2.S2_VERIFY_MAX_ATTEMPTS


def test_retryable_exception_retries_same_plan_and_second_pass_is_accepted(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    calls = []

    def fake_verifier(_photo, _guide, attempt_number, _plan=None):
        calls.append(attempt_number)
        if attempt_number == 1:
            raise TimeoutError("Gemini request timed out")
        return copy.deepcopy(HARD_PASS)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=fake_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    verification = result["plan"]["geometry_verification"]
    assert result["plan"]["disposition"] == "SAFE_FOR_GENERATION"
    assert calls == [1, 2]
    assert verification["retry_reason"] == "retryable_exception"
    assert verification["corrected"] is False
    assert result["verification_history"][0] == {
        "attempt_number": 1,
        "outcome": "exception",
        "exception_type": "TimeoutError",
        "exception_message": "Gemini request timed out",
        "retryable": True,
    }


def test_flaky_hard_fail_is_rechecked_and_passes(tmp_path):
    """D85B8525｜同一張照片＋同一份合約連跑 8 次得到 3 pass / 5 fail，
    5 次失敗給出 5 種互不相同的欄位組合＝判官雜訊。舊行為讓這個約 37%
    通過率的骰子擋在付費生成前，已付費的單一張圖都生不出來。
    同一份幾何再問一次只是 flash 呼叫，且發生在任何 fal 花費之前。"""
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    noisy = copy.deepcopy(HARD_PASS)
    noisy["sofa_back_contact"] = "fail"
    noisy["overall"] = "fail"
    noisy["unsafe_codes"] = ["SOFA_WALL_CONTACT_FAIL"]
    calls = []

    def flaky_verifier(_photo, _guide, attempt_number, _plan=None):
        calls.append(attempt_number)
        if attempt_number >= 3:
            return copy.deepcopy(HARD_PASS)
        # 真實雜訊的樣態：每次抱怨的欄位都不一樣（D85B8525 實測 walkway /
        # cross_axis / right_wall / tv_wall 忽有忽無）
        verdict = copy.deepcopy(noisy)
        if attempt_number == 2:
            verdict["sofa_back_contact"] = "pass"
            verdict["walkway_connected"] = "fail"
            verdict["unsafe_codes"] = ["WALKWAY_BLOCKED"]
        return verdict

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=flaky_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["geometry_verification"]["status"] == "pass"
    assert result["plan"]["pre_generation_eligible"] is True
    assert result["guide_artifact"] is not None
    assert len(calls) >= 3, "硬失敗必須重問，不可一次就擋死"


def _planner_first_sides(raw: dict) -> tuple[str, str]:
    """規劃器初選的那一側，以及對側。

    這兩個測試驗的是「一側共同失敗後，驗證器切到對側候選」。先試哪一側是規劃器
    排序的結果——一旦候選排序調整（例如把門距安全餘裕納入比較），先試的側別就會
    變，測試會以「情境不再重現」的形式假性失敗，而不是因為驗證器壞了。
    所以側別從 plan 推導，不寫死；行為（失敗→切一次對側）照樣被完整驗證，
    而且仍然走完整的 build_s2_plan，規劃器↔驗證器的銜接沒有被解耦掉。
    """
    plan = geometry_s2.build_s2_plan(
        raw, width=1000, height=700, expected_source_photo_index=0,
        sofa_side="free", can_float=False)
    assert plan["disposition"] == "SAFE_FOR_GENERATION", plan.get("unsafe_codes")
    first = verifier_s2._chosen_candidate(plan)["sofa_side"]
    assert first in ("left", "right"), f"初選不是靠牆候選：{first}"
    other = "right" if first == "left" else "left"
    assert any(
        c["eligible"] and c["sofa_side"] == other for c in plan["candidates"]
    ), f"前提不成立：沒有合格的 {other} 側候選可切換"
    return first, other


def test_common_fail_switches_once_to_best_opposite_candidate(tmp_path, monkeypatch):
    """2CD074F0／BDD0C702｜左側候選共同 fail 後，應改驗最佳右側候選一次。"""
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    # Railway 可把一般上限降到 2；相反側仍必須有且只有一次驗證機會。
    monkeypatch.setattr(verifier_s2, "S2_VERIFY_MAX_ATTEMPTS", 2)
    calls = []
    raw = _safe_geometry()
    first_side, other_side = _planner_first_sides(raw)

    def candidate_verifier(_photo, _guide, attempt_number, plan=None):
        chosen = verifier_s2._chosen_candidate(plan)
        calls.append((attempt_number, chosen.get("candidate_id"), chosen.get("sofa_side")))
        if chosen.get("sofa_side") == other_side:
            return copy.deepcopy(HARD_PASS)
        failed = copy.deepcopy(HARD_PASS)
        failed["sofa_back_contact"] = "fail"
        failed[f"{first_side}_wall_floor_alignment"] = "fail"
        failed["overall"] = "fail"
        failed["unsafe_codes"] = [
            "SOFA_NOT_WALL_ANCHORED", f"{first_side.upper()}_WALL_ALIGNMENT_MISMATCH"]
        return failed

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=raw,
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="free",
        verifier=candidate_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert [side for _attempt, _candidate, side in calls] == [
        first_side, first_side, other_side]
    assert result["plan"]["disposition"] == "SAFE_FOR_GENERATION"
    assert verifier_s2._chosen_candidate(result["plan"])["sofa_side"] == other_side
    retry = result["plan"]["geometry_verification"]["candidate_retry"]
    assert retry["from_sofa_side"] == first_side
    assert retry["to_sofa_side"] == other_side
    assert retry["trigger_common_failures"] == sorted(
        [f"{first_side}_wall_floor_alignment", "sofa_back_contact"])
    failed_ids = set(result["plan"]["geometry_verification"]["failed_candidate_ids"])
    assert retry["from_candidate_id"] in failed_ids
    failed_candidate = next(
        candidate for candidate in result["plan"]["candidates"]
        if candidate["candidate_id"] == retry["from_candidate_id"]
    )
    assert failed_candidate["eligible"] is False


def test_valid_wall_correction_from_opposite_candidate_replans_and_reverifies(
    tmp_path, monkeypatch,
):
    """71DC312E｜修正到第 3 次才出現也不能丟掉；應重建 guide 再驗一次。"""
    monkeypatch.setattr(verifier_s2, "S2_VERIFY_MAX_ATTEMPTS", 2)
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    calls = []
    floor_reference_inputs = []
    raw = _safe_geometry()
    first_side, other_side = _planner_first_sides(raw)

    def tracking_floor_reference(_photo, polygon):
        floor_reference_inputs.append(copy.deepcopy(polygon))
        return copy.deepcopy(OBSERVED_FLOOR_REFERENCE)

    def candidate_verifier(_photo, _guide, attempt_number, plan=None):
        chosen = verifier_s2._chosen_candidate(plan)
        calls.append((attempt_number, chosen.get("candidate_id"), chosen.get("sofa_side")))
        if attempt_number == 4:
            return copy.deepcopy(HARD_PASS)
        failed = copy.deepcopy(HARD_PASS)
        failed[f"{first_side}_wall_floor_alignment"] = "fail"
        failed["overall"] = "fail"
        if chosen.get("sofa_side") == first_side:
            failed["sofa_back_contact"] = "fail"
            failed["unsafe_codes"] = ["SOFA_NOT_WALL_ANCHORED"]
        else:
            failed["tv_wall_contact"] = "fail"
            failed["unsafe_codes"] = ["TV_NOT_WALL_ANCHORED"]
            # 修正的是「初選那一側」的牆；座標鏡射以配合側別
            segment = [[820, 90], [357, 430]]
            if first_side == "right":
                segment = [[point[0], 1000 - point[1]] for point in segment]
            failed[f"corrected_{first_side}_wall_floor_segment_yx1000"] = segment
        return failed

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=raw,
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="free",
        verifier=candidate_verifier,
        floor_reference_estimator=tracking_floor_reference,
        can_float=False,
    )

    assert [side for _attempt, _candidate, side in calls] == [
        first_side, first_side, other_side, first_side]
    assert [attempt for attempt, _candidate, _side in calls] == [1, 2, 3, 4]
    assert len(floor_reference_inputs) == 2
    assert floor_reference_inputs[0] != floor_reference_inputs[1]
    assert result["plan"]["disposition"] == "SAFE_FOR_GENERATION"
    assert result["plan"]["geometry_verification"]["corrected"] is True
    retry = result["plan"]["geometry_verification"]["candidate_retry"]
    assert retry["post_correction_candidate_id"] == calls[3][1]
    assert retry["post_correction_sofa_side"] == calls[3][2]
    failed_ids = result["plan"]["geometry_verification"]["failed_candidate_ids"]
    assert result["plan"]["chosen_candidate_id"] not in failed_ids
    assert retry["from_candidate_id"] in retry["pre_correction_failed_candidate_ids"]
    assert retry["post_correction_geometry_revision"] == "wall_correction_1"
    assert result["raw_geometry"]["elements"][
        f"{first_side}_wall_floor"]["status"] == "verifier_corrected"


def test_persistent_hard_fail_blocks_early_without_burning_rechecks(tmp_path):
    """重問不等於放水，也不該白燒：同一組欄位連兩次失敗＝穩定的真問題
    （D85B8525 的 left_wall_floor_alignment 與 sofa_back_contact 12/12 都失敗，
    連牆面修正都救不回），立刻定讞，不再問到上限。"""
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    hard_fail = copy.deepcopy(HARD_PASS)
    hard_fail["sofa_back_contact"] = "fail"
    hard_fail["overall"] = "fail"
    hard_fail["unsafe_codes"] = ["SOFA_WALL_CONTACT_FAIL"]
    calls = []

    def always_fails(_photo, _guide, attempt_number, _plan=None):
        calls.append(attempt_number)
        return copy.deepcopy(hard_fail)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=always_fails,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["disposition"] == "BLOCKED"
    assert result["plan"]["pre_generation_eligible"] is False
    assert len(calls) < verifier_s2.S2_VERIFY_MAX_ATTEMPTS, "穩定失敗不該問到上限"
    assert result["plan"]["geometry_verification"]["failed_fields"] == {
        "sofa_back_contact": "fail"}


def _legacy_hard_fail_blocks_without_retry(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    hard_fail = copy.deepcopy(HARD_PASS)
    hard_fail["sofa_back_contact"] = "fail"
    hard_fail["overall"] = "fail"
    hard_fail["unsafe_codes"] = ["SOFA_WALL_CONTACT_FAIL"]
    calls = []

    def fake_verifier(_photo, _guide, attempt_number, _plan=None):
        calls.append(attempt_number)
        return copy.deepcopy(hard_fail)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=fake_verifier,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["disposition"] == "BLOCKED"
    assert calls == [1]
    verification = result["plan"]["geometry_verification"]
    assert verification["attempt_count"] == 1
    assert verification["failed_fields"] == {"sofa_back_contact": "fail"}
    assert verification["corrected"] is False


@pytest.mark.parametrize("error", [
    RuntimeError("429 resource exhausted"),
    RuntimeError("503 service unavailable"),
])
def test_known_transient_verifier_errors_are_retryable(error):
    assert verifier_s2._is_retryable_verifier_exception(error) is True


def test_exhausted_malformed_json_is_not_retried_twice_again():
    error = verifier_s2.VerifierResponseError("malformed JSON after internal retry")
    assert verifier_s2._is_retryable_verifier_exception(error) is False


def test_missing_transverse_floor_reference_blocks_before_verifier_call(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=lambda *_: (_ for _ in ()).throw(
            AssertionError("missing floor reference must not call verifier")
        ),
        floor_reference_estimator=lambda *_: {
            "status": "missing",
            "direction_xy": None,
            "unsafe_codes": ["MISSING_TRANSVERSE_FLOOR_REFERENCE"],
        },
    )

    assert result["plan"]["disposition"] == "BLOCKED"
    assert result["plan"]["pre_generation_eligible"] is False
    assert result["plan"]["geometry_verification"]["status"] == "fail"
    assert "MISSING_TRANSVERSE_FLOOR_REFERENCE" in result["plan"]["geometry_verification"]["unsafe_codes"]
    assert result["guide_artifact"] is None


def test_gemini_verifier_sends_source_and_guide_and_requires_cross_axis(tmp_path):
    photo = tmp_path / "room.jpg"
    guide = tmp_path / "guide.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    Image.new("RGB", (1000, 700), "gray").save(guide)
    captured = {}

    class FakeResponse:
        text = json.dumps(HARD_PASS)

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeClient:
        models = FakeModels()

    result = verifier_s2.verify_s2_guide_gemini(
        photo, guide, 1, client=FakeClient(),
    )

    assert verifier_s2.verification_passes(result)
    assert captured["model"] == os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    assert len(captured["contents"]) == 3
    prompt = captured["contents"][-1]
    assert "cross_axis_matches_floor_transverse" in prompt
    assert "original source photo" in prompt
    assert "strict JSON" in prompt

# -*- coding: utf-8 -*-
"""Production S2 geometry verification/correction loop. No network calls."""
from __future__ import annotations

import copy
import json
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
        corrected, width=1000, height=700, expected_source_photo_index=0,
    )
    corrected_geometry = [
        item for item in replanned["geometry"]
        if item.get("source_name") in ("left_wall_floor", "left_deep-verifier-corrected")
    ]
    assert corrected_geometry
    assert all(item["evidence_mode"] == "verifier_corrected" for item in corrected_geometry)


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


def test_uncertain_verifier_result_blocks_every_candidate(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    uncertain = copy.deepcopy(HARD_PASS)
    uncertain["sofa_back_contact"] = "uncertain"
    uncertain["overall"] = "fail"
    uncertain["unsafe_codes"] = ["SOFA_WALL_CONTACT_UNCERTAIN"]

    result = verifier_s2.verify_and_replan_s2(
        raw_geometry=_safe_geometry(),
        photo_path=photo,
        output_dir=tmp_path,
        expected_source_photo_index=0,
        sofa_side="right",
        verifier=lambda *_: uncertain,
        floor_reference_estimator=_observed_floor_reference,
    )

    assert result["plan"]["disposition"] == "BLOCKED"
    assert result["plan"]["pre_generation_eligible"] is False
    assert "GEOM_NOT_ELIGIBLE" in result["plan"]["unsafe_codes"]
    assert all(not candidate["eligible"] for candidate in result["plan"]["candidates"])
    assert result["guide_artifact"] is None


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
    assert captured["model"] == "gemini-3.5-flash"
    assert len(captured["contents"]) == 3
    prompt = captured["contents"][-1]
    assert "cross_axis_matches_floor_transverse" in prompt
    assert "original source photo" in prompt
    assert "strict JSON" in prompt

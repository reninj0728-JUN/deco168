# -*- coding: utf-8 -*-
"""S2 structural geometry regression tests. No renderer or network calls."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import layout_contract_v1 as lcv1
import layout_geometry_s2 as s2
from jsonschema import Draft202012Validator
from PIL import Image


def _observed(kind: str, **shape):
    return {
        "kind": kind,
        "status": "observed",
        "confidence": "high",
        "visibility": "full",
        **shape,
    }


def _safe_geometry() -> dict:
    return {
        "schema_version": "struct-geometry-v1",
        "source_photo_index": 0,
        "elements": {
            "door_quad": _observed(
                "door_quad",
                polygon_yx1000=[[600, 20], [600, 190], [970, 190], [970, 20]],
            ),
            "door_floor_contact": _observed(
                "door_floor_contact_edge",
                segment_yx1000=[[970, 20], [970, 190]],
            ),
            "entrance_landing": _observed(
                "entrance_landing",
                polygon_yx1000=[[720, 0], [720, 300], [1000, 330], [1000, 0]],
            ),
            "walkway": _observed(
                "walkway",
                polygon_yx1000=[[430, 430], [430, 570], [1000, 620], [1000, 380]],
            ),
            "living_floor": _observed(
                "living_floor",
                polygon_yx1000=[[970, 0], [970, 1000], [355, 570], [355, 430]],
            ),
            "left_wall_floor": _observed(
                "wall_floor_boundary",
                segment_yx1000=[[970, 0], [355, 430]],
            ),
            "right_wall_floor": _observed(
                "wall_floor_boundary",
                segment_yx1000=[[970, 1000], [355, 570]],
            ),
        },
        "usable_wall_segments": [
            {
                "id": "left_deep",
                "side": "left",
                "status": "observed",
                "confidence": "high",
                "visibility": "full",
                "t_start": 0.52,
                "t_end": 0.92,
            },
            {
                "id": "right_deep",
                "side": "right",
                "status": "observed",
                "confidence": "high",
                "visibility": "full",
                "t_start": 0.52,
                "t_end": 0.92,
            },
        ],
    }


def test_missing_door_floor_contact_blocks():
    raw = _safe_geometry()
    raw["elements"].pop("door_floor_contact")

    plan = s2.build_s2_plan(raw, width=1000, height=700, expected_source_photo_index=0)

    assert plan["disposition"] == "BLOCKED"
    assert "MISSING_DOOR_FLOOR_CONTACT" in plan["unsafe_codes"]
    assert not plan["pre_generation_eligible"]


def test_source_photo_mismatch_blocks_before_geometry_use():
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=1,
    )

    assert plan["disposition"] == "BLOCKED"
    assert "CROSS_PHOTO_COORDS" in plan["unsafe_codes"]
    assert plan["geometry"] == []


def test_missing_real_wall_evidence_blocks():
    raw = _safe_geometry()
    raw["elements"]["right_wall_floor"]["status"] = "inferred"

    plan = s2.build_s2_plan(raw, width=1000, height=700, expected_source_photo_index=0)

    assert plan["disposition"] == "BLOCKED"
    assert "MISSING_WALL_PLANE_EVIDENCE" in plan["unsafe_codes"]
    assert not plan["pre_generation_eligible"]


def test_near_wall_band_hitting_entrance_never_becomes_safe():
    raw = _safe_geometry()
    # Only the near wall band remains usable. Both furniture assignments hit the entrance zone.
    for segment in raw["usable_wall_segments"]:
        segment["t_start"] = 0.02
        segment["t_end"] = 0.24

    plan = s2.build_s2_plan(raw, width=1000, height=700, expected_source_photo_index=0)

    assert plan["disposition"] == "BLOCKED"
    assert "CANDIDATE_HITS_ENTRANCE" in plan["unsafe_codes"]
    assert all(not candidate["eligible"] for candidate in plan["candidates"])


def test_floating_candidate_cannot_overlap_primary_walkway():
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700,
        expected_source_photo_index=0, sofa_side="free",
    )
    floating = [
        candidate for candidate in plan["candidates"]
        if candidate["candidate_type"] == "F"
    ]
    assert floating
    assert all(candidate["floating_walkway_clear"] is False for candidate in floating)
    assert all("FLOAT_NOT_PROVEN" in candidate["fail_codes"] for candidate in floating)
    assert all(candidate["eligible"] is False for candidate in floating)


def test_free_sofa_mode_adds_safe_floating_candidate_with_tv_on_right_wall():
    raw = _safe_geometry()
    raw["elements"]["walkway"]["polygon_yx1000"] = [
        [430, 250], [430, 380], [1000, 420], [1000, 220],
    ]
    plan = s2.build_s2_plan(
        raw, width=1000, height=700,
        expected_source_photo_index=0, sofa_side="free",
    )

    floating = [
        candidate for candidate in plan["candidates"]
        if candidate["candidate_type"] == "F" and candidate["eligible"]
    ]
    assert floating
    chosen = next(
        candidate for candidate in plan["candidates"]
        if candidate["candidate_id"] == plan["chosen_candidate_id"]
    )
    assert chosen["candidate_type"] == "F"
    assert chosen["sofa_side"] == "free"
    assert chosen["tv_side"] == "right"
    assert chosen["invariants"]["entrance_landing_clear"] is True
    assert chosen["invariants"]["walkway_clear"] is True
    assert chosen["floating_walkway_clear"] is True
    assert chosen["invariants"]["float_proven"] is True
    assert chosen["invariants"]["view_axis_clear_of_door"] is True


def test_safe_candidate_faces_tv_and_keeps_door_off_axis():
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )

    assert plan["disposition"] == "SAFE_FOR_GENERATION"
    assert plan["pre_generation_eligible"] is True
    chosen = next(c for c in plan["candidates"] if c["candidate_id"] == plan["chosen_candidate_id"])
    assert chosen["eligible"] is True
    assert chosen["invariants"]["sofa_faces_tv"] is True
    assert chosen["invariants"]["view_axis_clear_of_door"] is True
    assert chosen["invariants"]["entrance_landing_clear"] is True
    assert chosen["invariants"]["walkway_clear"] is True
    for key in (
        "sofa_footprint", "tv_footprint", "tv_wall_contact", "view_axis",
        "sofa_orientation", "tv_orientation",
    ):
        assert chosen[key]


def test_seven_point_living_floor_from_60b89463_remains_plannable():
    raw = _safe_geometry()
    elements = raw["elements"]
    elements["door_quad"]["polygon_yx1000"] = [
        [330, 120], [380, 258], [740, 258], [850, 128],
    ]
    elements["door_floor_contact"]["segment_yx1000"] = [
        [850, 128], [740, 258],
    ]
    elements["entrance_landing"]["polygon_yx1000"] = [
        [850, 128], [740, 258], [830, 380], [1000, 200],
    ]
    elements["walkway"]["polygon_yx1000"] = [
        [740, 258], [580, 430], [580, 500], [1000, 400],
    ]
    elements["living_floor"]["polygon_yx1000"] = [
        [850, 128], [580, 430], [560, 450], [560, 580],
        [620, 615], [1000, 970], [1000, 150],
    ]
    elements["left_wall_floor"]["segment_yx1000"] = [
        [850, 128], [580, 430],
    ]
    elements["right_wall_floor"]["segment_yx1000"] = [
        [1000, 970], [620, 615],
    ]
    raw["usable_wall_segments"] = [
        {
            "id": "right_continuous_wall", "side": "right",
            "status": "observed", "confidence": "high", "visibility": "full",
            "t_start": 0.0, "t_end": 1.0,
        },
        {
            "id": "left_wall_past_door", "side": "left",
            "status": "observed", "confidence": "high", "visibility": "full",
            "t_start": 0.35, "t_end": 1.0,
        },
    ]

    plan = s2.build_s2_plan(
        raw, width=1536, height=1024,
        expected_source_photo_index=0, sofa_side="free",
    )

    assert plan["disposition"] == "SAFE_FOR_GENERATION"
    assert plan["unsafe_codes"] == []
    chosen = next(
        candidate for candidate in plan["candidates"]
        if candidate["candidate_id"] == plan["chosen_candidate_id"]
    )
    assert chosen["eligible"] is True
    assert chosen["invariants"]["entrance_landing_clear"] is True
    assert chosen["invariants"]["walkway_clear"] is True
    assert chosen["invariants"]["view_axis_clear_of_door"] is True


def test_inferred_geometry_can_veto_but_never_grant():
    raw = _safe_geometry()
    raw["elements"]["entrance_landing"]["status"] = "inferred"

    plan = s2.build_s2_plan(raw, width=1000, height=700, expected_source_photo_index=0)

    assert plan["disposition"] == "BLOCKED"
    assert "GEOM_NOT_ELIGIBLE" in plan["unsafe_codes"]


def test_s2_safe_contract_passes_frozen_schema(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )
    plan["transverse_reference"] = {
        "status": "observed",
        "confidence": "high",
        "direction_xy": [1.0, 0.0],
        "angle_degrees": 0.0,
        "support_count": 20,
        "unsafe_codes": [],
    }
    plan["geometry_verification"] = {
        "status": "pass",
        "attempt_count": 1,
        "corrected": False,
        "unsafe_codes": [],
        "detail": "test verifier pass",
    }

    contract = lcv1.build_layout_contract_s2(
        job_id="job-s2-safe",
        photo_path=photo,
        photo_key="uploads/job-s2-safe/room.jpg",
        view_index=0,
        s2_plan=plan,
        photo_binding_verified=True,
        legacy_zoning={"best_photo_index": 0},
        legacy_shadow=None,
        created_at="2026-07-17T00:00:00Z",
    )

    schema = json.loads(
        (Path(__file__).parents[1] / "docs" / "shared_geometry_contract_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(contract)
    assert contract["decision"]["disposition"] == "SAFE_FOR_GENERATION"
    assert contract["decision"]["pre_generation_eligible"] is True
    chosen = next(
        candidate for candidate in contract["candidates"]
        if candidate["candidate_id"] == contract["decision"]["chosen_candidate_id"]
    )
    assert chosen["eligibility"]["status"] == "ELIGIBLE"
    expected_constraint_count = 14 if chosen["candidate_type"] == "F" else 13
    assert len(chosen["constraints"]) == expected_constraint_count
    assert all(item["status"] == "PASS" for item in chosen["constraints"])
    if chosen["candidate_type"] == "F":
        assert any(
            item["check_code"] == "FLOAT_PROVEN" and item["status"] == "PASS"
            for item in chosen["constraints"]
        )
    assert lcv1.recompute_contract_hash(contract) == contract["version_chain"]["contract_hash"]


def test_s2_contract_requires_explicit_safe_planner_fields(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )
    plan["transverse_reference"] = {
        "status": "observed", "confidence": "high", "direction_xy": [1.0, 0.0],
        "angle_degrees": 0.0, "support_count": 20, "unsafe_codes": [],
    }
    plan["geometry_verification"] = {
        "status": "pass", "attempt_count": 1, "corrected": False,
        "unsafe_codes": [], "detail": "pass",
    }
    variants = []
    missing_unsafe = copy.deepcopy(plan)
    missing_unsafe.pop("unsafe_codes", None)
    variants.append(missing_unsafe)
    null_verifier_unsafe = copy.deepcopy(plan)
    null_verifier_unsafe["geometry_verification"]["unsafe_codes"] = None
    variants.append(null_verifier_unsafe)
    wrong_disposition = copy.deepcopy(plan)
    wrong_disposition["disposition"] = "BLOCKED"
    variants.append(wrong_disposition)

    for index, unsafe_plan in enumerate(variants):
        contract = lcv1.build_layout_contract_s2(
            job_id=f"unsafe-{index}", photo_path=photo, photo_key="room.jpg",
            view_index=0, s2_plan=unsafe_plan, photo_binding_verified=True,
            legacy_zoning=None, legacy_shadow=None,
            created_at="2026-07-16T00:00:00Z",
        )
        assert contract["decision"]["disposition"] == "BLOCKED"


def test_s2_contract_blocks_without_observed_transverse_reference(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )
    plan["geometry_verification"] = {
        "status": "pass",
        "attempt_count": 1,
        "corrected": False,
        "unsafe_codes": [],
    }

    contract = lcv1.build_layout_contract_s2(
        job_id="job-s2-missing-transverse",
        photo_path=photo,
        photo_key="uploads/job-s2-missing-transverse/room.jpg",
        view_index=0,
        s2_plan=plan,
        photo_binding_verified=True,
        legacy_zoning={"best_photo_index": 0},
        legacy_shadow=None,
        created_at="2026-07-17T00:00:00Z",
    )

    assert contract["decision"]["disposition"] == "BLOCKED"
    assert contract["decision"]["pre_generation_eligible"] is False
    assert "GEOM_NOT_ELIGIBLE" in contract["decision"]["unsafe_codes"]


def test_s2_contract_never_grants_without_final_geometry_verification(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    unverified_plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )

    contract = lcv1.build_layout_contract_s2(
        job_id="job-s2-unverified",
        photo_path=photo,
        photo_key="uploads/job-s2-unverified/room.jpg",
        view_index=0,
        s2_plan=unverified_plan,
        photo_binding_verified=True,
        legacy_zoning={"best_photo_index": 0},
        legacy_shadow=None,
    )

    assert contract["decision"]["disposition"] == "BLOCKED"
    assert contract["decision"]["pre_generation_eligible"] is False
    assert "GEOM_NOT_ELIGIBLE" in contract["decision"]["unsafe_codes"]


def test_s2_contract_never_grants_when_photo_binding_is_false(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )

    contract = lcv1.build_layout_contract_s2(
        job_id="job-s2-unbound",
        photo_path=photo,
        photo_key="uploads/job-s2-unbound/room.jpg",
        view_index=0,
        s2_plan=plan,
        photo_binding_verified=False,
        legacy_zoning={"best_photo_index": 0},
        legacy_shadow=None,
    )

    assert contract["decision"]["disposition"] == "BLOCKED"
    assert contract["decision"]["pre_generation_eligible"] is False
    assert "MISSING_PHOTO_BINDING" in contract["decision"]["unsafe_codes"]


def test_s2_guide_is_rendered_from_chosen_candidate_on_bound_source(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )
    guide_path = tmp_path / "layout_guide_s2.jpg"

    artifact = s2.render_s2_guide(photo, guide_path, plan)

    assert artifact["path"] == str(guide_path)
    assert len(artifact["sha256"]) == 64
    assert artifact["size"] == {"width": 1000, "height": 700}
    assert guide_path.exists()
    with Image.open(guide_path) as guide:
        assert guide.size == (1000, 700)
    assert artifact["chosen_candidate_id"] == plan["chosen_candidate_id"]


def test_blocked_plan_audit_renders_every_candidate_and_fail_code(tmp_path):
    raw = _safe_geometry()
    for segment in raw["usable_wall_segments"]:
        segment["t_start"] = 0.02
        segment["t_end"] = 0.24
    plan = s2.build_s2_plan(
        raw, width=1000, height=700, expected_source_photo_index=0,
    )
    assert plan["disposition"] == "BLOCKED"
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    out = tmp_path / "audit.png"

    artifact = s2.render_s2_audit(photo, out, plan)

    assert out.exists()
    assert artifact["candidate_count"] == len(plan["candidates"])
    assert artifact["candidate_count"] >= 2
    assert {candidate["candidate_type"] for candidate in plan["candidates"]} == {"A", "B", "F"}
    assert any(
        candidate["candidate_type"] == "F"
        and "FLOAT_NOT_PROVEN" in candidate["fail_codes"]
        for candidate in plan["candidates"]
    )
    assert set(artifact["fail_codes"]) == {
        code for candidate in plan["candidates"] for code in candidate["fail_codes"]
    }
    assert Image.open(out).width >= 1000


def test_view_axis_may_point_near_door_but_is_safe_when_ray_misses_portal():
    door_quad = [(10.0, 430.0), (180.0, 430.0), (180.0, 690.0), (10.0, 690.0)]
    door_contact = [(10.0, 690.0), (180.0, 690.0)]

    assert s2._view_ray_clear_of_door(
        (850.0, 320.0), (300.0, 320.0), door_quad, door_contact,
    ) is True


def test_view_ray_through_door_portal_is_blocked():
    door_quad = [(10.0, 430.0), (180.0, 430.0), (180.0, 690.0), (10.0, 690.0)]
    door_contact = [(10.0, 690.0), (180.0, 690.0)]

    assert s2._view_ray_clear_of_door(
        (850.0, 300.0), (500.0, 480.0), door_quad, door_contact,
    ) is False


def test_walkway_edge_overlap_is_allowed_when_connected_clear_path_remains():
    walkway = [(400.0, 100.0), (600.0, 100.0), (600.0, 900.0), (400.0, 900.0)]
    edge_obstacle = [(400.0, 350.0), (485.0, 350.0), (485.0, 650.0), (400.0, 650.0)]

    assert s2._walkway_has_connected_path(
        walkway, [edge_obstacle], width=1000, height=1000,
    ) is True


def test_walkway_cross_section_block_is_rejected():
    walkway = [(400.0, 100.0), (600.0, 100.0), (600.0, 900.0), (400.0, 900.0)]
    cross_block = [(350.0, 430.0), (650.0, 430.0), (650.0, 570.0), (350.0, 570.0)]

    assert s2._walkway_has_connected_path(
        walkway, [cross_block], width=1000, height=1000,
    ) is False


def test_planner_searches_multiple_depth_bands_not_only_deepest():
    plan = s2.build_s2_plan(
        _safe_geometry(), width=1000, height=700, expected_source_photo_index=0,
    )

    assert len(plan["candidates"]) >= 4
    spans = {
        candidate["candidate_id"].rsplit("_", 2)[-2:][0]
        for candidate in plan["candidates"]
    }
    assert len(spans) >= 2


def test_e72_cross_room_pairing_uses_shared_transverse_depth_not_local_t():
    # Real E72 calibration coordinates in source pixels. The corrected left usable
    # wall starts much deeper than the full right wall; equal local t is invalid.
    living_floor = [
        (48.384, 2993.76),       # near-left
        (1757.952, 1747.872),    # deep-left
        (2487.744, 1865.808),    # deep-right
        (3983.616, 3008.88),     # near-right
    ]
    left_usable = [(1040.256, 2131.92), (1552.32, 1747.872)]
    right_usable = [(3983.616, 3008.88), (2487.744, 1865.808)]

    paired = s2._paired_cross_sections(
        living_floor, left_usable, right_usable, 0.64, 0.90,
    )

    left_mid = paired["left_mid"]
    right_mid = paired["right_mid"]
    vanishing = paired["transverse_vanishing_point"]
    cross_error = abs(s2._cross2(
        (left_mid[0] - vanishing[0], left_mid[1] - vanishing[1]),
        (right_mid[0] - vanishing[0], right_mid[1] - vanishing[1]),
    ))
    axis_slope = abs((right_mid[1] - left_mid[1]) / (right_mid[0] - left_mid[0]))

    assert cross_error < 1e-3
    assert axis_slope < 0.18
    # The old equal-local-t pairing produced roughly 0.28 and is visibly wrong.
    naive_left_mid = s2._line_point(left_usable, (0.64 + 0.90) / 2)
    naive_right_mid = s2._line_point(right_usable, (0.64 + 0.90) / 2)
    naive_slope = abs(
        (naive_right_mid[1] - naive_left_mid[1])
        / (naive_right_mid[0] - naive_left_mid[0])
    )
    assert naive_slope > 0.20
    assert naive_slope > axis_slope + 0.05


def test_observed_transverse_direction_overrides_untrusted_floor_corners():
    plan = s2.build_s2_plan(
        _safe_geometry(),
        width=1000,
        height=700,
        expected_source_photo_index=0,
        sofa_side="right",
        transverse_direction_xy=[1.0, 0.0],
        transverse_reference={
            "status": "observed",
            "angle_degrees": 0.0,
            "support_count": 20,
        },
    )

    assert plan["disposition"] == "SAFE_FOR_GENERATION"
    chosen = next(
        candidate for candidate in plan["candidates"]
        if candidate["candidate_id"] == plan["chosen_candidate_id"]
    )
    (x1, y1), (x2, y2) = chosen["view_axis"]
    assert abs(y2 - y1) < 1e-6
    assert chosen["cross_axis_slope"] == 0.0
    assert plan["transverse_reference"]["status"] == "observed"


def test_same_side_b_candidate_gets_conservative_entrance_buffer():
    plan = {
        "door_side": "left",
        "geometry": [{
            "geometry_id": "entrance_landing",
            "shape": {"type": "polygon", "coordinates": [
                [100, 700], [300, 600], [350, 800], [120, 900],
            ]},
        }],
    }
    chosen = {
        "candidate_type": "B",
        "sofa_side": "left",
        "sofa_footprint": [
            [500, 500], [650, 450], [760, 460], [610, 520],
        ],
    }

    buffer_poly = s2._compact_entry_buffer_polygon(
        plan, chosen, width=1000, height=1000,
    )

    assert buffer_poly is not None
    assert max(point[0] for point in buffer_poly) <= min(
        point[0] for point in chosen["sofa_footprint"]
    )
    chosen["candidate_type"] = "F"
    assert s2._compact_entry_buffer_polygon(
        plan, chosen, width=1000, height=1000,
    ) is None


def test_same_side_b_compact_candidate_is_generated_and_prioritized():
    plan = s2.build_s2_plan(
        _safe_geometry(),
        width=1000,
        height=700,
        expected_source_photo_index=0,
        sofa_side="left",
    )

    compact = [
        candidate for candidate in plan["candidates"]
        if candidate.get("candidate_type") == "B"
        and candidate.get("compact_entry") is True
        and candidate.get("eligible")
    ]
    regular = [
        candidate for candidate in plan["candidates"]
        if candidate.get("candidate_type") == "B"
        and candidate.get("compact_entry") is not True
        and candidate.get("eligible")
    ]

    assert compact
    assert regular
    assert plan["chosen_candidate_id"] in {
        candidate["candidate_id"] for candidate in compact
    }
    compact_back = compact[0]["sofa_footprint"][:2]
    regular_back = regular[0]["sofa_footprint"][:2]
    assert s2._distance(*compact_back) < s2._distance(*regular_back)

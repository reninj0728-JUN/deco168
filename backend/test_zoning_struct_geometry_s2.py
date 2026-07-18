# -*- coding: utf-8 -*-
"""S2 zoning producer contract tests. No Gemini call."""
from __future__ import annotations

import copy

import zoning_v2


def _payload() -> dict:
    return {
        "best_photo_index": 1,
        "struct_geometry_v1": {
            "schema_version": "struct-geometry-v1",
            "source_photo_index": 1,
            "status": "observed",
            "elements": {
                "door_floor_contact": {
                    "kind": "door_floor_contact_edge",
                    "status": "observed",
                    "confidence": "high",
                    "visibility": "full",
                    "segment_yx1000": [[900, 20], [900, 180]],
                },
            },
            "usable_wall_segments": [],
            "uncertainty_notes": "",
        },
    }


def test_prompt_requires_one_bound_best_photo_and_no_cross_photo_geometry():
    prompt = zoning_v2.PROMPT
    assert "source_photo_index" in prompt
    assert "struct_geometry_v1" in prompt
    assert "不可跨照片拼接座標" in prompt
    assert "door_floor_contact" in prompt
    assert "left_wall_floor" in prompt
    assert "right_wall_floor" in prompt
    assert "usable_wall_segments" in prompt
    assert '"best_photo_index": 0' not in prompt


def test_normalizer_preserves_valid_bound_struct_geometry():
    result = zoning_v2.normalize_struct_geometry_payload(_payload(), photo_count=2)

    struct = result["struct_geometry_v1"]
    assert struct["status"] == "observed"
    assert struct["source_photo_index"] == 1
    assert struct["elements"]["door_floor_contact"]["status"] == "observed"


def test_missing_struct_geometry_is_explicit_missing_not_invented():
    result = zoning_v2.normalize_struct_geometry_payload(
        {"best_photo_index": 0}, photo_count=1,
    )

    struct = result["struct_geometry_v1"]
    assert struct["status"] == "missing"
    assert struct["source_photo_index"] == 0
    assert struct["elements"] == {}
    assert struct["usable_wall_segments"] == []


def test_cross_photo_struct_geometry_is_invalidated_not_rebound():
    payload = _payload()
    payload["struct_geometry_v1"]["source_photo_index"] = 0

    result = zoning_v2.normalize_struct_geometry_payload(payload, photo_count=2)

    struct = result["struct_geometry_v1"]
    assert struct["status"] == "invalid"
    assert struct["source_photo_index"] == 0
    assert "CROSS_PHOTO_COORDS" in struct["validation_errors"]


def test_out_of_range_best_photo_does_not_default_to_zero():
    result = zoning_v2.normalize_struct_geometry_payload(
        {"best_photo_index": 5}, photo_count=2,
    )

    assert result["best_photo_index"] is None
    assert result["struct_geometry_v1"]["source_photo_index"] is None
    assert result["struct_geometry_v1"]["status"] == "invalid"

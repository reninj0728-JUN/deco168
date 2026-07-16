# -*- coding: utf-8 -*-
"""Shared Geometry Contract v1｜S1 shadow writer 回歸。"""
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

import layout_contract_v1 as lcv1


BACKEND = Path(__file__).parent
FIXTURES = BACKEND / "_phase0_layout_contract" / "fixtures"
SCHEMA_PATH = BACKEND.parent / "docs" / "shared_geometry_contract_v1.schema.json"


def _fixture():
    payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
    return FIXTURES / "E72F4ADB.jpg", payload["zoning_v2"]


def test_s1_builder_emits_schema_valid_blocked_contract_from_legacy_fixture():
    photo, zoning = _fixture()
    zoning["struct_keypoints"] = {
        "door_line": [[100, 100], [200, 100]],
        "source_context": "manual_fixture",
    }
    legacy_shadow = {
        "safe_layout": True,
        "chosen": "A_complete_wall_sofa",
        "disposition": "READY_FOR_SHADOW_MODE",
        "candidates": [{"id": "A_complete_wall_sofa", "pass": True}],
    }

    contract = lcv1.build_layout_contract(
        job_id="S1TEST",
        photo_path=photo,
        photo_key="uploads/S1TEST/photo_01.jpg",
        view_index=0,
        legacy_zoning=copy.deepcopy(zoning),
        legacy_shadow=legacy_shadow,
        legacy_bbox_binding_verified=True,
        created_at="2026-07-17T00:00:00Z",
    )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(contract))
    assert errors == [], [e.message for e in errors[:5]]

    assert contract["object_type"] == "layout_contract"
    assert contract["decision"]["disposition"] == "BLOCKED"
    assert contract["decision"]["pre_generation_eligible"] is False
    assert contract["candidates"] == []
    assert contract.get("model_input") is None
    assert contract["source"]["photo_key"] == "uploads/S1TEST/photo_01.jpg"
    assert contract["source"]["sha256"] == hashlib.sha256(photo.read_bytes()).hexdigest()
    assert contract["extensions"]["stage"] == "S1_SHADOW_DUAL_WRITE"
    assert contract["extensions"]["affects_delivery"] is False
    assert contract["legacy_inputs"]["legacy_shadow"]["safe_layout"] is True
    assert all(g["evidence"]["mode"] != "manual_fixture" for g in contract["geometry"])
    assert all(g["eligibility"]["status"] == "INELIGIBLE" for g in contract["geometry"])
    assert contract["version_chain"]["contract_hash"] == lcv1.recompute_contract_hash(contract)


def test_s1_builder_stays_schema_valid_blocked_when_legacy_bbox_is_invalid():
    photo, _ = _fixture()
    zoning = {
        "best_photo_index": 0,
        "existing_zones": {
            "entrance_zone": {"bbox_on_best_photo": [900, 900, 100, 100]},
        },
        "proposed_zones": {},
    }

    contract = lcv1.build_layout_contract(
        job_id="S1INVALID",
        photo_path=photo,
        photo_key="uploads/S1INVALID/photo_01.jpg",
        view_index=0,
        legacy_zoning=zoning,
        legacy_shadow={"safe_layout": True},
        legacy_bbox_binding_verified=True,
        created_at="2026-07-17T00:00:00Z",
    )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(contract))
    assert errors == [], [e.message for e in errors[:5]]
    assert contract["decision"]["disposition"] == "BLOCKED"
    assert contract["decision"]["pre_generation_eligible"] is False
    assert "MISSING_DOOR" in contract["decision"]["unsafe_codes"]
    assert len(contract["geometry"]) == 1
    assert contract["geometry"][0]["status"] == "missing"
    assert contract["geometry"][0]["shape"] is None


def test_s1_builder_does_not_bind_legacy_bbox_without_verified_photo_match():
    photo, zoning = _fixture()

    contract = lcv1.build_layout_contract(
        job_id="S1UNBOUND",
        photo_path=photo,
        photo_key="uploads/S1UNBOUND/photo_02.jpg",
        view_index=1,
        legacy_zoning=zoning,
        legacy_shadow={"safe_layout": True},
        legacy_bbox_binding_verified=False,
        created_at="2026-07-17T00:00:00Z",
    )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(contract))
    assert errors == [], [e.message for e in errors[:5]]
    assert "MISSING_PHOTO_BINDING" in contract["decision"]["unsafe_codes"]
    assert contract["legacy_inputs"]["legacy_bbox_binding_verified"] is False
    assert not any(g["status"] == "available" for g in contract["geometry"])
    assert contract["decision"]["pre_generation_eligible"] is False

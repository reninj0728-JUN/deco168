# -*- coding: utf-8 -*-
"""API-level S2 artifact wiring tests. No network or renderer calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import api
import pytest
from PIL import Image
from test_layout_geometry_s2 import _safe_geometry


VERIFIER_PASS = {
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
    "notes": "測試通過",
}


def test_s2_required_render_enables_image_edit_retry_even_when_env_is_off(monkeypatch):
    monkeypatch.setenv("USE_NANO_BANANA", "0")

    assert api._image_edit_retry_enabled([
        {"_layout_contract_s2_required": True},
    ]) is True
    assert api._image_edit_retry_enabled([
        {"_layout_contract_s2_required": False},
    ]) is False


def test_s2_chosen_candidate_sides_override_legacy_generation_context():
    contract = {
        "decision": {"chosen_candidate_id": "s2_f_free"},
        "candidates": [{
            "candidate_id": "s2_f_free",
            "notes": ["sofa_side=free", "tv_side=right"],
        }],
    }
    zoning = {
        "_sofa_layout": "left",
        "furniture_placement_rules": {"sofa_side": "left", "tv_side": "right"},
    }

    sides = api._sync_s2_candidate_sides(zoning, contract)

    assert sides == {"sofa_side": "free", "tv_side": "right"}
    assert zoning["_sofa_layout"] == "free"
    assert zoning["furniture_placement_rules"]["sofa_side"] == "free"
    assert zoning["furniture_placement_rules"]["tv_side"] == "right"


def test_run_layout_contract_s2_writes_safe_bound_artifacts(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": "upload/room.jpg",
            "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
        "proposed_zones": {"living_zone": {"sofa_side": "right"}},
    }

    summary, artifacts = api._run_layout_contract_s2(
        job_id="job-s2-api",
        job_dir=tmp_path,
        photo_path=str(photo),
        view_index=0,
        user_zoning_v2=zoning,
        legacy_zoning=zoning,
        sofa_mode="free",
        image_paths=[str(photo)],
        geometry_verifier=lambda *_: dict(VERIFIER_PASS),
        floor_reference_estimator=lambda *_: {
            "status": "observed",
            "confidence": "high",
            "direction_xy": [1.0, 0.0],
            "angle_degrees": 0.0,
            "support_count": 20,
            "unsafe_codes": [],
        },
    )

    assert summary["status"] == "safe"
    assert summary["contract_v1_disposition"] == "SAFE_FOR_GENERATION"
    assert artifacts["eligible"] is True
    assert artifacts["contract"]["decision"]["chosen_candidate_id"]
    assert Path(artifacts["contract_path"]).exists()
    assert Path(artifacts["guide_path"]).exists()
    assert Path(artifacts["reconciliation_path"]).exists()
    contract = json.loads(Path(artifacts["contract_path"]).read_text(encoding="utf-8"))
    assert contract["source"]["sha256"] == hashlib.sha256(photo.read_bytes()).hexdigest()
    assert contract["decision"]["pre_generation_eligible"] is True

    generation_zoning = {
        "_sofa_layout": "left",
        "furniture_placement_rules": {"sofa_side": "left", "tv_side": "right"},
    }
    api._sync_s2_candidate_sides(generation_zoning, artifacts["contract"])
    chosen_id = artifacts["contract"]["decision"]["chosen_candidate_id"]
    chosen = next(c for c in artifacts["contract"]["candidates"]
                  if c["candidate_id"] == chosen_id)
    expected_sofa = next(n.split("=", 1)[1] for n in chosen["notes"]
                         if n.startswith("sofa_side="))
    assert generation_zoning["_sofa_layout"] == expected_sofa


def test_run_layout_contract_s2_exposes_verifier_exception_and_history(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": "upload/room.jpg",
            "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
    }

    def timeout_verifier(*_args):
        raise TimeoutError("Gemini verifier timed out")

    summary, artifacts = api._run_layout_contract_s2(
        job_id="job-s2-timeout",
        job_dir=tmp_path,
        photo_path=str(photo),
        view_index=0,
        user_zoning_v2=zoning,
        legacy_zoning=zoning,
        sofa_mode="right",
        image_paths=[str(photo)],
        geometry_verifier=timeout_verifier,
        floor_reference_estimator=lambda *_: {
            "status": "observed",
            "confidence": "high",
            "direction_xy": [1.0, 0.0],
            "angle_degrees": 0.0,
            "support_count": 20,
            "unsafe_codes": [],
        },
    )

    assert summary["status"] == "blocked"
    assert summary["verification_status"] == "fail"
    assert summary["verification_attempt_count"] == 2
    assert summary["verification_corrected"] is False
    assert summary["verification_retry_reason"] == "retryable_exception"
    assert summary["verification_exception_type"] == "TimeoutError"
    assert "exception=TimeoutError" in summary["reason"]
    assert len(summary["verification_history"]) == 2
    assert summary["verification_history"] == artifacts["verification_history"]
    saved = json.loads(
        Path(artifacts["verification_path"]).read_text(encoding="utf-8")
    )
    assert len(saved["history"]) == 2
    assert saved["history"][-1]["exception_type"] == "TimeoutError"


@pytest.mark.parametrize("orientation", [6, 8])
def test_run_layout_contract_s2_uses_exif_transposed_dimensions(tmp_path, orientation):
    photo = tmp_path / f"orientation{orientation}.jpg"
    exif = Image.Exif()
    exif[274] = orientation
    Image.new("RGB", (1000, 700), "white").save(photo, exif=exif)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": photo.name,
            "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
    }
    summary, artifacts = api._run_layout_contract_s2(
        job_id=f"job-exif-{orientation}", job_dir=tmp_path,
        photo_path=str(photo), view_index=0, user_zoning_v2=zoning,
        legacy_zoning=zoning, sofa_mode="free", image_paths=[str(photo)],
        geometry_verifier=lambda *_: dict(VERIFIER_PASS),
        floor_reference_estimator=lambda *_: {
            "status": "observed", "confidence": "high",
            "direction_xy": [1.0, 0.0], "angle_degrees": 0.0,
            "support_count": 20, "unsafe_codes": [],
        },
    )
    assert summary["status"] == "safe"
    assert artifacts["contract"]["source"]["size"] == {"width": 700, "height": 1000}


def test_run_layout_contract_s2_wrong_photo_is_blocked_without_guide(tmp_path):
    best = tmp_path / "best.jpg"
    wrong = tmp_path / "wrong.jpg"
    Image.new("RGB", (1000, 700), "white").save(best)
    Image.new("RGB", (1000, 700), "gray").save(wrong)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": "upload/best.jpg",
            "sha256": hashlib.sha256(best.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
    }

    summary, artifacts = api._run_layout_contract_s2(
        job_id="job-s2-cross-photo",
        job_dir=tmp_path,
        photo_path=str(wrong),
        view_index=1,
        user_zoning_v2=zoning,
        legacy_zoning=zoning,
        sofa_mode="free",
        image_paths=[str(best), str(wrong)],
        geometry_verifier=lambda *_: (_ for _ in ()).throw(
            AssertionError("wrong-photo path must not call geometry verifier")
        ),
    )

    assert summary["status"] == "blocked"
    assert artifacts["eligible"] is False
    assert artifacts["guide_path"] is None
    contract = json.loads(Path(artifacts["contract_path"]).read_text(encoding="utf-8"))
    assert contract["decision"]["disposition"] == "BLOCKED"
    assert "MISSING_PHOTO_BINDING" in contract["decision"]["unsafe_codes"]


def test_s2_feature_flag_defaults_off_and_requires_explicit_one(monkeypatch):
    monkeypatch.delenv("LAYOUT_CONTRACT_S2", raising=False)
    assert api._layout_contract_s2_enabled() is False

    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "1")
    assert api._layout_contract_s2_enabled() is True

    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "0")
    assert api._layout_contract_s2_enabled() is False


def test_s2_compact_entry_mode_only_for_b_candidate_on_entrance_side():
    contract = {
        "decision": {"chosen_candidate_id": "s2_b_left"},
        "candidates": [{
            "candidate_id": "s2_b_left",
            "candidate_type": "B",
            "notes": ["sofa_side=left", "tv_side=right"],
        }],
    }

    assert api._s2_compact_entry_mode({"_entrance_side": "left"}, contract) is True
    assert api._s2_compact_entry_mode({"_entrance_side": "right"}, contract) is False

    contract["candidates"][0]["candidate_type"] = "F"
    assert api._s2_compact_entry_mode({"_entrance_side": "left"}, contract) is False

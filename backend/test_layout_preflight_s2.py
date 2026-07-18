# -*- coding: utf-8 -*-
"""S2 paid-render preflight and formal-artifact binding tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import layout_contract_v1 as lcv1
import layout_geometry_s2 as s2
import layout_preflight_s2 as preflight
from jsonschema import Draft202012Validator
from PIL import Image
from test_layout_geometry_s2 import _safe_geometry


def _artifacts(tmp_path):
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
        job_id="job-preflight",
        photo_path=photo,
        photo_key="uploads/job-preflight/room.jpg",
        view_index=0,
        s2_plan=plan,
        photo_binding_verified=True,
        legacy_zoning={"best_photo_index": 0},
        legacy_shadow=None,
        created_at="2026-07-17T00:00:00Z",
    )
    contract_path = tmp_path / "layout_contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    guide = s2.render_s2_guide(photo, tmp_path / "layout_guide.jpg", plan)
    verification_path = tmp_path / "geometry_verification.json"
    verification_path.write_text(json.dumps({
        "verification": plan["geometry_verification"],
        "history": [],
    }), encoding="utf-8")
    report_path = tmp_path / "reconciliation.json"
    report = preflight.write_reconciliation_report(
        contract=contract,
        guide_artifact=guide,
        verification_artifact_path=verification_path,
        out_path=report_path,
        created_at="2026-07-17T00:00:00Z",
    )
    return photo, contract, contract_path, guide, verification_path, report, report_path


def test_reconciliation_report_passes_frozen_schema(tmp_path):
    _, _, _, _, _, report, _ = _artifacts(tmp_path)
    schema = json.loads(
        (Path(__file__).parents[1] / "docs" / "shared_geometry_contract_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(report)
    assert report["consistent"] is True
    assert report["binding_eligible"] is True


def test_valid_bound_contract_and_guide_pass_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=(
            json.loads(Path(report_path).read_text(encoding="utf-8"))["extensions"]
            ["verification_artifact_sha256"]
        ),
    )

    assert result["ok"] is True
    assert result["reason_codes"] == []


@pytest.mark.parametrize("orientation", [6, 8])
def test_exif_rotated_contract_guide_and_preflight_share_transposed_space(tmp_path, orientation):
    photo = tmp_path / f"orientation{orientation}.jpg"
    exif = Image.Exif()
    exif[274] = orientation
    Image.new("RGB", (400, 200), "white").save(photo, exif=exif)
    plan = s2.build_s2_plan(
        _safe_geometry(), width=200, height=400, expected_source_photo_index=0)
    plan["transverse_reference"] = {
        "status": "observed", "confidence": "high", "direction_xy": [1.0, 0.0],
        "angle_degrees": 0.0, "support_count": 20, "unsafe_codes": [],
    }
    plan["geometry_verification"] = {
        "status": "pass", "attempt_count": 1, "corrected": False,
        "unsafe_codes": [], "detail": "orientation integration test",
    }
    contract = lcv1.build_layout_contract_s2(
        job_id=f"job-exif-{orientation}", photo_path=photo, photo_key=photo.name,
        view_index=0, s2_plan=plan, photo_binding_verified=True,
        legacy_zoning={"best_photo_index": 0}, legacy_shadow=None,
        created_at="2026-07-17T00:00:00Z")
    assert contract["source"]["size"] == {"width": 200, "height": 400}
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    guide = s2.render_s2_guide(photo, tmp_path / "guide.jpg", plan)
    verification_path = tmp_path / "verification.json"
    verification_path.write_text(json.dumps({
        "verification": plan["geometry_verification"], "history": [],
    }), encoding="utf-8")
    report_path = tmp_path / "reconciliation.json"
    report = preflight.write_reconciliation_report(
        contract=contract, guide_artifact=guide,
        verification_artifact_path=verification_path, out_path=report_path,
        created_at="2026-07-17T00:00:00Z")
    result = preflight.validate_s2_preflight(
        contract_path=contract_path, source_path=photo, guide_path=guide["path"],
        verification_path=verification_path, reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=report["extensions"]["verification_artifact_sha256"],
    )
    assert result["ok"] is True
    assert result["reason_codes"] == []


def test_missing_verification_artifact_fails_preflight(tmp_path):
    photo, _, contract_path, guide, _, _, report_path = _artifacts(tmp_path)

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path="",
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=(
            json.loads(Path(report_path).read_text(encoding="utf-8"))["extensions"]
            ["verification_artifact_sha256"]
        ),
    )

    assert result["ok"] is False
    assert "VERIFICATION_UNREADABLE" in result["reason_codes"]


def test_tampered_verification_artifact_fails_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)
    verification_path.write_text(json.dumps({"verification": {"status": "fail"}}), encoding="utf-8")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=(
            json.loads(Path(report_path).read_text(encoding="utf-8"))["extensions"]
            ["verification_artifact_sha256"]
        ),
    )

    assert result["ok"] is False
    assert "VERIFICATION_ARTIFACT_HASH_MISMATCH" in result["reason_codes"]
    assert "FINAL_GEOMETRY_VERIFICATION_NOT_PASSED" in result["reason_codes"]


def test_changed_source_photo_fails_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)
    Image.new("RGB", (1000, 700), "black").save(photo)

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=(
            json.loads(Path(report_path).read_text(encoding="utf-8"))["extensions"]
            ["verification_artifact_sha256"]
        ),
    )

    assert result["ok"] is False
    assert "SOURCE_HASH_MISMATCH" in result["reason_codes"]


def test_changed_guide_fails_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)
    Path(guide["path"]).write_bytes(b"tampered")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=(
            json.loads(Path(report_path).read_text(encoding="utf-8"))["extensions"]
            ["verification_artifact_sha256"]
        ),
    )

    assert result["ok"] is False
    assert "FORMAL_ARTIFACT_HASH_MISMATCH" in result["reason_codes"]


def test_changed_contract_fails_preflight(tmp_path):
    photo, contract, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)
    contract["decision"]["candidate_type"] = (
        "B" if contract["decision"]["candidate_type"] == "A" else "A"
    )
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=(
            json.loads(Path(report_path).read_text(encoding="utf-8"))["extensions"]
            ["verification_artifact_sha256"]
        ),
    )

    assert result["ok"] is False
    assert "CONTRACT_HASH_MISMATCH" in result["reason_codes"]


def test_verifier_pass_with_unsafe_codes_cannot_create_reconciliation(tmp_path):
    _, contract, _, guide, verification_path, _, report_path = _artifacts(tmp_path)
    contract["extensions"]["geometry_verification"]["unsafe_codes"] = ["CONTRADICTORY_PASS"]
    contract["version_chain"]["contract_hash"] = lcv1.recompute_contract_hash(contract)
    verification_path.write_text(json.dumps({
        "verification": contract["extensions"]["geometry_verification"],
        "history": [],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="geometry verification not passed"):
        preflight.write_reconciliation_report(
            contract=contract,
            guide_artifact=guide,
            verification_artifact_path=verification_path,
            out_path=report_path,
            created_at="2026-07-17T00:00:00Z",
        )


def test_tampered_reconciliation_policy_or_mismatches_fails_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, report, report_path = _artifacts(tmp_path)
    report["mismatches"] = ["tampered"]
    report["reconciliation_policy_version"] = "tampered"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=report["extensions"]["verification_artifact_sha256"],
    )

    assert result["ok"] is False
    assert "RECONCILIATION_POLICY_INVALID" in result["reason_codes"]


def test_missing_reconciliation_required_field_fails_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, report, report_path = _artifacts(tmp_path)
    report.pop("report_id", None)
    report["reconciler"].pop("model", None)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=report["extensions"]["verification_artifact_sha256"],
    )

    assert result["ok"] is False
    assert "RECONCILIATION_SCHEMA_INVALID" in result["reason_codes"]


def test_coordinated_contract_and_report_rewrite_fails_runtime_digest(tmp_path):
    photo, contract, contract_path, guide, verification_path, report, report_path = _artifacts(tmp_path)
    original_contract_sha = preflight._sha256_file(contract_path)
    original_report_sha = preflight._sha256_file(report_path)
    contract["geometry"][0]["shape"]["coordinates"][0][0] += 1
    contract["version_chain"]["contract_hash"] = lcv1.recompute_contract_hash(contract)
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    report["contract_hash"] = contract["version_chain"]["contract_hash"]
    report["formal_plan_hash"] = preflight._formal_plan_hash(contract)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=original_contract_sha,
        expected_reconciliation_sha256=original_report_sha,
        expected_guide_sha256=guide["sha256"],
        expected_verification_sha256=report["extensions"]["verification_artifact_sha256"],
    )

    assert result["ok"] is False
    assert "EXPECTED_CONTRACT_HASH_MISMATCH" in result["reason_codes"]
    assert "EXPECTED_RECONCILIATION_HASH_MISMATCH" in result["reason_codes"]


def test_coordinated_guide_and_report_replacement_fails_runtime_digest(tmp_path):
    photo, _, contract_path, guide, verification_path, report, report_path = _artifacts(tmp_path)
    original_guide_sha = guide["sha256"]
    Image.new("RGB", (1000, 700), "black").save(guide["path"])
    report["formal_artifact"]["sha256"] = preflight._sha256_file(guide["path"])
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = preflight.validate_s2_preflight(
        contract_path=contract_path,
        source_path=photo,
        guide_path=guide["path"],
        verification_path=verification_path,
        reconciliation_path=report_path,
        expected_contract_sha256=preflight._sha256_file(contract_path),
        expected_reconciliation_sha256=preflight._sha256_file(report_path),
        expected_guide_sha256=original_guide_sha,
        expected_verification_sha256=report["extensions"]["verification_artifact_sha256"],
    )

    assert result["ok"] is False
    assert "EXPECTED_GUIDE_HASH_MISMATCH" in result["reason_codes"]

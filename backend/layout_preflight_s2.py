# -*- coding: utf-8 -*-
"""S2 Contract ↔ formal guide reconciliation and paid-render preflight."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from layout_contract_v1 import recompute_contract_hash


RECONCILIATION_POLICY_VERSION = "s2-formal-artifact-binding-v1"


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_size(path: str | Path) -> dict:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        width, height = image.size
    return {"width": int(width), "height": int(height)}


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _formal_plan_hash(contract: dict) -> str:
    chosen_id = (contract.get("decision") or {}).get("chosen_candidate_id")
    candidate = next(
        (item for item in contract.get("candidates") or []
         if item.get("candidate_id") == chosen_id),
        None,
    )
    geometry_ids = set()
    if candidate:
        for key, value in candidate.items():
            if key.endswith("_geometry_id") and isinstance(value, str):
                geometry_ids.add(value)
        for constraint in candidate.get("constraints") or []:
            geometry_ids.update(constraint.get("geometry_ids") or [])
    geometry = [
        item for item in contract.get("geometry") or []
        if item.get("geometry_id") in geometry_ids
    ]
    extensions = contract.get("extensions") or {}
    return _canonical_hash({
        "candidate": candidate,
        "geometry": geometry,
        "geometry_verification": extensions.get("geometry_verification"),
        "transverse_reference": extensions.get("transverse_reference"),
    })


def write_reconciliation_report(
    *,
    contract: dict,
    guide_artifact: dict,
    verification_artifact_path: str | Path,
    out_path: str | Path,
    created_at: str | None = None,
) -> dict:
    """Bind one rendered S2 guide to one immutable Contract candidate."""
    guide_path = Path(str(guide_artifact.get("path") or ""))
    if not guide_path.is_file():
        raise FileNotFoundError(str(guide_path))
    actual_hash = _sha256_file(guide_path)
    actual_size = _image_size(guide_path)
    if actual_hash != guide_artifact.get("sha256") or actual_size != guide_artifact.get("size"):
        raise ValueError("guide artifact metadata does not match file")
    contract_hash = recompute_contract_hash(contract)
    if contract_hash != (contract.get("version_chain") or {}).get("contract_hash"):
        raise ValueError("contract hash mismatch")
    chosen_id = (contract.get("decision") or {}).get("chosen_candidate_id")
    if chosen_id != guide_artifact.get("chosen_candidate_id"):
        raise ValueError("guide candidate does not match contract decision")

    verification_path = Path(verification_artifact_path)
    try:
        verification_document = json.loads(verification_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("verification artifact unreadable") from exc
    extensions = contract.get("extensions") or {}
    contract_verification = extensions.get("geometry_verification") or {}
    transverse_reference = extensions.get("transverse_reference") or {}
    if (
        contract_verification.get("status") != "pass"
        or contract_verification.get("unsafe_codes") != []
    ):
        raise ValueError("contract geometry verification not passed")
    if (verification_document.get("verification") or {}) != contract_verification:
        raise ValueError("verification artifact does not match contract")
    direction_xy = transverse_reference.get("direction_xy")
    if not (
        transverse_reference.get("status") == "observed"
        and isinstance(direction_xy, (list, tuple))
        and len(direction_xy) == 2
        and int(transverse_reference.get("support_count") or 0) > 0
        and transverse_reference.get("unsafe_codes") == []
    ):
        raise ValueError("transverse floor reference not observed")
    verification_hash = _sha256_file(verification_path)
    evidence_hash = _canonical_hash({
        "geometry_verification": contract_verification,
        "transverse_reference": transverse_reference,
    })

    timestamp = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report = {
        "object_type": "contract_reconciliation_report",
        "schema_version": "1.0.0-draft",
        "report_id": f"lcr_{uuid.uuid4().hex}",
        "contract_id": contract["version_chain"]["contract_id"],
        "contract_hash": contract_hash,
        "formal_artifact": {
            "artifact_id": f"guide_{actual_hash[:20]}",
            "artifact_type": "layout_guide",
            "sha256": actual_hash,
            "size": actual_size,
            "coordinate_space": "source_px_xy",
            "path": str(guide_path),
        },
        "formal_plan_hash": _formal_plan_hash(contract),
        "reconciler": {
            "name": "layout_preflight_s2.write_reconciliation_report",
            "version": RECONCILIATION_POLICY_VERSION,
            "model": None,
        },
        "reconciliation_policy_version": RECONCILIATION_POLICY_VERSION,
        "created_at": timestamp,
        "consistent": True,
        "mismatches": [],
        "binding_eligible": True,
        "extensions": {
            "chosen_candidate_id": chosen_id,
            "verification_artifact_sha256": verification_hash,
            "verification_artifact_path": str(verification_path),
            "formal_evidence_hash": evidence_hash,
        },
    }
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def validate_s2_preflight(
    *,
    contract_path: str | Path,
    source_path: str | Path,
    guide_path: str | Path,
    verification_path: str | Path,
    reconciliation_path: str | Path,
    expected_contract_sha256: str,
    expected_reconciliation_sha256: str,
    expected_guide_sha256: str,
    expected_verification_sha256: str,
) -> dict:
    """Validate all immutable bindings immediately before a paid renderer call."""
    reasons = []
    try:
        contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
        contract_file_hash = _sha256_file(contract_path)
    except Exception:
        return {"ok": False, "reason_codes": ["CONTRACT_UNREADABLE"]}
    try:
        report = json.loads(Path(reconciliation_path).read_text(encoding="utf-8"))
        reconciliation_file_hash = _sha256_file(reconciliation_path)
    except Exception:
        return {"ok": False, "reason_codes": ["RECONCILIATION_UNREADABLE"]}
    if not expected_contract_sha256:
        reasons.append("EXPECTED_CONTRACT_HASH_MISSING")
    elif contract_file_hash != expected_contract_sha256:
        reasons.append("EXPECTED_CONTRACT_HASH_MISMATCH")
    if not expected_reconciliation_sha256:
        reasons.append("EXPECTED_RECONCILIATION_HASH_MISSING")
    elif reconciliation_file_hash != expected_reconciliation_sha256:
        reasons.append("EXPECTED_RECONCILIATION_HASH_MISMATCH")
    try:
        verification_document = json.loads(Path(verification_path).read_text(encoding="utf-8"))
        verification_hash = _sha256_file(verification_path)
    except Exception:
        verification_document, verification_hash = {}, None
        reasons.append("VERIFICATION_UNREADABLE")
    if not expected_verification_sha256:
        reasons.append("EXPECTED_VERIFICATION_HASH_MISSING")
    elif verification_hash is not None and verification_hash != expected_verification_sha256:
        reasons.append("EXPECTED_VERIFICATION_HASH_MISMATCH")

    stored_hash = (contract.get("version_chain") or {}).get("contract_hash")
    try:
        computed_hash = recompute_contract_hash(contract)
    except Exception:
        computed_hash = None
    if not stored_hash or computed_hash != stored_hash:
        reasons.append("CONTRACT_HASH_MISMATCH")

    contract_extensions = contract.get("extensions") or {}
    contract_verification = contract_extensions.get("geometry_verification") or {}
    transverse_reference = contract_extensions.get("transverse_reference") or {}
    direction_xy = transverse_reference.get("direction_xy")
    if (
        contract_verification.get("status") != "pass"
        or contract_verification.get("unsafe_codes") != []
    ):
        reasons.append("FINAL_GEOMETRY_VERIFICATION_NOT_PASSED")
    artifact_verification = verification_document.get("verification") or {}
    if (
        artifact_verification.get("status") != "pass"
        or artifact_verification.get("unsafe_codes") != []
    ):
        reasons.append("FINAL_GEOMETRY_VERIFICATION_NOT_PASSED")
    if verification_document and artifact_verification != contract_verification:
        reasons.append("VERIFICATION_CONTRACT_MISMATCH")
    if not (
        transverse_reference.get("status") == "observed"
        and isinstance(direction_xy, (list, tuple))
        and len(direction_xy) == 2
        and int(transverse_reference.get("support_count") or 0) > 0
        and transverse_reference.get("unsafe_codes") == []
    ):
        reasons.append("TRANSVERSE_REFERENCE_NOT_OBSERVED")

    decision = contract.get("decision") or {}
    if not (
        decision.get("disposition") == "SAFE_FOR_GENERATION"
        and decision.get("pre_generation_eligible") is True
        and decision.get("unsafe_codes") == []
    ):
        reasons.append("CONTRACT_NOT_ELIGIBLE")
    chosen_id = decision.get("chosen_candidate_id")
    chosen = next(
        (item for item in contract.get("candidates") or []
         if item.get("candidate_id") == chosen_id),
        None,
    )
    if not chosen or (chosen.get("eligibility") or {}).get("status") != "ELIGIBLE":
        reasons.append("CHOSEN_CANDIDATE_NOT_ELIGIBLE")
    if any(step.get("type") == "unknown" for step in contract.get("transforms") or []):
        reasons.append("TRANSFORM_CHAIN_INVALID")

    try:
        source_hash = _sha256_file(source_path)
        source_size = _image_size(source_path)
    except Exception:
        source_hash, source_size = None, None
        reasons.append("SOURCE_UNREADABLE")
    source = contract.get("source") or {}
    if source_hash is not None and source_hash != source.get("sha256"):
        reasons.append("SOURCE_HASH_MISMATCH")
    if source_size is not None and source_size != source.get("size"):
        reasons.append("SOURCE_SIZE_MISMATCH")

    try:
        guide_hash = _sha256_file(guide_path)
    except Exception:
        guide_hash = None
        reasons.append("FORMAL_ARTIFACT_UNREADABLE")
    if not expected_guide_sha256:
        reasons.append("EXPECTED_GUIDE_HASH_MISSING")
    elif guide_hash is not None and guide_hash != expected_guide_sha256:
        reasons.append("EXPECTED_GUIDE_HASH_MISMATCH")
    try:
        guide_size = _image_size(guide_path)
    except Exception:
        guide_size = None
        reasons.append("FORMAL_ARTIFACT_UNREADABLE")
    artifact = report.get("formal_artifact") or {}
    if guide_hash is not None and guide_hash != artifact.get("sha256"):
        reasons.append("FORMAL_ARTIFACT_HASH_MISMATCH")
    if guide_size is not None and guide_size != artifact.get("size"):
        reasons.append("FORMAL_ARTIFACT_SIZE_MISMATCH")
    if source_size is not None and guide_size is not None and guide_size != source_size:
        reasons.append("FORMAL_ARTIFACT_SOURCE_SIZE_MISMATCH")

    if report.get("contract_id") != (contract.get("version_chain") or {}).get("contract_id"):
        reasons.append("RECONCILIATION_CONTRACT_ID_MISMATCH")
    if report.get("contract_hash") != stored_hash:
        reasons.append("RECONCILIATION_CONTRACT_HASH_MISMATCH")
    if report.get("formal_plan_hash") != _formal_plan_hash(contract):
        reasons.append("FORMAL_PLAN_HASH_MISMATCH")
    if report.get("consistent") is not True or report.get("binding_eligible") is not True:
        reasons.append("RECONCILIATION_NOT_ELIGIBLE")
    reconciler = report.get("reconciler") or {}
    report_structure_valid = bool(
        report.get("object_type") == "contract_reconciliation_report"
        and report.get("schema_version") == "1.0.0-draft"
        and isinstance(report.get("report_id"), str)
        and report.get("report_id", "").startswith("lcr_")
        and isinstance(report.get("created_at"), str)
        and bool(report.get("created_at"))
        and isinstance(report.get("formal_plan_hash"), str)
        and len(report.get("formal_plan_hash")) == 64
        and isinstance(report.get("extensions"), dict)
        and "model" in reconciler
        and reconciler.get("model") is None
    )
    if not report_structure_valid:
        reasons.append("RECONCILIATION_SCHEMA_INVALID")
    if (
        report.get("mismatches") != []
        or report.get("reconciliation_policy_version") != RECONCILIATION_POLICY_VERSION
        or reconciler.get("name") != "layout_preflight_s2.write_reconciliation_report"
        or reconciler.get("version") != RECONCILIATION_POLICY_VERSION
    ):
        reasons.append("RECONCILIATION_POLICY_INVALID")
    artifact_path_matches = False
    try:
        artifact_path_matches = Path(artifact.get("path") or "").resolve() == Path(guide_path).resolve()
    except Exception:
        artifact_path_matches = False
    if (
        not isinstance(artifact.get("artifact_id"), str)
        or guide_hash is None
        or artifact.get("artifact_id") != f"guide_{guide_hash[:20]}"
        or artifact.get("artifact_type") != "layout_guide"
        or artifact.get("coordinate_space") != "source_px_xy"
        or not artifact_path_matches
    ):
        reasons.append("FORMAL_ARTIFACT_METADATA_MISMATCH")
    report_extensions = report.get("extensions") or {}
    verification_path_matches = False
    try:
        verification_path_matches = (
            Path(report_extensions.get("verification_artifact_path") or "").resolve()
            == Path(verification_path).resolve()
        )
    except Exception:
        verification_path_matches = False
    if not verification_path_matches:
        reasons.append("VERIFICATION_ARTIFACT_PATH_MISMATCH")
    if report_extensions.get("chosen_candidate_id") != chosen_id:
        reasons.append("RECONCILIATION_CANDIDATE_MISMATCH")
    if verification_hash is not None and verification_hash != report_extensions.get("verification_artifact_sha256"):
        reasons.append("VERIFICATION_ARTIFACT_HASH_MISMATCH")
    expected_evidence_hash = _canonical_hash({
        "geometry_verification": contract_verification,
        "transverse_reference": transverse_reference,
    })
    if report_extensions.get("formal_evidence_hash") != expected_evidence_hash:
        reasons.append("FORMAL_EVIDENCE_HASH_MISMATCH")

    reasons = list(dict.fromkeys(reasons))
    return {
        "ok": not reasons,
        "reason_codes": reasons,
        "contract_id": (contract.get("version_chain") or {}).get("contract_id"),
        "contract_hash": stored_hash,
        "chosen_candidate_id": chosen_id,
        "guide_sha256": guide_hash,
    }

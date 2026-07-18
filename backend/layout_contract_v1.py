# -*- coding: utf-8 -*-
"""Shared Geometry Contract v1｜S1 shadow-only writer.

本模組是 v1 `LayoutContract` 的唯一 writer。S1 只做 dual-write：
- 綁定來源照片、hash、尺寸與 view index
- 把 legacy zoning bbox 轉成 audit-only geometry
- 永遠輸出 BLOCKED / pre_generation_eligible=False
- 不接 guide、prompt、FAL、retry、validation 或 delivery
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


SCHEMA_VERSION = "1.0.0-draft"
BUILDER_VERSION = "s1-shadow-v1"
ELIGIBILITY_POLICY_VERSION = "geometry-eligibility-v1"

_BASE_UNSAFE_CODES = [
    "MISSING_DOOR_FLOOR_CONTACT",
    "MISSING_ENTRANCE_LANDING",
    "MISSING_WALKWAY_POLYGON",
    "MISSING_LIVING_FLOOR",
    "NO_USABLE_WALL",
    "MISSING_WALL_PLANE_EVIDENCE",
    "CANDIDATE_GEOMETRY_INCOMPLETE",
]

_LEGACY_ZONE_MAP = (
    ("existing_zones", "entrance_zone", "legacy_door_bbox"),
    ("existing_zones", "walkway", "legacy_walkway_bbox"),
    ("existing_zones", "living_zone", "legacy_living_bbox"),
    ("proposed_zones", "living_zone", "legacy_living_bbox"),
    ("proposed_zones", "no_large_furniture_zone", "legacy_no_go_bbox"),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _confidence_number(value: Any) -> float | None:
    return {
        "high": 0.9,
        "medium": 0.6,
        "low": 0.3,
    }.get(str(value or "").strip().lower())


def _ineligible(*codes: str) -> dict:
    failed = list(dict.fromkeys(code for code in codes if code)) or ["GEOM_NOT_ELIGIBLE"]
    return {
        "status": "INELIGIBLE",
        "policy_version": ELIGIBILITY_POLICY_VERSION,
        "passed_checks": [],
        "failed_checks": failed,
    }


def _legacy_bbox_geometry(
    *,
    geometry_id: str,
    kind: str,
    source_photo_key: str,
    bbox_yxyx_1000: Any,
    zone: dict,
    source_field: str,
) -> dict | None:
    if not isinstance(bbox_yxyx_1000, (list, tuple)) or len(bbox_yxyx_1000) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = [float(v) for v in bbox_yxyx_1000]
    except (TypeError, ValueError):
        return None
    in_bounds = all(0.0 <= v <= 1000.0 for v in (ymin, xmin, ymax, xmax))
    ordered = ymin < ymax and xmin < xmax
    if not (in_bounds and ordered):
        return None
    return {
        "geometry_id": geometry_id,
        "kind": kind,
        "status": "available",
        "source_photo_key": source_photo_key,
        "coordinate_space": "legacy_source_normalized_yxyx_1000",
        # v1 shape 的順序一律 xyxy；extensions 保留 legacy 原始 yxyx。
        "shape": {
            "type": "bbox_xyxy",
            "coordinates": [xmin, ymin, xmax, ymax],
        },
        "evidence": {
            "mode": "inferred",
            "confidence": _confidence_number(zone.get("confidence")),
            "visibility": "inferred",
            "producer": {
                "name": "legacy_zoning_adapter",
                "version": "s0-mapping-v1",
                "model": None,
            },
            "notes": "Legacy zoning bbox is audit/veto evidence only; never grant evidence.",
            "legacy_source": source_field,
        },
        "eligibility": _ineligible("GEOM_NOT_ELIGIBLE"),
        "validation": {
            "in_bounds": True,
            "non_self_intersect": True,
            "minimum_measure": True,
            "transform_valid": True,
            "notes": ["legacy_bbox_only", "not_hard_gate_eligible"],
        },
        "extensions": {
            "legacy_bbox_yxyx_1000": [ymin, xmin, ymax, xmax],
            "source_field": source_field,
        },
    }


def _missing_door_geometry(source_photo_key: str) -> dict:
    return {
        "geometry_id": "legacy_door_bbox_missing",
        "kind": "legacy_door_bbox",
        "status": "missing",
        "source_photo_key": source_photo_key,
        "coordinate_space": "legacy_source_normalized_yxyx_1000",
        "shape": None,
        "evidence": {
            "mode": "missing",
            "confidence": None,
            "visibility": "not_visible",
            "producer": {
                "name": "legacy_zoning_adapter",
                "version": "s0-mapping-v1",
                "model": None,
            },
            "notes": "No valid entrance bbox was available in the legacy payload.",
            "legacy_source": "existing_zones.entrance_zone.bbox_on_best_photo",
        },
        "eligibility": _ineligible("MISSING_DOOR"),
        "validation": {
            "in_bounds": None,
            "non_self_intersect": None,
            "minimum_measure": None,
            "transform_valid": True,
            "notes": ["missing_legacy_door_bbox"],
        },
    }


def _slim_legacy_shadow(legacy_shadow: dict | None) -> dict:
    source = legacy_shadow if isinstance(legacy_shadow, dict) else {}
    candidates = []
    for candidate in source.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        candidates.append({
            "id": candidate.get("id"),
            "pass": bool(candidate.get("pass")),
            "score": candidate.get("score"),
            "sofa_side": candidate.get("sofa_side"),
            "tv_side": candidate.get("tv_side"),
            "fail_reasons": list(candidate.get("fail_reasons") or []),
        })
    return {
        "safe_layout": bool(source.get("safe_layout")),
        "chosen": source.get("chosen"),
        "disposition": source.get("disposition"),
        "candidates": candidates,
        "authoritative": False,
    }


def _legacy_side_hints(legacy_zoning: dict) -> dict:
    proposed = legacy_zoning.get("proposed_zones") or {}
    living = proposed.get("living_zone") or {}
    return {
        "sofa_side": living.get("sofa_side"),
        "tv_side": living.get("tv_side"),
        "sofa_side_confidence": living.get("sofa_side_confidence"),
        "authoritative": False,
    }


def _canonical_hash_payload(contract: dict) -> bytes:
    payload = copy.deepcopy(contract)
    version_chain = payload.get("version_chain") or {}
    version_chain.pop("contract_hash", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def recompute_contract_hash(contract: dict) -> str:
    """依 frozen v1 規格重算 hash；排除 `version_chain.contract_hash` 本身。"""
    return hashlib.sha256(_canonical_hash_payload(contract)).hexdigest()


def build_layout_contract(
    *,
    job_id: str,
    photo_path: str | Path,
    photo_key: str,
    view_index: int,
    legacy_zoning: dict | None,
    legacy_shadow: dict | None,
    legacy_bbox_binding_verified: bool = False,
    created_at: str | None = None,
) -> dict:
    """建立 S1 shadow v1 Contract；此階段永遠 fail-closed。"""
    path = Path(photo_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    if not str(photo_key or "").strip():
        raise ValueError("photo_key is required")
    if not isinstance(view_index, int) or view_index < 0:
        raise ValueError("view_index must be a non-negative integer")

    with Image.open(path) as image:
        width, height = ImageOps.exif_transpose(image).size
    source_hash = _sha256_file(path)
    source_key = str(photo_key).strip()
    zoning = legacy_zoning if isinstance(legacy_zoning, dict) else {}

    geometry = []
    kind_counts: dict[str, int] = {}
    if legacy_bbox_binding_verified:
        for section, zone_key, kind in _LEGACY_ZONE_MAP:
            zone = (zoning.get(section) or {}).get(zone_key) or {}
            if not isinstance(zone, dict):
                continue
            count = kind_counts.get(kind, 0) + 1
            kind_counts[kind] = count
            geom = _legacy_bbox_geometry(
                geometry_id=f"{kind}_{count}",
                kind=kind,
                source_photo_key=source_key,
                bbox_yxyx_1000=zone.get("bbox_on_best_photo"),
                zone=zone,
                source_field=f"{section}.{zone_key}.bbox_on_best_photo",
            )
            if geom:
                geometry.append(geom)
    if not geometry:
        geometry.append(_missing_door_geometry(source_key))

    unsafe_codes = list(_BASE_UNSAFE_CODES)
    if not legacy_bbox_binding_verified:
        unsafe_codes.insert(0, "MISSING_PHOTO_BINDING")
    if not any(g.get("kind") == "legacy_door_bbox" and g.get("status") == "available" for g in geometry):
        unsafe_codes.insert(0, "MISSING_DOOR")
    unsafe_codes = list(dict.fromkeys(unsafe_codes))

    timestamp = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    contract_id = f"lc1_{uuid.uuid4().hex}"
    contract = {
        "object_type": "layout_contract",
        "schema_version": SCHEMA_VERSION,
        "version_chain": {
            "contract_id": contract_id,
            "contract_hash": "0" * 64,
            "parent_contract_id": None,
            "version": 1,
            "schema_version": SCHEMA_VERSION,
            "builder": {
                "name": "layout_contract_v1.build_layout_contract",
                "version": BUILDER_VERSION,
                "model": None,
            },
            "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
            "created_at": timestamp,
            "replan_reason_codes": [],
        },
        "source": {
            "photo_key": source_key,
            "canonical_photo_key": source_key,
            "sha256": source_hash,
            "size": {"width": int(width), "height": int(height)},
            "view_index": view_index,
            "room_id": None,
        },
        "transforms": [{
            "step_id": "source_identity",
            "type": "identity",
            "from_space": "source_px_xy",
            "to_space": "source_px_xy",
            "input_size": {"width": int(width), "height": int(height)},
            "output_size": {"width": int(width), "height": int(height)},
            "declared": True,
            "parameters": {},
        }],
        "model_input": None,
        "geometry": geometry,
        "candidates": [],
        "decision": {
            "chosen_candidate_id": None,
            "candidate_type": None,
            "disposition": "BLOCKED",
            "blocked_reason_class": "INSUFFICIENT_EVIDENCE",
            "unsafe_codes": unsafe_codes,
            "pre_generation_eligible": False,
            "eligibility": _ineligible(*unsafe_codes),
        },
        "legacy_inputs": {
            "mapping_version": "s0-mapping-v1",
            "best_photo_index": zoning.get("best_photo_index"),
            "legacy_bbox_binding_verified": bool(legacy_bbox_binding_verified),
            "side_hints": _legacy_side_hints(zoning),
            "legacy_shadow": _slim_legacy_shadow(legacy_shadow),
        },
        "extensions": {
            "stage": "S1_SHADOW_DUAL_WRITE",
            "job_id": str(job_id),
            "affects_delivery": False,
            "authoritative_for_generation": False,
            "authoritative_for_delivery": False,
        },
    }
    contract["version_chain"]["contract_hash"] = recompute_contract_hash(contract)
    return contract


def _eligible(*checks: str) -> dict:
    return {
        "status": "ELIGIBLE",
        "policy_version": ELIGIBILITY_POLICY_VERSION,
        "passed_checks": list(dict.fromkeys(checks)),
        "failed_checks": [],
    }


def _json_shape(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_shape(item) for item in value]
    if isinstance(value, list):
        return [_json_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in value.items()}
    return value


def _s2_geometry_record(
    *,
    geometry_id: str,
    kind: str,
    shape: dict,
    source_photo_key: str,
    mode: str,
    producer_name: str,
    passed_checks: list[str],
    notes: str,
) -> dict:
    return {
        "geometry_id": geometry_id,
        "kind": kind,
        "status": "available",
        "source_photo_key": source_photo_key,
        "coordinate_space": "source_px_xy",
        "shape": _json_shape(shape),
        "evidence": {
            "mode": mode,
            "confidence": 0.9,
            "visibility": "full",
            "producer": {
                "name": producer_name,
                "version": (
                    "s2-observed-geometry-v1"
                    if mode == "observed" else "s2-projective-depth-v2"
                ),
                "model": "gemini-3.5-flash" if mode == "observed" else None,
            },
            "notes": notes,
            "legacy_source": None,
        },
        "eligibility": _eligible(*passed_checks),
        "validation": {
            "in_bounds": True,
            "non_self_intersect": True,
            "minimum_measure": True,
            "transform_valid": True,
            "notes": ["s2_observed_geometry", "deterministic_validation_passed"],
        },
        "extensions": {},
    }


def _s2_constraint(check_code: str, geometry_ids: list[str], details: str) -> dict:
    return {
        "check_code": check_code,
        "status": "PASS",
        "failure_code": None,
        "geometry_ids": list(dict.fromkeys(geometry_ids)),
        "details": details,
    }


def _formalize_s2_plan(plan: dict, source_photo_key: str):
    geometry = []
    existing_ids = set()
    for item in plan.get("geometry") or []:
        geometry_id = str(item.get("geometry_id") or "")
        kind = str(item.get("kind") or "")
        shape = item.get("shape")
        if not geometry_id or not kind or not isinstance(shape, dict):
            continue
        evidence_mode = str(item.get("evidence_mode") or "observed")
        verifier_corrected = evidence_mode == "verifier_corrected"
        record = _s2_geometry_record(
            geometry_id=geometry_id,
            kind=kind,
            shape=shape,
            source_photo_key=source_photo_key,
            mode="inferred" if verifier_corrected else "observed",
            producer_name=(
                "layout_geometry_verifier_s2.bounded_wall_correction"
                if verifier_corrected else "zoning_v2.struct_geometry_v1"
            ),
            passed_checks=["PHOTO_BINDING_VALID", "TRANSFORM_CHAIN_VALID", "GEOMETRY_VALID"],
            notes=(
                "Bounded verifier correction; partial/medium derived evidence, final verifier pass required."
                if verifier_corrected
                else "Visible structure annotated on the bound source photo and normalized by S2."
            ),
        )
        if verifier_corrected:
            record["evidence"]["confidence"] = 0.65
            record["evidence"]["visibility"] = "partial"
            record["evidence"]["producer"]["version"] = "s2-bounded-wall-correction-v1"
            record["evidence"]["producer"]["model"] = "gemini-3.5-flash"
            record["validation"]["notes"].append("verifier_corrected_not_directly_observed")
        geometry.append(record)
        existing_ids.add(geometry_id)

    candidates = []
    audit = []
    for raw_candidate in plan.get("candidates") or []:
        audit.append({
            "candidate_id": raw_candidate.get("candidate_id"),
            "candidate_type": raw_candidate.get("candidate_type"),
            "eligible": bool(raw_candidate.get("eligible")),
            "score": raw_candidate.get("score"),
            "fail_codes": list(raw_candidate.get("fail_codes") or []),
        })
        if not raw_candidate.get("eligible"):
            continue
        candidate_id = str(raw_candidate["candidate_id"])
        derived = {
            "layout_floor": ("living_floor", {
                "type": "polygon", "coordinates": raw_candidate["layout_floor"],
            }),
            "sofa_footprint": ("sofa_footprint", {
                "type": "polygon", "coordinates": raw_candidate["sofa_footprint"],
            }),
            "tv_footprint": ("tv_footprint", {
                "type": "polygon", "coordinates": raw_candidate["tv_footprint"],
            }),
            "tv_wall_contact": ("tv_wall_contact", {
                "type": "segment", "coordinates": raw_candidate["tv_wall_contact"],
            }),
            "view_axis": ("view_axis", {
                "type": "segment", "coordinates": raw_candidate["view_axis"],
            }),
            "sofa_orientation": ("sofa_orientation", {
                "type": "vector",
                "origin": raw_candidate["sofa_orientation"]["origin"],
                "direction": raw_candidate["sofa_orientation"]["direction"],
            }),
            "tv_orientation": ("tv_orientation", {
                "type": "vector",
                "origin": raw_candidate["tv_orientation"]["origin"],
                "direction": raw_candidate["tv_orientation"]["direction"],
            }),
        }
        derived_ids = {}
        for suffix, (kind, shape) in derived.items():
            geometry_id = f"{candidate_id}_{suffix}"
            derived_ids[suffix] = geometry_id
            geometry.append(_s2_geometry_record(
                geometry_id=geometry_id,
                kind=kind,
                shape=shape,
                source_photo_key=source_photo_key,
                mode="inferred",
                producer_name="layout_geometry_s2",
                passed_checks=["SOURCE_GEOMETRY_ELIGIBLE", "CANDIDATE_GEOMETRY_VALID"],
                notes="Deterministic candidate geometry derived from eligible observed wall/floor evidence.",
            ))
            existing_ids.add(geometry_id)

        source_ids = [
            geometry_id for geometry_id in raw_candidate.get("source_geometry_ids") or []
            if geometry_id in existing_ids
        ]
        constraints = [
            _s2_constraint("PHOTO_BINDING_VALID", source_ids, "Contract and S2 coordinates bind to the same source photo."),
            _s2_constraint("TRANSFORM_CHAIN_VALID", source_ids, "All S2 geometry is in source_px_xy with a declared identity model-input transform."),
            _s2_constraint("DOOR_FLOOR_CONTACT_VALID", ["door_floor_contact"], "Observed entrance door floor contact is available."),
            _s2_constraint("ENTRANCE_LANDING_CLEAR", ["entrance_landing", derived_ids["sofa_footprint"], derived_ids["tv_footprint"]], "Furniture footprints do not intersect the entrance landing."),
            _s2_constraint("WALKWAY_CLEAR", ["walkway", derived_ids["sofa_footprint"], derived_ids["tv_footprint"]], "Furniture footprints preserve a connected near-to-deep path inside the observed walkway."),
            _s2_constraint("LIVING_FLOOR_VALID", ["living_floor", derived_ids["layout_floor"]], "Projective candidate floor is derived from observed living-floor transverse geometry and usable wall spans."),
            _s2_constraint("USABLE_WALL_VALID", source_ids, "Both furniture anchors use observed usable wall segments."),
            _s2_constraint("WALL_PLANE_EVIDENCE_VALID", ["left_wall_floor", "right_wall_floor"], "Both wall-floor boundaries are observed on the bound photo."),
            _s2_constraint("SOFA_INSIDE_LIVING_FLOOR", [derived_ids["layout_floor"], derived_ids["sofa_footprint"]], "The entire sofa footprint lies inside the projectively paired candidate floor."),
            _s2_constraint("TV_ON_USABLE_WALL", [derived_ids["tv_footprint"], derived_ids["tv_wall_contact"]], "The TV footprint has verified wall contact."),
            _s2_constraint("VIEW_AXIS_VALID", [derived_ids["view_axis"], derived_ids["sofa_orientation"], derived_ids["tv_orientation"], derived_ids["layout_floor"]], "Sofa and TV centres share one projectively paired transverse floor axis."),
            _s2_constraint("VIEW_AXIS_CLEAR_OF_DOOR", ["door_quad", derived_ids["view_axis"]], "The seated forward axis stays outside the entrance door field."),
            _s2_constraint("ORIENTATIONS_ALIGNED", [derived_ids["sofa_orientation"], derived_ids["tv_orientation"]], "Sofa and TV normals face each other."),
        ]
        if raw_candidate.get("candidate_type") == "F":
            float_proven = bool((raw_candidate.get("invariants") or {}).get("float_proven"))
            constraints.append({
                "check_code": "FLOAT_PROVEN",
                "status": "PASS" if float_proven else "FAIL",
                "failure_code": None if float_proven else "FLOAT_NOT_PROVEN",
                "geometry_ids": [
                    "entrance_landing", "walkway", derived_ids["layout_floor"],
                    derived_ids["sofa_footprint"],
                ],
                "details": (
                    "Floating sofa is fully inside the living floor, beyond the entrance band, "
                    "and preserves connected circulation."
                    if float_proven else
                    "Floating sofa lacks sufficient entrance depth or circulation proof."
                ),
            })
        candidates.append({
            "candidate_id": candidate_id,
            "candidate_type": raw_candidate["candidate_type"],
            "sofa_footprint_geometry_id": derived_ids["sofa_footprint"],
            "tv_footprint_geometry_id": derived_ids["tv_footprint"],
            "tv_wall_contact_geometry_id": derived_ids["tv_wall_contact"],
            "view_axis_geometry_id": derived_ids["view_axis"],
            "sofa_orientation_geometry_id": derived_ids["sofa_orientation"],
            "tv_orientation_geometry_id": derived_ids["tv_orientation"],
            "constraints": constraints,
            "eligibility": _eligible(*(item["check_code"] for item in constraints)),
            "score": float(raw_candidate.get("score") or 0.0),
            "notes": [
                f"sofa_side={raw_candidate.get('sofa_side')}",
                f"tv_side={raw_candidate.get('tv_side')}",
                f"door_axis_separation_deg={raw_candidate.get('door_axis_separation_deg')}",
            ],
        })
    return geometry, candidates, audit


def build_layout_contract_s2(
    *,
    job_id: str,
    photo_path: str | Path,
    photo_key: str,
    view_index: int,
    s2_plan: dict | None,
    photo_binding_verified: bool,
    legacy_zoning: dict | None,
    legacy_shadow: dict | None,
    created_at: str | None = None,
) -> dict:
    """Build the authoritative S2 pre-generation Contract from an S2 plan."""
    base = build_layout_contract(
        job_id=job_id,
        photo_path=photo_path,
        photo_key=photo_key,
        view_index=view_index,
        legacy_zoning=legacy_zoning,
        legacy_shadow=legacy_shadow,
        legacy_bbox_binding_verified=photo_binding_verified,
        created_at=created_at,
    )
    plan = s2_plan if isinstance(s2_plan, dict) else {}
    geometry_verification = plan.get("geometry_verification") or {}
    geometry_verification_passed = bool(
        geometry_verification.get("status") == "pass"
        and geometry_verification.get("unsafe_codes") == []
    )
    transverse_reference = plan.get("transverse_reference") or {}
    direction_xy = transverse_reference.get("direction_xy")
    transverse_reference_passed = bool(
        transverse_reference.get("status") == "observed"
        and isinstance(direction_xy, (list, tuple))
        and len(direction_xy) == 2
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in direction_xy)
        and int(transverse_reference.get("support_count") or 0) > 0
        and transverse_reference.get("unsafe_codes") == []
    )
    plan_declares_safe = bool(
        plan.get("disposition") == "SAFE_FOR_GENERATION"
        and plan.get("unsafe_codes") == []
        and plan.get("pre_generation_eligible") is True
    )
    if (
        not photo_binding_verified
        or not plan_declares_safe
        or not geometry_verification_passed
        or not transverse_reference_passed
    ):
        unsafe = list(plan.get("unsafe_codes") or [])
        if not photo_binding_verified:
            unsafe.insert(0, "MISSING_PHOTO_BINDING")
        if not geometry_verification_passed:
            unsafe.insert(0, "GEOM_NOT_ELIGIBLE")
        if not transverse_reference_passed:
            unsafe.insert(0, "GEOM_NOT_ELIGIBLE")
        unsafe = list(dict.fromkeys(unsafe)) or ["CANDIDATE_GEOMETRY_INCOMPLETE"]
        base["decision"].update({
            "chosen_candidate_id": None,
            "candidate_type": None,
            "disposition": "BLOCKED",
            "blocked_reason_class": plan.get("blocked_reason_class") or "INSUFFICIENT_EVIDENCE",
            "unsafe_codes": unsafe,
            "pre_generation_eligible": False,
            "eligibility": _ineligible(*unsafe),
        })
        base["extensions"].update({
            "stage": "S2_GEOMETRY_BLOCKED",
            "affects_delivery": False,
            "authoritative_for_generation": True,
            "s2_planner_version": plan.get("planner_version"),
            "geometry_verification": copy.deepcopy(geometry_verification),
            "transverse_reference": copy.deepcopy(transverse_reference),
        })
        base["version_chain"]["builder"]["version"] = "s2-contract-v1"
        base["version_chain"]["contract_hash"] = recompute_contract_hash(base)
        return base

    source_key = base["source"]["photo_key"]
    geometry, candidates, audit = _formalize_s2_plan(plan, source_key)
    chosen_id = plan.get("chosen_candidate_id")
    chosen = next((candidate for candidate in candidates if candidate["candidate_id"] == chosen_id), None)
    if not chosen:
        base["decision"].update({
            "unsafe_codes": ["NO_ELIGIBLE_CANDIDATE"],
            "eligibility": _ineligible("NO_ELIGIBLE_CANDIDATE"),
        })
        base["extensions"]["stage"] = "S2_GEOMETRY_BLOCKED"
        base["version_chain"]["builder"]["version"] = "s2-contract-v1"
        base["version_chain"]["contract_hash"] = recompute_contract_hash(base)
        return base

    source_size = base["source"]["size"]
    base["transforms"] = [
        base["transforms"][0],
        {
            "step_id": "source_to_model_input_identity",
            "type": "identity",
            "from_space": "source_px_xy",
            "to_space": "model_input_px_xy",
            "input_size": dict(source_size),
            "output_size": dict(source_size),
            "declared": True,
            "parameters": {},
        },
    ]
    base["model_input"] = {
        "size": dict(source_size),
        "sha256": base["source"]["sha256"],
        "coordinate_space": "model_input_px_xy",
    }
    base["geometry"] = geometry
    base["candidates"] = candidates
    base["decision"] = {
        "chosen_candidate_id": chosen["candidate_id"],
        "candidate_type": chosen["candidate_type"],
        "disposition": "SAFE_FOR_GENERATION",
        "blocked_reason_class": None,
        "unsafe_codes": [],
        "pre_generation_eligible": True,
        "eligibility": _eligible(*(item["check_code"] for item in chosen["constraints"])),
    }
    base["extensions"].update({
        "stage": "S2_GEOMETRY_CONTRACT",
        "affects_delivery": True,
        "authoritative_for_generation": True,
        "authoritative_for_delivery": False,
        "s2_planner_version": plan.get("planner_version"),
        "s2_candidate_audit": audit,
        "geometry_verification": copy.deepcopy(geometry_verification),
        "transverse_reference": copy.deepcopy(transverse_reference),
    })
    base["version_chain"]["builder"]["version"] = "s2-contract-v1"
    base["version_chain"]["contract_hash"] = recompute_contract_hash(base)
    return base

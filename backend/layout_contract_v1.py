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

from PIL import Image


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
        width, height = image.size
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

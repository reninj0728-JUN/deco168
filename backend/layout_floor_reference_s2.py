# -*- coding: utf-8 -*-
"""Deterministic transverse floor-reference extraction for S2."""
from __future__ import annotations

import math
from pathlib import Path


def _normal_angle_degrees(x1: float, y1: float, x2: float, y2: float) -> float:
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
    while angle > 90.0:
        angle -= 180.0
    while angle < -90.0:
        angle += 180.0
    return angle


def _missing(detail: str) -> dict:
    return {
        "status": "missing",
        "confidence": "none",
        "direction_xy": None,
        "angle_degrees": None,
        "support_count": 0,
        "support_length_px": 0.0,
        "reference_segments_yx1000": [],
        "unsafe_codes": ["MISSING_TRANSVERSE_FLOOR_REFERENCE"],
        "detail": detail,
    }


def estimate_transverse_floor_reference(
    photo_path: str | Path,
    living_floor_polygon_yx1000,
) -> dict:
    """Find repeated cross-room floor edges; return missing rather than infer."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return _missing(f"opencv unavailable: {exc}")

    source = Path(photo_path)
    if not source.is_file():
        return _missing("source photo missing")
    if not isinstance(living_floor_polygon_yx1000, list) or len(living_floor_polygon_yx1000) < 4:
        return _missing("living-floor polygon missing")

    image = cv2.imdecode(np.frombuffer(source.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return _missing("source photo decode failed")
    source_height, source_width = image.shape[:2]
    target_width = min(1000, source_width)
    scale = target_width / float(source_width)
    target_height = max(1, int(round(source_height * scale)))
    if scale != 1.0:
        image = cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_AREA)

    polygon = []
    try:
        for y_norm, x_norm in living_floor_polygon_yx1000:
            polygon.append([
                int(round(float(x_norm) / 1000.0 * target_width)),
                int(round(float(y_norm) / 1000.0 * target_height)),
            ])
    except (TypeError, ValueError):
        return _missing("living-floor polygon invalid")
    polygon_array = np.asarray(polygon, dtype=np.int32)
    if abs(cv2.contourArea(polygon_array)) < target_width * target_height * 0.02:
        return _missing("living-floor polygon too small")

    blurred = cv2.GaussianBlur(image, (3, 3), 0)
    edges = cv2.Canny(blurred, 25, 90)
    mask = np.zeros_like(edges)
    cv2.fillPoly(mask, [polygon_array], 255)
    edges = cv2.bitwise_and(edges, mask)
    detected = cv2.createLineSegmentDetector(cv2.LSD_REFINE_ADV).detect(edges)[0]
    if detected is None:
        return _missing("no floor edge segments detected")

    minimum_length = max(24.0, target_width * 0.024)
    candidates = []
    for raw_line in detected:
        values = np.asarray(raw_line).reshape(-1)
        if values.size < 4:
            continue
        x1, y1, x2, y2 = map(float, values[:4])
        length = math.hypot(x2 - x1, y2 - y1)
        if length < minimum_length:
            continue
        midpoint = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        if cv2.pointPolygonTest(polygon_array, midpoint, False) < 0:
            continue
        angle = _normal_angle_degrees(x1, y1, x2, y2)
        # A source image with EXIF applied should keep room-transverse evidence
        # broadly lateral. Steeper lines are longitudinal seams or wall edges.
        if abs(angle) > 25.0:
            continue
        candidates.append({
            "line": (x1, y1, x2, y2),
            "angle": angle,
            "length": length,
            "mid_y": midpoint[1],
        })
    if not candidates:
        return _missing("no lateral floor references inside living floor")

    bins = {}
    for candidate in candidates:
        key = int(round(candidate["angle"]))
        bins[key] = bins.get(key, 0.0) + candidate["length"]
    dominant_bin = max(bins, key=bins.get)
    support = [
        candidate for candidate in candidates
        if abs(candidate["angle"] - dominant_bin) <= 3.0
    ]
    support_length = sum(candidate["length"] for candidate in support)
    y_band_size = max(12.0, target_height * 0.025)
    depth_bands = {int(candidate["mid_y"] // y_band_size) for candidate in support}
    if len(support) < 8 or len(depth_bands) < 3 or support_length < target_width * 0.35:
        return _missing("transverse floor evidence lacks repeated multi-depth support")

    weighted_angle = sum(
        candidate["angle"] * candidate["length"] for candidate in support
    ) / support_length
    radians = math.radians(weighted_angle)
    direction = [math.cos(radians), math.sin(radians)]
    strongest = sorted(support, key=lambda item: item["length"], reverse=True)[:12]
    references = []
    for candidate in strongest:
        x1, y1, x2, y2 = candidate["line"]
        references.append([
            [round(y1 / target_height * 1000, 3), round(x1 / target_width * 1000, 3)],
            [round(y2 / target_height * 1000, 3), round(x2 / target_width * 1000, 3)],
        ])
    return {
        "status": "observed",
        "confidence": "high",
        "direction_xy": [round(direction[0], 9), round(direction[1], 9)],
        "angle_degrees": round(weighted_angle, 6),
        "support_count": len(support),
        "support_length_px": round(support_length, 3),
        "reference_segments_yx1000": references,
        "unsafe_codes": [],
        "detail": "repeated transverse floor edges observed inside living floor",
    }

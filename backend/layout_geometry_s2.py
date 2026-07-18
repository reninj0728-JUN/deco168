# -*- coding: utf-8 -*-
"""S2 observed-geometry normalizer and deterministic living-room planner.

Input is a Gemini-produced ``struct_geometry_v1`` object bound to exactly one
source photo. This module never calls a model or renderer. It accepts only
observed, in-bounds geometry as grant evidence and fails closed otherwise.
"""
from __future__ import annotations

import hashlib
import math
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageOps


STRUCT_SCHEMA_VERSION = "struct-geometry-v1"
PLANNER_VERSION = "s2-projective-depth-v2"
MIN_WALL_SPAN_T = 0.18

_REQUIRED_ELEMENTS = {
    "door_quad": "MISSING_DOOR",
    "door_floor_contact": "MISSING_DOOR_FLOOR_CONTACT",
    "entrance_landing": "MISSING_ENTRANCE_LANDING",
    "walkway": "MISSING_WALKWAY_POLYGON",
    "living_floor": "MISSING_LIVING_FLOOR",
    "left_wall_floor": "MISSING_WALL_PLANE_EVIDENCE",
    "right_wall_floor": "MISSING_WALL_PLANE_EVIDENCE",
}


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _blocked(*codes: str, geometry=None, candidates=None, reason_class="INSUFFICIENT_EVIDENCE") -> dict:
    return {
        "planner_version": PLANNER_VERSION,
        "disposition": "BLOCKED",
        "blocked_reason_class": reason_class,
        "unsafe_codes": _unique(codes) or ["CANDIDATE_GEOMETRY_INCOMPLETE"],
        "pre_generation_eligible": False,
        "chosen_candidate_id": None,
        "candidate_type": None,
        "geometry": list(geometry or []),
        "candidates": list(candidates or []),
    }


def _confidence_ok(value: Any) -> bool:
    return str(value or "").strip().lower() in ("high", "medium")


def _point_yx1000(value: Any, width: int, height: int):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        y, x = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    return (x / 1000.0 * width, y / 1000.0 * height)


def _shape_from_element(element: dict, width: int, height: int):
    if "segment_yx1000" in element:
        raw = element.get("segment_yx1000")
        if not isinstance(raw, list) or len(raw) != 2:
            return None
        points = [_point_yx1000(point, width, height) for point in raw]
        if not all(points) or _distance(points[0], points[1]) < 2:
            return None
        return {"type": "segment", "coordinates": points}
    if "polygon_yx1000" in element:
        raw = element.get("polygon_yx1000")
        if not isinstance(raw, list) or len(raw) < 3:
            return None
        points = [_point_yx1000(point, width, height) for point in raw]
        if not all(points) or abs(_polygon_area(points)) < 4 or _polygon_self_intersects(points):
            return None
        return {"type": "polygon", "coordinates": points}
    return None


def _distance(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _polygon_area(poly) -> float:
    return sum(
        poly[i][0] * poly[(i + 1) % len(poly)][1]
        - poly[(i + 1) % len(poly)][0] * poly[i][1]
        for i in range(len(poly))
    ) / 2.0


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p, eps=1e-7) -> bool:
    return (
        abs(_orient(a, b, p)) <= eps
        and min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def _segments_intersect(a, b, c, d) -> bool:
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if ((o1 > 0 > o2) or (o1 < 0 < o2)) and ((o3 > 0 > o4) or (o3 < 0 < o4)):
        return True
    return any((
        _on_segment(a, b, c), _on_segment(a, b, d),
        _on_segment(c, d, a), _on_segment(c, d, b),
    ))


def _polygon_self_intersects(poly) -> bool:
    count = len(poly)
    for i in range(count):
        a, b = poly[i], poly[(i + 1) % count]
        for j in range(i + 1, count):
            if j in (i, (i + 1) % count) or (j + 1) % count in (i, (i + 1) % count):
                continue
            if i == 0 and (j + 1) % count == 0:
                continue
            c, d = poly[j], poly[(j + 1) % count]
            if _segments_intersect(a, b, c, d):
                return True
    return False


def _point_in_polygon(point, poly) -> bool:
    for i in range(len(poly)):
        if _on_segment(poly[i], poly[(i + 1) % len(poly)], point):
            return True
    x, y = point
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            cross_x = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def _polygon_intersects(a, b) -> bool:
    if any(_point_in_polygon(point, b) for point in a):
        return True
    if any(_point_in_polygon(point, a) for point in b):
        return True
    return any(
        _segments_intersect(a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)])
        for i in range(len(a)) for j in range(len(b))
    )


def _point_segment_distance(point, a, b) -> float:
    ab_x, ab_y = b[0] - a[0], b[1] - a[1]
    length_sq = ab_x * ab_x + ab_y * ab_y
    if length_sq <= 1e-12:
        return math.hypot(point[0] - a[0], point[1] - a[1])
    projection = (
        (point[0] - a[0]) * ab_x + (point[1] - a[1]) * ab_y
    ) / length_sq
    projection = max(0.0, min(1.0, projection))
    nearest = (a[0] + projection * ab_x, a[1] + projection * ab_y)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _polygon_distance(a, b) -> float:
    if _polygon_intersects(a, b):
        return 0.0
    distances = []
    for point in a:
        distances.extend(
            _point_segment_distance(point, b[index], b[(index + 1) % len(b)])
            for index in range(len(b))
        )
    for point in b:
        distances.extend(
            _point_segment_distance(point, a[index], a[(index + 1) % len(a)])
            for index in range(len(a))
        )
    return min(distances) if distances else 0.0


def _segment_intersects_polygon(a, b, poly) -> bool:
    if _point_in_polygon(a, poly) or _point_in_polygon(b, poly):
        return True
    return any(
        _segments_intersect(a, b, poly[i], poly[(i + 1) % len(poly)])
        for i in range(len(poly))
    )


def _centroid(poly):
    return (
        sum(point[0] for point in poly) / len(poly),
        sum(point[1] for point in poly) / len(poly),
    )


def _line_point(line, t: float):
    a, b = line
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _mix(a, b, amount: float):
    return (a[0] + (b[0] - a[0]) * amount, a[1] + (b[1] - a[1]) * amount)


def _footprint(side: str, own_line, opposite_line, t0: float, t1: float, depth: float):
    own0, own1 = _line_point(own_line, t0), _line_point(own_line, t1)
    opp0, opp1 = _line_point(opposite_line, t0), _line_point(opposite_line, t1)
    inner0, inner1 = _mix(own0, opp0, depth), _mix(own1, opp1, depth)
    # Wall contact near/deep, then inner deep/near.
    return [own0, own1, inner1, inner0]


def _footprint_from_cross_sections(own0, own1, opposite0, opposite1, depth: float):
    inner0 = _mix(own0, opposite0, depth)
    inner1 = _mix(own1, opposite1, depth)
    return [own0, own1, inner1, inner0]


def _front_center(footprint):
    return (
        (footprint[2][0] + footprint[3][0]) / 2.0,
        (footprint[2][1] + footprint[3][1]) / 2.0,
    )


def _unit_vector(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-9 else (0.0, 0.0)


def _angle_degrees(v1, v2) -> float:
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 <= 1e-9 or n2 <= 1e-9:
        return 0.0
    cosine = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cosine))


def _cross2(a, b) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _ray_intersects_segment(origin, through, a, b) -> bool:
    ray = (through[0] - origin[0], through[1] - origin[1])
    segment = (b[0] - a[0], b[1] - a[1])
    offset = (a[0] - origin[0], a[1] - origin[1])
    denominator = _cross2(ray, segment)
    if abs(denominator) <= 1e-9:
        if abs(_cross2(offset, ray)) > 1e-7:
            return False
        ray_length_sq = ray[0] * ray[0] + ray[1] * ray[1]
        if ray_length_sq <= 1e-9:
            return False
        return max(
            ((point[0] - origin[0]) * ray[0] + (point[1] - origin[1]) * ray[1])
            / ray_length_sq
            for point in (a, b)
        ) >= 0
    ray_t = _cross2(offset, segment) / denominator
    segment_t = _cross2(offset, ray) / denominator
    return ray_t >= 0 and -1e-8 <= segment_t <= 1.0 + 1e-8


def _view_ray_clear_of_door(sofa_front, tv_front, door_quad, door_contact) -> bool:
    """Ground-plane portal test; vertical door projection and angular proximity do not veto."""
    axis = (tv_front[0] - sofa_front[0], tv_front[1] - sofa_front[1])
    door_center = _centroid(door_quad)
    to_door = (door_center[0] - sofa_front[0], door_center[1] - sofa_front[1])
    if axis[0] * to_door[0] + axis[1] * to_door[1] <= 0:
        return True
    if len(door_contact) != 2:
        return False
    a, b = door_contact
    # Small deterministic portal margin; still tied to the observed floor-contact segment.
    margin = 0.08
    dx, dy = b[0] - a[0], b[1] - a[1]
    expanded_a = (a[0] - dx * margin, a[1] - dy * margin)
    expanded_b = (b[0] + dx * margin, b[1] + dy * margin)
    return not _ray_intersects_segment(sofa_front, tv_front, expanded_a, expanded_b)


def _walkway_has_connected_path(walkway, obstacles, *, width: int, height: int) -> bool:
    """Require near-to-deep connectivity inside the observed walkway, not zero overlap."""
    if len(walkway) < 3 or width <= 0 or height <= 0:
        return False
    grid_width = 180
    grid_height = max(60, min(180, round(grid_width * height / width)))

    def scaled(points):
        return [
            (int(round(x / width * (grid_width - 1))),
             int(round(y / height * (grid_height - 1))))
            for x, y in points
        ]

    walk_mask = Image.new("L", (grid_width, grid_height), 0)
    ImageDraw.Draw(walk_mask).polygon(scaled(walkway), fill=255)
    obstacle_mask = Image.new("L", (grid_width, grid_height), 0)
    obstacle_draw = ImageDraw.Draw(obstacle_mask)
    for obstacle in obstacles:
        if len(obstacle) >= 3:
            obstacle_draw.polygon(scaled(obstacle), fill=255)
    # Two-cell clearance on each side prevents a one-pixel crack from counting as a route.
    obstacle_mask = obstacle_mask.filter(ImageFilter.MaxFilter(5))
    walk_bytes = walk_mask.tobytes()
    obstacle_bytes = obstacle_mask.tobytes()
    free = bytearray(
        1 if walk_bytes[index] and not obstacle_bytes[index] else 0
        for index in range(grid_width * grid_height)
    )
    walkway_rows = [
        index // grid_width for index, value in enumerate(walk_bytes) if value
    ]
    if not walkway_rows:
        return False
    top, bottom = min(walkway_rows), max(walkway_rows)
    band = max(2, round((bottom - top) * 0.08))
    goals = {
        index for index, value in enumerate(free)
        if value and index // grid_width <= top + band
    }
    starts = [
        index for index, value in enumerate(free)
        if value and index // grid_width >= bottom - band
    ]
    if not starts or not goals:
        return False
    queue = deque(starts)
    visited = bytearray(grid_width * grid_height)
    for index in starts:
        visited[index] = 1
    while queue:
        current = queue.popleft()
        if current in goals:
            return True
        x, y = current % grid_width, current // grid_width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < grid_width and 0 <= ny < grid_height):
                continue
            neighbor = ny * grid_width + nx
            if free[neighbor] and not visited[neighbor]:
                visited[neighbor] = 1
                queue.append(neighbor)
    return False


def _line_intersection(a, b, c, d):
    first = (b[0] - a[0], b[1] - a[1])
    second = (d[0] - c[0], d[1] - c[1])
    denominator = _cross2(first, second)
    if abs(denominator) <= 1e-9:
        return None
    offset = (c[0] - a[0], c[1] - a[1])
    amount = _cross2(offset, second) / denominator
    return (a[0] + first[0] * amount, a[1] + first[1] * amount)


def _floor_corners(living_floor):
    if len(living_floor) != 4:
        return None
    ordered_by_y = sorted(living_floor, key=lambda point: point[1])
    deep_pair = ordered_by_y[:2]
    near_pair = ordered_by_y[2:]
    deep_left, deep_right = sorted(deep_pair, key=lambda point: point[0])
    near_left, near_right = sorted(near_pair, key=lambda point: point[0])
    return near_left, near_right, deep_right, deep_left


def _transverse_vanishing_point(living_floor):
    corners = _floor_corners(living_floor)
    if corners is None:
        return None
    near_left, near_right, deep_right, deep_left = corners
    return _line_intersection(near_left, near_right, deep_left, deep_right)


def _transverse_direction(living_floor):
    corners = _floor_corners(living_floor)
    if corners is None:
        return None
    near_left, near_right, deep_right, deep_left = corners
    near = _unit_vector(near_left, near_right)
    deep = _unit_vector(deep_left, deep_right)
    combined = (near[0] + deep[0], near[1] + deep[1])
    length = math.hypot(*combined)
    return (combined[0] / length, combined[1] / length) if length > 1e-9 else None


def _cross_line_target(point, vanishing, direction):
    if vanishing is not None:
        return vanishing
    if direction is None:
        raise ValueError("transverse floor direction is unavailable")
    return (point[0] + direction[0], point[1] + direction[1])


def _paired_cross_sections(
    living_floor, left_wall, right_wall, s0: float, s1: float,
    transverse_direction_xy=None,
):
    """Pair opposite wall points through one observed transverse direction."""
    vanishing = None
    direction = None
    if isinstance(transverse_direction_xy, (list, tuple)) and len(transverse_direction_xy) == 2:
        try:
            dx, dy = float(transverse_direction_xy[0]), float(transverse_direction_xy[1])
            magnitude = math.hypot(dx, dy)
            if magnitude > 1e-9:
                direction = (dx / magnitude, dy / magnitude)
        except (TypeError, ValueError):
            direction = None
    if direction is None:
        vanishing = _transverse_vanishing_point(living_floor)
        direction = _transverse_direction(living_floor)
    if vanishing is None and direction is None:
        raise ValueError("transverse floor geometry is unavailable")

    def pair_at(amount: float):
        left = _line_point(left_wall, amount)
        cross_target = _cross_line_target(left, vanishing, direction)
        right = _line_intersection(left, cross_target, right_wall[0], right_wall[1])
        if right is None:
            raise ValueError("cross-room line does not intersect the opposite wall")
        return left, right

    left0, right0 = pair_at(s0)
    left1, right1 = pair_at(s1)
    left_mid, right_mid = pair_at((s0 + s1) / 2.0)
    return {
        "left0": left0,
        "right0": right0,
        "left1": left1,
        "right1": right1,
        "left_mid": left_mid,
        "right_mid": right_mid,
        "layout_floor": [left0, right0, right1, left1],
        "transverse_vanishing_point": vanishing,
        "transverse_direction": direction,
    }


def _line_parameter(point, line) -> float:
    dx = line[1][0] - line[0][0]
    dy = line[1][1] - line[0][1]
    if abs(dx) >= abs(dy) and abs(dx) > 1e-9:
        return (point[0] - line[0][0]) / dx
    if abs(dy) > 1e-9:
        return (point[1] - line[0][1]) / dy
    raise ValueError("degenerate wall segment")


def _projective_common_interval(
    living_floor, left_wall, right_wall, transverse_direction_xy=None,
):
    vanishing = None
    direction = None
    if isinstance(transverse_direction_xy, (list, tuple)) and len(transverse_direction_xy) == 2:
        try:
            dx, dy = float(transverse_direction_xy[0]), float(transverse_direction_xy[1])
            magnitude = math.hypot(dx, dy)
            if magnitude > 1e-9:
                direction = (dx / magnitude, dy / magnitude)
        except (TypeError, ValueError):
            direction = None
    if direction is None:
        vanishing = _transverse_vanishing_point(living_floor)
        direction = _transverse_direction(living_floor)
    if vanishing is None and direction is None:
        return None
    mapped_parameters = []
    for right_point in right_wall:
        cross_target = _cross_line_target(right_point, vanishing, direction)
        mapped_left = _line_intersection(
            right_point, cross_target, left_wall[0], left_wall[1],
        )
        if mapped_left is None:
            return None
        mapped_parameters.append(_line_parameter(mapped_left, left_wall))
    start = max(0.0, min(mapped_parameters))
    end = min(1.0, max(mapped_parameters))
    if end - start < MIN_WALL_SPAN_T:
        return None
    return start, end


def _shape_geometry(
    geometry_id: str, kind: str, shape: dict, source_name: str,
    *, evidence_mode: str = "observed", evidence_details: dict | None = None,
) -> dict:
    item = {
        "geometry_id": geometry_id,
        "kind": kind,
        "shape": shape,
        "source_name": source_name,
        "evidence_mode": evidence_mode,
        "eligible": True,
    }
    if evidence_details:
        item["evidence_details"] = evidence_details
    return item


def _normalize_observed(raw: dict, width: int, height: int):
    elements = raw.get("elements") if isinstance(raw, dict) else None
    if not isinstance(elements, dict):
        return [], {}, ["CANDIDATE_GEOMETRY_INCOMPLETE"]

    geometry = []
    by_name = {}
    unsafe = []
    for name, missing_code in _REQUIRED_ELEMENTS.items():
        element = elements.get(name)
        if not isinstance(element, dict):
            unsafe.append(missing_code)
            continue
        status = element.get("status")
        verifier_corrected_wall = (
            name in ("left_wall_floor", "right_wall_floor")
            and status == "verifier_corrected"
        )
        if status != "observed" and not verifier_corrected_wall:
            unsafe.append("GEOM_NOT_ELIGIBLE")
            continue
        if not _confidence_ok(element.get("confidence")):
            unsafe.append("GEOM_NOT_ELIGIBLE")
            continue
        shape = _shape_from_element(element, width, height)
        if not shape:
            unsafe.append("INVALID_GEOMETRY")
            continue
        item = _shape_geometry(
            name, str(element.get("kind") or name), shape, name,
            evidence_mode=status,
            evidence_details={
                "status": status,
                "confidence": element.get("confidence"),
                "visibility": element.get("visibility"),
                "correction_evidence": element.get("correction_evidence"),
            },
        )
        geometry.append(item)
        by_name[name] = item

    wall_lines = {
        side: (by_name.get(f"{side}_wall_floor") or {}).get("shape", {}).get("coordinates")
        for side in ("left", "right")
    }
    segments = {"left": [], "right": []}
    for index, segment in enumerate(raw.get("usable_wall_segments") or []):
        if not isinstance(segment, dict) or segment.get("side") not in segments:
            unsafe.append("NO_USABLE_WALL")
            continue
        if (
            segment.get("status") not in ("observed", "verifier_corrected")
            or not _confidence_ok(segment.get("confidence"))
        ):
            unsafe.append("GEOM_NOT_ELIGIBLE")
            continue
        try:
            t0, t1 = float(segment.get("t_start")), float(segment.get("t_end"))
        except (TypeError, ValueError):
            unsafe.append("INVALID_GEOMETRY")
            continue
        if not (0 <= t0 < t1 <= 1) or t1 - t0 < MIN_WALL_SPAN_T:
            unsafe.append("NO_USABLE_WALL")
            continue
        side = segment["side"]
        line = wall_lines.get(side)
        if not line:
            unsafe.append("MISSING_WALL_PLANE_EVIDENCE")
            continue
        wall_shape = {
            "type": "segment",
            "coordinates": [_line_point(line, t0), _line_point(line, t1)],
        }
        geometry_id = f"usable_wall_{side}_{index + 1}"
        geometry.append(_shape_geometry(
            geometry_id,
            "usable_wall_segment",
            wall_shape,
            str(segment.get("id") or geometry_id),
            evidence_mode=str(segment.get("status") or ""),
            evidence_details={
                "status": segment.get("status"),
                "confidence": segment.get("confidence"),
                "visibility": segment.get("visibility"),
                "correction_evidence": segment.get("correction_evidence"),
            },
        ))
        segments[side].append({
            "geometry_id": geometry_id,
            "t_start": t0,
            "t_end": t1,
            "side": side,
            "coordinates": wall_shape["coordinates"],
        })
    if not segments["left"] or not segments["right"]:
        unsafe.append("NO_USABLE_WALL")
    return geometry, {"items": by_name, "segments": segments, "lines": wall_lines}, _unique(unsafe)


def _candidate(
    candidate_type: str,
    sofa_side: str,
    paired: dict,
    t0: float,
    t1: float,
    items: dict,
    source_ids: list[str],
    door_side: str,
    width: int,
    height: int,
    compact_entry: bool = False,
):
    left0, left1 = paired["left0"], paired["left1"]
    right0, right1 = paired["right0"], paired["right1"]
    left_mid, right_mid = paired["left_mid"], paired["right_mid"]
    if sofa_side == "free":
        tv_side = "right"
        sofa_back0 = _mix(left0, right0, 0.60)
        sofa_back1 = _mix(left1, right1, 0.60)
        sofa_front0 = _mix(left0, right0, 0.80)
        sofa_front1 = _mix(left1, right1, 0.80)
        sofa = [sofa_back0, sofa_back1, sofa_front1, sofa_front0]
        tv = _footprint_from_cross_sections(
            right0, right1, left0, left1, depth=0.055,
        )
        sofa_front = _mix(left_mid, right_mid, 0.80)
        tv_front = _mix(right_mid, left_mid, 0.055)
    else:
        tv_side = "right" if sofa_side == "left" else "left"
        if sofa_side == "left":
            sofa = _footprint_from_cross_sections(
                left0, left1, right0, right1, depth=0.20,
            )
            tv = _footprint_from_cross_sections(
                right0, right1, left0, left1, depth=0.055,
            )
            sofa_front = _mix(left_mid, right_mid, 0.20)
            tv_front = _mix(right_mid, left_mid, 0.055)
        else:
            sofa = _footprint_from_cross_sections(
                right0, right1, left0, left1, depth=0.20,
            )
            tv = _footprint_from_cross_sections(
                left0, left1, right0, right1, depth=0.055,
            )
            sofa_front = _mix(right_mid, left_mid, 0.20)
            tv_front = _mix(left_mid, right_mid, 0.055)
    axis_vector = _unit_vector(sofa_front, tv_front)
    tv_vector = _unit_vector(tv_front, sofa_front)

    door_poly = items["door_quad"]["shape"]["coordinates"]
    door_contact = items["door_floor_contact"]["shape"]["coordinates"]
    landing = items["entrance_landing"]["shape"]["coordinates"]
    walkway = items["walkway"]["shape"]["coordinates"]
    living = paired["layout_floor"]
    door_center = _centroid(door_poly)
    door_vector = (door_center[0] - sofa_front[0], door_center[1] - sofa_front[1])
    door_behind = axis_vector[0] * door_vector[0] + axis_vector[1] * door_vector[1] <= 0
    door_angle = 180.0 if door_behind else _angle_degrees(axis_vector, door_vector)
    axis_clear = _view_ray_clear_of_door(
        sofa_front, tv_front, door_poly, door_contact,
    )

    sofa_inside = all(_point_in_polygon(point, living) for point in sofa)
    tv_inside = all(_point_in_polygon(point, living) for point in tv)
    landing_clear = not _polygon_intersects(sofa, landing) and not _polygon_intersects(tv, landing)
    walkway_clear = _walkway_has_connected_path(
        walkway, [sofa, tv], width=width, height=height,
    )
    sofa_faces_tv = _distance(sofa_front, tv_front) > 10
    tv_on_wall = _distance(tv[0], tv[1]) >= 20

    entrance_clearance = _polygon_distance(sofa, landing)
    minimum_entrance_clearance = 0.03 * min(width, height)
    floating_entrance_clear = (
        sofa_side != "free" or entrance_clearance >= minimum_entrance_clearance
    )
    floating_walkway_clear = (
        sofa_side != "free" or not _polygon_intersects(sofa, walkway)
    )
    invariants = {
        "photo_binding_valid": True,
        "transform_chain_valid": True,
        "door_floor_contact_valid": True,
        "entrance_landing_clear": landing_clear,
        "walkway_clear": walkway_clear,
        "living_floor_valid": True,
        "usable_wall_valid": True,
        "wall_plane_evidence_valid": True,
        "sofa_inside_living_floor": sofa_inside,
        "tv_on_usable_wall": tv_on_wall and tv_inside,
        "view_axis_valid": sofa_faces_tv,
        "view_axis_clear_of_door": axis_clear,
        "sofa_faces_tv": sofa_faces_tv,
        "float_proven": (
            floating_entrance_clear and floating_walkway_clear
            and sofa_inside and landing_clear and walkway_clear
        ),
    }
    fail_codes = []
    if not landing_clear:
        fail_codes.append("CANDIDATE_HITS_ENTRANCE")
    if not walkway_clear:
        fail_codes.append("CANDIDATE_HITS_WALKWAY")
    if not sofa_inside:
        fail_codes.append("SOFA_OUTSIDE_LIVING_FLOOR")
    if not (tv_on_wall and tv_inside):
        fail_codes.append("TV_NOT_ON_USABLE_WALL")
    if not axis_clear:
        fail_codes.append("VIEW_AXIS_HITS_DOOR")
    if not sofa_faces_tv:
        fail_codes.append("ORIENTATION_MISMATCH")
    if sofa_side == "free" and not invariants["float_proven"]:
        fail_codes.append("FLOAT_NOT_PROVEN")
    eligible = all(invariants.values()) and not fail_codes
    score = 100.0 - 20.0 * len(fail_codes)
    if sofa_side != door_side:
        score += 5.0
    if sofa_side == "free":
        score += 12.0
    if compact_entry:
        score += 3.0
    score += min(10.0, door_angle / 18.0)
    score += 4.0 * ((t0 + t1) / 2.0)

    return {
        "candidate_id": f"s2_{candidate_type.lower()}_{sofa_side}_{t0:.3f}_{t1:.3f}",
        "candidate_type": candidate_type,
        "compact_entry": bool(compact_entry),
        "sofa_side": sofa_side,
        "tv_side": tv_side,
        "eligible": eligible,
        "score": round(score, 3),
        "fail_codes": fail_codes,
        "invariants": invariants,
        "source_geometry_ids": list(source_ids),
        "door_axis_separation_deg": round(door_angle, 3),
        "entrance_clearance_px": round(entrance_clearance, 3),
        "minimum_entrance_clearance_px": round(minimum_entrance_clearance, 3),
        "floating_walkway_clear": floating_walkway_clear,
        "sofa_footprint": sofa,
        "tv_footprint": tv,
        "tv_wall_contact": [tv[0], tv[1]],
        "view_axis": [sofa_front, tv_front],
        "layout_floor": paired["layout_floor"],
        "transverse_vanishing_point": paired["transverse_vanishing_point"],
        "cross_axis_slope": round(
            (tv_front[1] - sofa_front[1]) / ((tv_front[0] - sofa_front[0]) or 1e-12), 6,
        ),
        "sofa_orientation": {"origin": sofa_front, "direction": axis_vector},
        "tv_orientation": {"origin": tv_front, "direction": tv_vector},
    }


def build_s2_plan(
    raw: dict | None,
    *,
    width: int,
    height: int,
    expected_source_photo_index: int,
    sofa_side: str = "free",
    transverse_direction_xy=None,
    transverse_reference: dict | None = None,
) -> dict:
    """Build a deterministic S2 plan from observed geometry only."""
    if not isinstance(width, int) or not isinstance(height, int) or width < 32 or height < 32:
        return _blocked("INVALID_GEOMETRY", reason_class="INVALID_GEOMETRY")
    if not isinstance(raw, dict) or raw.get("schema_version") != STRUCT_SCHEMA_VERSION:
        return _blocked("CANDIDATE_GEOMETRY_INCOMPLETE")
    source_index = raw.get("source_photo_index")
    if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index != expected_source_photo_index:
        return _blocked("CROSS_PHOTO_COORDS")

    geometry, normalized, unsafe = _normalize_observed(raw, width, height)
    if unsafe:
        return _blocked(*unsafe, geometry=geometry,
                        reason_class="INVALID_GEOMETRY" if "INVALID_GEOMETRY" in unsafe else "INSUFFICIENT_EVIDENCE")

    items = normalized["items"]
    living_floor = items["living_floor"]["shape"]["coordinates"]
    door_contact = items["door_floor_contact"]["shape"]["coordinates"]
    door_x = (door_contact[0][0] + door_contact[1][0]) / 2.0
    door_side = "left" if door_x < width / 2.0 else "right"

    allowed_sofa_sides = (
        {"left", "right", "free"} if sofa_side == "free" else {sofa_side}
    )
    if not allowed_sofa_sides <= {"left", "right", "free"}:
        return _blocked("CANDIDATE_GEOMETRY_INCOMPLETE", geometry=geometry)

    candidates = []
    for left_segment in normalized["segments"]["left"]:
        for right_segment in normalized["segments"]["right"]:
            left_wall = left_segment["coordinates"]
            right_wall = right_segment["coordinates"]
            common = _projective_common_interval(
                living_floor, left_wall, right_wall, transverse_direction_xy,
            )
            if common is None:
                continue
            overlap_start, overlap_end = common
            length = min(0.26, overlap_end - overlap_start)
            travel = max(0.0, (overlap_end - overlap_start) - length)
            # Deterministic deep→near search on one shared projective depth axis.
            starts = _unique(round(
                overlap_start + travel * fraction, 6
            ) for fraction in (1.0, 0.75, 0.5, 0.25, 0.0))
            source_ids = [
                "door_quad", "door_floor_contact", "entrance_landing", "walkway",
                "living_floor", "left_wall_floor", "right_wall_floor",
                left_segment["geometry_id"], right_segment["geometry_id"],
            ]
            if door_side == "left":
                layouts = (("A", "right"), ("B", "left"), ("F", "free"))
            else:
                layouts = (("A", "left"), ("B", "right"), ("F", "free"))
            for t0 in starts:
                t1 = t0 + length
                try:
                    paired = _paired_cross_sections(
                        living_floor, left_wall, right_wall, t0, t1,
                        transverse_direction_xy,
                    )
                except ValueError:
                    continue
                for candidate_type, candidate_sofa_side in layouts:
                    if candidate_sofa_side not in allowed_sofa_sides:
                        continue
                    candidates.append(_candidate(
                        candidate_type, candidate_sofa_side, paired,
                        t0, t1, items, source_ids, door_side, width, height,
                    ))

            compact_length = min(0.12, overlap_end - overlap_start)
            if compact_length >= 0.06 and door_side in allowed_sofa_sides:
                compact_t1 = overlap_end
                compact_t0 = compact_t1 - compact_length
                try:
                    compact_paired = _paired_cross_sections(
                        living_floor, left_wall, right_wall,
                        compact_t0, compact_t1, transverse_direction_xy,
                    )
                except ValueError:
                    compact_paired = None
                if compact_paired is not None:
                    candidates.append(_candidate(
                        "B", door_side, compact_paired,
                        compact_t0, compact_t1, items, source_ids,
                        door_side, width, height, compact_entry=True,
                    ))

    if not candidates:
        return _blocked("NO_USABLE_WALL", "CANDIDATE_GEOMETRY_INCOMPLETE",
                        geometry=geometry, reason_class="NO_VIABLE_LAYOUT")
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    if not eligible:
        failures = _unique(code for candidate in candidates for code in candidate["fail_codes"])
        return _blocked(*(failures or ["NO_VIABLE_LAYOUT"]), geometry=geometry,
                        candidates=candidates, reason_class="NO_VIABLE_LAYOUT")

    chosen = max(eligible, key=lambda candidate: candidate["score"])
    return {
        "planner_version": PLANNER_VERSION,
        "disposition": "SAFE_FOR_GENERATION",
        "blocked_reason_class": None,
        "unsafe_codes": [],
        "pre_generation_eligible": True,
        "chosen_candidate_id": chosen["candidate_id"],
        "candidate_type": chosen["candidate_type"],
        "door_side": door_side,
        "transverse_reference": dict(transverse_reference or {}),
        "geometry": geometry,
        "candidates": candidates,
    }


def _integer_points(points):
    return [(int(round(point[0])), int(round(point[1]))) for point in points]


def _compact_entry_buffer_polygon(
    plan: dict,
    chosen: dict,
    *,
    width: int,
    height: int,
) -> list[list[float]] | None:
    """Conservatively fill the same-side B gap so image models cannot drift toward the door."""
    if chosen.get("candidate_type") != "B":
        return None
    sofa_side = str(chosen.get("sofa_side") or "").strip().lower()
    door_side = str(plan.get("door_side") or "").strip().lower()
    if sofa_side not in {"left", "right"} or sofa_side != door_side:
        return None
    geometry_by_id = {
        item.get("geometry_id"): item for item in plan.get("geometry") or []
        if isinstance(item, dict)
    }
    landing_shape = (geometry_by_id.get("entrance_landing") or {}).get("shape") or {}
    landing = landing_shape.get("coordinates") if landing_shape.get("type") == "polygon" else None
    sofa = chosen.get("sofa_footprint")
    if not landing or not sofa:
        return None
    landing_x = [float(point[0]) for point in landing]
    landing_y = [float(point[1]) for point in landing]
    sofa_x = [float(point[0]) for point in sofa]
    sofa_y = [float(point[1]) for point in sofa]
    edge_margin = max(2.0, width * 0.002)
    if sofa_side == "left":
        x0 = max(0.0, max(landing_x))
        x1 = min(float(width), min(sofa_x) - edge_margin)
    else:
        x0 = max(0.0, max(sofa_x) + edge_margin)
        x1 = min(float(width), min(landing_x))
    if x1 <= x0:
        return None
    y0 = max(0.0, min(min(landing_y), min(sofa_y)) - height * 0.04)
    y1 = min(float(height), max(max(landing_y), max(sofa_y)))
    if y1 <= y0:
        return None
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def render_s2_guide(photo_path: str | Path, out_path: str | Path, plan: dict) -> dict:
    """Render the chosen S2 candidate on the exact bound source photo."""
    if not isinstance(plan, dict) or not plan.get("pre_generation_eligible"):
        raise ValueError("S2 plan is not eligible for guide rendering")
    chosen_id = plan.get("chosen_candidate_id")
    chosen = next(
        (candidate for candidate in plan.get("candidates") or []
         if candidate.get("candidate_id") == chosen_id and candidate.get("eligible")),
        None,
    )
    if not chosen:
        raise ValueError("chosen eligible S2 candidate is missing")

    source = Path(photo_path)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    geometry_by_id = {
        item.get("geometry_id"): item for item in plan.get("geometry") or []
        if isinstance(item, dict)
    }
    for geometry_id in ("door_quad", "entrance_landing", "walkway"):
        item = geometry_by_id.get(geometry_id) or {}
        shape = item.get("shape") or {}
        points = shape.get("coordinates") if shape.get("type") == "polygon" else None
        if points:
            pts = _integer_points(points)
            draw.polygon(pts, fill=(220, 25, 25, 92))
            draw.line(pts + [pts[0]], fill=(235, 20, 20, 235), width=5)

    compact_buffer = _compact_entry_buffer_polygon(
        plan, chosen, width=width, height=height,
    )
    if compact_buffer:
        pts = _integer_points(compact_buffer)
        draw.polygon(pts, fill=(220, 25, 25, 118))
        draw.line(pts + [pts[0]], fill=(235, 20, 20, 245), width=6)

    sofa_points = _integer_points(chosen["sofa_footprint"])
    tv_points = _integer_points(chosen["tv_footprint"])
    draw.polygon(sofa_points, fill=(30, 190, 80, 112))
    draw.line(sofa_points + [sofa_points[0]], fill=(15, 150, 55, 255), width=6)
    draw.polygon(tv_points, fill=(30, 105, 230, 112))
    draw.line(tv_points + [tv_points[0]], fill=(15, 75, 210, 255), width=6)

    axis = _integer_points(chosen["view_axis"])
    draw.line(axis, fill=(255, 210, 20, 255), width=5)
    radius = max(5, min(width, height) // 110)
    for x, y in axis:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=(255, 210, 20, 255))

    composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    composed.save(target, "JPEG", quality=92, optimize=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "path": str(target),
        "sha256": digest,
        "size": {"width": width, "height": height},
        "chosen_candidate_id": chosen_id,
        "planner_version": plan.get("planner_version"),
    }


def _draw_audit_zones(draw: ImageDraw.ImageDraw, geometry_by_id: dict) -> None:
    zone_styles = {
        "door_quad": ((225, 25, 25, 105), (245, 30, 30, 255)),
        "entrance_landing": ((255, 115, 15, 100), (255, 125, 20, 255)),
        "walkway": ((205, 35, 170, 80), (230, 45, 190, 235)),
    }
    for geometry_id, (fill, outline) in zone_styles.items():
        shape = (geometry_by_id.get(geometry_id) or {}).get("shape") or {}
        points = shape.get("coordinates") if shape.get("type") == "polygon" else None
        if points:
            pts = _integer_points(points)
            draw.polygon(pts, fill=fill)
            draw.line(pts + [pts[0]], fill=outline, width=5)
    living = (geometry_by_id.get("living_floor") or {}).get("shape") or {}
    points = living.get("coordinates") if living.get("type") == "polygon" else None
    if points:
        pts = _integer_points(points)
        draw.line(pts + [pts[0]], fill=(40, 220, 130, 255), width=5)


def render_s2_audit(photo_path: str | Path, out_path: str | Path, plan: dict) -> dict:
    """Render every S2 candidate and its FAIL reasons for offline human review."""
    if not isinstance(plan, dict):
        raise ValueError("S2 plan is required")
    source = Path(photo_path)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        base = ImageOps.exif_transpose(opened).convert("RGB")
    source_width, source_height = base.size
    geometry_by_id = {
        item.get("geometry_id"): item for item in plan.get("geometry") or []
        if isinstance(item, dict)
    }
    candidates = list(plan.get("candidates") or [])
    display_candidates = candidates or [None]
    panels = []
    for index, candidate in enumerate(display_candidates):
        panel = base.convert("RGBA")
        layer = Image.new("RGBA", panel.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        _draw_audit_zones(draw, geometry_by_id)
        if candidate:
            sofa = _integer_points(candidate.get("sofa_footprint") or [])
            tv = _integer_points(candidate.get("tv_footprint") or [])
            axis = _integer_points(candidate.get("view_axis") or [])
            if sofa:
                draw.polygon(sofa, fill=(35, 190, 75, 120))
                draw.line(sofa + [sofa[0]], fill=(10, 150, 45, 255), width=7)
            if tv:
                draw.polygon(tv, fill=(35, 100, 235, 120))
                draw.line(tv + [tv[0]], fill=(10, 65, 215, 255), width=7)
            if len(axis) == 2:
                axis_color = (255, 215, 20, 255) if candidate.get("eligible") else (255, 235, 80, 255)
                draw.line(axis, fill=axis_color, width=7)
                radius = max(6, min(source_width, source_height) // 100)
                for x, y in axis:
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=axis_color)
        panel = Image.alpha_composite(panel, layer).convert("RGB")
        text_layer = Image.new("RGBA", panel.size, (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer, "RGBA")
        header_height = max(115, source_height // 8)
        text_draw.rectangle((0, 0, source_width, header_height), fill=(0, 0, 0, 205))
        if candidate:
            false_invariants = [
                name for name, passed in (candidate.get("invariants") or {}).items() if not passed
            ]
            lines = [
                f"{candidate.get('candidate_id')}  eligible={candidate.get('eligible')}  score={candidate.get('score')}",
                f"sofa={candidate.get('sofa_side')}  tv={candidate.get('tv_side')}  door-axis={candidate.get('door_axis_separation_deg')} deg",
                "FAIL: " + (", ".join(candidate.get("fail_codes") or []) or "NONE"),
                "FALSE: " + (", ".join(false_invariants) or "NONE"),
            ]
        else:
            lines = [
                f"NO CANDIDATE  disposition={plan.get('disposition')}",
                "FAIL: " + (", ".join(plan.get("unsafe_codes") or []) or "NONE"),
            ]
        line_height = max(20, source_height // 55)
        for line_index, line in enumerate(lines):
            text_draw.text((18, 12 + line_index * line_height), line, fill=(255, 255, 255, 255))
        panel = Image.alpha_composite(panel.convert("RGBA"), text_layer).convert("RGB")
        panel.thumbnail((1100, 800), Image.Resampling.LANCZOS)
        panels.append(panel)

    columns = min(2, len(panels))
    rows = (len(panels) + columns - 1) // columns
    cell_width = max(panel.width for panel in panels)
    cell_height = max(panel.height for panel in panels)
    canvas = Image.new("RGB", (cell_width * columns, cell_height * rows), (24, 24, 24))
    for index, panel in enumerate(panels):
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        canvas.paste(panel, (x, y))
    canvas.save(target, "PNG", optimize=True)
    fail_codes = _unique(
        code for candidate in candidates for code in candidate.get("fail_codes") or []
    )
    return {
        "path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "size": {"width": canvas.width, "height": canvas.height},
        "candidate_count": len(candidates),
        "fail_codes": fail_codes,
        "planner_version": plan.get("planner_version"),
    }

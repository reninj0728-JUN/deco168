# -*- coding: utf-8 -*-
"""Fail-closed S2 geometry verification and one-cycle wall correction."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

import layout_geometry_s2 as lgs2


# 前置幾何判官的嘗試上限。D85B8525 實測：同一張照片＋同一份合約連跑 8 次
# 得到 3 pass / 5 fail，5 次失敗還給出 5 種互不相同的欄位組合＝判官雜訊。
# 每次只是一通 flash 呼叫、發生在任何 fal 花費之前，用它換「已付費的單至少
# 生得出圖」非常划算：單次通過率約 37%，四次至少一次通過約 84%。
# 需要臨時退回舊行為時設 S2_VERIFY_MAX_ATTEMPTS=2。
S2_VERIFY_MAX_ATTEMPTS = max(2, int(os.environ.get("S2_VERIFY_MAX_ATTEMPTS", "4") or 4))


_REQUIRED_PASS_FIELDS = (
    "right_wall_floor_alignment",
    "left_wall_floor_alignment",
    "sofa_back_contact",
    "tv_wall_contact",
    "axis_clear_of_entrance",
    "walkway_connected",
    "source_entrance_retained",
    "cross_axis_matches_floor_transverse",
)

_RETRYABLE_VERIFIER_ERROR_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "deadline",
    "rate limit",
    "ratelimit",
    "resource exhausted",
    "resourceexhausted",
    "service unavailable",
    "serviceunavailable",
    "temporarily unavailable",
    "timeout",
    "timed out",
)


class VerifierResponseError(ValueError):
    """Verifier output is malformed or incomplete and therefore unsafe."""


def parse_verifier_response(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise VerifierResponseError("verifier JSON is empty")
    stripped = text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise VerifierResponseError(f"verifier JSON has trailing or invalid content: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VerifierResponseError("verifier JSON must be an object")
    return parsed


def _parse_floating_verifier_response(text: str) -> dict:
    parsed = parse_verifier_response(text)
    required = (
        "tv_intended_wall_contact",
        "floating_sofa_inside_living_floor",
        "floating_sofa_rear_clear",
        "floating_sofa_clear_of_entrance_walkway",
        "axis_clear_of_entrance",
        "walkway_connected",
        "source_entrance_retained",
        "shared_floor_depth_axis",
    )
    for field in required:
        if parsed.get(field) not in {"pass", "fail", "uncertain"}:
            raise VerifierResponseError(f"floating verifier field is missing or invalid: {field}")
    inside_and_clear = (
        parsed["floating_sofa_inside_living_floor"] == "pass"
        and parsed["floating_sofa_clear_of_entrance_walkway"] == "pass"
    )
    return {
        "right_wall_floor_alignment": parsed["tv_intended_wall_contact"],
        "left_wall_floor_alignment": "pass" if inside_and_clear else "fail",
        "sofa_back_contact": parsed["floating_sofa_rear_clear"],
        "tv_wall_contact": parsed["tv_intended_wall_contact"],
        "axis_clear_of_entrance": parsed["axis_clear_of_entrance"],
        "walkway_connected": parsed["walkway_connected"],
        "source_entrance_retained": parsed["source_entrance_retained"],
        "cross_axis_matches_floor_transverse": parsed["shared_floor_depth_axis"],
        "corrected_left_wall_floor_segment_yx1000": None,
        "corrected_right_wall_floor_segment_yx1000": None,
        "overall": parsed.get("overall"),
        "unsafe_codes": list(parsed.get("unsafe_codes") or []),
        "notes": str(parsed.get("notes") or ""),
        "floating_audit": {field: parsed[field] for field in required},
    }


def verification_passes(result: dict) -> bool:
    if not isinstance(result, dict) or result.get("overall") != "pass":
        return False
    return all(result.get(field) == "pass" for field in _REQUIRED_PASS_FIELDS)


def _verifier_failed_fields(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    return {
        field: result.get(field, "missing")
        for field in _REQUIRED_PASS_FIELDS
        if result.get(field) != "pass"
    }


def _verifier_has_hard_fail(result: dict | None) -> bool:
    return any(
        status == "fail"
        for status in _verifier_failed_fields(result).values()
    )


def _is_retryable_verifier_exception(error: Exception) -> bool:
    # verify_s2_guide_gemini already retries malformed JSON once internally;
    # do not multiply that path into four paid model calls here.
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    signature = f"{type(error).__name__}: {error}".lower()
    return any(marker in signature for marker in _RETRYABLE_VERIFIER_ERROR_MARKERS)


def _format_failed_fields(result: dict | None) -> str:
    return ",".join(
        f"{field}:{status}"
        for field, status in _verifier_failed_fields(result).items()
    )


def _verdict_history_entry(verdict: dict, attempt_number: int) -> dict:
    entry = copy.deepcopy(verdict)
    entry["attempt_number"] = attempt_number
    entry["outcome"] = (
        "pass" if verification_passes(verdict)
        else "hard_fail" if _verifier_has_hard_fail(verdict)
        else "uncertain"
    )
    entry["failed_fields"] = _verifier_failed_fields(verdict)
    return entry


def _exception_history_entry(
    error: Exception,
    attempt_number: int,
    retryable: bool,
) -> dict:
    return {
        "attempt_number": attempt_number,
        "outcome": "exception",
        "exception_type": type(error).__name__,
        "exception_message": str(error)[:240],
        "retryable": retryable,
    }


def _valid_segment_yx1000(value) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    points = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            return False
        if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in point):
            return False
        if any(number < 0 or number > 1000 for number in point):
            return False
        points.append(point)
    return points[0] != points[1]


def _line_point_yx(line, t: float):
    return [
        float(line[0][0]) + (float(line[1][0]) - float(line[0][0])) * t,
        float(line[0][1]) + (float(line[1][1]) - float(line[0][1])) * t,
    ]


_MAX_CORRECTION_ANGLE_DELTA_DEGREES = 12.0
_MAX_CORRECTION_DISTANCE_YX1000 = 60.0
_MAX_CORRECTION_EXTRAPOLATION_RATIO = 5.25
_CORRECTION_DEPTH_OVERLAP_MARGIN_YX1000 = 40.0


def _segment_length_yx(segment) -> float:
    return math.hypot(
        float(segment[1][0]) - float(segment[0][0]),
        float(segment[1][1]) - float(segment[0][1]),
    )


def _segment_angle_degrees(segment) -> float:
    dy = float(segment[1][0]) - float(segment[0][0])
    dx = float(segment[1][1]) - float(segment[0][1])
    return math.degrees(math.atan2(dy, dx)) % 180.0


def _orientation_delta_degrees(first, second) -> float:
    delta = abs(_segment_angle_degrees(first) - _segment_angle_degrees(second))
    return min(delta, 180.0 - delta)


def _point_line_distance_yx(point, line) -> float:
    dy = float(line[1][0]) - float(line[0][0])
    dx = float(line[1][1]) - float(line[0][1])
    py = float(point[0]) - float(line[0][0])
    px = float(point[1]) - float(line[0][1])
    length = math.hypot(dy, dx)
    if length <= 1e-6:
        return float("inf")
    return abs(dy * px - dx * py) / length


def _segment_stays_on_side(segment, side: str) -> bool:
    xs = [float(point[1]) for point in segment]
    return all(x < 500.0 for x in xs) if side == "left" else all(x > 500.0 for x in xs)


def _extend_correction_to_usable_depth(raw_geometry: dict, side: str, observed_segment):
    element = (raw_geometry.get("elements") or {}).get(f"{side}_wall_floor") or {}
    original_line = element.get("segment_yx1000")
    usable = [
        item for item in raw_geometry.get("usable_wall_segments", [])
        if isinstance(item, dict) and item.get("side") == side
    ]
    if not _valid_segment_yx1000(original_line) or not usable:
        return None, None
    try:
        starts = [float(item["t_start"]) for item in usable]
        ends = [float(item["t_end"]) for item in usable]
    except (KeyError, TypeError, ValueError):
        return None, None
    global_start, global_end = min(starts), max(ends)
    if not 0.0 <= global_start < global_end <= 1.0:
        return None, None
    target_start = _line_point_yx(original_line, global_start)
    target_end = _line_point_yx(original_line, global_end)
    target_start_y, target_end_y = target_start[0], target_end[0]
    if (
        _orientation_delta_degrees(original_line, observed_segment)
        > _MAX_CORRECTION_ANGLE_DELTA_DEGREES
        or any(
            _point_line_distance_yx(point, original_line) > _MAX_CORRECTION_DISTANCE_YX1000
            for point in observed_segment
        )
        or not _segment_stays_on_side(observed_segment, side)
    ):
        return None, None
    target_y_min, target_y_max = sorted((target_start_y, target_end_y))
    observed_y_min, observed_y_max = sorted((
        float(observed_segment[0][0]), float(observed_segment[1][0]),
    ))
    if (
        observed_y_max < target_y_min - _CORRECTION_DEPTH_OVERLAP_MARGIN_YX1000
        or observed_y_min > target_y_max + _CORRECTION_DEPTH_OVERLAP_MARGIN_YX1000
    ):
        return None, None
    (y0, x0), (y1, x1) = observed_segment
    dy = float(y1) - float(y0)
    if abs(dy) < 1e-6:
        return None, None
    dx_per_y = (float(x1) - float(x0)) / dy

    def at_y(target_y):
        x = float(x0) + (target_y - float(y0)) * dx_per_y
        if not 0.0 <= x <= 1000.0:
            raise ValueError("extended correction exits normalized source bounds")
        return [target_y, x]

    try:
        extended = [at_y(target_start_y), at_y(target_end_y)]
    except ValueError:
        return None, None
    observed_length = _segment_length_yx(observed_segment)
    extended_length = _segment_length_yx(extended)
    if (
        observed_length <= 1e-6
        or extended_length / observed_length > _MAX_CORRECTION_EXTRAPOLATION_RATIO
        or not _segment_stays_on_side(extended, side)
        or any(
            _point_line_distance_yx(point, original_line) > _MAX_CORRECTION_DISTANCE_YX1000
            for point in extended
        )
    ):
        return None, None
    span = global_end - global_start
    remapped = []
    for item in usable:
        cloned = copy.deepcopy(item)
        cloned["id"] = f"{item.get('id') or side + '-wall'}-verifier-corrected"
        cloned["status"] = "verifier_corrected"
        cloned["visibility"] = "partial"
        cloned["confidence"] = "medium"
        cloned["correction_evidence"] = {
            "observed_segment_yx1000": copy.deepcopy(observed_segment),
            "derivation": "bounded_line_extension",
        }
        cloned["t_start"] = (float(item["t_start"]) - global_start) / span
        cloned["t_end"] = (float(item["t_end"]) - global_start) / span
        remapped.append(cloned)
    return extended, remapped


def _apply_wall_corrections(raw_geometry: dict, verdict: dict) -> tuple[dict, bool]:
    corrected = copy.deepcopy(raw_geometry)
    changed = False
    side_specs = (
        ("left", "left_wall_floor_alignment", "corrected_left_wall_floor_segment_yx1000"),
        ("right", "right_wall_floor_alignment", "corrected_right_wall_floor_segment_yx1000"),
    )
    for side, alignment_field, correction_field in side_specs:
        if verdict.get(alignment_field) != "fail":
            continue
        segment = verdict.get(correction_field)
        if not _valid_segment_yx1000(segment):
            continue
        element_key = f"{side}_wall_floor"
        element = corrected.get("elements", {}).get(element_key)
        if not isinstance(element, dict):
            continue
        extended_segment, remapped_segments = _extend_correction_to_usable_depth(
            corrected, side, segment,
        )
        if not extended_segment or not remapped_segments:
            continue
        element["segment_yx1000"] = extended_segment
        element["status"] = "verifier_corrected"
        element["visibility"] = "partial"
        element["confidence"] = "medium"
        element["correction_evidence"] = {
            "observed_segment_yx1000": copy.deepcopy(segment),
            "derivation": "bounded_line_extension",
        }
        retained_segments = [
            item for item in corrected.get("usable_wall_segments", [])
            if isinstance(item, dict) and item.get("side") != side
        ]
        retained_segments.extend(remapped_segments)
        corrected["usable_wall_segments"] = retained_segments
        changed = True
    if changed:
        corrected.setdefault("verification_history", []).append({
            "action": "wall_floor_correction",
            "producer": "layout_geometry_verifier_s2",
            "unsafe_codes": list(verdict.get("unsafe_codes") or []),
        })
    return corrected, changed


def _blocked_after_verification(plan: dict, verdict: dict | None, detail: str) -> dict:
    blocked = copy.deepcopy(plan)
    candidates = []
    for candidate in blocked.get("candidates") or []:
        candidate["eligible"] = False
        candidate["fail_codes"] = list(dict.fromkeys([
            *(candidate.get("fail_codes") or []), "GEOM_NOT_ELIGIBLE",
        ]))
        candidates.append(candidate)
    blocked.update({
        "disposition": "BLOCKED",
        "blocked_reason_class": "INVALID_GEOMETRY",
        "unsafe_codes": list(dict.fromkeys([
            *(blocked.get("unsafe_codes") or []), "GEOM_NOT_ELIGIBLE",
        ])),
        "pre_generation_eligible": False,
        "chosen_candidate_id": None,
        "candidate_type": None,
        "candidates": candidates,
        "geometry_verification": {
            "status": "fail",
            "attempt_count": 1,
            "corrected": False,
            "unsafe_codes": list((verdict or {}).get("unsafe_codes") or []),
            "detail": detail,
        },
    })
    return blocked


def _finalize_blocked_verification(
    plan: dict,
    verdict: dict | None,
    detail: str,
    *,
    attempt_number: int,
    correction_applied: bool,
    retry_reason: str | None = None,
    error: Exception | None = None,
) -> dict:
    blocked = _blocked_after_verification(plan, verdict, detail)
    verification = blocked["geometry_verification"]
    verification["attempt_count"] = attempt_number
    verification["corrected"] = correction_applied
    verification["failed_fields"] = _verifier_failed_fields(verdict)
    if retry_reason:
        verification["retry_reason"] = retry_reason
    if error is not None:
        verification["exception_type"] = type(error).__name__
        verification["exception_message"] = str(error)[:240]
    return blocked


_VERIFIER_PROMPT = """You are the final structural geometry gate before a paid interior render.
Image 1 is the original source photo and is the only visual evidence.
Image 2 is the S2 guide drawn on that exact photo. Guide colours: red entrance/landing/walkway,
green sofa footprint, blue TV/media-console footprint, yellow seated sofa-to-TV centre axis.
Do not trust any SAFE label. Inspect the original source photo and return strict JSON only.

Hard checks — each value must be exactly pass, fail, or uncertain. Apply the candidate-specific
interpretation above; do not assume fixed furniture colours for a fixed wall side:
- right_wall_floor_alignment: the footprint intended for the right wall follows its visible wall/floor seam
- left_wall_floor_alignment: the footprint intended for the left wall follows its seam; when candidate F
  declares no left-wall furniture, pass only if no footprint is falsely anchored there and the green sofa
  is visibly floating clear of entrance/walkway
- sofa_back_contact: wall-anchored candidates touch their intended wall; candidate F instead passes only
  when the green sofa is intentionally floating, fully on living floor, and leaves rear circulation clear
- tv_wall_contact: TV/media-console footprint is anchored to its candidate-declared intended wall
- axis_clear_of_entrance: seated forward ray faces TV and does not go through the apartment entrance
- walkway_connected: furniture leaves a connected entrance-to-interior path
- source_entrance_retained: source photo is uncropped and retains the apartment entrance evidence
- cross_axis_matches_floor_transverse: yellow axis follows one shared projective floor depth,
  consistent with visible transverse tile seams/architectural floor perspective; sofa and TV are not
  paired by mismatched left/right local depth and are not diagonally looking at each other

If a wall alignment fails and the true visible wall/floor seam is unambiguous, return a corrected
segment as exactly two [y,x] points normalized 0..1000, ordered near-camera then deep. Otherwise null.
Never guess an occluded seam. overall may be pass only when all eight hard checks pass.

JSON schema:
{
  "right_wall_floor_alignment":"pass|fail|uncertain",
  "left_wall_floor_alignment":"pass|fail|uncertain",
  "sofa_back_contact":"pass|fail|uncertain",
  "tv_wall_contact":"pass|fail|uncertain",
  "axis_clear_of_entrance":"pass|fail|uncertain",
  "walkway_connected":"pass|fail|uncertain",
  "source_entrance_retained":"pass|fail|uncertain",
  "cross_axis_matches_floor_transverse":"pass|fail|uncertain",
  "corrected_left_wall_floor_segment_yx1000":[[0,0],[0,0]] or null,
  "corrected_right_wall_floor_segment_yx1000":[[0,0],[0,0]] or null,
  "overall":"pass|fail",
  "unsafe_codes":["..."],
  "notes":"short Traditional Chinese audit note"
}
"""


_FLOATING_VERIFIER_PROMPT = r"""
You are auditing an intentional floating-sofa S2 layout guide against its source room photo.
Image 1 is the uncropped source. Image 2 is the guide: red entrance/walkway forbidden zones,
green floating sofa footprint, blue intended-wall TV/media-console footprint, yellow viewing axis.
The green sofa is NOT supposed to touch either wall. Do not require the GREEN sofa to touch either wall.
The candidate-specific text below names the intended TV wall; audit the BLUE TV against that wall only.
Judge only visible geometry. uncertain is not pass. Return strict JSON only:
{
  "tv_intended_wall_contact": "pass|fail|uncertain",
  "floating_sofa_inside_living_floor": "pass|fail|uncertain",
  "floating_sofa_rear_clear": "pass|fail|uncertain",
  "floating_sofa_clear_of_entrance_walkway": "pass|fail|uncertain",
  "axis_clear_of_entrance": "pass|fail|uncertain",
  "walkway_connected": "pass|fail|uncertain",
  "source_entrance_retained": "pass|fail|uncertain",
  "shared_floor_depth_axis": "pass|fail|uncertain",
  "overall": "pass|fail|uncertain",
  "unsafe_codes": ["short_code"],
  "notes": "short evidence-based explanation"
}
All eight checks must be pass for overall=pass. Do not return wall correction segments for candidate F.
"""


def _is_floating_plan(plan: dict | None) -> bool:
    if not isinstance(plan, dict):
        return False
    chosen_id = plan.get("chosen_candidate_id")
    return any(
        isinstance(candidate, dict)
        and candidate.get("candidate_id") == chosen_id
        and candidate.get("candidate_type") == "F"
        and candidate.get("sofa_side") == "free"
        for candidate in (plan.get("candidates") or [])
    )


def _mime_type(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")


def _verifier_candidate_context(plan: dict | None) -> str:
    if not isinstance(plan, dict):
        return "No candidate metadata supplied; use the default wall-anchored interpretation."
    chosen_id = plan.get("chosen_candidate_id")
    chosen = next(
        (
            candidate for candidate in (plan.get("candidates") or [])
            if isinstance(candidate, dict) and candidate.get("candidate_id") == chosen_id
        ),
        {},
    )
    if chosen.get("candidate_type") == "F" and chosen.get("sofa_side") == "free":
        tv_side = str(chosen.get("tv_side") or "unknown").upper()
        return (
            "Candidate F is intentional: the GREEN sofa is floating inside the living floor and "
            "must leave clear space behind it; it is not supposed to touch either wall. "
            f"The BLUE TV/media console must contact the visible {tv_side} wall/floor seam. "
            "Audit the green footprint only as a floating sofa: fully inside the living floor, "
            "clear of the entrance and walkway, with a visible rear circulation band."
        )
    return (
        f"Candidate {chosen.get('candidate_type') or 'unknown'}: GREEN sofa side="
        f"{chosen.get('sofa_side') or 'unknown'}, BLUE TV side="
        f"{chosen.get('tv_side') or 'unknown'}. Audit each footprint against its intended wall."
    )


def verify_s2_guide_gemini(
    photo_path: str | Path,
    guide_path: str | Path,
    attempt_number: int,
    plan: dict | None = None,
    *,
    client=None,
) -> dict:
    """Verify source+guide with strict JSON; malformed output raises and blocks upstream."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(f"google-genai not installed: {exc}") from exc

    source = Path(photo_path)
    guide = Path(guide_path)
    if not source.is_file() or not guide.is_file():
        raise FileNotFoundError("source photo or S2 guide is missing")
    if client is None:
        key = (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_AI_KEY")
            or ""
        ).strip()
        if not key:
            raise RuntimeError("missing GEMINI_API_KEY / GOOGLE_AI_KEY")
        client = genai.Client(api_key=key)

    floating_plan = _is_floating_plan(plan)
    verifier_prompt = _FLOATING_VERIFIER_PROMPT if floating_plan else _VERIFIER_PROMPT
    response_parser = _parse_floating_verifier_response if floating_plan else parse_verifier_response
    contents = [
        types.Part.from_bytes(data=source.read_bytes(), mime_type=_mime_type(source)),
        types.Part.from_bytes(data=guide.read_bytes(), mime_type=_mime_type(guide)),
        f"Attempt {attempt_number}.\nCandidate-specific interpretation:\n"
        f"{_verifier_candidate_context(plan)}\n{verifier_prompt}",
    ]
    last_parse_error = None
    for response_attempt in (1, 2):
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        try:
            return response_parser((response.text or "").strip())
        except VerifierResponseError as exc:
            last_parse_error = exc
            if response_attempt == 2:
                raise
            print("[layout-verifier] malformed Gemini JSON; retrying once")
    raise last_parse_error or VerifierResponseError("verifier JSON retry exhausted")


def verify_and_replan_s2(
    *,
    raw_geometry: dict,
    photo_path: str | Path,
    output_dir: str | Path,
    expected_source_photo_index: int,
    sofa_side: str,
    verifier: Callable[[str | Path, str | Path, int], dict],
    floor_reference_estimator=None,
) -> dict:
    """Plan, verify, optionally correct one wall cycle, and fail closed."""
    import layout_floor_reference_s2 as floor_reference_s2

    source = Path(photo_path)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        width, height = ImageOps.exif_transpose(image).size

    active_raw = copy.deepcopy(raw_geometry)
    living_floor = (
        ((active_raw.get("elements") or {}).get("living_floor") or {})
        .get("polygon_yx1000")
    )
    estimator = floor_reference_estimator or floor_reference_s2.estimate_transverse_floor_reference
    floor_reference = estimator(source, living_floor)
    observed_direction = (
        floor_reference.get("direction_xy")
        if isinstance(floor_reference, dict) and floor_reference.get("status") == "observed"
        else None
    )
    plan = lgs2.build_s2_plan(
        active_raw,
        width=width,
        height=height,
        expected_source_photo_index=expected_source_photo_index,
        sofa_side=sofa_side,
        transverse_direction_xy=observed_direction,
        transverse_reference=floor_reference if isinstance(floor_reference, dict) else None,
    )
    if observed_direction is None:
        blocked = _blocked_after_verification(
            plan, None, "observed transverse floor reference is unavailable",
        )
        blocked["transverse_reference"] = copy.deepcopy(floor_reference)
        blocked["geometry_verification"]["unsafe_codes"] = list(
            (floor_reference or {}).get("unsafe_codes")
            or ["MISSING_TRANSVERSE_FLOOR_REFERENCE"]
        )
        return {
            "plan": blocked,
            "raw_geometry": active_raw,
            "guide_artifact": None,
            "verification_history": [],
        }
    if not plan.get("pre_generation_eligible"):
        return {
            "plan": plan,
            "raw_geometry": active_raw,
            "guide_artifact": None,
            "verification_history": [],
        }

    history = []
    correction_applied = False
    retry_reason = None
    last_failed_fields: frozenset = frozenset()
    for attempt_number in range(1, S2_VERIFY_MAX_ATTEMPTS + 1):
        attempt_path = target_dir / f"layout_guide_s2_attempt{attempt_number}.jpg"
        lgs2.render_s2_guide(source, attempt_path, plan)
        try:
            verdict = verifier(source, attempt_path, attempt_number, plan)
            if not isinstance(verdict, dict):
                raise VerifierResponseError("verifier returned a non-object")
        except Exception as exc:
            retryable = _is_retryable_verifier_exception(exc)
            history.append(_exception_history_entry(exc, attempt_number, retryable))
            print(
                f"[layout-verifier] attempt={attempt_number} outcome=exception "
                f"type={type(exc).__name__} retryable={retryable}"
            )
            if attempt_number < S2_VERIFY_MAX_ATTEMPTS and retryable:
                retry_reason = "retryable_exception"
                continue
            blocked = _finalize_blocked_verification(
                plan,
                None,
                f"verifier exception: {type(exc).__name__}",
                attempt_number=attempt_number,
                correction_applied=correction_applied,
                retry_reason=retry_reason,
                error=exc,
            )
            return {
                "plan": blocked,
                "raw_geometry": active_raw,
                "guide_artifact": None,
                "verification_history": history,
            }
        history.append(_verdict_history_entry(verdict, attempt_number))
        if verification_passes(verdict):
            final_path = target_dir / "layout_guide_s2_final.jpg"
            artifact = lgs2.render_s2_guide(source, final_path, plan)
            verified_plan = copy.deepcopy(plan)
            verified_plan["geometry_verification"] = {
                "status": "pass",
                "attempt_count": attempt_number,
                "corrected": correction_applied,
                "unsafe_codes": [],
                "failed_fields": {},
                "detail": "all hard verifier checks passed",
            }
            if retry_reason:
                verified_plan["geometry_verification"]["retry_reason"] = retry_reason
            return {
                "plan": verified_plan,
                "raw_geometry": active_raw,
                "guide_artifact": artifact,
                "verification_history": history,
            }
        hard_fail = _verifier_has_hard_fail(verdict)
        if attempt_number == 1 and hard_fail and not correction_applied:
            active_raw, changed = _apply_wall_corrections(active_raw, verdict)
            if changed:
                plan = lgs2.build_s2_plan(
                    active_raw,
                    width=width,
                    height=height,
                    expected_source_photo_index=expected_source_photo_index,
                    sofa_side=sofa_side,
                    transverse_direction_xy=observed_direction,
                    transverse_reference=floor_reference,
                )
                if plan.get("pre_generation_eligible"):
                    correction_applied = True
                    retry_reason = "wall_correction"
                    print(
                        "[layout-verifier] attempt=1 outcome=hard_fail "
                        "action=wall_correction_retry"
                    )
                    continue
        if attempt_number < S2_VERIFY_MAX_ATTEMPTS and not hard_fail:
            retry_reason = "uncertain_verdict"
            print(
                f"[layout-verifier] attempt={attempt_number} outcome=uncertain "
                f"failed_fields={_format_failed_fields(verdict)} "
                "action=same_plan_retry"
            )
            continue
        # 判官對同一張圖確實會自打架，但不是全部都在抖。D85B8525 實測 12 次嘗試：
        # left_wall_floor_alignment 與 sofa_back_contact 每一次都失敗（真的幾何問題，
        # 連牆面修正都救不回），walkway_connected / cross_axis / right_wall / tv_wall
        # 則忽有忽無（判官雜訊）。
        # 所以判準是「判決穩不穩」而不是「重試幾次」：失敗欄位組合跟上一次一模一樣
        # ＝穩定的真問題，立刻定讞、不再燒呼叫；組合每次都變＝判官還沒想清楚，
        # 值得再問一次（一通 flash、發生在任何 fal 花費之前）。
        current_fields = frozenset(_verifier_failed_fields(verdict))
        if (attempt_number < S2_VERIFY_MAX_ATTEMPTS and hard_fail
                and current_fields and current_fields != last_failed_fields):
            last_failed_fields = current_fields
            retry_reason = retry_reason or "unstable_verdict_recheck"
            print(
                f"[layout-verifier] attempt={attempt_number} outcome=hard_fail "
                f"failed_fields={_format_failed_fields(verdict)} "
                "action=same_plan_recheck"
            )
            continue
        detail = (
            "hard verifier checks failed"
            if hard_fail else "verifier remained uncertain after retry"
        )
        blocked = _finalize_blocked_verification(
            plan,
            verdict,
            detail,
            attempt_number=attempt_number,
            correction_applied=correction_applied,
            retry_reason=retry_reason,
        )
        print(
            f"[layout-verifier] attempt={attempt_number} outcome=blocked "
            f"hard_fail={hard_fail} "
            f"failed_fields={_format_failed_fields(verdict)}"
        )
        return {
            "plan": blocked,
            "raw_geometry": active_raw,
            "guide_artifact": None,
            "verification_history": history,
        }

    raise AssertionError("unreachable verifier loop")

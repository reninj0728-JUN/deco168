"""
Step 2 v2 UI 前端 payload 驗證 (本地 only, 不打 fal / Gemini / 不跑新圖).

鏡像 upload.html 的 JS:
    computePhotoContains(target_zone, secondary_zone)
    buildPhotoMeta(photo_keys) per-photo

跑 3 個案例 + 過一次 backend _normalize_photo_meta_for_room 確認後端能吃.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from api import _normalize_photo_meta_for_room  # noqa: E402

SECONDARY_NONE = "none"
DEFAULT_TARGET_ZONE = "living"
DEFAULT_SECONDARY_ZONE = SECONDARY_NONE
DEFAULT_TARGET_LOCATION_HINT = "unspecified"
DEFAULT_TARGET_NOTE = ""
TARGET_NOTE_MAX = 100


def compute_photo_contains(target_zone, secondary_zone):
    """1:1 mirror upload.html computePhotoContains"""
    tz = target_zone or DEFAULT_TARGET_ZONE
    sec = secondary_zone or SECONDARY_NONE
    arr = [tz]
    if sec and sec != SECONDARY_NONE and sec != tz:
        arr.append(sec)
    return arr


def build_photo_meta_entry(photo_key, p):
    """1:1 mirror upload.html buildPhotoMeta(photo_keys).map(...)"""
    tzone = p.get("target_zone") or DEFAULT_TARGET_ZONE
    sec = p.get("secondary_zone") or SECONDARY_NONE
    contains = compute_photo_contains(tzone, sec)
    note = (p.get("target_note") or "")
    if not isinstance(note, str):
        note = ""
    note = note[:TARGET_NOTE_MAX]
    return {
        "photo_key":            photo_key,
        "photo_contains":       contains,
        "target_zone":          tzone,
        "target_location_hint": p.get("target_location_hint") or DEFAULT_TARGET_LOCATION_HINT,
        "target_note":          note,
    }


def _check_payload(label, p_state, photo_key, expected):
    print(f"\n[{label}] state={p_state}")
    out = build_photo_meta_entry(photo_key, p_state)
    print(f"  payload = {out}")
    ok = (
        out["photo_key"] == expected["photo_key"]
        and out["photo_contains"] == expected["photo_contains"]
        and out["target_zone"] == expected["target_zone"]
        and out["target_location_hint"] == expected["target_location_hint"]
        and out["target_note"] == expected["target_note"]
    )
    print(f"  expected={expected}")
    print(f"  payload PASS={ok}")
    return out, ok


def _check_backend_accepts(label, room_type, photo_key, payload_entry):
    """payload 餵後端 _normalize_photo_meta_for_room, 確認不被拒絕."""
    room = {
        "room_type":  room_type,
        "photo_keys": [photo_key],
        "photo_meta": [payload_entry],
    }
    normalized, err = _normalize_photo_meta_for_room(room)
    accepted = (not err) and len(normalized) == 1
    print(f"  backend normalize  err={err!r}  accepted={accepted}")
    if accepted:
        print(f"  normalized[0] = {normalized[0]}")
    return accepted


def case1():
    """主要=客廳, 也包含=不需要, 補充說明空 (toggle 未展開) — hint 由 UI 強制 unspecified"""
    label = "Case 1"
    pk = "uploads/case1/photo_01.jpg"
    p_state = {
        "target_zone":    "living",
        "secondary_zone": SECONDARY_NONE,
        "target_note":    "",
    }
    expected = {
        "photo_key":            pk,
        "photo_contains":       ["living"],
        "target_zone":          "living",
        "target_location_hint": "unspecified",
        "target_note":          "",
    }
    out, payload_ok = _check_payload(label, p_state, pk, expected)
    backend_ok = _check_backend_accepts(label, "living_room", pk, out)
    return payload_ok and backend_ok


def case2():
    """主要=客廳, 也包含=餐廳, 補充說明空 — hint 由 UI 強制 unspecified"""
    label = "Case 2"
    pk = "uploads/case2/photo_01.jpg"
    p_state = {
        "target_zone":    "living",
        "secondary_zone": "dining",
        "target_note":    "",
    }
    expected = {
        "photo_key":            pk,
        "photo_contains":       ["living", "dining"],
        "target_zone":          "living",
        "target_location_hint": "unspecified",
        "target_note":          "",
    }
    out, payload_ok = _check_payload(label, p_state, pk, expected)
    backend_ok = _check_backend_accepts(label, "living_room", pk, out)
    return payload_ok and backend_ok


def case3():
    """主要=客廳, 也包含=走道, 補充說明=客廳靠窗，走道不要放大型家具 (toggle 展開), hint 強制 unspecified"""
    label = "Case 3"
    pk = "uploads/case3/photo_01.jpg"
    note = "客廳靠窗，走道不要放大型家具"
    p_state = {
        "target_zone":    "living",
        "secondary_zone": "walkway",
        "target_note":    note,
    }
    expected = {
        "photo_key":            pk,
        "photo_contains":       ["living", "walkway"],
        "target_zone":          "living",
        "target_location_hint": "unspecified",
        "target_note":          note,
    }
    out, payload_ok = _check_payload(label, p_state, pk, expected)
    backend_ok = _check_backend_accepts(label, "living_room", pk, out)
    return payload_ok and backend_ok


def case_extra_target_zone_eq_secondary():
    """防呆: secondary 跟 target_zone 同值 → 自動去重, photo_contains 只剩一個"""
    label = "Case extra (target==secondary 防呆)"
    pk = "uploads/case_x/photo_01.jpg"
    p_state = {
        "target_zone":    "bedroom",
        "secondary_zone": "bedroom",  # state 偶發同值
        "target_note":    "",
    }
    expected = {
        "photo_key":            pk,
        "photo_contains":       ["bedroom"],
        "target_zone":          "bedroom",
        "target_location_hint": "unspecified",
        "target_note":          "",
    }
    out, payload_ok = _check_payload(label, p_state, pk, expected)
    backend_ok = _check_backend_accepts(label, "bedroom", pk, out)
    return payload_ok and backend_ok


def main():
    print("=" * 72)
    print("Step 2 v2 UI — 前端 buildPhotoMeta + 後端 normalize 驗收")
    print("=" * 72)
    results = [
        ("Case 1", case1()),
        ("Case 2", case2()),
        ("Case 3", case3()),
        ("Case extra (dedupe)", case_extra_target_zone_eq_secondary()),
    ]
    print("\n" + "=" * 72)
    print("驗收彙整")
    print("=" * 72)
    for name, ok in results:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in results)
    print(f"\n總結: {'ALL PASS' if all_ok else 'HAS FAIL'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

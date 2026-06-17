"""
PhotoMeta v1 Step 2 — minimum prompt 注入測試.

目的:
    確認 build_nano_banana_inputs 在收到 target_zone + target_location_hint
    時, 會把 PHOTO TARGET 段 / 對應 zone 英文敘述 / location 英文敘述
    完整寫進 prompt 字串.

範圍:
    - 不打 fal
    - 不打 Gemini
    - 不跑新圖
    - 只組 prompt + 看字串
    - 用最小 entry / zoning input

執行:
    cd backend
    python test_photo_meta_v2_prompt.py
"""

import sys
import os
from pathlib import Path

# 讓本檔案直接 python ./test_photo_meta_v2_prompt.py 也跑得起來
sys.path.insert(0, str(Path(__file__).parent))

from prompt_builder import build_nano_banana_inputs


def _minimal_entry() -> dict:
    """build_nano_banana_inputs 需要的最簡 entry: style + style_label + (空) matched_furniture"""
    return {
        "style":          "modern",
        "style_label":    "現代風",
        "flux_prompt":    "modern living room, clean lines, warm tones",
        "matched_furniture": [],
    }


def _minimal_zoning() -> dict:
    """unusable zoning → build_nano_banana_inputs 會走 fallback layout (不影響本測試)"""
    return {"confidence": "none", "error": "test stub"}


def _assert_contains(haystack: str, needle: str, label: str) -> None:
    if needle in haystack:
        print(f"  PASS  {label}  contains  '{needle[:60]}'")
        return
    print(f"  FAIL  {label}  MISSING  '{needle[:120]}'")
    raise AssertionError(f"prompt missing required phrase: {needle[:120]}")


def case_living_rear_near_window():
    print("\n[case A] target_zone=living, target_location_hint=rear_near_window")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        customer_notes="",
        budget_tier="tier3",
        retry_context=None,
        target_zone="living",
        target_location_hint="rear_near_window",
    )
    prompt = out.get("prompt") or ""
    assert prompt, "build_nano_banana_inputs 沒回傳 prompt"

    _assert_contains(prompt, "PHOTO TARGET", "PHOTO TARGET header")
    _assert_contains(prompt, "living-room area", "target_zone EN expansion")
    _assert_contains(prompt, "BACK / WINDOW-SIDE / DEEP",
                     "target_location_hint EN expansion (rear_near_window)")
    _assert_contains(prompt, "MUST be placed at", "MUST be placed at directive")
    _assert_contains(prompt, "Do NOT place large furniture",
                     "negative constraint on other zones")
    _assert_contains(prompt, "untouched and empty of large furniture",
                     "other-zones-stay-empty wording")


def case_bedroom_left_side():
    print("\n[case B] target_zone=bedroom, target_location_hint=left_side")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_zone="bedroom",
        target_location_hint="left_side",
    )
    prompt = out.get("prompt") or ""
    _assert_contains(prompt, "PHOTO TARGET", "PHOTO TARGET header")
    _assert_contains(prompt, "bedroom", "target_zone EN expansion (bedroom)")
    _assert_contains(prompt, "LEFT side of the room",
                     "target_location_hint EN expansion (left_side)")


def case_unspecified_skips():
    print("\n[case C] target_location_hint=unspecified → 整段 PHOTO TARGET 不應出現")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_zone="living",
        target_location_hint="unspecified",
    )
    prompt = out.get("prompt") or ""
    if "PHOTO TARGET" in prompt:
        print("  FAIL  unspecified 居然出現 PHOTO TARGET 段")
        raise AssertionError("unspecified should suppress PHOTO TARGET section")
    print("  PASS  unspecified 正確跳過 PHOTO TARGET 段")


def case_no_kwargs_skips():
    print("\n[case D] 完全沒傳 kwargs → PHOTO TARGET 不應出現 (back-compat)")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
    )
    prompt = out.get("prompt") or ""
    if "PHOTO TARGET" in prompt:
        print("  FAIL  back-compat 破了: 沒傳 kwargs 也注入了 PHOTO TARGET")
        raise AssertionError("back-compat broken: PHOTO TARGET appeared without kwargs")
    if "USER SUPPLEMENTARY NOTE" in prompt:
        raise AssertionError("back-compat broken: USER SUPPLEMENTARY NOTE appeared without kwargs")
    print("  PASS  沒傳 kwargs 時 prompt 維持原樣 (PHOTO TARGET / USER SUPPLEMENTARY NOTE 都不出現)")


def case_combined_living_dining_walkway():
    """
    Step 2 補完核心案例:
        同一張照片包含【客廳 + 餐廳 + 走道】 (photo_contains 多選),
        target_zone = living,
        target_location_hint = rear_near_window (靠窗深處後段),
        target_note = '餐廳在中間，走道不要放大型家具'
    驗證:
        - PHOTO TARGET 段出現, 鎖死 living + rear_near_window
        - USER SUPPLEMENTARY NOTE 段出現, 內含補充說明原文
        - 段內明示 structured > note > AI inference 的優先順序
        - target_note 不會把 PHOTO TARGET 段擠掉 (兩者共存)
    """
    print("\n[case E] 客廳+餐廳+走道 同框, target=客廳+靠窗深處後段, "
          "note='餐廳在中間，走道不要放大型家具'")
    note = "餐廳在中間，走道不要放大型家具"
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_zone="living",
        target_location_hint="rear_near_window",
        target_note=note,
    )
    prompt = out.get("prompt") or ""

    _assert_contains(prompt, "PHOTO TARGET", "PHOTO TARGET header (structured intent)")
    _assert_contains(prompt, "living-room area", "target_zone=living EN expansion")
    _assert_contains(prompt, "BACK / WINDOW-SIDE / DEEP",
                     "target_location_hint=rear_near_window EN expansion")
    _assert_contains(prompt, "Do NOT place large furniture",
                     "negative constraint on non-target zones")
    _assert_contains(prompt, "USER SUPPLEMENTARY NOTE",
                     "USER SUPPLEMENTARY NOTE header (target_note)")
    _assert_contains(prompt, note, "target_note 原文逐字注入")
    _assert_contains(prompt, "structured fields", "priority order: structured > note")
    _assert_contains(prompt, "structured field wins",
                     "conflict resolution: structured 贏")
    # 順序檢查: PHOTO TARGET 必須在 USER SUPPLEMENTARY NOTE 之前
    pt_idx = prompt.find("PHOTO TARGET")
    un_idx = prompt.find("USER SUPPLEMENTARY NOTE")
    assert pt_idx >= 0 and un_idx >= 0 and pt_idx < un_idx, (
        f"順序錯誤: PHOTO TARGET ({pt_idx}) 必須在 USER SUPPLEMENTARY NOTE ({un_idx}) 之前"
    )
    print("  PASS  PHOTO TARGET 在 USER SUPPLEMENTARY NOTE 之前 (優先順序正確)")


def case_note_only_no_zone():
    print("\n[case F] 只有 target_note, 沒有 target_zone/hint → 仍應注入 NOTE 段")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_note="走道留出 90 cm 通行",
    )
    prompt = out.get("prompt") or ""
    # PHOTO TARGET 段的 header marker (區分 NOTE 段裡引用 "PHOTO TARGET" 字串本身)
    if "PHOTO TARGET (user explicit intent" in prompt:
        raise AssertionError("PHOTO TARGET 段不應在 zone/hint 缺值時出現")
    _assert_contains(prompt, "USER SUPPLEMENTARY NOTE", "NOTE 段在僅有 note 時仍應出現")
    _assert_contains(prompt, "走道留出 90 cm 通行", "note 原文")


def case_note_empty_string_skips():
    print("\n[case G] target_note='' / 全空白 → NOTE 段不出現")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_zone="living",
        target_location_hint="rear_near_window",
        target_note="   ",
    )
    prompt = out.get("prompt") or ""
    if "USER SUPPLEMENTARY NOTE" in prompt:
        raise AssertionError("全空白 target_note 不應觸發 NOTE 段")
    _assert_contains(prompt, "PHOTO TARGET", "PHOTO TARGET 仍正常存在")
    print("  PASS  空白 target_note 不注入 NOTE, PHOTO TARGET 仍正常")


def case_backend_normalize_target_note():
    """
    驗證 backend _normalize_photo_meta_for_room:
      - target_note ≤100 字 → PASS
      - target_note >100 字 → 回 error string (caller 對應 400)
      - target_note 缺 → 退化成 ""
    """
    print("\n[case H] backend _normalize_photo_meta_for_room target_note 驗證")
    from api import _normalize_photo_meta_for_room

    pk = "uploads/test/photo_01.jpg"
    base_room = {
        "room_type":  "living_room",
        "photo_keys": [pk],
    }

    # H1: 有 100 字, 邊界內 → PASS
    room = dict(base_room)
    room["photo_meta"] = [{
        "photo_key":            pk,
        "photo_contains":       ["living", "dining", "walkway"],
        "target_zone":          "living",
        "target_location_hint": "rear_near_window",
        "target_note":          "餐廳在中間，走道不要放大型家具",
    }]
    normalized, err = _normalize_photo_meta_for_room(room)
    assert not err, f"H1 不該失敗, got err={err!r}"
    assert normalized[0]["target_note"] == "餐廳在中間，走道不要放大型家具", \
        f"target_note 內容不對: {normalized[0].get('target_note')!r}"
    assert normalized[0]["photo_contains"] == ["living", "dining", "walkway"]
    print("  PASS  H1 100 字以內 + 多選 photo_contains 正常通過")

    # H2: 超過 100 字 → 回 error
    room = dict(base_room)
    long_note = "X" * 101
    room["photo_meta"] = [{
        "photo_key":            pk,
        "photo_contains":       ["living"],
        "target_zone":          "living",
        "target_location_hint": "unspecified",
        "target_note":          long_note,
    }]
    normalized, err = _normalize_photo_meta_for_room(room)
    assert err and "target_note" in err and "100" in err, (
        f"H2 應該回 target_note 超長 error, 實際 err={err!r}"
    )
    print(f"  PASS  H2 >100 字回 error: {err}")

    # H3: 缺 target_note → 退化成 ""
    room = dict(base_room)
    room["photo_meta"] = [{
        "photo_key":            pk,
        "photo_contains":       ["living"],
        "target_zone":          "living",
        "target_location_hint": "unspecified",
    }]
    normalized, err = _normalize_photo_meta_for_room(room)
    assert not err, f"H3 不該失敗, got err={err!r}"
    assert normalized[0]["target_note"] == "", \
        f"H3 預期 target_note='', got {normalized[0].get('target_note')!r}"
    print("  PASS  H3 缺 target_note 退化成空字串")

    # H4: target_note 非字串 → error
    room = dict(base_room)
    room["photo_meta"] = [{
        "photo_key":            pk,
        "photo_contains":       ["living"],
        "target_zone":          "living",
        "target_location_hint": "unspecified",
        "target_note":          123,
    }]
    normalized, err = _normalize_photo_meta_for_room(room)
    assert err and "target_note" in err, (
        f"H4 應拒絕非字串 target_note, err={err!r}"
    )
    print(f"  PASS  H4 非字串 target_note 拒絕: {err}")


def main():
    print("=" * 60)
    print("PhotoMeta v1 Step 2 — prompt 注入測試")
    print("=" * 60)
    case_living_rear_near_window()
    case_bedroom_left_side()
    case_unspecified_skips()
    case_no_kwargs_skips()
    case_combined_living_dining_walkway()
    case_note_only_no_zone()
    case_note_empty_string_skips()
    case_backend_normalize_target_note()
    print("\nALL PASS")


if __name__ == "__main__":
    main()

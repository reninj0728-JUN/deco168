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
    print("  PASS  沒傳 kwargs 時 prompt 維持原樣")


def main():
    print("=" * 60)
    print("PhotoMeta v1 Step 2 — prompt 注入測試")
    print("=" * 60)
    case_living_rear_near_window()
    case_bedroom_left_side()
    case_unspecified_skips()
    case_no_kwargs_skips()
    print("\nALL PASS")


if __name__ == "__main__":
    main()

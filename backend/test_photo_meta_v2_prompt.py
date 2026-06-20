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
    """
    Step 3 微調 (2026-06-19): hint=unspecified 時 note 段應改為 USER PHOTO DIRECTIVE
    (Step 3 微調收斂版 — 不再宣稱「最高優先級」, 改成「主要照片理解指引」, 並列出 4 個
    不可覆蓋的硬約束, 含 user-confirmed layout binding).
    """
    print("\n[case F] 只有 target_note, 沒有 target_zone/hint → 注入 USER PHOTO DIRECTIVE 段")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_zone="living",   # UI 預設值, 帶進去看 Structured target zone 行為
        target_note="走道留出 90 cm 通行",
    )
    prompt = out.get("prompt") or ""
    # PHOTO TARGET 段不應出現 (hint 缺值)
    if "PHOTO TARGET (user explicit intent" in prompt:
        raise AssertionError("PHOTO TARGET 段不應在 zone/hint 缺值時出現")
    # USER PHOTO DIRECTIVE 段應出現, SUPPLEMENTARY 與舊 PRIMARY 都不應出現
    _assert_contains(prompt, "USER PHOTO DIRECTIVE", "新 PHOTO DIRECTIVE header")
    if "USER SUPPLEMENTARY NOTE" in prompt:
        raise AssertionError("SUPPLEMENTARY 不應在 hint=unspecified 時出現")
    if "USER PRIMARY DIRECTIVE" in prompt:
        raise AssertionError("舊 PRIMARY DIRECTIVE header 不應殘留")
    _assert_contains(prompt, "走道留出 90 cm 通行", "note 原文")
    # Point 4: target_zone 帶進去, 應顯示 "Structured target zone selected by user"
    _assert_contains(prompt, "Structured target zone selected by user",
                     "Point 4: target_zone context line")
    _assert_contains(prompt, "living-room area", "target_zone EN expansion")
    _assert_contains(prompt, "but do not infer a location",
                     "Point 4: 不可由 target_zone 推位置")
    # 應明確告訴 model: 房間不一定有窗 + 不可假設
    _assert_contains(prompt, "may not have a window", "提醒 model 房間可能無窗")
    # 4 個硬約束都要出現
    _assert_contains(prompt, "User-confirmed layout binding",
                     "硬性約束 1: zoning-confirm layout binding")
    _assert_contains(prompt, "Structural preservation",
                     "硬性約束 2: 結構保留")
    _assert_contains(prompt, "Walkway / corridor opening clearance",
                     "硬性約束 3: 動線淨空")
    _assert_contains(prompt, "no floating furniture",
                     "硬性約束 4: 安全規則")


def case_photo_directive_no_window_scenario():
    """
    無窗房間情境: target_note='客廳擺中段, 沒有窗', 沒有結構化 hint.
    應走 USER PHOTO DIRECTIVE 模式.
    Critical: PHOTO TARGET 段不出現, BACK / WINDOW-SIDE 強制詞不出現.
    target_zone='living' 帶進去, 應顯示 Structured target zone line.
    """
    print("\n[case I] 無窗房間 + 中段擺位 directive → USER PHOTO DIRECTIVE, 不含靠窗詞")
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
        target_zone="living",
        target_note="客廳擺中段, 沒有窗",
    )
    prompt = out.get("prompt") or ""
    _assert_contains(prompt, "USER PHOTO DIRECTIVE", "PHOTO DIRECTIVE header")
    _assert_contains(prompt, "客廳擺中段, 沒有窗", "note 原文逐字")
    # PHOTO TARGET 段 (含 WINDOW-SIDE 強制詞) 不應出現
    if "PHOTO TARGET (user explicit intent" in prompt:
        raise AssertionError("PHOTO TARGET 段不應強制注入 (本 case 沒結構化 hint)")
    if "BACK / WINDOW-SIDE / DEEP" in prompt:
        raise AssertionError("PHOTO TARGET 的靠窗詞不應出現 — 用戶說了沒有窗")
    # Point 3: fallback layout 不應有「Anchor ... near the window for natural light」硬綁
    if "Anchor the living conversation zone near the window for natural light" in prompt:
        raise AssertionError("fallback layout 仍有 c08042a 之前的硬綁靠窗句, Point 3 沒生效")
    # fallback layout 改成條件式: 若有窗才 prefer
    _assert_contains(prompt, "If a main window is visible",
                     "Point 3: fallback layout 改條件式")
    # Point 4: target_zone 帶進 PHOTO DIRECTIVE
    _assert_contains(prompt, "Structured target zone selected by user",
                     "Point 4: target_zone context line")
    # 還是要明示「不能覆蓋結構保留」
    _assert_contains(prompt, "Structural preservation", "結構保留鐵則")


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

    # H5: render PhotoMeta 選取不應只看 best_photo。
    # best_photo 是餐廳角度且沒有 note, 另一張 living photo 有「客廳靠窗」時,
    # render 必須吃 living note, 並升格成 rear_near_window。
    from api import _select_render_photo_meta
    meta = {
        "uploads/U/photo_01.jpg": {
            "photo_key": "uploads/U/photo_01.jpg",
            "photo_contains": ["dining"],
            "target_zone": "dining",
            "target_location_hint": "unspecified",
            "target_note": "",
        },
        "uploads/U/photo_02.jpg": {
            "photo_key": "uploads/U/photo_02.jpg",
            "photo_contains": ["living", "dining"],
            "target_zone": "living",
            "target_location_hint": "unspecified",
            "target_note": "客廳靠窗",
        },
    }
    tz, hint, note, idx = _select_render_photo_meta(
        meta,
        ["C:/tmp/job/photo_01.jpg", "C:/tmp/job/photo_02.jpg"],
        {"best_photo_index": 0},
    )
    assert (tz, hint, note, idx) == ("living", "rear_near_window", "客廳靠窗", 1), (
        f"H5 render meta selection wrong: {(tz, hint, note, idx)!r}"
    )
    print("  PASS  H5 非 best_photo 的「客廳靠窗」會進 render 並升格靠窗 hint")

    # H6: 客人只寫自然語言「客廳靠窗 中段是餐廳」,
    # 後端要轉成 zoning contract, 不要求客人寫 prompt-engineering 句子。
    from api import _apply_target_note_layout_constraints
    zoning = {
        "zones": {
            "living_zone": {"where": "圖 1 中間偏右的木地板區域"},
            "dining_zone": {"where": ""},
        },
        "furniture_placement_rules": {
            "no_large_furniture_zones": [],
        },
    }
    out = _apply_target_note_layout_constraints(
        zoning,
        "客廳靠窗 中段是餐廳",
        "living",
        "rear_near_window",
    )
    assert "客廳靠窗端" in out["zones"]["living_zone"]["where"]
    assert "餐廳位於空間中段" in out["zones"]["dining_zone"]["where"]
    assert any("中段餐廳區" in x for x in out["furniture_placement_rules"]["no_large_furniture_zones"])
    print("  PASS  H6 自然語言 note 轉成靠窗客廳 + 中段餐廳硬規則")


def case_sofa_wall_rule_overrides_window_side_depth():
    """
    Hotfix 2026-06-19:
    Window-side / back-end living zone should control depth only.
    If zoning provides sofa_wall, that rule must control sofa back-wall / facing.
    """
    print("\n[case J] window-side living zone + explicit sofa_wall -> sofa_wall controls direction")
    zoning = {
        "confidence": "high",
        "_origin": "user_confirmed_v2",
        "_layout_choice": "A",
        "zones": {
            "living_zone": {"where": "空間最深處靠窗區域"},
            "walkway": {"where": "貫穿室內的中心軸線"},
        },
        "spatial_synthesis": {
            "room_shape": "長型矩形格局",
            "main_window_wall": "位於長型空間的最深處",
        },
        "furniture_placement_rules": {
            "sofa_wall": "沙發靠左側牆面擺放",
            "tv_wall": "電視牆可設於右側實牆",
            "coffee_table_position": "in front of the sofa",
            "rug_anchor": "anchored under the coffee table",
        },
    }
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=zoning,
        room_image_url="https://example.test/room.jpg",
    )
    prompt = out.get("prompt") or ""
    _assert_contains(prompt, "Use the explicit Sofa wall rule",
                     "explicit sofa_wall binding wording")
    _assert_contains(prompt, "沙發靠左側牆面擺放", "sofa_wall text")
    _assert_contains(prompt, "not which wall the sofa must back onto",
                     "depth/area separated from sofa back-wall")
    _assert_contains(prompt, "MUST NOT be interpreted",
                     "window-side must not imply back-window placement")
    _assert_contains(prompt, "directly against the window wall",
                     "no sofa-back-against-window unless explicit")
    _assert_contains(prompt, "Use the explicit TV/focal wall rule",
                     "explicit focal wall binding wording")
    _assert_contains(prompt, "電視牆可設於右側實牆", "tv_wall text")


def case_sofa_wall_text_can_bind_focal_wall_when_tv_wall_empty():
    """
    Job 414BE77C regression:
    Some zoning outputs leave tv_wall empty but put "TV wall + opposite sofa"
    inside sofa_wall. The render prompt must still bind media console and sofa
    as a facing pair.
    """
    print("\n[case K] tv_wall empty + sofa_wall mentions TV wall -> focal pair binding")
    entry = _minimal_entry()
    entry["matched_furniture"] = [
        {
            "id": "media-console-test",
            "category_en": "media_console",
            "name_zh": "淺色奶油色電視櫃",
            "image_url": "https://example.test/media-console.jpg",
        },
    ]
    zoning = {
        "confidence": "high",
        "_origin": "user_confirmed_v2",
        "_layout_choice": "A",
        "zones": {
            "living_zone": {"where": "第一張照片右前方的寬敞區域，往第二張照片靠窗方向延伸"},
            "walkway": {"where": "保留左側動線"},
        },
        "spatial_synthesis": {
            "room_shape": "長型矩形格局",
            "main_window_wall": "空間底端",
        },
        "furniture_placement_rules": {
            "sofa_wall": "右側大白牆適合規劃為電視牆，對側擺放沙發",
            "tv_wall": "",
            "coffee_table_position": "in front of the sofa",
            "rug_anchor": "anchored under the coffee table",
        },
    }
    out = build_nano_banana_inputs(
        entry=entry,
        zoning=zoning,
        room_image_url="https://example.test/room.jpg",
    )
    prompt = out.get("prompt") or ""
    _assert_contains(prompt, "Use the explicit TV/focal wall rule",
                     "focal wall inferred from sofa_wall text")
    _assert_contains(prompt, "右側大白牆適合規劃為電視牆，對側擺放沙發",
                     "overloaded sofa_wall text preserved")
    _assert_contains(prompt, "SOFA-FOCAL PAIRING",
                     "sofa/media console facing-pair rule")
    _assert_contains(prompt, "MUST NOT sit on the same wall",
                     "no same-wall/same-side focal pairing")
    _assert_contains(prompt, "MEDIA CONSOLE",
                     "media console product ref present")
    _assert_contains(prompt, "directly opposite the sofa",
                     "media console opposite sofa placement")


def case_dining_middle_note_tightens_window_side_depth():
    """
    Regression: user note "客廳靠窗 中段是餐廳" must not allow the living group
    to park around the middle boundary.
    """
    print("\n[case K2] dining middle note -> stricter window-side depth target")
    zoning = {
        "confidence": "high",
        "_origin": "user_confirmed_v2",
        "_layout_choice": "A",
        "zones": {
            "living_zone": {"where": "使用者補充指定：客廳靠窗端／窗邊後段。"},
            "dining_zone": {"where": "使用者補充指定：餐廳位於空間中段。"},
            "walkway": {"where": "保留左側走道"},
        },
        "spatial_synthesis": {
            "room_shape": "長型矩形格局",
            "main_window_wall": "空間底端",
        },
        "furniture_placement_rules": {
            "sofa_wall": "",
            "tv_wall": "",
            "no_large_furniture_zones": [
                "空間中段餐廳區需保留給餐桌與通行；沙發、客廳地毯、茶几、電視櫃等大型客廳家具不得佔用此中段餐廳區。"
            ],
        },
    }
    out = build_nano_banana_inputs(
        entry=_minimal_entry(),
        zoning=zoning,
        room_image_url="https://example.test/room.jpg",
        target_zone="living",
        target_location_hint="rear_near_window",
        target_note="客廳靠窗 中段是餐廳",
    )
    prompt = out.get("prompt") or ""
    _assert_contains(prompt, "depth >= 75%", "sofa depth tightened to back 25%")
    _assert_contains(prompt, "depth >= 60%", "focal anchor depth tightened")
    _assert_contains(prompt, "middle zone for dining", "dining middle clause")
    _assert_contains(prompt, "中段餐廳區", "no-large-furniture dining middle zone")


def case_soft_furnishing_product_refs_limited_to_three():
    """
    Soft furnishings should behave like product references, but only 2-3 per render.
    Full soft_furnishing[] can still feed result URLs; render refs stay capped.
    """
    print("\n[case L] soft furnishing product refs -> max 3 refs, categorized")
    entry = _minimal_entry()
    entry["soft_furnishing"] = [
        {
            "id": "soft-curtain-1",
            "category_en": "curtain",
            "name_zh": "純色窗簾",
            "image_url": "https://example.test/curtain.jpg",
        },
        {
            "id": "soft-lighting-1",
            "category_en": "lighting",
            "name_zh": "桌燈",
            "image_url": "https://example.test/lamp.jpg",
        },
        {
            "id": "soft-wall-art-1",
            "category_en": "wall_art",
            "name_zh": "壁掛畫框",
            "image_url": "https://example.test/wall-art.jpg",
        },
        {
            "id": "soft-plant-1",
            "category_en": "plant",
            "name_zh": "盆栽",
            "image_url": "https://example.test/plant.jpg",
        },
        {
            "id": "soft-vase-1",
            "category_en": "vase",
            "name_zh": "花器",
            "image_url": "https://example.test/vase.jpg",
        },
    ]
    out = build_nano_banana_inputs(
        entry=entry,
        zoning=_minimal_zoning(),
        room_image_url="https://example.test/room.jpg",
    )
    prompt = out.get("prompt") or ""
    soft_refs = [r for r in out["reference_map"] if r.get("kind") == "SOFT"]
    assert len(soft_refs) == 3, f"expected 3 soft refs, got {len(soft_refs)}"
    assert [r.get("cat_en") for r in soft_refs] == ["curtain", "lighting", "wall_art"], soft_refs
    assert len(out["image_urls"]) == 4, f"room + 3 soft refs expected, got {len(out['image_urls'])}"
    _assert_contains(prompt, "SOFT FURNISHING PRODUCT REFERENCES",
                     "soft product refs header")
    _assert_contains(prompt, "Reference image 2 is the CURTAIN SOFT FURNISHING PRODUCT",
                     "curtain input classification")
    _assert_contains(prompt, "Reference image 3 is the LAMP SOFT FURNISHING PRODUCT",
                     "lighting input classification")
    _assert_contains(prompt, "Reference image 4 is the WALL ART SOFT FURNISHING PRODUCT",
                     "wall_art input classification")
    _assert_contains(prompt, "Use reference image 2", "soft ref placement line")
    _assert_contains(prompt, "must remain secondary accents",
                     "soft refs stay secondary to main furniture")


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
    case_photo_directive_no_window_scenario()
    case_note_empty_string_skips()
    case_backend_normalize_target_note()
    case_sofa_wall_rule_overrides_window_side_depth()
    case_sofa_wall_text_can_bind_focal_wall_when_tv_wall_empty()
    case_dining_middle_note_tightens_window_side_depth()
    case_soft_furnishing_product_refs_limited_to_three()
    print("\nALL PASS")


if __name__ == "__main__":
    main()

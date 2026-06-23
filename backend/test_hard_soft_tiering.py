"""
test_hard_soft_tiering.py — 硬傷/軟傷分級回歸測試 (2026-06-21).

不打 fal / Gemini。驗證產品決策：
  - 硬傷（結構/動線/錯邊/錯區/背窗/完全沒對向）→ 不交付、要重生。
  - 軟傷（深度小偏差、茶几略偏、軟裝不齊）→ 照交付、不重生。
"""

import sys
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    assert cond, f"{name}: {detail}"


def case_a_hard_fail_flags():
    print("[case A] HARD_FAIL_FLAGS 涵蓋核心硬傷、不含軟傷")
    from gemini_analyze import HARD_FAIL_FLAGS
    must_hard = [
        "kitchen_added", "walls_changed", "windows_changed",
        "furniture_blocks_walkway", "sofa_faces_walkway",
        "sofa_on_wrong_side", "sofa_outside_living_zone",
        "sofa_back_against_window", "focal_anchor_misaligned_with_sofa",
    ]
    for f in must_hard:
        _check(f"硬傷含 {f}", f in HARD_FAIL_FLAGS)
    # 軟傷標記不該在硬傷清單
    _check("soft_issues 不算硬傷", "soft_issues" not in HARD_FAIL_FLAGS)
    _check("sofa_depth_percent 不算硬傷", "sofa_depth_percent_estimate" not in HARD_FAIL_FLAGS)


def case_b_retry_only_on_hard():
    print("[case B] z3_needs_retry 只在硬傷重生")
    from api import z3_needs_retry

    should, _ = z3_needs_retry({"hard_fail": True, "ok": False, "reason": "封住房門"})
    _check("硬傷 → 重生", should is True)

    should2, _ = z3_needs_retry({"hard_fail": False, "ok": False,
                                 "soft_issues": ["sofa 深度 55% 略低"]})
    _check("軟傷(ok=False) → 不重生", should2 is False)

    should3, _ = z3_needs_retry({"hard_fail": False, "ok": True})
    _check("通過 → 不重生", should3 is False)

    should4, _ = z3_needs_retry({"sofa_on_wrong_side": True, "hard_fail": True,
                                 "ok": False, "reason": "錯邊"})
    _check("錯邊(硬傷) → 重生", should4 is True)


def case_c_delivery_uses_hard_fail():
    print("[case C] delivery gate 用 hard_fail 判定（源碼）")
    import api
    src = inspect.getsource(api.run_pipeline)
    _check("delivery 用 _is_hard_fail",
           "_is_hard_fail" in src and "delivery_final = [r for r in final if not _is_hard_fail(r)]" in src)
    _check("hard_fail 來自 validation",
           '.get("hard_fail")' in src)
    # render 本身失敗（沒產出圖 / 有 error）也不可交付（修「生成中」卡死）
    _check("render 失敗也判不可交付",
           'r.get("error")' in src and "render_path" in src)


def case_d_phase2_hardfix():
    print("[case D] Phase 2 硬傷補生一次 + needs_regen（源碼）")
    import api
    src = inspect.getsource(api.run_pipeline)
    _check("有 Phase2 硬傷補生區塊", "Phase2 硬傷補生" in src or "phase2_hardfix" in src)
    _check("補生只針對 hard_fail", 'if not v.get("hard_fail"):' in src)
    _check("補生帶完整原因 retry_ctx", "_build_retry_ctx_from_validation(v)" in src)
    _check("仍硬傷記 needs_regen", '"needs_regen"' in src)


def case_e_strict_depth_only_on_position_note():
    print("[case E] 只有位置語意 note 才啟動嚴格深度，非位置 note 不啟動")
    from gemini_analyze import (
        _note_has_position_intent, _compute_strict_depth, _depth_classification,
    )

    # 位置語意偵測
    _check("『客廳靠窗、餐廳中段』含位置語意",
           _note_has_position_intent("客廳靠窗、餐廳中段") is True)
    _check("『喜歡淺木色』非位置語意",
           _note_has_position_intent("喜歡淺木色") is False)
    _check("『不要紅色』非位置語意",
           _note_has_position_intent("不要紅色") is False)
    _check("空 note 非位置語意", _note_has_position_intent("") is False)

    # strict_depth 啟動條件
    _check("位置 note → strict",
           _compute_strict_depth("客廳靠窗、餐廳中段", "unspecified", True) is True)
    _check("非位置 note 單獨不啟動 strict",
           _compute_strict_depth("喜歡淺木色", "unspecified", False) is False)
    _check("rear_near_window hint → strict",
           _compute_strict_depth("", "rear_near_window", False) is True)

    # 回歸 1：客廳靠窗、餐廳中段 + 深度 65%（門檻 75/80）→ hard
    _check("客廳靠窗+餐廳中段 深度65% → hard",
           _depth_classification(65, 75, 80, strict=True, qual_wrong=False) == "hard")
    # 回歸 2：喜歡淺木色（非位置, strict=False）同樣 65%（門檻 75/80）→ 不因 note 變 hard
    _check("喜歡淺木色 深度65% → 不 hard（soft）",
           _depth_classification(65, 75, 80, strict=False, qual_wrong=False) == "soft")
    # Codex 原始意圖：55 vs 60 小偏差（非嚴格）→ soft
    _check("55 vs 60 寬鬆 → soft",
           _depth_classification(55, 60, 65, strict=False, qual_wrong=False) == "soft")
    # 達標 → ok
    _check("達標 80% → ok",
           _depth_classification(82, 75, 80, strict=True, qual_wrong=False) == "ok")


def case_f_grounded_depth_from_bbox():
    print("[case F] 用 bbox 客觀算深度（取代 Gemini 自述百分比）")
    from gemini_analyze import _grounded_depth_pct, _depth_classification

    win = [50, 300, 150, 700]            # 窗在上端
    near = [180, 300, 320, 700]          # 沙發貼窗
    mid = [480, 300, 620, 700]           # 沙發中段
    _check("貼窗沙發 → 高深度(>75)", _grounded_depth_pct(near, win) > 75)
    _check("中段沙發 → 約 50", 45 <= _grounded_depth_pct(mid, win) <= 55)
    _check("缺窗 → None（退回自述）", _grounded_depth_pct(mid, None) is None)

    # 核心修復：Gemini 自述 72% 靠窗，但 bbox 量到中段 50% → 取較小 → 嚴格模式判 hard
    narrative = 72
    grounded = _grounded_depth_pct(mid, win)
    effective = min(narrative, grounded)
    _check("自述偏高被客觀量測壓回中段", effective <= 55, f"effective={effective}")
    # 餐廳在中段 → 硬門檻 75、strict → effective(50) < 70 → hard
    _check("壓回後 → hard fail（不再默默交付中段沙發）",
           _depth_classification(effective, 75, 80, strict=True, qual_wrong=False) == "hard")


def case_g_depth_unverified_retries_not_dropped():
    print("[case G] 明確位置案 bbox 缺失 → 重試、不靠自述放行、不 drop")
    import api
    from gemini_analyze import HARD_FAIL_FLAGS
    # bbox 缺失但明確位置案 → 需重試
    should, _ = api.z3_needs_retry({"sofa_depth_unverified": True, "hard_fail": False, "ok": True})
    _check("depth_unverified → 重試", should is True)
    # 但 depth_unverified 不是硬傷 → 交付閘門不會 drop（重試後仍量不到就帶標記交付）
    _check("depth_unverified 不在 HARD_FAIL_FLAGS（不被 drop）",
           "sofa_depth_unverified" not in HARD_FAIL_FLAGS)
    # 一般通過、無此旗標 → 不重試
    should2, _ = api.z3_needs_retry({"hard_fail": False, "ok": True})
    _check("正常通過 → 不重試", should2 is False)


def case_h_dining_middle_reasonable_floor():
    print("[case H] 客廳靠窗+餐廳中段 → <72 硬、72-80 軟交、>=80 理想（不死卡 80）")
    from gemini_analyze import _depth_classification
    # dining-middle 門檻：sofa_hard_floor=77, soft=80, strict grace=5 → 實際 <72 才硬
    _check("76.9% → soft（照交付，不再硬擋）",
           _depth_classification(76.9, 77, 80, strict=True, qual_wrong=False) == "soft")
    _check("73.9% → soft（照交付）",
           _depth_classification(73.9, 77, 80, strict=True, qual_wrong=False) == "soft")
    _check("70% → hard（明顯太靠前）",
           _depth_classification(70, 77, 80, strict=True, qual_wrong=False) == "hard")
    _check(">=80% → ok（達理想）",
           _depth_classification(82, 77, 80, strict=True, qual_wrong=False) == "ok")
    # 焦點 dining-middle: anchor_hard_floor=70 → 實際 <65 才硬
    _check("focal 55% → hard（太靠前）",
           _depth_classification(55, 70, 80, strict=True, qual_wrong=False) == "hard")
    _check("focal 70% → soft（照交付）",
           _depth_classification(70, 70, 80, strict=True, qual_wrong=False) == "soft")


def case_i_reason_keyword_blocks_delivery():
    print("[case I] 驗收理由點名電視櫃偏前/佔用餐廳/擋入口 → 強制硬傷（源碼）")
    import inspect
    from gemini_analyze import validate_render
    src = inspect.getsource(validate_render)
    _check("有理由關鍵字安全網", "_BLOCK_FOCAL_KW" in src and "_BLOCK_WALK_KW" in src)
    _check("命中焦點關鍵字 → focal_anchor_misaligned",
           'result["focal_anchor_misaligned_with_sofa"] = True' in src
           and "_BLOCK_FOCAL_KW" in src)
    _check("命中走道關鍵字 → furniture_blocks_walkway",
           'result["furniture_blocks_walkway"] = True' in src and "_BLOCK_WALK_KW" in src)
    _check("含『佔用餐廳』關鍵字", "佔用餐廳" in src)
    _check("含『擋入口』關鍵字", "擋入口" in src)


if __name__ == "__main__":
    case_a_hard_fail_flags()
    case_b_retry_only_on_hard()
    case_c_delivery_uses_hard_fail()
    case_d_phase2_hardfix()
    case_e_strict_depth_only_on_position_note()
    case_f_grounded_depth_from_bbox()
    case_g_depth_unverified_retries_not_dropped()
    case_h_dining_middle_reasonable_floor()
    case_i_reason_keyword_blocks_delivery()
    print("\nALL PASS")

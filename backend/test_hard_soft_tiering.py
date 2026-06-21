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


def case_d_phase2_hardfix():
    print("[case D] Phase 2 硬傷補生一次 + needs_regen（源碼）")
    import api
    src = inspect.getsource(api.run_pipeline)
    _check("有 Phase2 硬傷補生區塊", "Phase2 硬傷補生" in src or "phase2_hardfix" in src)
    _check("補生只針對 hard_fail", 'if not v.get("hard_fail"):' in src)
    _check("補生帶完整原因 retry_ctx", "_build_retry_ctx_from_validation(v)" in src)
    _check("仍硬傷記 needs_regen", '"needs_regen"' in src)


if __name__ == "__main__":
    case_a_hard_fail_flags()
    case_b_retry_only_on_hard()
    case_c_delivery_uses_hard_fail()
    case_d_phase2_hardfix()
    print("\nALL PASS")

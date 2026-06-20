"""
test_partial_delivery_retry.py — 部分交付 + 重試回饋回歸測試 (2026-06-21).

不打 fal / Gemini / 不跑新圖。覆蓋兩個 2026-06-21 修正:

  [A] prompt_builder._build_retry_context_section
      重試 prompt 必須帶上次 validation 的具體失敗 flag (沙發背窗 / 侵入走道 …)
      與 reason，而不是只有深度數字。

  [B] api.py delivery gate (部分交付) — 用源碼斷言確認:
      - 不再「任一風格失敗就整單 raise」
      - 只有「全部失敗 (not delivery_final)」才 raise
      - 被移除的 style 記進 validation_summary.dropped_renders
      - C2.6 anchored 第二道整單 raise gate 已移除
"""

import sys
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from prompt_builder import _build_retry_context_section  # noqa: E402


def _check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    assert cond, f"{name}: {detail}"


def case_a_retry_carries_failure_flags():
    print("[case A] 重試帶具體失敗 flag + reason")
    s = _build_retry_context_section({
        "failed_flags": ["sofa_back_against_window", "sofa_intrudes_walkway"],
        "sofa_pct": 40,
    })
    _check("header 明示要修正", "FIX THESE EXACT PROBLEMS" in s, s)
    _check("帶沙發背窗修正", "sofa back to the window" in s, s)
    _check("帶侵入走道修正", "clear of the main" in s, s)
    _check("仍帶深度數字", "estimated at 40%" in s, s)

    # 沒有結構化 flag 時, 至少帶 reason
    s2 = _build_retry_context_section({"reason": "沙發未貼長牆"})
    _check("reason fallback", "Reviewer note" in s2 and "沙發未貼長牆" in s2, s2)

    # 完全沒回饋 → 空字串 (向後相容)
    _check("空 ctx → 空字串", _build_retry_context_section({}) == "")
    _check("None → 空字串", _build_retry_context_section(None) == "")

    # 重複 flag 去重
    s4 = _build_retry_context_section({
        "failed_flags": ["sofa_intrudes_walkway", "sofa_intrudes_walkway"],
    })
    _check("重複 flag 去重", s4.count("clear of the main") == 1, s4)


def case_b_delivery_gate_partial():
    print("[case B] delivery gate 為部分交付 (源碼斷言)")
    import api
    src = inspect.getsource(api.run_pipeline)

    # 全部失敗才 raise
    _check("全部失敗才 raise",
           "if not delivery_final:" in src and
           "all renders failed validation after retries" in src, src[:0])

    # 不再有「one or more renders failed → raise」整單失敗
    _check("移除 one-or-more raise",
           "one or more renders failed validation after retries" not in src)

    # 被移除 style 記進 summary 給前端
    _check("記錄 dropped_renders",
           'validation_summary["dropped_renders"]' in src or
           "validation_summary['dropped_renders']" in src, src[:0])

    # C2.6 anchored 第二道整單 raise gate 已移除
    _check("移除 anchored 第二道 raise",
           "anchored validation failed on" not in src)

    # 重試一律帶回饋 (移除 current_rc>=1 條件)
    _check("重試一律帶回饋",
           "_build_retry_ctx_from_validation(v) if current_rc >= 1 else None" not in src)


if __name__ == "__main__":
    case_a_retry_carries_failure_flags()
    case_b_delivery_gate_partial()
    print("\nALL PASS")

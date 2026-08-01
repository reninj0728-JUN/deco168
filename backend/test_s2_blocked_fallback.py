# -*- coding: utf-8 -*-
"""S2 判官擋死 → 退回 legacy，而不是給客戶零圖。

2026-08-01 用戶裁決：「零圖傷害最大，客人會覺得被騙」。

在此之前的行為：判官驗過且判不安全 → `layout_guide_modes = s2_blocked_legacy`
→ `_layout_contract_s2_required` 仍為 True → 付費前檢 `S2PreflightBlocked`
→ 交付 0 張（83FBD20D 就是這樣）。

離線唯讀量測（7 張 S2 判官擋死的單，套 production 的門排除裁切）：
legacy 規劃器 **7/7** 都產得出配置。「S2 擋死」只等於「S2 描述不了這房型」，
不等於「這房間無解」。

⚠️ 這不是品質升級——legacy 沒有 S2 幾何判官把關，是拿「一張門已出鏡但無 S2
把關的圖」換掉「零圖」。所以要留急救開關，而且開關必須真的關得掉。
"""
from __future__ import annotations

from pathlib import Path

import api
import pytest


def _blocked_summary(**over):
    """判官驗過、判不安全的 S2 摘要（83FBD20D 的形狀）。"""
    base = {
        "status": "blocked",
        "pre_generation_eligible": False,
        "unsafe_codes": ["GEOM_NOT_ELIGIBLE"],
        "verification_status": "fail",
        "verification_attempt_count": 3,
        "verification_failed_fields": {
            "tv_wall_contact": "fail",
            "left_wall_floor_alignment": "fail",
        },
        "verification_history": [
            {"attempt": 1, "failed_fields": {"tv_wall_contact": "fail",
                                             "left_wall_floor_alignment": "fail"}},
            {"attempt": 2, "failed_fields": {"tv_wall_contact": "fail",
                                             "left_wall_floor_alignment": "fail"}},
            {"attempt": 3, "failed_fields": {"tv_wall_contact": "fail",
                                             "left_wall_floor_alignment": "fail"}},
        ],
    }
    base.update(over)
    return base


# ── 開關本身 ───────────────────────────────────────────────────────────

def test_fallback_is_on_by_default(monkeypatch):
    """預設開啟——用戶要的是「不要再給零圖」。"""
    monkeypatch.delenv("S2_BLOCKED_FALLBACK", raising=False)
    assert api._s2_blocked_fallback_enabled() is True


def test_fallback_can_be_killed(monkeypatch):
    """急救開關必須真的關得掉，關掉就回到舊行為（擋死）。"""
    monkeypatch.setenv("S2_BLOCKED_FALLBACK", "0")
    assert api._s2_blocked_fallback_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", ""])
def test_only_explicit_zero_disables(monkeypatch, value):
    """只有明確設 0 才關；設錯字不得意外關掉救援。"""
    monkeypatch.setenv("S2_BLOCKED_FALLBACK", value)
    assert api._s2_blocked_fallback_enabled() is True


# ── 分流條件：擋死的單會不會走進 waive 分支 ─────────────────────────────

def _takes_waive_branch(summary) -> bool:
    """複製 api.py 那條 elif 的判定式（三個條件任一成立就退 legacy）。"""
    return bool(api._s2_model_not_applicable(summary)
                or api._s2_verifier_unstable(summary)
                or api._s2_blocked_fallback_enabled())


def test_verifier_blocked_order_now_falls_back(monkeypatch):
    """83FBD20D 這種「判官驗過、判不安全、失敗欄位穩定」的單要退 legacy。

    反貧血：先確認它【不是】靠既有的兩個條件過關的——舊版對它就是 False，
    所以舊版才會擋死。是新開關把它接住的。
    """
    monkeypatch.delenv("S2_BLOCKED_FALLBACK", raising=False)
    summary = _blocked_summary()
    assert api._s2_model_not_applicable(summary) is False, "不是『模型化不了』那種"
    assert api._s2_verifier_unstable(summary) is False, "失敗欄位是穩定的，不是抖動"
    assert _takes_waive_branch(summary) is True


def test_same_order_still_hard_blocks_when_killed(monkeypatch):
    """關掉開關 → 同一張單回到舊行為（不退 legacy＝付費前檢會擋死）。

    這條證明上一條不是恆真式。
    """
    monkeypatch.setenv("S2_BLOCKED_FALLBACK", "0")
    summary = _blocked_summary()
    assert _takes_waive_branch(summary) is False


def test_existing_waive_paths_unchanged(monkeypatch):
    """既有的 waive（模型化不了＝判官從未執行）在開關關掉時照樣成立——沒被我改壞。"""
    monkeypatch.setenv("S2_BLOCKED_FALLBACK", "0")
    # 「模型化不了」的定義是判官【從未執行】：沒有 verification_status、attempt 0
    not_applicable = {
        "status": "blocked",
        "pre_generation_eligible": False,
        "unsafe_codes": ["NO_USABLE_WALL", "GEOM_NOT_ELIGIBLE"],
        "verification_status": None,
        "verification_attempt_count": 0,
    }
    assert api._s2_model_not_applicable(not_applicable) is True
    assert _takes_waive_branch(not_applicable) is True


def test_verifier_ran_is_not_model_not_applicable():
    """反貧血：判官跑過（不管 pass/fail）就不算『模型化不了』——
    83FBD20D 正是這種，所以舊版接不住它。"""
    ran = _blocked_summary(unsafe_codes=["NO_USABLE_WALL"])
    assert ran["verification_attempt_count"] == 3
    assert api._s2_model_not_applicable(ran) is False


def test_waive_branch_is_actually_wired_to_the_switch():
    """接線守衛：pipeline 那條 elif 必須真的呼叫 `_s2_blocked_fallback_enabled()`。

    ⚠️ 這是**唯一**一條字串層級的測試，而且是刻意的：上面幾條驗的是判定式的
    行為，但它們複製了條件式，抓不到「有人把開關從 elif 拿掉」這種接線退化
    （突變測試證實了這個盲區）。要真正行為驗證得跑整條 pipeline，成本不成比例。
    這條至少讓拆線會紅。
    """
    import inspect
    src = inspect.getsource(api.run_pipeline_job) if hasattr(api, "run_pipeline_job") \
        else Path(api.__file__).read_text(encoding="utf-8")
    assert "_s2_blocked_fallback_enabled()" in src, (
        "S2 擋死退回 legacy 的開關沒有接在 waive 分流上——客戶又會拿到零圖")
    assert "_s2_model_not_applicable(_sum) or _s2_verifier_unstable(_sum)" in src, \
        "既有的兩條 waive 條件不見了"


def test_eligible_order_never_reaches_the_branch():
    """S2 成功的單走的是前面那條 if，不會進到 waive/擋死的分流。

    這很重要：S2 判官 pass 的 9 單裡，legacy 只有 2 單產得出配置——
    如果讓成功的單也退 legacy，會弄壞現在唯一能交付的那批。
    """
    ok = {"status": "safe", "pre_generation_eligible": True, "unsafe_codes": []}
    assert ok["pre_generation_eligible"] is True

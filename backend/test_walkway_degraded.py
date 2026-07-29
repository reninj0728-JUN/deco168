# -*- coding: utf-8 -*-
"""走道多邊形畫壞時的容錯（B4CC27C3 根因第二層）。

Gemini 把走道畫成細楔形（前三點近乎共線、第四點跑到左下角），面積只有地板 4%
（正常房 19%），連通演算法穿不過去 → 13 個候選全被判 CANDIDATE_HITS_WALKWAY，
看起來像「家具擋路」，實際上**零家具就已經不通**。

修法：零家具連通測試判定走道可不可用（不用面積門檻——門檻是猜的，而且分不出
「畫壞的走道」和「真的很窄但能走的走道」）；不可用時改用客廳地板當通道區，
同一套連通檢查照跑。
"""
import json
from pathlib import Path

import pytest

import layout_geometry_s2 as s2

BAD = Path(__file__).parent / "fixtures" / "_walkway_degraded_geom.json"      # B4CC27C3
GOOD = Path(__file__).parent / "fixtures" / "_planner_door_gap_geom.json"     # 71DC312E
W, H = 4032, 3024


def _px(raw):
    return [(p[1] * W / 1000.0, p[0] * H / 1000.0) for p in raw]


def _walkway(fixture):
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    return _px(raw["elements"]["walkway"]["polygon_yx1000"])


@pytest.mark.skipif(not (BAD.exists() and GOOD.exists()), reason="缺 fixture")
def test_zero_furniture_test_separates_broken_from_narrow():
    """判定用「零家具時通不通」，不是面積門檻。
    畫壞的走道零家具就不通；正常走道零家具會通。"""
    assert s2._walkway_polygon_is_usable(_walkway(BAD), width=W, height=H) is False
    assert s2._walkway_polygon_is_usable(_walkway(GOOD), width=W, height=H) is True


@pytest.mark.skipif(not BAD.exists(), reason="缺 fixture")
def test_degraded_walkway_room_becomes_designable():
    """B4CC27C3：修復前 0 合格候選（S2 全擋→退 legacy→畫出擋門圖）。
    走道退化改用地板當通道後，這房其實設計得出來。"""
    raw = json.loads(BAD.read_text(encoding="utf-8"))
    for side in ("free", "left"):
        plan = s2.build_s2_plan(raw, width=W, height=H, expected_source_photo_index=0,
                                sofa_side=side, can_float=False)
        eligible = [c for c in (plan.get("candidates") or []) if c.get("eligible")]
        assert eligible, f"sofa_side={side}：走道退化容錯後應找得到合格配置"
        assert not plan.get("unsafe_codes"), f"sofa_side={side} 不該再整包封鎖"


@pytest.mark.skipif(not BAD.exists(), reason="缺 fixture")
def test_fail_code_says_walkway_data_is_degraded_not_furniture():
    """碼名不得誤導：走道資料壞掉時要講明白，否則會害人去修家具
    （昨天的教訓：有數字還要確認數字的因果方向）。"""
    raw = json.loads(BAD.read_text(encoding="utf-8"))
    plan = s2.build_s2_plan(raw, width=W, height=H, expected_source_photo_index=0,
                            sofa_side="free", can_float=False)
    codes = [f for c in (plan.get("candidates") or []) for f in (c.get("fail_codes") or [])]
    blocked = [f for f in codes if f.startswith("CANDIDATE_BLOCKS_FLOOR_PATH")]
    assert blocked, "走道退化時應使用可自我說明的碼"
    assert "walkway_polygon_degraded" in blocked[0]
    assert not any(f == "CANDIDATE_HITS_WALKWAY" for f in codes), \
        "走道資料壞掉時不得再回報成『家具撞走道』"


@pytest.mark.skipif(not GOOD.exists(), reason="缺 fixture")
def test_good_walkway_room_is_untouched():
    """回歸：走道正常的房完全不受影響（候選數、合格數、選中候選皆不變）。"""
    raw = json.loads(GOOD.read_text(encoding="utf-8"))
    # 2026-07-29 golden 更新：候選比較鍵改成「先比門距安全餘裕、再比原分數」
    # （1F24858B：規劃器選了 0.449 門寬的壓線候選，同批有更寬鬆的沒選，出圖後
    # 掉到驗收門檻以下）。候選數與合格數皆未變——只有 free 模式選中的候選變成
    # 餘裕較大的那個，這正是本次刻意的行為變更。左右綁邊的結果不受影響。
    baseline = {"free": (13, 7, "s2_a_right_0.740_1.000"),
                "left": (5, 1, "s2_b_left_0.555_0.815"),
                "right": (4, 2, "s2_a_right_0.740_1.000")}
    for side, (nc, ne, chosen) in baseline.items():
        plan = s2.build_s2_plan(raw, width=W, height=H, expected_source_photo_index=0,
                                sofa_side=side, can_float=False)
        cands = plan.get("candidates") or []
        assert len(cands) == nc, f"{side}: 候選數量變了"
        assert len([c for c in cands if c.get("eligible")]) == ne, f"{side}: 合格數變了"
        assert plan.get("chosen_candidate_id") == chosen, f"{side}: 選中候選變了"
        eligible_list = [c for c in cands if c.get("eligible")]
        # 釘規則而不是釘快照：選中的候選必須是「同一個候選池裡」餘裕最大的。
        # 候選池要跟選擇時一致——can_float=False 時浮動 F 已被浮動守門排除，
        # 拿 F 來比會得到假的最大值。fixture 換掉也不會讓這條失去意義。
        if eligible_list and plan.get("chosen_candidate_id"):
            pool = [c for c in eligible_list if c.get("candidate_type") in ("A", "B")]                 or eligible_list
            picked = next(c for c in eligible_list
                          if c["candidate_id"] == plan["chosen_candidate_id"])
            best_margin = max(c.get("door_gap_margin") or 0.0 for c in pool)
            assert (picked.get("door_gap_margin") or 0.0) == best_margin, (
                f"{side}: 選中的候選餘裕不是同池最大 "
                f"({picked.get('door_gap_margin')} < {best_margin})")

        assert not any("FLOOR_PATH" in f for c in cands for f in (c.get("fail_codes") or [])), \
            "走道正常時不該走 fallback"

# -*- coding: utf-8 -*-
"""規劃器的門距標準必須與生成後驗證器一致（67866097 根因）。

舊 bug 兩層：
  ① `sofa_side != "free" or clearance >= min` —— 沙發指定左/右時門距檢查整個跳過。
     指定左右只能決定「靠哪面牆」，不能豁免門距。
  ② 就算檢查，規劃器用 `0.03 × 圖片短邊`(≈91px)，驗證器要 `0.25/0.28 × 門寬`
     (139～201px)，寬鬆 1.5～2.2 倍 → 規劃出來的目標框本來就過不了自家驗收，
     Fal 照著錯誤目標畫，再被後段閘門擋掉（客廳落選 65% 是「家具擋門」）。
"""
import inspect
import json
from pathlib import Path

import pytest

import layout_geometry_s2 as s2

FIXTURE = Path(__file__).parent / "fixtures" / "_planner_door_gap_geom.json"


def test_planner_and_validator_share_one_door_gap_method():
    """必須直接呼叫驗證器的同一個函式，不得各自複製一份（會漂移）。"""
    src = inspect.getsource(s2._shared_door_gap_violation)
    assert "_door_adjacency_violation" in src, "規劃器必須共用驗證器的門距判定"
    assert "0.25" not in src and "0.28" not in src, "門檻不得在規劃器複製，必須來自驗證器"


def test_bound_sofa_side_no_longer_bypasses_door_gap():
    """① 指定左/右不得豁免門距檢查。"""
    src = inspect.getsource(s2._candidate)
    assert 'sofa_side != "free" or entrance_clearance' not in src, \
        "指定左右跳過門距的漏洞必須移除"
    assert "door_gap_clear" in src, "門距必須成為候選 invariant"


def test_door_gap_uses_validator_thresholds_for_both_furniture():
    """② 沙發 0.25、電視櫃 0.28，兩者都要檢查（不能只檢查沙發）。"""
    from gemini_analyze import DOOR_GAP_MIN_FOCAL, DOOR_GAP_MIN_SOFA
    W = H = 1000
    door = [(100, 300), (240, 300), (240, 700), (100, 700)]       # 門寬 140
    far = [(700, 300), (900, 300), (900, 700), (700, 700)]         # 遠離門
    # 沙發貼門（gap=0）→ 必須抓到，且門檻用 0.25
    jam = [(245, 300), (400, 300), (400, 700), (245, 700)]
    v = s2._shared_door_gap_violation(jam, far, door, W, H)
    assert v is not None and v[0] == "sofa" and v[3] == DOOR_GAP_MIN_SOFA
    # 電視櫃貼門 → 必須抓到，門檻用 0.28
    v = s2._shared_door_gap_violation(far, jam, door, W, H)
    assert v is not None and v[0] == "focal_anchor" and v[3] == DOOR_GAP_MIN_FOCAL
    # 兩者都離門夠遠 → 放行
    assert s2._shared_door_gap_violation(far, far, door, W, H) is None


@pytest.mark.skipif(not FIXTURE.exists(), reason="缺真實幾何 fixture")
def test_real_room_rejects_zero_gap_candidates_but_still_has_options():
    """真實房型（71DC312E）：門距為 0 的候選必須淘汰，但不得全滅——
    指定左/右仍要能沿該牆找到安全位置，否則等於砍掉客戶的指定功能。"""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seen_gap_reject = False
    for side in ("free", "left", "right"):
        plan = s2.build_s2_plan(raw, width=4032, height=3024,
                                expected_source_photo_index=0,
                                sofa_side=side, can_float=False)
        cands = plan.get("candidates") or []
        eligible = [c for c in cands if c.get("eligible")]
        assert eligible, f"sofa_side={side} 不得完全無解（會砍掉指定功能）"
        for c in cands:
            if any(str(f).startswith("CANDIDATE_DOOR_GAP_") for f in (c.get("fail_codes") or [])):
                seen_gap_reject = True
                assert not c.get("eligible"), "門距不合格的候選不得 eligible"
    assert seen_gap_reject, "真實房型應至少淘汰一個門距不合格的候選"

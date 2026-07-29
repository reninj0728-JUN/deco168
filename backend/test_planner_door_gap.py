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


# ── 診斷碼：landing 距離拒收必須留痕跡（純可觀測性，不改行為）──────────────
@pytest.mark.skipif(not FIXTURE.exists(), reason="缺真實幾何 fixture")
def test_landing_rejection_leaves_a_diagnostic_code():
    """landing 距離規則會透過 invariants 讓候選 eligible=False，但以前只有
    sofa_side=="free" 才記 FLOAT_NOT_PROVEN → 指定 left/right 被它刷掉時
    完全不留痕跡，會把「兩層規則過度封鎖」誤判成「房型無解」。"""
    import copy
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    g = copy.deepcopy(raw)
    # 玄關落塵區放大到深入室內 → 靠牆候選落在門檻內
    g["elements"]["entrance_landing"]["polygon_yx1000"] = [
        [500, 80], [500, 600], [990, 700], [990, 0]]
    for side in ("left", "right"):
        plan = s2.build_s2_plan(g, width=4032, height=3024,
                                expected_source_photo_index=0,
                                sofa_side=side, can_float=False)
        cands = plan.get("candidates") or []
        tagged = [c for c in cands
                  if any("CANDIDATE_NEAR_LANDING" in f for f in (c.get("fail_codes") or []))]
        assert tagged, f"sofa_side={side}：被 landing 規則刷掉時必須留下診斷碼"
        for c in tagged:
            assert not c.get("eligible"), "帶此碼的候選本來就不合格"


@pytest.mark.skipif(not FIXTURE.exists(), reason="缺真實幾何 fixture")
def test_diagnostic_code_changes_no_behaviour():
    """驗收條件：候選數量、合格數、最終選定候選都不得改變（診斷碼在 score 算完後才附加）。
    基準取自加碼前的實測值。"""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # 2026-07-29 golden 更新：候選比較鍵改成「先比門距安全餘裕、再比原分數」
    # （1F24858B：規劃器選了 0.449 門寬的壓線候選，同批有更寬鬆的沒選，出圖後
    # 掉到驗收門檻以下）。候選數與合格數皆未變——只有 free 模式選中的候選變成
    # 餘裕較大的那個，這正是本次刻意的行為變更。左右綁邊的結果不受影響。
    baseline = {"free": (13, 7, "s2_a_right_0.740_1.000"),
                "left": (5, 1, "s2_b_left_0.555_0.815"),
                "right": (4, 2, "s2_a_right_0.740_1.000")}
    for side, (n_cand, n_elig, chosen_id) in baseline.items():
        plan = s2.build_s2_plan(raw, width=4032, height=3024,
                                expected_source_photo_index=0,
                                sofa_side=side, can_float=False)
        cands = plan.get("candidates") or []
        eligible = [c for c in cands if c.get("eligible")]
        assert len(cands) == n_cand, f"{side}: 候選數量變了"
        assert len(eligible) == n_elig, f"{side}: 合格數量變了"
        assert plan.get("chosen_candidate_id") == chosen_id, f"{side}: 選中的候選變了"
        eligible_list = eligible
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

        # 合格候選不得帶此碼
        for c in eligible:
            assert not any("CANDIDATE_NEAR_LANDING" in f for f in (c.get("fail_codes") or []))


# ── 直接測門距餘裕公式本身（不是只驗排序服從欄位）──────────────────────
def _rect(x0, y0, x1, y1):
    """畫素座標的矩形 footprint。"""
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


W_PX, H_PX = 1000, 1000


def _margin(sofa, tv, door):
    return s2._shared_door_gap_margin(sofa, tv, door, W_PX, H_PX)


def test_margin_is_normalised_by_each_items_own_threshold():
    """沙發門檻 0.25、電視櫃 0.28 不同——同樣的 x-gap 對兩者的安全程度不同。
    不各自除以自己的門檻，就不是同一個安全尺度（GPT 抓漏）。"""
    door = _rect(0, 0, 100, 900)          # 門寬 100px
    # 兩者與門的 x-gap 都是 30px → 原始 ratio 都是 0.30
    sofa = _rect(130, 100, 300, 800)
    tv = _rect(130, 100, 300, 800)
    margin = _margin(sofa, tv, door)
    assert margin is not None
    # 標準化後：沙發 0.30/0.25 = 1.20、電視櫃 0.30/0.28 ≈ 1.071 → 取較小者
    assert abs(margin - (0.30 / 0.28)) < 0.02, (
        f"應取電視櫃的 1.071（較嚴的那個），實得 {margin}")
    assert margin < 0.30 / 0.25, "沒有各自標準化：不該等於沙發的 1.20"


def test_items_without_vertical_overlap_do_not_drag_margin_down():
    """驗收只在「與門有垂直重疊」時才判門距。沒有重疊的家具不構成門距風險，
    不可拿它的 x-gap 把餘裕拉低——否則排序與驗收仍是兩把尺（GPT 抓漏）。"""
    door = _rect(0, 0, 100, 400)          # 門只佔畫面上半 y 0~400
    near_but_below = _rect(105, 600, 300, 900)   # x 很近，但 y 完全在門下方
    far_and_overlapping = _rect(400, 100, 600, 300)
    margin_pair = _margin(near_but_below, far_and_overlapping, door)
    margin_far_only = _margin(far_and_overlapping, far_and_overlapping, door)
    assert margin_pair is not None and margin_far_only is not None
    assert margin_pair >= margin_far_only, (
        f"沒有垂直重疊的家具把餘裕拉低了：{margin_pair} < {margin_far_only}")


def test_no_vertical_overlap_at_all_is_safe_not_unknown():
    """兩件都與門無垂直重疊＝驗收判定不相鄰＝安全；
    不可跟「量不到資料」一樣回 None（None 會被排序當成最差）。"""
    door = _rect(0, 0, 100, 300)
    below_a = _rect(200, 600, 400, 900)
    below_b = _rect(500, 600, 700, 900)
    margin = _margin(below_a, below_b, door)
    assert margin == s2.DOOR_GAP_MARGIN_NOT_ADJACENT, f"實得 {margin}"


def test_missing_door_evidence_is_unknown_not_safe():
    """門框證據無效（退化成零寬）→ None（不知道），呼叫端不得當成很安全。
    這跟「確定不相鄰」必須分得開——後者才是安全。"""
    degenerate_door = _rect(50, 100, 50, 400)     # 零寬，_valid_render_bbox 會擋掉
    margin = _margin(_rect(200, 100, 400, 300), _rect(500, 100, 700, 300),
                     degenerate_door)
    assert margin is None, f"量不到門框卻回了 {margin}"
    assert margin != s2.DOOR_GAP_MARGIN_NOT_ADJACENT


def test_margin_matches_validator_verdict_at_the_threshold():
    """餘裕 <1 必定是驗收會擋的，>1 必定是驗收放行的——同一把尺的定義。"""
    from gemini_analyze import _door_adjacency_violation
    door = _rect(0, 0, 100, 900)
    for gap_px, expect_blocked in ((10, True), (20, True), (40, False), (60, False)):
        sofa = _rect(100 + gap_px, 100, 300, 800)
        tv = _rect(700, 100, 900, 800)
        margin = _margin(sofa, tv, door)
        rb = {
            "entrance_door": s2._bbox_yx1000(door, W_PX, H_PX),
            "sofa": s2._bbox_yx1000(sofa, W_PX, H_PX),
            "focal_anchor": s2._bbox_yx1000(tv, W_PX, H_PX),
        }
        blocked = _door_adjacency_violation(rb) is not None
        assert blocked is expect_blocked, f"gap={gap_px}px 驗收判定不如預期"
        assert (margin < 1.0) is blocked, (
            f"gap={gap_px}px：餘裕 {margin:.3f} 與驗收判定 {blocked} 不一致")

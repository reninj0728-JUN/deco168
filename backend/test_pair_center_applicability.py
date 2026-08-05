# -*- coding: utf-8 -*-
"""中心差指標的適用前提：兩個緊湊量體面對面。

757845E3（2026-08-05）：長條走廊型客廳，模型第 5 次畫出一張**各項全過**的圖
（沙發-門距 49/門寬 154 = 0.32 > 0.25、必備商品全到、判官給 hard_fail=False），
卻被 `_fail_closed_validation` 的中心差極端值翻判成 hard_fail，客戶零圖。

那個 109 不是家具錯位，是判官這次把電視櫃 bbox 畫得比前四次更寬更深：
    #1 中心差 105.5   focal 寬 256 高 287
    #2         95.5          259     287
    #3         66.5          220     254
    #4         38.5          255     407   ← bbox 最高，中心差反而最小
    #5        108.5          310     336   ← bbox 最寬，中心差最大
同一單、同一配置（沙發 x 263~453、電視 x 623~948 都貼右牆），量測跨度 70，
而門檻 100 就落在這團雜訊正中間。

修法：**門檻 100 不動**（它有用戶裁決背書），只限制指標的適用條件。
校準庫實測信封見 `FOCAL_COMPACT_MAX_W` 上方註解。
"""
from __future__ import annotations

import json
from pathlib import Path

import api
import pytest

CALIB = Path(__file__).resolve().parent / "fixtures" / "living_layout_calibration.json"

# 757845E3 attempt 5（phase3）判官原始判決，逐字取自 validation_history
J757_A5 = {
    "ok": False, "hard_fail": False, "room_type": "living",
    "reason": "購買清單中的天鵝絨扇形靠背單椅未出現在圖上",
    "product_visibility_fail": False,
    "visibility_nice_bad": ["accent_chair"],
    # 以下幾何旗標逐字取自該次 raw_verdict——全部乾淨，這正是它該被交付的理由
    "sofa_focal_face_each_other": True,
    "furniture_blocks_door": False,
    "furniture_blocks_walkway": False,
    "sofa_intrudes_walkway": False,
    "sofa_faces_walkway": False,
    "sofa_facing_entrance_door": False,
    "sofa_outside_living_zone": False,
    "sofa_on_wrong_side": False,
    "sofa_facing_window": False,
    "sofa_back_against_window": False,
    "coffee_table_in_walkway": False,
    "spatial_fidelity_fail": False,
    "offframe_room_invaded": False,
    "focal_anchor_misaligned_with_sofa": False,
    "render_bboxes": {
        "sofa": [513, 263, 736, 412],
        "focal_anchor": [565, 638, 901, 948],   # 寬 310、高 336
        "entrance_door": [168, 60, 891, 214],
        "main_window": [352, 442, 481, 477],
    },
}


def _bbox_case(sofa, focal, **extra):
    v = {"ok": False, "hard_fail": False, "room_type": "living",
         "render_bboxes": {"sofa": sofa, "focal_anchor": focal}}
    v.update(extra)
    return v


# ── 核心：這單必須被豁免 ────────────────────────────────────────────

def test_j757845E3_attempt5_is_no_longer_flipped():
    """回歸鎖：這張各項全過的圖不得再被中心差翻成 hard_fail。"""
    pair = api._pair_center_delta(J757_A5, tolerance=0)
    assert pair["abs_delta_y"] == 109, f"前提變了，中心差不是 109 而是 {pair}"
    assert pair["abs_delta_y"] > api.PAIR_CENTER_EXTREME, "門檻被動過了，這條測試前提不成立"

    out = api._fail_closed_validation(J757_A5, "living")
    assert out.get("hard_fail") is False, (
        f"仍被翻判成 hard_fail：{out.get('reason')}")
    assert out.get("pair_center_delta_y") == pair["delta_y"], "量測值不該被拿掉，診斷還要用"
    assert out.get("pair_center_skipped_reason"), "跳過必須留下原因，不得靜默"


def test_measurement_is_still_recorded_when_skipped():
    """跳過的是『翻判』不是『量測』——校正圖與診斷仍需要這個數字。"""
    out = api._fail_closed_validation(J757_A5, "living")
    assert out["pair_center_delta_y"] == -109
    assert "沿牆延伸" in out["pair_center_skipped_reason"]


# ── 反貧血：緊湊量體的極端錯位仍然要擋 ──────────────────────────────

def test_compact_focal_with_extreme_delta_still_flips():
    """把同一張圖的 focal bbox 縮成緊湊量體，翻判必須回來。

    證明豁免條件真的是「bbox 尺寸」在把關，不是整條翻判被拿掉。
    """
    v = json.loads(json.dumps(J757_A5))
    # 保持中心 y=733 不變，只把寬高縮進緊湊範圍（寬 100、高 150）
    v["render_bboxes"]["focal_anchor"] = [658, 743, 808, 843]
    pair = api._pair_center_delta(v, tolerance=0)
    assert pair["abs_delta_y"] > api.PAIR_CENTER_EXTREME, f"fixture 沒踩到門檻：{pair}"
    out = api._fail_closed_validation(v, "living")
    assert out.get("hard_fail") is True, "緊湊量體的極端錯位竟然沒被擋"
    assert "極端值" in (out.get("reason") or "")


# ── 豁免的第二道條件：其他幾何必須全部乾淨 ──────────────────────────

@pytest.mark.parametrize("dirty_flag", [
    "furniture_blocks_door", "furniture_blocks_walkway", "sofa_intrudes_walkway",
    "sofa_faces_walkway", "sofa_facing_entrance_door", "sofa_outside_living_zone",
    "sofa_on_wrong_side", "coffee_table_in_walkway", "spatial_fidelity_fail",
    "offframe_room_invaded", "focal_anchor_misaligned_with_sofa",
])
def test_large_bbox_but_real_geometry_problem_still_flips(dirty_flag):
    """🔴 這是最重要的一條：bbox 大**不等於**免罰。

    只看尺寸就放行，等於「大型電視櫃一律沒有防線」——而 bbox 大不必然是沿牆
    延伸，也可能只是離鏡頭近的正常櫃體真的擺錯。判官只要標出任何一個幾何硬傷，
    中心差就要照常翻判。
    """
    v = json.loads(json.dumps(J757_A5))
    v["sofa_focal_face_each_other"] = True
    v[dirty_flag] = True
    assert api._focal_pair_metric_applicable(v)[0] is False, "fixture 的 bbox 應該是大的"
    out = api._fail_closed_validation(v, "living")
    assert out.get("hard_fail") is True, (
        f"{dirty_flag}=True 卻仍被豁免——大 bbox 變成了免罪金牌")


def test_large_bbox_not_facing_each_other_still_flips():
    """沙發與電視櫃根本沒有面對面時，不得豁免。"""
    v = json.loads(json.dumps(J757_A5))
    v["sofa_focal_face_each_other"] = False
    assert api._fail_closed_validation(v, "living").get("hard_fail") is True


def test_exemption_requires_both_conditions():
    """豁免＝(bbox 超出信封) AND (其他幾何乾淨)，缺一不可。"""
    base = json.loads(json.dumps(J757_A5))
    base["sofa_focal_face_each_other"] = True
    # ① 大 bbox ＋ 乾淨 → 豁免
    assert api._fail_closed_validation(base, "living").get("hard_fail") is False
    # ② 大 bbox ＋ 髒 → 不豁免
    dirty = json.loads(json.dumps(base)); dirty["sofa_on_wrong_side"] = True
    assert api._fail_closed_validation(dirty, "living").get("hard_fail") is True
    # ③ 小 bbox ＋ 乾淨 → 不豁免（尺寸在信封內，量測可信）
    small = json.loads(json.dumps(base))
    small["render_bboxes"]["focal_anchor"] = [658, 743, 808, 843]
    assert api._fail_closed_validation(small, "living").get("hard_fail") is True


@pytest.mark.parametrize("w,h,applicable", [
    (125, 172, True),    # 校準庫接受組的上限
    (200, 280, True),    # 邊界本身仍適用
    (201, 280, False),   # 寬度超過
    (200, 281, False),   # 高度超過
    (310, 336, False),   # 757845E3#5
])
def test_applicability_boundary(w, h, applicable):
    v = _bbox_case([500, 200, 600, 300], [400, 100, 400 + h, 100 + w])
    assert api._focal_pair_metric_applicable(v)[0] is applicable


# ── 校準庫回歸：11 個 case 判定一個都不能變 ─────────────────────────

def _calibration_cases():
    data = json.loads(CALIB.read_text(encoding="utf-8"))
    for group in ("accepted", "rejected"):
        for case in data.get(group, []):
            bb = case.get("render_bboxes") or {}
            if not (bb.get("sofa") and bb.get("focal_anchor")):
                continue
            yield group, case.get("case_id"), bb


def test_calibration_library_verdicts_unchanged():
    """校準庫每一個 case 的『會不會被中心差翻判』必須跟修改前完全相同。

    修改前的行為＝只看 `abs_delta_y > 100`。這條把新舊兩種判定逐案對比。
    """
    changed = []
    for group, case_id, bb in _calibration_cases():
        v = _bbox_case(bb["sofa"], bb["focal_anchor"])
        pair = api._pair_center_delta(v, tolerance=0)
        assert pair, f"{case_id} 算不出中心差"
        before = pair["abs_delta_y"] > api.PAIR_CENTER_EXTREME
        after = api._fail_closed_validation(v, "living").get("hard_fail") is True
        if before != after:
            fx = bb["focal_anchor"]
            changed.append(f"{group}/{case_id} 寬{fx[3]-fx[1]} 高{fx[2]-fx[0]} "
                           f"差{pair['abs_delta_y']} {before}→{after}")
    assert not changed, "校準庫判定被改變了：\n  " + "\n  ".join(changed)


def test_calibration_library_still_has_the_two_gated_cases():
    """反貧血：校準庫裡本來就有兩個『靠中心差擋下』的案例，它們必須仍被擋。

    若哪天這兩個也被豁免了，代表門檻或適用條件被放寬到失效。
    """
    gated = []
    for _group, case_id, bb in _calibration_cases():
        v = _bbox_case(bb["sofa"], bb["focal_anchor"])
        if api._fail_closed_validation(v, "living").get("hard_fail") is True:
            gated.append(case_id)
    assert len(gated) >= 2, f"靠中心差擋下的案例剩 {gated}，守門形同失效"
    for expected in ("6DA08412", "31E341CF"):
        assert any(expected in (g or "") for g in gated), f"{expected} 不再被擋"


# ── 兩個消費點都要套同一個前提 ──────────────────────────────────────

def test_both_consumers_share_the_precondition():
    """只修極端值翻判的話，同一團雜訊會搬到 `_focal_door_axis_conflict` 繼續誤殺。"""
    import inspect
    for fn in (api._fail_closed_validation, api._focal_door_axis_conflict):
        src = inspect.getsource(fn)
        assert "_focal_pair_metric_applicable" in src, (
            f"{fn.__name__} 沒有套用中心差的適用前提")


def test_axis_conflict_skips_elongated_focal():
    """長牆延伸的櫃體，`_focal_door_axis_conflict` 也不得用中心差下判斷。"""
    v = json.loads(json.dumps(J757_A5))
    # 把電視櫃推到貼門，觸發 door adjacency；但 bbox 仍是長條
    v["render_bboxes"]["focal_anchor"] = [565, 100, 901, 410]   # 寬 310
    assert api._focal_door_axis_conflict(v) is None, "長條櫃體仍被中心差判成軸衝突"

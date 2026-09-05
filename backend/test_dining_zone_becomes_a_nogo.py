# -*- coding: utf-8 -*-
"""客戶標的餐廳區必須變成「大型客廳家具禁區」，不能只留一句沒人讀的文字。

71B8E762（2026-09-04）：客戶在同一張照片標「客廳＋餐廳」，Gemini 也確實算出
`dining_zone = [580,250,850,750]`（畫面中段），但 `flatten_zoning_v2_to_v1` 組
dining dict 時**只帶 where 文字、沒帶 bbox**，`no_large_furniture_zone` 同樣只取
文字。下游因此完全不知道那塊是餐廳 → 沙發右半段直接坐進餐廳框。

🔴 為什麼牙齒要長在 `no_large_furniture_zones` 而不是 `zones.dining_zone`：
   `zones.dining_zone` **下游沒有任何讀取點**——`prompt_builder` 只讀
   living_zone / walkway / entrance_zone。光把 bbox 帶下去是死欄位。
   `no_large_furniture_zones` 才是兩端都吃的欄位：
     · 生成端：prompt_builder 的 NO-LARGE-FURNITURE 段落
     · 驗收端：`has_dining_middle_constraint` 把沙發深度門檻
       從 hard 58 / soft 64 拉到 65 / 72（gemini_analyze.py:1598-1599）

⚠️ 觸發靠關鍵字比對，用詞不可自由發揮：必須同時出現「餐廳」與
   「中段／中間／中央」之一（gemini_analyze.py:1577-1581）。
⚠️ bbox 軸序是 [ymin, xmin, ymax, xmax]（y 在前）；別跟 `_living_door_axis_clear_rect`
   那邊的 x-first 像素座標搞混——這個 codebase 兩套並存。
"""
from __future__ import annotations

import api


def _v2(dining_bbox=None, no_go=None):
    pz = {"living_zone": {"where": "靠採光落地窗處之客廳主區域",
                          "rationale": "最長實牆",
                          "bbox_on_best_photo": [570, 0, 999, 1000],
                          "sofa_side": "left", "sofa_side_source": "user_explicit"}}
    if dining_bbox is not None:
        pz["dining_zone"] = {"where": "", "bbox_on_best_photo": dining_bbox}
    if no_go is not None:
        pz["no_large_furniture_zone"] = no_go
    return {"proposed_zones": pz, "existing_zones": {}, "overall_confidence": "high"}


def test_dining_bbox_is_carried_into_the_flattened_zone():
    """客廳有帶 bbox，餐廳也要帶——不能一邊有一邊沒有。"""
    out = api.flatten_zoning_v2_to_v1(_v2([580, 250, 850, 750]), "A")
    d = (out.get("zones") or {}).get("dining_zone") or {}
    assert d.get("bbox_on_best_photo") == [580, 250, 850, 750], f"餐廳 bbox 掉了：{d}"


def test_dining_zone_becomes_a_no_go_rule():
    """🔴 真正的牙齒：餐廳框要變成 no_large_furniture_zones 的一條。"""
    out = api.flatten_zoning_v2_to_v1(_v2([580, 250, 850, 750]), "A")
    no_go = (out.get("furniture_placement_rules") or {}).get("no_large_furniture_zones") or []
    assert no_go, "餐廳框沒有變成任何禁區規則"
    txt = " ".join(str(x) for x in no_go)
    assert "沙發" in txt and "不得佔用" in txt, f"禁區條目沒有禁止沙發：{txt}"


def test_wording_actually_triggers_the_judge_threshold():
    """🔴 用詞必須同時含「餐廳」＋「中段/中間/中央」，否則判官門檻不會拉高。

    這條直接複製 gemini_analyze.py:1577-1581 的觸發條件——寫成
    「y58-85% 區域為餐廳區」會咬不住，那是本次最容易寫錯的一步。
    """
    out = api.flatten_zoning_v2_to_v1(_v2([580, 250, 850, 750]), "A")
    signal = " ".join(str(x) for x in
                      ((out.get("furniture_placement_rules") or {})
                       .get("no_large_furniture_zones") or []))
    kws = ["中段", "中間", "中央", "middle", "center", "centre"]
    assert ("餐廳" in signal or "dining" in signal.lower()), "缺『餐廳』關鍵字"
    assert any(k in signal for k in kws), f"缺『中段』類關鍵字，判官門檻不會觸發：{signal}"


def test_percentages_use_the_y_first_axis_order():
    """bbox 是 [ymin,xmin,ymax,xmax]；橫向要取 x、縱深要取 y，不可對調。"""
    out = api.flatten_zoning_v2_to_v1(_v2([580, 250, 850, 750]), "A")
    txt = " ".join(str(x) for x in
                   ((out.get("furniture_placement_rules") or {})
                    .get("no_large_furniture_zones") or []))
    assert "橫向 25%–75%" in txt, f"橫向取錯軸（應為 x 250-750）：{txt}"
    assert "縱深 58%–85%" in txt, f"縱深取錯軸（應為 y 580-850）：{txt}"


def test_no_dining_zone_adds_nothing():
    """沒有餐廳框就不准憑空生出禁區——不得無中生有限制。"""
    out = api.flatten_zoning_v2_to_v1(_v2(None), "A")
    no_go = (out.get("furniture_placement_rules") or {}).get("no_large_furniture_zones") or []
    assert not any("餐廳" in str(x) for x in no_go), f"沒有餐廳框卻加了餐廳禁區：{no_go}"


def test_bad_bbox_does_not_crash():
    """壞資料只能被忽略，不得讓整個攤平炸掉。"""
    for bad in ([1, 2, 3], "x", None, ["a", "b", "c", "d"]):
        out = api.flatten_zoning_v2_to_v1(_v2(bad), "A")
        assert isinstance(out.get("zones"), dict)


def test_existing_no_go_text_is_kept():
    """原本就有的禁區文字不得被餐廳那條蓋掉。"""
    out = api.flatten_zoning_v2_to_v1(
        _v2([580, 250, 850, 750], no_go={"where": "大門迴轉區需淨空"}), "A")
    no_go = (out.get("furniture_placement_rules") or {}).get("no_large_furniture_zones") or []
    txt = " ".join(str(x) for x in no_go)
    assert "大門迴轉區" in txt and "餐廳" in txt, f"兩條該並存：{no_go}"

# -*- coding: utf-8 -*-
"""綁邊無解時的安全降級｜側別是偏好，安全約束不是。

ED43F9F9（2026-08-10）證明的問題：客戶綁「沙發靠右」，`_layout_guide_plan`
正確算出無解——**然後系統什麼都沒做**，仍以 bound_constraints 進生成卻沒有
引導圖，安全約束被靜默丟掉。前檢 `_auto_layout_safety_check` 只擋 auto
（`sofa_side != "free"` 直接 return ""，那是刻意的「用戶選擇是法律」），
綁邊單直接穿過去。

用原圖 1477x1108 ＋ 原 zoning 量出來的歸因（保留恰好一個硬限制）：
    只有客廳框 → 無解 ／ 只有大門通道 → 無解 ／ 只有 walkway → ✅ 有解
所以「客廳框」和「大門通道」各自就足以否決右牆，**walkway 無罪**。

⚠️ 兩個已被推翻的說法，別再走：
  1. 「三個約束都有份、要三個全拿掉才活」——那是**累加消去的順序假象**。
     累加只證明「拿到第三個才活」，證不出三者都是必要條件。
  2. 「唯一解是 free」——`free` 不是乾淨的解除側別實驗：它同時把沙發從
     0.38W×0.48H 縮成 0.18W×0.24H、解除綁牆，**而且 is_auto 會整包丟掉
     blocked_rects**（只留門廊）。拿它當對照組會得到錯的因果。
     ED43 的 free guide 實際把沙發框壓在陽台落地窗上，不可採用。

⚠️ 容差不得自備一份：`BOUND_WALL_HALF_TOLERANCE = 0.08`（門檻約 0.42W）。
   我的診斷腳本一度寫死 0.05／0.45W，數字是錯的。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import api
import pytest

ROOT = Path(__file__).resolve().parent
ED43 = ROOT / "_ed43"


# ── ① 降級階梯本身 ────────────────────────────────────────────────

def _wide_room(**over):
    """一間左右都很空的房：綁哪邊都放得下。"""
    kw = dict(W=2000, H=1000, preferred="right", entrance_side="",
              entrance_bbox=None, focal_side="left", blocked_rects=[],
              living_bbox=(0, 0, 2000, 1000), window_bbox=None, can_float=False)
    kw.update(over)
    return kw


def test_preferred_side_is_kept_when_it_works():
    """偏好側可行就不准動它——降級只在無解時發生。"""
    side, plan, note = api._resolve_sofa_side_with_fallback(**_wide_room())
    assert side == "right"
    assert plan and plan["valid"]
    assert note == "", "沒降級卻留了降級說明"


def test_falls_back_to_the_other_side_when_preferred_is_infeasible():
    """偏好側無解、反側可行 → 用反側，而且要說出來。

    這是這一刀真正的收益：以前這種單直接變成「沒有引導圖」的裸生。
    """
    # 用 ED43 真正的機制：客廳框只到 55%，綁右時沙發中心必然落在框外。
    # ⚠️ 別改成「把右半整片封死」——那會連「沙發靠左時電視要放右邊」
    #    一起封死，左側也跟著無解，測不到降級（我第一版就是這樣紅的）。
    kw = _wide_room(living_bbox=(0, 0, 1100, 1000))
    side, plan, note = api._resolve_sofa_side_with_fallback(**kw)
    assert side == "left", f"反側可行卻沒有降級過去（拿到 {side!r}）"
    assert plan and plan["valid"]
    assert "right" in note and "left" in note, f"降級沒留下可讀的理由：{note!r}"


def test_returns_none_rather_than_pretending_when_nothing_is_safe():
    """兩側都無解時必須回 None——不得挑一個「比較不糟」的硬上。"""
    kw = _wide_room(blocked_rects=[(0, 0, 2000, 1000)])
    side, plan, note = api._resolve_sofa_side_with_fallback(**kw)
    assert side is None and plan is None
    assert note, "無解卻沒有說明"


def test_float_is_refused_without_window_geometry():
    """🔴 沒有窗戶幾何就不得用浮動——不知道窗在哪 ≠ 沒有窗。

    規格要求浮動候選必須補上窗戶禁區。全站目前沒有任何窗的多邊形／bbox
    （zoning_v2 與 layout_geometry_s2 都沒有），所以浮動今天一律不採用。
    """
    kw = _wide_room(blocked_rects=[(0, 0, 2000, 1000)],
                    can_float=True, window_bbox=None)
    side, _plan, note = api._resolve_sofa_side_with_fallback(**kw)
    assert side != "free", "沒有窗戶幾何卻採用了浮動配置"
    assert side is None
    assert "浮動" in note, f"沒說明浮動為何不採用：{note!r}"


def test_window_is_a_hard_constraint_for_every_candidate():
    """窗戶對綁邊候選也是硬限制，不是只有浮動才檢查。"""
    src = inspect.getsource(api._resolve_sofa_side_with_fallback)
    assert "_clear_of_window" in src
    # 靠窗那側整片是窗 → 不得選它
    kw = _wide_room(window_bbox=(1000, 0, 2000, 1000), preferred="right")
    side, plan, _note = api._resolve_sofa_side_with_fallback(**kw)
    assert side != "right", "沙發被放到窗戶上了"
    if plan:
        assert not api._rects_intersect(tuple(plan["sofa"]),
                                        (1000, 0, 2000, 1000))


def test_float_candidate_is_rejected_when_it_lands_on_the_window():
    """🔴 浮動的窗戶禁區只能靠事後檢查——`is_auto` 會整包丟掉 blocked_rects。

    所以「把窗戶塞進 blocked_rects」對浮動候選是無效的：
    `forbidden = [door_clear] if is_auto else blocked + [door_clear]`。
    ED43 的 free guide 正是這樣把沙發框壓在陽台落地窗上的。
    """
    # 走道整片封死右半 → 兩個綁邊側都無解（沙發或對牆電視必有一個踩到）；
    # 但 auto 會忽略 blocked，所以浮動仍找得到位置。
    kw = _wide_room(blocked_rects=[(900, 0, 2000, 1000)], can_float=True,
                    window_bbox=(0, 0, 1, 1))
    side, plan, _note = api._resolve_sofa_side_with_fallback(**kw)
    assert side == "free" and plan, (
        f"這組輸入本來要走到浮動候選才測得到窗戶檢查（拿到 {side!r}）")

    # 把窗戶挪到浮動沙發正下方 → 必須改判無解，不得照畫
    on_the_window = tuple(plan["sofa"])
    kw2 = dict(kw, window_bbox=on_the_window)
    side2, plan2, note2 = api._resolve_sofa_side_with_fallback(**kw2)
    assert side2 is None and plan2 is None, (
        f"浮動沙發壓在窗戶上卻被採用了（side={side2!r}）")
    assert note2


def test_free_preference_is_not_touched():
    """未綁邊（free）不進降級搜尋——那條路有自己的 auto 前檢。"""
    side, plan, note = api._resolve_sofa_side_with_fallback(**_wide_room(
        preferred="free"))
    assert (side, plan, note) == ("free", None, "")


def test_never_calls_a_model():
    """付費前後都一樣：降級搜尋是純幾何，不得偷叫模型。"""
    src = inspect.getsource(api._resolve_sofa_side_with_fallback)
    for banned in ("genai", "generate_content", "fal_client", "requests.post"):
        assert banned not in src, f"降級搜尋呼叫了 {banned}"


# ── ② 寫回：guide 與 prompt 必須讀到同一個側別 ──────────────────────

def test_downgrade_is_written_where_both_readers_look():
    """🔴 只改一邊，模型會同時收到兩套互斥配置。

    guide 讀 `_guide_sofa_side(zoning)`；prompt 讀
    `furniture_placement_rules.sofa_side`（prompt_builder.py）。
    """
    z = {"furniture_placement_rules": {"sofa_side": "right", "tv_side": "left"}}
    api._apply_sofa_side_downgrade(z, "right", "left", "測試")
    assert api._guide_sofa_side(z) == "left", "guide 那邊沒跟著改"
    assert z["furniture_placement_rules"]["sofa_side"] == "left", "prompt 那邊沒改"
    assert z["furniture_placement_rules"]["tv_side"] == "right", "電視沒跟著換到對牆"


def test_downgrade_leaves_a_trace_so_nothing_claims_compliance():
    """降級必須留痕——交付／驗收不得宣稱「已照客戶指定的側別」。"""
    z = {"furniture_placement_rules": {"sofa_side": "right"}}
    api._apply_sofa_side_downgrade(z, "right", "left", "右側無解")
    d = z.get("_sofa_side_downgraded")
    assert isinstance(d, dict)
    assert d["from"] == "right" and d["to"] == "left" and d["reason"]
    # 來源標記不得繼續宣稱是客戶選的
    assert z["furniture_placement_rules"]["sofa_side_source"] != "user_explicit", (
        "降級後仍掛著 user_explicit——等於把客戶沒選的側別說成客戶選的")


def test_float_downgrade_also_switches_layout_mode():
    z = {"furniture_placement_rules": {"sofa_side": "right", "tv_side": "left"}}
    api._apply_sofa_side_downgrade(z, "right", "free", "兩側皆無解")
    assert api._guide_sofa_side(z) == "free"
    assert z["furniture_placement_rules"]["sofa_side"] == ""
    assert z["furniture_placement_rules"]["tv_side"] == ""


# ── ③ pipeline 有真的接上 ────────────────────────────────────────

def test_pipeline_runs_the_ladder_before_drawing_the_guide():
    """降級搜尋必須在畫 guide 之前，且用同一組硬限制。"""
    src = inspect.getsource(api).split("_resolve_sofa_side_with_fallback")
    assert len(src) >= 3, "pipeline 沒有呼叫降級搜尋"
    call = src[2]
    head = call[:call.index("_build_layout_guide_image")]
    for field in ("living_bbox=_living_bbox_crop", "entrance_bbox=_door_bbox_crop",
                  "blocked_rects=_blocked_crop"):
        assert field in head, f"降級搜尋沒吃到同一組硬限制：{field}"


def test_no_safe_placement_gets_its_own_skip_reason():
    """『沒有合法配置』要跟『畫圖失敗』分開記——兩者要修的東西不同。"""
    src = inspect.getsource(api)
    assert '"no_safe_sofa_placement"' in src
    assert '"guide_render_returned_empty"' in src


# ── ④ ED43 金樣品：真圖真 zoning ─────────────────────────────────

@pytest.mark.skipif(not (ED43 / "zoning.json").exists(),
                    reason="樣品資料不在（backend/_ed43 未下載）")
def test_ed43_reports_no_safe_placement_instead_of_faking_one():
    """🔴 ED43 在客戶確認的分區下**真的無解**，降級救不了它。

    這條測試存在的意義是防止我們日後為了「有 guide」而放寬安全約束：
    右側被客廳框與大門通道各自否決，左側同樣無解，浮動因無窗戶幾何不採用。
    正確結果是 None（記為 no_safe_sofa_placement），不是硬擠一個配置出來。
    """
    from PIL import Image

    z = json.loads((ED43 / "zoning.json").read_text(encoding="utf-8"))
    zones = z.get("zones") or {}
    W, H = Image.open(ED43 / "photo_02.jpg").size
    assert (W, H) == (1477, 1108), "樣品底圖換了，下面的數字要重量"

    def px(k):
        bb = (zones.get(k) or {}).get("bbox_on_best_photo")
        return api._bbox1000_to_crop_px(bb, W, H, (0, 0, W, H)) if bb else None

    ent, living = px("entrance_zone"), px("living_zone")
    blocked = [r for r in (px("walkway"), px("no_go_zone")) if r]
    blocked = [r for r in blocked
               if not (r == px("no_go_zone") and ent
                       and api._rects_intersect(r, ent))]
    assert api._guide_sofa_side(z) == "right"

    side, plan, note = api._resolve_sofa_side_with_fallback(
        W, H, "right", entrance_side=api._entrance_side_from_zoning(z),
        entrance_bbox=ent, focal_side=api._preferred_focal_side(z),
        blocked_rects=blocked, living_bbox=living,
        window_bbox=None, can_float=False)
    assert side is None and plan is None, (
        f"ED43 在確認分區下無解，卻擠出了 {side!r} 的配置——安全約束被放寬了")
    assert note

    # 歸因：客廳框、大門通道各自就足以否決右牆；walkway 不是必要因素
    def one(**keep):
        kw = dict(entrance_bbox=None, blocked_rects=[], living_bbox=None)
        kw.update(keep)
        return api._layout_guide_plan(
            W, H, "right", focal_side=api._preferred_focal_side(z),
            entrance_side=api._entrance_side_from_zoning(z), **kw).get("valid")

    assert one(living_bbox=living) is False, "客廳框單獨不再否決右牆？重新量"
    assert one(entrance_bbox=ent) is False, "大門通道單獨不再否決右牆？重新量"
    assert one(blocked_rects=blocked) is True, (
        "walkway 單獨變成否決因素了——那先前『walkway 無罪』的結論要重寫")

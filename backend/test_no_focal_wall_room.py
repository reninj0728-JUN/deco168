# -*- coding: utf-8 -*-
"""三面牆都不能放電視櫃的房型，不要硬配一個。

7F0874C7：客廳右牆「中段至深端內嵌整合有一字型廚具料理台與吊櫃」，左牆中深段有
房門，底部是落地窗——**一面乾淨的電視牆都沒有**。但 LIVING_MUST_HAVE 寫死了
media_console，系統仍推 tv_side=right，模型為了完成「放電視櫃」這個任務，把整組
廚具蓋掉。

🔴 資料系統本來就有，缺的只是交叉檢查：
     kitchen_zone      「廚房位置為固定建築設備，不可變動」   ← zoning 自己寫的
     no_go             「右側廚具操作走道」
     sofa_side_reason  「右側已被廚具佔滿」
   同一份規則裡卻是 tv_side='right'。而 kitchen_zone 只被拿去畫分區圖的顏色，
   `grep kitchen_zone api.py` = 0 行。

步驟 1｜焦點牆排除固定廚具那面牆。
步驟 2｜排除後真的沒有乾淨牆時，四邊一起鬆：配對／清單／prompt／判官。

⚠️ 精度要留住：客廳【可以】沿進深碰到開放式廚房（台灣小宅本來就這樣），
   不能放的是「電視櫃／沙發背牆佔住廚具那面牆」。規則若寫成「有廚具那一端整段
   都不是客廳」會誤傷開放式客餐廳。
⚠️ 找不到乾淨牆要回空字串，不能「右邊是廚具就改選左邊」——左邊常是門牆，
   那只是換一個東西撞（2879173D：沙發吃掉進門落腳區）。
⚠️ 讀 wall_inventory 的文字描述，不是 kitchen_zone：zoning 攤平成 v1 時
   kitchen_zone 會被丟掉，決策當下拿不到。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api
import furniture_match as fm
from prompt_builder import SOFA_TV_FACE_TO_FACE_CONTRACT, build_nano_banana_inputs

ROOT = Path(__file__).resolve().parent
KITCHEN_WALL = {"name": "右側長牆", "has_opening": True,
                "description": "近端為部分留白牆面，中段至深端內嵌整合有一字型廚具料理台與吊櫃，緊鄰陽台落地門。"}
DOOR_WALL = {"name": "左側長牆", "has_opening": True,
             "description": "近端為完整大面實牆，中深段有一房間門洞開口。"}
CLEAN_WALL = {"name": "左側長牆", "has_opening": False,
              "description": "整片平整無走道與門洞干擾的長向實牆。"}


def _z(walls, entrance="center", window=""):
    return {"spatial_synthesis": {"wall_inventory": walls},
            "_entrance_side": entrance, "_window_side": window}


# ── 步驟 1：認得出固定廚具 ──────────────────────────────────────
def test_kitchen_wall_is_recognised():
    assert api._wall_has_fixed_fixture(KITCHEN_WALL) is True
    assert api._wall_has_fixed_fixture(DOOR_WALL) is False
    assert api._wall_has_fixed_fixture(CLEAN_WALL) is False
    assert api._fixture_sides_from_zoning(_z([KITCHEN_WALL, DOOR_WALL])) == {"right"}


@pytest.mark.parametrize("desc", [
    "一字型廚具料理台與吊櫃", "牆面設有流理台與水槽", "整面櫥櫃與瓦斯爐",
    "kitchen counter with upper cabinets", "island with sink",
])
def test_various_kitchen_wordings(desc):
    assert api._wall_has_fixed_fixture({"name": "右牆", "description": desc}) is True


def test_focal_side_never_lands_on_the_kitchen_wall():
    """右牆是廚具、左牆有門 → 兩側都不乾淨 → 回空，不改選門牆。"""
    assert api._preferred_focal_side(_z([KITCHEN_WALL, DOOR_WALL])) == ""


def test_clean_wall_on_the_other_side_is_still_used():
    """⚠️ 不能矯枉過正：右邊是廚具、左邊是完整實牆時，照樣選左邊。"""
    assert api._preferred_focal_side(_z([KITCHEN_WALL, CLEAN_WALL])) == "left"


def test_rooms_without_a_kitchen_are_untouched():
    """沒有廚具的房型行為完全不變。"""
    z = _z([DOOR_WALL, {"name": "右側長牆", "has_opening": False, "description": "完整實牆"}])
    assert api._preferred_focal_side(z) == "right"


def test_real_orders():
    """7F0874C7 沒有乾淨牆；C1168B61 有（左牆完整），不能被誤傷。"""
    for f, expect in (("_7f08.json", ""), ("_c116.json", "left")):
        p = ROOT / f
        if not p.exists():
            pytest.skip(f"{f} 不在本機")
        d = json.loads(p.read_text(encoding="utf-8"))
        r = d.get("result") if isinstance(d.get("result"), dict) else d
        got = api._preferred_focal_side(r.get("zoning_v2") or {})
        assert got == expect, f"{f} 焦點牆 {got!r}，應為 {expect!r}"


# ── 步驟 2：四邊一起鬆 ──────────────────────────────────────────
def _mini_catalog():
    """一個小目錄：電視櫃故意寫得跟北歐風不搭，正常情況下分數墊底。
    must 會保證它有位子；拿掉 must 之後它就不該再被硬塞進來。"""
    def it(i, cat, name, kws):
        return {"id": i, "name_zh": name, "brand": "x", "category": cat,
                "style_tags": ["nordic"], "keywords": kws, "colors": ["白"],
                "price_twd": 9000, "image_url": "https://x/i.jpg",
                "purchase_url": "https://x/p", "dimensions": "",
                "flux_descriptor": " ".join(kws), "source": "test"}
    items = [
        it("s1", "沙發", "北歐淺木三人沙發", ["nordic sofa", "light oak", "linen"]),
        it("c1", "茶几", "北歐圓形實木茶几", ["nordic coffee table", "round", "oak"]),
        it("r1", "地毯", "北歐羊毛地毯", ["nordic rug", "wool", "beige"]),
    ]
    # 唯一的電視櫃，關鍵字刻意跟 prompt 無關
    items += [it(f"m{i}", "電視櫃", f"深色工業風電視櫃{i}",
                 ["industrial", "dark metal", "media console"]) for i in range(1, 4)]
    return items


def _cats(no_focal_wall):
    got = fm.match_furniture("nordic", "nordic light oak linen sofa, round oak coffee table, wool rug",
                             _mini_catalog(), top_n=5, mode="living",
                             no_focal_wall=no_focal_wall)
    return [fm.resolve_category(x) for x in got]


def test_matching_drops_media_console_from_must():
    """🔴 行為測，不是掃字串。

    第一版只斷言原始碼裡有 `must = [...]` 那一行——把 `if no_focal_wall` 改成
    `if False` 照樣全綠（破壞驗證當場抓到）。跟前面兩刀同一種錯：測了字面、
    沒測行為。
    """
    assert "media_console" in _cats(False), "一般情況本來就該保證有電視櫃"
    assert "media_console" not in _cats(True),         "沒有電視牆卻仍硬塞電視櫃——must 沒有真的鬆開"


def test_judge_expected_categories_drop_it_too():
    """🔴 四邊要一起鬆：清單不給、判官還要求 → 每張圖都 ok=False，
    正是 _display_cats_for_room 自己 docstring 記載的那個病。"""
    assert "media_console" in api._display_cats_for_room("living")
    assert "media_console" not in api._display_cats_for_room("living", no_focal_wall=True)
    # 其他房型不受影響
    assert api._display_cats_for_room("bedroom") == api._display_cats_for_room(
        "bedroom", no_focal_wall=True)


def test_prompt_stops_demanding_a_tv():
    base = {"style": "nordic", "style_label": "北歐", "flux_prompt": "x", "matched_furniture": []}
    key = SOFA_TV_FACE_TO_FACE_CONTRACT[:60]
    normal = build_nano_banana_inputs(dict(base), None, "https://x/a.jpg", room_type="living")["prompt"]
    none_ = build_nano_banana_inputs({**base, "_no_focal_wall": True}, None,
                                     "https://x/a.jpg", room_type="living")["prompt"]
    assert key in normal, "一般房型的沙發對電視合約被誤刪"
    assert key not in none_, "沒有電視牆卻仍要求沙發正對電視"
    assert "NO TV WALL" in none_ and "Do NOT invent a TV" in none_


# ── 接線：旗標要真的從 pipeline 傳到四端 ────────────────────────
def test_pipeline_computes_and_propagates_the_flag():
    """🔴 前面兩刀都栽在這：函式改對了、接線沒接上，測試照樣全綠。"""
    src = (ROOT / "api.py").read_text(encoding="utf-8")
    assert "_no_focal = not _preferred_focal_side(zoning_result)" in src, "沒有計算旗標"
    assert 'zoning_result["_no_focal_wall"] = bool(_no_focal)' in src, "沒有寫回 zoning"
    assert 'no_focal_wall=(_no_focal and _rt == "living")' in src, "配對端沒收到"
    assert src.count("no_focal_wall=bool(") >= 2, "判官／清單端沒收到"
    tfp = (ROOT / "test_full_pipeline.py").read_text(encoding="utf-8")
    i = tfp.index("inputs = build_nano_banana_inputs(")
    assert "_no_focal_wall" in tfp[max(0, i - 900):i], "render entry 上沒掛旗標給 prompt_builder"


def test_flag_only_applies_to_the_living_room():
    """臥室／書房沒有「電視牆」這個概念，不能被這刀波及。"""
    src = (ROOT / "api.py").read_text(encoding="utf-8")
    assert '_rt == "living"' in src, "旗標沒有限定客廳"

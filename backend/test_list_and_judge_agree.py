# -*- coding: utf-8 -*-
"""判官驗的清單 = 客戶看到的清單；PDF 軟裝每風格印一次。

D7F52CB1（2026-08-18）四個房間全部 `ok=False`，理由分別是：
  客廳「購物清單中的單人躺椅未出現在圖上」——躺椅不在客戶清單裡
  臥室/書房「清單落地燈未出現」——燈具歸軟裝，主清單從來沒有它
根因：客戶清單在存檔前被 `_rendered_core_only` 依品類過濾，`must_products`
卻吃驗證當下的完整清單。兩邊各留一份品類表 ⇒ 判官永遠在追客戶看不到的品項。
我跟兩個審查者先後各追過一次那隻幽靈躺椅。

⚠️ 這不是放寬閘門：被濾掉的品項客戶沒買、圖上沒有也不影響交付；
   真正會賣給客戶的品類一件都沒少驗。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import api

ROOT = Path(__file__).resolve().parent.parent


def _entry(room_type: str, cats: list[str]) -> dict:
    return {
        "room_type": room_type,
        "matched_furniture": [
            {"category_en": c, "name_zh": f"測試{c}", "flux_descriptor": f"a {c}"}
            for c in cats
        ],
    }


def _must_cats(room_type: str, cats: list[str]) -> list[str]:
    out = api._product_fidelity_into_layout_ctx(None, _entry(room_type, cats)) or {}
    return [p["cat"] for p in (out.get("must_products") or [])]


# ── ① 判官只驗客戶看得到的品項 ─────────────────────────────────

def test_lighting_is_not_demanded_of_the_render():
    """🔴 燈具歸軟裝、主清單不列 ⇒ 判官不得要求它入圖。

    D7F52CB1 四間全被這條咬：臥室清單只有床/衣櫃/地毯/邊桌，
    判官卻回「清單落地燈未出現」。
    """
    got = _must_cats("bedroom", ["bed", "storage", "rug", "side_table", "lighting"])
    assert "lighting" not in got, f"判官仍在要求客戶沒買的燈具：{got}"
    assert {"bed", "storage", "rug", "side_table"} <= set(got), f"該驗的少了：{got}"


def test_accent_chair_is_not_demanded_in_living():
    """客廳的單人躺椅同理——不渲染、不顯示、不驗。"""
    got = _must_cats("living", ["sofa", "coffee_table", "rug", "media_console",
                                "accent_chair"])
    assert "accent_chair" not in got, f"幽靈躺椅又回來了：{got}"
    assert {"sofa", "coffee_table", "rug", "media_console"} <= set(got)


def test_every_sellable_category_is_still_checked():
    """🔴 反向護欄：真的會賣給客戶的品類，一件都不准少驗。"""
    for rt, cats in (("living", ["sofa", "coffee_table", "rug", "media_console"]),
                     ("bedroom", ["bed", "storage", "side_table", "rug"]),
                     ("dining", ["dining_table", "dining_chair", "rug"]),
                     ("study", ["table", "chair", "storage", "rug"])):
        got = set(_must_cats(rt, cats))
        assert got == set(cats), f"{rt} 漏驗了 {set(cats) - got}"


def test_unknown_room_type_falls_back_to_living():
    got = _must_cats("balcony", ["sofa", "rug"])
    assert "sofa" in got and "rug" in got


# ── ② 單一真相源：兩邊必須讀同一份表 ───────────────────────────

def test_one_shared_category_table():
    """🔴 兩邊各留一份就是這個病的病根，必須共用同一個函式。"""
    src = inspect.getsource(api)
    assert src.count("_DISPLAY_CATS_BY_ROOM") == 0, "run_pipeline 裡還留著第二份表"
    assert "def _display_cats_for_room" in src
    fid = inspect.getsource(api._product_fidelity_into_layout_ctx)
    assert "_display_cats_for_room" in fid, "判官那邊沒讀共用表"


def test_display_filter_and_judge_agree_on_every_room_type():
    """行為級對照：客戶清單留下的品類集合，必須等於判官驗的集合。"""
    pool = ["sofa", "coffee_table", "rug", "media_console", "accent_chair",
            "lighting", "bed", "storage", "side_table",
            "dining_table", "dining_chair", "table", "chair"]
    for rt in ("living", "bedroom", "dining", "study"):
        cats = api._display_cats_for_room(rt)
        shown = {c for c in pool if c in cats}
        judged = set(_must_cats(rt, pool))
        assert shown == judged, f"{rt}: 客戶看到 {shown}、判官驗 {judged}"


# ── ③ PDF 軟裝每風格一次 ───────────────────────────────────────

def test_pdf_prints_soft_furnishing_once_per_style():
    """🔴 四個房間拿到同一份軟裝（match_soft_furnishing 只吃 style）。

    每間印一次 ⇒ 手冊出現四遍一模一樣的清單。複製購物清單早就是每風格一次，
    PDF 必須同口徑。
    """
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    i = html.index("function downloadHandbook")
    seg = html[i:i + 6000]
    # 房間迴圈內不得再組軟裝表格。
    # ⚠️ 只看程式碼本體：註解裡提到 match_soft_furnishing 是說明，不是實作
    #    （我第一版連註解一起掃，紅在自己寫的說明上）。
    first = seg.index("g.items.forEach")
    second = seg.index("g.items.forEach", first + 1)      # 第二個是軟裝收集迴圈
    room_loop = chr(10).join(l for l in seg[first:second].splitlines()
                          if not l.strip().startswith("//"))
    assert "soft_furnishing" not in room_loop, "房間迴圈裡還在印軟裝"
    assert seg.count("更多搭配建議") == 1, "手冊裡的軟裝區塊不只一處"
    assert "不保證與渲染圖為同一款" in seg, "免責聲明掉了"
    # ⚠️ 只驗「有 _softSeen 這個變數」抓不到「去重的寫入被拿掉」，
    #    只驗「沒在房間迴圈印」也抓不到「整段永遠不輸出」——
    #    我第一版兩個破壞都沒紅。這兩條釘的是實際機制。
    assert "_softSeen[key] = 1" in seg, "去重的寫入被拿掉了（會印出重複軟裝）"
    assert "if (_soft.length) sections +=" in seg, "軟裝區塊沒有實際輸出（手冊會整段消失）"

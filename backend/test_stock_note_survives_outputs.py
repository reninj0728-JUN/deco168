# -*- coding: utf-8 -*-
"""商品名稱裡的「購買前請詢問現貨」要真的出現在三個出口。

背景：清祥 FLT-09LT01 的商品頁明寫「下單前請先詢問有無現貨」。目錄是固定 13 欄
schema（CLAUDE.md 禁止改），沒有欄位可以存這種限制，所以寫進 `name_zh`——
但那句在名稱**最後面、又是括號**，最容易被截斷吃掉。

🔴 「看程式沒有截斷」不等於「出口真的印得出來」。複製清單與設計手冊都用 node
   【實際執行】那兩個函式再檢查輸出（沿用 test_shopping_list_quantity 的 _run /
   _handbook 樁，不另寫一套）。只有結果頁需要 DOM 跑不動，退而檢查它實際組
   .f-name 的寫法與樣式。測試名稱已標明哪幾條是真執行。
   ⚠️ 手冊那條驗的是【內容】，不是列印排版——瀏覽器印成 PDF 時仍可能折行或溢出。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_shopping_list_quantity import NODE, _run, _handbook  # 重用既有 node 樁，不另寫一套

ROOT = Path(__file__).resolve().parent.parent
NOTE = "購買前請詢問現貨"
NAME = f"清祥 FLT-09LT01 香檳色橢圓大茶几（{NOTE}）"


def _render_with_the_item():
    return [{
        "style": "french", "style_label": "法式浪漫", "room_type": "living",
        "room_key": "living", "angle_label": "客廳",
        "matched_furniture": [{
            "name_zh": NAME, "brand": "清祥傢俱", "price_twd": 24800,
            "category": "茶几", "category_en": "coffee_table",
            "purchase_url": "https://www.hi-great.com.tw/page/product/show.aspx?kind=83&num=6087",
            "image_url": "https://www.hi-great.com.tw/upload/product/202207221839051.jpg",
        }],
        "soft_furnishing": [],
    }]


@pytest.mark.skipif(NODE is None, reason="沒有 node，無法真的執行 copyShoppingList")
def test_copy_shopping_list_really_prints_the_stock_note():
    """真執行：node 跑 copyShoppingList，取回的文字必須含現貨提醒。"""
    out = _run(_render_with_the_item())
    assert NAME in out, f"複製清單裡沒有完整商品名稱。實際輸出：\n{out[:400]}"
    assert NOTE in out, f"現貨提醒被吃掉了。實際輸出：\n{out[:400]}"


def test_catalog_record_actually_carries_the_note():
    """目錄裡那筆的名稱真的帶著提醒——不是只寫在腳本註解裡。"""
    import json
    cat = json.loads((ROOT / "backend" / "furniture_catalog_real.json")
                     .read_text(encoding="utf-8"))
    it = next((x for x in cat if x.get("id") == "higreat_FLT-09LT01"), None)
    assert it is not None, "清祥那件不在目錄裡"
    assert NOTE in str(it.get("name_zh")), f"名稱沒帶現貨提醒：{it.get('name_zh')}"
    assert NOTE not in str(it.get("keywords")), "提醒不該塞進 keywords（會污染配對關鍵字）"


def test_result_page_prints_the_whole_name():
    """結果頁的家具清單：直接輸出 name_zh，不得截斷，也不得用省略號藏尾巴。

    ⚠️ 不是真執行（需要 DOM）。驗的是 result.html 裡實際組 `.f-name` 的那幾處寫法。
    """
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    rows = re.findall(r'f-name">\'\s*\+\s*([^\n]{0,60})', html)
    assert rows, "result.html 找不到組 .f-name 的地方，測試目標錯了"
    for expr in rows:
        assert "name_zh" in expr, f".f-name 沒有用 name_zh：{expr.strip()}"
        assert "substr" not in expr and "slice" not in expr, \
            f".f-name 對名稱做了截斷：{expr.strip()}"
    css = re.search(r"\.f-name \{[^}]*\}", html)
    assert css, "找不到 .f-name 樣式"
    assert "text-overflow" not in css.group(0), \
        f".f-name 有 text-overflow，尾端的現貨提醒會被省略號吃掉：{css.group(0)}"


@pytest.mark.skipif(NODE is None, reason="沒有 node，無法真的執行 downloadHandbook")
def test_handbook_really_prints_the_stock_note():
    """真執行：node 跑 downloadHandbook()，攔下手冊 HTML，必須含現貨提醒。

    ⚠️ 這驗的是【手冊內容】，不是列印排版——瀏覽器把 HTML 印成 PDF 時
    仍可能因欄寬折行或溢出，那要人眼看一次。
    """
    out = _handbook(_render_with_the_item())
    assert NAME in out, f"手冊裡沒有完整商品名稱。前 400 字：\n{out[:400]}"
    assert NOTE in out, f"手冊把現貨提醒吃掉了。前 400 字：\n{out[:400]}"


def test_backend_delivery_payload_keeps_full_name():
    """最小 payload（_tiny_furn）也要保留完整 name_zh——降級存檔時不得掉字。"""
    api = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    i = api.index("def _tiny_furn")
    seg = api[i:i + 600]
    assert '"name_zh"' in seg, "_tiny_furn 沒有保留 name_zh"
    assert not re.search(r'name_zh[^\n]{0,30}\[:\d+\]', seg), \
        "_tiny_furn 對 name_zh 做了截斷"

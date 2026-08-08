# -*- coding: utf-8 -*-
"""上傳頁的影片文案只能講證得出來的效益。

A559DD2B（2026-08-08）實測，這是目前唯一一單有完整證據鏈的影片訂單：

  ✅ 影片確實進了 Gemini 的輸入
     log 有「[影片] 就緒 uri=files/…」，而三條退回純照片的路
     （上傳失敗／PROCESSING 超過 120s／上傳例外）都會把 uploaded_video 設成 None，
     那三行 log 一條都沒出現。「就緒」印得出來就一定會進 contents。

  ✅ 貢獻：格局完整度＋坪數估算
     四張照片裡**完全沒有廚房**（客廳那張只有落地窗、兩個門洞、黑色大門），
     analysis 卻寫出「開放式廚房」「廚房後方連通工作陽台」，而且
     room_dimensions.reference_used = 「門框高度與廚房流理台高度基準」、
     confidence=high。這些只能來自影片。

  ❌ 沒有貢獻：渲染
     `[zoning_v2] photos=1 video_frames=0`——幾何分區拿到 0 幀。
     底圖、家具配對、判官驗收全走照片。交付張數跟沒傳影片一樣。

所以文案可以講「格局」「坪數」，**不可以講「提升渲染品質」**。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "upload.html").read_text(encoding="utf-8")


def _video_block() -> str:
    i = HTML.index("空間影片（選填）")
    j = HTML.index("upload-formats", i)
    return HTML[i:j]


def test_video_copy_states_the_two_proven_benefits():
    """格局判讀＋坪數估算——這兩項 A559DD2B 有證據。"""
    blk = _video_block()
    assert "動線" in blk or "怎麼連" in blk, "沒講格局／動線這個已證實的效益"
    # ⚠️ 措辭是「可輔助」不是「更準」：我們只證明了影片提供照片沒有的資訊
    #    （廚房、工作陽台、流理台比例尺），**沒有做過 A/B 證明坪數一定更準**。
    #    有證據的宣稱才寫得死，沒證據的只能寫成輔助。
    assert "可輔助坪數估算" in blk, "沒講坪數這個已證實的輔助效益"
    assert "坪數估算更準" not in blk, (
        "把「可輔助」寫成「更準」了——那是沒做過 A/B 的過度宣稱")


def test_video_copy_manages_the_render_count_expectation():
    """🔴 客戶會以為傳影片就多幾張圖。實測：張數一模一樣。"""
    blk = _video_block()
    assert "設計圖張數不會因為上傳影片而增加" in blk, (
        "沒告訴客戶影片不會增加張數——他們會有錯誤期待")


def test_video_copy_never_claims_better_renders():
    """🔴 不得宣稱影片提升渲染品質。

    `[zoning_v2] photos=1 video_frames=0`：影片沒有參與 zoning、S2、底圖、
    家具配置或驗收。宣稱渲染變好是無法交付的承諾。
    """
    blk = _video_block()
    for lie in ("渲染品質", "圖更好", "設計圖更", "畫得更", "更精準的設計圖",
                "提升成品", "成品更"):
        assert lie not in blk, f"影片文案宣稱了證不出來的效益：「{lie}」"


def test_photos_are_still_declared_the_primary_source():
    """照片為主、影片為輔——這是 7/8 定案的產品原則，不得被文案稀釋。"""
    assert "照片仍是設計提案的主要依據" in _video_block()


def test_single_room_orders_are_still_told_not_to_upload():
    """單一空間的單後端直接丟棄影片，這個警告不得被新文案擠掉。"""
    blk = _video_block()
    assert "請不要上傳影片" in blk and "白等" in blk


def test_no_unclosed_bold_tags_in_the_video_block():
    blk = _video_block()
    assert blk.count("<b>") == blk.count("</b>"), "影片文案的 <b> 沒閉合"

# -*- coding: utf-8 -*-
"""照片單也要產出 architectural_features，PRESERVE 才鎖得住廚具。

7F0874C7：右牆的一字型開放廚房（上下櫃＋水槽＋爐具）在渲染圖裡整組消失，
換成一排電視櫃。Gemini 其實判對了（layout_notes 寫「右側為開放式一字型廚房」），
但那段資訊沒有進到給模型的保留指令。

🔴 根因：`architectural_features` 只有 analyze_space（純影片單）會產生。
   analyze_image——所有【照片單】走的那條路——從來不產這個欄位。
   `_build_preserve_clause` 的每一條保留指令都掛在它上面，所以照片單送出去的是
   「PRESERVE EXACTLY:」加一個空字串：門窗數量、廚具、天花管線全部沒鎖。
   （這也解釋為什麼「天花板長出一整排明管」那條專門寫的守門一直沒生效。）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_full_pipeline as tfp

FEATS = {
    "doors": "主入口在鏡頭後方，房門在左牆中段",
    "kitchen": "右牆一字型開放廚房，含上下櫃、水槽與爐具，長約 2.4m",
    "windows": "底端落地拉門通陽台",
    "ceiling": "外露消防明管與橫樑",
    "floor": "米白拋光石英磚",
    "walls": "白色平釘",
}
SRC = (Path(__file__).resolve().parent / "test_full_pipeline.py").read_text(encoding="utf-8")


def test_photo_analysis_asks_for_architectural_features():
    """🔴 照片單的分析必須產出這個欄位，否則 PRESERVE 永遠是空的。"""
    i = SRC.index("def analyze_image(")
    body = SRC[i:SRC.index("client = _get_client()", i)]
    assert '"architectural_features"' in body, \
        "analyze_image 的回傳格式沒有 architectural_features——照片單的保留指令會是空的"
    for field in ("doors", "kitchen", "windows", "ceiling", "floor", "walls"):
        assert f'"{field}"' in body, f"architectural_features 少了 {field}"


def test_kitchen_field_asks_for_the_cabinetry_not_just_a_location():
    """要鎖得住，就得知道廚具在哪面牆、多長，不是只回一句『有廚房』。"""
    i = SRC.index('"architectural_features"')
    seg = SRC[i:i + 700]
    assert "固定廚具" in seg and "上下櫃" in seg, f"kitchen 欄位問得太籠統：{seg[:200]}"


@pytest.mark.parametrize("mode", ["furnish", "full"])
def test_kitchen_is_locked_in_both_modes(mode):
    """買裝潢也不等於可以把廚房拆掉——固定設備兩種模式都要保。"""
    c = tfp._build_preserve_clause({"architectural_features": FEATS}, design_mode=mode)
    assert "FIXED built-in kitchen cabinetry" in c, f"{mode} 沒有鎖住廚具"
    for word in ("TV console", "sideboard", "sofa"):
        assert word in c, f"{mode} 沒有明講不准換成 {word}"
    assert FEATS["kitchen"] in c, "廚具的實際位置沒有寫進指令"


def test_kitchen_absent_does_not_emit_the_lock():
    """沒有廚房的房間不要硬塞這段（會變成叫模型畫一個不存在的廚房）。"""
    for val in ("無", "None", "none", "", "   "):
        c = tfp._build_preserve_clause({"architectural_features": {**FEATS, "kitchen": val}})
        assert "FIXED built-in kitchen" not in c, f"kitchen={val!r} 仍送出廚具保留指令"


def test_empty_features_does_not_emit_a_dangling_header():
    """一條 feature 都沒有時，不要只送一個「PRESERVE EXACTLY:」空標題。

    ⚠️ 這個檢查必須在 MODE 那行【之前】做——放後面的話 parts 永遠不只 1 個，
    條件不成立，等於沒修（我第一版就犯了這個錯）。
    """
    c = tfp._build_preserve_clause({"architectural_features": {}, "room_dimensions": {}})
    assert "PRESERVE EXACTLY:" not in c, "features 是空的卻還送出空標題"
    assert "STRICT RULES" in c, "反幻想那段不該被一起拿掉"


def test_features_present_still_emits_the_header():
    c = tfp._build_preserve_clause({"architectural_features": FEATS})
    assert "PRESERVE EXACTLY:" in c
    assert FEATS["ceiling"] in c and "do NOT add, duplicate or invent any extra pipes" in c

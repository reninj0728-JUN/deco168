# -*- coding: utf-8 -*-
"""模型說不準的時候，寬度守門就不該當它準；兩份尺規也不准各說各話。

背景（2026-09-22）：
  Gemini 回 `room_dimensions.confidence = high/medium/low`，但全站只有
  `_room_can_float_sofa` 在讀。`parse_max_width_cm` 完全不看 —— 模型自承
  不可靠的公尺數照樣拿去篩商品。

🔴 低信心【不能】往下掉到坪數那層：
   坪數估短邊是 √(坪×3.305)×0.85，7 坪就算出 4.09m ≥ 3.8 → 上限 300 →
   `filter_by_dimensions` 第一行就整段 return。那會從「拿不確定的數字篩」
   變成「完全不篩」，比修之前更糟。所以要直接回保守常數。

🔴 medium 不准解鎖 300：高估的代價不對稱——短邊 ≥3.8m 會同時關掉尺寸過濾
   【和】is_small_room 的加大沙發降權（is_small_room = max_w <= 240），
   兩把刀一起收；低估只是沙發偏小。寧可偏小。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from furniture_match import (
    SMALL_ROOM_MAX_WIDTH_DEFAULT,
    filter_by_dimensions,
    parse_max_width_cm,
)

BACKEND = Path(__file__).resolve().parent


def _dims(short_m, conf):
    return {"length_m": 8.0, "width_m": short_m, "confidence": conf}


def test_low_confidence_returns_the_conservative_constant():
    assert parse_max_width_cm("", _dims(4.0, "low")) == SMALL_ROOM_MAX_WIDTH_DEFAULT


def test_low_confidence_does_not_fall_through_to_ping_estimate():
    """🔴 這條是整支測試的重點：退錯地方會變成『完全不篩』。"""
    got = parse_max_width_cm("7-9坪", _dims(4.0, "low"))
    assert got == SMALL_ROOM_MAX_WIDTH_DEFAULT, f"掉到坪數層了：{got}"
    # 反證：坪數那層真的會給出「不過濾」的上限
    assert parse_max_width_cm("7-9坪", None) >= 280, "坪數層行為變了，這條註解要重寫"
    assert got < 280, "低信心竟然解鎖了不過濾"


def test_low_confidence_still_actually_filters():
    """上限要小到 filter_by_dimensions 真的會動作（>=280 它第一行就 return）。"""
    cap = parse_max_width_cm("7-9坪", _dims(4.0, "low"))
    items = [{"category": "沙發", "dimensions": "W300 x D95 x H85 cm"},
             {"category": "沙發", "dimensions": "W180 x D90 x H85 cm"}]
    assert len(filter_by_dimensions(items, cap)) == 1


@pytest.mark.parametrize("short_m", [4.0, 5.5, 9.0])
def test_medium_confidence_never_unlocks_the_no_filter_tier(short_m):
    got = parse_max_width_cm("", _dims(short_m, "medium"))
    assert got <= SMALL_ROOM_MAX_WIDTH_DEFAULT, f"medium 解鎖了 {got}"


def test_high_confidence_keeps_the_old_behaviour():
    """大房＋高信心才准放行，這是原本就有的行為，不能被這刀改掉。"""
    assert parse_max_width_cm("", _dims(4.0, "high")) == 300


@pytest.mark.parametrize("conf", ["high", "medium", "low", "", None])
def test_small_rooms_are_unaffected_by_confidence(conf):
    """小房本來就只給 200，不管信心如何都一樣——這刀不該動到它。"""
    d = _dims(2.5, conf)
    if conf is None:
        d.pop("confidence")
    assert parse_max_width_cm("", d) == (200 if conf != "low"
                                         else SMALL_ROOM_MAX_WIDTH_DEFAULT)


def test_missing_confidence_is_treated_as_not_high():
    """舊訂單沒有這個欄位 → 當成不是 high，不准解鎖 300。"""
    assert parse_max_width_cm("", {"length_m": 8.0, "width_m": 4.0}) <= \
        SMALL_ROOM_MAX_WIDTH_DEFAULT


# ── 三份尺規不准各說各話 ──────────────────────────────────────────
# 升級前有【三】份基準物清單，不是兩份：
#   A `test_full_pipeline.py`【空間量測步驟】—— 照片路徑的使用者 prompt
#   B `gemini_analyze.py`【精準尺規法】     —— system prompt（兩條路都會送）
#   C `gemini_analyze.py`【空間量測步驟】   —— 純影片路徑的使用者 prompt
#
# 🔴 第一版只改了 A、B，而且測試是用「整個檔案有沒有出現那句話」來驗——
#    C 跟 B 在同一個檔案裡，B 有新文字就讓 C 矇混過關。Grok 抓到的。
#    所以這裡改成**逐區塊**取出來驗，檔案層級的檢查擋不住這種漏改。
RULER_BLOCKS = (
    ("test_full_pipeline.py", "【空間量測步驟 — 必須先做】"),
    ("gemini_analyze.py", "【精準尺規法】"),
    ("gemini_analyze.py", "【空間量測步驟 — 必須先做】"),
)


def _block(fname: str, marker: str) -> str:
    """取出這一份尺規的內文：從標題到下一個【小節】為止。"""
    src = (BACKEND / fname).read_text(encoding="utf-8")
    i = src.index(marker)
    # chr(10) 而不是跳脫字元：這行用 heredoc 寫入時反斜線會被吃掉（踩過兩次）
    j = src.find(chr(10) + "【", i + len(marker))
    return src[i:j if j != -1 else i + 1500]


@pytest.mark.parametrize("fname,marker", RULER_BLOCKS)
def test_every_ruler_conditions_the_sofa_reference(fname, marker):
    """空屋照裡沒有沙發。三份都要寫成「有才用」，漏一份 prompt 就會互相打架。"""
    seg = _block(fname, marker)
    if "沙發" not in seg:
        pytest.skip(f"{fname}{marker} 沒把沙發列為基準物，本來就不會誤導")
    assert "真的有家具時才用" in seg, f"{fname}{marker} 仍無條件參考沙發"


@pytest.mark.parametrize("fname,marker", RULER_BLOCKS)
def test_every_ruler_has_calibrated_tile_method(fname, marker):
    """地磚是空屋最強的基準物，但**必須先校準**——假設規格會更自信地錯。"""
    seg = _block(fname, marker)
    assert "地板接縫" in seg, f"{fname}{marker} 沒有地磚法"
    assert "校準" in seg, f"{fname}{marker} 的地磚法沒要求先校準"
    assert "不要假設" in seg, f"{fname}{marker} 沒擋住『假設磚的規格』"


@pytest.mark.parametrize("fname,marker", RULER_BLOCKS)
def test_every_ruler_tells_the_model_low_confidence_is_safe(fname, marker):
    """要模型誠實填 low，就得讓它知道 low 不會被丟掉，而是換成保守值。"""
    seg = _block(fname, marker)
    assert "填 low 不會被" in seg, f"{fname}{marker} 沒說明誠實填 low 的後果"


def test_system_prompt_writes_into_the_declared_schema_field():
    """system prompt 原本叫模型給 estimated_length_m，schema 卻宣告
    room_dimensions.length_m —— 兩套欄位名，api.py 還得寫相容 shim 讀兩種。"""
    seg = _block("gemini_analyze.py", "【精準尺規法】")
    assert "room_dimensions" in seg and "length_m" in seg
    assert "estimated_length_m" not in seg, "又叫模型填舊的欄位名了"

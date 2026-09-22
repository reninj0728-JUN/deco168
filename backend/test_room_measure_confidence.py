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


# ── 兩份尺規不准各說各話 ────────────────────────────────────────────
# 升級前 `test_full_pipeline.py` 與 `gemini_analyze.py` 各有一份基準物清單，
# 兩份都把「標準沙發」列為基準物，而同一份 prompt 下面就寫著
# 「本產品目標客戶是空屋，多數照片裡不會有家具」。只改一份，system prompt
# 會把沙發那條再教回去。
RULERS = ("test_full_pipeline.py", "gemini_analyze.py")


@pytest.mark.parametrize("fname", RULERS)
def test_sofa_reference_is_conditional_not_assumed(fname):
    src = (BACKEND / fname).read_text(encoding="utf-8")
    i = src.index("沙發高")
    seg = src[i:i + 200]
    assert "真的有家具時才用" in seg, (
        f"{fname} 仍把沙發當成無條件基準物——空屋照裡根本沒有沙發")


@pytest.mark.parametrize("fname", RULERS)
def test_tile_method_requires_calibration_not_assumed_sizes(fname):
    """地磚可以用，但必須先校準；假設規格會比原本的門框反推更自信地錯。"""
    src = (BACKEND / fname).read_text(encoding="utf-8")
    i = src.index("地板接縫")
    seg = src[i:i + 400]
    assert "校準" in seg, f"{fname} 的地磚法沒有要求先校準"
    assert "不要假設" in seg, f"{fname} 沒有擋住『假設磚的規格』"


@pytest.mark.parametrize("fname", RULERS)
def test_prompt_tells_the_model_low_confidence_is_safe(fname):
    """要模型誠實回報 low，就得讓它知道填 low 不會被丟掉、而是換保守值。"""
    src = (BACKEND / fname).read_text(encoding="utf-8")
    assert "填 low 不會被" in src, f"{fname} 沒有告訴模型誠實填 low 的後果"


def test_system_prompt_writes_into_the_declared_schema_field():
    """system prompt 原本叫模型給 estimated_length_m，schema 卻宣告
    room_dimensions.length_m —— 兩套欄位名，api.py 還得寫相容 shim 讀兩種。"""
    src = (BACKEND / "gemini_analyze.py").read_text(encoding="utf-8")
    i = src.index("【精準尺規法】")
    seg = src[i:i + 900]
    assert "room_dimensions" in seg and "length_m" in seg
    assert "estimated_length_m" not in seg, "又叫模型填舊的欄位名了"

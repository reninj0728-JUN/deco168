# -*- coding: utf-8 -*-
"""固定廚具的保留指令，要真的出現在送給 fal 的那串 prompt 裡。

7F0874C7：畫面右牆的一字型開放廚房（上下櫃＋水槽＋爐具）在成品圖裡整組消失，
換成一排電視櫃。

🔴 第一次修錯地方。我把禁令加在 `_build_preserve_clause`，但那條只流進
   **舊 Flux 分支**（test_full_pipeline.py 的 "fal-ai/flux-pro/kontext"，
   payload 用 final_prompt）。現行的 gpt-image/sunburst 分支送的是
   `camera_constraints + inputs["system_prompt"] + inputs["prompt"]`，
   而 `inputs` 來自 build_nano_banana_inputs——它【沒有】收到 preserve_clause。
   所以「寫好禁令」不等於「模型收得到」。測試只驗 _build_preserve_clause 的文字
   就會全綠，那是假的安心。

這支測試驗兩件事：
  1. build_nano_banana_inputs 回的 prompt 真的帶這條（那就是 fal_args["prompt"] 的料）
  2. sunburst 的 payload 確實是用 inputs["prompt"] 組的（鏈路不能被默默改掉）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt_builder import build_nano_banana_inputs

ROOT = Path(__file__).resolve().parent
KITCHEN = "右牆一字型開放廚房，含上下櫃、水槽與爐具，長約 2.4m"
BASE = {"style": "luxury", "style_label": "現代輕奢", "flux_prompt": "x", "matched_furniture": []}
ZONING = {"living_zone": {"where": "左側"}, "walls": []}


def _prompt(entry, zoning=None, **kw):
    return build_nano_banana_inputs(entry, zoning, "https://x/a.jpg",
                                    room_type=kw.pop("room_type", "living"), **kw)["prompt"]


@pytest.mark.parametrize("zoning", [None, ZONING], ids=["無zoning", "有zoning"])
@pytest.mark.parametrize("mode", ["furnish", "full"])
def test_kitchen_lock_is_in_the_prompt(zoning, mode):
    """有無 zoning、furnish 或 full，都要帶到。

    ⚠️ 不能放進 _build_layout_section：那支只有帶 zoning 的單會跑，而且沒有 entry 參數
    （我第一版就放錯，有 zoning 的單會 NameError）。
    """
    p = _prompt({**BASE, "_fixed_kitchen": KITCHEN}, zoning, design_mode=mode)
    assert "FIXED KITCHEN" in p, f"mode={mode} zoning={bool(zoning)} 沒帶到廚具保留指令"
    for word in ("TV console", "sideboard", "sofa"):
        assert word in p, f"沒明講不准換成 {word}"


def test_no_kitchen_no_clause():
    """沒有固定廚具的房間不要塞這段。"""
    assert "FIXED KITCHEN" not in _prompt(dict(BASE))


def test_clause_is_self_guarding():
    """🔴 必須寫成『看得到才保留、看不到就別畫』。

    prompt 裡的 _scrub_offframe_rooms 之所以把廚房字眼全刪掉，就是因為「畫面外的
    廚房被寫進 prompt」會誘發模型憑空長出一個廚房。這條若寫成無條件「保留廚房」，
    等於把那個老毛病放回來。
    """
    p = _prompt({**BASE, "_fixed_kitchen": KITCHEN})
    i = p.index("FIXED KITCHEN")
    seg = p[i:i + 420]
    assert "VISIBLE" in seg and "image_1" in seg, "沒有綁定『在 image_1 裡看得到』"
    assert "do not add one" in seg, "沒有『看不到就別畫』的保護句"


def test_pipeline_attaches_the_kitchen_from_analysis():
    """analysis 的 architectural_features.kitchen 要掛到 entry 上，否則上面都是空談。"""
    src = (ROOT / "test_full_pipeline.py").read_text(encoding="utf-8")
    i = src.index("inputs = build_nano_banana_inputs(")
    seg = src[max(0, i - 700):i]
    assert '_fixed_kitchen' in seg, "呼叫 build_nano_banana_inputs 前沒有掛 _fixed_kitchen"
    assert 'architectural_features' in seg, "沒有從 analysis 取廚具資訊"
    assert '"無"' in seg, "沒有排除『無』——沒有廚房的房間會被硬塞保留指令"


def test_sunburst_payload_really_uses_inputs_prompt():
    """🔴 鏈路守門：gpt-image 的 payload 必須是用 inputs["prompt"] 組的。

    若有人改回 final_prompt 或另組一份，上面所有測試照樣全綠、模型卻收不到。
    """
    src = (ROOT / "test_full_pipeline.py").read_text(encoding="utf-8")
    i = src.index("fal_args = {")
    seg = src[i:i + 700]
    assert 'inputs["prompt"]' in seg, "gpt-image 的 payload 不是用 inputs['prompt'] 組的"
    assert "camera_constraints" in seg
    # 舊 Flux 分支才用 final_prompt，兩者不得混淆
    j = src.index('_fal_subscribe_timed(' + chr(10) + ' ' * 16 + chr(34) + 'fal-ai/flux-pro/kontext')
    assert '"prompt":              final_prompt' in src[j:j + 500], \
        "flux 分支的 final_prompt 不見了，preserve_clause 的註解要重寫"

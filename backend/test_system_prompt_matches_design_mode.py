# -*- coding: utf-8 -*-
"""furnish 的單，連 system prompt 都不准教模型做天花。

88cc4af 把 design_mode 送進分析端、在 user prompt 加了禁止建議硬裝工程的約束（原文只在 gemini_analyze）。
但 system_instruction 裡有一整段「梁柱因應」在教相反的事：

    modern → floating ceiling soffit, concealed beam, indirect cove lighting
    cream  → soft plaster beam wrap, warm ivory tone, indirect warm cove light above

同一個 request 裡兩邊打架，模型通常聽 system。1227725E 的「利用間接天花修飾梁柱」
幾乎就是第一行的中譯——所以光有 user prompt 的約束不夠，那是半刀。

🔴 三個坑，測試都要守住：
  1. 替換是字串比對。上游動了 SYSTEM_PROMPT 的字，替換就對不上，會【靜默】退回
     舊行為。所以要驗「12 段全部有換到」，不是只驗結果裡沒有禁詞。
  2. 快取若不分模式，第一張單是 furnish、第二張 full 就會拿到 furnish 那份
     （或反過來）。舊的 _SYSTEM_PROMPT 單一格快取正是這個問題。
  3. 十種風格每一種都要還有梁柱建議——把整段刪掉也會讓禁詞消失，
     但那是讓模型沒東西可講，不是我們要的。

⚠️ 這驗的是「送進去的教材對了」，不是「Gemini 一定服從」。舊單不回填。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini_analyze as ga
import test_full_pipeline as tfp

# 這些出現在 furnish 的教材裡＝在教客戶沒買的工程
# ⚠️ 不能用裸關鍵字「間接照明」判——furnish 版的【禁令句】本身就會提到它
#    （「間接照明屬裝修，這個模式不建議」）。要擋的是【教它去做】的那些句子。
BANNED_IN_FURNISH = [
    "indirect cove lighting", "indirect warm cove light", "coffered ceiling",
    "ceiling soffit", "concealed beam panel", "beam casing", "beam wrap",
    "moulding wrap", "flush ceiling", "painted beam white",
    "善用間接照明掩蓋低矮天花板",     # modern 的台灣特性原文
    "用深色或造型天花板化解",          # luxury 的台灣特性原文
]
STYLES = ["modern", "japanese", "luxury", "nordic", "muji",
          "art-deco", "cream", "wood", "french", "chinese-modern"]

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class _Stop(Exception):
    pass


@pytest.fixture(autouse=True)
def _clear_cache():
    """每條測試都從空快取開始，避免互相污染。"""
    ga._SYSTEM_PROMPT_CACHE.clear()
    yield
    ga._SYSTEM_PROMPT_CACHE.clear()


def _capture_system_instruction(monkeypatch, tmp_path, **kw) -> str:
    """真的跑 analyze_image，把送出去的 system_instruction 攔下來。

    不是讀原始碼——接線接錯、快取拿錯，只有真的執行才看得到。
    """
    img = tmp_path / "p.png"
    img.write_bytes(_PNG)
    seen: list[str] = []

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen.append(config.system_instruction)
            raise _Stop

    class _Client:
        models = _Models()

    monkeypatch.setattr(tfp, "_get_client", lambda: _Client())
    with pytest.raises(_Stop):
        tfp.analyze_image(str(img), ["modern"], **kw)
    assert seen and seen[0], "沒攔到 system_instruction"
    return seen[0]


# ── 替換本身 ────────────────────────────────────────────────────────
def test_every_swap_actually_applied():
    """🔴 12 段全部要對得上。對不上會靜默退回「教你做天花」的舊版本。"""
    _text, missing = ga._build_furnish_system_prompt()
    assert not missing, f"這幾段沒對上 SYSTEM_PROMPT 原文，仍是裝潢用語：{missing}"


@pytest.mark.parametrize("word", BANNED_IN_FURNISH)
def test_furnish_system_prompt_has_no_construction_vocab(word):
    assert word not in ga.system_prompt_for("furnish"), \
        f"furnish 的 system prompt 還在教「{word}」"


def test_full_system_prompt_keeps_construction_vocab():
    """full 是有買裝潢的單，不能被這刀誤傷。"""
    full = ga.system_prompt_for("full")
    assert "indirect cove lighting" in full
    assert "coffered ceiling" in full
    assert full == ga.SYSTEM_PROMPT, "full 模式不該對 SYSTEM_PROMPT 做任何加工"


@pytest.mark.parametrize("style", STYLES)
def test_furnish_still_gives_every_style_a_beam_answer(style):
    """不是把整段刪掉了事——刪掉禁詞也會消失，但模型就沒東西可講了。"""
    furnish = ga.system_prompt_for("furnish")
    # 標題那行自己結尾也有 ━━，要從標題行【之後】才開始找下一個區塊
    head = furnish.index("━━ 梁柱因應")
    body = furnish.index(chr(10), head) + 1
    section = furnish[body:furnish.index("━━", body)]
    line = next((l for l in section.splitlines() if l.startswith(style)), None)
    assert line, f"furnish 版少了 {style} 的梁柱建議"
    assert "→" in line and len(line.split("→", 1)[1].strip()) > 20, \
        f"{style} 的建議是空的：{line}"


def test_furnish_section_says_the_beam_stays():
    furnish = ga.system_prompt_for("furnish")
    assert "梁就是梁" in furnish and "不得提議包樑" in furnish


@pytest.mark.parametrize("mode", [None, "", "FURNISH", "unknown", 0])
def test_unknown_mode_gets_the_furnish_prompt(mode):
    assert ga.system_prompt_for(mode) == ga.system_prompt_for("furnish")


# ── 快取 ────────────────────────────────────────────────────────────
def test_cache_does_not_leak_between_modes():
    """🔴 舊的 _SYSTEM_PROMPT 是單一格快取：先 furnish 再 full 會拿到同一份。"""
    first = ga.system_prompt_for("furnish")
    second = ga.system_prompt_for("full")
    third = ga.system_prompt_for("furnish")
    assert first != second, "兩種模式拿到同一份 system prompt"
    assert third == first, "第二次要 furnish 卻拿到別的"
    assert "indirect cove lighting" not in third


# ── 真的送出去了嗎 ──────────────────────────────────────────────────
def test_analyze_image_sends_the_furnish_prompt(monkeypatch, tmp_path):
    got = _capture_system_instruction(monkeypatch, tmp_path, space_type="living")
    assert "indirect cove lighting" not in got
    assert "梁就是梁" in got


def test_analyze_image_sends_the_full_prompt(monkeypatch, tmp_path):
    got = _capture_system_instruction(monkeypatch, tmp_path,
                                      space_type="living", design_mode="full")
    assert "indirect cove lighting" in got, "full 的單拿到了 furnish 的教材"


def test_analyze_space_uses_the_mode_aware_prompt():
    """純影片的單走 analyze_space，不能還寫死 SYSTEM_PROMPT。"""
    src = (Path(__file__).resolve().parent / "gemini_analyze.py").read_text(encoding="utf-8")
    assert "system_instruction=system_prompt_for(design_mode)" in src
    assert "system_instruction=SYSTEM_PROMPT," not in src


def test_pipeline_no_longer_parses_source_text():
    """舊做法是去切 gemini_analyze.py 的原始碼字串，拿不到依模式調整後的版本。"""
    src = (Path(__file__).resolve().parent / "test_full_pipeline.py").read_text(encoding="utf-8")
    assert 'txt.split(' not in src, "還在切原始碼取 system prompt"
    assert "system_instruction=_get_system_prompt(design_mode)" in src

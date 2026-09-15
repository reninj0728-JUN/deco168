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
# 🔴 第一版只列了「梁柱因應」那段換掉的字，所以 12 段替換全綠、教材卻還有
#    recessed LED ceiling / double-height ceiling / cove light 在別頁示範——
#    「替換成功」被誤當成「整份教材沒有矛盾」。這份清單是【施作用語】全集，
#    由 test_final_furnish_prompt_teaches_no_construction 掃最終提示，
#    不是掃替換清單。
#
# ⚠️ 不能用裸關鍵字「間接照明」判——furnish 版的【禁令句】本身就會提到它
#    （「間接照明屬裝修，這個模式不建議」）。要擋的是【教它去做】的那些句子。
BUILT_WORK_VOCAB = [
    # 天花與燈槽
    "recessed", "cove", "soffit", "coffered ceiling", "ceiling panel",
    "double-height ceiling",
    # 牆面施作
    "feature wall", "accent wall", "texture wall", "wall panel", "wood panels",
    "veneer panels", "indirect wall wash", "moulding", "wainscot", "built-in",
    # 地坪施作
    "tile floor",
    # 需要配電的固定燈具：換吸頂燈是電路工程，preserve clause 也明寫要保留原有燈具
    "pendant", "chandelier", "sconce", "LED strip",
]

BANNED_IN_FURNISH = [
    "indirect cove lighting", "indirect warm cove light", "coffered ceiling",
    "ceiling soffit", "concealed beam panel", "beam casing", "beam wrap",
    "moulding wrap", "flush ceiling", "painted beam white",
    "善用間接照明掩蓋低矮天花板",     # modern 的台灣特性原文
    "用深色或造型天花板化解",          # luxury 的台灣特性原文
]
# 兩份清單刻意不同，不是筆誤：
#   · 梁柱因應那段列了 10 種，含已停售的 art-deco（舊資料，這刀不動它）
#   · 風格詞庫只有上線中的 9 種——直接綁 VALID_STYLES，停售/新增風格時自動跟上
BEAM_STYLES = ["modern", "japanese", "luxury", "nordic", "muji",
               "art-deco", "cream", "wood", "french", "chinese-modern"]
VOCAB_STYLES = list(tfp.VALID_STYLES)

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


@pytest.mark.parametrize("term", BUILT_WORK_VOCAB)
def test_final_furnish_prompt_teaches_no_construction(term):
    """🔴 驗【最終組出來的整份提示】，不是驗替換清單有沒有跑完。

    eb78d16 就是栽在這個差別上：12 段替換全部成功、測試全綠，但同一份教材的
    「正確格式」「風格詞庫」「空間規模規則」「few-shot 示範」還在示範
    recessed LED ceiling、double-height ceiling、warm 3000K cove light。
    一邊禁止、一邊示範，模型當然照示範走。
    """
    furnish = ga.system_prompt_for("furnish")
    assert term not in furnish, (
        f"furnish 的教材還在示範「{term}」——"
        f"user prompt 禁止、system 示範，模型會照示範走")


@pytest.mark.parametrize("style", VOCAB_STYLES)
def test_furnish_keeps_every_style_usable(style):
    """不是禁止描述原況，是不能指示新增或改造——清過頭一樣是壞掉。

    ⚠️ 第一版這條寫成「提示裡有沒有 natural light」，但每個 flux_prompt 的固定
    結尾都有 soft natural light，怎麼砍都矇得過去。改成查結構：十種風格每一種的
    材質／色調／燈光／家具四行都要還在、而且有東西。
    """
    furnish = ga.system_prompt_for("furnish")
    head = furnish.index(f" {style}】") if f" {style}】" in furnish else furnish.index(style + "】")
    block = furnish[head:head + 700]
    for field in ("材質：", "色調：", "燈光：", "家具："):
        line = next((l for l in block.splitlines() if l.startswith(field)), None)
        assert line, f"{style} 少了「{field}」整行"
        body = line.split("：", 1)[1].strip()
        assert len(body) >= 20 and "/" in body,             f"{style} 的「{field}」被清空或只剩一項：{line}"


def test_furnish_still_describes_the_existing_room():
    """原況詞彙要留著，否則模型看不懂房間——但不能用固定結尾那句去驗。"""
    furnish = ga.system_prompt_for("furnish")
    body = furnish.replace("soft natural light", "")   # 每條 flux_prompt 都有的樣板尾巴
    for keep in ("window", "daylight", "floor lamp", "rug"):
        assert keep in body, f"連描述原況的「{keep}」都被清掉了，清過頭"


def test_full_system_prompt_keeps_construction_vocab():
    """full 是有買裝潢的單，不能被這刀誤傷。"""
    full = ga.system_prompt_for("full")
    assert "indirect cove lighting" in full
    assert "coffered ceiling" in full
    assert full == ga.SYSTEM_PROMPT, "full 模式不該對 SYSTEM_PROMPT 做任何加工"


@pytest.mark.parametrize("style", BEAM_STYLES)
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


# ── guidance_scale 的真相 ──────────────────────────────────────────
def test_guidance_scale_is_not_sent_to_gpt_image():
    """🔴 別再拿 guidance=3.0 當「渲染端有保護」的證據。

    guidance_scale 是 fal-ai/flux-pro/kontext 的參數。現行 RENDER_MODEL 是
    gpt-image 系列，payload 只有 image_urls / prompt / quality / output_format /
    image_size——沒有 guidance_scale。furnish 的結構保留靠的是 preserve_clause
    的文字約束。2026-09-15 我拿它當證據講錯過一次，這條測試把事實釘住。
    """
    assert tfp._legacy_render_model_uses_guidance() is False, \
        "現行模型改成吃 guidance_scale 了？那要重新確認保護來源"
    src = (Path(__file__).resolve().parent / "test_full_pipeline.py").read_text(encoding="utf-8")
    # ⚠️ 用【實際呼叫】定位，不要只找模型字串——註解裡也會提到它，
    #    第一版就抓到自己寫的註解，測試因此誤紅。
    call = src.index('_fal_subscribe_timed(' + chr(10) + ' ' * 16 + chr(34) + 'fal-ai/flux-pro/kontext' + chr(34))
    assert chr(34) + 'guidance_scale' + chr(34) in src[call:call + 600], (
        'guidance_scale 不在 flux 呼叫裡了，註解要重寫')
    # gpt-image 的 fal_args 組裝處不得出現 guidance_scale
    i = src.index('fal_args = {')
    assert "guidance_scale" not in src[i:i + 900], \
        "gpt-image 的 payload 出現 guidance_scale——跟實測不符"


def test_furnish_preserve_clause_is_the_real_guard():
    """渲染端真正在保護結構的是這段文字，不是任何數字。"""
    clause = tfp._build_preserve_clause(None, design_mode="furnish")
    low = clause.lower()
    assert "do not" in low or "preserve" in low
    assert "furniture" in low
    full = tfp._build_preserve_clause(None, design_mode="full").lower()
    assert full != low, "furnish 與 full 的 preserve 指令一樣，等於沒分模式"

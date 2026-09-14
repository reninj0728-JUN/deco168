# -*- coding: utf-8 -*-
"""furnish 的單，空間摘要不准建議硬裝工程。

事故 1227725E：客戶買的是 furnish（只配家具、不動工），結果頁的空間摘要卻寫
「利用間接天花修飾梁柱」——那是木作＋水電的工程建議，客戶沒買、我們也不會做。
根因：`design_mode` 從 /api/job 一路傳到【渲染】端（api.py 有十幾處在傳），
唯獨【分析】端從來沒收到——analyze_image / analyze_space 連這個參數都沒有。

🔴 兩個坑，測試都要守住：
  1. 規則若寫進 `if space_type == "whole":` 分支裡，非全室的單會 NameError，
     整張單在第一步就死。所以每個 space_type 都要真的把 prompt 組出來。
  2. 規則文字若在 analyze_image / analyze_space 各抄一份，改一邊會漏另一邊。
     只准有 gemini_analyze.design_scope_rule() 這一份。

⚠️ 這裡驗的是「約束有送到 Gemini」，不是「Gemini 一定服從」。模型會不會守規矩
   要看真單，測不出來。舊訂單也不回填。
"""
from __future__ import annotations

import base64
import inspect
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini_analyze as ga
import test_full_pipeline as tfp

ROOT = Path(__file__).resolve().parent

# 這些字眼出現在 furnish 的建議裡＝賣客戶一個他沒買的工程
BANNED = ["間接照明", "木作天花", "包樑", "拆牆", "換地板", "油漆", "造型牆", "泥作", "水電"]

# 1×1 PNG，讓 load_img 有東西可讀（不打 API）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class _Stop(Exception):
    """prompt 已經組好了，不用真的打 Gemini。"""


def _capture_prompt(monkeypatch, tmp_path, **kw) -> str:
    """真的跑 analyze_image 到送出前一刻，把 prompt 攔下來。

    不是讀原始碼字串比對——NameError 這種錯只有真的執行才抓得到。
    """
    img = tmp_path / "p.png"
    img.write_bytes(_PNG)
    seen: list[str] = []

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen.extend(c for c in contents if isinstance(c, str))
            raise _Stop

    class _Client:
        models = _Models()

    monkeypatch.setattr(tfp, "_get_client", lambda: _Client())
    with pytest.raises(_Stop):
        tfp.analyze_image(str(img), ["modern"], **kw)
    assert seen, "沒攔到 prompt"
    return "\n".join(seen)


# ── 規則本身 ────────────────────────────────────────────────────────
def test_furnish_rule_forbids_construction():
    r = ga.design_scope_rule("furnish")
    assert "furnish" in r
    for w in BANNED:
        assert w in r, f"furnish 規則沒有禁止「{w}」"


def test_furnish_still_allows_describing_the_room():
    """只禁『建議施工』，不禁『描述現況』——有梁就是有梁，不准隱瞞。"""
    r = ga.design_scope_rule("furnish")
    assert "有大樑" in r and "可以" in r


def test_full_mode_allows_construction():
    r = ga.design_scope_rule("full")
    assert "full" in r
    assert "間接照明" not in r, "full 模式不該還在禁止硬裝"


@pytest.mark.parametrize("bad", [None, "", "FURNISH", "unknown", "  ", 0])
def test_unknown_mode_falls_back_to_furnish(bad):
    """未知值一律當 furnish：保守的那邊是少講一句，不是多賣一個工程。"""
    assert ga.design_scope_rule(bad) == ga.FURNISH_SCOPE_RULE


def test_rule_text_exists_only_once():
    """🔴 規則只准有一份。兩支函式各抄一份，改一邊就會漂移。"""
    hits = [f.name for f in ROOT.glob("*.py")
            if f.name not in ("test_design_scope_reaches_analysis.py",)
            and "嚴禁建議木作天花" in f.read_text(encoding="utf-8")]
    assert hits == ["gemini_analyze.py"], f"規則文字散落在多個檔案：{hits}"


# ── analyze_image（照片單走這條，客戶大多數的單）────────────────────
@pytest.mark.parametrize("space_type", sorted(tfp._SPACE_LABEL))
def test_rule_reaches_prompt_for_every_space_type(monkeypatch, tmp_path, space_type):
    """🔴 每一種 space_type 都要組得出 prompt。

    規則若被放進 `if space_type == "whole":` 裡，living/dining/bedroom/study/kitchen
    會 NameError——那不是少一句提醒，是整張單在第一步就失敗。
    """
    p = _capture_prompt(monkeypatch, tmp_path, space_type=space_type)
    assert "【設計範圍" in p, f"space_type={space_type} 的 prompt 沒有設計範圍規則"
    assert "嚴禁建議木作天花" in p


def test_full_mode_prompt_does_not_forbid_construction(monkeypatch, tmp_path):
    p = _capture_prompt(monkeypatch, tmp_path, space_type="living", design_mode="full")
    assert "含裝潢 full" in p
    assert "嚴禁建議木作天花" not in p, "full 的單仍被禁止建議裝修"


def test_both_output_fields_are_bound_to_the_rule(monkeypatch, tmp_path):
    """design_analysis 與 recommend_reason 都要指向這條規則，不能只綁一個。"""
    p = _capture_prompt(monkeypatch, tmp_path, space_type="living")
    for field in ("design_analysis", "recommend_reason"):
        line = next(l for l in p.split("\n") if f'"{field}"' in l)
        assert "設計範圍" in line, f"{field} 沒有綁到設計範圍規則：{line.strip()}"


# ── analyze_space（純影片單走這條）──────────────────────────────────
def test_analyze_space_takes_design_mode():
    assert "design_mode" in inspect.signature(ga.analyze_space).parameters


def test_analyze_space_prompt_carries_the_rule():
    """prompt 本體真的插了 {_scope_rule}，不是只加了個沒人用的參數。"""
    src = (ROOT / "gemini_analyze.py").read_text(encoding="utf-8")
    i = src.index("def analyze_space(")
    body = src[i:i + 12000]
    assert "_scope_rule = design_scope_rule(design_mode)" in body
    assert "{_scope_rule}" in body, "算了規則卻沒插進 prompt"


# ── 傳遞層：api.py 五個呼叫點 ───────────────────────────────────────
def test_every_analysis_call_site_passes_design_mode():
    """🔴 api.py 有五個進入分析的呼叫點，漏一個就有一類客戶收到工程建議。

    （影片+keyframes / Gemini URI / 照片+影片 / 純影片 / 純照片）
    """
    src = (ROOT / "api.py").read_text(encoding="utf-8")
    blocks = re.findall(r"analysis = (?:analyze_image|analyze_space)\((?:[^()]|\([^()]*\))*\)", src)
    assert len(blocks) == 5, f"分析呼叫點數量變了（{len(blocks)}），測試要重新對齊"
    for b in blocks:
        assert "design_mode=design_mode" in b, f"這個呼叫點沒傳 design_mode：\n{b}"

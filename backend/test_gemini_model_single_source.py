# -*- coding: utf-8 -*-
"""模型 id 只准有一份，而且每個生產呼叫點都要吃得到環境變數。

🔴 2026-09-22 的病：生產有 9 個 Gemini 呼叫點——
   8 個各自寫 `os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")`（預設值抄了八份），
   1 個寫死 `model="gemini-3.5-flash"`（`analyze_image`，**估房間長寬**那支），
   **完全不吃環境變數**。

   後果：在 Railway 設 `GEMINI_MODEL` 以為全站升級了，其實最需要準確度的
   量測呼叫點紋風不動，而且從外面看不出來。連 `_probe_gemini38.py` 的註解
   都寫「五個呼叫點共用」——沒有人對得起這個數字，因為它散在九個地方。

這支測試擋的就是「有人又複製一份預設值 / 又寫死一個模型名」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent

# 生產路徑上真的會呼叫 Gemini 的檔案。
# （scraper_* / poc_* / reclassify_* / _*.py 是離線工具，不在客戶請求的路徑上，
#   壞了只影響我自己跑腳本，不影響訂單，所以不納管。）
PRODUCTION = (
    "gemini_analyze.py",
    "zoning.py",
    "zoning_v2.py",
    "layout_contract_v1.py",
    "layout_geometry_verifier_s2.py",
    "test_full_pipeline.py",       # ⚠️ 名字叫 test_ 但它是 pipeline 核心，不是測試
)

_HARDCODED = re.compile(r'["\']gemini-[0-9]+(?:\.[0-9]+)*-[a-z]+["\']')


@pytest.mark.parametrize("fname", PRODUCTION)
def test_no_hardcoded_model_id(fname):
    """生產檔不准出現任何寫死的 gemini model id。"""
    src = (BACKEND / fname).read_text(encoding="utf-8")
    # 註解與 docstring 講歷史可以提到舊模型名，只擋「真的被當成值用」的。
    code = "\n".join(
        l for l in src.splitlines()
        if not l.lstrip().startswith("#") and "⚠️" not in l
    )
    hits = _HARDCODED.findall(code)
    assert not hits, (
        f"{fname} 又寫死了模型名 {hits}——改用 gemini_model()。"
        "模型 id 只准有一份，見 gemini_model.py 的說明。")


@pytest.mark.parametrize("fname", PRODUCTION)
def test_imports_the_single_source(fname):
    src = (BACKEND / fname).read_text(encoding="utf-8")
    assert "from gemini_model import gemini_model" in src, (
        f"{fname} 沒有接上唯一來源")


def test_env_var_overrides_every_call_site(monkeypatch):
    """設了 GEMINI_MODEL，每一個呼叫點都要跟著變——這正是舊版做不到的事。"""
    import gemini_model as gm
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-override")
    assert gm.gemini_model() == "gemini-test-override"


def test_blank_env_falls_back_to_default(monkeypatch):
    """Railway 上把變數設成空字串是常見手殘，不能因此送出空的 model id。"""
    import gemini_model as gm
    for blank in ("", "   "):
        monkeypatch.setenv("GEMINI_MODEL", blank)
        assert gm.gemini_model() == gm.DEFAULT_GEMINI_MODEL


def test_default_is_a_plausible_model_id():
    """預設值要長得像模型 id。

    ⚠️ 刻意**不**把版本號寫死在這裡——那又是複製一份預設值，
       升級時還要回來改這條，等於重蹈覆轍。只驗格式。
    """
    import gemini_model as gm
    assert re.fullmatch(r"gemini-[0-9]+(?:\.[0-9]+)*-[a-z]+", gm.DEFAULT_GEMINI_MODEL), (
        f"預設值長得不像模型 id：{gm.DEFAULT_GEMINI_MODEL!r}")


def test_measurement_call_site_is_env_driven():
    """🔴 最關鍵的一條：估房間尺寸的那支必須吃得到環境變數。

    它就是當初唯一寫死的那個，也是這整輪要升級的主角
    （`room_dimensions` 餵寬度守門、is_small_room、is_long_room 三處）。
    """
    src = (BACKEND / "test_full_pipeline.py").read_text(encoding="utf-8")
    i = src.index("def analyze_image")
    body = src[i:i + 40000]
    assert "model=gemini_model()" in body, "analyze_image 沒有用唯一來源取模型"

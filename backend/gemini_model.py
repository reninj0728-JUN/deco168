# -*- coding: utf-8 -*-
"""Gemini 模型 id 的唯一來源。

🔴 為什麼需要這個檔（2026-09-22）：
   升級前生產環境有 **9 個** Gemini 呼叫點——
   · 8 個各自寫 `os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")`，
     預設值複製了八份；
   · 1 個是**寫死** `model="gemini-3.5-flash"`（`test_full_pipeline.py`
     的 `analyze_image`，也就是**估房間長寬、給家具寬度守門用**的那一支），
     **完全不吃環境變數**。

   後果是：在 Railway 設 `GEMINI_MODEL` 只升級得了其中 8 個，
   最需要準確度的那個量測呼叫點紋風不動，而且從外面完全看不出來。
   （`_probe_gemini38.py` 的註解寫「五個呼叫點共用」——那份也是舊的，
     可見這個數字沒人對得起來，正是「複製一份預設值」的代價。）

⚠️ 模型 id 只准有一份。換版本＝改 `GEMINI_MODEL` 環境變數，
   不用改程式、不用重新部署；換壞了也是改環境變數就回得來，
   這就是這個檔存在的意義——把「升級模型」從一次部署降成一次設定。

⚠️ 這個模組**只准 import os**。它被 gemini_analyze / zoning / zoning_v2 /
   layout_contract_v1 / layout_geometry_verifier_s2 / test_full_pipeline
   共用，一旦 import 到專案內其他模組就可能繞出 import 迴圈。
"""
import os

# 生產預設。要臨時降版（例如新模型出事）不要改這裡，改 Railway 的 GEMINI_MODEL。
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def gemini_model() -> str:
    """目前該用的 Gemini 模型 id。

    每次呼叫都重讀環境變數，不在 import 時固定：
    Railway 改 env 本來就會重啟，但本機和測試要能 monkeypatch `os.environ`
    之後立刻生效，否則這個函式沒辦法被測。
    空字串或只有空白一律當成沒設定。
    """
    return (os.environ.get("GEMINI_MODEL") or "").strip() or DEFAULT_GEMINI_MODEL

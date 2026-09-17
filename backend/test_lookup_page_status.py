# -*- coding: utf-8 -*-
"""查詢頁不能叫客戶「繼續等」一個系統早就放棄的訂單。

EA8C677D：客廳圖生出來了、品檢沒過、Phase3 修完仍沒補上 → 後端寫
status=incomplete、progress=100、message=「主空間仍未通過配置驗收，請聯絡客服
免費重出」。**後端講得完全正確。**

🔴 但查詢頁把 incomplete 與 repairing 綁成同一個 `_partial`，兩者都顯示
   「部分交付．主空間仍在優化中」——而後端那句 message 只在「還在生成」的
   else 分支才拿得到，一進 incomplete 就被硬寫的字蓋掉。

   結果是同一張單兩套說法：
     成品頁 result.html →「請聯絡客服免費重出客廳」  ✅
     查詢頁 download.html →「主空間仍在優化中」      ❌ 客戶會一直等

⚠️ repairing 是真的還在跑，那句「仍在優化中」對它是正確的，不能一起改掉。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
HTML = (ROOT / "download.html").read_text(encoding="utf-8")


def _js(marker: str) -> str:
    """從 download.html 抽出一段 JS（跟其他前端測試同一套做法）。"""
    i = HTML.index(marker)
    depth, j, started = 0, i, False
    while j < len(HTML):
        if HTML[j] == "{":
            depth += 1; started = True
        elif HTML[j] == "}":
            depth -= 1
            if started and depth == 0:
                return HTML[i:j + 1]
        j += 1
    raise AssertionError(f"抽不出 {marker}")


def _status_text(status: dict) -> str:
    """真的跑那段狀態判斷，不是掃字串。"""
    block = HTML[HTML.index("var _partial = (status.status"):]
    block = block[:block.index("document.getElementById('statusText').textContent = _txt;")]
    # ⚠️ 這段夾著外層 `if (completed || _partial) {` 的開頭大括號，直接丟給 node 會
    #    SyntaxError: Unexpected end of input。那一行只決定要不要進來，真正的判斷
    #    是裡面的 if/else if/else，所以抽掉它就好。
    block = chr(10).join(
        l for l in block.splitlines()
        if "if (status.status === 'completed' || _partial) {" not in l)
    src = ("var status = JSON.parse(process.argv[2]);" + chr(10)
           + block + chr(10) + "console.log(_txt);")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.js"
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, str(f), json.dumps(status, ensure_ascii=False)],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"node 執行失敗：{r.stderr[:500]}"
        return r.stdout.strip()


pytestmark = pytest.mark.skipif(NODE is None, reason="沒有 node，無法真的執行查詢頁邏輯")


def test_incomplete_shows_the_backend_message():
    """🔴 系統已經放棄了，就照後端的話講——不要叫客戶繼續等。"""
    msg = "主空間仍未通過配置驗收，請聯絡客服免費重出"
    got = _status_text({"status": "incomplete", "progress": 100, "message": msg})
    assert got == msg, f"incomplete 沒有顯示後端訊息，而是：{got}"
    assert "優化中" not in got


def test_incomplete_without_a_message_still_says_what_to_do():
    got = _status_text({"status": "incomplete", "progress": 100})
    assert "優化中" not in got, f"後端沒給訊息時仍叫客戶等：{got}"
    assert "客服" in got, f"沒有告訴客戶下一步：{got}"


def test_repairing_still_says_it_is_being_optimised():
    """⚠️ repairing 是真的還在跑，這句對它是正確的，不能一起改掉。"""
    got = _status_text({"status": "repairing", "progress": 80,
                        "message": "主空間仍未通過配置驗收，請聯絡客服免費重出"})
    assert "優化中" in got, f"repairing 不該改口：{got}"


def test_completed_unchanged():
    assert _status_text({"status": "completed", "progress": 100}) == "✓ 設計完成"


def test_lookup_and_result_pages_tell_the_same_story():
    """兩頁不能一個叫他來找客服、一個叫他繼續等。"""
    res = (ROOT / "result.html").read_text(encoding="utf-8")
    assert "免費重出" in res, "成品頁的說法變了，測試目標要重新對齊"
    got = _status_text({"status": "incomplete", "progress": 100,
                        "message": "主空間仍未通過配置驗收，請聯絡客服免費重出"})
    assert "重出" in got or "客服" in got


# ── 縮圖與檔名要分得出是哪一間 ──────────────────────────────────
def test_thumbnail_label_includes_the_room():
    """三張臥室原本全部只標「現代輕奢」，客戶分不出哪張是哪間。"""
    i = HTML.index("var _style = render.style_label")
    seg = HTML[i:i + 400]
    assert "angle_label" in seg, "縮圖標籤沒有帶房間名"
    assert "_style + ' · ' + _room" in seg


def test_download_filename_is_filesystem_safe():
    """檔名帶了房間名，就要清掉作業系統不收的字元——否則瀏覽器會整個忽略
    download 屬性，退回拿網址結尾當檔名（三張又變成一樣）。"""
    i = HTML.index("a.download = String(name)")
    line = HTML[i:HTML.index("\n", i)]
    for ch in ("\\\\", "/", ":", "*", "?", "<", ">", "|"):
        assert ch in line, f"檔名清理漏了 {ch}"
    assert "·" in line, "中點沒有換掉，檔名會出現全形符號"

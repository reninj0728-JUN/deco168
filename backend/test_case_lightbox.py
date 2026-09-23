# -*- coding: utf-8 -*-
"""案例卡點下去要能「看」，不是被推去付款。

首頁的「設計案例」區原本六張卡整張都是 `onclick → upload.html`：
標題寫著案例，行為卻是購買按鈕。一個想看清楚一點的訪客點下去，
直接落進付款流程——比沒有案例區更傷信任。

⚠️ 清單只准放「查得到的單」。舊案例（0A3D74C7 那批）的後端資料早就被
   30 天保留期清掉、render URL 實測回 400，沒有依據就不要編一份清單出來。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="沒有 node，無法真的執行燈箱邏輯")

_STUB = """
const store = {};
function el() {
  return { style:{}, src:'', alt:'', hidden:true,
    set innerHTML(v){this._h=v}, get innerHTML(){return this._h||''},
    set textContent(v){this._t=v}, get textContent(){return this._t||''} };
}
global.document = { getElementById: id => (store[id] = store[id] || el()),
                    addEventListener(){}, body:{style:{}} };
global.window = { location: { href: '' } };
"""


def _run(js_tail: str) -> dict:
    m = re.search(r"<script>([\s\S]*?)</script>", HTML)
    assert m, "index.html 沒有 script 區塊"
    src = _STUB + m.group(1) + "\n" + js_tail
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.js"
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True,
                           encoding="utf-8")
        assert r.returncode == 0, f"node 執行失敗：{r.stderr[:600]}"
        return json.loads(r.stdout.strip().splitlines()[-1])


def test_case_cards_do_not_jump_to_the_payment_flow():
    """🔴 六張案例卡都不准是「整張卡連去 upload」。"""
    # ⚠️ 從 grid 的位置【往後】找收尾的 CTA——「立即體驗」這四個字在上面的
    #    CSS 註解裡也出現過，直接 index 會切出空字串（第一版就是這樣假綠）。
    i = HTML.index('<div class="examples-grid"')
    grid = HTML[i:HTML.index("立即體驗", i)]
    assert len(grid) > 500, "案例區切不出來，測試的錨點要重新對齊"
    cards = re.findall(r'<div style="position:relative;overflow:hidden;cursor:pointer;"[^>]*>', grid)
    assert len(cards) == 6, f"案例卡數量變了：{len(cards)}"
    for c in cards:
        assert "upload.html" not in c, f"案例卡又直接跳付款頁了：{c[:90]}"
        assert "openCase(" in c, f"案例卡沒有接上燈箱：{c[:90]}"


def test_traceable_case_shows_the_real_shopping_list():
    """能查證的那張單，要列出它真的配到的商品與合計。"""
    out = _run("""
      openCase('68036DF7');
      console.log(JSON.stringify({
        items: (store['caseList'].innerHTML.match(/case-item/g)||[]).length,
        total: (store['caseList'].innerHTML.match(/<strong>([^<]+)<\\/strong>/)||[])[1],
        links: (store['caseList'].innerHTML.match(/target="_blank"/g)||[]).length,
        opened: store['caseBox'].hidden === false,
        locked: document.body.style.overflow
      }));
    """)
    assert out["items"] == 4, out
    assert out["links"] == 4, "每一件都要能點到賣場"
    assert out["total"] == "NT$73,544", out
    assert out["opened"] and out["locked"] == "hidden"


def test_price_note_is_present_because_prices_age():
    """價格是抓取當下的快照，一定要寫「以賣場頁面為準」。"""
    out = _run("openCase('68036DF7');"
               "console.log(JSON.stringify({n: store['caseNote'].textContent}));")
    assert "以賣場頁面為準" in out["n"], out


def test_legacy_cases_do_not_invent_a_list():
    """⚠️ 舊案例後端資料已過期，只留成品圖——不准編一份清單出來。"""
    out = _run("""
      openCase('nordic');
      console.log(JSON.stringify({list: store['caseList'].innerHTML,
                                  note: store['caseNote'].textContent}));
    """)
    assert out["list"] == "", "替查不到的案例編了清單"
    assert "保留期限" in out["note"], out


def test_close_restores_page_scroll():
    out = _run("openCase('68036DF7'); closeCase();"
               "console.log(JSON.stringify({hidden: store['caseBox'].hidden,"
               "overflow: document.body.style.overflow}));")
    assert out["hidden"] is True and out["overflow"] == ""


def test_unknown_id_falls_back_to_upload():
    """資料找不到時不要開一個空燈箱，就照舊帶去上傳頁。"""
    out = _run("openCase('沒這個id');"
               "console.log(JSON.stringify({href: window.location.href}));")
    assert out["href"] == "upload.html", out

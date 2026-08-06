# -*- coding: utf-8 -*-
"""重拍卡到底會不會跳出來｜**真的執行 result.html 的 showDroppedNotice**。

293BDE11（2026-08-06）客廳零圖，客戶看到「這個拍攝角度…建議重拍」。
但該單照片拍得很正、S2 前檢也過，死因是模型憑空重建天花板——重拍不會改善。

`test_dropped_reason_and_reshoot_copy.py` 只掃字串（白名單有沒有寫對），
掃不到「這段 JS 跑起來到底輸出什麼」。這一檔補行為層：塞 dropped payload
進去真的跑，斷言重拍卡有沒有出現。

⚠️ 兩套等價判斷的教訓（GPT 2026-08-07）：原本 `_isReshoot = _preflight ||
_modellingFailed`，兩個變數的判斷式逐字相同。掃字串看不出重複，只有把
兩套都改壞才會露餡——所以這裡驗的是行為，重複與否交給 single-path 那條測。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "result.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_node_is_available_so_this_protection_actually_runs():
    """沒有 node 就整檔跳過＝保護消失而測試全綠。要放棄就明示 ALLOW_NO_NODE=1。"""
    if NODE is None and os.environ.get("ALLOW_NO_NODE") == "1":
        pytest.skip("已明示放棄前端行為驗證層")
    assert NODE, "找不到 node——重拍卡的行為驗證層無法執行"


def _extract_js() -> str:
    """取出 blockedPreviewHTML + showDroppedNotice 兩支函式。"""
    lines = HTML.splitlines()
    # 2026-08-07：重拍訊號抽成共用的 isModellingFailure()（客廳佔位卡吃同一份），
    # 片段要連常數與函式一起帶，否則跑起來是 ReferenceError 而不是行為驗證。
    sig = next(l for l in lines if "var _MODELLING_FAILURE_SIGNALS" in l
               and not l.lstrip().startswith("//"))
    imf_i = next(i for i, l in enumerate(lines) if "function isModellingFailure(" in l)
    imf_j = next(i for i, l in enumerate(lines) if i > imf_i and l.rstrip() == "  }")
    start = next(i for i, l in enumerate(lines) if "function blockedPreviewHTML" in l)
    sdn = next(i for i, l in enumerate(lines) if "function showDroppedNotice" in l)
    end = next(i for i, l in enumerate(lines) if i > sdn and l.rstrip() == "  }")
    src = "\n".join([sig] + lines[imf_i:imf_j + 1] + lines[start:end + 1])
    assert "showDroppedNotice" in src and "_isReshoot" in src, (
        "抽出來的片段不含要驗的邏輯——result.html 結構變了，這條要重寫")
    return src


HARNESS = r"""
const _boxes = [];
global._groups = [];
global.document = {
  getElementById: () => null,
  createElement: () => { const o = {innerHTML:"", style:{cssText:""}, className:""};
                         _boxes.push(o); return o; }
};
function run(dropped, message) {
  _boxes.length = 0;
  const vs = { dropped: dropped.length, dropped_renders: dropped, delivered: 2 };
  try { showDroppedNotice(vs, { message: message || "" }); }
  catch (e) { return "THREW:" + e.message; }
  return _boxes.map(b => b.innerHTML).join("\n");
}
const [payload, message] = JSON.parse(process.argv[2]);
process.stdout.write(run(payload, message));
"""


def _render(dropped: list[dict], message: str = "") -> str:
    import json
    js = _extract_js() + HARNESS
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h.js"
        p.write_text(js, encoding="utf-8")
        r = subprocess.run([NODE, str(p), json.dumps([dropped, message])],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert r.returncode == 0, f"node 執行失敗：{r.stderr[:500]}"
    out = r.stdout
    assert not out.startswith("THREW:"), f"showDroppedNotice 丟例外：{out}"
    return out


def _d(**kw) -> dict:
    base = {"failure_class": "render_quality", "layout_mode": "legacy_fallback",
            "room_type": "living", "triggered_hard_flags": []}
    base.update(kw)
    return base


# ── 付費前擋死 → 該講重拍 ──────────────────────────────────────────

@pytest.mark.parametrize("field,signal", [
    ("failure_class", "s2_preflight_blocked"),
    ("layout_mode", "s2_blocked_legacy"),
])
def test_preflight_signals_show_the_reshoot_card(field, signal):
    """反貧血：真的建模不了時，重拍卡必須還在——那條路本來就有用。"""
    html = _render([_d(**{field: signal})])
    assert "重拍" in html, f"{signal} 不再顯示重拍卡"
    assert "生成前" in html, (
        f"{signal} 沒告訴客戶「生成前就停了、沒產生錯誤設計圖」——"
        f"兩個訊號都會走進後端 _s2_preflight_blocked_result，這句對兩者都成立")


# ── 生成後的任何 hard flag → 不該講重拍 ─────────────────────────────

@pytest.mark.parametrize("flag", [
    "ceiling_changed",          # 293BDE11 的真死因
    "furniture_blocks_door",    # 黑名單版本漏掉的那批
    "walls_changed", "windows_changed", "sofa_focal_face_each_other",
])
def test_post_generation_hardfail_never_asks_for_reshoot(flag):
    """生成後的硬傷不是照片的錯，重拍不會改善——一律免費重出。"""
    html = _render([_d(triggered_hard_flags=[flag])])
    assert "重拍" not in html, f"{flag} 仍叫客戶重拍"
    assert "客服" in html, "沒給客服窗口，客戶不知道下一步"


def test_plain_legacy_fallback_is_not_reshoot_evidence():
    """legacy_fallback 是救援路徑不是失敗——A62AC21A 走同一條路成功交付。"""
    assert "重拍" not in _render([_d()])


def test_infrastructure_failure_is_not_the_customers_fault():
    assert "重拍" not in _render([_d(failure_class="infrastructure")])


def test_non_living_dropped_does_not_trigger_the_living_card():
    """臥室落選不該套用客廳零圖的紅卡文案。"""
    html = _render([_d(room_type="bedroom")])
    assert "重拍" not in html
    assert "設計不出安全" not in html


# ── 後端訊息原封轉貼的風險 ──────────────────────────────────────────

def test_backend_message_is_sanitised_before_display():
    """後端 message 直接進 innerHTML，尖括號必須先剝掉。

    ⚠️ `_msg` 只在重拍卡那一支才渲染，所以要用會走到那支的 payload 驗——
    拿 infrastructure 驗會假綠（訊息根本沒被輸出，`<script>` 當然不在裡面）。
    """
    html = _render([_d(failure_class="s2_preflight_blocked")],
                   "<script>alert(1)</script> 站在客廳一端重拍")
    assert "站在客廳一端重拍" in html, "後端訊息沒有被顯示，這條驗不到淨化"
    assert "<script>" not in html, "後端訊息未淨化就塞進 innerHTML"


def test_non_reshoot_branch_deliberately_does_not_echo_backend_message():
    """記錄現況：客廳零圖但非建模失敗時，前端用自己的文案，不轉貼後端 message。

    `_customer_safe_message` 的分類標籤（額度不足／逾時）是給客服與排查看的，
    存在 `dropped_renders[].exception_message`，不是走這條顯示路徑——
    2026-07-19 fal 餘額耗盡被誤診那次，缺的就是這個欄位。
    """
    html = _render([_d(failure_class="infrastructure")], "額度／餘額不足")
    assert "額度" not in html, (
        "非重拍分支開始轉貼後端 message 了——那條路徑沒有淨化以外的把關，"
        "改動前要先確認訊息來源都經過 _customer_safe_message")
    assert "客服" in html, "至少要給客服窗口"

# -*- coding: utf-8 -*-
"""購物清單的數量語意｜**真的執行 result.html 的 JS**，不是掃字串。

A62AC21A 之後加的 `qty` 是按「渲染張數」累加，但 `renders` 同時包含三種東西：
  ① 不同房間        → 三房各一張地毯，要買 3 張   ✅ 該累加
  ② 同一房間多視角  → 94105856 是同一個客廳 3 個角度，只要 1 張沙發
  ③ 不同風格方案    → 94105856 有 modern/muji 兩種風格，那是**替代方案**，
                      不是「兩套都買」

94105856 實測：6 張渲染＝同一客廳 × 3 視角 × 2 風格。照張數累加沙發會變 ×6，
客戶以為要買六張沙發、金額也膨脹 6 倍。

⚠️ 掃原始碼有沒有 `qty` / `×` / `合計` **抓不到這個 bug**（GPT 2026-08-05 指出），
所以這裡用 node 真的跑一次 `copyShoppingList`，斷言輸出文字。
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
HTML = (ROOT / "result.html").read_text(encoding="utf-8")
NODE = shutil.which("node")

def test_node_is_available_so_this_protection_actually_runs():
    """🔴 這一檔是購物清單數量語意的**唯一**驗證層（其他都是掃字串）。

    原本寫 `pytest.mark.skipif(NODE is None)`——沒有 node 就整檔靜默跳過，
    保護等於不存在而測試還是全綠。那正是這個專案一路在對抗的失敗模式，
    所以改成**明確失敗**。真的要在沒有 node 的環境跑，設 `ALLOW_NO_NODE=1`
    明示放棄這層保護，別讓它默默消失。
    """
    import os
    if NODE is None and os.environ.get("ALLOW_NO_NODE") == "1":
        pytest.fail("ALLOW_NO_NODE=1：已明示放棄購物清單數量語意的行為驗證")
    assert NODE is not None, (
        "找不到 node，購物清單數量語意（×N、多視角、多風格）完全沒有被驗證。"
        "裝 node，或設 ALLOW_NO_NODE=1 明示接受這個缺口。")


HARNESS = r"""
// 只抽出「複製購物清單」真正需要的東西單獨執行，不 eval 整頁——
// 整頁 eval 會被 DOM/location 樁的缺口打斷，測到的行為是假的。
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function grab(re, what) {
  const m = src.match(re);
  if (!m) { process.stderr.write('EXTRACT_FAIL: 找不到 ' + what); process.exit(2); }
  return m[0];
}
const roomMap   = grab(/var ROOM_TYPE_TO_ZONE = \{[\s\S]*?\};/, 'ROOM_TYPE_TO_ZONE');
const normalize = grab(/function normalizeRoomType\(rt\) \{[\s\S]*?\n  \}/, 'normalizeRoomType');
// 三個出口（複製清單／頁面預算／PDF）共用的金額與數量計算
const totals    = grab(/function planTotals\(renders\) \{[\s\S]*?\n  \}/, 'planTotals');
// ⚠️ _money 是單行函式，不能用 /\n  \}/ 收尾——那會一路吞到後面別的函式，
// 抽出來的東西看起來沒報錯、行為卻是假的（我第一版就踩了）。
const money     = grab(/function _money\(n\) \{[^\n]*\}/, '_money');
const copyFn    = grab(/function copyShoppingList\(\) \{[\s\S]*?\n  \}/, 'copyShoppingList');

let captured = null;
// ⚠️ 不要寫 global.navigator —— Node 21+ 有內建唯讀的 navigator，賦值會靜默失效，
// 結果傳進去的是內建物件（沒有 clipboard），copyShoppingList 會走 textarea 退路
// 而什麼都抓不到。用區域物件直接當參數傳。
const stubEl = { style: {}, classList: { add(){}, remove(){} }, value: '',
                 select() {}, remove() {}, appendChild() {} };
const doc = { createElement: () => stubEl, body: stubEl, execCommand: () => {} };
const nav = { clipboard: { writeText: (t) => { captured = t; return Promise.resolve(); } } };
const alertFn = () => {};

const runner = new Function('_hb', 'document', 'navigator', 'alert', `
  ${roomMap}
  ${normalize}
  ${grab(/function roomKeyOf\(r\) \{[\s\S]*?\n  \}/, 'roomKeyOf')}
  ${totals}
  ${money}
  ${copyFn}
  copyShoppingList();
`);
try {
  runner(data, doc, nav, alertFn);
} catch (e) {
  process.stderr.write('CALL_ERR: ' + (e && e.message));
}
process.stdout.write(captured === null ? '<<NO_OUTPUT>>' : captured);
"""


def _run(renders: list) -> str:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "h.js"
        h.write_text(HARNESS, encoding="utf-8")
        d = Path(td) / "d.json"
        d.write_text(json.dumps({"renders": renders}, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run([NODE, str(h), str(ROOT / "result.html"), str(d)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert p.returncode == 0, f"node 執行失敗：{p.stderr[:600]}"
        out = p.stdout
        assert out != "<<NO_OUTPUT>>", (
            f"copyShoppingList 沒有產生文字；node stderr：{p.stderr[:600]}")
        return out


TOTALS_HARNESS = r"""
// 直接驗共用計算 planTotals——頁面預算判定與 PDF 合計都吃它。
// 只驗複製清單不夠：那三個出口過去各算各的，修一個不會修到另外兩個。
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
function grab(re, what) {
  const m = src.match(re);
  if (!m) { process.stderr.write('EXTRACT_FAIL: ' + what); process.exit(2); }
  return m[0];
}
const runner = new Function('renders', `
  ${grab(/var ROOM_TYPE_TO_ZONE = \{[\s\S]*?\};/, 'ROOM_TYPE_TO_ZONE')}
  ${grab(/function normalizeRoomType\(rt\) \{[\s\S]*?\n  \}/, 'normalizeRoomType')}
  ${grab(/function roomKeyOf\(r\) \{[\s\S]*?\n  \}/, 'roomKeyOf')}
  ${grab(/function planTotals\(renders\) \{[\s\S]*?\n  \}/, 'planTotals')}
  var pt = planTotals(renders);
  var out = { multiStyle: pt.multiStyle, styles: {} };
  pt.styleOrder.forEach(function(s) {
    out.styles[s] = { total: pt.byStyle[s].total, roomCount: pt.byStyle[s].roomCount };
  });
  return JSON.stringify(out);
`);
process.stdout.write(runner(data.renders));
"""


def _totals(renders: list) -> dict:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "t.js"
        h.write_text(TOTALS_HARNESS, encoding="utf-8")
        d = Path(td) / "d.json"
        d.write_text(json.dumps({"renders": renders}, ensure_ascii=False), encoding="utf-8")
        p = subprocess.run([NODE, str(h), str(ROOT / "result.html"), str(d)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert p.returncode == 0, f"node 執行失敗：{p.stderr[:600]}"
        return json.loads(p.stdout)


def _f(name, price, url="https://x/1"):
    return {"name_zh": name, "price_twd": price, "purchase_url": url}


RUG = _f("條紋編織地毯", 7990)
SOFA = _f("現代風雙人皮沙發", 24990)


def test_quantity_wording_states_rooms_not_pieces():
    """🔴 商業承諾邊界：系統沒有商品數量辨識，`×N` 只代表「用在 N 個空間」。

    餐廳渲染出 4 張餐椅、主臥左右各一個床頭櫃，清單都只算 1 件。
    若把 `×3` 講成精確採購數量，成組家具客戶會少買、總價低估。
    """
    out = _run([
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "matched_furniture": [RUG]},
        {"style": "modern", "style_label": "現代簡約", "room_type": "bedroom",
         "matched_furniture": [RUG]},
    ])
    assert "× 2 個空間" in out, f"數量沒有標明單位是「空間」：\n{out}"
    assert "參考合計（每空間每款計 1 件）" in out, f"合計沒有標明計價方式：\n{out}"
    assert "非精確採購件數" in out, f"缺少「不是精確件數」的提醒：\n{out}"
    assert "餐椅" in out, f"沒有點名成組家具要自行確認數量：\n{out}"


def test_same_item_in_three_rooms_counts_three():
    """三個房間各一張地毯 → ×3，合計 23,970。"""
    out = _run([
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "angle_label": "客廳", "matched_furniture": [RUG]},
        {"style": "modern", "style_label": "現代簡約", "room_type": "bedroom",
         "angle_label": "主臥", "matched_furniture": [RUG]},
        {"style": "modern", "style_label": "現代簡約", "room_type": "study",
         "angle_label": "書房", "matched_furniture": [RUG]},
    ])
    assert "× 3" in out, f"三房沒有算成 ×3：\n{out}"
    assert "23,970" in out, f"合計沒有按數量計算：\n{out}"


def test_same_room_multiple_angles_counts_once():
    """🔴 同一個客廳的三個視角 → 沙發只要 1 張。"""
    out = _run([
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "angle_label": "客廳主視覺區", "matched_furniture": [SOFA]},
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "angle_label": "角度2", "matched_furniture": [SOFA]},
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "angle_label": "角度3", "matched_furniture": [SOFA]},
    ])
    assert "× 3" not in out and "× 2" not in out, f"多視角被誤算成多件：\n{out}"
    assert "24,990" in out, f"單張沙發的金額不對：\n{out}"


def test_room_type_aliases_are_one_room():
    """living_room 與 living 是同一間，不得算成兩間。"""
    out = _run([
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "matched_furniture": [SOFA]},
        {"style": "modern", "style_label": "現代簡約", "room_type": "living_room",
         "matched_furniture": [SOFA]},
    ])
    assert "× 2" not in out, f"living/living_room 被當成兩間房：\n{out}"


def test_different_styles_are_alternatives_not_summed():
    """🔴 94105856 的形狀：同一客廳 × 3 視角 × 2 風格 = 6 張渲染。

    兩種風格是**替代方案**，必須各自成一份清單、金額不可相加，
    而且每份裡的沙發都是 1 張。
    """
    def block(style, label):
        return [{"style": style, "style_label": label, "room_type": "living",
                 "angle_label": a, "matched_furniture": [SOFA]}
                for a in ("客廳主視覺區", "角度2", "角度3")]
    out = _run(block("muji", "無印簡約") + block("modern", "現代簡約"))

    assert "替代方案" in out, f"沒有標示風格是替代方案：\n{out}"
    assert "【無印簡約】方案" in out and "【現代簡約】方案" in out, f"沒有分成兩份：\n{out}"
    assert "× 6" not in out and "× 3" not in out and "× 2" not in out, (
        f"跨風格／跨視角被累加：\n{out}")
    # 兩份各自 24,990，不得出現相加後的 49,980 或 149,940
    assert "49,980" not in out and "149,940" not in out, f"風格金額被相加：\n{out}"
    assert out.count("24,990") >= 2, f"兩份方案的金額不對：\n{out}"


def test_soft_furnishing_never_in_main_total():
    """軟裝仍歸「更多搭配建議」，不進主家具合計。"""
    out = _run([
        {"style": "modern", "style_label": "現代簡約", "room_type": "living",
         "matched_furniture": [SOFA],
         "soft_furnishing": [_f("甜甜圈造型桌燈", 1799)]},
    ])
    assert "更多搭配建議" in out
    assert "不保證與渲染圖同款" in out
    main_total = re.search(r"參考合計（每空間每款計 1 件）：NT\$([\d,]+)", out)
    assert main_total, f"找不到主家具參考合計：\n{out}"
    assert main_total.group(1) == "24,990", (
        f"軟裝被併進主家具合計（應為 24,990，實際 {main_total.group(1)}）")


# ── 另外兩個出口：頁面預算判定 與 設計手冊 PDF ──────────────────────
# GPT 2026-08-06 抓到：我第一版只修了複製清單，`_budgetStatusHtml` 仍對每個
# 【視角】roomCount++ 且金額累加、PDF 的 grandTotal 又把【替代風格】相加。
# 措辭改對了但金額還是錯的。這組直接驗共用計算 planTotals。

def _six_render_shape():
    """94105856 的真實形狀：同一客廳 × 3 視角 × 2 風格 = 6 張。"""
    out = []
    for style, label in (("muji", "無印極簡"), ("modern", "都會簡約")):
        for angle in ("客廳主視覺區", "角度2", "角度3"):
            out.append({"style": style, "style_label": label, "room_type": "living",
                        "angle_label": angle, "matched_furniture": [SOFA, RUG]})
    return out


def test_budget_room_count_is_rooms_not_renders():
    """🔴 頁面預算：同一客廳 3 視角不得變成「全室 3 個空間」。"""
    t = _totals(_six_render_shape())
    for style, v in t["styles"].items():
        assert v["roomCount"] == 1, (
            f"{style} 的空間數算成 {v['roomCount']}（同一客廳 3 視角應為 1）")


def test_budget_total_not_multiplied_by_angles():
    """🔴 頁面預算：金額不得被視角數乘大，否則預算達標/超標判定整個錯。"""
    t = _totals(_six_render_shape())
    expect = 24990 + 7990
    for style, v in t["styles"].items():
        assert v["total"] == expect, (
            f"{style} 合計 {v['total']}，應為 {expect}（3 視角被乘成 {expect*3} 就是錯的）")


def test_pdf_does_not_sum_alternative_styles():
    """🔴 PDF 封面：兩種風格是替代方案，不得出現跨方案總和。"""
    t = _totals(_six_render_shape())
    assert t["multiStyle"] is True, "沒有辨識出多風格"
    assert len(t["styles"]) == 2
    # 各自 32,980；封面若相加會是 65,960，那是客戶不會付的數字
    assert sum(v["total"] for v in t["styles"].values()) == 65960, "fixture 前提變了"


def test_three_rooms_still_accumulate():
    """反貧血：真的三個房間就要累加，別把去重做過頭。"""
    t = _totals([
        {"style": "modern", "style_label": "現代", "room_type": r,
         "matched_furniture": [RUG]}
        for r in ("living", "bedroom", "study")
    ])
    v = t["styles"]["modern"]
    assert v["roomCount"] == 3, f"三房被算成 {v['roomCount']} 間"
    assert v["total"] == 7990 * 3, f"三張地毯合計應為 23,970，實際 {v['total']}"


def test_three_outlets_share_one_calculation():
    """三個出口必須吃同一份 planTotals，不得各算各的。

    ⚠️ **不要斷言呼叫時用的變數名**。我第一版寫了
    `assert "planTotals(rs)" in html`，而 `rs` 在 downloadHandbook 裡根本沒宣告
    ——那條斷言不但沒抓到 ReferenceError，還把壞掉的寫法鎖死了。
    真正的保證是上面那些「實際把函式跑起來」的測試。
    """
    assert HTML.count("planTotals(") >= 4, (
        "planTotals 沒有被三個出口共用（定義 1 次 + 至少 3 個呼叫）")
    for fn in ("copyShoppingList", "_budgetStatusHtml", "downloadHandbook"):
        m = re.search(rf"function {re.escape(fn)}\([^)]*\)(.*?)\n  \}}", HTML, re.S)
        assert m, f"找不到 {fn}"
        assert "planTotals(" in m.group(1), f"{fn} 沒有改吃共用計算"


# ── 設計手冊 PDF：必須真的呼叫，不能只掃字串 ────────────────────────
# 🔴 2026-08-06：`downloadHandbook` 裡寫成 `planTotals(rs)`，但 `rs` 沒有宣告，
# 點下載會 ReferenceError 整個功能壞掉。682 passed + node --check 都沒抓到，
# 因為未定義變數是**執行期**錯誤，而測試只掃了 `"planTotals(rs)" in HTML`
# ——那條斷言反而把壞掉的寫法鎖住。所以這裡真的把函式跑起來。

HANDBOOK_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
function grab(re, what) {
  const m = src.match(re);
  if (!m) { process.stderr.write('EXTRACT_FAIL: ' + what); process.exit(2); }
  return m[0];
}
let doc = null;
// downloadHandbook 走 window.open(...) 再往回傳的視窗 document.write
const popup = { document: { write: (h) => { doc = (doc || '') + h; }, close: () => {} } };
const win = { open: () => popup };
const runner = new Function('_hb', 'window', 'alert', `
  ${grab(/var ROOM_TYPE_TO_ZONE = \{[\s\S]*?\};/, 'ROOM_TYPE_TO_ZONE')}
  ${grab(/function normalizeRoomType\(rt\) \{[\s\S]*?\n  \}/, 'normalizeRoomType')}
  ${grab(/function roomKeyOf\(r\) \{[\s\S]*?\n  \}/, 'roomKeyOf')}
  ${grab(/function planTotals\(renders\) \{[\s\S]*?\n  \}/, 'planTotals')}
  ${grab(/function _money\(n\) \{[^\n]*\}/, '_money')}
  ${grab(/function _furnRows\([\s\S]*?\n  \}/, '_furnRows')}
  ${grab(/function downloadHandbook\(\) \{[\s\S]*?\n  \}/, 'downloadHandbook')}
  downloadHandbook();
`);
try {
  runner(data, win, () => {});
} catch (e) {
  process.stderr.write('CALL_ERR: ' + (e && e.message));
  process.exit(3);
}
process.stdout.write(doc === null ? '<<NO_OUTPUT>>' : doc);
"""


def _handbook(renders: list) -> str:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "hb.js"
        h.write_text(HANDBOOK_HARNESS, encoding="utf-8")
        d = Path(td) / "d.json"
        d.write_text(json.dumps({"renders": renders, "analysis": {"space_type": "whole"}},
                                ensure_ascii=False), encoding="utf-8")
        p = subprocess.run([NODE, str(h), str(ROOT / "result.html"), str(d)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert p.returncode == 0, f"downloadHandbook 執行失敗：{p.stderr[:500]}"
        out = p.stdout
        assert out != "<<NO_OUTPUT>>", f"沒有產生手冊內容；stderr：{p.stderr[:400]}"
        return out


def test_handbook_does_not_throw():
    """🔴 回歸鎖：`planTotals(rs)` 那種未宣告變數會讓下載整個壞掉。"""
    out = _handbook(_six_render_shape())
    assert "DECO168" in out and "專屬設計手冊" in out, "手冊內容不完整"


def test_handbook_cover_has_no_cross_style_total():
    """多風格時封面不得出現跨方案總價，要明說不可相加。"""
    out = _handbook(_six_render_shape())
    assert "金額不可相加" in out, "封面沒有標示替代方案不可相加"
    assert "65,960" not in out, "封面出現了兩個替代方案相加的金額"


def test_handbook_style_totals_are_deduped():
    """各風格章節末的合計要是去重後的金額，不是逐視角累加。"""
    out = _handbook(_six_render_shape())
    assert "32,980" in out, f"風格合計不是去重後的 32,980：\n{out[:800]}"
    assert "98,940" not in out, "風格合計被 3 個視角乘大了"


def test_handbook_single_style_still_shows_cover_total():
    """反貧血：單一風格時封面仍要有總計，別把功能改沒了。"""
    out = _handbook([
        {"style": "modern", "style_label": "現代", "room_type": r,
         "matched_furniture": [RUG]}
        for r in ("living", "bedroom", "study")
    ])
    assert "23,970" in out, f"單風格封面總計不見了：\n{out[:800]}"

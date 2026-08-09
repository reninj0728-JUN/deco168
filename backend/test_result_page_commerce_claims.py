# -*- coding: utf-8 -*-
"""結果頁的商業承諾：清單上的東西必須真的在圖上。

A62AC21A（2026-08-05）商業化前掃描抓到的兩件事，都在 `result.html`：

**① 頁首寫「4 個空間」但只有 3 個。**
後端有兩套房型命名——`rooms[]` 用 `living_room`，`renders[]` 用 `living`。
前端把兩邊丟進同一個 set，同一間客廳被算成兩間。客戶會以為少交一間。

**② 軟裝掛「前往購買」但圖上不是同一款。**
臥室清單寫「橘色甜甜圈桌燈／白色方形畫框」，成品是白色蘑菇燈＋金框橫幅。
根因兩層：
  a. 非客廳房型走 `prompt_builder._build_nonliving_nano_inputs`，那條路
     **完全沒有軟裝參考圖邏輯**，軟裝段落寫死 illustrative only。
  b. 前端有個 fallback：沒有 SOFT 錨定就硬把前 3 件當主清單、掛購買按鈕。
⚠️ 而且 `kind=SOFT` 只證明「參考圖有送出去」，不證明「模型畫了同款」
   （客廳盆栽：參考圖送了，圖上只能確認有植栽、不能確認同一個水泥盆），
   軟裝也**沒有任何商品級驗證層**。所以口徑定成：主清單只放 matched_furniture。

軟裝有三個出口，缺一個就漏一個：主展示、複製購物清單、設計手冊 PDF。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "result.html").read_text(encoding="utf-8")


# ── ① 空間數 ────────────────────────────────────────────────────────

def test_room_type_is_normalized_before_counting():
    assert "normalizeRoomType" in HTML, "沒有房型歸一化函式"
    assert "ROOM_TYPE_TO_ZONE" in HTML, "沒有 mirror 後端的映射表"
    # 兩個來源都要歸一化，只做一邊等於沒修
    body = HTML.split("function updateMetaSummary")[1][:900]
    assert body.count("normalizeRoomType(") >= 2, (
        "renders 與 data.rooms 兩個來源必須都歸一化")


@pytest.mark.parametrize("raw,zone", [
    ("living_room", "living"), ("living", "living"),
    ("dining_room", "dining"), ("study_workspace", "study"),
    ("bedroom", "bedroom"),
])
def test_mapping_table_matches_backend(raw, zone):
    """前端的映射表必須跟後端 api.ROOM_TYPE_TO_ZONE 同值，不得各寫各的。"""
    import api
    assert api.ROOM_TYPE_TO_ZONE[raw] == zone
    m = re.search(r"var ROOM_TYPE_TO_ZONE = \{(.*?)\};", HTML, re.S)
    assert m, "找不到前端映射表"
    assert re.search(rf"\b{raw}\s*:\s*'{zone}'", m.group(1)), (
        f"前端映射表缺 {raw}→{zone}")


def test_unknown_room_type_falls_back_to_itself():
    """未知房型要原值回傳，不能讓未來新增的房型憑空消失。"""
    m = re.search(r"function normalizeRoomType\(rt\) \{(.*?)\n  \}", HTML, re.S)
    assert m, "找不到 normalizeRoomType"
    assert "|| rt" in m.group(1), "未知房型沒有 fallback 回原值"


# ── ② 軟裝的三個出口 ────────────────────────────────────────────────

def test_soft_furnishing_never_enters_main_list():
    """主清單只放 matched_furniture；軟裝一律歸更多搭配建議。"""
    assert "var mainSoft = [];" in HTML, "軟裝仍可能進主清單"
    assert "mainSoft = soft.slice(0, 3)" not in HTML, (
        "『沒錨定就推前 3 件當主清單』的 fallback 還在——這正是掛錯購買按鈕的來源")


def test_soft_section_still_renders_when_main_is_empty():
    """🔴 陷阱回歸鎖：軟裝全降級後，區塊不得整個消失。

    舊版 `var softSection = mainSoftItems ? ... : ''`，mainSoft 一空整段
    就變空字串，客戶連建議都看不到。
    """
    assert "var softSection = (mainSoftItems || extraSoftItems)" in HTML, (
        "渲染條件仍只看 mainSoftItems，軟裝會整個消失")


def test_copy_shopping_list_separates_soft():
    """出口 2：複製清單不得把軟裝混進購物清單主體。"""
    m = re.search(r"function copyShoppingList\(\)(.*?)\n  \}", HTML, re.S)
    assert m, "找不到 copyShoppingList"
    body = m.group(1)
    assert "matched_furniture || []).concat(r.soft_furnishing" not in body, (
        "仍把軟裝直接併進購物清單")
    assert "更多搭配建議" in body, "複製內容沒有把軟裝標成建議"
    assert "不保證與渲染圖同款" in body, "複製內容缺少不保證同款的說明"


def test_handbook_pdf_marks_soft_as_suggestion():
    """出口 3：設計手冊 PDF 也會印軟裝＋購買連結，同樣要標示。"""
    assert "更多搭配建議（不併入主總計）" in HTML, "手冊仍寫「軟裝參考」而非建議"
    assert "不保證與渲染圖為同一款" in HTML, "手冊缺少不保證同款的說明"


def test_soft_section_heading_and_disclaimer():
    """主展示區的標題與說明要跟新口徑一致。"""
    assert "以下是依本風格挑選的軟裝" in HTML
    assert "<b>不保證與上方渲染圖為同一款</b>" in HTML


# ── ③ 付款欄位 ──────────────────────────────────────────────────────

def test_payment_date_label_renamed():
    """DB 的 paid_at/payment/order_no 全空、欄位讀的是 localStorage，
    金流接通前這個欄位不得掛「付款」二字。

    只驗 meta-label 元素本身，不掃整份檔案——註解裡會提到「付款」是正常的。
    """
    labels = re.findall(r'<span class="meta-label">([^<]+)</span>', HTML)
    assert labels, "找不到任何 meta-label"
    assert "付款日期" not in labels, f"仍有『付款日期』標籤：{labels}"
    assert "訂單日期" in labels, f"沒有改成訂單日期，目前是 {labels}"


def test_order_date_prefers_backend():
    """優先吃後端 created_at，localStorage 只當退路。"""
    m = re.search(r"function fillMeta\(\)(.*?)\n  \}\n", HTML, re.S)
    assert m, "找不到 fillMeta"
    body = m.group(1)
    assert "_hb.created_at" in body, "沒有優先讀後端建立時間"
    assert body.index("_hb.created_at") < body.index("parseInt(orderTime"), (
        "localStorage 仍排在後端資料前面")


def test_fill_meta_reruns_after_data_arrives():
    """🔴 假修回歸鎖：`fillMeta` 若只是 IIFE，執行時 `_hb` 還是 null，
    「優先吃後端」永遠走不到——而只驗字串順序的測試會全綠。

    必須是具名可重入函式，而且在 `_hb = data` 之後被再呼叫一次。
    """
    assert "  function fillMeta() {" in HTML, "fillMeta 不是具名函式（無法重入）"
    assert "(function fillMeta()" not in HTML, "fillMeta 仍是 IIFE，只會跑一次"
    idx_assign = HTML.index("_hb = data;")
    after = HTML[idx_assign:idx_assign + 600]
    assert "fillMeta()" in after, "拿到資料後沒有重新呼叫 fillMeta，後端日期永遠不會顯示"


def test_backend_result_api_returns_created_at():
    """光修前端沒用——result endpoint 原本直接回 result_json，裡面沒有 created_at。"""
    import inspect
    import api
    src = inspect.getsource(api.get_result)
    assert 'row.get("created_at")' in src, "result API 沒有把 DB 的 created_at 帶出去"
    assert '"created_at": row["created_at"]' in src, "沒有把 created_at 併進回傳"


# ── ④ 複製清單的跨房數量 ────────────────────────────────────────────

def test_copy_list_quantity_semantics_are_behaviourally_tested():
    """數量語意不在這裡掃字串驗——掃字串抓不到語意錯。

    第一版我寫了 `assert "seen[key].qty += 1" in body`，它對「按渲染張數累加」
    這個 bug 完全無感（94105856 是同一客廳 × 3 視角 × 2 風格 = 6 張，
    照張數累加沙發會變 ×6）。真正的驗證在 `test_shopping_list_quantity.py`，
    那裡用 node 實際執行 `copyShoppingList` 並斷言輸出文字。

    這條只確保那組行為測試還在，不會被悄悄刪掉。
    """
    beh = Path(__file__).resolve().parent / "test_shopping_list_quantity.py"
    assert beh.exists(), "數量語意的行為測試不見了"
    src = beh.read_text(encoding="utf-8")
    for required in ("test_same_item_in_three_rooms_counts_three",
                     "test_same_room_multiple_angles_counts_once",
                     "test_different_styles_are_alternatives_not_summed"):
        assert required in src, f"行為測試缺少 {required}"


def test_three_outlets_share_one_total_calculation():
    """🔴 金額與數量必須只有一份計算來源。

    我第一版只修了複製清單，`_budgetStatusHtml` 仍對每個【視角】roomCount++
    且金額累加、PDF 的 grandTotal 又把【替代風格】相加——措辭改對了、金額還是
    錯的（GPT 2026-08-06 抓到）。三個出口各算各的，修一個不會修到另外兩個。
    """
    m = re.search(r"function planTotals\(renders\) \{(.*?)\n  \}", HTML, re.S)
    assert m, "找不到共用計算 planTotals"
    body = m.group(1)
    # 2026-08-09：房間身分改吃 room_key（哪一間），不是 room_type（哪一類）——
    # 臥室 1 與臥室 2 的 room_type 都是 bedroom，用 room_type 分組會少算一間。
    assert "roomKeyOf(r)" in body, "沒有用實際房間去重（多視角會被誤算）"
    assert "rooms[room] = 1" in body, "同房多視角沒有收斂成一份"
    assert "Object.keys(e.rooms).length" in body, "數量不是用『幾個房間』算的"

    # ⚠️ 這裡只驗「有沒有呼叫共用計算」，**不得把呼叫時的變數名寫進斷言**。
    # 我第一版寫了 `assert "planTotals(rs)" in HTML`，而 `rs` 在
    # downloadHandbook 裡根本沒宣告——測試不但沒抓到 ReferenceError，
    # 還把壞掉的寫法鎖住了（GPT 2026-08-06 抓到）。真正的保證在
    # test_shopping_list_quantity.py 那組「實際呼叫函式」的行為測試。
    assert HTML.count("planTotals(") >= 4, (
        "planTotals 沒有被三個出口共用（定義 1 次 + 至少 3 個呼叫）")
    for fn_name, who in (("copyShoppingList", "複製購物清單"),
                         ("_budgetStatusHtml", "頁面預算判定"),
                         ("downloadHandbook", "設計手冊 PDF")):
        m2 = re.search(rf"function {re.escape(fn_name)}\([^)]*\)(.*?)\n  \}}", HTML, re.S)
        assert m2, f"找不到 {fn_name}"
        assert "planTotals(" in m2.group(1), f"{who} 沒有改吃共用計算，會自己算出錯的金額"

    # 舊的逐視角累加寫法不得復活
    assert "f.forEach(function(it) { styleTotal += (it.price_twd || 0); });" not in HTML, (
        "頁面預算又在逐視角累加金額")
    assert "if (f.length) roomCount++;" not in HTML, "頁面預算又在逐視角數空間"


def test_pdf_cover_never_sums_alternative_styles():
    """PDF 封面不得出現跨替代方案的總價（客戶不會付那個數字）。"""
    assert "if (!_pt.multiStyle) grandTotal += styleTotal;" in HTML, (
        "多風格時仍把各方案金額加進封面總計")
    assert "金額不可相加" in HTML, "封面沒有明說替代方案不可相加"

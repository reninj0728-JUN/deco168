# -*- coding: utf-8 -*-
"""客廳佔位卡｜**真的執行 buildUI + renderPanelContent**，不是掃字串。

產品鐵則：有客廳就一定要有客廳，而且在第一頁。
後端早就實作了一半（`living_incomplete` → status=incomplete，commit 3cc5ad6，
293BDE11 跑的就是它、也確實標了 incomplete），但前端在客廳零圖時直接拿餐廳
當第一張——兩端語意不一致。293BDE11 的客戶看到的第一頁就是餐廳。

排序規則（`_RT_ORDER`）本身沒壞：它只能排「已交付的圖」，變不出不存在的客廳。
所以缺的是一張**佔位卡**。

🔴 佔位卡是純 UI，絕不進資料層。這一檔的重點有一半在驗「它沒有污染什麼」：
   金額、空間數、購物清單都不得把佔位算成一個空間或一件商品。
   （我這幾天才剛把「×N ＝使用於 N 個空間」的語意鎖死，多一張假 render 就破功。）
"""
from __future__ import annotations

import json
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
    if NODE is None and os.environ.get("ALLOW_NO_NODE") == "1":
        pytest.skip("已明示放棄前端行為驗證層")
    assert NODE, "找不到 node——佔位卡的行為驗證層無法執行"


_STRIP = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|//.*$")


def _line(marker: str) -> str:
    """單行常數宣告（沒有大括號可數）。"""
    lines = HTML.splitlines()
    hit = [l for l in lines if marker in l and not l.lstrip().startswith("//")]
    assert len(hit) == 1, f"{marker} 找到 {len(hit)} 行，預期剛好 1 行"
    return hit[0]


def _slice(start_marker: str) -> str:
    """從 start_marker 那一行起，**數大括號**切到真正的結尾。

    ⚠️ 第一版拿「縮排剛好兩空格的 `}`」當結尾，遇到內部有同縮排區塊的函式就切錯，
    node 直接 `Unexpected end of input`。靠版面猜邊界不可靠——數括號才是對的，
    而且要先把字串與行註解剝掉，否則 HTML 片段裡的 `{` 會把深度算歪。
    """
    lines = HTML.splitlines()
    i = next(k for k, l in enumerate(lines) if start_marker in l)
    depth, started, out = 0, False, []
    for line in lines[i:]:
        out.append(line)
        bare = _STRIP.sub("", line)
        depth += bare.count("{") - bare.count("}")
        if "{" in bare:
            started = True
        if started and depth <= 0:
            break
    assert started and depth <= 0, f"切不出完整區塊：{start_marker}"
    return "\n".join(out)


def _harness() -> str:
    """把佔位卡會用到的函式整段抓出來（依 result.html 的定義順序）。"""
    parts = [
        # ⚠️ 用 result.html 裡真正的那份對照表，不要在測試裡自己編一個假的
        # ——假常數會讓房型正規化的行為跟線上不一致，測到的就不是真的。
        _slice("var ROOM_TYPE_TO_ZONE"),
        _slice("var _RT_ORDER = {"),
        _slice("function _roomRank("),
        _line("var _MODELLING_FAILURE_SIGNALS"),
        _slice("function isModellingFailure("),
        _line("var LIVING_PLACEHOLDER_RT"),
        _slice("function shouldBuildUI("),
        _slice("function _livingPlaceholder("),
        _slice("function normalizeRoomType("),
        _slice("function planTotals("),
        _slice("function updateMetaSummary("),
        _slice("function buildUI("),
        _slice("function livingPlaceholderHTML("),
        _slice("function _thumbHtml("),
        _slice("function _budgetStatusHtml("),
        _slice("function renderPanelContent("),
    ]
    src = "\n\n".join(parts)
    for need in ("_placeholder", "isModellingFailure", "planTotals", "buildUI"):
        assert need in src, f"抽出來的片段缺 {need}——result.html 結構變了，這檔要重寫"
    return src


HARNESS_TAIL = r"""
var _groups = [], _angleIdx = {}, _hb = null;
var _els = {};
global.localStorage = { getItem: function(){ return 'A'; } };
global.document = {
  getElementById: function(id){
    if (!_els[id]) _els[id] = { innerHTML:"", textContent:"", style:{cssText:""} };
    return _els[id];
  },
  createElement: function(){ return { innerHTML:"", style:{cssText:""}, className:"" }; }
};
function _money(n){ return String(n); }

var IN = JSON.parse(process.argv[2]);
// 走跟載入處一樣的決策，不要繞過它——繞過去就測不到「零交付要不要建 UI」
var _built = shouldBuildUI(IN.renders, IN.dropped);
if (_built) buildUI(IN.renders, IN.dropped);
var out = {
  built: _built,
  firstAngleOfEachStyle: _groups.map(function(g){
    var a = g.angles[0] || {};
    return { style: g.style, room_type: a.room_type, placeholder: !!a._placeholder };
  }),
  angleLabels: _groups.map(function(g){
    return g.angles.map(function(a){ return a.angle_label; });
  }),
  panels: _groups.map(function(g){ return renderPanelContent(g); })
};
// 金額/空間數走的是原始 renders（跟正式呼叫端一致）
var pt = planTotals(IN.renders);
out.totals = {};
Object.keys(pt.byStyle).forEach(function(st){
  var g = pt.byStyle[st];
  out.totals[st] = { rooms: Object.keys(g.rooms).length, items: g.order.length };
});
// 防呆：就算誤傳含佔位卡的 g.angles 進去，金額也不能變
var ptDirty = planTotals([].concat.apply([], _groups.map(function(g){ return g.angles; })));
out.totalsDirty = {};
Object.keys(ptDirty.byStyle).forEach(function(st){
  var g = ptDirty.byStyle[st];
  out.totalsDirty[st] = { rooms: Object.keys(g.rooms).length, items: g.order.length };
});
// ⚠️ rooms 要帶真的。293BDE11 的 payload 有 rooms[].room_type='living_room'，
// updateMetaSummary 會把它算進 roomSet——傳空陣列等於少算一間規劃空間，
// 我第一版的「端到端」就是這樣印出錯的 3（GPT 2026-08-07 抓到）。
updateMetaSummary({ renders: IN.renders, rooms: IN.rooms || [] });
out.metaPlan = _els['metaPlan'] ? _els['metaPlan'].textContent : '';

// 佔位卡在任何一個風格裡出現過嗎（不是只看第一個位置）
out.placeholderCount = _groups.reduce(function(n, g){
  return n + g.angles.filter(function(a){ return a._placeholder; }).length;
}, 0);

// 🔴 直接打守門：造一張「帶商品的佔位卡」——未來有人給佔位卡塞商品時，
// planTotals / updateMetaSummary 必須靠自己的 _placeholder 判斷擋住，
// 不能靠「佔位卡剛好沒有商品」這個巧合。
var poisoned = {
  _placeholder: true, style: 'french', style_label: '法式浪漫',
  room_type: 'living', angle_label: '客廳（優化中）',
  matched_furniture: [{ name_zh: '假沙發', price_twd: 99999,
                        purchase_url: 'https://x/fake' }],
  soft_furnishing: []
};
var ptP = planTotals(IN.renders.concat([poisoned]));
out.totalsPoisoned = {};
Object.keys(ptP.byStyle).forEach(function(st){
  var g = ptP.byStyle[st];
  out.totalsPoisoned[st] = { rooms: Object.keys(g.rooms).length, items: g.order.length };
});
_els['metaPlan'] = { textContent: '' };
updateMetaSummary({ renders: IN.renders.concat([poisoned]), rooms: IN.rooms || [] });
out.metaPlanPoisoned = _els['metaPlan'].textContent;

process.stdout.write(JSON.stringify(out));
"""


def _run(renders: list[dict], dropped: list[dict], rooms=None) -> dict:
    js = _harness() + HARNESS_TAIL
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h.js"
        p.write_text(js, encoding="utf-8")
        r = subprocess.run([NODE, str(p), json.dumps({"renders": renders,
                                                      "dropped": dropped,
                                                      "rooms": rooms or []})],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"node 執行失敗：{r.stderr[:900]}"
    return json.loads(r.stdout)


def _render(room_type, style="french", **kw):
    d = {"style": style, "style_label": "法式浪漫", "room_type": room_type,
         "angle_label": {"dining": "餐廳", "bedroom": "主臥室",
                         "study": "書房", "living": "客廳"}[room_type],
         "render_url": f"https://x/{room_type}.jpg",
         "matched_furniture": [{"name_zh": f"{room_type}沙發", "price_twd": 10000,
                                "purchase_url": f"https://s/{room_type}"}],
         "soft_furnishing": []}
    d.update(kw)
    return d


def _dropped(room_type="living", style="french", **kw):
    d = {"style": style, "room_type": room_type, "failure_class": "render_quality",
         "layout_mode": "legacy_fallback", "triggered_hard_flags": ["ceiling_changed"]}
    d.update(kw)
    return d


# 293BDE11 的實際形狀：客廳落選，餐廳/主臥/書房交付
R293 = [_render("dining"), _render("bedroom"), _render("study")]
D293 = [_dropped("living")]


# ── ① 缺客廳時，第一頁是佔位卡 ───────────────────────────────────────

def test_living_placeholder_is_first_when_living_missing():
    out = _run(R293, D293)
    first = out["firstAngleOfEachStyle"][0]
    assert first["placeholder"] is True, (
        f"第一頁不是客廳佔位卡而是 {first}——293BDE11 的客戶就是這樣看到餐廳當首頁")
    assert first["room_type"] == "living"
    assert out["angleLabels"][0][0] == "客廳（優化中）"


def test_placeholder_panel_says_what_and_what_next():
    out = _run(R293, D293)
    panel = out["panels"][0]
    assert "客廳設計優化中" in panel
    # 跟後端 completed_msg 同一句
    assert "主空間（客廳）設計仍在優化中；其他房間已先交付" in panel
    assert "<img" not in panel, "佔位卡不該秀圖"
    assert "下載設計圖" not in panel, "佔位卡不該有下載按鈕"


# ── ② 其他房間仍可切換 ──────────────────────────────────────────────

def test_other_rooms_are_still_reachable():
    out = _run(R293, D293)
    labels = out["angleLabels"][0]
    assert labels == ["客廳（優化中）", "餐廳", "主臥室", "書房"], labels
    panel = out["panels"][0]
    for nm in ("餐廳", "主臥室", "書房"):
        assert f"goAngle" in panel and nm in panel, f"{nm} 的子分頁不見了"


# ── ③ 佔位卡不得污染金額 / 空間數 / 清單 ─────────────────────────────

def test_placeholder_is_not_counted_as_a_room_or_an_item():
    out = _run(R293, D293)
    t = out["totals"]["french"]
    assert t["rooms"] == 3, f"空間數被佔位卡算進去了：{t['rooms']}（應為 3）"
    assert t["items"] == 3, f"商品數不對：{t['items']}"
    assert "3 個空間" in out["metaPlan"], f"頁首空間數不對：{out['metaPlan']}"


def test_totals_survive_even_if_someone_passes_the_dirty_array():
    """防呆層：哪天有人把含佔位卡的 g.angles 傳進 planTotals，金額仍不得變。

    現在三個呼叫端都吃 _hb.renders，佔位卡不在裡面——但那是「目前」。
    planTotals 自己擋 _placeholder，語意才不靠呼叫端自律。
    """
    out = _run(R293, D293)
    assert out["totals"] == out["totalsDirty"], (
        f"傳含佔位的陣列進去金額就變了：{out['totals']} vs {out['totalsDirty']}")


# ── ④ 客廳成功時，佔位卡完全不出現 ──────────────────────────────────

def test_no_placeholder_when_living_delivered():
    """反貧血：客廳有圖就不能冒出佔位卡。

    ⚠️ 只斷言「第一個不是佔位卡」不夠——排序是穩定的，真客廳仍會排在前面，
    佔位卡會安靜地多出一個分頁而測試全綠（2026-08-07 蓄意破壞抓到）。
    要驗的是**它一張都不存在**。
    """
    out = _run([_render("living")] + R293, D293)
    first = out["firstAngleOfEachStyle"][0]
    assert first["placeholder"] is False, "客廳已交付卻還是秀佔位卡"
    assert out["placeholderCount"] == 0, (
        f"客廳已交付卻仍插了 {out['placeholderCount']} 張佔位卡（多出假分頁）")
    assert out["angleLabels"][0][0] == "客廳"
    assert out["totals"]["french"]["rooms"] == 4


def test_no_placeholder_when_customer_never_asked_for_living():
    """客戶根本沒點客廳的單（只傳臥室），不得對他喊缺客廳。"""
    out = _run([_render("bedroom")], [_dropped("study")])
    assert out["firstAngleOfEachStyle"][0]["placeholder"] is False
    assert out["totals"]["french"]["rooms"] == 1


def test_no_dropped_at_all_means_no_placeholder():
    out = _run(R293, [])
    assert out["firstAngleOfEachStyle"][0]["placeholder"] is False


# ── ⑤ 文案分岔：不能一律叫客戶重拍 ──────────────────────────────────

@pytest.mark.parametrize("field,signal", [
    ("failure_class", "s2_preflight_blocked"),
    ("layout_mode", "s2_blocked_legacy"),
])
def test_modelling_failure_asks_for_a_reshoot(field, signal):
    out = _run(R293, [_dropped("living", **{field: signal})])
    panel = out["panels"][0]
    assert "重拍" in panel or "正面拍" in panel, "付費前建模失敗時沒給重拍指引"


@pytest.mark.parametrize("flag", [
    "ceiling_changed", "furniture_blocks_door", "sofa_on_wrong_side",
    "walls_changed", "product_visibility_fail",
])
def test_post_generation_hardfail_offers_free_regeneration_not_reshoot(flag):
    """🔴 生成後的硬傷不是照片的錯——293BDE11 就是被錯誤地叫去重拍。"""
    out = _run(R293, [_dropped("living", triggered_hard_flags=[flag])])
    panel = out["panels"][0]
    assert "重拍" not in panel, f"{flag} 仍叫客戶重拍"
    assert "免費重出" in panel


def test_placeholder_and_dropped_notice_share_one_signal_function():
    """佔位卡與落選紅卡必須共用 isModellingFailure()，不得各寫一套。

    兩套等價判斷剛在 2026-08-07 被抓過一次（_preflight / _modellingFailed）。
    """
    assert HTML.count("var _MODELLING_FAILURE_SIGNALS") == 1, "訊號清單被寫了不只一份"
    assert HTML.count("function isModellingFailure") == 1
    assert HTML.count("isModellingFailure(") >= 3, (
        "落選紅卡或佔位卡沒有走共用函式")


# ── ⑥ 佔位卡不得進資料層 ────────────────────────────────────────────

def test_placeholder_never_enters_hb_renders():
    """程式碼層：佔位卡只能進 group.angles（buildUI 自建的陣列）。"""
    src = _slice("function buildUI(")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
    assert "_hb" not in code, "buildUI 動到 _hb 了——佔位卡會流進購物清單與 PDF"
    assert "g.angles.push(_livingPlaceholder(" in code


def test_money_outlets_all_read_hb_renders():
    """三個金額出口都必須吃 _hb.renders，不得改吃 _groups。"""
    calls = re.findall(r"planTotals\((.*?)\)", HTML)
    for c in calls:
        if c.strip() == "renders":       # 函式定義本身
            continue
        assert "_hb" in c, f"planTotals 被傳了非 _hb 的來源：{c}"


def test_planTotals_guard_holds_even_if_placeholder_gains_items():
    """🔴 守門必須靠 `_placeholder` 判斷，不能靠「佔位卡剛好沒有商品」。

    `g.rooms[room] = 1` 寫在 `matched_furniture.forEach` **裡面**，所以空商品的
    佔位卡本來就加不出東西——把守門整行刪掉，測試照樣全綠（2026-08-07 蓄意破壞
    抓到我這條測試是假的）。這裡改成餵一張**帶商品的佔位卡**，直接打那行守門。
    """
    out = _run(R293, D293)
    assert out["totalsPoisoned"] == out["totals"], (
        f"帶商品的佔位卡被算進金額/空間：{out['totalsPoisoned']} vs {out['totals']}")


def test_meta_summary_guard_holds_even_if_placeholder_is_in_the_list():
    """頁首「N 個空間」同樣不得把佔位卡算成一間。"""
    out = _run(R293, D293)
    assert out["metaPlanPoisoned"] == out["metaPlan"], (
        f"佔位卡進了頁首空間數：{out['metaPlanPoisoned']} vs {out['metaPlan']}")
    assert "3 個空間" in out["metaPlanPoisoned"]


# ── ⑦ 零交付：整單一張圖都沒有，客廳仍要有佔位卡 ─────────────────────

# 293BDE11 payload 裡真正的 rooms（一筆，room_type='living_room'）
ROOMS293 = [{"room_id": "living_room", "room_type": "living_room",
             "room_label": "客廳", "is_primary": True}]


def test_placeholder_appears_even_when_nothing_was_delivered():
    """🔴 單房客廳零交付時，第一版連 group 都建不出來，佔位卡永遠插不進去。

    `_groups` 只從成功的 `renders` 建，而載入處是 `if (renders.length)` 才呼叫
    buildUI——所以「客廳失敗但其他房間成功」修好了，「整單零交付」沒修
    （GPT 2026-08-07）。鐵則是「只要有客廳，第一頁就保留給客廳」，
    不是「只要還有別的房間交付」。
    """
    out = _run([], D293, ROOMS293)
    assert len(out["firstAngleOfEachStyle"]) == 1, (
        f"零交付時沒有建出任何 style group：{out['firstAngleOfEachStyle']}")
    first = out["firstAngleOfEachStyle"][0]
    assert first["placeholder"] is True, "零交付時客廳佔位卡沒出現"
    assert out["angleLabels"] == [["客廳（優化中）"]]
    panel = out["panels"][0]
    assert "客廳設計優化中" in panel
    assert "<img" not in panel


def test_zero_delivery_group_uses_the_dropped_style_label():
    """零交付時 group 的名字要取自落選紀錄，不能顯示成 style 代碼。"""
    out = _run([], [_dropped("living", style_label="法式浪漫")], ROOMS293)
    assert "法式浪漫" in out["panels"][0]


def test_zero_delivery_with_no_living_keeps_the_generic_path():
    """反貧血：零交付而且沒有客廳落選時，不得憑空生出客廳佔位卡。

    （呼叫端會走原本的 repairing／尚無渲染圖分支，那條路不歸這裡管。）
    """
    out = _run([], [_dropped("bedroom")], [])
    assert out["firstAngleOfEachStyle"] == []
    assert out["placeholderCount"] == 0


def test_zero_delivery_totals_are_empty_not_polluted():
    """零交付時金額必須是空的——佔位卡不得讓清單長出東西。"""
    out = _run([], D293, ROOMS293)
    assert out["totals"] == {}, f"零交付卻算出金額分組：{out['totals']}"
    assert out["totalsDirty"] == {}, "把含佔位的陣列傳進去就長出金額了"


# ── ⑧ 頁首空間數：帶真實 rooms ──────────────────────────────────────

def test_meta_summary_counts_planned_rooms_including_the_missing_living():
    """🔴 頁首算的是【規劃空間】，客廳本來就在訂單範圍內。

    293BDE11 真實 payload：rooms=[living_room]、renders=[餐廳/主臥/書房]。
    `updateMetaSummary` 會把 rooms[] 也併進 roomSet（living_room → living），
    所以正確答案是 **4 個空間**，不是 3。
    我第一版的「端到端」示範傳了 `rooms: []`，印出 3——是示範錯，不是程式錯，
    但那份示範會讓人以為頁首少算了一間（GPT 2026-08-07 抓到）。
    """
    out = _run(R293, D293, ROOMS293)
    assert "4 個空間" in out["metaPlan"], (
        f"頁首規劃空間數不對：{out['metaPlan']}（rooms 的客廳沒算進去）")
    # 分頁也是 4 個（客廳佔位 + 三間已交付）→ 數字與畫面一致
    assert len(out["angleLabels"][0]) == 4


def test_placeholder_does_not_double_count_the_living_room():
    """客廳同時出現在 rooms[] 與佔位卡，不得被算成兩間。"""
    out = _run(R293, D293, ROOMS293)
    assert "4 個空間" in out["metaPlan"]
    assert out["metaPlanPoisoned"] == out["metaPlan"], "毒佔位卡讓空間數變了"


def test_delivered_living_gives_the_same_planned_count():
    """反貧血：客廳成功交付時，規劃空間數一樣是 4——佔位卡有無不影響這個數字。"""
    out = _run([_render("living")] + R293, D293, ROOMS293)
    assert "4 個空間" in out["metaPlan"]
    assert out["placeholderCount"] == 0


def test_should_build_ui_is_the_gate_the_loader_actually_uses():
    """🔴 載入處的「要不要建 UI」必須是可測的具名判斷。

    原本是行內條件，而行為測試直接呼叫 buildUI ——把它改回舊的
    `renders.length` 測試照樣全綠（2026-08-07 蓄意破壞抓到）。
    抽成 shouldBuildUI 之後，harness 走同一條路，這條才真的有牙。
    """
    assert _run([], D293, ROOMS293)["built"] is True, "零交付+客廳落選卻不建 UI"
    assert _run(R293, D293, ROOMS293)["built"] is True
    assert _run([], [_dropped("bedroom")], [])["built"] is False, (
        "沒有客廳落選、也沒有交付圖時不該建 UI（該走原本的優化中訊息）")
    assert _run([], [], [])["built"] is False


def test_loader_calls_the_named_gate_not_an_inline_condition():
    """程式碼層：載入處只能問 shouldBuildUI，不得自己再寫一次條件。"""
    body = HTML
    i = body.index("var _dropped = ((data.validation_summary")
    seg = body[i:i + 700]
    code = chr(10).join(l for l in seg.splitlines() if not l.strip().startswith("//"))
    assert "shouldBuildUI(renders, _dropped)" in code
    assert "renders.length ||" not in code, "載入處又自己寫了一次判斷"


# ── ⑨ 文案不得對客戶說假話 ──────────────────────────────────────────

def test_partial_delivery_says_other_rooms_were_delivered():
    """有別的房間交付時，講「其他房間已先交付」——跟後端 completed_msg 同一句。"""
    panel = _run(R293, D293, ROOMS293)["panels"][0]
    # 逐字鎖：這句跟後端 completed_msg（api.py:7602）必須一字不差
    assert "主空間（客廳）設計仍在優化中；其他房間已先交付。" in panel


def test_zero_delivery_does_not_claim_other_rooms_were_delivered():
    """🔴 零交付時不得說「其他房間已先交付」——那單一張圖都沒有。

    我第一版無條件用後端那句，端到端跑零交付情境時印出「其他房間已先交付」，
    但畫面上只有一個佔位分頁、金額是空的。對客戶說假話比少說話嚴重。
    後端在這個情境用的也是另一句（all_failed_repairing）。
    """
    panel = _run([], D293, ROOMS293)["panels"][0]
    assert "其他房間已先交付" not in panel, "零交付卻宣稱其他房間已交付"
    assert "客廳設計優化中" in panel
    # ⚠️ 斷言【完整那一句】。原本只驗「盡快補上」，所以前半句寫成
    # 「仍在為我們優化中」這種錯字照樣全綠——客戶看得到的字要逐字鎖。
    assert "你的客廳設計仍在優化中，我們會盡快補上。" in panel, (
        "零交付文案不是逐字正確的那一句")


def test_zero_delivery_copy_still_branches_on_reshoot_signal():
    """零交付時「重拍 vs 免費重出」的分岔一樣要成立。"""
    p_quality = _run([], D293, ROOMS293)["panels"][0]
    assert "免費重出" in p_quality and "重拍" not in p_quality
    p_model = _run([], [_dropped("living", failure_class="s2_preflight_blocked")],
                   ROOMS293)["panels"][0]
    assert "正面拍" in p_model or "重拍" in p_model


# 客戶看得到的每一句，逐字鎖。只驗關鍵詞的話錯字照樣全綠——
# 「你的客廳設計仍在為我們優化中」這個語序錯字就是這樣溜過一輪的（2026-08-07）。
PLACEHOLDER_COPY = {
    "標題":       "客廳設計優化中",
    "部分交付":   "主空間（客廳）設計仍在優化中；其他房間已先交付。",
    "零交付":     "你的客廳設計仍在優化中，我們會盡快補上。",
    "品質重出":   "設計圖沒有通過我們的配置驗收，系統已擋下不合格的版本，正在為你重出。"
                  "需要加快請聯絡客服<strong>免費重出</strong>。",
    "建模重拍":   "我們的空間建模判讀不了這個拍攝角度（不是你的空間問題）。"
                  "請站在客廳<strong>一端</strong>、鏡頭順著<strong>長邊</strong>正面拍一張，"
                  "讓左右兩側牆與大門都入鏡，重新上傳即可。",
}


def test_every_customer_facing_sentence_is_exact():
    """四種情境的完整句子逐字比對。"""
    partial = _run(R293, D293, ROOMS293)["panels"][0]
    zero = _run([], D293, ROOMS293)["panels"][0]
    model = _run(R293, [_dropped("living", failure_class="s2_preflight_blocked")],
                 ROOMS293)["panels"][0]

    assert PLACEHOLDER_COPY["標題"] in partial
    assert PLACEHOLDER_COPY["部分交付"] in partial
    assert PLACEHOLDER_COPY["品質重出"] in partial

    assert PLACEHOLDER_COPY["零交付"] in zero
    assert PLACEHOLDER_COPY["部分交付"] not in zero

    assert PLACEHOLDER_COPY["建模重拍"] in model
    assert PLACEHOLDER_COPY["品質重出"] not in model


def test_no_placeholder_copy_has_stray_markup_or_double_punctuation():
    """客戶看得到的字裡不得有沒閉合的標籤或連續標點。"""
    import re as _re
    for name, txt in PLACEHOLDER_COPY.items():
        assert txt.count("<strong>") == txt.count("</strong>"), f"{name} 的 <strong> 沒閉合"
        plain = _re.sub(r"<[^>]+>", "", txt)
        assert not _re.search(r"[。，；、]{2,}", plain), f"{name} 有連續標點"
        assert "  " not in plain, f"{name} 有連續空白"

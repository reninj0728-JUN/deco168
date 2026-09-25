# -*- coding: utf-8 -*-
"""目錄裡的尺寸必須可信——不可信就留空。

🔴 2026-09-24：第一趟補沙發尺寸的腳本沒有「高度下限」「整組商品」兩條規則，
   寫進了 12 筆錯資料（蘑菇懶骨頭 高11、折疊三人沙發 高15、1+2+3 人座整組
   寬197、電動躺椅 高45…）。後來的腳本補了規則，但「已有尺寸就跳過」，
   等於把錯資料凍住。當時測試數一直沒變——**規則只存在腳本裡，沒有測試鎖住**，
   下一趟寫入可以把同一類數字寫回去。Grok 抓到的。

   判準只有一份：furniture_match.dimension_red_flags。這支測試鎖的是
   「目錄裡沒有任何一筆違反它」，所以不管哪支腳本寫入，壞資料都進不來。

錯尺寸比沒尺寸更糟：沒尺寸時寬度守門與出圖 prompt 都沉默；
錯尺寸會叫模型照著畫，也會讓守門誤殺或誤放。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import furniture_match as fm

BACKEND = Path(__file__).resolve().parent
CATALOG = BACKEND / "furniture_catalog_real.json"
MATTRESS_FLAG = "寫的是床墊尺寸，不是床架外徑"


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _item(cat, name, dims):
    return {"category": cat, "name_zh": name, "dimensions": dims}


# ── 目錄層：鎖住「壞資料進不來」────────────────────────────────────

def test_no_untrustworthy_dimensions_in_catalog(catalog):
    """🔴 核心：任何一支腳本寫進去的尺寸，都必須通過同一份判準。

    唯一放行的是「適用床墊尺寸」——那是真資訊（能放多大的床墊），
    只是不能當床架寬；下一條測試鎖住它不會被讀成寬度。
    """
    bad = []
    for x in catalog:
        flags = [f for f in fm.dimension_red_flags(x) if f != MATTRESS_FLAG]
        if flags:
            bad.append((x["id"], x.get("dimensions"), flags))
    assert not bad, f"目錄裡有 {len(bad)} 筆不可信的尺寸：{bad[:5]}"


def test_mattress_size_text_never_becomes_a_bed_width(catalog):
    """「床架查外徑，不拿床墊尺寸代替」——資料留著，但絕不能讀成寬度。"""
    leaked = [(x["id"], x["dimensions"], fm.extract_item_width_cm(x))
              for x in catalog
              if fm.resolve_category(x) == "bed"
              and "床墊" in str(x.get("dimensions") or "")
              and fm.extract_item_width_cm(x) is not None]
    assert not leaked, f"床墊尺寸被讀成床架寬：{leaked}"


def test_hand_verified_items_are_never_flagged(catalog):
    """1227725E 那批人工核實過的尺寸，判準不准把它們標成不可信。"""
    src = (BACKEND / "test_verified_dimensions_reach_the_gate.py").read_text(encoding="utf-8")
    verified = set(re.findall(
        r'"((?:momo|pchome|hola|nitori|ikea)[-_][A-Za-z0-9_-]+)"', src))
    assert len(verified) >= 8, "核實清單抓不到，錨點要重對"
    by_id = {x["id"]: x for x in catalog}
    hit = [i for i in verified if i in by_id and fm.dimension_red_flags(by_id[i])]
    assert not hit, f"人工核實的尺寸被判成不可信：{hit}"


# ── 規則層：每一條都用真實出過事的字串釘住 ───────────────────────────

@pytest.mark.parametrize("dims", ["寬99x深99x高99cm", "100x100x100cm", "999*999*999"])
def test_cube_placeholder_is_flagged(dims):
    """memory：「這層不是 Volume 佔位值偵測器，30³/50³/99³ 會被放行」。"""
    flags = fm.dimension_red_flags(_item("沙發", "三人座沙發", dims))
    assert any("三邊相等" in f for f in flags), flags


@pytest.mark.parametrize("name,dims", [
    ("Mushroom日風蘑菇懶骨頭沙發", "寬155x深70x高11cm"),
    ("北歐風實木三段式折疊三人沙發", "寬180x深96x高15cm"),
])
def test_flat_pack_height_is_flagged(name, dims):
    """高 11／15cm 是平壓紙箱，不是組好的沙發。"""
    flags = fm.dimension_red_flags(_item("沙發", name, dims))
    assert any("高" in f for f in flags), flags


@pytest.mark.parametrize("name", [
    "ASSARI -北海道全柚木1+2+3人座沙發",
    "IHouse 愛屋家具 進口半牛皮獨立筒沙發 1+3人座",
    "柚木復古沙發客廳組 不含墊",
])
def test_set_product_is_flagged(name):
    """整組商品量到的不是一張沙發的寬。"""
    flags = fm.dimension_red_flags(_item("沙發", name, "寬197x深88x高100cm"))
    assert any("整組" in f for f in flags), flags


def test_shallow_depth_is_flagged():
    """奶油雙人沙發 90x50x50：深 50 坐不下人。"""
    flags = fm.dimension_red_flags(_item("沙發", "圓弧造型奶油色雙人沙發", "寬90x深50x高50cm"))
    assert any("深" in f for f in flags), flags


def test_low_back_sofa_with_real_size_is_not_flagged():
    """⚠️ 別誤殺：低背沙發真的存在，尺寸寫在品名裡的「180x61x57」是真的。"""
    assert fm.dimension_red_flags(
        _item("沙發", "【木洸】180x61x57cm三人位大象耳朵沙發", "寬180x深61x高57cm")) == []


def test_normal_sofa_is_not_flagged():
    assert fm.dimension_red_flags(
        _item("沙發", "ASSARI -法式費黛里三人座半牛皮沙發", "寬210x深90x高103cm")) == []


# ── 床的讀寬 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("dims,want", [
    ("寬205X深213X高109(cm)", 205),     # 已核實外徑，6 尺床墊是 182
    ("寬183X深210X高120(公分)", 183),   # 已核實外徑
    ("寬152x深188x高25cm", 152),
    ("寬 150 X 長 190 cm", 150),        # 通用解析會取「長」讀成 190
    ("193*60*30cm", None),              # 折疊床：長邊 193、短邊 60 都不是床架寬
    ("適用床墊尺寸：150 x 190 cm", None),  # 床墊不是床架
])
def test_bed_width_reader(dims, want):
    got = fm.bed_width_cm(dims)
    assert got == want, f"{dims} → {got}，應為 {want}"


# ── 第二輪（2026-09-24）：Grok ＋ GPT 抓到的三個洞 ────────────────

@pytest.mark.parametrize("dims", [
    "抽屜：88 x 93 x 20.5 cm",
    "抽屜：88 x 93 cm",              # GPT：兩個數字、沒有高度，舊版靠深度擋不住
    "適用床墊尺寸：" + chr(10) + "150x190",   # 標籤在上一行、數字在下一行
    "包裝尺寸：190x100x30",
])
def test_bed_reader_ignores_non_frame_measurements(dims):
    """🔴 看的是尺寸的【用途標籤】，不是數字範圍——88 剛好落在單人加大區間，
    只靠數字範圍會把抽屜寬讀成床寬。"""
    assert fm.bed_width_cm(dims) is None, f"{dims!r} 被讀成床寬"
    assert fm.dimension_red_flags(_item("床架", "原木床架", dims)),         f"{dims!r} 沒被判成不可信"


def test_frame_width_survives_when_mattress_size_is_also_listed():
    """GPT：舊版看到「床墊」就整欄丟，連寫在同一欄的正確外徑 205 也不要了。"""
    dims = "床架外徑：寬205x長213cm；適用床墊：182x188"
    assert fm.bed_width_cm(dims) == 205
    assert fm.dimension_red_flags(_item("床架", "義式雙人加大床", dims)) == []


@pytest.mark.parametrize("w,ok", [(60, False), (100, True), (205, True), (230, False)])
def test_bed_frame_window_is_only_a_coarse_filter(w, ok):
    """⚠️ Grok 抓到舊註解說得比程式強。這道門實際放行 88～207 的連續區間，
    只擋「明顯不是床」的數字；100 是單人加大床架還是別的，它分不出來。
    真正決定讀哪個數字的是用途標籤分段。這條測試把它的真實能耐釘住，
    免得以後又有人把它當成規格檢查。"""
    assert fm._plausible_bed_frame_width(w) is ok


# ── 守門【實際拿到】的寬度：尺寸欄以外還有品名這條路 ──────────────
# 🔴 Grok 抓到我講錯：「清空尺寸欄＝守門沉默」只對一部分品類成立。
#    沙發尺寸欄空了會退回讀品名——「懶人折疊沙發床-特大120cm」清空後
#    照樣讀到 120。上面的目錄測試只看尺寸欄，看不到這條路。

def test_widths_the_gate_actually_receives_are_plausible(catalog):
    """掃 extract_item_width_cm 的【回傳值】，不管它來自尺寸欄還是品名。"""
    bad = []
    for x in catalog:
        cat = fm.resolve_category(x)
        if cat not in fm.DIM_SANITY:
            continue
        w = fm.extract_item_width_cm(x)
        if w is None:
            continue
        lo, hi = fm.DIM_SANITY[cat][0]
        if not (lo <= w <= hi):
            bad.append((x["id"], cat, w, x["name_zh"][:30]))
    assert not bad, f"守門拿到不合理的寬度：{bad[:6]}"


def test_set_product_sofas_get_no_width_from_either_source(catalog):
    """整組的寬不是一張沙發的寬——尺寸欄空了，品名那條路也不准給。"""
    leaked = [(x["id"], fm.extract_item_width_cm(x)) for x in catalog
              if fm.resolve_category(x) == "sofa"
              and fm.sofa_is_set_product(x["name_zh"])
              and fm.extract_item_width_cm(x) is not None]
    assert not leaked, f"整組沙發還讀得到寬度：{leaked}"
    # 反證：品名路徑本身會讀數字，不擋的話這件會回 530
    assert fm.extract_item_width_cm(_item("沙發", "柚木1+2+3人座沙發 總長530cm", "")) is None


def test_merchant_stated_width_in_name_is_kept():
    """⚠️ 別擋過頭：商家寫在品名裡的寬度多半是真的商品寬。
    懶人類規則只擋「尺寸欄跟品名對不上」的 Volume 包裝尺寸。"""
    assert fm.extract_item_width_cm(_item("沙發", "懶人拼色豆袋沙發 雙人位110CM", "")) == 110
    assert fm.dimension_red_flags(
        _item("沙發", "AOTTO 多功能懶人折疊沙發床-特大120cm", "寬120cm")) == []
    assert fm.dimension_red_flags(
        _item("沙發", "Mushroom日風蘑菇懶骨頭沙發", "寬155x深70x高11cm")),         "跟品名對不上的壓縮包裝尺寸沒被擋"


# ── 長寬寫反（2026-09-25）──────────────────────────────────────────
# HOLA「長100，寬254」照慣例寫成「寬100x深254」；三圍檢查會按大小重排而放行，
# 解析器卻照「寬」字讀到 100。這條用解析器實際讀到的寬去比深。

def test_width_smaller_than_depth_is_flagged():
    flags = fm.dimension_red_flags(_item("沙發", "La-Z-Boy 676三電動牛皮沙發一字型", "寬100x深254x高110cm"))
    assert any("寫反" in f for f in flags), flags
    flags = fm.dimension_red_flags(_item("收納", "現代電視櫃", "寬40x深180x高45cm"))
    assert any("寫反" in f for f in flags), flags


def test_single_seat_slightly_deeper_than_wide_is_fine():
    """⚠️ 別誤殺：單人座本來就可能深一點點（Bernice 一人座 64×65）。"""
    assert fm.dimension_red_flags(_item("沙發", "布面實木一人座沙發椅", "寬64x深65x高73cm")) == []

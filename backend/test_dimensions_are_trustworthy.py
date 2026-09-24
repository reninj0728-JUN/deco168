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


def test_drawer_dims_are_caught_by_the_catalog_rule_not_the_reader():
    """⚠️ 讀寬函式的極限：「抽屜：88 x 93」的 88 剛好落在單人加大範圍，
    它分不出這是抽屜。所以這種要靠目錄層的 dimension_red_flags 清掉——
    兩道防線各管各的，不能只靠讀寬。"""
    item = _item("床架", "紐松原木床架 單人加大3.5尺", "抽屜：88 x 93 x 20.5 cm")
    assert fm.bed_width_cm(item["dimensions"]) == 88          # 讀寬擋不住
    assert fm.dimension_red_flags(item), "目錄層判準也沒擋住抽屜尺寸"

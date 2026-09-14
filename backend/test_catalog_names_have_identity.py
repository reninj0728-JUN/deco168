# -*- coding: utf-8 -*-
"""目錄裡的商品名必須在講「一個東西」，不是一句宣傳。

事故 1227725E：客戶拿到的購物清單上，兩張床叫「全程台灣工廠製造」、
衣櫃叫「無縫防塵 輕奢收納」——爬蟲把商品頁的行銷句抓成了 name_zh。
第一輪只修了 5 句寫死的口號，剩下「MIT製造」「刮泥落塵 吸水快乾」
「亮面 / 131CM」這種沒被涵蓋。

🔴 判準不能用關鍵字黑名單。「耐磨」是文創集電視櫃真名的一部分，拿它當判準
   會誤殺 54 件真貨。改問「這名字裡有沒有主詞」。
   （花瓶／掛鐘／相框／寢具／掛畫也算主詞——少列這些會把 IKEA、HOLA
     的真名整批誤判。）

⚠️ 這是【現況凍結】測試，不是零容忍：目前已知還有 1 件合格例外（純英文型號的
   Herman Miller）與 11 組舊重複列。數字只准降、不准升——升了代表有人又把
   行銷句寫進 name_zh，或又把同一個商品存了第二次。
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

CATALOG = Path(__file__).resolve().parent / "furniture_catalog_real.json"

# 名字裡出現任一個 = 這個名字在講一個東西
IDENTITY_WORDS = [
    "沙發", "茶几", "茶幾", "地毯", "地墊", "腳踏墊", "電視櫃", "床架", "床墊", "床包", "床",
    "衣櫃", "收納櫃", "鞋櫃", "書櫃", "餐邊櫃", "櫃", "餐桌", "餐椅", "書桌", "辦公桌",
    "邊几", "矮几", "几", "桌", "椅", "凳", "燈", "窗簾", "抱枕", "枕", "被", "毯", "墊",
    "盆", "架", "屏風", "鏡", "層板", "置物", "收納",
    "花瓶", "花器", "掛鐘", "鬧鐘", "時鐘", "相框", "畫框", "掛畫", "壁畫", "版畫",
    "裝飾畫", "水墨畫", "國畫", "擺飾", "擺件", "壁飾", "簾", "寢具", "獨立筒",
    "瓶", "杯", "盤", "盒", "框", "魚缸", "推車", "腳蹬", "布料", "涼感", "展示罩",
    "模型", "拼貼", "組合", "件組", "件套", "雙拼", "聯組",
]

# 判準是中文主詞，純英文型號名會被誤判。這件的名字是真的（Herman Miller Aeron）。
KNOWN_OK_WITHOUT_CHINESE_NOUN = {"pchome_DCBV0W-A900JRYN6"}

# 同一個商品被存兩次、兩筆同名。這輪只清掉「因為改名才浮出來」的，
# 這 11 組是更早就存在的，另案處理——凍結住不讓它長大。
LEGACY_DUP_GROUPS = 11


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def has_identity(name: str) -> bool:
    return any(w in name for w in IDENTITY_WORDS)


def _icode(url) -> str | None:
    m = re.search(r"i_code=(\d+)", str(url or ""))
    return m.group(1) if m else None


def test_no_name_is_pure_marketing_copy(catalog):
    """沒有主詞的品名＝客戶清單上會出現「MIT製造」這種東西。"""
    bad = [(x["id"], x["name_zh"]) for x in catalog
           if x["id"] not in KNOWN_OK_WITHOUT_CHINESE_NOUN
           and not has_identity(str(x.get("name_zh") or ""))]
    assert not bad, f"品名沒有商品主詞（{len(bad)} 件）：{bad[:6]}"


def test_the_expensive_ones_carry_real_names(catalog):
    """三件貴的曾經全叫「符合國際標準低甲醛防蛀木心板」，最貴的 NT$28,200。"""
    by_id = {x["id"]: x for x in catalog}
    for cid in ("momo_8738884", "momo_8740859", "momo_8740858"):
        n = str(by_id[cid]["name_zh"])
        assert "符合國際標準低甲醛防蛀木心板" != n, f"{cid} 還是規格句"
        assert "WAKUHOME" in n or has_identity(n), f"{cid} 名字沒有商品身分：{n}"


def test_repaired_rows_were_not_truncated(catalog):
    """真名裡的尺寸不准被截掉——舊腳本 name[:40] 把 160x230cm 切成 160x2。"""
    for x in catalog:
        n = str(x.get("name_zh") or "")
        assert not re.search(r"\d+x\d$", n), f"尺寸被截斷：{x['id']} {n}"


def test_no_escaped_characters_left_in_names(catalog):
    """品名不准出現反斜線。

    PChome 的 API 回的是 JSON 字串，斜線寫成反斜線加斜線。只做
    unicode_escape 不會解開它，「餐桌巾／餐桌墊」那串就會帶著反斜線
    進目錄，客戶的購物清單上直接看得到。
    """
    bad = [(x["id"], x["name_zh"]) for x in catalog
           if chr(92) in str(x.get("name_zh") or "")]
    assert not bad, f"品名裡有沒解開的跳脫字元：{bad[:5]}"


def test_removed_rows_stay_removed(catalog):
    """這輪刪掉的：5 列重複、1 列改名後同商品同名、2 件已下架。"""
    gone = {"momo-lux-0001", "momo-lux-0011", "momo-lux-0019", "momo-lux-0027",
            "momo-lux-0041", "momo-lux-0040",
            "momo_11856247", "momo_11918204"}
    still = gone & {x["id"] for x in catalog}
    assert not still, f"已刪除的列又回來了：{still}"


def test_same_product_duplicates_do_not_grow(catalog):
    """同一個 i_code 底下不准出現新的同名重複列。

    ⚠️ 凍結在 11 組舊債，不是 0。降下來要順手把這個數字改小。
    """
    by = defaultdict(list)
    for x in catalog:
        k = _icode(x.get("purchase_url"))
        if k:
            by[k].append(x)
    dup = {k: g for k, g in by.items()
           if len({str(y.get("name_zh")) for y in g}) < len(g)}
    assert len(dup) <= LEGACY_DUP_GROUPS, (
        f"同商品同名重複從 {LEGACY_DUP_GROUPS} 組長到 {len(dup)} 組："
        f"{list(dup)[:5]}")


def test_catalog_is_still_one_line():
    """目錄是單行 JSON——用 indent 寫回會變成 9 萬行，diff 整個爆掉。"""
    with CATALOG.open(encoding="utf-8") as f:
        assert sum(1 for _ in f) == 1, "目錄不再是單行 JSON"

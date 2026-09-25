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
    # 2026-09-23 換回官方品名後補上：真商品名，只是詞表沒收。
    # 上面註解早就警告「少列這些會把 IKEA、HOLA 的真名整批誤判」——這次就是。
    # （有「衣櫃」卻沒有「衣櫥」，nitori 官方名兩種都用。）
    "衣櫥", "化妝台", "梳妝台", "工作站", "滑門", "窗紗",
    "碗", "鐘", "飾品", "溫度計", "溼度計",
    # 2026-09-25 PChome 換回官方品名後補上（茶席＝桌上鋪的布、毛巾＝浴巾，都是真實物件）
    "茶席", "毛巾",
]

# 判準是中文主詞，純英文型號名會被誤判。這件的名字是真的（Herman Miller Aeron）。
KNOWN_OK_WITHOUT_CHINESE_NOUN = {"pchome_DCBV0W-A900JRYN6",
                                 "pchome_DCBV0W-A900HALHO"}   # 同款另一列，2026-09-25 換回官方名

# 同一個商品頁（同 i_code）被存成好幾列。凍結住不讓它長大，另案逐列清。
#
# 🔴 2026-09-23 改量法：原本量的是「同 i_code **且同名**」——那是代理指標，有盲點：
#    同一件商品用兩個【不同的假名】存兩次，它完全看不到。
#    換回官方品名那天這個數字從 11 跳到 16，但【一列都沒有新增】：
#        同 i_code 多列  換名前 24 組／多 26 列   換名後 24 組／多 26 列
#        其中同名的      換名前 11 組             換名後 16 組
#    也就是舊測試只看得到 24 組裡的 11 組，另外 13 組重複上架被假名偽裝成
#    不同商品（RICHOME 同一張沙發同時叫「L型布藝沙發」和「貴妃沙發」）。
#    改量「同 i_code 有幾列」：不受換名影響，也不再被假名騙過——這是變嚴不是放寬。
# ⚠️ 這 24 組【不是】逐欄相同：flux_descriptor／keywords／常常連 image_url 都不同，
#    不能直接刪或合併（memory：同商品頁≠同款，拼接會做出不存在的商品）。
# 巴芙洛桌墊（i_code 11824471）兩列刪掉後整組消失：24→23 組、26→25 列。
LEGACY_DUP_GROUPS = 23
LEGACY_DUP_EXTRA_ROWS = 25


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
    """這輪刪掉的：5 列重複、1 列改名後同商品同名、2 件已下架。

    2026-09-23 追加 2 列：巴芙洛軟玻璃桌墊（NT$549 的桌巾）掛在「茶几」類目。
    換回官方名後，既有的「耗材」與「保護布/墊」兩條偵測同時抓到——它們一直是
    垃圾，只是假名「輕奢風收納抽屜茶几」讓守衛看不到。
    """
    gone = {"momo-lux-0001", "momo-lux-0011", "momo-lux-0019", "momo-lux-0027",
            "momo-lux-0041", "momo-lux-0040",
            "momo_11856247", "momo_11918204",
            "momo-mod-0027", "momo-lux-0085"}
    still = gone & {x["id"] for x in catalog}
    assert not still, f"已刪除的列又回來了：{still}"


def test_same_product_duplicates_do_not_grow(catalog):
    """同一個商品頁（i_code）不准被多存一列——不管那一列取了什麼名字。

    ⚠️ 凍結在 24 組／多 26 列的舊債，不是 0。降下來要順手把數字改小。
    """
    by = defaultdict(list)
    for x in catalog:
        k = _icode(x.get("purchase_url"))
        if k:
            by[k].append(x)
    multi = {k: g for k, g in by.items() if len(g) > 1}
    extra = sum(len(g) - 1 for g in multi.values())
    assert len(multi) <= LEGACY_DUP_GROUPS and extra <= LEGACY_DUP_EXTRA_ROWS, (
        f"同商品頁多存的列從 {LEGACY_DUP_GROUPS} 組／{LEGACY_DUP_EXTRA_ROWS} 列"
        f"長到 {len(multi)} 組／{extra} 列：{list(multi)[:5]}")


def test_renaming_did_not_invent_duplicate_names_across_products(catalog):
    """換名只准讓【同一件商品】同名，不准讓【不同商品】撞成同一個名字。

    去重吃 name_zh（_catalog_without 會把同名當同一件），兩件不同商品同名，
    下一間房就少一件可配的貨。MALM 單人／雙人床框共用同一個官方標題，
    所以 _apply_realnames 會跳過它們——這條鎖住那個跳過沒有被拿掉。
    """
    by_name = defaultdict(set)
    for x in catalog:
        k = _icode(x.get("purchase_url"))
        if k and str(x.get("id", "")).startswith("momo"):
            by_name[str(x.get("name_zh"))].add(k)
    clash = {n: ks for n, ks in by_name.items()
             if len(ks) > 1 and n not in _PREEXISTING_NAME_CLASH}
    assert not clash, f"不同商品撞成同一個名字：{list(clash.items())[:3]}"


# 換名【之前】就撞名的舊債，不是換名造成的——兩件都在撈不到官方名的那批 momo 裡，
# 名字本身還被截斷在括號中間（原本應是兩個色款）。等撈到官方名時一起修。
_PREEXISTING_NAME_CLASH = {"120公分雙層茶几（大理石紋色"}


def test_catalog_is_still_one_line():
    """目錄是單行 JSON——用 indent 寫回會變成 9 萬行，diff 整個爆掉。"""
    with CATALOG.open(encoding="utf-8") as f:
        assert sum(1 for _ in f) == 1, "目錄不再是單行 JSON"

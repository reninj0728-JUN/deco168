# -*- coding: utf-8 -*-
"""已核實的尺寸要真的餵得動尺寸守門。

1227725E 那 12 件裡 11 件 dimensions 是空的。配對有一道 filter_by_dimensions
（furniture_match.py:1548），但它讀不到寬度就等於沒擋——那 206cm 的三人沙發
在短邊 2.5m 的房間照樣配得出來。

🔴 補資料前必須先確認解析器讀到的是【佔地寬】。商家的寫法不一致：
     hola   長206，寬103  ← 長才是靠牆跨距，寬是深度
     momo   寬205X深213   ← 寬才是
   如果解析器取到 103，寫進去會比空著更糟：守門會「自信地放行」一件放不下的沙發。
   所以這裡驗的是每個字串實際解析出來的數字，不是「欄位有沒有填」。

⚠️ 寬度對了不等於放得下。深度、走道、門片開啟空間都沒驗，這批資料只到寬度為止。
⚠️ 配對本身不是決定性的（同分時 random.random() 打散），所以不能斷言
   「補資料後某某商品會被換掉」。能斷言的只有這裡這種純函式行為。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import furniture_match as fm

CATALOG = Path(__file__).resolve().parent / "furniture_catalog_real.json"

# id → (商家標示的佔地寬 cm, 說明)
VERIFIED_WIDTH = {
    "hola_014412973":          (206, "同款沙發 淺米，一樣是三人 206cm"),
    "hola_014412974":          (206, "三人沙發，長206是靠牆跨距（寬103是深度）"),
    "hola_014412975":          (206, "同款沙發 淺綠，一樣是三人 206cm"),
    "pchome_DQCBDJ-A900IFYDM": (100, "茶几，長100"),
    "pchome_DQCD28-A900HN9Y9": (200, "伸縮電視櫃，取下限200（可收到這個寬度）"),
    "momo_12731234":           (205, "床架外徑，不是6尺床墊的182"),
    "momo_10501869":           (183, "床架外徑"),
    "ikea-mod-0379":           (68,  "BOLLSIDAN 68x36"),
    "pchome_DEABQL-A900ET8FZ": (46,  "圓邊桌直徑46；品名的『40』是型號不是尺寸"),
}


@pytest.fixture(scope="module")
def by_id():
    return {x["id"]: x for x in json.loads(CATALOG.read_text(encoding="utf-8"))}


@pytest.mark.parametrize("cid,expect,why", [(k, v[0], v[1]) for k, v in VERIFIED_WIDTH.items()])
def test_gate_reads_the_footprint_width(by_id, cid, expect, why):
    item = by_id.get(cid)
    assert item, f"{cid} 不在目錄"
    got = fm.extract_item_width_cm(item)
    assert got == expect, f"{cid} 守門讀到 {got}，應該是 {expect}（{why}）"


def test_two_oversized_items_are_now_blocked_in_small_rooms(by_id):
    """補資料前這兩件在任何房間都擋不住（讀不到寬度）；補完之後小房間會擋。"""
    for cid in ("hola_014412974", "momo_12731234"):
        kept = fm.filter_by_dimensions([by_id[cid]], 200)
        assert not kept, f"{cid} 在上限200的房間仍然通過（寬度 205~206）"
        assert fm.filter_by_dimensions([by_id[cid]], 240), \
            f"{cid} 在上限240的房間被誤擋了"


def test_no_guessed_width_enters_the_gate(by_id):
    """查不到就不准有數字。nitori 那件尺寸待確認，品名的「80BC」不得被當成寬80。"""
    item = by_id["nitori_2190500001527s"]
    assert not str(item.get("dimensions") or "").strip(), "待確認的商品被填了尺寸"
    assert fm.extract_item_width_cm(item) is None, \
        "品名被解析成寬度了——那是猜的，守門會拿猜測值去擋／放行"


def test_the_sofa_name_says_three_seater(by_id):
    """名字錯不只是好看問題：小空間對三人座有降權，名字不寫就不會生效。"""
    n = str(by_id["hola_014412974"]["name_zh"])
    assert "三人" in n, f"沙發名稱沒有標三人座：{n}"
    assert "雙人" not in n, f"沙發名稱還寫著雙人：{n}"


def test_the_whole_sofa_family_says_three_seater(by_id):
    """🔴 同一款沙發有三個顏色（973 淺米／974 白灰／975 淺綠），三件都是 206cm 三人座。

    只修客戶碰到的那一件沒有用——離線跑配對時被選中的是 975。
    """
    for sku in ("hola_014412973", "hola_014412974", "hola_014412975"):
        n = str(by_id[sku]["name_zh"])
        assert "三人" in n and "雙人" not in n, f"{sku} 還寫著雙人：{n}"
        assert fm.extract_item_width_cm(by_id[sku]) == 206, f"{sku} 寬度不是 206"


def test_bed_widths_are_not_derived_from_the_six_foot_label(by_id):
    """兩張床商家都標『6尺』(床墊182cm)，外徑卻是205與183——不能用尺換算。"""
    a = fm.extract_item_width_cm(by_id["momo_12731234"])
    b = fm.extract_item_width_cm(by_id["momo_10501869"])
    assert a == 205 and b == 183, f"床架外徑不對：{a} / {b}"
    assert a != b, "兩張都標6尺卻拿到同一個寬度——八成是用尺換算的"

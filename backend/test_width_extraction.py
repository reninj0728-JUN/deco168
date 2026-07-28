# -*- coding: utf-8 -*-
"""商品橫寬抽取的契約測試。

2026-07-28 由 39606371「電視櫃太長」查出來的 P0：抽錯的寬度會進 prompt 的
「Its real width is about N cm — render it at TRUE SCALE」——**錯尺寸比沒尺寸更糟**，
沒尺寸時 prompt 沉默，錯尺寸會主動要求模型畫錯。

釘死三件事：
  1. 語意選橫寬（長×寬×高 取長；寬×深×高 取寬），不是盲目「長優先」；
  2. 型號碼（W2002）不得被當成公分；
  3. 不確定或數值離譜一律回 None，讓 prompt 沉默。
"""
from __future__ import annotations

import pytest
from furniture_match import _extract_width_cm, extract_item_width_cm


def _item(name, cat="沙發", dims=""):
    return {"name_zh": name, "category": cat, "dimensions": dims}


# ── 1. 生產環境實際抽錯的案例（修復前 4/9 錯）────────────────────
REAL_WORLD = [
    # (商品名, 品類, 真實橫寬)
    ("弧形沙發三人座(長240*寬85*高78)(預購)", "沙發", 240),
    ("弧形沙發五人座(長360*寬85*高78)(預購)", "沙發", 360),
    ("弧形沙發雙人座(長180*寬85*高78)(預購)", "沙發", 180),
    ("弧形沙發四人座(長280*寬85*高78)(預購)", "沙發", 280),
    ("【木洸】180x61x57cm三人位大象耳朵沙發 小戶型客廳家用沙發床", "沙發", 180),
    ("【155x60x71cm】三人款客廳實木沙發 小戶型家用酒店公寓沙發", "沙發", 155),
    ("【木洸】180x30x29cm客廳落地式電視櫃 室內家用簡約電視機櫃", "收納", 180),
    ("家用200x30x60cm落地電視櫃 小戶型實木現代簡約兩門四抽櫃子", "收納", 200),
    ("ASSARI-夏佐6尺推門電視櫃(寬180x深40x高49cm)", "收納", 180),
    ("hoi! WH小滿橡木實木雙抽滑門電視櫃1.8M 原木色 W2002", "收納", 180),
]


@pytest.mark.parametrize("name,cat,truth", REAL_WORLD)
def test_real_world_widths(name, cat, truth):
    got = extract_item_width_cm(_item(name, cat))
    assert got is not None, f"抽不到寬度：{name}"
    assert abs(got - truth) <= max(5, 0.05 * truth), (
        f"{name}\n  期待 ≈{truth}cm，實得 {got}cm")


# ── 2. 語意：哪一個標籤才是橫寬 ───────────────────────────────
def test_length_wins_when_width_means_depth():
    """長240×寬85×高78：有『長』時，『寬』指的是深度。"""
    assert _extract_width_cm("長240*寬85*高78") == 240


def test_width_wins_when_depth_is_labelled():
    """寬200×深43×高31：深度已另外標了，『寬』就是橫寬。"""
    assert _extract_width_cm("寬200x深43x高31cm") == 200


def test_wdh_letters_follow_the_same_rule():
    assert _extract_width_cm("W270 x D90 x H80") == 270


def test_width_only_is_still_taken():
    assert _extract_width_cm("寬180x高49") == 180


def test_not_blindly_length_first():
    """沒有『長』時不可硬找長；寬×深 仍取寬。"""
    assert _extract_width_cm("寬150 深45") == 150


# ── 3. 型號碼不得被當成尺寸 ──────────────────────────────────
@pytest.mark.parametrize("text", [
    "電視櫃1.8M 原木色 W2002",
    "YW西雅圖橡木實木雙抽拉門岩板電視櫃1.8M 原木色 Y9031",
])
def test_model_codes_are_not_widths(text):
    got = _extract_width_cm(text, allow_bare=True)
    assert got is None or got <= 400, f"型號被當成寬度：{text} -> {got}"


def test_model_code_alone_yields_nothing():
    assert _extract_width_cm("原木色 W2002", allow_bare=True) is None


# ── 4. 不確定 / 離譜 → None（讓 prompt 沉默）──────────────────
@pytest.mark.parametrize("text", ["寬2002", "寬5", "長999x寬85", "W0972"])
def test_absurd_values_return_none(text):
    assert _extract_width_cm(text, allow_bare=True) is None


def test_missing_dims_return_none():
    assert _extract_width_cm("") is None
    assert extract_item_width_cm(_item("簡約電視櫃", "收納")) is None


def test_brand_3m_is_not_300cm():
    """品牌 3M 不可被讀成 300cm；少收幾筆勝過餵假比例。"""
    assert _extract_width_cm("3M 無痕掛勾 電視櫃專用", allow_bare=True) is None


# ── 5. 鎖住修復前就正確的行為，別修沙發打壞電視櫃 ──────────────
def test_chinese_feet_still_works():
    """50873CF0：9.7尺 L 型電視櫃 = 294cm。"""
    assert _extract_width_cm("9.7尺L型電視中空櫃") == 294


def test_triple_format_still_takes_first_number():
    assert _extract_width_cm("120*60*45CM") == 120


def test_bare_cm_still_works_for_sofa():
    assert _extract_width_cm("-270cm-", allow_bare=True) == 270


def test_height_prefix_still_not_taken_as_width():
    """高180cm 不可被當寬度，也不可從中段配出 80。"""
    assert _extract_width_cm("高180cm", allow_bare=True) is None


def test_dimensions_field_still_beats_name():
    item = _item("某某電視櫃", "收納", dims="寬150x深40x高45cm")
    assert extract_item_width_cm(item) == 150


def test_catalog_style_dimension_sentence():
    """目錄裡真實出現過的敘述式 dimensions。"""
    assert _extract_width_cm("電視櫃：寬度180cm、高度33cm") == 180

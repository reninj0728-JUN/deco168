# -*- coding: utf-8 -*-
"""副風格標籤進目錄前的最後一道白名單。

背景：分類器會判「這件家具主風格 A，但也搭得上 B」，讓同一件貨同時填兩個
風格缺口（目錄的 style_tags 是陣列、配對器讀任一格命中）。
但合併端不可以只信上游——停售風格掛上去客戶根本選不到，那件貨等於白收。

規則：只收上線九種、排除主標籤、去重、最多 2 個。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _safe_merge_raw as m


def test_only_live_styles_survive():
    """停售的四種風格不得進 style_tags——客戶選不到，配對永遠命不中。"""
    got = m._clean_also_fits(
        ["industrial", "art-deco", "boho", "mediterranean", "luxury"], "modern")
    assert got == ["luxury"], f"停售風格沒被擋掉：{got}"


def test_primary_is_not_duplicated():
    """主標籤不得再出現在副標籤裡。"""
    assert m._clean_also_fits(["modern", "luxury"], "modern") == ["luxury"]


def test_deduped():
    assert m._clean_also_fits(["luxury", "luxury", "cream"], "modern") == ["luxury", "cream"]


def test_capped_at_two():
    """上限 2 個：一件家具掛三四種風格，等於哪一種都不像。"""
    got = m._clean_also_fits(["luxury", "cream", "nordic", "wood"], "modern")
    assert len(got) == 2, f"沒有限制在 2 個：{got}"
    assert got == ["luxury", "cream"]


def test_garbage_input_does_not_crash():
    """上游是 JSON，型別不保證。數字會在 iterate 時炸；字典會把「鍵」當標籤混進去。"""
    for bad in (None, [], ["", None, "  "], ["不存在的風格"], "not-a-list",
                123, True, 3.14, {"luxury": 1, "industrial": 2}, object()):
        got = m._clean_also_fits(bad, "modern")
        assert got == [], f"垃圾輸入 {bad!r} 回了 {got!r}"


def test_non_string_elements_are_skipped():
    """清單裡混進非字串元素時，只跳過那一個，不整批放棄。"""
    assert m._clean_also_fits(["luxury", 123, None, {"x": 1}, "cream"], "modern")         == ["luxury", "cream"]


def test_whitelist_matches_style_form():
    """白名單必須等於 style-form.html 上線中的九種，不多不少。"""
    import re
    form = (Path(__file__).resolve().parent.parent / "style-form.html").read_text(encoding="utf-8")
    ids = set(re.findall(r"id:'([a-z-]+)',\s*emoji:", form))
    assert ids == set(m.LIVE_STYLES), (
        f"白名單與選風格頁不一致；只在白名單={set(m.LIVE_STYLES)-ids}，"
        f"只在選風格頁={ids-set(m.LIVE_STYLES)}")

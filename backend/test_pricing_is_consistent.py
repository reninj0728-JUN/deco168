# -*- coding: utf-8 -*-
"""方案價格散在三個頁面，後端不算錢——任何一頁改了另一頁沒跟上，客人就會看到兩個價。

定價（2026-09-24 用戶定案）：
  單一空間 999（1 個空間、2 種風格，不開放加購）
  兩房 1,388（最多 4 個空間）／三房 1,688（最多 5 個空間），全室同一種風格
  每多 1 個空間 +199，只給全室方案；全室上限 5 個空間（後端 MAX_ROOM_INSTANCES）
  原價 1,999／2,388／2,688 劃線、「現省 NT$1,000」——用戶決定的呈現
⚠️ 兩房＋加購 1 個＝1,587 < 三房 1,688 是**刻意的**（讓客人自己算到），不是 bug。
"""
from __future__ import annotations

import re
from pathlib import Path

import api

ROOT = Path(__file__).resolve().parent.parent
PAY = (ROOT / "zoning-confirm.html").read_text(encoding="utf-8")
HOME = (ROOT / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "style-form.html").read_text(encoding="utf-8")
UPLOAD = (ROOT / "upload.html").read_text(encoding="utf-8")

WANT = {"A": (999, 1, 1999), "two": (1388, 4, 2388), "three": (1688, 5, 2688)}


def _pay_plans():
    out = {}
    for key, price, cap in re.findall(
            r"(\w+):\s*\{\s*name:\s*'[^']+',\s*price:\s*(\d+),\s*cap:\s*(\d+)\s*\}", PAY):
        out[key] = (int(price), int(cap))
    return out


def test_payment_page_prices_and_caps():
    assert _pay_plans() == {k: v[:2] for k, v in WANT.items()}
    assert re.search(r"SPACE_ADDON_PRICE\s*=\s*199\b", PAY)


def test_space_cap_matches_backend():
    """三房含 5 個＝全室上限；兩房加購最多加到這裡。前後端不一致就會賣出做不出來的空間。"""
    m = re.search(r"MAX_SPACES\s*=\s*(\d+)", PAY)
    assert m and int(m.group(1)) == api.MAX_ROOM_INSTANCES
    assert WANT["three"][1] == api.MAX_ROOM_INSTANCES
    m = re.search(r"var MAX_ROOM_INSTANCES\s*=\s*(\d+)", UPLOAD)
    assert m and int(m.group(1)) == api.MAX_ROOM_INSTANCES


def test_home_and_payment_page_show_the_same_prices():
    for _key, (price, _cap, was) in WANT.items():
        now = f"{price:,}"
        assert f"<sup>NT$</sup>{now}</div>" in HOME, f"首頁沒有 NT${now}"
        assert f'<div class="plan-price">NT${now}</div>' in PAY, f"付款頁沒有 NT${now}"
        assert f"NT${was:,}" in HOME and f"NT${was:,}" in PAY, f"原價 {was:,} 兩頁不一致"
        assert was - price == 1000, "統一「現省 NT$1,000」"
    assert "+NT$199" in HOME


def test_old_prices_are_gone_everywhere():
    """舊定價（全室 1,199 起、加風格 +500）不能殘留在任何客人看得到的地方。"""
    for name, html in (("首頁", HOME), ("付款頁", PAY), ("風格頁", STYLE)):
        for old in ("1,199", "1199", "+NT$500", "+500"):
            assert old not in html, f"{name}還有舊價格 {old}"


def test_whole_house_is_one_style():
    """全室整戶同一種風格：風格頁上限 1，付款頁送單只送第 1 種。"""
    assert re.search(r"st\.value === 'whole'\)\s*\?\s*1\s*:\s*2", STYLE)
    assert "styles = styles.split(',')[0]" in PAY

"""真正的 pytest 測試（無網路、秒級）：家具目錄健全性 + 9 風格 × 4 房型配對迴歸。

這是商業化的 CI 基線：任何人改了 furniture_match.py 或目錄資料，
跑 `pytest backend/ -q` 就能在 push 前抓到「配對爆掉 / 目錄壞掉」。
"""
import json
from pathlib import Path

import pytest

import furniture_match as fm

CATALOG_PATH = Path(__file__).parent / "furniture_catalog_real.json"
CURRENT_STYLES = ["modern", "cream", "nordic", "japanese", "wood", "luxury",
                  "french", "muji", "chinese-modern"]
ROOMS = ["living", "bedroom", "dining", "kitchen"]

REQUIRED_FIELDS = ["id", "name_zh", "category", "style_tags",
                   "price_twd", "image_url", "purchase_url"]


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_catalog_loads_and_size(catalog):
    assert len(catalog) >= 3000, f"目錄只剩 {len(catalog)} 件，疑似被覆寫（merge 腳本事故？）"


def test_catalog_schema(catalog):
    bad = [it.get("id", "?") for it in catalog
           if any(not it.get(f) for f in REQUIRED_FIELDS)]
    assert not bad, f"{len(bad)} 件缺必要欄位，例如: {bad[:5]}"


def test_no_duplicate_ids(catalog):
    ids = [it["id"] for it in catalog]
    assert len(ids) == len(set(ids)), "目錄有重複 id"


def test_urls_look_real(catalog):
    bad_img = [it["id"] for it in catalog if not str(it["image_url"]).startswith("http")]
    assert not bad_img, f"image_url 非 http: {bad_img[:5]}"
    homepages = {"https://www.momoshop.com.tw", "https://24h.pchome.com.tw",
                 "https://www.ikea.com.tw", "https://www.hola.com.tw",
                 "https://www.nitori-net.tw"}
    fake = [it["id"] for it in catalog
            if str(it["purchase_url"]).rstrip("/") in homepages]
    assert not fake, f"purchase_url 是首頁假連結: {fake[:5]}"


def test_only_current_styles(catalog):
    stray = {}
    for it in catalog:
        s = (it.get("style_tags") or [None])[0]
        if s not in CURRENT_STYLES:
            stray[s] = stray.get(s, 0) + 1
    assert not stray, f"有商品掛著已停售風格標籤（永遠配對不到）: {stray}"


def test_match_all_style_room_combos(catalog):
    """9 風格 × 4 房型 = 36 組合：不噴例外、不回空清單。"""
    fails, empties = [], []
    for style in CURRENT_STYLES:
        for room in ROOMS:
            prompt = f"{style} style {room} room with sofa bed table chair"
            try:
                items = fm.match_furniture(style, prompt, catalog, top_n=5, mode=room)
            except Exception as e:  # noqa: BLE001
                fails.append((style, room, f"{type(e).__name__}: {e}"))
                continue
            if not items:
                empties.append((style, room))
    assert not fails, f"配對噴例外: {fails}"
    assert not empties, f"配對回空清單: {empties}"


def test_must_have_categories_never_empty(catalog):
    """每個房型的 must-have 品類在每種風格下都配得到（跨風格保命網有效）。"""
    missing = []
    for style in CURRENT_STYLES:
        for room, rule in fm.ROOM_RULES.items():
            prompt = f"{style} style {room} room"
            items = fm.match_furniture(style, prompt, catalog, top_n=5, mode=room)
            got_cats = {fm.resolve_category(it) for it in items}
            for must_cat in rule["must"]:
                if must_cat not in got_cats:
                    missing.append((style, room, must_cat))
    assert not missing, f"must-have 品類缺配: {missing}"

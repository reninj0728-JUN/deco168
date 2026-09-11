# -*- coding: utf-8 -*-
"""官網靜態頁不得引用會被保留期清掉的 storage 網址。

真實事故（2026-09-11）：首頁案例區直連 `renders` bucket 的公開網址，
而 api.py 的 `_purge_expired_storage()` 每次部署啟動都會刪掉 renders／uploads
裡超過 RETENTION_DAYS 天的檔案。推版當天就刪掉了 60F540A4（8/07）與
A559DD2B（8/08）兩張首頁用圖，官網直接出現兩個破圖 alt 字，而且沒有任何錯誤。

案例圖是網站素材、不是客戶交付檔，必須放在 repo 裡由 Vercel 靜態服務。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 這兩個 bucket 被 api.py 的保留期清理掃到，靜態頁不可引用
PURGED_BUCKETS = ("/object/public/renders/", "/object/public/uploads/")
PAGES = ("index.html",)


def _pages():
    for name in PAGES:
        p = ROOT / name
        assert p.exists(), f"{name} 不存在，測試目標錯了"
        yield name, p.read_text(encoding="utf-8")


def test_static_pages_do_not_link_purged_buckets():
    """首頁不得出現 renders／uploads 的公開網址。"""
    bad = {}
    for name, html in _pages():
        hits = [b for b in PURGED_BUCKETS if b in html]
        if hits:
            bad[name] = hits
    assert not bad, (
        f"靜態頁引用了會被保留期清掉的 storage 網址：{bad}。"
        "案例圖請放 assets/cases/ 由 Vercel 服務。")


def test_case_images_exist_on_disk():
    """首頁引用的每一張 assets/ 圖都要真的存在，而且不是零位元組。"""
    missing, empty = [], []
    for name, html in _pages():
        for rel in re.findall(r'src="(assets/[^"]+)"', html):
            f = ROOT / rel
            if not f.exists():
                missing.append(f"{name} → {rel}")
            elif f.stat().st_size == 0:
                empty.append(rel)
    assert not missing, f"首頁引用了不存在的圖：{missing}"
    assert not empty, f"這些圖是 0 位元組：{empty}"


def test_case_images_are_real_jpegs():
    """檔案要是真的 JPEG（擋掉把 HTML 錯誤頁存成 .jpg 這種事）。"""
    broken = []
    for f in sorted((ROOT / "assets" / "cases").glob("*.jpg")):
        head = f.read_bytes()[:3]
        if head != b"\xff\xd8\xff":
            broken.append(f"{f.name} 開頭是 {head!r}")
    assert not broken, f"不是合法 JPEG：{broken}"


def test_homepage_style_names_match_style_form():
    """首頁的風格名必須與客戶在選風格頁看到的一致。

    2026-09-11：首頁寫「現代簡約」、選風格頁卻是「都會簡約」，六格有五格不一致。
    """
    form = (ROOT / "style-form.html").read_text(encoding="utf-8")
    official = dict(re.findall(r"id:'([a-z-]+)',\s*emoji:'[^']*',\s*name:'([^']+)'", form))
    assert len(official) >= 9, f"從 style-form 只解析到 {len(official)} 種風格，解析壞了"

    home = (ROOT / "index.html").read_text(encoding="utf-8")
    sec = re.search(r'<div class="examples-grid".*?\n    </div>', home, re.S)
    assert sec, "首頁找不到 examples-grid"
    labels = re.findall(r'margin-bottom:4px;">([^<]+)</div>', sec.group(0))
    assert labels, "首頁案例區抓不到任何風格標籤"

    stray = [x for x in labels if x not in official.values()]
    assert not stray, (
        f"首頁出現不在 style-form 的風格名：{stray}；"
        f"客戶選得到的是 {sorted(official.values())}")

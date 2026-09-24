# -*- coding: utf-8 -*-
"""沙發牆寬（2026-09-24）：只有客人自己點了靠左／右才問，大概就好、可以跳過。

它能做的只有選品：拿掉放不下的沙發，並用真的牆寬取代「房間大小猜不到就當小空間」。
⚠️ 它**不會**讓出圖時沙發畫成正確大小——模型畫的沙發本來就是目標的 1.5～6 倍，
   那是另一個還沒解的問題（memory: sofa-target-size-vs-render-size）。文案別承諾精準尺寸。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import api
import furniture_match as fm

LIVE = ("modern", "cream", "nordic", "japanese", "wood", "luxury", "french", "muji", "chinese-modern")


def _sofa(name, dims=""):
    return {"category": "沙發", "name_zh": name, "dimensions": dims}


@pytest.mark.parametrize("raw,want", [(350, 320), ("420", 390), (None, None), ("", None),
                                      ("abc", None), (100, None), (5000, None)])
def test_cap_from_wall(raw, want):
    assert fm.sofa_cap_from_wall(raw) == want


def test_cap_drops_measured_sofas_that_do_not_fit():
    cat = [_sofa("三人座沙發", "寬230x深90x高85cm"), _sofa("雙人座沙發", "寬160x深85x高80cm"), _sofa("布沙發")]
    got = [x["name_zh"] for x in fm._catalog_for_sofa_cap(cat, 190)]
    assert got == ["雙人座沙發", "布沙發"], "量不到寬度的一般沙發要留著（約八成沒寬度）"


def test_small_wall_also_drops_unmeasured_corner_sofas():
    """實測：牆 220 → 上限 190，配到一張量不到寬的「L型沙發床」。"""
    cat = [_sofa("布質L型沙發床"), _sofa("轉角沙發"), _sofa("貴妃沙發"), _sofa("布沙發")]
    assert [x["name_zh"] for x in fm._catalog_for_sofa_cap(cat, 190)] == ["布沙發"]
    # 牆夠寬就不擋轉角型
    assert len(fm._catalog_for_sofa_cap(cat, 300)) == 4


def test_sofas_known_to_be_too_wide_are_never_put_back():
    """🔴 GPT 2026-09-24：舊版「篩完沒沙發就整池放回」，會把確定放不下的推給客人。
    寧可客廳清單少一張沙發（實測配對照常完成），也不推確定放不下的。"""
    cat = [_sofa("三人座沙發", "寬230x深90x高85cm"), _sofa("L型沙發"),
           {"category": "茶几", "name_zh": "茶几", "dimensions": ""}]
    assert [x["name_zh"] for x in fm._catalog_for_sofa_cap(cat, 150)] == ["茶几"]


def test_enrich_with_only_oversized_sofas_recommends_no_sofa(monkeypatch):
    wide = [x for x in fm.load_catalog()
            if fm.resolve_category(x) != "sofa" or (fm.extract_item_width_cm(x) or 0) > 250]
    monkeypatch.setattr(fm, "load_catalog", lambda: wide)
    out = fm.enrich_renders([{"style": "modern", "flux_prompt": "living room sofa"}],
                            analysis={}, room_type="living", sofa_wall_cm=220)
    sofas = [f["name_zh"] for f in out[0]["matched_furniture"] if f["category_en"] == "sofa"]
    assert sofas == [], f"牆 220 卻推了：{sofas}"


@pytest.mark.parametrize("wall", [220, 300])
def test_matched_sofa_fits_the_wall_in_every_style(wall):
    cap = fm.sofa_cap_from_wall(wall)
    bad = []
    for style in LIVE:
        out = fm.enrich_renders([{"style": style, "flux_prompt": "living room sofa tv"}],
                                analysis={}, room_type="living", sofa_wall_cm=wall)
        sofas = [f for f in out[0]["matched_furniture"] if f["category_en"] == "sofa"]
        if not sofas:
            bad.append((style, "沒沙發")); continue
        s = sofas[0]
        w = fm.extract_item_width_cm({"category": "沙發", "name_zh": s["name_zh"], "dimensions": s["dimensions"]})
        corner = any(k in s["name_zh"].lower() for k in fm._SOFA_CORNER_KW)
        if (w and w > cap) or (w is None and corner and cap < fm.SOFA_CORNER_MIN_CM):
            bad.append((style, w, s["name_zh"][:30]))
    assert not bad, f"牆 {wall} 配到放不下的沙發：{bad}"


def test_real_wall_overrides_the_small_room_guess(monkeypatch):
    """房間尺寸抓不到時系統預設當小空間；客人說牆有 4.5 公尺，三人座就不該被降權。"""
    seen = []
    real = fm.match_furniture
    monkeypatch.setattr(fm, "match_furniture",
                        lambda *a, **k: seen.append(k["is_small_room"]) or real(*a, **k))
    fm.enrich_renders([{"style": "modern", "flux_prompt": "sofa"}], analysis={}, room_type="living")
    fm.enrich_renders([{"style": "modern", "flux_prompt": "sofa"}], analysis={}, room_type="living", sofa_wall_cm=450)
    fm.enrich_renders([{"style": "modern", "flux_prompt": "sofa"}], analysis={}, room_type="living", sofa_wall_cm=220)
    assert seen == [True, False, True]


def test_wall_width_is_ignored_outside_the_living_room(monkeypatch):
    seen = []
    real = fm.match_furniture
    monkeypatch.setattr(fm, "match_furniture",
                        lambda *a, **k: seen.append(k["is_small_room"]) or real(*a, **k))
    fm.enrich_renders([{"style": "modern", "flux_prompt": "bed"}], analysis={}, room_type="bedroom", sofa_wall_cm=450)
    assert seen == [True], "臥室不該吃到客廳的牆寬"


@pytest.mark.parametrize("raw,want", [("350", 350), ("", None), ("abc", None), ("80", None)])
def test_parse_sofa_wall_cm(raw, want):
    assert api._parse_sofa_wall_cm(raw) == want


def test_job_endpoint_hands_wall_width_to_the_pipeline(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(api, "run_pipeline", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(api, "sb_upsert", lambda *a, **k: True)
    monkeypatch.setattr(api, "JOBS_DIR", Path(tmp_path) / "jobs")
    monkeypatch.setattr(api, "UPLOADS_DIR", Path(tmp_path) / "uploads")
    (Path(tmp_path) / "jobs").mkdir(parents=True)
    up = api.UPLOADS_DIR / "PYTEST_WALL"
    up.mkdir(parents=True)
    (up / "paths.json").write_text(json.dumps([str(up / "p0.jpg")]), encoding="utf-8")
    from fastapi.testclient import TestClient
    r = TestClient(api.app).post("/api/job", data={
        "upload_id": "PYTEST_WALL", "styles": "modern", "plan": "A", "space_type": "living",
        "sofa_wall_cm": "350"})
    assert r.status_code == 200, r.text
    assert seen.get("sofa_wall_cm") == 350

# -*- coding: utf-8 -*-
"""臥室床型：客人只選「單人／雙人」（2026-09-24 產品決定：選，不是量）。

前端付款頁 → /api/job 的 bed_sizes_json → run_pipeline → enrich_renders(bed_size=)
→ _catalog_for_bed_size 篩掉另一類的床。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import api
import furniture_match as fm

LIVE = ("modern", "cream", "nordic", "japanese", "wood", "luxury", "french", "muji", "chinese-modern")


def _bed(name, dims=""):
    return {"category": "床架", "name_zh": name, "dimensions": dims}


# ── 分類 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,dims,want", [
    ("木紋雙人床架", "寬90x深188x高25cm", "single"),        # 🔴 寬度推翻品名（GPT 抓的實例）
    ("原木雙人床架附收納抽屜", "寬107x深189x高26cm", "single"),
    ("木紋雙人床架", "寬152x深188x高25cm", "double"),
    ("KLEPPSTAD 床框，白色，90x200 公分", "", "single"),      # 品名裡的床墊規格
    ("MALM 掀床，白色，150x200 公分", "", "double"),
    ("3.5尺單人加大床架", "", "single"),                     # 3.5尺 不能被讀成 5尺
    ("本木 收納側掀床架-單人加大3.5尺", "", "single"),        # 「單人加大」是單人
    ("標準5尺雙人床架", "", "double"),
    ("6尺床架", "", "double"),
    ("特大7尺床架", "", "double"),
    ("三尺床", "", "single"),
    ("日式簡約原木板條床架", "", None),                       # 真的分不出來
])
def test_bed_size_class(name, dims, want):
    assert fm.bed_size_class(_bed(name, dims)) == want


def test_mattress_text_in_dims_does_not_decide_the_class():
    """尺寸欄的床墊規格不是床架外徑——分類退回品名，而不是讀床墊寬。"""
    assert fm.bed_size_class(_bed("原木單人床架", "適用床墊尺寸：150 x 190 cm")) == "single"


# ── 篩選 ────────────────────────────────────────────────────────────

def test_filter_drops_the_other_size_and_unknowns():
    cat = [_bed("單人床架A"), _bed("雙人床架B"), _bed("板條床架C"),
           {"category": "床頭櫃", "name_zh": "床頭櫃", "dimensions": ""}]
    got = [x["name_zh"] for x in fm._catalog_for_bed_size(cat, "single")]
    assert got == ["單人床架A", "床頭櫃"]


def test_filter_keeps_unknowns_only_when_nothing_is_certain():
    cat = [_bed("雙人床架B"), _bed("板條床架C")]
    assert [x["name_zh"] for x in fm._catalog_for_bed_size(cat, "single")] == ["板條床架C"]


def test_filter_never_leaves_a_bedroom_without_a_bed():
    cat = [_bed("雙人床架B")]
    assert fm._catalog_for_bed_size(cat, "single") == cat


def test_no_choice_means_no_filter():
    cat = [_bed("雙人床架B"), _bed("單人床架A")]
    assert fm._catalog_for_bed_size(cat, "") is cat


@pytest.fixture(scope="module")
def catalog():
    return fm.load_catalog()


@pytest.mark.parametrize("size", ["single", "double"])
def test_every_live_style_gets_a_bed_of_the_chosen_size(catalog, size):
    """每種上線風格 × 每種床型，配出來的床都要是客人選的那一類。
    （cream 目錄沒有單人床——床型優先於風格，會跨風格配單人床。）"""
    wrong = []
    for style in LIVE:
        out = fm.enrich_renders([{"style": style, "flux_prompt": "bedroom with bed"}],
                                analysis={}, room_type="bedroom", bed_size=size)
        beds = [f for f in out[0]["matched_furniture"] if f["category_en"] == "bed"]
        got = fm.bed_size_class({"name_zh": beds[0]["name_zh"], "dimensions": beds[0]["dimensions"]}) if beds else "無床"
        if got != size:
            wrong.append((style, got))
    assert not wrong, f"選{size}卻配到：{wrong}"


def test_bed_size_does_not_touch_the_living_room():
    out = fm.enrich_renders([{"style": "modern", "flux_prompt": "living room"}],
                            analysis={}, room_type="living", bed_size="single")
    assert "bed" not in [f["category_en"] for f in out[0]["matched_furniture"]]


# ── API 接線 ────────────────────────────────────────────────────────

def test_parse_bed_sizes_keeps_only_valid_pairs():
    raw = json.dumps({"bedroom_1": "double", "bedroom": "single", "living": "double",
                      "bedroom_2": "king", "bedroom_x": "single"})
    assert api._parse_bed_sizes(raw) == {"bedroom_1": "double", "bedroom": "single"}
    assert api._parse_bed_sizes("not json") == {}
    assert api._parse_bed_sizes("[1,2]") == {}


def test_bed_size_for_room():
    bs = {"bedroom_1": "double", "bedroom": "single"}
    assert api._bed_size_for_room(bs, "bedroom_1") == "double"
    assert api._bed_size_for_room(bs, "bedroom_2") == "single"   # 單一空間只送 'bedroom'
    assert api._bed_size_for_room(bs, "living") == ""
    assert api._bed_size_for_room({}, "bedroom_1") == ""


def test_job_endpoint_hands_bed_sizes_to_the_pipeline(monkeypatch, tmp_path):
    """真的打 /api/job，確認選擇有送到 run_pipeline（不跑 pipeline、不碰 Supabase）。"""
    seen = {}
    monkeypatch.setattr(api, "run_pipeline", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(api, "sb_upsert", lambda *a, **k: True)
    monkeypatch.setattr(api, "JOBS_DIR", Path(tmp_path) / "jobs")
    monkeypatch.setattr(api, "UPLOADS_DIR", Path(tmp_path) / "uploads")
    (Path(tmp_path) / "jobs").mkdir(parents=True)
    up = api.UPLOADS_DIR / "PYTEST_BED"
    up.mkdir(parents=True)
    (up / "paths.json").write_text(json.dumps([str(up / "p0.jpg")]), encoding="utf-8")
    from fastapi.testclient import TestClient
    r = TestClient(api.app).post("/api/job", data={
        "upload_id": "PYTEST_BED", "styles": "modern", "plan": "A", "space_type": "bedroom",
        "bed_sizes_json": json.dumps({"bedroom": "double", "living": "single"})})
    assert r.status_code == 200, r.text
    assert seen.get("bed_sizes") == {"bedroom": "double"}

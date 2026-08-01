# -*- coding: utf-8 -*-
"""分區確認頁要畫出「門→對面牆」禁區，而且必須是規劃器那**同一份**多邊形。

用戶問了兩次「灰色有實做到牆壁嗎」——答案本來是沒有：`draw_overlay()` 只畫
`proposed_zones` 的軸對齊方框，門→對面牆那條帶只存在於
①S2 引導圖（紅色，只給模型看）②legacy 規劃器內部（從不畫出來）。
顯示層與生成層不同步＝客戶確認的東西不是系統遵守的東西。

鐵則：分區頁**不可以自己再拉一個灰色 bbox**。多畫一份就是第三套口徑，
正是這條帶當初出問題的原因。這裡的測試就是鎖住「同一份幾何」。
"""
from __future__ import annotations

import json
from pathlib import Path

import layout_geometry_s2 as s2
import pytest
import zoning_v2
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"
GEOM = json.loads((FIXTURES / "_entrance_no_go_geom.json").read_text(encoding="utf-8"))


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "room.jpg"
    Image.new("RGB", (1008, 756), (240, 240, 240)).save(path, "JPEG", quality=92)
    return path


def test_overlay_polygon_equals_the_planner_polygon(photo, monkeypatch):
    """分區頁畫的多邊形必須跟規劃器算的逐點相同（同一份幾何，不是另外拉的）。"""
    direction = [0.999926791, -0.012100107]
    monkeypatch.setattr(
        "layout_floor_reference_s2.estimate_transverse_floor_reference",
        lambda *a, **k: {"status": "observed", "direction_xy": direction,
                         "support_count": 9, "unsafe_codes": []})

    norm = zoning_v2.entrance_no_go_polygon_for_overlay(photo, GEOM)
    assert norm, "分區頁算不出禁區"

    with Image.open(photo) as opened:
        width, height = opened.size
    plan = s2.build_s2_plan(
        GEOM, width=width, height=height,
        expected_source_photo_index=GEOM.get("source_photo_index") or 0,
        sofa_side="free", transverse_direction_xy=direction,
        transverse_reference={"status": "observed", "direction_xy": direction,
                              "support_count": 9, "unsafe_codes": []})
    planner = plan["entrance_hard_no_go"]["polygon"]

    drawn = [[round(x * width, 3), round(y * height, 3)] for x, y in norm]
    assert drawn == [[round(x, 3), round(y, 3)] for x, y in planner], (
        "分區頁畫的不是規劃器那一份＝第三套口徑又出現了")


def test_band_actually_reaches_the_opposite_wall(photo, monkeypatch):
    """反貧血：畫出來的帶必須真的橫跨到對面牆，不是門旁邊一小塊。

    用戶原話：「灰色 有實做 到牆壁?」——這條就是在答那句。
    """
    direction = [0.999926791, -0.012100107]
    monkeypatch.setattr(
        "layout_floor_reference_s2.estimate_transverse_floor_reference",
        lambda *a, **k: {"status": "observed", "direction_xy": direction,
                         "support_count": 9, "unsafe_codes": []})
    norm = zoning_v2.entrance_no_go_polygon_for_overlay(photo, GEOM)
    xs = [x for x, _ in norm]
    door = GEOM["elements"]["door_floor_contact"]["segment_yx1000"]
    door_x = [p[1] / 1000.0 for p in door]
    assert min(xs) <= min(door_x) + 0.05, "帶沒有從門那端起算"
    assert max(xs) - min(xs) > 0.55, (
        f"帶只橫跨畫面 {max(xs)-min(xs):.0%}，沒有到對面牆")


def test_overlay_draws_the_band(photo, tmp_path, monkeypatch):
    """畫出來的圖必須真的多了灰色像素——不是只回傳座標沒畫。"""
    direction = [0.999926791, -0.012100107]
    monkeypatch.setattr(
        "layout_floor_reference_s2.estimate_transverse_floor_reference",
        lambda *a, **k: {"status": "observed", "direction_xy": direction,
                         "support_count": 9, "unsafe_codes": []})
    norm = zoning_v2.entrance_no_go_polygon_for_overlay(photo, GEOM)

    without = tmp_path / "a.png"
    with_band = tmp_path / "b.png"
    zoning_v2.draw_overlay(photo, {}, "t", without)
    zoning_v2.draw_overlay(photo, {}, "t", with_band, entrance_no_go_norm=norm)

    a = Image.open(without).convert("RGB")
    b = Image.open(with_band).convert("RGB")
    assert a.size == b.size
    diff = sum(1 for pa, pb in zip(a.getdata(), b.getdata()) if pa != pb)
    assert diff > a.size[0] * a.size[1] * 0.01, (
        f"只有 {diff} 個像素不同，禁區沒被畫上去")


# ── 關鍵路徑保護：分區頁不能因為這個功能掛掉 ──────────────────────────

@pytest.mark.parametrize("bad", [
    None, {}, {"elements": None}, {"elements": {}},
    {"elements": {"door_floor_contact": {"segment_yx1000": [[1, 1], [2, 2]]}}},
])
def test_never_raises_on_bad_geometry(photo, bad):
    """缺幾何／壞幾何一律回 None，不得丟例外——/api/zoning 掛掉全站就不能下單。"""
    assert zoning_v2.entrance_no_go_polygon_for_overlay(photo, bad) is None


def test_missing_photo_returns_none(tmp_path):
    """照片讀不到也不能炸。"""
    assert zoning_v2.entrance_no_go_polygon_for_overlay(
        tmp_path / "nope.jpg", GEOM) is None


def test_draw_overlay_without_band_is_unchanged(photo, tmp_path):
    """不給禁區時，分區頁行為與改動前完全一致（沒弄壞既有的頁面）。"""
    zones = {"living_zone": {"bbox_on_best_photo": [300, 100, 900, 900]}}
    a = tmp_path / "x.png"
    b = tmp_path / "y.png"
    zoning_v2.draw_overlay(photo, zones, "t", a)
    zoning_v2.draw_overlay(photo, zones, "t", b, entrance_no_go_norm=None)
    assert a.read_bytes() == b.read_bytes()

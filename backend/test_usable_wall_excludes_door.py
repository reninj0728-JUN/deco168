# -*- coding: utf-8 -*-
"""可用牆段必須把大門挖掉。

91F88E0F（2026-08-01）：zoning 標了一段 `id="left_wall_after_door"`、
`t_start=0.25 / t_end=1.0`，但把 `door_floor_contact` 投影到同一條牆線上，
大門實際佔用 `t=0.333–0.637`——**那段「門後可用牆」有 30% 就是大門本身**。

後果：規劃器把靠左牆的家具擺在大門上，13 個候選的門距全是 `0/137`，
一個都不合格，判官連跑都沒跑到，訂單在付費前擋死。

宣告的 t 範圍會騙人；門洞與牆線本來就在同一份 payload 裡，位置自己算就好。

波及面（34 份去重幾何）：改善 19、退步 1、不變 14。
"""
from __future__ import annotations

import copy

import pytest

import layout_geometry_s2 as s2

WIDTH, HEIGHT = 4032, 3024


def _px(point):
    """yx1000 → 像素 (x, y)，跟 _shape_from_element 同一個換算。"""
    return (point[1] / 1000 * WIDTH, point[0] / 1000 * HEIGHT)


# 91F88E0F 線上實際存下來的數字
LEFT_WALL = [_px([980, 0]), _px([578, 385])]
RIGHT_WALL = [_px([980, 1000]), _px([615, 616])]
DOOR_CONTACT = [_px([846, 128]), _px([736, 258])]


# ── 門在哪一面牆、佔多少 ─────────────────────────────────────────
def test_door_span_matches_the_hand_computed_range():
    span = s2._door_span_on_wall(DOOR_CONTACT, LEFT_WALL)
    assert span is not None, "大門明明在左牆上"
    low, high = span
    assert low == pytest.approx(0.333, abs=0.01)
    assert high == pytest.approx(0.637, abs=0.01)


def test_door_on_the_other_wall_is_not_subtracted():
    """左牆的門不可以從右牆挖掉——否則會平白砍掉一整面好牆。"""
    assert s2._door_span_on_wall(DOOR_CONTACT, RIGHT_WALL) is None


def test_missing_or_broken_door_contact_changes_nothing():
    for bad in (None, [], [_px([846, 128])], "nope"):
        assert s2._door_span_on_wall(bad, LEFT_WALL) is None
    assert s2._door_span_on_wall(DOOR_CONTACT, None) is None


# ── 挖掉之後剩什麼 ───────────────────────────────────────────────
def test_declared_after_door_span_actually_excludes_the_door():
    """91F88E0F 的核心：宣告 0.25–1.0，實際只能用 0.637–1.0。"""
    spans = s2._wall_spans_outside_door(0.25, 1.0, (0.333, 0.637))
    assert len(spans) == 1, f"門前那 0.08 太短，應該被丟掉：{spans}"
    start, end = spans[0]
    assert start == pytest.approx(0.637, abs=0.01)
    assert end == pytest.approx(1.0)


def test_door_in_the_middle_leaves_two_pieces():
    spans = s2._wall_spans_outside_door(0.0, 1.0, (0.4, 0.6))
    assert [(round(a, 2), round(b, 2)) for a, b in spans] == [(0.0, 0.4), (0.6, 1.0)]


def test_door_covering_the_whole_segment_leaves_nothing():
    """整段都是門 → 這面牆沒有可用段，該講實話，不是硬留一段給家具貼門。"""
    assert s2._wall_spans_outside_door(0.3, 0.6, (0.0, 1.0)) == []


def test_no_door_span_leaves_the_segment_untouched():
    assert s2._wall_spans_outside_door(0.2, 0.9, None) == [(0.2, 0.9)]


def test_slivers_below_the_minimum_span_are_dropped():
    tiny = s2.MIN_WALL_SPAN_T / 2
    spans = s2._wall_spans_outside_door(0.0, 1.0, (tiny, 1.0))
    assert spans == [], f"比 MIN_WALL_SPAN_T 還短的碎片不該留：{spans}"


# ── 端到端：規劃器不再把家具排到門上 ────────────────────────────
def _geometry_91f():
    return {
        "schema_version": "struct-geometry-v1",
        "status": "observed",
        "source_photo_index": 0,
        "elements": {
            "walkway": {"kind": "walkway", "status": "observed", "confidence": "high",
                        "visibility": "full",
                        "polygon_yx1000": [[846, 128], [736, 258], [578, 436],
                                           [578, 480], [615, 616], [980, 500]]},
            "door_quad": {"kind": "door_quad", "status": "observed", "confidence": "high",
                          "visibility": "full",
                          "polygon_yx1000": [[325, 121], [376, 258], [736, 258], [846, 128]]},
            "living_floor": {"kind": "living_floor", "status": "observed",
                             "confidence": "high", "visibility": "full",
                             "polygon_yx1000": [[846, 128], [736, 258], [615, 616],
                                                [980, 1000], [980, 0]]},
            "left_wall_floor": {"kind": "wall_floor_boundary", "status": "observed",
                                "confidence": "high", "visibility": "full",
                                "segment_yx1000": [[980, 0], [578, 385]]},
            "entrance_landing": {"kind": "entrance_landing", "status": "observed",
                                 "confidence": "high", "visibility": "full",
                                 "polygon_yx1000": [[846, 128], [736, 258],
                                                    [700, 260], [870, 100]]},
            "right_wall_floor": {"kind": "wall_floor_boundary", "status": "observed",
                                 "confidence": "high", "visibility": "full",
                                 "segment_yx1000": [[980, 1000], [615, 616]]},
            "door_floor_contact": {"kind": "door_floor_contact", "status": "observed",
                                   "confidence": "high", "visibility": "full",
                                   "segment_yx1000": [[846, 128], [736, 258]]},
        },
        # 這就是騙人的那一段：名字說 after_door，範圍卻從門前蓋到底
        "usable_wall_segments": [
            {"id": "left_wall_after_door", "side": "left", "t_start": 0.25, "t_end": 1,
             "status": "observed", "confidence": "high", "visibility": "full"},
            {"id": "right_wall_full", "side": "right", "t_start": 0, "t_end": 1,
             "status": "observed", "confidence": "high", "visibility": "full"},
        ],
    }


def _left_usable_spans(plan_geometry):
    return [
        (item["shape"], item["evidence"])
        for item in plan_geometry
        if item.get("kind") == "usable_wall_segment" and "left" in str(item.get("id"))
    ]


def test_planner_no_longer_places_left_wall_furniture_on_the_door():
    """端到端：靠左牆的家具不得再落在大門佔用的 t 區間裡。"""
    plan = s2.build_s2_plan(
        _geometry_91f(), width=WIDTH, height=HEIGHT,
        expected_source_photo_index=0, sofa_side="free", can_float=False)
    door_low, door_high = s2._door_span_on_wall(DOOR_CONTACT, LEFT_WALL)
    left = [item for item in (plan.get("geometry") or [])
            if item.get("kind") == "usable_wall_segment"
            and "left" in str(item.get("geometry_id") or item.get("id") or "")]
    assert left, "左牆應該還有可用段（門後那截）"
    for item in left:
        ts = sorted(s2._t_on_line(LEFT_WALL, point)
                    for point in item["shape"]["coordinates"])
        # 必須驗「區間重疊」而不是「端點落在門內」——宣告的 0.25–1.0 兩個端點
        # 都在門外，卻整段橫跨大門。第一版只查端點，蓄意破壞時沒被抓到。
        overlap = min(ts[1], door_high) - max(ts[0], door_low)
        assert overlap <= 1e-6, (
            f"可用牆段 t={ts[0]:.3f}–{ts[1]:.3f} 與大門 "
            f"{door_low:.3f}–{door_high:.3f} 重疊 {overlap:.3f}")


def test_the_declared_span_alone_would_have_put_furniture_on_the_door():
    """先證明這份 fixture 真的有病，否則上面那條可能只是空過。"""
    declared = next(s for s in _geometry_91f()["usable_wall_segments"]
                    if s["side"] == "left")
    low, high = s2._door_span_on_wall(DOOR_CONTACT, LEFT_WALL)
    overlap = min(high, declared["t_end"]) - max(low, declared["t_start"])
    assert overlap > 0.3, f"宣告的可用段與大門重疊 {overlap:.3f}，這就是 91F88E0F 的病"


def test_geometry_without_door_overlap_is_unchanged():
    """門不在宣告區間裡的房型，可用牆段一格都不該動。"""
    raw = _geometry_91f()
    raw["usable_wall_segments"][0]["t_start"] = 0.7   # 完全在門後
    plan = s2.build_s2_plan(raw, width=WIDTH, height=HEIGHT,
                            expected_source_photo_index=0, sofa_side="free",
                            can_float=False)
    left = [item for item in (plan.get("geometry") or [])
            if item.get("kind") == "usable_wall_segment"
            and "left" in str(item.get("geometry_id") or item.get("id") or "")]
    assert len(left) == 1
    ts = sorted(s2._t_on_line(LEFT_WALL, p) for p in left[0]["shape"]["coordinates"])
    assert ts[0] == pytest.approx(0.7, abs=0.01)
    assert ts[1] == pytest.approx(1.0, abs=0.01)


def test_wall_entirely_covered_by_the_door_yields_no_usable_segment():
    """宣告的可用段整段都是門 → 不得硬留給家具貼門，要老實說沒有可用牆。"""
    raw = _geometry_91f()
    raw["usable_wall_segments"][0]["t_start"] = 0.35   # 完全落在門的 0.333–0.637 內
    raw["usable_wall_segments"][0]["t_end"] = 0.60
    plan = s2.build_s2_plan(raw, width=WIDTH, height=HEIGHT,
                            expected_source_photo_index=0, sofa_side="free",
                            can_float=False)
    left = [item for item in (plan.get("geometry") or [])
            if item.get("kind") == "usable_wall_segment"
            and "left" in str(item.get("geometry_id") or item.get("id") or "")]
    assert left == [], f"整段都是門卻還留了可用牆段：{left}"
    assert "NO_USABLE_WALL" in (plan.get("unsafe_codes") or []), plan.get("unsafe_codes")
    assert plan.get("disposition") == "BLOCKED"


def test_right_wall_is_not_shortened_by_the_left_wall_door():
    raw = _geometry_91f()
    plan = s2.build_s2_plan(raw, width=WIDTH, height=HEIGHT,
                            expected_source_photo_index=0, sofa_side="free",
                            can_float=False)
    right = [item for item in (plan.get("geometry") or [])
             if item.get("kind") == "usable_wall_segment"
             and "right" in str(item.get("geometry_id") or item.get("id") or "")]
    assert len(right) == 1, "右牆沒有門，不該被切開"
    ts = sorted(s2._t_on_line(RIGHT_WALL, p) for p in right[0]["shape"]["coordinates"])
    assert ts[0] == pytest.approx(0.0, abs=0.01)
    assert ts[1] == pytest.approx(1.0, abs=0.01)

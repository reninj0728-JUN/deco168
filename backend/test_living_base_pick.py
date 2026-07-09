# -*- coding: utf-8 -*-
"""P1：全室客廳底圖不得 first-wins 走廊角（C79C7ECC 根因）。"""
import api


def test_score_prefers_window_note():
    corridor = {"target_zone": "living", "photo_contains": ["living", "dining"], "target_note": ""}
    windowed = {
        "target_zone": "living",
        "photo_contains": ["living", "dining"],
        "target_note": "客廳靠窗",
        "target_location_hint": "unspecified",
    }
    assert api._score_photo_for_room(windowed, "living") > api._score_photo_for_room(corridor, "living")


def test_build_user_regions_picks_noted_living_not_first():
    """模擬 C79：photo_01 走廊 living、photo_03 客廳靠窗 → 必須選 idx=2。"""
    paths = [
        "uploads/x/photo_01.jpg",
        "uploads/x/photo_02.jpg",
        "uploads/x/photo_03.jpg",
        "uploads/x/photo_04.jpg",
    ]
    meta = {
        "uploads/x/photo_01.jpg": {
            "target_zone": "living",
            "photo_contains": ["living", "dining"],
            "target_note": "",
        },
        "uploads/x/photo_02.jpg": {
            "target_zone": "bedroom",
            "photo_contains": ["bedroom"],
            "target_note": "",
        },
        "uploads/x/photo_03.jpg": {
            "target_zone": "living",
            "photo_contains": ["living", "dining"],
            "target_note": "客廳靠窗",
        },
        "uploads/x/photo_04.jpg": {
            "target_zone": "study",
            "photo_contains": ["study"],
            "target_note": "",
        },
    }
    regions = api._build_user_regions_whole(paths, meta)
    by_rt = {r["room_type"]: r["best_photo_index"] for r in regions}
    assert by_rt["living"] == 2, f"expected living base idx=2, got {by_rt}"
    assert by_rt["bedroom"] == 1
    assert by_rt["study"] == 3
    # 客廳仍排第一
    assert regions[0]["room_type"] == "living"


def test_build_user_regions_first_wins_when_scores_equal():
    paths = ["a.jpg", "b.jpg"]
    meta = {
        "a.jpg": {"target_zone": "living", "photo_contains": ["living"], "target_note": ""},
        "b.jpg": {"target_zone": "living", "photo_contains": ["living"], "target_note": ""},
    }
    regions = api._build_user_regions_whole(paths, meta)
    assert len(regions) == 1
    assert regions[0]["best_photo_index"] == 0

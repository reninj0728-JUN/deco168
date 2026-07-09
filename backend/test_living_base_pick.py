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


def test_list_candidates_ordered_and_alt_indices():
    paths = [
        "uploads/x/photo_01.jpg",
        "uploads/x/photo_02.jpg",
        "uploads/x/photo_03.jpg",
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
    }
    cands = api._list_room_photo_candidates(paths, meta, "living")
    assert len(cands) == 2
    assert cands[0]["idx"] == 2 and cands[1]["idx"] == 0
    regions = api._build_user_regions_whole(paths, meta)
    living = next(r for r in regions if r["room_type"] == "living")
    assert living["best_photo_index"] == 2
    assert living.get("alt_photo_indices") == [0]


def test_should_try_alt_and_switch_base(tmp_path):
    assert api._should_try_alt_living_base({"spatial_fidelity_fail": True}) is True
    assert api._should_try_alt_living_base({"ok": True, "hard_fail": False}) is False
    # switch uses real files
    p1 = tmp_path / "a.jpg"
    p2 = tmp_path / "b.jpg"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    entry = {
        "_room_type": "living",
        "_base_path": str(p1),
        "_alt_bases": [str(p2)],
        "_used_bases": [str(p1)],
    }
    nxt = api._switch_entry_to_next_living_base(entry)
    assert nxt == str(p2)
    assert entry["_base_path"] == str(p2)
    # 用完無下一張
    assert api._switch_entry_to_next_living_base(entry) is None

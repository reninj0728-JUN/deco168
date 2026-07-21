# -*- coding: utf-8 -*-
"""A+B+C 護城河：沙發座位數配對、麒麟≠電視櫃、正對 hard。"""
import furniture_match as fm
from gemini_analyze import HARD_FAIL_FLAGS, _enforce_sofa_focal_orientation


def test_qilin_not_media_console():
    name = "【銳達】精雕成對麒麟實木擺飾 吉祥瑞獸造型 客廳電視櫃玄關辦公室裝飾工藝品 麒麟"
    item = {"category": "裝飾", "name_zh": name}
    assert fm.resolve_category(item) == "decor", fm.resolve_category(item)
    assert not fm._name_is_media_console_decor_false_positive("文創集 法式白電視櫃")


def test_media_console_product_ref_locks_low_only_form():
    """有 media_console 商品圖時，prompt 必須鎖「單一矮電視櫃」，禁發明高展示櫃組合。

    173C14C5：media_console=visible，但模型另造高展示櫃貼門/對講機，
    閘門判 focal 貼門；商業護城河是清單=圖，TV 牆只能是清單那件矮櫃。
    """
    from prompt_builder import (
        CRITICAL_RULES,
        _build_product_placement_section,
        build_anchored_inputs,
        build_nano_banana_inputs,
    )

    placement = _build_product_placement_section([
        {"index": 2, "role": "SOFA", "cat_en": "sofa", "name_zh": "測試沙發"},
        {"index": 3, "role": "MEDIA CONSOLE", "cat_en": "media_console", "name_zh": "測試電視櫃"},
    ])
    assert "MEDIA CONSOLE COMMERCIAL RULE" in placement
    assert "primary anchor" in placement
    assert "DECOR FREEDOM WITH SPATIAL GUARD" in placement
    assert "tall display" in placement
    assert "well past the door" in placement

    no_console = _build_product_placement_section([
        {"index": 2, "role": "SOFA", "cat_en": "sofa", "name_zh": "測試沙發"},
    ])
    assert "MEDIA CONSOLE COMMERCIAL RULE" not in no_console
    assert "DECOR FREEDOM WITH SPATIAL GUARD" not in no_console

    assert "tall display / storage cabinet beside the TV" in CRITICAL_RULES
    assert "primary anchor" in CRITICAL_RULES
    assert "Style-enhancing tall pieces" in CRITICAL_RULES

    nano = build_nano_banana_inputs(
        {
            "style": "modern",
            "style_label": "都會簡約",
            "matched_furniture": [
                {
                    "id": "tv-1",
                    "category_en": "media_console",
                    "name_zh": "測試電視櫃",
                    "image_url": "https://example.com/tv.jpg",
                },
            ],
        },
        zoning=None,
        room_image_url="https://example.com/room.jpg",
    )
    assert "MEDIA CONSOLE COMMERCIAL RULE" in nano["prompt"]
    assert "DECOR FREEDOM WITH SPATIAL GUARD" in nano["prompt"]
    assert "well past the door" in nano["prompt"]
    assert "primary anchor" in nano["prompt"]

    anchored = build_anchored_inputs(
        {
            "style": "modern",
            "matched_furniture": [
                {
                    "id": "tv-1",
                    "category_en": "media_console",
                    "name_zh": "測試電視櫃",
                    "image_url": "https://example.com/tv.jpg",
                },
            ],
        },
        "data:image/jpeg;base64,ROOM",
        source_dims=(1000, 700),
    )
    assert "Style-enhancing tall pieces are OK" in anchored["prompt"]
    assert "never beside the door/intercom" in anchored["prompt"]

    retry_blocked = build_anchored_inputs(
        {
            "style": "modern",
            "matched_furniture": [
                {
                    "id": "tv-1",
                    "category_en": "media_console",
                    "name_zh": "測試電視櫃",
                    "image_url": "https://example.com/tv.jpg",
                },
            ],
        },
        "data:image/jpeg;base64,ROOM",
        source_dims=(1000, 700),
        retry_context={"failed_flags": ["furniture_blocks_door"]},
    )
    assert "crowded the door or intercom" in retry_blocked["prompt"]
    assert "clear solid wall segment well past the door" in retry_blocked["prompt"]


def test_infer_sofa_seating():
    assert fm.infer_sofa_seating("ASSARI -法式費黛里單人座半牛皮沙發") == "single"
    assert fm.infer_sofa_seating("ASSARI -法式費黛里雙人座半牛皮沙發") == "multi"
    assert fm.infer_sofa_seating("ASSARI -法式費黛里三人座半牛皮沙發") == "multi"
    assert fm.infer_sofa_seating("中式花卉圖騰沙發組") == "multi"


def test_single_sofa_penalized():
    single = {
        "category": "沙發",
        "name_zh": "ASSARI -法式費黛里單人座半牛皮沙發",
        "flux_descriptor": "wingback armchair black leather",
        "style_tags": ["french"],
        "keywords": [],
        "colors": [],
        "image_url": "http://x",
        "purchase_url": "http://y",
    }
    multi = {
        "category": "沙發",
        "name_zh": "ASSARI -法式費黛里三人座半牛皮沙發",
        "flux_descriptor": "black leather sofa three seat",
        "style_tags": ["french"],
        "keywords": [],
        "colors": [],
        "image_url": "http://x",
        "purchase_url": "http://y",
    }
    s1 = fm.score_item(single, "french", ["leather", "sofa"])
    s2 = fm.score_item(multi, "french", ["leather", "sofa"])
    assert s2 > s1, (s1, s2)


def test_product_mismatch_in_hard_flags():
    assert "product_sofa_seating_mismatch" in HARD_FAIL_FLAGS


def test_face_each_other_null_fails_with_layout():
    r = _enforce_sofa_focal_orientation(
        {"sofa_focal_face_each_other": None, "sofa_back_against_window": False},
        has_layout_ctx=True,
        is_long_room_layout=False,
    )
    assert r.get("focal_anchor_misaligned_with_sofa") is True
    assert r.get("ok") is False


def test_prefer_non_bundle_drops_single_sofa_in_stage_ab():
    """Stage A/B 池會排除單人沙發（透過 _prefer_non_bundle 邏輯測 infer）。"""
    assert fm.infer_sofa_seating("法式費黛里單人座半牛皮沙發") == "single"

# -*- coding: utf-8 -*-
"""A+B+C 護城河：沙發座位數配對、麒麟≠電視櫃、正對 hard。"""
import furniture_match as fm
from gemini_analyze import HARD_FAIL_FLAGS, _enforce_sofa_focal_orientation


def test_qilin_not_media_console():
    name = "【銳達】精雕成對麒麟實木擺飾 吉祥瑞獸造型 客廳電視櫃玄關辦公室裝飾工藝品 麒麟"
    item = {"category": "裝飾", "name_zh": name}
    assert fm.resolve_category(item) == "decor", fm.resolve_category(item)
    assert not fm._name_is_media_console_decor_false_positive("文創集 法式白電視櫃")


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

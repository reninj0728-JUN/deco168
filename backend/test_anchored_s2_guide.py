# -*- coding: utf-8 -*-
"""Anchored renderer must actually receive and obey the S2 guide."""
from __future__ import annotations

from prompt_builder import SYSTEM_PROMPT, build_anchored_inputs, build_nano_banana_inputs
from test_full_pipeline import _gpt_image2_mask_repair_prompt


def test_anchored_inputs_insert_s2_guide_before_product_refs():
    room_url = "data:image/jpeg;base64,ROOM"
    guide_url = "data:image/jpeg;base64,GUIDE"
    entry = {
        "style": "modern",
        "style_label": "現代風",
        "matched_furniture": [
            {
                "id": "sofa-1",
                "category_en": "sofa",
                "name_zh": "測試沙發",
                "image_url": "https://example.com/sofa.jpg",
            }
        ],
    }

    result = build_anchored_inputs(
        entry,
        room_url,
        source_dims=(1000, 700),
        layout_guide_url=guide_url,
    )

    assert result["image_urls"][:3] == [room_url, room_url, guide_url]
    assert result["reference_map"][2]["role"] == "LAYOUT_GUIDE"
    assert "Image 3 is an instruction-only S2 layout guide" in result["prompt"]
    assert "NEVER the output canvas" in result["prompt"]
    assert "ZERO guide graphics" in result["prompt"]
    assert "yellow line" in result["prompt"]
    assert "Add the sofa shown in image 4" in result["prompt"]


def test_anchored_retry_receives_previous_structure_and_circulation_failures():
    result = build_anchored_inputs(
        {"style": "modern", "matched_furniture": []},
        "data:image/jpeg;base64,ROOM",
        source_dims=(1000, 700),
        layout_guide_url="data:image/jpeg;base64,GUIDE",
        retry_context={
            "failed_flags": [
                "spatial_fidelity_fail",
                "furniture_blocks_door",
                "sofa_intrudes_walkway",
                "guide_overlay_present",
            ],
        },
    )

    prompt = result["prompt"]
    assert "CORRECTION PASS" in prompt
    assert "door-leaf count" in prompt
    assert "room geometry and viewpoint changed" in prompt
    assert "complete entrance door swing area" in prompt
    assert "Previous failure: S2 guide graphics leaked into the render" in prompt


def test_anchored_inputs_accept_optional_media_console_reference():
    result = build_anchored_inputs(
        {
            "style": "modern",
            "matched_furniture": [{
                "id": "tv-1",
                "category_en": "media_console",
                "name_zh": "測試電視櫃",
                "image_url": "https://example.com/tv.jpg",
            }],
        },
        "data:image/jpeg;base64,ROOM",
        source_dims=(1000, 700),
        layout_guide_url="data:image/jpeg;base64,GUIDE",
    )

    assert "Add the TV/media console shown in image 4" in result["prompt"]
    assert result["reference_map"][3]["cat_en"] == "media_console"


def test_gpt_image2_multi_image_prompt_hard_blocks_guide_leakage_on_retry():
    result = build_nano_banana_inputs(
        {"style": "modern", "matched_furniture": []},
        zoning=None,
        room_image_url="data:image/jpeg;base64,ROOM",
        layout_guide_url="data:image/jpeg;base64,GUIDE",
        layout_guide_mode="auto_s2_contract",
        retry_context={"failed_flags": ["guide_overlay_present"]},
    )

    assert result["image_urls"][1] == "data:image/jpeg;base64,GUIDE"
    assert "ZERO red, green, blue, or yellow guide" in result["prompt"]
    assert "Previous render leaked the S2 guide" in result["prompt"]


def test_s2_compact_entry_mode_suppresses_oversized_visual_refs_and_accessories():
    result = build_nano_banana_inputs(
        {
            "style": "modern",
            "_s2_compact_entry_mode": True,
            "matched_furniture": [
                {"category_en": "sofa", "image_url": "https://x/sofa.jpg"},
                {"category_en": "coffee_table", "image_url": "https://x/table.jpg"},
                {"category_en": "rug", "image_url": "https://x/rug.jpg"},
                {"category_en": "media_console", "image_url": "https://x/tv.jpg"},
            ],
            "soft_furnishing": [
                {"category_en": "plant", "image_url": "https://x/plant.jpg"},
            ],
        },
        zoning=None,
        room_image_url="data:image/jpeg;base64,ROOM",
        layout_guide_url="data:image/jpeg;base64,GUIDE",
        layout_guide_mode="auto_s2_contract",
    )

    assert result["image_urls"] == [
        "data:image/jpeg;base64,ROOM",
        "data:image/jpeg;base64,GUIDE",
    ]
    assert "120–140 cm" in result["prompt"]
    assert "No rug, coffee table, side table, plant, vase, or floor lamp" in result["prompt"]
    assert "same cross-room centreline" in result["prompt"]


def test_s2_sofa_alignment_edit_adds_local_repair_guide_as_third_reference():
    result = build_nano_banana_inputs(
        {
            "style": "modern",
            "_s2_compact_entry_mode": True,
        },
        zoning=None,
        room_image_url="data:image/jpeg;base64,PREVIOUS_RENDER",
        layout_guide_url="data:image/jpeg;base64,FORMAL_GUIDE",
        layout_guide_mode="auto_s2_contract",
        consistency_ref_url="data:image/jpeg;base64,LOCAL_REPAIR_GUIDE",
        retry_context={
            "sofa_alignment_edit": True,
            "failed_flags": ["furniture_blocks_door"],
        },
    )

    assert result["image_urls"] == [
        "data:image/jpeg;base64,PREVIOUS_RENDER",
        "data:image/jpeg;base64,FORMAL_GUIDE",
        "data:image/jpeg;base64,LOCAL_REPAIR_GUIDE",
    ]
    assert result["reference_map"][2]["kind"] == "LOCAL_REPAIR_GUIDE"
    assert "MOVE ONLY THE SOFA" in result["prompt"]
    assert "local repair guide" in result["prompt"].lower()


def test_gpt_image2_mask_retry_uses_short_local_only_prompt():
    prompt = _gpt_image2_mask_repair_prompt([
        {"index": 3, "kind": "LOCAL_REPAIR_GUIDE"},
    ])

    assert len(prompt) < 1200
    assert "Image #3" in prompt
    assert "completely erase the old sofa" in prompt
    assert "fully contained inside the GREEN body target" in prompt
    assert "Preserve every pixel outside the transparent mask" in prompt
    assert "Apply this interior design style" not in prompt


def test_global_prompt_never_allows_living_sofa_to_face_window():
    lower = SYSTEM_PROMPT.lower()
    assert "sofa must face the tv/media console directly" in lower
    assert "the window for a view" not in lower
    assert "long axis of the room toward the window" not in lower

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


def test_console_door_and_sofa_relocation_masks_reach_fal_without_spending():
    """P2｜遮罩硬修的驗證債：不等隨機重現，用 fixture 證明 routing → 遮罩 →
    fal mask_url 整條通，零 fal 花費。涵蓋 593408CC 電視櫃貼門 + 8AD3E711 沙發跨房搬移。"""
    import json
    import tempfile
    from pathlib import Path

    from PIL import Image

    import api
    from test_full_pipeline import _gpt_image2_mask_data_url, _resolve_render_model

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        prev = root / "render.jpg"
        Image.new("RGB", (1000, 1000), "white").save(prev)

        # ── 電視櫃貼門：routing → console_door 遮罩 → gpt-image-2/edit + mask_url ──
        console_v = {
            "furniture_blocks_door": True,
            "camera_axis_preserved": True,
            "passage_openings_preserved": True,
            "render_bboxes": {
                "entrance_door": [250, 40, 720, 210],
                "focal_anchor": [430, 230, 560, 430],   # 貼門(gap=0)→offender=focal_anchor
                "sofa": [470, 520, 780, 870],
            },
        }
        assert api._door_block_offender(console_v) == "focal_anchor"
        console_entry = {"_room_type": "living"}
        assert api._activate_console_door_edit(
            console_v, {"render_path": str(prev)}, console_entry, str(root), 0, "1"
        ) == str(prev)
        assert console_entry.get("_edit_mask_mode") == "console_door"
        assert console_entry.get("_force_mask_local_edit") is True
        assert _resolve_render_model(console_entry) == "openai/gpt-image-2/edit"
        console_mask_url = _gpt_image2_mask_data_url(console_entry)
        assert console_mask_url and console_mask_url.startswith("data:image/png;base64,")

        # ── 沙發跨房搬移：wrong_side 遮罩需真的進 fal（entry 帶 S2 required）──
        contract = {
            "source": {"size": {"width": 1000, "height": 1000}},
            "decision": {"chosen_candidate_id": "c1"},
            "candidates": [{
                "candidate_id": "c1",
                "sofa_footprint_geometry_id": "g_sofa",
                "notes": ["sofa_side=left"],
            }],
            "geometry": [
                {"geometry_id": "g_sofa",
                 "shape": {"coordinates": [[270, 520], [470, 520], [470, 760], [270, 760]]}},
            ],
        }
        contract_path = root / "contract.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        sofa_v = {
            "sofa_on_wrong_side": True,
            "render_bboxes": {
                "sofa": [520, 620, 760, 900],           # 現在貼右牆(錯)
                "entrance_door": [200, 40, 700, 200],
            },
        }
        sofa_mask = api._build_s2_sofa_edit_mask(
            str(prev), str(contract_path), sofa_v, str(root / "sofa_mask.png"))
        assert sofa_mask
        sofa_entry = {
            "_room_type": "living",
            "_layout_contract_s2_required": True,   # S2 客廳首要條件下沙發遮罩才建
            "_edit_mask_path": sofa_mask,
            "_edit_mask_mode": "sofa",
        }
        assert _resolve_render_model(sofa_entry) == "openai/gpt-image-2/edit"
        sofa_mask_url = _gpt_image2_mask_data_url(sofa_entry)
        assert sofa_mask_url and sofa_mask_url.startswith("data:image/png;base64,")

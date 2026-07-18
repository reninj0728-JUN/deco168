# -*- coding: utf-8 -*-
"""E72F4ADB｜門感知格局回歸測試。

不呼叫 Gemini / FAL。先鎖住資料流與引導圖契約，再改正式程式。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

import api
import prompt_builder as pb


class DoorAwareLayoutTests(unittest.TestCase):
    def _free_zoning_v2(self):
        return {
            "best_photo_index": 0,
            "existing_zones": {
                "entrance_zone": {
                    "where": "文字故意寫右側，bbox 才是真相",
                    "bbox_on_best_photo": [320, 10, 950, 300],
                },
                "walkway": {"bbox_on_best_photo": [550, 300, 980, 600]},
            },
            "proposed_zones": {
                "living_zone": {
                    "where": "前半段客廳",
                    "bbox_on_best_photo": [100, 300, 980, 980],
                    "sofa_side": "free",
                    "tv_side": "",
                }
            },
            "spatial_synthesis": {
                "room_shape": "狹長型",
                "entrance_position": "文字故意寫右側",
            },
        }

    def test_door_side_uses_bbox_before_free_text(self):
        """文字會飄，門 bbox 在左就必須判左。"""
        flat = api.flatten_zoning_v2_to_v1(self._free_zoning_v2(), "A")
        self.assertEqual(flat["_entrance_side"], "left")

    def test_free_mode_is_not_converted_to_right_wall_sofa(self):
        """free／隨機不得再被 guide 偷改成「靠無門側右牆」。"""
        flat = api.flatten_zoning_v2_to_v1(self._free_zoning_v2(), "A")
        self.assertEqual(flat["_sofa_layout"], "free")
        self.assertEqual(api._guide_sofa_side(flat), "free")

    def test_free_guide_left_door_uses_compact_front_sofa_and_right_tv(self):
        """左側大門｜小沙發放門後前側、TV 在右牆，中央後方走道保持可通。"""
        plan = api._layout_guide_plan(
            1000, 700,
            sofa_side="free",
            entrance_side="left",
            entrance_bbox=(10, 220, 300, 690),
            focal_side="right",
            auto_float=False,
        )
        self.assertEqual(plan["mode"], "auto_compact")
        self.assertEqual(plan["chosen_sofa_side"], "left")
        self.assertEqual(plan["sofa_facing"], "right")
        self.assertLessEqual(plan["door_clear"][2], plan["sofa"][0])
        self.assertLess(plan["sofa"][2], plan["tv"][0])
        self.assertAlmostEqual(plan["sofa"][1], int(700 * 0.48), delta=2)
        self.assertEqual(
            (plan["sofa"][1] + plan["sofa"][3]) // 2,
            (plan["tv"][1] + plan["tv"][3]) // 2,
        )
        self.assertFalse(api._rects_intersect(plan["sofa"], plan["door_clear"]))
        self.assertFalse(api._rects_intersect(plan["tv"], plan["door_clear"]))
        walkway = (300, 385, 600, 686)
        safe = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(10, 220, 300, 690), focal_side="right",
            auto_float=False, blocked_rects=[walkway],
        )
        self.assertTrue(safe["valid"])
        # 透視畫面中的牆邊家具視覺框可以與地面 walkway 投影重疊；
        # 不可再因此把整張成對 guide 丟掉。
        self.assertTrue(api._rects_intersect(safe["sofa"], walkway))
        self.assertFalse(api._rects_intersect(safe["tv"], walkway))

    def test_invalid_wide_door_never_returns_reversed_or_overlapping_boxes(self):
        plan = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(0, 100, 700, 700), focal_side="right",
            auto_float=False,
        )
        self.assertFalse(plan["valid"])
        self.assertIsNone(plan["sofa"])
        self.assertIsNone(plan["tv"])

    def test_bound_left_right_still_keep_door_clear(self):
        door_left = (10, 220, 300, 690)
        right_sofa = api._layout_guide_plan(
            1000, 700, sofa_side="right", entrance_side="left",
            entrance_bbox=door_left,
        )
        self.assertEqual(right_sofa["mode"], "bound")
        self.assertEqual(right_sofa["chosen_sofa_side"], "right")
        self.assertFalse(api._rects_intersect(right_sofa["tv"], right_sofa["door_clear"]))

        door_right = (700, 220, 990, 690)
        left_sofa = api._layout_guide_plan(
            1000, 700, sofa_side="left", entrance_side="right",
            entrance_bbox=door_right,
        )
        self.assertEqual(left_sofa["chosen_sofa_side"], "left")
        self.assertFalse(api._rects_intersect(left_sofa["tv"], left_sofa["door_clear"]))

    def test_ai_auto_retry_never_drops_door_aware_guide(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base.jpg"
            alt = Path(td) / "alt.jpg"
            guide = Path(td) / "guide.jpg"
            for p in (base, alt, guide):
                p.write_bytes(b"x")
            entry = {
                "_room_type": "living",
                "_base_path": str(base),
                "_alt_bases": [str(alt)],
                "_used_bases": [str(base)],
                "_layout_guide": str(guide),
                "_layout_guide_mode": "auto_compact",
            }
            self.assertIsNone(api._switch_entry_to_next_living_base(entry))
            self.assertEqual(entry["_base_path"], str(base))
            self.assertEqual(entry["_layout_guide"], str(guide))
            strategies = api._phase3_base_strategies(entry)
            self.assertEqual(strategies, [("門感知同底圖修正", str(base), None)])

    def test_ai_auto_allows_floating_only_when_room_is_large(self):
        self.assertFalse(api._room_can_float_sofa(
            {"space_type": "living", "room_dimensions": {
                "length_m": 8.0, "width_m": 3.0, "confidence": "high"}},
            {"spatial_synthesis": {"room_shape": "狹長型"}},
        ))
        self.assertTrue(api._room_can_float_sofa(
            {"space_type": "living", "room_dimensions": {
                "length_m": 6.0, "width_m": 5.0, "confidence": "high"}},
            {"spatial_synthesis": {"room_shape": "寬敞方正客廳"}},
        ))
        self.assertFalse(api._room_can_float_sofa(
            {"space_type": "living", "room_dimensions": {
                "length_m": 6.0, "width_m": 5.0, "confidence": "low"}},
            {"spatial_synthesis": {"room_shape": "寬敞方正客廳"}},
        ))
        self.assertFalse(api._room_can_float_sofa(
            {"space_type": "whole", "room_dimensions": {
                "length_m": 8.0, "width_m": 6.0, "confidence": "high"}},
            {"spatial_synthesis": {"room_shape": "寬敞方正"}},
        ))
        plan = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(10, 220, 250, 690), focal_side="right", auto_float=True,
        )
        self.assertEqual(plan["mode"], "auto_float")
        self.assertEqual(plan["sofa_facing"], "right")
        self.assertGreater(plan["sofa"][0], plan["door_clear"][2])
        self.assertLess(plan["sofa"][2], plan["tv"][0])

    def test_ai_auto_uses_solid_wall_and_avoids_window_wall(self):
        z = {
            "_entrance_side": "left",
            "_window_side": "left",
            "spatial_synthesis": {
                "wall_inventory": [
                    {"name": "左側長牆", "has_opening": True},
                    {"name": "右側長牆", "has_opening": False},
                ]
            },
        }
        self.assertEqual(api._preferred_focal_side(z), "right")
        no_window = {
            "_entrance_side": "left",
            "spatial_synthesis": {
                "room_shape": "狹長深型格局",
                "main_window_wall": "主客廳沒有直接對外窗",
                "wall_inventory": [
                    {"name": "左側長牆", "has_opening": True},
                    {"name": "右側長牆", "has_opening": False},
                ],
            },
        }
        # 憲法配置（用戶裁決庫）：無左右窗約束時，完整右牆給「沙發」當背牆，
        # TV/焦點留在門牆過門段（21CCB9AF/A08E612D 接受組佈局；
        # 反向=2879173D 被拒的沙發過門佈局）。
        self.assertEqual(api._preferred_focal_side(no_window), "left")
        job_2879173d = {
            "_entrance_side": "left",
            "_window_side": "back",
            "spatial_synthesis": {
                "room_shape": "長方形長條格局",
                "main_window_wall": "深處端點",
                "wall_inventory": [
                    {"name": "左側長牆", "has_opening": True},
                    {"name": "右側長牆", "has_opening": False},
                    {"name": "遠端壁面", "has_opening": True},
                ],
            },
        }
        # 憲法配置（用戶裁決庫裁定）：門左+窗後 → TV/焦點留在門牆過門段、
        # 沙發拿右側完整實牆。此為接受組全體佈局（21CCB9AF/1164DFC6/A08E612D,
        # TV-門間距 0.29-0.42 由 0.28 閘門把關）；「沙發放門牆過門」正是
        # 2879173D 被用戶拒絕的配置,不得再當成期望值。
        focal_side = api._preferred_focal_side(job_2879173d)
        self.assertEqual(focal_side, "left")
        prompt_z = {
            **job_2879173d,
            "_origin": "user_confirmed_v2",
            "_layout_choice": "A",
            "_sofa_layout": "free",
            "_auto_focal_side": focal_side,
            "zones": {"living_zone": {"where": "客廳前中段"},
                      "entrance_zone": {"where": "左側大門"}},
            "furniture_placement_rules": {"sofa_side": "", "tv_side": ""},
        }
        layout_prompt = pb._build_layout_section(prompt_z)
        self.assertIn("focal/TV wall is LEFT", layout_prompt)
        self.assertIn("sofa belongs on the RIGHT", layout_prompt)
        # 門牆放 TV：櫃體必須整組過門框一個門寬,門邊留空牆
        self.assertIn("past the outer door frame", layout_prompt)
        self.assertIn("never the entrance", layout_prompt)
        guide_2879 = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(120, 227, 320, 665), focal_side=focal_side,
            auto_float=False,
        )
        self.assertEqual(guide_2879["chosen_sofa_side"], "right")
        self.assertEqual(guide_2879["sofa_facing"], "left")
        # 沙發在右實牆,不得碰門淨空;TV 在門牆必須過門淨空
        self.assertFalse(api._rects_intersect(guide_2879["sofa"], guide_2879["door_clear"]))
        self.assertFalse(api._rects_intersect(guide_2879["tv"], guide_2879["door_clear"]))
        # 2879173D 真實 bbox：粗糙的地面 walkway 矩形不可否決牆邊家具視覺框。
        actual_guide = api._layout_guide_plan(
            1000, 1000, sofa_side="free", entrance_side="left",
            entrance_bbox=(120, 325, 320, 850), focal_side="right",
            auto_float=False,
            blocked_rects=[(300, 550, 600, 990)],
            living_bbox=(150, 450, 950, 990),
        )
        self.assertTrue(actual_guide["valid"])
        self.assertEqual(actual_guide["chosen_sofa_side"], "left")
        sofa_cy = (actual_guide["sofa"][1] + actual_guide["sofa"][3]) // 2
        tv_cy = (actual_guide["tv"][1] + actual_guide["tv"][3]) // 2
        self.assertEqual(sofa_cy, tv_cy)
        validator_src = (Path(__file__).parent / "gemini_analyze.py").read_text(encoding="utf-8")
        self.assertIn("仍有約 80–90 cm 寬的連續可走路徑", validator_src)
        self.assertNotIn("只要行走需繞過就填 true", validator_src)
        # 若唯一實牆跟入口同側，TV 仍必須被推過門淨空，不能藍紅重疊。
        conflict = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(10, 220, 300, 690), focal_side="left", auto_float=False,
        )
        self.assertFalse(api._rects_intersect(conflict["tv"], conflict["door_clear"]))

    def test_sofa_facing_door_uses_previous_render_for_local_alignment_edit(self):
        with tempfile.TemporaryDirectory() as td:
            previous = Path(td) / "render_luxury_00.jpg"
            previous.write_bytes(b"render")
            validation = {
                "sofa_facing_entrance_door": True,
                "focal_anchor_past_door_in_depth": True,
                "camera_axis_preserved": True,
                "passage_openings_preserved": True,
                "render_bboxes": {
                    "sofa": [480, 620, 840, 940],
                    "focal_anchor": [530, 260, 690, 400],
                    "entrance_door": [220, 120, 860, 260],
                },
            }
            self.assertEqual(
                api._sofa_alignment_edit_base(validation, {"render_path": str(previous)}, "living"),
                str(previous),
            )
            validation["camera_axis_preserved"] = False
            self.assertIsNone(
                api._sofa_alignment_edit_base(validation, {"render_path": str(previous)}, "living")
            )
            validation["camera_axis_preserved"] = True
            validation["sofa_facing_entrance_door"] = False
            validation["focal_anchor_misaligned_with_sofa"] = True
            self.assertEqual(
                api._sofa_alignment_edit_base(validation, {"render_path": str(previous)}, "living"),
                str(previous),
            )
            validation["focal_anchor_misaligned_with_sofa"] = False
            validation["furniture_blocks_door"] = True
            self.assertEqual(
                api._sofa_alignment_edit_base(validation, {"render_path": str(previous)}, "living"),
                str(previous),
            )

        edit_prompt = pb._build_retry_context_section({
            "sofa_alignment_edit": True,
            "failed_flags": ["sofa_facing_entrance_door"],
            "reason": "沙發正對大門",
        })
        self.assertIn("MOVE ONLY THE SOFA", edit_prompt)
        self.assertIn("SOFA-TV FACE-TO-FACE HARD CONTRACT", edit_prompt)
        self.assertIn("the exact centre directly in front of the sofa MUST be the TV-screen centre", edit_prompt)
        self.assertIn("NEVER move the TV closer to a stationary sofa", edit_prompt)
        self.assertIn("LOCK the TV", edit_prompt)
        self.assertNotIn("coffee-table centre", edit_prompt)
        self.assertNotIn("whole living group", edit_prompt.lower())
        api_source = Path(api.__file__).read_text(encoding="utf-8")
        # definition + Z3 + Phase2 + Phase3
        self.assertGreaterEqual(api_source.count("_sofa_alignment_edit_base("), 4)
        self.assertGreaterEqual(api_source.count("_activate_pair_alignment_edit("), 4)
        self.assertIn("if (pair_alignment_base or alignment_base) else base_p", api_source)

    def test_pair_centre_delta_is_diagnostic_only(self):
        """C63D5284 違憲拆除：y 中心差無分類力（校準庫接受組 32-89 與拒絕組
        61-106 重疊，tolerance=25 實測殺掉接受組 4/5）——量測值保留為診斷欄位，
        不得再影響 ok/hard_fail。"""
        validation = {
            "ok": True,
            "hard_fail": False,
            "sofa_facing_entrance_door": False,
            "camera_axis_preserved": True,
            "passage_openings_preserved": True,
            "render_bboxes": {
                "sofa": [473, 228, 733, 417],
                "focal_anchor": [570, 654, 809, 888],
            },
        }
        checked = api._fail_closed_validation(validation, "living")
        self.assertTrue(checked["ok"])                      # 不再翻案
        self.assertFalse(checked.get("hard_fail", False))
        self.assertEqual(checked["pair_center_delta_y"], -87)  # 診斷值仍在

        with tempfile.TemporaryDirectory() as td:
            source = str(Path(td) / "render.jpg")
            cv2.imwrite(source, np.full((1000, 1500, 3), 205, dtype=np.uint8))
            guide = api._build_pair_alignment_guide_image(source, td, 0, validation)
            self.assertTrue(guide and Path(guide).exists())
            image = cv2.imread(guide).astype(np.int16)
            green = ((image[:, :, 1] > image[:, :, 0] + 35)
                     & (image[:, :, 1] > image[:, :, 2] + 35))
            blue = ((image[:, :, 0] > image[:, :, 1] + 45)
                    & (image[:, :, 0] > image[:, :, 2] + 45))
            self.assertGreater(int(green.sum()), 250)
            self.assertGreater(int(blue.sum()), 250)

        edit_prompt = pb._build_retry_context_section({"tv_alignment_edit": True})
        self.assertIn("MOVE ONLY THE TV AND MEDIA CONSOLE", edit_prompt)
        self.assertIn("GREEN sofa target stays fixed", edit_prompt)
        self.assertIn("BLUE TV / media-console target", edit_prompt)

    def test_s2_pair_alignment_preserves_formal_guide_and_uses_dynamic_ref(self):
        validation = {
            "camera_axis_preserved": True,
            "passage_openings_preserved": True,
            "render_bboxes": {
                "sofa": [473, 228, 733, 417],
                "focal_anchor": [570, 654, 809, 888],
            },
        }
        with tempfile.TemporaryDirectory() as td:
            source = str(Path(td) / "render.jpg")
            formal_guide = str(Path(td) / "formal-s2-guide.jpg")
            cv2.imwrite(source, np.full((1000, 1500, 3), 205, dtype=np.uint8))
            cv2.imwrite(formal_guide, np.full((1000, 1500, 3), 180, dtype=np.uint8))
            entry = {
                "_room_type": "living",
                "_layout_contract_s2_required": True,
                "_layout_guide": formal_guide,
                "_layout_guide_mode": "auto_s2_contract",
                "_layout_guide_s2_sha256": "formal-hash",
            }
            base = api._activate_pair_alignment_edit(
                validation, {"render_path": source}, entry, td, 0)
            self.assertEqual(base, source)
            self.assertEqual(entry["_layout_guide"], formal_guide)
            self.assertEqual(entry["_layout_guide_mode"], "auto_s2_contract")
            self.assertTrue(Path(entry["_consistency_ref_path"]).exists())
            self.assertTrue(entry["_s2_retry_artifacts_active"])
            api._clear_s2_retry_edit_artifacts(entry)
            self.assertEqual(entry["_layout_guide"], formal_guide)
            self.assertEqual(entry["_layout_guide_s2_sha256"], "formal-hash")
            self.assertNotIn("_consistency_ref_path", entry)

    def test_wide_crop_keeps_left_or_right_edge_door(self):
        left = api._full_frame_3_2_crop_box(1600, 900, preserve_bbox=(0, 100, 220, 850))
        right = api._full_frame_3_2_crop_box(1600, 900, preserve_bbox=(1380, 100, 1600, 850))
        self.assertEqual(left, (0, 0, 1350, 900))
        self.assertEqual(right, (250, 0, 1600, 900))

    def test_zoning_bbox_requires_stable_source_hash_not_array_index(self):
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p0 = Path(tmp) / "photo0.jpg"
            p1 = Path(tmp) / "photo1.jpg"
            p0.write_bytes(b"photo-zero-content")
            p1.write_bytes(b"photo-one-content")
            zoning = {
                "best_photo_index": 0,
                "_source_binding": {
                    "photo_key": "upload/photo0.jpg",
                    "sha256": hashlib.sha256(p0.read_bytes()).hexdigest(),
                },
            }
            paths = [str(p1), str(p0)]
            self.assertTrue(api._zoning_bbox_matches_source(str(p0), paths, zoning))
            self.assertFalse(api._zoning_bbox_matches_source(str(p1), paths, zoning))
            self.assertFalse(api._zoning_bbox_matches_source(
                str(p0), paths, {"best_photo_index": 1},
            ))

    def test_ui_keeps_three_choices_without_public_ai_wording(self):
        html = (Path(__file__).parent.parent / "zoning-confirm.html").read_text(encoding="utf-8")
        for value in ("left", "right", "free"):
            self.assertIn(f'value="{value}"', html)
        self.assertIn("採用建議配置", html)
        self.assertIn("依門窗與動線安排", html)
        self.assertNotIn("交給 AI 自動配置", html)
        self.assertIn("currentSofaSide = 'free'; // 非長型房未讓客戶指定", html)

    def test_validator_hard_fails_sofa_facing_door_or_window(self):
        import gemini_analyze as ga
        self.assertIn("sofa_facing_entrance_door", ga.HARD_FAIL_FLAGS)
        self.assertIn("sofa_facing_window", ga.HARD_FAIL_FLAGS)
        source = Path(ga.__file__).read_text(encoding="utf-8")
        self.assertIn("Q2g: sofa_facing_window", source)
        self.assertIn("auto_layout", source)
        prompt_source = Path(pb.__file__).read_text(encoding="utf-8")
        self.assertIn("MAIN WINDOW: the sofa must NEVER directly face", prompt_source)
        missing = ga._enforce_window_facing_field({"ok": True}, is_living=True)
        self.assertFalse(missing["ok"])
        self.assertTrue(missing["sofa_facing_window_unverified"])
        facing = ga._enforce_window_facing_field(
            {"ok": True, "sofa_facing_window": True}, is_living=True)
        self.assertFalse(facing["ok"])
        self.assertTrue(facing["sofa_facing_window"])
        self.assertTrue(ga._living_bbox_is_broad_depth_zone([380, 250, 980, 980]))
        self.assertFalse(ga._living_bbox_is_broad_depth_zone([700, 250, 980, 980]))


    def test_free_mode_crop_keeps_entrance_and_exact_three_to_two(self):
        """free 模式不再把門裁掉｜底圖保留門證據並維持精確 3:2。"""
        box = api._full_frame_3_2_crop_box(4032, 3024)
        x0, y0, x1, y1 = box
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), 1.5, delta=0.001)
        door = (40, 979, 1209, 2872)
        self.assertTrue(api._rects_intersect(box, door))

    def test_free_crop_file_and_bbox_use_same_transform(self):
        """實際裁圖與門 bbox 必須走同一 crop_box，guide 才不會畫錯門。"""
        with tempfile.TemporaryDirectory() as td:
            source = str(Path(td) / "source.jpg")
            cv2.imwrite(source, np.full((600, 800, 3), 180, dtype=np.uint8))
            out, did, note, crop_box = api._crop_full_frame_3_2_base(source, td, 0)
            self.assertTrue(did)
            self.assertEqual(crop_box, (0, 67, 800, 600))
            self.assertEqual(cv2.imread(out).shape[:2], (533, 800))
            # normalized [ymin,xmin,ymax,xmax] → crop pixel bbox
            mapped = api._bbox1000_to_crop_px([200, 50, 950, 300], 800, 600, crop_box)
            self.assertEqual(mapped, (40, 53, 240, 503))

    def test_ratio_crop_does_not_disable_living_zone_validation(self):
        ratio_ctx = api._product_fidelity_into_layout_ctx({}, {"_cropped": True})
        zone_ctx = api._product_fidelity_into_layout_ctx({}, {
            "_cropped": True, "_zone_cropped": True})
        self.assertNotIn("base_is_room_crop", ratio_ctx or {})
        self.assertTrue((zone_ctx or {}).get("base_is_room_crop"))

    def test_free_layout_guide_prompt_does_not_force_back_against_wall(self):
        entry = {
            "style": "muji",
            "style_label": "無印極簡",
            "matched_furniture": [],
            "flux_prompt": "warm oak",
            "_layout_guide_mode": "free",
        }
        inputs = pb.build_nano_banana_inputs(
            entry, None, "https://x/room.jpg",
            layout_guide_url="https://x/guide.jpg",
            layout_guide_mode="free",
        )
        prompt = inputs["prompt"]
        self.assertIn("LAYOUT CONSTRAINT MAP", prompt)
        self.assertIn("no left/right preference", prompt)
        self.assertIn("floor perspective", prompt)
        self.assertIn("not a random draw", prompt)
        self.assertNotIn("inside the GREEN zone", prompt)
        self.assertNotIn("with its back against that wall", prompt)

    def test_free_guide_image_contains_door_clear_zone(self):
        with tempfile.TemporaryDirectory() as td:
            fake = str(Path(td) / "room.jpg")
            cv2.imwrite(fake, np.full((700, 1000, 3), 210, dtype=np.uint8))
            out = api._build_layout_guide_image(
                fake, td, 0, "free",
                entrance_side="left",
                entrance_bbox=(10, 220, 300, 690),
            )
            self.assertTrue(out and Path(out).exists())
            image = cv2.imread(out)
            # 必須同時有紅色禁區、綠色沙發框與藍色 TV 框。
            px = image.astype(np.int16)
            reddish = (px[:, :, 2] > px[:, :, 1] + 45) & (px[:, :, 2] > px[:, :, 0] + 45)
            # >100 連 ENTRANCE 箭頭細線都能混過；門邊禁區實心紅框的邊線
            # 至少貢獻數千像素——沒畫紅框就該當掉（10AAED25 貼門教訓）。
            self.assertGreater(int(reddish.sum()), 5000)
            greenish = (px[:, :, 1] > px[:, :, 0] + 35) & (px[:, :, 1] > px[:, :, 2] + 35)
            blueish = (px[:, :, 0] > px[:, :, 1] + 45) & (px[:, :, 0] > px[:, :, 2] + 45)
            self.assertGreater(int(greenish.sum()), 250)
            self.assertGreater(int(blueish.sum()), 250)
            plan = api._layout_guide_plan(
                1000, 700, sofa_side="free", entrance_side="left",
                entrance_bbox=(10, 220, 300, 690), focal_side="right",
                auto_float=False,
            )
            sofa_y = (plan["sofa"][1] + plan["sofa"][3]) // 2
            tv_y = (plan["tv"][1] + plan["tv"][3]) // 2
            self.assertEqual(sofa_y, tv_y)

    def test_synthetic_e72_geometry_crop_guide_prompt_share_one_contract(self):
        """可提交的合成回歸｜bbox 判門→保門裁切→guide→prompt，不依賴私有照片。"""
        flat = api.flatten_zoning_v2_to_v1(self._free_zoning_v2(), "A")
        flat["_auto_focal_side"] = "right"
        flat["_auto_can_float"] = False
        self.assertEqual(flat["_entrance_side"], "left")
        self.assertEqual(flat["_sofa_layout"], "free")
        with tempfile.TemporaryDirectory() as td:
            source = str(Path(td) / "synthetic_room.jpg")
            image = np.full((3024, 4032, 3), 210, dtype=np.uint8)
            cv2.rectangle(image, (40, 979), (1209, 2872), (30, 30, 30), -1)
            cv2.imwrite(source, image)
            door1000 = flat["zones"]["entrance_zone"]["bbox_on_best_photo"]
            cropped, did, _, crop_box = api._crop_full_frame_3_2_base(
                source, td, 0, entrance_bbox1000=door1000)
            self.assertTrue(did)
            door_crop = api._bbox1000_to_crop_px(door1000, 4032, 3024, crop_box)
            self.assertIsNotNone(door_crop)
            guide = api._build_layout_guide_image(
                cropped, td, 0, "free", entrance_side="left",
                entrance_bbox=door_crop, focal_side="right", auto_float=False,
            )
            self.assertTrue(guide and Path(guide).exists())
            inputs = pb.build_nano_banana_inputs(
                {"style": "muji", "style_label": "無印極簡",
                 "matched_furniture": [], "flux_prompt": "warm oak",
                 "_layout_guide_mode": "auto_compact"},
                flat, "https://x/room.jpg", layout_guide_url="https://x/guide.jpg",
                layout_guide_mode="auto_compact",
            )
            prompt = inputs["prompt"]
            self.assertIn("AI-AUTO LAYOUT", prompt)
            self.assertIn("not a random side", prompt)
            self.assertIn("RED ENTRANCE, WALKWAY or NO FURNITURE", prompt)
            self.assertNotIn("DOOR-ON-A-LONG-WALL LAYOUT", prompt)


class WideEntranceZonePlannerRegression(unittest.TestCase):
    """48B75FBF/10AAED25 根因回歸｜玄關落塵區 bbox 極寬（實體門 1.8 倍）時，
    規劃器不得無解——無解 → 退化引導圖（只剩門箭頭）→ 模型往門邊畫，六連燒。
    數字全部來自該單真實 crop（W=4032 H=2688、door_crop、walkway、living bbox）。"""

    REAL = dict(
        W=4032, H=2688,
        door_crop=(40, 631, 1128, 2264),
        blocked=[(1008, 873, 2620, 2657)],
        living=(201, 1024, 3830, 2657),
    )

    def _plan(self):
        r = self.REAL
        return api._layout_guide_plan(
            r["W"], r["H"], "free", "left", r["door_crop"],
            focal_side="left", auto_float=False,
            blocked_rects=r["blocked"], living_bbox=r["living"],
        )

    def test_wide_entrance_zone_without_wall_plane_fails_closed(self):
        plan = self._plan()
        # 10AAED25 實圖證明：把固定 TV 矩形推到 door_clear 右邊，仍可能浮在
        # 中央走道而非入口側的真實牆面。沒有牆面 polygon / usable segment 時，
        # 不得把「螢幕矩形沒相交」冒充物理可配置。
        self.assertFalse(plan["valid"])
        self.assertIsNone(plan["tv"])

    def test_auto_living_without_valid_guide_never_calls_paid_renderer(self):
        import test_full_pipeline as tfp

        with tempfile.TemporaryDirectory() as td:
            base = str(Path(td) / "room.jpg")
            cv2.imwrite(base, np.full((700, 1000, 3), 210, dtype=np.uint8))
            valid_guide = str(Path(td) / "guide.jpg")
            cv2.imwrite(valid_guide, np.full((700, 1000, 3), 180, dtype=np.uint8))
            zoning = {"_sofa_layout": "free"}
            cases = (
                (None, "1", "guide is None"),
                (str(Path(td) / "missing-guide.jpg"), "1", "guide path is missing"),
                (valid_guide, "0", "guide feature is disabled"),
            )
            for guide_path, guide_flag, label in cases:
                with self.subTest(label):
                    entry = {
                        "style": "modern", "style_label": "現代",
                        "_room_type": "living",
                        "_layout_guide_mode": "auto_constraints",
                        "_layout_guide": guide_path,
                        "matched_furniture": [],
                    }
                    with patch.dict("os.environ", {
                            "USE_NANO_BANANA": "1", "LAYOUT_GUIDE": guide_flag}), \
                            patch.object(tfp, "_fal_subscribe_timed",
                                         side_effect=AssertionError(
                                             "paid renderer must not be called")):
                        result = tfp.generate_renders(
                            base, [entry], output_dir=td,
                            zoning=zoning, room_type="living")
                    self.assertEqual(result[0]["error_type"], "LayoutPreflightBlocked")
                    self.assertTrue(result[0]["validation"]["hard_fail"])

    def test_invalid_plan_never_emits_degenerate_guide(self):
        # 建構必然無解的場景：door_clear 幾乎蓋滿畫面
        with tempfile.TemporaryDirectory() as td:
            fake = str(Path(td) / "room.jpg")
            cv2.imwrite(fake, np.full((700, 1000, 3), 210, dtype=np.uint8))
            out = api._build_layout_guide_image(
                fake, td, 0, "free",
                entrance_side="left",
                entrance_bbox=(0, 10, 940, 690),   # 門區蓋掉 94% 寬
                focal_side="left", auto_float=False,
            )
            self.assertIsNone(out)   # 寧可沒有 guide，也不給「只剩門箭頭」的反引導

    def test_s2_door_clearance_shift_uses_validator_gap_without_weakening_threshold(self):
        validation = {
            "render_bboxes": {
                "sofa": [540, 260, 738, 408],
                "entrance_door": [326, 122, 857, 256],
            }
        }
        # gap=4, required=0.25*134=33.5, plus 10/1000 repair safety.
        self.assertEqual(api._s2_door_clearance_shift_px(validation, 1536, "left"), 61)
        target = api._s2_repair_target_box(
            validation, 1536, 1024, "left", [(500.0, 600.0)],
            compact_entry_mode=True)
        self.assertEqual(target[0], round(260 * 1536 / 1000) + 4 * 61)
        self.assertLess(target[1], round(540 * 1024 / 1000))
        self.assertLess(target[2] - target[0], round((408 - 260) * 1536 / 1000))
        self.assertLess(target[3] - target[1], round((738 - 540) * 1024 / 1000))

        normal_target = api._s2_repair_target_box(
            validation, 1536, 1024, "left", [(500.0, 600.0)],
            compact_entry_mode=False)
        self.assertEqual(normal_target[0], round(260 * 1536 / 1000) + 61)

    def test_retry_artifacts_are_cleared_before_each_new_base_decision(self):
        entry = {
            "_s2_retry_artifacts_active": True,
            "_edit_mask_path": "old-mask.png",
            "_consistency_ref_path": "old-guide.jpg",
            "_base_path": "original.jpg",
        }
        api._clear_s2_retry_edit_artifacts(entry)
        self.assertNotIn("_edit_mask_path", entry)
        self.assertNotIn("_consistency_ref_path", entry)
        self.assertNotIn("_s2_retry_artifacts_active", entry)
        self.assertEqual(entry["_base_path"], "original.jpg")

        formal_entry = {"_consistency_ref_path": "formal-product-ref.jpg"}
        api._clear_s2_retry_edit_artifacts(formal_entry)
        self.assertEqual(formal_entry["_consistency_ref_path"], "formal-product-ref.jpg")

        source = Path(api.__file__).read_text(encoding="utf-8")
        phase3 = source.split("# ── Phase 3", 1)[1]
        self.assertLess(
            phase3.index("_clear_s2_retry_edit_artifacts(entry)"),
            phase3.index("_activate_pair_alignment_edit("),
        )

    def test_s2_sofa_edit_mask_unlocks_sofa_corridor_but_locks_door(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            previous = root / "previous.png"
            contract_path = root / "contract.json"
            mask_path = root / "mask.png"
            Image.new("RGB", (100, 100), "white").save(previous)
            contract_path.write_text(json.dumps({
                "source": {"size": {"width": 1000, "height": 1000}},
                "decision": {"chosen_candidate_id": "b-compact"},
                "candidates": [{
                    "candidate_id": "b-compact",
                    "sofa_footprint_geometry_id": "sofa-target",
                    "notes": ["sofa_side=left"],
                }],
                "geometry": [{
                    "geometry_id": "sofa-target",
                    "shape": {"coordinates": [
                        [600, 500], [700, 500], [700, 700], [600, 700],
                    ]},
                }],
            }), encoding="utf-8")
            validation = {
                "render_bboxes": {
                    "sofa": [500, 250, 750, 450],
                    "entrance_door": [300, 100, 800, 240],
                }
            }

            result = api._build_s2_sofa_edit_mask(
                str(previous), str(contract_path), validation, str(mask_path))

            self.assertEqual(result, str(mask_path))
            alpha = Image.open(mask_path).getchannel("A")
            self.assertEqual(alpha.getpixel((35, 62)), 0)
            self.assertEqual(alpha.getpixel((17, 55)), 255)
            self.assertEqual(alpha.getpixel((95, 5)), 255)


if __name__ == "__main__":
    unittest.main(verbosity=2)

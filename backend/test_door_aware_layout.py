# -*- coding: utf-8 -*-
"""E72F4ADB｜門感知格局回歸測試。

不呼叫 Gemini / FAL。先鎖住資料流與引導圖契約，再改正式程式。
"""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

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
        self.assertGreaterEqual(plan["sofa"][1], int(700 * 0.70))
        self.assertFalse(api._rects_intersect(plan["sofa"], plan["door_clear"]))
        self.assertFalse(api._rects_intersect(plan["tv"], plan["door_clear"]))
        walkway = (300, 385, 600, 686)
        safe = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(10, 220, 300, 690), focal_side="right",
            auto_float=False, blocked_rects=[walkway],
        )
        self.assertTrue(safe["valid"])
        self.assertFalse(api._rects_intersect(safe["sofa"], walkway))
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
        # 無主窗時，完整右牆優先給沙發；左側只在過門後放 TV。
        self.assertEqual(api._preferred_focal_side(no_window), "left")
        prompt_z = {
            **no_window,
            "_origin": "user_confirmed_v2",
            "_layout_choice": "A",
            "_sofa_layout": "free",
            "_auto_focal_side": "left",
            "zones": {"living_zone": {"where": "客廳前中段"},
                      "entrance_zone": {"where": "左側大門"}},
            "furniture_placement_rules": {"sofa_side": "", "tv_side": ""},
        }
        self.assertIn("ONE FULL visible door-width", pb._build_layout_section(prompt_z))
        # 若唯一實牆跟入口同側，TV 仍必須被推過門淨空，不能藍紅重疊。
        conflict = api._layout_guide_plan(
            1000, 700, sofa_side="free", entrance_side="left",
            entrance_bbox=(10, 220, 300, 690), focal_side="left", auto_float=False,
        )
        self.assertFalse(api._rects_intersect(conflict["tv"], conflict["door_clear"]))

    def test_wide_crop_keeps_left_or_right_edge_door(self):
        left = api._full_frame_3_2_crop_box(1600, 900, preserve_bbox=(0, 100, 220, 850))
        right = api._full_frame_3_2_crop_box(1600, 900, preserve_bbox=(1380, 100, 1600, 850))
        self.assertEqual(left, (0, 0, 1350, 900))
        self.assertEqual(right, (250, 0, 1600, 900))

    def test_zoning_bbox_only_applies_to_its_source_photo(self):
        paths = ["C:/tmp/photo0.jpg", "C:/tmp/photo1.jpg"]
        zoning = {"best_photo_index": 0}
        self.assertTrue(api._zoning_bbox_matches_source(paths[0], paths, zoning))
        self.assertFalse(api._zoning_bbox_matches_source(paths[1], paths, zoning))

    def test_ui_keeps_three_choices_and_explains_ai_auto(self):
        html = (Path(__file__).parent.parent / "zoning-confirm.html").read_text(encoding="utf-8")
        for value in ("left", "right", "free"):
            self.assertIn(f'value="{value}"', html)
        self.assertIn("交給 AI 自動配置", html)
        self.assertIn("空間夠大時可不靠牆", html)
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
            # 必須有紅色入口／走道標記，但 AI auto 不得畫綠色沙發或藍色 TV 框。
            px = image.astype(np.int16)
            reddish = (px[:, :, 2] > px[:, :, 1] + 45) & (px[:, :, 2] > px[:, :, 0] + 45)
            self.assertGreater(int(reddish.sum()), 100)
            greenish = (px[:, :, 1] > px[:, :, 0] + 35) & (px[:, :, 1] > px[:, :, 2] + 35)
            blueish = (px[:, :, 0] > px[:, :, 1] + 45) & (px[:, :, 0] > px[:, :, 2] + 45)
            self.assertEqual(int(greenish.sum()), 0)
            self.assertEqual(int(blueish.sum()), 0)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

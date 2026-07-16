# -*- coding: utf-8 -*-
"""layout contract shadow 回歸｜不呼叫生圖、不改交付。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import api


FIXTURES = Path(__file__).parent / "_phase0_layout_contract" / "fixtures"


class LayoutContractShadowTests(unittest.TestCase):
    def test_shadow_writes_summary_and_never_affects_delivery(self):
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            job_dir = Path(td)
            summary = api._run_layout_contract_shadow(
                job_id="SHADOWTEST01",
                job_dir=job_dir,
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=payload["zoning_v2"] | {
                    "struct_keypoints": payload.get("struct_keypoints"),
                },
                analysis={"space_type": "living", "room_dimensions": {"confidence": "low"}},
                sofa_mode="free",
                can_float=False,
                # best_photo_index=0 → 必須與 image_paths[0] 為同一檔，才 map bbox
                image_paths=[str(photo)],
            )
            self.assertIsNotNone(summary)
            self.assertEqual(summary.get("status"), "ok")
            self.assertFalse(summary.get("affects_delivery"))
            self.assertIn(summary.get("disposition"), (
                "READY_FOR_SHADOW_MODE",
                "UNSAFE_LAYOUT_CONTRACT",
                "NEEDS_ALTERNATE_LIVING_ANGLE",
            ))
            self.assertTrue(summary.get("contract_json"))
            self.assertTrue(Path(summary["contract_json"]).exists())

            v1 = summary.get("contract_v1") or {}
            self.assertEqual(v1.get("status"), "ok")
            self.assertEqual(v1.get("schema_version"), "1.0.0-draft")
            self.assertEqual(v1.get("disposition"), "BLOCKED")
            self.assertFalse(v1.get("pre_generation_eligible"))
            self.assertFalse(v1.get("affects_delivery"))
            self.assertTrue(v1.get("legacy_bbox_binding_verified"))
            self.assertTrue(v1.get("contract_id"))
            self.assertTrue(v1.get("contract_hash"))
            self.assertTrue(v1.get("contract_json"))
            self.assertTrue(Path(v1["contract_json"]).exists())

            full_v1 = json.loads(Path(v1["contract_json"]).read_text(encoding="utf-8"))
            self.assertEqual(full_v1["version_chain"]["contract_id"], v1["contract_id"])
            self.assertEqual(full_v1["version_chain"]["contract_hash"], v1["contract_hash"])
            self.assertFalse(full_v1["extensions"]["affects_delivery"])
            self.assertTrue(full_v1["legacy_inputs"]["legacy_bbox_binding_verified"])

    def test_shadow_respects_left_right_mode(self):
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        z2 = dict(payload["zoning_v2"])
        z2["struct_keypoints"] = payload.get("struct_keypoints")
        with tempfile.TemporaryDirectory() as td:
            left = api._run_layout_contract_shadow(
                job_id="SHADOWLEFT",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=z2,
                analysis={},
                sofa_mode="left",
                can_float=False,
            )
            right = api._run_layout_contract_shadow(
                job_id="SHADOWRIGHT",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=1,
                zoning_result=None,
                user_zoning_v2=z2,
                analysis={},
                sofa_mode="right",
                can_float=False,
            )
            self.assertTrue(all(c["sofa_side"] == "left" for c in left["candidates"]))
            self.assertTrue(all(c["sofa_side"] == "right" for c in right["candidates"]))

    def test_v1_shadow_flag_disables_only_v1_dual_write(self):
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
                os.environ, {"LAYOUT_CONTRACT_V1_SHADOW": "0"}):
            summary = api._run_layout_contract_shadow(
                job_id="V1DISABLED",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=payload["zoning_v2"],
                analysis={},
                sofa_mode="free",
                can_float=False,
                image_paths=[str(photo)],
            )
        self.assertEqual(summary.get("status"), "ok")
        self.assertFalse(summary.get("affects_delivery"))
        self.assertTrue(summary.get("contract_json"))
        self.assertEqual((summary.get("contract_v1") or {}).get("status"), "disabled")
        self.assertFalse((summary.get("contract_v1") or {}).get("affects_delivery"))

    def test_v1_writer_failure_does_not_break_legacy_shadow_or_delivery(self):
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td, mock.patch(
                "layout_contract_v1.build_layout_contract",
                side_effect=RuntimeError("forced v1 shadow failure")):
            summary = api._run_layout_contract_shadow(
                job_id="V1ERROR",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=payload["zoning_v2"],
                analysis={},
                sofa_mode="free",
                can_float=False,
                image_paths=[str(photo)],
            )
        self.assertEqual(summary.get("status"), "ok")
        self.assertFalse(summary.get("affects_delivery"))
        self.assertTrue(summary.get("contract_json"))
        self.assertEqual((summary.get("contract_v1") or {}).get("status"), "error")
        self.assertFalse((summary.get("contract_v1") or {}).get("affects_delivery"))

    def test_v1_bbox_not_mapped_when_photo_is_not_zoning_best_photo(self):
        """有 best_photo_index 仍不夠；shadow 底圖必須等於 image_paths[best]。"""
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        z2 = dict(payload["zoning_v2"])
        z2["best_photo_index"] = 0
        with tempfile.TemporaryDirectory() as td:
            # image_paths[0] 是另一個假路徑 → 與實際 shadow 照片不同 → binding false
            decoy = Path(td) / "other.jpg"
            Image.new("RGB", (64, 64), color=(10, 20, 30)).save(decoy)
            summary = api._run_layout_contract_shadow(
                job_id="V1UNBOUND",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=z2,
                analysis={},
                sofa_mode="free",
                can_float=False,
                image_paths=[str(decoy), str(photo)],
            )
            self.assertEqual(summary.get("status"), "ok")
            self.assertFalse(summary.get("affects_delivery"))
            v1 = summary.get("contract_v1") or {}
            self.assertEqual(v1.get("status"), "ok")
            self.assertFalse(v1.get("legacy_bbox_binding_verified"))
            full_v1 = json.loads(Path(v1["contract_json"]).read_text(encoding="utf-8"))
            self.assertIn("MISSING_PHOTO_BINDING", full_v1["decision"]["unsafe_codes"])
            self.assertFalse(full_v1["legacy_inputs"]["legacy_bbox_binding_verified"])
            self.assertFalse(any(g.get("status") == "available" for g in full_v1["geometry"]))

    def test_v1_bbox_not_mapped_when_image_paths_has_sparse_slot_before_photo(self):
        """best_photo_index 必須套原始陣列；不得先濾空值再重排 index。"""
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        z2 = dict(payload["zoning_v2"])
        z2["best_photo_index"] = 0
        with tempfile.TemporaryDirectory() as td:
            summary = api._run_layout_contract_shadow(
                job_id="V1SPARSEPATHS",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=z2,
                analysis={},
                sofa_mode="free",
                can_float=False,
                image_paths=[None, str(photo)],
            )
            v1 = summary.get("contract_v1") or {}
            self.assertEqual(v1.get("status"), "ok")
            self.assertFalse(v1.get("legacy_bbox_binding_verified"))
            full_v1 = json.loads(Path(v1["contract_json"]).read_text(encoding="utf-8"))
            self.assertIn("MISSING_PHOTO_BINDING", full_v1["decision"]["unsafe_codes"])
            self.assertFalse(any(g.get("status") == "available" for g in full_v1["geometry"]))

    def test_v1_bbox_not_mapped_without_image_paths(self):
        """缺 image_paths 時不得因「有 user_zoning」就當 binding verified。"""
        photo = FIXTURES / "E72F4ADB.jpg"
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            summary = api._run_layout_contract_shadow(
                job_id="V1NOPATHS",
                job_dir=Path(td),
                photo_path=str(photo),
                view_index=0,
                zoning_result=None,
                user_zoning_v2=payload["zoning_v2"],
                analysis={},
                sofa_mode="free",
                can_float=False,
                # 刻意不傳 image_paths
            )
            v1 = summary.get("contract_v1") or {}
            self.assertEqual(v1.get("status"), "ok")
            self.assertFalse(v1.get("legacy_bbox_binding_verified"))
            full_v1 = json.loads(Path(v1["contract_json"]).read_text(encoding="utf-8"))
            self.assertIn("MISSING_PHOTO_BINDING", full_v1["decision"]["unsafe_codes"])

    def test_shadow_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"LAYOUT_CONTRACT_SHADOW": "0"}):
            out = api._run_layout_contract_shadow(
                job_id="X",
                job_dir=Path("."),
                photo_path="nope.jpg",
                view_index=0,
                zoning_result={},
                user_zoning_v2={},
                analysis={},
                sofa_mode="free",
                can_float=False,
            )
            self.assertIsNone(out)

    def test_zoning_payload_prefers_user_v2(self):
        user = {"existing_zones": {"walkway": {"bbox_on_best_photo": [1, 2, 3, 4]}}}
        flat = {"zones": {"walkway": {"bbox_on_best_photo": [9, 9, 9, 9]}}}
        payload = api._zoning_payload_for_layout_contract(flat, user)
        self.assertEqual(payload["existing_zones"]["walkway"]["bbox_on_best_photo"], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main(verbosity=2)

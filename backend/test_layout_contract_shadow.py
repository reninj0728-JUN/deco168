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

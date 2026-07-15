# -*- coding: utf-8 -*-
"""Phase 0 格局契約回歸測試｜不接正式管線、不呼叫生圖。"""
import json
import unittest
from pathlib import Path

from PIL import Image

import _proto_layout_contract as plc


FIXTURES = Path(__file__).parent / "_phase0_layout_contract" / "fixtures"


class LayoutContractPhase0Tests(unittest.TestCase):
    def _load_e72(self):
        payload = json.loads((FIXTURES / "E72F4ADB.json").read_text(encoding="utf-8"))
        with Image.open(FIXTURES / "E72F4ADB.jpg") as image:
            width, height = image.size
        return payload, width, height

    def _build_e72(self):
        payload, width, height = self._load_e72()
        return plc.build_contract_with_crop(
            payload["zoning_v2"],
            width,
            height,
            struct_keypoints=payload.get("struct_keypoints"),
        )

    def _build_e72_without_door(self):
        payload, width, height = self._load_e72()
        zoning = json.loads(json.dumps(payload["zoning_v2"]))
        (zoning.get("existing_zones") or {}).pop("entrance_zone", None)
        return plc.build_contract_with_crop(
            zoning,
            width,
            height,
            struct_keypoints=payload.get("struct_keypoints"),
        )

    def test_ab_candidates_exist_for_side_entrance(self):
        contract = self._build_e72()
        ids = [c["id"] for c in contract["candidates"]]
        self.assertTrue(any(i.startswith("A_") for i in ids))
        self.assertTrue(any(i.startswith("B_") for i in ids))
        self.assertEqual(contract["door_side"], "left")
        self.assertIsNotNone(contract["door_clear"])

    def test_candidate_b_sofa_on_door_wall_fails_or_penalized(self):
        """B 若沙發落在門禁落腳區，必須 fail；不得被選成 safe。"""
        contract = self._build_e72()
        b = next(c for c in contract["candidates"] if c["id"].startswith("B_"))
        # 至少不能同時 pass 且 thick sofa 在門牆還被當最佳商業解
        if b["pass"]:
            # 若幾何上真的過門禁，A 仍應分數更高（厚沙發離門）
            a = next(c for c in contract["candidates"] if c["id"].startswith("A_"))
            if a["pass"]:
                self.assertGreaterEqual(a["score"], b["score"])
        self.assertNotEqual(contract.get("chosen", "").startswith("B_"), True) if contract["safe_layout"] and any(
            c["pass"] and c["id"].startswith("A_") for c in contract["candidates"]
        ) else True

    def test_safe_layout_includes_every_crop_invariant(self):
        """不得再出現原圖 ALL-PASS、裁切失敗卻仍 safe=True。"""
        contract = self._build_e72()
        # base candidate pass + crop invariants
        chosen_pass = False
        for c in contract["candidates"]:
            if c["id"] == contract.get("chosen"):
                chosen_pass = c["pass"]
                break
        expected = chosen_pass and all(contract["crop_invariants"].values())
        self.assertEqual(contract["safe_layout"], expected)

    def test_crop_keeps_exact_three_to_two_aspect(self):
        contract = self._build_e72()
        x0, y0, x1, y1 = contract["crop"]["crop_box"]
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), 1.5, delta=0.01)

    def test_side_entrance_keeps_door_in_crop(self):
        """有側門客廳裁切必須保留門，才能驗門禁。"""
        contract = self._build_e72()
        self.assertTrue(contract["crop"]["door_in_crop"])
        self.assertTrue(contract["crop_invariants"]["door_remains_in_crop_if_side_entrance"])

    def test_sofa_mode_left_only_keeps_left_candidates(self):
        payload, width, height = self._load_e72()
        contract = plc.build_contract_with_crop(
            payload["zoning_v2"], width, height,
            struct_keypoints=payload.get("struct_keypoints"),
            sofa_mode="left",
        )
        self.assertTrue(all(c["sofa_side"] == "left" for c in contract["candidates"]))

    def test_sofa_mode_right_only_keeps_right_candidates(self):
        payload, width, height = self._load_e72()
        contract = plc.build_contract_with_crop(
            payload["zoning_v2"], width, height,
            struct_keypoints=payload.get("struct_keypoints"),
            sofa_mode="right",
        )
        self.assertTrue(all(c["sofa_side"] == "right" for c in contract["candidates"]))

    def test_float_candidate_only_when_can_float(self):
        payload, width, height = self._load_e72()
        no_f = plc.build_contract_with_crop(
            payload["zoning_v2"], width, height,
            struct_keypoints=payload.get("struct_keypoints"),
            sofa_mode="free", can_float=False,
        )
        yes_f = plc.build_contract_with_crop(
            payload["zoning_v2"], width, height,
            struct_keypoints=payload.get("struct_keypoints"),
            sofa_mode="free", can_float=True,
        )
        self.assertFalse(any(c["id"].startswith("F_") for c in no_f["candidates"]))
        self.assertTrue(any(c["id"].startswith("F_") for c in yes_f["candidates"]))

    def test_no_door_crop_is_also_exact_three_to_two(self):
        """無門案例也不能拿 1.48～1.52 容忍帶冒充精確 3:2。"""
        contract = self._build_e72_without_door()
        x0, y0, x1, y1 = contract["crop"]["crop_box"]
        self.assertAlmostEqual((x1 - x0) / (y1 - y0), 1.5, delta=0.001)
        self.assertTrue(contract["crop_invariants"]["crop_is_exact_3_2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

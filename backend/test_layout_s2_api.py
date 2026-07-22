# -*- coding: utf-8 -*-
"""API-level S2 artifact wiring tests. No network or renderer calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import api
import layout_geometry_verifier_s2 as verifier_s2
import pytest
from PIL import Image
from test_layout_geometry_s2 import _safe_geometry


VERIFIER_PASS = {
    "right_wall_floor_alignment": "pass",
    "left_wall_floor_alignment": "pass",
    "sofa_back_contact": "pass",
    "tv_wall_contact": "pass",
    "axis_clear_of_entrance": "pass",
    "walkway_connected": "pass",
    "source_entrance_retained": "pass",
    "cross_axis_matches_floor_transverse": "pass",
    "overall": "pass",
    "unsafe_codes": [],
    "notes": "測試通過",
}


def test_s2_required_render_enables_image_edit_retry_even_when_env_is_off(monkeypatch):
    monkeypatch.setenv("USE_NANO_BANANA", "0")

    assert api._image_edit_retry_enabled([
        {"_layout_contract_s2_required": True},
    ]) is True
    assert api._image_edit_retry_enabled([
        {"_layout_contract_s2_required": False},
    ]) is False


def test_s2_chosen_candidate_sides_override_legacy_generation_context():
    contract = {
        "decision": {"chosen_candidate_id": "s2_f_free"},
        "candidates": [{
            "candidate_id": "s2_f_free",
            "notes": ["sofa_side=free", "tv_side=right"],
        }],
    }
    zoning = {
        "_sofa_layout": "left",
        "furniture_placement_rules": {"sofa_side": "left", "tv_side": "right"},
    }

    sides = api._sync_s2_candidate_sides(zoning, contract)

    assert sides == {"sofa_side": "free", "tv_side": "right"}
    assert zoning["_sofa_layout"] == "free"
    assert zoning["furniture_placement_rules"]["sofa_side"] == "free"
    assert zoning["furniture_placement_rules"]["tv_side"] == "right"


def test_run_layout_contract_s2_writes_safe_bound_artifacts(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": "upload/room.jpg",
            "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
        "proposed_zones": {"living_zone": {"sofa_side": "right"}},
    }

    summary, artifacts = api._run_layout_contract_s2(
        job_id="job-s2-api",
        job_dir=tmp_path,
        photo_path=str(photo),
        view_index=0,
        user_zoning_v2=zoning,
        legacy_zoning=zoning,
        sofa_mode="free",
        image_paths=[str(photo)],
        geometry_verifier=lambda *_: dict(VERIFIER_PASS),
        floor_reference_estimator=lambda *_: {
            "status": "observed",
            "confidence": "high",
            "direction_xy": [1.0, 0.0],
            "angle_degrees": 0.0,
            "support_count": 20,
            "unsafe_codes": [],
        },
    )

    assert summary["status"] == "safe"
    assert summary["contract_v1_disposition"] == "SAFE_FOR_GENERATION"
    assert artifacts["eligible"] is True
    assert artifacts["contract"]["decision"]["chosen_candidate_id"]
    assert Path(artifacts["contract_path"]).exists()
    assert Path(artifacts["guide_path"]).exists()
    assert Path(artifacts["reconciliation_path"]).exists()
    contract = json.loads(Path(artifacts["contract_path"]).read_text(encoding="utf-8"))
    assert contract["source"]["sha256"] == hashlib.sha256(photo.read_bytes()).hexdigest()
    assert contract["decision"]["pre_generation_eligible"] is True

    generation_zoning = {
        "_sofa_layout": "left",
        "furniture_placement_rules": {"sofa_side": "left", "tv_side": "right"},
    }
    api._sync_s2_candidate_sides(generation_zoning, artifacts["contract"])
    chosen_id = artifacts["contract"]["decision"]["chosen_candidate_id"]
    chosen = next(c for c in artifacts["contract"]["candidates"]
                  if c["candidate_id"] == chosen_id)
    expected_sofa = next(n.split("=", 1)[1] for n in chosen["notes"]
                         if n.startswith("sofa_side="))
    assert generation_zoning["_sofa_layout"] == expected_sofa


def test_run_layout_contract_s2_exposes_verifier_exception_and_history(tmp_path):
    photo = tmp_path / "room.jpg"
    Image.new("RGB", (1000, 700), "white").save(photo)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": "upload/room.jpg",
            "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
    }

    def timeout_verifier(*_args):
        raise TimeoutError("Gemini verifier timed out")

    summary, artifacts = api._run_layout_contract_s2(
        job_id="job-s2-timeout",
        job_dir=tmp_path,
        photo_path=str(photo),
        view_index=0,
        user_zoning_v2=zoning,
        legacy_zoning=zoning,
        sofa_mode="right",
        image_paths=[str(photo)],
        geometry_verifier=timeout_verifier,
        floor_reference_estimator=lambda *_: {
            "status": "observed",
            "confidence": "high",
            "direction_xy": [1.0, 0.0],
            "angle_degrees": 0.0,
            "support_count": 20,
            "unsafe_codes": [],
        },
    )

    assert summary["status"] == "blocked"
    assert summary["verification_status"] == "fail"
    assert summary["verification_attempt_count"] == verifier_s2.S2_VERIFY_MAX_ATTEMPTS
    assert summary["verification_corrected"] is False
    assert summary["verification_retry_reason"] == "retryable_exception"
    assert summary["verification_exception_type"] == "TimeoutError"
    assert "exception=TimeoutError" in summary["reason"]
    assert len(summary["verification_history"]) == verifier_s2.S2_VERIFY_MAX_ATTEMPTS
    assert summary["verification_history"] == artifacts["verification_history"]
    saved = json.loads(
        Path(artifacts["verification_path"]).read_text(encoding="utf-8")
    )
    assert len(saved["history"]) == verifier_s2.S2_VERIFY_MAX_ATTEMPTS
    assert saved["history"][-1]["exception_type"] == "TimeoutError"


@pytest.mark.parametrize("orientation", [6, 8])
def test_run_layout_contract_s2_uses_exif_transposed_dimensions(tmp_path, orientation):
    photo = tmp_path / f"orientation{orientation}.jpg"
    exif = Image.Exif()
    exif[274] = orientation
    Image.new("RGB", (1000, 700), "white").save(photo, exif=exif)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": photo.name,
            "sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
    }
    summary, artifacts = api._run_layout_contract_s2(
        job_id=f"job-exif-{orientation}", job_dir=tmp_path,
        photo_path=str(photo), view_index=0, user_zoning_v2=zoning,
        legacy_zoning=zoning, sofa_mode="free", image_paths=[str(photo)],
        geometry_verifier=lambda *_: dict(VERIFIER_PASS),
        floor_reference_estimator=lambda *_: {
            "status": "observed", "confidence": "high",
            "direction_xy": [1.0, 0.0], "angle_degrees": 0.0,
            "support_count": 20, "unsafe_codes": [],
        },
    )
    assert summary["status"] == "safe"
    assert artifacts["contract"]["source"]["size"] == {"width": 700, "height": 1000}


def test_run_layout_contract_s2_wrong_photo_is_blocked_without_guide(tmp_path):
    best = tmp_path / "best.jpg"
    wrong = tmp_path / "wrong.jpg"
    Image.new("RGB", (1000, 700), "white").save(best)
    Image.new("RGB", (1000, 700), "gray").save(wrong)
    zoning = {
        "best_photo_index": 0,
        "_source_binding": {
            "photo_key": "upload/best.jpg",
            "sha256": hashlib.sha256(best.read_bytes()).hexdigest(),
        },
        "struct_geometry_v1": _safe_geometry(),
    }

    summary, artifacts = api._run_layout_contract_s2(
        job_id="job-s2-cross-photo",
        job_dir=tmp_path,
        photo_path=str(wrong),
        view_index=1,
        user_zoning_v2=zoning,
        legacy_zoning=zoning,
        sofa_mode="free",
        image_paths=[str(best), str(wrong)],
        geometry_verifier=lambda *_: (_ for _ in ()).throw(
            AssertionError("wrong-photo path must not call geometry verifier")
        ),
    )

    assert summary["status"] == "blocked"
    assert artifacts["eligible"] is False
    assert artifacts["guide_path"] is None
    contract = json.loads(Path(artifacts["contract_path"]).read_text(encoding="utf-8"))
    assert contract["decision"]["disposition"] == "BLOCKED"
    assert "MISSING_PHOTO_BINDING" in contract["decision"]["unsafe_codes"]


def test_s2_feature_flag_defaults_off_and_requires_explicit_one(monkeypatch):
    monkeypatch.delenv("LAYOUT_CONTRACT_S2", raising=False)
    assert api._layout_contract_s2_enabled() is False

    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "1")
    assert api._layout_contract_s2_enabled() is True

    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "0")
    assert api._layout_contract_s2_enabled() is False


def test_s2_compact_entry_mode_only_for_b_candidate_on_entrance_side():
    contract = {
        "decision": {"chosen_candidate_id": "s2_b_left"},
        "candidates": [{
            "candidate_id": "s2_b_left",
            "candidate_type": "B",
            "notes": ["sofa_side=left", "tv_side=right"],
        }],
    }

    assert api._s2_compact_entry_mode({"_entrance_side": "left"}, contract) is True
    assert api._s2_compact_entry_mode({"_entrance_side": "right"}, contract) is False

    contract["candidates"][0]["candidate_type"] = "F"
    assert api._s2_compact_entry_mode({"_entrance_side": "left"}, contract) is False


def test_s2_model_not_applicable_is_distinguished_from_unsafe_verdict():
    """3135DE37｜S2 的幾何模型要「兩面相對長牆」，斜角拍的方正房（左落地窗牆、
    中間兩扇臥室門與牆垛、右大門）根本沒有這種結構 → NO_USABLE_WALL 直接擋死，
    判官連叫都沒叫，已付費的客戶一張圖都拿不到。
    「模型化不了」必須跟「驗過判定不安全」分開處理。"""
    not_applicable = {
        "verification_status": None,
        "verification_attempt_count": 0,
        "unsafe_codes": ["GEOM_NOT_ELIGIBLE", "NO_USABLE_WALL",
                         "CANDIDATE_GEOMETRY_INCOMPLETE"],
    }
    assert api._s2_model_not_applicable(not_applicable) is True

    # 判官真的驗過並判不安全 → 不得回退
    verified_unsafe = {
        "verification_status": "fail",
        "verification_attempt_count": 2,
        "unsafe_codes": ["GEOM_NOT_ELIGIBLE"],
    }
    assert api._s2_model_not_applicable(verified_unsafe) is False

    # 判官驗過而且通過 → 本來就不是 blocked
    assert api._s2_model_not_applicable({
        "verification_status": "pass", "verification_attempt_count": 1,
        "unsafe_codes": []}) is False

    # 沒有任何碼 / 非 dict → 保守回 False（不豁免）
    assert api._s2_model_not_applicable({"unsafe_codes": []}) is False
    assert api._s2_model_not_applicable(None) is False

    # 混進非結構類的碼（例如判官語意碼）→ 不豁免
    assert api._s2_model_not_applicable({
        "verification_status": None, "verification_attempt_count": 0,
        "unsafe_codes": ["NO_USABLE_WALL", "SOFA_WALL_CONTACT_FAIL"]}) is False


def test_unmodellable_room_only_gets_one_paid_shot_when_door_is_excluded():
    """2CD074F0｜門仍在鏡內且沒有 guide 時不得付費裸生；只有門確定出鏡才放行。"""
    source = Path(api.__file__).read_text(encoding="utf-8")

    assert api._allow_waived_single_shot_without_guide(
        True, "living", None, True) is True
    assert api._allow_waived_single_shot_without_guide(
        True, "living", None, False) is False
    assert api._allow_waived_single_shot_without_guide(
        True, "living", "guide.jpg", True) is False
    assert api._allow_waived_single_shot_without_guide(
        True, "bedroom", None, True) is False
    assert "bool(door_excluded_flags[vi])" in source

    # 三個補生管道都必須認這個旗標，否則又退回反覆付費
    assert source.count('_allow_single_shot_without_guide"):') >= 3

    pipeline = (Path(api.__file__).parent / "test_full_pipeline.py").read_text(
        encoding="utf-8")
    # 付費前閘門要放行，但只放行有旗標的
    assert "_single_shot = bool(render.get(\"_allow_single_shot_without_guide\"))" in pipeline
    assert "and not _single_shot" in pipeline


def test_living_zone_zoom_crop_rules():
    """928AD8B4｜用戶提案：分區層認得出哪塊是客廳，就裁進去再擺家具。
    實測那張斜角照裁出 2298x2057，門與臥室門全部出鏡，規劃器就有解。"""
    import tempfile
    import cv2
    import numpy as np

    with tempfile.TemporaryDirectory() as td:
        photo = str(Path(td) / "room.jpg")
        cv2.imwrite(photo, np.full((3024, 4032, 3), 200, dtype=np.uint8))

        # 正常客廳區（畫面左半、下半）→ 裁得出來
        zoom = api._crop_to_living_zone(photo, td, 0, [360, 30, 960, 530])
        assert zoom is not None
        base, box = zoom
        assert Path(base).exists()
        img = cv2.imread(base)
        assert img.shape[1] > 4032 * 0.30 and img.shape[0] > 3024 * 0.30

        # 分區抓得太小 → 寧可不裁（可能是誤判，裁下去會失真又缺牆）
        assert api._crop_to_living_zone(photo, td, 1, [400, 400, 550, 550]) is None
        # bbox 壞掉 → 不裁
        assert api._crop_to_living_zone(photo, td, 2, None) is None
        assert api._crop_to_living_zone(photo, td, 3, [1, 2, 3]) is None


def test_zoom_path_is_wired_into_the_s2_waiver():
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "_crop_to_living_zone(" in source
    assert "_rebuild_guide_on_zoom(" in source
    assert '"living_zone_zoom"' in source
    # 只有 bbox 明確完全在 crop 外才可標記出鏡；未知時保留避門 prompt。
    assert "door_excluded_flags[_vi] = (_zoom_door_visible is False)" in source
    # zoom 座標改變後，重建失敗也不得沿用舊 guide。
    clear_pos = source.index("layout_guide_paths.pop(_vi, None)", source.index("_zoom_door_visible"))
    accept_pos = source.index("if _zoom_guide:", clear_pos)
    assert clear_pos < accept_pos


def test_2cd074f0_tv_target_moves_past_left_door_clearance():
    """2CD074F0｜左側門禁區到 44%，TV 必須從禁區終點後取樣，不得只試 4/18/28%。"""
    plan = api._layout_guide_plan(
        880, 780, "free",
        entrance_side="left",
        entrance_bbox=(10, 110, 190, 640),
        focal_side="left",
        auto_float=False,
        living_bbox=(40, 40, 840, 740),
    )
    assert plan["valid"] is True
    assert plan["chosen_sofa_side"] == "right"
    assert plan["tv"][0] > plan["door_clear"][2]


def test_2cd074f0_tv_target_mirrors_past_right_door_clearance():
    """同一規則左右鏡像：右側門牆的 TV 必須完整落在 door_clear 左邊。"""
    plan = api._layout_guide_plan(
        880, 780, "free",
        entrance_side="right",
        entrance_bbox=(690, 110, 870, 640),
        focal_side="right",
        auto_float=False,
        living_bbox=(40, 40, 840, 740),
    )
    assert plan["valid"] is True
    assert plan["chosen_sofa_side"] == "left"
    assert plan["tv"][2] < plan["door_clear"][0]


def test_zoom_guide_reports_door_visibility_from_actual_crop(tmp_path):
    """30FBA4A5｜living-zone zoom 不能寫死門已出鏡；以原圖 bbox 與 crop 交集為準。"""
    import cv2
    import numpy as np

    source = str(tmp_path / "source.jpg")
    zoom = str(tmp_path / "zoom.jpg")
    cv2.imwrite(source, np.full((1000, 1000, 3), 200, dtype=np.uint8))
    cv2.imwrite(zoom, np.full((800, 800, 3), 200, dtype=np.uint8))
    zoning = {
        "_sofa_layout": "free",
        "zones": {
            "entrance_zone": {"bbox_on_best_photo": [100, 100, 500, 300]},
            "living_zone": {"bbox_on_best_photo": [100, 100, 900, 900]},
        },
        "furniture_placement_rules": {},
    }

    _guide_in, door_visible = api._rebuild_guide_on_zoom(
        zoom, tmp_path, 0, zoning, source, (0, 0, 800, 800))
    assert door_visible is True

    _guide_out, door_visible = api._rebuild_guide_on_zoom(
        zoom, tmp_path, 1, zoning, source, (400, 0, 1000, 800))
    assert door_visible is False


def test_zoom_guide_without_entrance_bbox_keeps_door_status_unknown(tmp_path):
    """入口資料缺失或損壞都不是出鏡證據；必須保留避門 prompt。"""
    import cv2
    import numpy as np

    source = str(tmp_path / "source.jpg")
    zoom = str(tmp_path / "zoom.jpg")
    cv2.imwrite(source, np.full((600, 900, 3), 200, dtype=np.uint8))
    cv2.imwrite(zoom, np.full((600, 900, 3), 200, dtype=np.uint8))
    _guide, door_visible = api._rebuild_guide_on_zoom(
        zoom, tmp_path, 2,
        {"_sofa_layout": "free", "zones": {}, "furniture_placement_rules": {}},
        source, (0, 0, 900, 600))
    assert door_visible is None

    _guide, door_visible = api._rebuild_guide_on_zoom(
        zoom, tmp_path, 3,
        {"_sofa_layout": "free",
         "zones": {"entrance_zone": {"bbox_on_best_photo": [1, 2, 3]}},
         "furniture_placement_rules": {}},
        source, (0, 0, 900, 600))
    assert door_visible is None


def test_s2_verifier_unstable_triggers_waiver():
    """173C14C5／D85B8525｜判官 fail 但 fail 欄位跨多次不穩定 = 判官不確定、
    S2 model 不動此房型 → 回退 legacy（通用，不是 173 特例）。"""
    # 不穩定：三次失敗，欄位組合每次不同
    unstable = {
        "verification_status": "fail",
        "verification_history": [
            {"outcome": "hard_fail", "sofa_back_contact": "fail",
             "walkway_connected": "fail", "cross_axis_matches_floor_transverse": "fail"},
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail",
             "right_wall_floor_alignment": "fail", "sofa_back_contact": "fail"},
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail",
             "sofa_back_contact": "fail", "walkway_connected": "fail"},
        ],
    }
    assert api._s2_verifier_unstable(unstable) is True

    # 穩定的真不安全：每次都同一組 fail 欄位 → 不 waive，照擋
    stable = {
        "verification_status": "fail",
        "verification_history": [
            {"outcome": "hard_fail", "sofa_back_contact": "fail"},
            {"outcome": "hard_fail", "sofa_back_contact": "fail"},
        ],
    }
    assert api._s2_verifier_unstable(stable) is False

    # 判官 pass / 只一次 / 沒 history → 不觸發
    assert api._s2_verifier_unstable({"verification_status": "pass"}) is False
    assert api._s2_verifier_unstable({
        "verification_status": "fail",
        "verification_history": [{"outcome": "hard_fail", "sofa_back_contact": "fail"}]}) is False
    assert api._s2_verifier_unstable(None) is False


def test_waiver_branch_honours_verifier_instability():
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "_s2_model_not_applicable(_sum) or _s2_verifier_unstable(_sum)" in source

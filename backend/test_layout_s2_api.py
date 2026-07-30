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
    """2CD074F0／BDD0C702｜附加欄位抖動不能蓋掉每次都存在的共同 hard fail。"""
    # 2CD074F0：完整集合交替變動，但四次共同 fail sofa_back + left_wall。
    # 這是穩定的不安全，不得 waive。
    common_core = {
        "verification_status": "fail",
        "verification_history": [
            {"outcome": "hard_fail", "sofa_back_contact": "fail",
             "tv_wall_contact": "fail", "left_wall_floor_alignment": "fail",
             "right_wall_floor_alignment": "fail"},
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail",
             "sofa_back_contact": "fail"},
            {"outcome": "hard_fail", "sofa_back_contact": "fail",
             "tv_wall_contact": "fail", "left_wall_floor_alignment": "fail",
             "right_wall_floor_alignment": "fail"},
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail",
             "sofa_back_contact": "fail"},
        ],
    }
    assert api._s2_verifier_unstable(common_core) is False

    # BDD0C702：兩次完全相同，同樣不 waive。
    bdd_stable = {
        "verification_status": "fail",
        "verification_history": [
            {"outcome": "hard_fail", "sofa_back_contact": "fail",
             "left_wall_floor_alignment": "fail"},
            {"outcome": "hard_fail", "sofa_back_contact": "fail",
             "left_wall_floor_alignment": "fail"},
        ],
    }
    assert api._s2_verifier_unstable(bdd_stable) is False

    # 真不穩定：多次判決完全沒有共同 fail，才准回退 legacy。
    no_common_failure = {
        "verification_status": "fail",
        "verification_history": [
            {"outcome": "hard_fail", "sofa_back_contact": "fail"},
            {"outcome": "hard_fail", "walkway_connected": "fail"},
            {"outcome": "hard_fail", "tv_wall_contact": "fail"},
        ],
    }
    assert api._s2_verifier_unstable(no_common_failure) is True

    # 判官 pass / 只一次 / 沒 history → 不觸發
    assert api._s2_verifier_unstable({"verification_status": "pass"}) is False
    assert api._s2_verifier_unstable({
        "verification_status": "fail",
        "verification_history": [{"outcome": "hard_fail", "sofa_back_contact": "fail"}]}) is False
    assert api._s2_verifier_unstable(None) is False


def test_waiver_branch_honours_verifier_instability():
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "_s2_model_not_applicable(_sum) or _s2_verifier_unstable(_sum)" in source


def test_s2_would_hard_block_only_when_not_eligible_and_not_waive():
    art_ok = {"eligible": True}
    assert api._s2_would_hard_block({"verification_status": "fail"}, art_ok) is False

    # 結構不足、判官沒跑 → waive，不是硬擋
    struct_fail = {
        "verification_status": None,
        "verification_attempt_count": 0,
        "unsafe_codes": ["NO_USABLE_WALL"],
    }
    assert api._s2_model_not_applicable(struct_fail) is True
    assert api._s2_would_hard_block(struct_fail, {"eligible": False}) is False

    # 判官真的跑過、欄位一致、無例外 → 硬擋（FD73 型）
    hard = {
        "verification_status": "fail",
        "verification_attempt_count": 3,
        "verification_failed_fields": {"left_wall_floor_alignment": "fail",
                                       "tv_wall_contact": "fail"},
        "unsafe_codes": ["GEOM_NOT_ELIGIBLE"],
        "verification_history": [
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail",
             "sofa_back_contact": "fail"},
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail",
             "tv_wall_contact": "fail"},
        ],
    }
    assert api._s2_verifier_unstable(hard) is False
    assert api._s2_would_hard_block(hard, {"eligible": False}) is True


def test_s2_zoning_resample_max_bounds(monkeypatch):
    """只核准多抽 1 次 → 預設與硬上限都是 2。"""
    monkeypatch.delenv("S2_ZONING_RESAMPLE_MAX", raising=False)
    assert api._s2_zoning_resample_max() == 2
    monkeypatch.setenv("S2_ZONING_RESAMPLE_MAX", "1")
    assert api._s2_zoning_resample_max() == 1
    monkeypatch.setenv("S2_ZONING_RESAMPLE_MAX", "99")
    assert api._s2_zoning_resample_max() == 2
    monkeypatch.setenv("S2_ZONING_RESAMPLE_MAX", "nope")
    assert api._s2_zoning_resample_max() == 2


def test_s2_contract_with_zoning_resample_stops_on_first_eligible():
    """第 1 次就合格 → 不重抽、不替換 zoning。"""
    calls = {"n": 0, "rezone": 0}

    def run_contract(zv2):
        calls["n"] += 1
        return ({"verification_status": "pass"}, {"eligible": True, "guide_path": "g"})

    def rezone(prev):
        calls["rezone"] += 1
        return {"_provenance": {"request_fingerprint": "x"}}

    summary, art, commit, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"id": "orig"},
        photo_path="/tmp/p.jpg",
        max_attempts=3,
        run_contract=run_contract,
        rezone=rezone,
    )
    assert art["eligible"] is True
    assert calls["n"] == 1 and calls["rezone"] == 0
    assert commit is None
    assert len(log) == 1 and log[0]["eligible"] is True


def test_s2_contract_with_zoning_resample_retries_until_eligible():
    """硬擋一次後重抽，第 2 次合格 → 重抽 1 次並 commit 換過幾何的那份。"""
    hard = _hard_block_summary()
    states = iter([
        (hard, {"eligible": False}),
        ({"verification_status": "pass"}, {"eligible": True, "guide_path": "g"}),
    ])
    rezoned = []

    def run_contract(zv2):
        return next(states)

    def rezone(prev):
        z = {"struct_geometry_v1": {"id": f"z{len(rezoned) + 1}"},
             "_provenance": {"request_fingerprint": f"fp{len(rezoned)}"}}
        rezoned.append(z)
        return z

    summary, art, commit, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"}},
        photo_path="/tmp/p.jpg",
        max_attempts=2,
        run_contract=run_contract,
        rezone=rezone,
    )
    assert art["eligible"] is True
    assert len(rezoned) == 1
    assert commit["struct_geometry_v1"] == rezoned[-1]["struct_geometry_v1"]
    assert commit["_provenance"]["request_fingerprint"] == "fp0"
    assert [e["eligible"] for e in log] == [False, True]


def test_s2_contract_with_zoning_resample_does_not_retry_on_waive():
    """模型化不了 → waive，不重抽。"""
    struct = {
        "verification_status": None,
        "verification_attempt_count": 0,
        "unsafe_codes": ["NO_USABLE_WALL"],
    }
    calls = {"n": 0, "rezone": 0}

    def run_contract(zv2):
        calls["n"] += 1
        return (struct, {"eligible": False})

    def rezone(prev):
        calls["rezone"] += 1
        return {"id": "new"}

    summary, art, commit, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"id": "orig"},
        photo_path="/tmp/p.jpg",
        max_attempts=3,
        run_contract=run_contract,
        rezone=rezone,
    )
    assert art["eligible"] is False
    assert calls["n"] == 1 and calls["rezone"] == 0
    assert commit is None
    assert log[0]["waive_model"] is True


def test_s2_contract_with_zoning_resample_exhausts_then_stays_blocked():
    """N 次都硬擋 → 不 commit，log 長度 = N，且與今天一樣維持 incomplete。"""
    hard = _hard_block_summary()
    rezoned = []

    def run_contract(zv2):
        return (hard, {"eligible": False})

    def rezone(prev):
        z = {"struct_geometry_v1": {"id": len(rezoned)}}
        rezoned.append(z)
        return z

    summary, art, commit, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"}},
        photo_path="/tmp/p.jpg",
        max_attempts=2,
        run_contract=run_contract,
        rezone=rezone,
    )
    assert art["eligible"] is False
    assert commit is None
    assert len(rezoned) == 1  # 第 1 次用原幾何，只再重抽 1 次
    assert len(log) == 2
    assert all(e.get("hard_block") for e in log)


def test_s2_would_hard_block_accepts_the_real_geometry_block():
    """先釘住正例，否則下面每條「不准重抽」都可能只是因為函式永遠回 False。"""
    assert api._s2_would_hard_block(
        _hard_block_summary(), {"eligible": False}) is True


# 每個案例只破壞一個條件，其餘保持合格 —— 否則拿掉任一守門都不會讓測試變紅
# （我第一版把多個不合格條件塞在同一個 fixture，蓄意破壞驗出三個空洞）。
@pytest.mark.parametrize("field,value,why", [
    ("verification_exception_type", "ValueError", "判官丟例外＝基礎設施壞了"),
    ("verification_attempt_count", 0, "判官根本沒跑過"),
    ("verification_failed_fields", {}, "fail 但講不出哪個欄位"),
    ("verification_status", "pass", "判官沒判 fail"),
])
def test_s2_would_hard_block_needs_every_condition(field, value, why):
    summary = dict(_hard_block_summary())
    summary[field] = value
    assert api._s2_would_hard_block(summary, {"eligible": False}) is False, why


@pytest.mark.parametrize("code_field", ["unsafe_codes", "verification_unsafe_codes"])
def test_s2_would_hard_block_rejects_infrastructure_code(code_field):
    """S2 的 fail-closed 分支會寫 verification_status='fail'，光看 status
    會把「程式炸了」當成「幾何硬擋」，白花一次 zoning ＋一整輪判官。"""
    summary = dict(_hard_block_summary())
    summary[code_field] = ["S2_PIPELINE_ERROR"]
    assert api._s2_would_hard_block(summary, {"eligible": False}) is False


def test_s2_would_hard_block_defers_to_waive_when_verifier_is_unstable():
    """判官失敗欄位亂跳＝S2 對這房型算不穩，該回退 legacy 引導，不是花錢重抽幾何。

    這條刻意用「其他條件全部合格、只有 history 欄位不一致」的 summary——
    第一版測試沒有這種案例，於是拿掉 waive 守門也不會變紅（蓄意破壞驗出來的）。
    """
    summary = dict(_hard_block_summary())
    summary["verification_history"] = [
        {"outcome": "hard_fail", "left_wall_floor_alignment": "fail"},
        {"outcome": "hard_fail", "walkway_connected": "fail"},
    ]
    assert api._s2_verifier_unstable(summary) is True
    assert api._s2_would_hard_block(summary, {"eligible": False}) is False


def test_s2_would_hard_block_rejects_unknown_state():
    """空 summary → 不知道發生什麼，不花錢。"""
    assert api._s2_would_hard_block({}, {"eligible": False}) is False
    assert api._s2_would_hard_block(None, {"eligible": False}) is False
    assert api._s2_would_hard_block(
        _hard_block_summary(), {"eligible": True}) is False


def test_s2_resample_max_is_capped_at_one_extra_draw(monkeypatch):
    """每次重抽 = 1 次 zoning flash + 整輪 S2 判官；只核准多抽 1 次。"""
    monkeypatch.delenv("S2_ZONING_RESAMPLE_MAX", raising=False)
    assert api._s2_zoning_resample_max() == 2
    for raw in ("3", "5", "99"):
        monkeypatch.setenv("S2_ZONING_RESAMPLE_MAX", raw)
        assert api._s2_zoning_resample_max() == 2, f"{raw} 應被夾到 2"
    monkeypatch.setenv("S2_ZONING_RESAMPLE_MAX", "1")
    assert api._s2_zoning_resample_max() == 1, "設 1 要能關閉重抽"


def test_max_attempts_argument_cannot_bypass_the_hard_cap():
    """max_attempts 是為了測試可注入，不是繞過成本核准的後門。"""
    hard = _hard_block_summary()
    rezoned = []
    _s, _a, _c, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"}},
        photo_path="/tmp/p.jpg",
        max_attempts=99,
        run_contract=lambda z: (hard, {"eligible": False}),
        rezone=lambda prev: rezoned.append(1) or {"struct_geometry_v1": {"id": len(rezoned)}},
    )
    assert len(rezoned) == 1, f"max_attempts=99 仍只准重抽 1 次，實際 {len(rezoned)}"
    assert len(log) == 2


def test_every_attempt_records_which_geometry_was_judged():
    """第 2 抽仍失敗時訂單存的是舊幾何——沒有雜湊就對不回任何一份幾何。"""
    hard = _hard_block_summary()
    _s, _a, commit, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"}},
        photo_path="/tmp/p.jpg",
        max_attempts=2,
        run_contract=lambda z: (hard, {"eligible": False}),
        rezone=lambda prev: {"struct_geometry_v1": {"id": "resampled"}},
    )
    assert commit is None, "全敗就不該改動訂單的 zoning"
    shas = [e.get("geometry_sha256") for e in log]
    assert all(shas), f"每次 attempt 都要記幾何雜湊，實際 {shas}"
    assert shas[0] != shas[1], "兩次驗的是不同幾何，雜湊不可相同"
    assert shas[0] == api._struct_geometry_sha256({"struct_geometry_v1": {"id": "orig"}})
    # 判官最後判的那份幾何必須留得住，否則下次查死因又對不起來
    assert log[-1]["verified_struct_geometry_v1"] == {"id": "resampled"}


def test_no_leftover_geometry_when_nothing_was_resampled():
    """第一次就結案時不該塞多餘幾何進診斷（payload 不要無謂長大）。"""
    _s, _a, _c, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"}},
        photo_path="/tmp/p.jpg",
        max_attempts=2,
        run_contract=lambda z: ({"verification_status": "pass"}, {"eligible": True}),
        rezone=lambda prev: {"struct_geometry_v1": {"id": "should-not-happen"}},
    )
    assert len(log) == 1
    assert "verified_struct_geometry_v1" not in log[0]


@pytest.mark.parametrize("raw", [
    r"C:\Users\user\AppData\Local\Temp\jobs\FD73C48C\photo_01.jpg",
    "/app/jobs/FD73C48C/photo_01.jpg",
    "/tmp/upload/photo_01.jpg",
    r"D:\railway\photo_01.jpg",
])
def test_portable_photo_key_never_leaks_a_local_path(raw):
    """canonical_photo_key 只正規化斜線——本機路徑會原封不動被吐回來。

    這條是行為測：直接餵真實的 Windows／容器路徑，斷言輸出不含磁碟機、
    父目錄與反斜線。舊測試只擋「直接寫 str(_contract_photo)」，抓不到
    經由 canonical_photo_key 的間接洩漏。
    """
    key = api._portable_photo_key(raw)
    assert key == "photo_01.jpg", key
    assert "\\" not in key and "/" not in key
    assert ":" not in key
    for leak in ("Users", "AppData", "app", "tmp", "jobs", "railway", "FD73C48C"):
        assert leak not in key, f"{leak} 洩漏進 photo_key：{key}"


def test_portable_photo_key_prefers_the_upload_binding():
    """有上傳綁定就用它——那才是跨容器對得起來的識別。"""
    zoning = {"_source_binding": {"photo_key": "upload-abc/photo_01.jpg"}}
    assert api._portable_photo_key(
        "/app/jobs/J/photo_01.jpg", zoning=zoning) == "upload-abc/photo_01.jpg"
    # 綁定本身是容器路徑時不可採用，要退到 basename
    bad_bind = {"_source_binding": {"photo_key": "/app/jobs/J/photo_01.jpg"}}
    assert api._portable_photo_key(
        "/app/jobs/J/photo_01.jpg", zoning=bad_bind) == "photo_01.jpg"


def test_portable_photo_key_falls_back_to_the_local_key_map():
    assert api._portable_photo_key(
        "/app/jobs/J/photo_01.jpg",
        key_by_local={"/app/jobs/J/photo_01.jpg": "uploads/up-9/photo_01.jpg"},
    ) == "up-9/photo_01.jpg"


def test_is_portable_photo_key_accepts_real_storage_keys():
    """先釘正例，否則上面每條「不可洩漏」都可能只是因為函式永遠退回 basename。"""
    assert api._is_portable_photo_key("upload-abc/photo_01.jpg") is True
    assert api._is_portable_photo_key("photo_01.jpg") is True
    assert api._is_portable_photo_key("app/jobs/J/photo.jpg") is False
    assert api._is_portable_photo_key("C:/Users/u/photo.jpg") is False
    assert api._is_portable_photo_key("tmp/photo.jpg") is False
    assert api._is_portable_photo_key("") is False


def test_resample_diagnostics_do_not_store_server_paths():
    """診斷不可寫入 Railway 容器的完整路徑（換容器就失效，還洩執行環境）。"""
    source = Path(api.__file__).read_text(encoding="utf-8")
    block = source.split("s2_zoning_resample_log.append(")[1][:460]
    assert "_portable_photo_key(" in block, "必須走可攜 key，不可直接用 canonical"
    assert "photo_sha256" in block
    assert 'str(_contract_photo),' not in block, "又把完整路徑存回去了"


def test_resample_never_spends_more_than_the_capped_draws(monkeypatch):
    """不給 max_attempts 時，實際重抽次數必須受環境上限約束。"""
    monkeypatch.delenv("S2_ZONING_RESAMPLE_MAX", raising=False)
    hard = _hard_block_summary()
    rezoned = []
    api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"}},
        photo_path="/tmp/p.jpg",
        run_contract=lambda z: (hard, {"eligible": False}),
        rezone=lambda prev: rezoned.append(1) or {"struct_geometry_v1": {"id": len(rezoned)}},
    )
    assert len(rezoned) == 1, f"只准多抽 1 次，實際 {len(rezoned)}"


def _hard_block_summary():
    return {
        "verification_status": "fail",
        "verification_attempt_count": 3,
        "verification_failed_fields": {"left_wall_floor_alignment": "fail"},
        "unsafe_codes": ["GEOM_NOT_ELIGIBLE"],
        "verification_history": [
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail"},
            {"outcome": "hard_fail", "left_wall_floor_alignment": "fail"},
        ],
    }


def test_resample_swaps_geometry_and_keeps_the_customer_side():
    """重抽只換結構觀測。整包換掉會讓新一輪 Gemini 的 AI 建議冒充客戶硬綁左右。"""
    original = {
        "struct_geometry_v1": {"id": "old-geom"},
        "best_photo_index": 0,
        "proposed_zones": {"living_zone": {
            "sofa_side": "left", "sofa_side_source": "user_explicit",
            "alt_sofa_side": "right"}},
        "existing_zones": {"walkway": {"where": "中央"}},
        "overall_confidence": "high",
    }
    resampled = {
        "struct_geometry_v1": {"id": "new-geom"},
        "best_photo_index": 0,
        "_source_binding": {"photo_key": "k", "sha256": "s"},
        "_provenance": {"request_fingerprint": "fp-new"},
        # 新一輪 Gemini 自己的建議——絕對不可蓋掉客戶的選擇
        "proposed_zones": {"living_zone": {
            "sofa_side": "right", "sofa_side_source": "ai_default"}},
        "existing_zones": {"walkway": {"where": "靠左"}},
    }
    merged = api._s2_zoning_with_resampled_geometry(original, resampled)
    assert merged["struct_geometry_v1"] == {"id": "new-geom"}, "幾何要換新"
    assert merged["_provenance"]["request_fingerprint"] == "fp-new"
    assert merged["_source_binding"] == {"photo_key": "k", "sha256": "s"}
    living = merged["proposed_zones"]["living_zone"]
    assert living["sofa_side"] == "left", "客戶選的側別被覆蓋了"
    assert living["sofa_side_source"] == "user_explicit", "來源被覆蓋＝AI 冒充客戶"
    assert living["alt_sofa_side"] == "right"
    assert merged["existing_zones"] == {"walkway": {"where": "中央"}}
    assert merged["overall_confidence"] == "high"
    assert api._s2_zoning_with_resampled_geometry(original, None) is None


def test_resample_keeps_derived_sofa_side_stable_across_attempts():
    """幾何換了，_guide_sofa_side 導出的側別必須不變——否則就是「新牆線配舊擺法」。"""
    original = {
        "struct_geometry_v1": {"id": "old"},
        "proposed_zones": {"living_zone": {
            "where": "靠右長牆", "sofa_side": "right",
            "sofa_side_source": "user_explicit"}},
        "existing_zones": {},
    }
    merged = api._s2_zoning_with_resampled_geometry(
        original, {"struct_geometry_v1": {"id": "new"},
                   "proposed_zones": {"living_zone": {
                       "sofa_side": "left", "sofa_side_source": "ai_default"}}})
    before = api._guide_sofa_side(api.flatten_zoning_v2_to_v1(original, "A"))
    after = api._guide_sofa_side(api.flatten_zoning_v2_to_v1(merged, "A"))
    assert before == after == "right"


def test_run_contract_sees_the_resampled_geometry():
    """重抽後那一次規劃，收到的必須是新幾何——不是又拿舊的跑一遍。"""
    seen = []
    hard = _hard_block_summary()
    outcomes = iter([
        (hard, {"eligible": False}),
        ({"verification_status": "pass"}, {"eligible": True, "guide_path": "g"}),
    ])

    def run_contract(zv2):
        seen.append((zv2 or {}).get("struct_geometry_v1", {}).get("id"))
        return next(outcomes)

    _s, _a, commit, log = api._s2_contract_with_zoning_resample(
        initial_zoning_v2={"struct_geometry_v1": {"id": "orig"},
                           "proposed_zones": {"living_zone": {"sofa_side": "left"}}},
        photo_path="/tmp/p.jpg",
        max_attempts=2,
        run_contract=run_contract,
        rezone=lambda prev: {"struct_geometry_v1": {"id": "resampled"}},
    )
    assert seen == ["orig", "resampled"]
    assert commit["struct_geometry_v1"] == {"id": "resampled"}
    # 合格後提交的那份，客戶側別仍在
    assert commit["proposed_zones"]["living_zone"]["sofa_side"] == "left"
    # commit 已經帶著新幾何了，診斷不該再複製一份大 blob 進 result_json
    assert "verified_struct_geometry_v1" not in log[-1]
    assert [e["geometry_sha256"] for e in log] == [
        api._struct_geometry_sha256({"struct_geometry_v1": {"id": "orig"}}),
        api._struct_geometry_sha256({"struct_geometry_v1": {"id": "resampled"}}),
    ]


def test_pipeline_hard_block_path_actually_calls_the_resample_helper(monkeypatch):
    """行為測：走硬擋路徑時，pipeline 真的會呼叫 resample helper。

    先前這條只 grep api.py 的字串——而 helper 的 def 本身就含那個字串，
    所以把 run_pipeline 裡真正的呼叫整段刪掉，測試照樣綠。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(api.run_pipeline))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_s2_contract_with_zoning_resample"
    ]
    assert calls, "run_pipeline 內找不到對 resample helper 的實際呼叫"
    kwargs = {kw.arg for kw in calls[0].keywords}
    assert {"initial_zoning_v2", "photo_path", "run_contract"} <= kwargs, kwargs
    # 而且 _run_layout_contract_s2 不再被 run_pipeline 直接呼叫繞過重抽
    direct = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_run_layout_contract_s2"
    ]
    for node in direct:
        assert any(
            isinstance(fn, ast.FunctionDef) and fn.name == "_run_one_s2"
            and fn.lineno <= node.lineno <= (fn.end_lineno or node.lineno)
            for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        ), "run_pipeline 直接呼叫 S2 合約＝繞過重抽路徑"


def test_pipeline_does_not_reflatten_after_resample():
    """重抽只換幾何，flatten 出來必然相同；重跑只是多一次改到客戶側別的機會。"""
    source = Path(api.__file__).read_text(encoding="utf-8")
    block = source.split("_zv2_commit, dict")[1][:700]
    assert "flatten_zoning_v2_to_v1" not in block, "重抽提交後不應再 flatten"
    assert "_guide_sofa_side" not in block, "重抽提交後不應重算側別"


def test_s2_preflight_blocked_result_is_terminal_and_not_infrastructure():
    blocked = api._s2_preflight_blocked_result({
        "style": "nordic",
        "_room_type": "living",
        "_layout_mode": "s2_blocked_legacy",
        "_layout_contract_s2_required": True,
    })
    assert blocked is not None
    assert blocked["render_path"] is None
    assert blocked["render_mode"] == "preflight_blocked"
    assert blocked["error_type"] == "S2PreflightBlocked"
    assert blocked["_s2_preflight_blocked"] is True
    assert blocked["validation"]["s2_preflight_blocked"] is True

    event = api._record_validation_attempt_inner(
        blocked,
        job_id="BDD0C702",
        stage="pre_generation",
        attempt=1,
        validation=blocked["validation"],
    )
    assert event["failure_class"] == "s2_preflight_blocked"
    assert api._validation_diagnostics(blocked)["failure_class"] == "s2_preflight_blocked"
    assert len(blocked["validation_history"]) == 1


def test_s2_preflight_block_is_wired_before_generation_and_all_retry_phases():
    source = Path(api.__file__).read_text(encoding="utf-8")
    initial = source.index("preflight_blocked = _s2_preflight_blocked_result(entry)")
    paid = source.index("single_result = generate_renders", initial)
    assert initial < paid
    assert "S2 前檢已封鎖 → 跳過 Z3" in source
    assert "S2 前檢已封鎖 → 跳過 Phase2" in source
    assert "S2 前檢已封鎖 → 跳過 Phase3" in source


def test_s2_shadow_free_signal_reports_legacy_guide_presence(tmp_path):
    """影子模式免費信號：正確回報 S2 擋掉的房有沒有 legacy 引導圖。純讀 entry，零 fal。"""
    guide = tmp_path / "guide.jpg"
    Image.new("RGB", (10, 10), "white").save(guide)
    # 有 legacy 引導圖 → legacy_guide=True（legacy 有東西可救）
    with_guide = {
        "_room_type": "living", "style": "modern",
        "_layout_guide": str(guide), "_layout_guide_mode": "s2_blocked_legacy",
        "_door_excluded": False,
    }
    sig = api._s2_shadow_free_signal(with_guide, "S2 候選未通過牆面貼合")
    assert sig["legacy_guide"] is True
    assert sig["room_type"] == "living"
    assert sig["style"] == "modern"
    assert sig["guide_mode"] == "s2_blocked_legacy"
    assert "牆面貼合" in sig["s2_reason"]
    # 沒有引導圖（門可見禁裸生 → legacy 也交不出）→ legacy_guide=False
    no_guide = {"_room_type": "living", "style": "modern", "_layout_guide": None}
    assert api._s2_shadow_free_signal(no_guide)["legacy_guide"] is False
    # 引導圖路徑指向不存在的檔 → 也算 False（不能用）
    ghost = {"_room_type": "living", "_layout_guide": str(tmp_path / "nope.jpg")}
    assert api._s2_shadow_free_signal(ghost)["legacy_guide"] is False


def test_s2_block_legacy_shadow_defaults_off_and_never_changes_delivery():
    """影子模式預設關；碼上保證只印 log、不改封鎖/交付（final 只 append preflight_blocked）。"""
    source = Path(api.__file__).read_text(encoding="utf-8")
    # 預設關：env 讀取有預設 "0"
    assert 'os.environ.get("S2_BLOCK_LEGACY_SHADOW", "0")' in source
    # 影子區塊在「append(preflight_blocked) + continue」之前，且包在 try/except
    shadow = source.index("S2_BLOCK_LEGACY_SHADOW")
    appended = source.index("final.append(preflight_blocked)", shadow)
    assert shadow < appended
    assert "影子模式例外（不影響封鎖）" in source

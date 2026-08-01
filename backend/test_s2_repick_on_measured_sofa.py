# -*- coding: utf-8 -*-
"""第一張畫完之後，用實測沙發寬度重挑一個容得下它的候選。

09B924C4：規劃叫模型在 64/1000 寬的框裡畫沙發，模型畫了 176——多出來的 112
往左溢出，正好蓋在大門上。四次修復在門邊來回震盪（門距 0→15→145→0），
因為每一次都拿**同一個過小的目標框**重畫。

24 張真單量到模型一律畫得比目標大（中位 2.93x）。事前不可預測
（單間 0.94–2.72 倍門寬），事後很穩（單內多次渲染只差 13%）——
這個不對稱就是「畫完再挑」可行、「事前用門寬推」不可行的原因
（後者實測會毀掉僅有的 3 張成功單）。
"""
from __future__ import annotations

import api
import pytest


SRC_W, SRC_H = 4032, 3024


def _quad(x0, x1, y0=1700.0, y1=1900.0):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _contract(chosen="near_door", candidates=None):
    """左牆有大門（x 480–1040）；near_door 貼著門，far 在對面。"""
    cands = candidates if candidates is not None else [
        {"candidate_id": "near_door", "eligible": True,
         "sofa_footprint_geometry_id": "s_near", "tv_footprint_geometry_id": "t_near"},
        {"candidate_id": "far_from_door", "eligible": True,
         "sofa_footprint_geometry_id": "s_far", "tv_footprint_geometry_id": "t_far"},
    ]
    return {
        "source": {"size": {"width": SRC_W, "height": SRC_H}},
        "decision": {"chosen_candidate_id": chosen},
        "candidates": cands,
        "geometry": [
            {"kind": "door_quad", "geometry_id": "door",
             "shape": {"coordinates": _quad(480, 1040, 900, 2100)}},
            # near_door：沙發貼著門那側；放大之後會壓到門
            {"geometry_id": "s_near", "shape": {"coordinates": _quad(1100, 1180)}},
            {"geometry_id": "t_near", "shape": {"coordinates": _quad(2600, 2680)}},
            # far_from_door：沙發在對面；電視櫃退到門後那段，放大後仍讓得開
            {"geometry_id": "s_far", "shape": {"coordinates": _quad(2500, 2580)}},
            {"geometry_id": "t_far", "shape": {"coordinates": _quad(1500, 1580)}},
        ],
    }


def _validation(sofa_width_1000):
    half = sofa_width_1000 / 2
    return {"render_bboxes": {"sofa": [400, 500 - half, 700, 500 + half]}}


# ── 沒有實測資料時什麼都不做 ─────────────────────────────────────
@pytest.mark.parametrize("bad", [None, {}, {"render_bboxes": {}},
                                 {"render_bboxes": {"sofa": [1, 2, 3]}},
                                 {"render_bboxes": {"sofa": "nope"}}])
def test_no_measurement_means_no_repick(bad):
    assert api._s2_candidate_for_measured_sofa(_contract(), bad) is None


def test_missing_door_geometry_means_no_repick():
    contract = _contract()
    contract["geometry"] = [g for g in contract["geometry"] if g.get("kind") != "door_quad"]
    assert api._s2_candidate_for_measured_sofa(contract, _validation(180)) is None


def test_broken_contract_never_raises():
    for bad in (None, {}, {"source": {}}, {"source": {"size": {"width": 0}}}):
        assert api._s2_candidate_for_measured_sofa(bad, _validation(180)) is None


# ── 量得到就要換到容得下的位置 ───────────────────────────────────
def test_repicks_away_from_the_door_when_the_sofa_is_wide():
    """實測沙發放大之後，貼門那個候選會壓到門，必須換到對面。"""
    picked = api._s2_candidate_for_measured_sofa(_contract("near_door"), _validation(180))
    assert picked == "far_from_door", picked


def test_returns_none_when_the_current_choice_is_already_best():
    """已經是最好的位置就不要亂動（回 None＝維持原候選）。"""
    assert api._s2_candidate_for_measured_sofa(
        _contract("far_from_door"), _validation(180)) is None


def test_never_picks_an_ineligible_candidate():
    contract = _contract("near_door", candidates=[
        {"candidate_id": "near_door", "eligible": True,
         "sofa_footprint_geometry_id": "s_near", "tv_footprint_geometry_id": "t_near"},
        {"candidate_id": "far_from_door", "eligible": False,
         "sofa_footprint_geometry_id": "s_far", "tv_footprint_geometry_id": "t_far"},
    ])
    assert api._s2_candidate_for_measured_sofa(contract, _validation(180)) is None


def test_no_candidate_clears_the_door_means_no_repick():
    """全部都過不了門距 → 回 None，不得挑一個「比較不爛」的來假裝安全。"""
    assert api._s2_candidate_for_measured_sofa(
        _contract("near_door"), _validation(950)) is None


def test_never_repicks_to_a_candidate_that_only_improves_but_still_fails():
    """『比較好』不等於『安全』。門距餘裕沒到 1.0 就不准換過去。

    ⚠️ 這條是蓄意破壞驗出來補的：上一版把門檻拿掉時沒有任何測試變紅，
    因為既有案例剛好「全部 margin 相同」，兩種寫法結果一樣。這裡刻意讓
    對面候選比原候選好、但仍然不合格。
    """
    contract = _contract("near_door")
    geo = {g["geometry_id"]: g for g in contract["geometry"] if "geometry_id" in g}
    # 放大後距門僅 ~70px，門寬 560、沙發門檻 0.25 → 餘裕約 0.5，優於原候選但不合格
    geo["s_far"]["shape"]["coordinates"] = _quad(1432, 1512)
    geo["t_far"]["shape"]["coordinates"] = _quad(2500, 2580)
    picked = api._s2_candidate_for_measured_sofa(contract, _validation(180))
    assert picked is None, f"換到了仍然不合格的候選：{picked}"


# ── 換算與縮放本身 ───────────────────────────────────────────────
def test_measured_width_is_converted_into_contract_pixels():
    assert api._measured_sofa_width_px(_validation(250), SRC_W) == pytest.approx(
        250 / 1000 * SRC_W)
    assert api._measured_sofa_width_px(_validation(250), 0) is None
    assert api._measured_sofa_width_px(None, SRC_W) is None


def test_scaling_keeps_the_centre_and_hits_the_target_width():
    poly = _quad(1000, 1080)
    scaled = api._scale_polygon_about_centre(poly, 400.0)
    xs = [p[0] for p in scaled]
    assert max(xs) - min(xs) == pytest.approx(400.0)
    assert sum(xs) / len(xs) == pytest.approx(sum(p[0] for p in poly) / len(poly))
    # 退化輸入不得炸掉
    assert api._scale_polygon_about_centre(_quad(500, 500), 100.0)


# ── 接線：修復引導圖真的會用重挑的結果 ──────────────────────────
def test_repair_guide_consults_the_repick():
    import inspect
    source = inspect.getsource(api._build_s2_sofa_repair_guide)
    assert "_s2_candidate_for_measured_sofa(contract, validation)" in source, \
        "修復引導圖沒有接上重挑，會繼續拿同一個過小的框重畫"
    # 明確傳入的 candidate_id 要優先於自動重挑
    assert "candidate_id\n" in source or "candidate_id or " in source


def test_repair_guide_falls_back_to_the_original_choice():
    """重挑回 None 時要用合約原本的 chosen，不能變成沒有候選。"""
    import inspect
    source = inspect.getsource(api._build_s2_sofa_repair_guide)
    assert 'or (contract.get("decision") or {}).get("chosen_candidate_id")' in source


# ── 給遮罩測試用的合約 fixture ─────────────────────────────────
def _write_contract(tmp_path, contract, previous_size=(4032, 3024)):
    from PIL import Image
    prev = tmp_path / "prev.png"
    Image.new("RGB", previous_size, "white").save(prev)
    cf = tmp_path / "contract.json"
    cf.write_text(__import__("json").dumps(contract), encoding="utf-8")
    return str(prev), str(cf)


def _contract_with_axis_and_landing(chosen="far_from_door"):
    c = _contract(chosen)
    for cand in c["candidates"]:
        cand["view_axis_geometry_id"] = "axis"
    c["geometry"] += [
        {"geometry_id": "axis", "shape": {"coordinates": [[1500.0, 1800.0], [2600.0, 1800.0]]}},
        {"kind": "entrance_landing", "geometry_id": "landing",
         "shape": {"coordinates": _quad(480, 1040, 2000, 2300)}},
    ]
    return c





# ── 引導圖與遮罩必須指向同一個候選 ──────────────────────────────
def _mask_inputs(tmp_path, contract):
    from PIL import Image
    prev = tmp_path / "prev.png"
    Image.new("RGB", (SRC_W, SRC_H), "white").save(prev)
    cf = tmp_path / "c.json"
    cf.write_text(__import__("json").dumps(contract), encoding="utf-8")
    return str(prev), str(cf)


def test_guide_and_mask_pick_the_candidate_with_the_same_expression():
    """8beef63 只改了引導圖：引導叫沙發搬去新位置，遮罩還開在舊位置。

    兩支吃同一份 contract 與 validation；只要挑候選的運算式一致，就必然一致。
    """
    import inspect
    guide = inspect.getsource(api._build_s2_sofa_repair_guide)
    mask = inspect.getsource(api._build_s2_sofa_edit_mask)
    for needle in ("_s2_candidate_for_measured_sofa(contract, validation)",
                   'or (contract.get("decision") or {}).get("chosen_candidate_id")'):
        assert needle in guide, f"引導圖少了 {needle}"
        assert needle in mask, f"遮罩少了 {needle}（會開在舊候選位置）"


def test_repick_actually_changes_the_mask(tmp_path):
    """行為測：重挑會不會真的把可編輯區換到新候選。

    比較「有別的候選可換」與「只有一個候選」兩份合約產出的遮罩——
    若重挑沒有影響遮罩，兩張圖會一模一樣。
    """
    from PIL import Image
    import numpy as np
    validation = _validation(180)
    validation["render_bboxes"]["entrance_door"] = [220, 120, 520, 260]

    def _mask(contract, name):
        prev = tmp_path / f"{name}_prev.png"
        Image.new("RGB", (SRC_W, SRC_H), "white").save(prev)
        cf = tmp_path / f"{name}.json"
        cf.write_text(__import__("json").dumps(contract), encoding="utf-8")
        out = api._build_s2_sofa_edit_mask(
            str(prev), str(cf), validation, str(tmp_path / f"{name}.png"))
        return np.asarray(Image.open(out).convert("L")) if out else None

    with_alt = _contract_with_axis_and_landing("near_door")
    only_one = _contract_with_axis_and_landing("near_door")
    only_one["candidates"] = [c for c in only_one["candidates"]
                              if c["candidate_id"] == "near_door"]
    assert api._s2_candidate_for_measured_sofa(with_alt, validation) == "far_from_door"
    assert api._s2_candidate_for_measured_sofa(only_one, validation) is None
    a, b = _mask(with_alt, "alt"), _mask(only_one, "one")
    # ⚠️ 不可以 skip：產出路徑被改壞時，測試會從紅燈變成跳過，防護就沒了。
    assert a is not None and b is not None, "遮罩沒產出——這本身就是回歸"
    assert not np.array_equal(a, b), "重挑之後遮罩完全沒變＝可編輯區仍開在舊位置"


def test_repick_actually_changes_the_guide_box(tmp_path):
    """引導圖的綠框也要跟著換位置。

    ⚠️ 這條是蓄意破壞驗出來補的：只拿掉引導圖那邊的 prefer_contract_target 時，
    原本沒有任何測試變紅——遮罩換了、引導圖還畫在舊位置，兩張圖各指一邊。
    """
    from PIL import Image
    import numpy as np
    validation = _validation(180)
    validation["render_bboxes"]["entrance_door"] = [220, 120, 520, 260]

    def _guide(contract, name):
        prev = tmp_path / f"g_{name}_prev.png"
        Image.new("RGB", (SRC_W, SRC_H), "white").save(prev)
        cf = tmp_path / f"g_{name}.json"
        cf.write_text(__import__("json").dumps(contract), encoding="utf-8")
        out = api._build_s2_sofa_repair_guide(
            str(prev), str(cf), str(tmp_path / f"g_{name}.jpg"), validation=validation)
        return np.asarray(Image.open(out).convert("RGB")) if out else None

    with_alt = _contract_with_axis_and_landing("near_door")
    only_one = _contract_with_axis_and_landing("near_door")
    only_one["candidates"] = [c for c in only_one["candidates"]
                              if c["candidate_id"] == "near_door"]
    a, b = _guide(with_alt, "alt"), _guide(only_one, "one")
    # ⚠️ 不可以 skip：理由同上。
    assert a is not None and b is not None, "引導圖沒產出——這本身就是回歸"
    assert not np.array_equal(a, b), "重挑之後綠框完全沒變＝引導仍畫在舊位置"

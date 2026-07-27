# -*- coding: utf-8 -*-
"""67866097 / EDD4856E：沙發不該被排到「跟大門同一面牆」。

該牆前段被大門佔掉（實測 t_start=0.35），沙發只能卡門後窄段——往門靠一點違規、
往深處推一點出客廳區，容錯只有幾十像素；三次局部修不是過頭就是不夠，四次全滅。
對面整面牆乾淨（t_start=0），放那邊這題根本不存在。

安全條件：分區有把握時尊重原值；對面牆不可用時不翻（硬翻只會讓 S2 無解）；
翻的時候必須改共用 ground truth，讓引導圖／S2／prompt／驗證四邊一致。
"""
import api


# 67866097 / EDD4856E 的實際牆段：左牆前 35% 被門佔掉、右牆整面乾淨
REAL_WALLS = {"usable_wall_segments": [
    {"id": "left_wall_post_door", "side": "left", "t_start": 0.35, "t_end": 1,
     "status": "observed", "confidence": "high", "visibility": "full"},
    {"id": "right_wall_full", "side": "right", "t_start": 0, "t_end": 1,
     "status": "observed", "confidence": "high", "visibility": "full"},
]}


def _zoning(sofa_side="left", confidence="", entrance="left"):
    return {
        "_origin": "user_confirmed_v2",
        "_sofa_layout": sofa_side,
        "_entrance_side": entrance,
        "furniture_placement_rules": {
            "sofa_side": sofa_side, "tv_side": "right",
            "sofa_side_confidence": confidence,
        },
    }


def test_real_case_sofa_moves_off_the_door_wall():
    """67866097：沙發原在左（門也在左）→ 改到乾淨的右牆，電視櫃換到左。"""
    z = _zoning()
    note = api._prefer_sofa_wall_without_entrance(z, REAL_WALLS)
    assert note, "應該要翻面"
    rules = z["furniture_placement_rules"]
    assert rules["sofa_side"] == "right"
    assert rules["tv_side"] == "left"
    assert z["_sofa_layout"] == "right", "共用 ground truth 必須一起更新"


def test_flip_is_visible_to_guide_s2_and_validation_alike():
    """關鍵安全性：翻面必須改『來源』，否則引導圖用右、驗證用左 → 每張都被判貼錯邊。"""
    z = _zoning()
    api._prefer_sofa_wall_without_entrance(z, REAL_WALLS)
    # 引導圖／S2 讀這個
    assert api._guide_sofa_side(z) == "right"
    # 驗證 layout_context 讀 furniture_placement_rules["sofa_side"]（api.py 共用 ground truth）
    assert z["furniture_placement_rules"]["sofa_side"] == "right"


def test_no_flip_when_zoning_is_confident():
    """分區有把握 → 尊重原值，不覆寫。"""
    z = _zoning(confidence="high")
    assert api._prefer_sofa_wall_without_entrance(z, REAL_WALLS) == ""
    assert z["furniture_placement_rules"]["sofa_side"] == "left"


def test_no_flip_when_sofa_already_away_from_door():
    """沙發本來就不在門那面 → 不動（避免無謂翻面）。"""
    z = _zoning(sofa_side="right", entrance="left")
    assert api._prefer_sofa_wall_without_entrance(z, REAL_WALLS) == ""
    assert z["furniture_placement_rules"]["sofa_side"] == "right"


def test_no_flip_when_opposite_wall_unusable():
    """對面牆不可用（整面窗／被切碎）→ 硬翻只會讓 S2 無解，維持原值。"""
    walls = {"usable_wall_segments": [
        {"side": "left", "t_start": 0.35, "t_end": 1, "status": "observed"},
        {"side": "right", "t_start": 0.8, "t_end": 1, "status": "observed"},  # 只剩 20%
    ]}
    z = _zoning()
    assert api._prefer_sofa_wall_without_entrance(z, walls) == ""
    assert z["furniture_placement_rules"]["sofa_side"] == "left"

    z2 = _zoning()
    assert api._prefer_sofa_wall_without_entrance(z2, None) == "", "沒有牆段資料就不猜"
    assert z2["furniture_placement_rules"]["sofa_side"] == "left"


def test_free_layout_is_untouched():
    """使用者選 free（自動佈局）→ 完全不介入。"""
    z = _zoning()
    z["_sofa_layout"] = "free"
    assert api._prefer_sofa_wall_without_entrance(z, REAL_WALLS) == ""
    assert api._guide_sofa_side(z) == "free"


def test_malformed_input_never_raises():
    for bad in (None, {}, {"furniture_placement_rules": None},
                {"furniture_placement_rules": {"sofa_side": "?"}}):
        assert api._prefer_sofa_wall_without_entrance(bad, REAL_WALLS) == ""


def test_wired_before_anything_reads_sofa_side():
    """必須在 _guide_sofa_side 之前呼叫，否則引導圖拿到的還是舊值。"""
    import inspect
    src = inspect.getsource(api.run_pipeline)
    assert "_prefer_sofa_wall_without_entrance(" in src
    assert (src.index("_prefer_sofa_wall_without_entrance(")
            < src.index("_sofa_side_for_guide = _guide_sofa_side("))

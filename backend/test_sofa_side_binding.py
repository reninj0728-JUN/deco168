"""
test_sofa_side_binding.py — 沙發左右邊 ground truth 綁定回歸測試 (2026-06-21).

不打 fal / Gemini。驗證「錯邊」根因修正：sofa_side 在 zoning 決定後，
flatten → prompt → validation 全程共用同一份，不再各自重猜。

  [A] flatten_zoning_v2_to_v1：proposed_zones.living_zone.sofa_side/tv_side
      正確帶進 furniture_placement_rules；方案 B 用 alt_*。
  [B] prompt_builder._build_layout_section：sofa_side 已知時輸出 BOUND SIDE，
      並把對側標為 FAILURE，不再出現「choose the left or right side」。
  [C] 重試 fix map + HIGH_SEVERITY 串接 sofa_on_wrong_side。
"""

import sys
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    assert cond, f"{name}: {detail}"


def _zoning_v2(sofa_side, tv_side, alt_sofa=None, alt_tv=None):
    return {
        "overall_confidence": "high",
        "spatial_synthesis": {"room_shape": "長型狹長客餐廳一體空間"},
        "existing_zones": {"walkway": {"where": "前段左側"}, "entrance_zone": {"where": "左前"}},
        "proposed_zones": {
            "living_zone": {
                "where": "靠窗那一端的深處區域",
                "rationale": "靠窗採光最好",
                "alt_option": "與餐廳對調",
                "sofa_side": sofa_side,
                "tv_side": tv_side,
                "sofa_side_confidence": "high",
                "sofa_side_reason": "右牆多個臥室門，沙發靠左牆才不擋門",
                "alt_sofa_side": alt_sofa or sofa_side,
                "alt_tv_side": alt_tv or tv_side,
            },
            "dining_zone": {"where": "中段"},
        },
    }


def case_a_flatten_carries_side():
    print("[case A] flatten 帶 sofa_side / tv_side（A 與 B）")
    import api
    v2 = _zoning_v2("left", "right", alt_sofa="right", alt_tv="left")

    za = api.flatten_zoning_v2_to_v1(v2, "A")
    ra = za["furniture_placement_rules"]
    _check("A: sofa_side=left", ra.get("sofa_side") == "left", ra)
    _check("A: tv_side=right", ra.get("tv_side") == "right", ra)
    _check("A: confidence 帶入", ra.get("sofa_side_confidence") == "high", ra)

    zb = api.flatten_zoning_v2_to_v1(v2, "B")
    rb = zb["furniture_placement_rules"]
    _check("B: alt sofa_side=right", rb.get("sofa_side") == "right", rb)
    _check("B: alt tv_side=left", rb.get("tv_side") == "left", rb)

    # tv_side 缺值 → 用 sofa_side 對面補
    v2b = _zoning_v2("left", "")
    zc = api.flatten_zoning_v2_to_v1(v2b, "A")
    _check("tv_side 缺值自動補對面", zc["furniture_placement_rules"].get("tv_side") == "right")


def case_b_prompt_binds_side():
    print("[case B] prompt 綁定 sofa_side，移除模型自選")
    import api
    from prompt_builder import _build_layout_section

    z_left = api.flatten_zoning_v2_to_v1(_zoning_v2("left", "right"), "A")
    s = _build_layout_section(z_left)
    _check("出現 BOUND SIDE", "BOUND SIDE" in s, s[:0])
    _check("綁左牆", "LEFT long side wall" in s, s[:0])
    _check("對側(右)標 FAILURE", "RIGHT side is a FAILURE" in s, s[:0])
    _check("不再出現模型自選句",
           "Choose the left or right side according to visible doors" not in s)

    z_right = api.flatten_zoning_v2_to_v1(_zoning_v2("right", "left"), "A")
    s2 = _build_layout_section(z_right)
    _check("綁右牆", "RIGHT long side wall" in s2, s2[:0])
    _check("對側(左)標 FAILURE", "LEFT side is a FAILURE" in s2, s2[:0])

    # sofa_side 未知時保留原本「模型自選」行為（向後相容）
    z_none = api.flatten_zoning_v2_to_v1(_zoning_v2("", ""), "A")
    s3 = _build_layout_section(z_none)
    _check("無 side 時退回模型自選",
           "Choose the left or right side according to visible doors" in s3, s3[:0])


def case_c_retry_and_severity_wired():
    print("[case C] sofa_on_wrong_side 串接重試 + 高嚴重度")
    import api
    from prompt_builder import _RETRY_FLAG_FIX_EN, _build_retry_context_section

    _check("HIGH_SEVERITY 含 wrong_side",
           "sofa_on_wrong_side" in api.HIGH_SEVERITY_FLAGS if hasattr(api, "HIGH_SEVERITY_FLAGS")
           else "sofa_on_wrong_side" in inspect.getsource(api.run_pipeline))
    _check("retry fix map 含 wrong_side", "sofa_on_wrong_side" in _RETRY_FLAG_FIX_EN)

    sec = _build_retry_context_section({"failed_flags": ["sofa_on_wrong_side"]})
    _check("重試帶錯邊修正", "WRONG side wall" in sec, sec)


def case_d_narrow_room_constraint():
    print("[case D] 窄房才加 NARROW-ROOM CONSTRAINT")
    import api
    from prompt_builder import _build_layout_section

    def _z(shape):
        v2 = _zoning_v2("left", "right")
        v2["spatial_synthesis"]["room_shape"] = shape
        return api.flatten_zoning_v2_to_v1(v2, "A")

    s_narrow = _build_layout_section(_z("長型窄深格局"))
    _check("窄房 → 有窄房限制", "NARROW-ROOM CONSTRAINT" in s_narrow, s_narrow[:0])
    _check("窄房限制含 80cm 走道 + 淺沙發",
           "80 cm" in s_narrow and "2-seater" in s_narrow, s_narrow[:0])
    _check("禁 L 型/貴妃", "L-shape" in s_narrow and "chaise" in s_narrow, s_narrow[:0])

    s_wide = _build_layout_section(_z("方正客廳格局"))
    _check("非窄房 → 不加窄房限制", "NARROW-ROOM CONSTRAINT" not in s_wide)


if __name__ == "__main__":
    case_a_flatten_carries_side()
    case_b_prompt_binds_side()
    case_c_retry_and_severity_wired()
    case_d_narrow_room_constraint()
    print("\nALL PASS")

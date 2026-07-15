# -*- coding: utf-8 -*-
"""Phase 0 離線原型 v3: 格局契約——牆腳基準線 + A/B 候選硬閘門。

不接管線、不呼叫生圖。

【座標系】影像 2D 像素平面，不假裝完整 3D。
- 左／右牆腳線 = 近端 → 深端
- 家具框 = 沿牆腳線的透視梯形，深度參數 t∈[0,1]（0=近端，1=深端）
- 正對 = 沙發與 TV 共用同一深度帶 shared_depth_t
- fail closed：硬閘門全過才 safe_layout

【A/B 候選｜有側門客廳】
- A 厚沙發放完整實牆，薄 TV 放門牆門後段（優先）
- B 沙發放門牆門後段，TV 放完整實牆（僅在門禁／落腳區仍完整時可用）

商業裁決（2879173D）：厚沙發即使過門框，仍常吃掉落腳區 → A 優先，B 必須證明門禁清空。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path


# 共用深度帶：同 t 才叫正對（可微調，但 A/B 必須共用）
SHARED_DEPTH_T = (0.28, 0.58)
SOFA_T_SPAN = SHARED_DEPTH_T
TV_T_SPAN = (0.30, 0.56)
# 門牆薄櫃可再往深推一點，避開門禁
TV_T_SPAN_ON_DOOR_WALL = (0.36, 0.62)


def _bbox_px(bbox1000, W, H):
    if not bbox1000 or len(bbox1000) != 4:
        return None
    ymin, xmin, ymax, xmax = [float(v) for v in bbox1000]
    return (int(xmin / 1000 * W), int(ymin / 1000 * H),
            int(xmax / 1000 * W), int(ymax / 1000 * H))


def _lerp(p0, p1, t):
    return (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)


def _wall_quad(p0, p1, t0, t1, h_near, W, H):
    """沿地腳線 p0→p1 的透視梯形：底邊=線段[t0,t1]，高按深度線性縮。"""
    a, b = _lerp(p0, p1, t0), _lerp(p0, p1, t1)
    scale0, scale1 = 1.0 - 0.72 * t0, 1.0 - 0.72 * t1
    qa = (a[0], a[1] - h_near * scale0)
    qb = (b[0], b[1] - h_near * scale1)
    quad = [(int(a[0]), int(a[1])), (int(b[0]), int(b[1])),
            (int(qb[0]), int(qb[1])), (int(qa[0]), int(qa[1]))]
    return [(max(0, min(W - 1, x)), max(0, min(H - 1, y))) for x, y in quad]


def _quad_bbox(q):
    xs = [p[0] for p in q]
    ys = [p[1] for p in q]
    return (min(xs), min(ys), max(xs), max(ys))


def _rects_intersect(a, b):
    if not a or not b:
        return False
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _pt(kp, W, H):
    """keypoint [y,x]/1000 → (x_px, y_px)。"""
    if not kp or len(kp) != 2:
        return None
    return (kp[1] / 1000.0 * W, kp[0] / 1000.0 * H)


def _line_at_x(p0, p1, x):
    """牆腳線上 x 處的點（線性內插，允許外插少量）。"""
    if abs(p1[0] - p0[0]) < 1e-6:
        return (x, p0[1])
    t = (x - p0[0]) / (p1[0] - p0[0])
    return (x, p0[1] + (p1[1] - p0[1]) * t)


def _t_at_x(p0, p1, x):
    """把影像 x 投到基準線參數 t。"""
    if abs(p1[0] - p0[0]) < 1e-6:
        return 0.0
    return (x - p0[0]) / (p1[0] - p0[0])


def _clamp_t_span(t0, t1, lo=0.02, hi=0.98, min_span=0.12):
    t0 = max(lo, min(hi, t0))
    t1 = max(lo, min(hi, t1))
    if t1 - t0 < min_span:
        t1 = min(hi, t0 + min_span)
        if t1 - t0 < min_span:
            t0 = max(lo, t1 - min_span)
    return (t0, t1)


def _door_clear_zone(door, door_side, W, H):
    """門禁落腳區：門旁近端地面，不是整條縱深走廊。

    透視圖中門後深牆的 x 會落在門框右側；若 clear 全高全深，會誤殺深段 TV。
    """
    if not door:
        return None
    x0, y0, x1, y1 = door
    door_w = max(1, x1 - x0)
    pad_x = max(int(door_w * 0.10), int(W * 0.012))
    into = int(door_w * 0.35)
    # 只取門下半到畫面底部的近端落腳帶
    y_land0 = max(int((y0 + y1) * 0.55), int(H * 0.58))
    if door_side == "left":
        clear = (
            max(0, x0 - pad_x),
            y_land0,
            min(W, x1 + into),
            H,
        )
    elif door_side == "right":
        clear = (
            max(0, x0 - into),
            y_land0,
            min(W, x1 + pad_x),
            H,
        )
    else:
        clear = (
            max(0, x0 - pad_x),
            y_land0,
            min(W, x1 + pad_x),
            H,
        )
    return clear


def _door_wall_t_span(p0, p1, door, door_side, margin_t=0.06, length_t=0.22):
    """門牆家具的深度帶：門內緣投到基準線後，再往深處走。"""
    if not door or door_side not in ("left", "right"):
        return SHARED_DEPTH_T
    edge_x = door[2] if door_side == "left" else door[0]
    t_door = _t_at_x(p0, p1, edge_x)
    # 門牆可用段在「過門之後」；若 t_door 已接近 1，表示這張角度門後牆幾乎不可用
    t0 = t_door + margin_t
    t1 = t0 + length_t
    return _clamp_t_span(t0, t1)


def _match_depth_span(ref_span, length=None):
    """讓對面牆家具對齊同一深度中心。"""
    r0, r1 = ref_span
    length = length if length is not None else (r1 - r0)
    mid = (r0 + r1) / 2.0
    return _clamp_t_span(mid - length / 2.0, mid + length / 2.0)


def _walkway_core(walkway):
    if not walkway:
        return None
    w = walkway[2] - walkway[0]
    return (
        walkway[0] + int(w * 0.18),
        walkway[1],
        walkway[2] - int(w * 0.18),
        walkway[3],
    )


def _foot_mid(q):
    return ((q[0][0] + q[1][0]) / 2.0, (q[0][1] + q[1][1]) / 2.0)


def _depth_center(t0, t1):
    return (t0 + t1) / 2.0


def _foot_bbox(q, pad=8):
    """只取地面邊（quad 前兩點）做門禁碰撞，避免家具立面高度誤撞。"""
    if not q or len(q) < 2:
        return None
    xs = [q[0][0], q[1][0]]
    ys = [q[0][1], q[1][1]]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _evaluate_candidate(
    *,
    cid: str,
    sofa_quad,
    tv_quad,
    sofa_t,
    tv_t,
    sofa_side: str,
    tv_side: str,
    door,
    door_side,
    door_clear,
    walkway,
    walk_core,
    notes_prefix="",
) -> dict:
    inv = {}
    fail = []
    sofa_bb = _quad_bbox(sofa_quad) if sofa_quad else None
    tv_bb = _quad_bbox(tv_quad) if tv_quad else None
    sofa_foot = _foot_bbox(sofa_quad) if sofa_quad else None
    tv_foot = _foot_bbox(tv_quad) if tv_quad else None

    inv["has_sofa_quad"] = bool(sofa_quad)
    inv["has_tv_quad"] = bool(tv_quad)
    if not inv["has_sofa_quad"]:
        fail.append("missing_sofa_quad")
    if not inv["has_tv_quad"]:
        fail.append("missing_tv_quad")

    if door_clear and sofa_foot:
        inv["sofa_clear_of_door_landing"] = not _rects_intersect(sofa_foot, door_clear)
        if not inv["sofa_clear_of_door_landing"]:
            fail.append("sofa_hits_door_landing")
    else:
        inv["sofa_clear_of_door_landing"] = door_clear is None

    if door_clear and tv_foot:
        inv["tv_clear_of_door_landing"] = not _rects_intersect(tv_foot, door_clear)
        if not inv["tv_clear_of_door_landing"]:
            fail.append("tv_hits_door_landing")
    else:
        inv["tv_clear_of_door_landing"] = door_clear is None

    if door and sofa_bb:
        inv["sofa_clear_of_door_bbox"] = not _rects_intersect(sofa_bb, door)
        if not inv["sofa_clear_of_door_bbox"]:
            fail.append("sofa_hits_door_bbox")
    else:
        inv["sofa_clear_of_door_bbox"] = True

    if door and tv_bb:
        inv["tv_clear_of_door_bbox"] = not _rects_intersect(tv_bb, door)
        if not inv["tv_clear_of_door_bbox"]:
            fail.append("tv_hits_door_bbox")
    else:
        inv["tv_clear_of_door_bbox"] = True

    # 門牆家具必須過門框內緣（用門框，不用放大後的 landing AABB）
    if door_side == "left" and door:
        edge = door[2]
        if sofa_side == "left" and sofa_bb:
            inv["sofa_starts_past_door_clear"] = sofa_bb[0] >= edge - 2
            if not inv["sofa_starts_past_door_clear"]:
                fail.append("sofa_not_past_door_frame")
        else:
            inv["sofa_starts_past_door_clear"] = True
        if tv_side == "left" and tv_bb:
            inv["tv_starts_past_door_clear"] = tv_bb[0] >= edge - 2
            if not inv["tv_starts_past_door_clear"]:
                fail.append("tv_not_past_door_frame")
        else:
            inv["tv_starts_past_door_clear"] = True
    elif door_side == "right" and door:
        edge = door[0]
        if sofa_side == "right" and sofa_bb:
            inv["sofa_starts_past_door_clear"] = sofa_bb[2] <= edge + 2
            if not inv["sofa_starts_past_door_clear"]:
                fail.append("sofa_not_past_door_frame")
        else:
            inv["sofa_starts_past_door_clear"] = True
        if tv_side == "right" and tv_bb:
            inv["tv_starts_past_door_clear"] = tv_bb[2] <= edge + 2
            if not inv["tv_starts_past_door_clear"]:
                fail.append("tv_not_past_door_frame")
        else:
            inv["tv_starts_past_door_clear"] = True
    else:
        inv["sofa_starts_past_door_clear"] = True
        inv["tv_starts_past_door_clear"] = True

    # 視線規則
    if sofa_side == "float":
        facing = "left" if tv_side == "left" else "right" if tv_side == "right" else "left"
        inv["door_behind_sofa"] = True
        inv["sofa_faces_tv_wall"] = True
        inv["thick_sofa_not_on_door_wall"] = True  # 浮置不算門牆厚沙發
    elif door_side in ("left", "right") and sofa_side in ("left", "right"):
        facing = "left" if sofa_side == "right" else "right"
        if sofa_side == door_side:
            inv["door_behind_sofa"] = facing != door_side
            if not inv["door_behind_sofa"]:
                fail.append("sofa_faces_door_side")
        else:
            inv["door_behind_sofa"] = True
            inv["sofa_faces_tv_wall"] = facing == door_side or facing == tv_side
    else:
        inv["door_behind_sofa"] = True
        facing = "left" if sofa_side == "right" else "right"

    # 走道核心：foot mid 不可落入
    if walk_core and sofa_quad:
        fx, fy = _foot_mid(sofa_quad)
        inv["sofa_foot_outside_walkway"] = not (
            walk_core[0] <= fx <= walk_core[2] and walk_core[1] <= fy <= walk_core[3]
        )
        if not inv["sofa_foot_outside_walkway"]:
            fail.append("sofa_in_walkway_core")
    else:
        inv["sofa_foot_outside_walkway"] = True

    if walk_core and tv_quad:
        fx, fy = _foot_mid(tv_quad)
        inv["tv_foot_outside_walkway"] = not (
            walk_core[0] <= fx <= walk_core[2] and walk_core[1] <= fy <= walk_core[3]
        )
        if not inv["tv_foot_outside_walkway"]:
            fail.append("tv_in_walkway_core")
    else:
        inv["tv_foot_outside_walkway"] = True

    # 同深度正對
    sc = _depth_center(*sofa_t)
    tc = _depth_center(*tv_t)
    depth_delta = abs(sc - tc)
    inv["shared_depth_alignment"] = depth_delta <= 0.10
    if not inv["shared_depth_alignment"]:
        fail.append(f"depth_delta_{depth_delta:.3f}")

    # 厚沙發不應與門同側（商業硬偏好；B 用 score 懲罰）
    thick_sofa_on_door = (
        sofa_side in ("left", "right")
        and sofa_side == door_side
        and door_side in ("left", "right")
    )
    inv["thick_sofa_not_on_door_wall"] = not thick_sofa_on_door if sofa_side != "float" else True
    penalty = 0
    if thick_sofa_on_door:
        penalty += 2
    if fail:
        penalty += 10 * len(fail)

    if sofa_side == "float":
        pass_ok = all(v for k, v in inv.items())
    elif thick_sofa_on_door:
        pass_ok = all(v for k, v in inv.items() if k != "thick_sofa_not_on_door_wall")
    else:
        pass_ok = all(inv.values())

    score = 100 - penalty
    if inv.get("thick_sofa_not_on_door_wall") and sofa_side != "float":
        score += 5  # A 偏好
    if sofa_side == "float" and not fail:
        score += 0  # 基線；外層 can_float 再加分

    return {
        "id": cid,
        "sofa_side": sofa_side,
        "tv_side": tv_side,
        "sofa_facing": facing if sofa_side else None,
        "sofa_quad": sofa_quad,
        "tv_quad": tv_quad,
        "sofa_t": list(sofa_t),
        "tv_t": list(tv_t),
        "depth_center_sofa": sc,
        "depth_center_tv": tc,
        "depth_delta": depth_delta,
        "invariants": inv,
        "fail_reasons": fail,
        "pass": bool(pass_ok and not fail),
        "score": score,
        "notes": notes_prefix,
    }


def _float_sofa_quad(left_near, left_deep, right_near, right_deep,
                     t0, t1, facing: str, W: int, H: int,
                     wall_inset=0.30, width_frac=0.34, h_near=None):
    """不靠牆沙發：落在左右牆腳線之間的地面帶，背對非 TV 側、面向 TV。

    facing=left → 看左牆 TV，沙發偏右半場但離右牆仍有 inset
    facing=right → 看右牆 TV，沙發偏左半場
    """
    h_near = h_near if h_near is not None else int(H * 0.22)
    l0, l1 = _lerp(left_near, left_deep, t0), _lerp(left_near, left_deep, t1)
    r0, r1 = _lerp(right_near, right_deep, t0), _lerp(right_near, right_deep, t1)

    def mix(a, b, u):
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)

    # 左右牆之間可用橫向參數 u∈[0,1]
    if facing == "left":
        # 面向左：沙發本體在右側場，u 偏大
        u0, u1 = 0.50, 0.50 + width_frac
    else:
        u0, u1 = 0.50 - width_frac, 0.50
    u0 = max(wall_inset, min(1.0 - wall_inset - 0.05, u0))
    u1 = max(u0 + 0.12, min(1.0 - wall_inset, u1))

    fl0, fr0 = mix(l0, r0, u0), mix(l0, r0, u1)  # near depth edge
    fl1, fr1 = mix(l1, r1, u0), mix(l1, r1, u1)  # deep depth edge
    # 地面四邊形：近左、近右、深右、深左（與 wall_quad 點序一致概念）
    floor = [fl0, fr0, fr1, fl1]
    # 立面高度：從地面邊往上
    scale0, scale1 = 1.0 - 0.55 * t0, 1.0 - 0.55 * t1
    top0 = (fl0[0], fl0[1] - h_near * scale0)
    top1 = (fr0[0], fr0[1] - h_near * scale0)
    top2 = (fr1[0], fr1[1] - h_near * scale1)
    top3 = (fl1[0], fl1[1] - h_near * scale1)
    # 對外仍回「視覺框」：地面近邊 + 上緣（便於畫 guide）
    quad = [
        (int(fl0[0]), int(fl0[1])),
        (int(fr0[0]), int(fr0[1])),
        (int(top1[0]), int(top1[1])),
        (int(top0[0]), int(top0[1])),
    ]
    # 更完整的地面 footprint 存在 notes 用；驗證仍用 foot=前兩點
    return [(max(0, min(W - 1, x)), max(0, min(H - 1, y))) for x, y in quad]


def build_layout_contract(zoning_v2: dict, W: int, H: int,
                          struct_keypoints: dict | None = None,
                          sofa_mode: str = "free",
                          can_float: bool = False) -> dict:
    """sofa_mode: left | right | free
    - left/right：只保留沙發在該側的靠牆候選
    - free：AI 自動，評估 A/B（靠牆）+ 可選 F（浮置）
    can_float：空間夠大才允許 F 候選進入比較
    """
    mode = str(sofa_mode or "free").strip().lower()
    if mode not in ("left", "right", "free"):
        mode = "free"
    ez = (zoning_v2.get("existing_zones") or {})
    door = _bbox_px((ez.get("entrance_zone") or {}).get("bbox_on_best_photo"), W, H)
    walkway = _bbox_px((ez.get("walkway") or {}).get("bbox_on_best_photo"), W, H)
    c = {
        "coord_system": "image_2d_px_baseline",
        "image_size": [W, H],
        "sofa_mode": mode,
        "can_float": bool(can_float),
        "door_bbox": door,
        "door_side": None,
        "door_clear": None,
        "walkway": walkway,
        "walkway_core": None,
        "baseline_left": None,
        "baseline_right": None,
        "template": None,
        "sofa_quad": None,
        "tv_quad": None,
        "sofa_facing": None,
        "candidates": [],
        "chosen": None,
        "invariants": {},
        "safe_layout": False,
        "notes": [],
    }

    if not walkway:
        c["notes"].append("無走道 bbox → 無法建基準線 → 保守模板")
        c["template"] = "CONSERVATIVE_no_geometry"
        c["disposition"] = "NEEDS_ALTERNATE_LIVING_ANGLE"
        return c

    kp = struct_keypoints or {}
    ln_l = (_pt(kp.get("left_baseboard_near"), W, H), _pt(kp.get("left_baseboard_deep"), W, H))
    ln_r = (_pt(kp.get("right_baseboard_near"), W, H), _pt(kp.get("right_baseboard_deep"), W, H))
    vp_hint = _pt(kp.get("vanishing_hint"), W, H)
    has_kp = all(ln_l) and all(ln_r)
    vp = vp_hint or ((walkway[0] + walkway[2]) / 2.0, float(walkway[1]))

    def _toward_vp(near, frac=0.75):
        return (near[0] + (vp[0] - near[0]) * frac,
                near[1] + (vp[1] - near[1]) * frac)

    if door:
        dcx = (door[0] + door[2]) / 2
        c["door_side"] = "left" if dcx < W * 0.35 else (
            "right" if dcx > W * 0.65 else "center")

    door_side = c["door_side"]
    door_clear = _door_clear_zone(door, door_side, W, H)
    c["door_clear"] = door_clear
    walk_core = _walkway_core(walkway)
    c["walkway_core"] = walk_core

    if has_kp:
        left_near, left_deep = ln_l
        right_near, right_deep = ln_r
    else:
        if door_side == "left" and door:
            left_near = (door[2], min(door[3], int(H * 0.96)))
            right_near = (int(W * 0.99), int(H * 0.94))
        elif door_side == "right" and door:
            left_near = (int(W * 0.01), int(H * 0.94))
            right_near = (door[0], min(door[3], int(H * 0.96)))
        else:
            left_near = (int(W * 0.01), int(H * 0.94))
            right_near = (int(W * 0.99), int(H * 0.94))
        left_deep = _toward_vp(left_near)
        right_deep = _toward_vp(right_near)

    c["baseline_left"] = [list(left_near), list(left_deep)]
    c["baseline_right"] = [list(right_near), list(right_deep)]
    c["notes"].append(
        "baseline_source=" + ("struct_keypoints" if has_kp else "walkway_vp_fallback")
    )
    c["notes"].append(f"sofa_mode={mode} can_float={bool(can_float)}")

    h_near = int(H * 0.24)
    h_tv = int(h_near * 0.66)
    candidates = []

    def _keep_sofa_side(side: str) -> bool:
        if mode == "free":
            return True
        return side == mode

    if door_side in ("left", "right"):
        solid = "right" if door_side == "left" else "left"
        if door_side == "left":
            door_wall_span = _door_wall_t_span(left_near, left_deep, door, door_side)
        else:
            door_wall_span = _door_wall_t_span(right_near, right_deep, door, door_side)
        solid_span = _match_depth_span(door_wall_span, length=0.28)
        c["notes"].append(
            f"door_wall_t={door_wall_span[0]:.3f}-{door_wall_span[1]:.3f} "
            f"solid_t={solid_span[0]:.3f}-{solid_span[1]:.3f}"
        )
        if door_wall_span[1] - door_wall_span[0] < 0.12 or door_wall_span[0] > 0.90:
            c["notes"].append("door_wall_segment_too_short")

        # A 厚沙發完整實牆
        if solid == "right":
            sofa_a = _wall_quad(right_near, right_deep, *solid_span, h_near, W, H)
            tv_a = _wall_quad(left_near, left_deep, *door_wall_span, h_tv, W, H)
            sofa_side_a, tv_side_a = "right", "left"
        else:
            sofa_a = _wall_quad(left_near, left_deep, *solid_span, h_near, W, H)
            tv_a = _wall_quad(right_near, right_deep, *door_wall_span, h_tv, W, H)
            sofa_side_a, tv_side_a = "left", "right"
        if _keep_sofa_side(sofa_side_a):
            candidates.append(_evaluate_candidate(
                cid="A_sofa_solid_tv_doorwall_past_door",
                sofa_quad=sofa_a, tv_quad=tv_a,
                sofa_t=solid_span, tv_t=door_wall_span,
                sofa_side=sofa_side_a, tv_side=tv_side_a,
                door=door, door_side=door_side, door_clear=door_clear,
                walkway=walkway, walk_core=walk_core,
                notes_prefix="bound: thick sofa on solid wall",
            ))

        # B 沙發門牆門後
        if door_side == "left":
            sofa_b = _wall_quad(left_near, left_deep, *door_wall_span, h_near, W, H)
            tv_b = _wall_quad(right_near, right_deep, *solid_span, h_tv, W, H)
            sofa_side_b, tv_side_b = "left", "right"
        else:
            sofa_b = _wall_quad(right_near, right_deep, *door_wall_span, h_near, W, H)
            tv_b = _wall_quad(left_near, left_deep, *solid_span, h_tv, W, H)
            sofa_side_b, tv_side_b = "right", "left"
        if _keep_sofa_side(sofa_side_b):
            candidates.append(_evaluate_candidate(
                cid="B_sofa_doorwall_past_door_tv_solid",
                sofa_quad=sofa_b, tv_quad=tv_b,
                sofa_t=door_wall_span, tv_t=solid_span,
                sofa_side=sofa_side_b, tv_side=tv_side_b,
                door=door, door_side=door_side, door_clear=door_clear,
                walkway=walkway, walk_core=walk_core,
                notes_prefix="bound: sofa on door wall past door",
            ))

        # F 浮置：僅 free 模式且 can_float
        if mode == "free" and can_float:
            # TV 放完整實牆；沙發浮在場中面向 TV
            float_span = _match_depth_span(solid_span if solid_span else SHARED_DEPTH_T, length=0.26)
            if solid == "right":
                # TV 右？ 等等 solid 是完整牆給沙發偏好的對面...
                # A 定義 solid = opposite of door = 完整牆 for sofa
                # 浮置時 TV 仍放完整實牆更穩，沙發浮置面向它
                tv_f = _wall_quad(right_near, right_deep, *float_span, h_tv, W, H)
                facing_f = "right"
                tv_side_f = "right"
            else:
                tv_f = _wall_quad(left_near, left_deep, *float_span, h_tv, W, H)
                facing_f = "left"
                tv_side_f = "left"
            sofa_f = _float_sofa_quad(
                left_near, left_deep, right_near, right_deep,
                *float_span, facing=facing_f, W=W, H=H, h_near=int(h_near * 0.92),
            )
            # sofa_side 標記 float
            cand_f = _evaluate_candidate(
                cid="F_float_sofa_tv_solid_wall",
                sofa_quad=sofa_f, tv_quad=tv_f,
                sofa_t=float_span, tv_t=float_span,
                sofa_side="float", tv_side=tv_side_f,
                door=door, door_side=door_side, door_clear=door_clear,
                walkway=walkway, walk_core=walk_core,
                notes_prefix="float: sofa free-standing, TV on solid wall",
            )
            # 浮置額外：不得標 thick_sofa_on_door 罰則誤傷；加分若過關
            if cand_f["pass"]:
                cand_f["score"] += 3
            candidates.append(cand_f)
    else:
        # 無側門
        sofa_a = _wall_quad(right_near, right_deep, *SOFA_T_SPAN, h_near, W, H)
        tv_a = _wall_quad(left_near, left_deep, *TV_T_SPAN, h_tv, W, H)
        if _keep_sofa_side("right"):
            candidates.append(_evaluate_candidate(
                cid="A_classic_no_side_door",
                sofa_quad=sofa_a, tv_quad=tv_a,
                sofa_t=SOFA_T_SPAN, tv_t=TV_T_SPAN,
                sofa_side="right", tv_side="left",
                door=door, door_side=door_side, door_clear=door_clear,
                walkway=walkway, walk_core=walk_core,
            ))
        sofa_l = _wall_quad(left_near, left_deep, *SOFA_T_SPAN, h_near, W, H)
        tv_r = _wall_quad(right_near, right_deep, *TV_T_SPAN, h_tv, W, H)
        if _keep_sofa_side("left"):
            candidates.append(_evaluate_candidate(
                cid="A_classic_sofa_left",
                sofa_quad=sofa_l, tv_quad=tv_r,
                sofa_t=SOFA_T_SPAN, tv_t=TV_T_SPAN,
                sofa_side="left", tv_side="right",
                door=door, door_side=door_side, door_clear=door_clear,
                walkway=walkway, walk_core=walk_core,
            ))
        if mode == "free" and can_float:
            float_span = SHARED_DEPTH_T
            sofa_f = _float_sofa_quad(
                left_near, left_deep, right_near, right_deep,
                *float_span, facing="left", W=W, H=H, h_near=int(h_near * 0.92),
            )
            tv_f = _wall_quad(left_near, left_deep, *float_span, h_tv, W, H)
            candidates.append(_evaluate_candidate(
                cid="F_float_sofa_tv_left",
                sofa_quad=sofa_f, tv_quad=tv_f,
                sofa_t=float_span, tv_t=float_span,
                sofa_side="float", tv_side="left",
                door=door, door_side=door_side, door_clear=door_clear,
                walkway=walkway, walk_core=walk_core,
                notes_prefix="float classic",
            ))

    c["candidates"] = candidates
    if not candidates:
        c["notes"].append("no_candidates_for_sofa_mode")
        c["disposition"] = "UNSAFE_LAYOUT_CONTRACT"
        return c

    passed = [x for x in candidates if x["pass"]]
    if passed:
        chosen = max(passed, key=lambda x: x["score"])
        c["safe_layout"] = True
    else:
        chosen = max(candidates, key=lambda x: x["score"])
        c["safe_layout"] = False

    c["chosen"] = chosen["id"]
    c["template"] = chosen["id"]
    c["sofa_quad"] = chosen["sofa_quad"]
    c["tv_quad"] = chosen["tv_quad"]
    c["sofa_facing"] = chosen["sofa_facing"]
    c["invariants"] = chosen["invariants"]
    c["notes"].append(f"chosen={chosen['id']} pass={chosen['pass']} score={chosen['score']}")
    if chosen["fail_reasons"]:
        c["notes"].append("fail=" + ",".join(chosen["fail_reasons"]))
    return c


def crop_transform_for_contract(c: dict) -> dict:
    W, H = c["image_size"]
    # 有側門客廳：優先全幅 3:2，保留門與深處門後牆段；
    # 舊版先砍門／砍上緣，會把 t>0.8 的深段家具裁到不可用。
    if c.get("door_side") in ("left", "right"):
        target_ar = 1.5
        ar = W / max(1, H)
        if ar > target_ar + 0.001:
            need_w = int(round(H * target_ar))
            cx = W // 2
            x0 = max(0, min(cx - need_w // 2, W - need_w))
            x1 = x0 + need_w
            y0, y1 = 0, H
        elif ar < target_ar - 0.001:
            need_h = int(round(W / target_ar))
            # 偏下保留地面／門腳，少裁家具落點
            y0 = max(0, H - need_h)
            y1 = y0 + need_h
            x0, x1 = 0, W
        else:
            x0, y0, x1, y1 = 0, 0, W, H
    else:
        x0, y0, x1, y1 = 0, int(H * 0.10), W, H
        cw, ch = x1 - x0, y1 - y0
        target_ar = 1.5
        ar = cw / max(1, ch)
        if ar > target_ar:
            need_w = int(round(ch * target_ar))
            cx = (x0 + x1) // 2
            x0 = max(x0, cx - need_w // 2)
            x1 = x0 + need_w
        elif ar < target_ar:
            need_h = int(round(cw / target_ar))
            trim = ch - need_h
            y0 = y0 + int(trim * 0.25)
            y1 = y0 + need_h

    def to_crop_quad(q):
        if not q:
            return None
        return [(max(0, min(x1 - x0, px - x0)), max(0, min(y1 - y0, py - y0)))
                for px, py in q]

    out = {
        "crop_box": (x0, y0, x1, y1),
        "sofa_quad_crop": to_crop_quad(c.get("sofa_quad")),
        "tv_quad_crop": to_crop_quad(c.get("tv_quad")),
        "walkway_crop": None,
        "door_clear_crop": None,
        "door_in_crop": _rects_intersect(c["door_bbox"], (x0, y0, x1, y1)) if c.get("door_bbox") else False,
    }
    if c.get("walkway"):
        wk = c["walkway"]
        out["walkway_crop"] = (
            max(0, wk[0] - x0), max(0, wk[1] - y0),
            min(x1 - x0, wk[2] - x0), min(y1 - y0, wk[3] - y0),
        )
    if c.get("door_clear"):
        dc = c["door_clear"]
        out["door_clear_crop"] = (
            max(0, dc[0] - x0), max(0, dc[1] - y0),
            min(x1 - x0, dc[2] - x0), min(y1 - y0, dc[3] - y0),
        )
    sq = out["sofa_quad_crop"]
    if sq:
        xs = [p[0] for p in sq]
        out["sofa_box_survives_crop"] = (max(xs) - min(xs)) > (x1 - x0) * 0.06
    else:
        out["sofa_box_survives_crop"] = False
    return out


def _guide_crop_invariant(quad, crop_box, *, min_width_ratio=0.06,
                          min_height_ratio=0.10, min_retained_area=0.75):
    """裁切後引導框必須完整，且大到足以讓生圖模型辨識。"""
    if not quad:
        return False
    qx0, qy0, qx1, qy1 = _quad_bbox(quad)
    cx0, cy0, cx1, cy1 = crop_box
    iw = max(0, min(qx1, cx1) - max(qx0, cx0))
    ih = max(0, min(qy1, cy1) - max(qy0, cy0))
    original_area = max(1, (qx1 - qx0) * (qy1 - qy0))
    retained = (iw * ih) / original_area
    crop_w, crop_h = max(1, cx1 - cx0), max(1, cy1 - cy0)
    return (
        retained >= min_retained_area
        and iw / crop_w >= min_width_ratio
        and ih / crop_h >= min_height_ratio
    )


def build_contract_with_crop(zoning_v2: dict, W: int, H: int,
                             struct_keypoints: dict | None = None,
                             sofa_mode: str = "free",
                             can_float: bool = False) -> dict:
    """Phase 0 單一入口｜原圖契約與裁切可用性一起決定 safe_layout。"""
    contract = build_layout_contract(
        zoning_v2, W, H,
        struct_keypoints=struct_keypoints,
        sofa_mode=sofa_mode,
        can_float=can_float,
    )
    crop = crop_transform_for_contract(contract)
    x0, y0, x1, y1 = crop["crop_box"]
    crop_invariants = {
        "crop_is_exact_3_2": abs(((x1 - x0) / max(1, y1 - y0)) - 1.5) <= 0.01,
        # 有門客廳：門應留在裁切內，才能驗門禁
        "door_remains_in_crop_if_side_entrance": (
            True if contract.get("door_side") not in ("left", "right")
            else bool(crop.get("door_in_crop"))
        ),
        "sofa_guide_survives_crop": _guide_crop_invariant(
            contract.get("sofa_quad"), crop["crop_box"]
        ),
        "tv_guide_survives_crop": _guide_crop_invariant(
            contract.get("tv_quad"), crop["crop_box"],
            min_width_ratio=0.04, min_height_ratio=0.05,
        ),
    }
    contract["crop"] = crop
    contract["crop_invariants"] = crop_invariants
    base_ok = bool(contract.get("safe_layout"))
    contract["safe_layout"] = (
        base_ok
        and all(crop_invariants.values())
    )
    if contract["safe_layout"]:
        contract["disposition"] = "READY_FOR_SHADOW_MODE"
    elif not crop_invariants["sofa_guide_survives_crop"] \
            or not crop_invariants["tv_guide_survives_crop"]:
        contract["disposition"] = "NEEDS_ALTERNATE_LIVING_ANGLE"
    elif contract.get("template") == "CONSERVATIVE_no_geometry":
        contract["disposition"] = "NEEDS_ALTERNATE_LIVING_ANGLE"
    else:
        contract["disposition"] = "UNSAFE_LAYOUT_CONTRACT"
    return contract


def _imread_unicode(photo_path: str):
    """Windows 中文路徑下 cv2.imread 會失敗；用 bytes decode。"""
    import cv2
    import numpy as np
    data = np.fromfile(photo_path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img


def _imwrite_unicode(path: str, img) -> bool:
    import cv2
    ext = os.path.splitext(path)[1] or ".jpg"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(path)
    return True


def render_overlays(photo_path: str, c: dict, ct: dict, out_dir: str, tag: str):
    import cv2
    import numpy as np
    img = _imread_unicode(photo_path)
    if img is None:
        raise FileNotFoundError(f"cannot read image: {photo_path}")
    os.makedirs(out_dir, exist_ok=True)

    def poly(im, q, color, label, thick=8):
        if not q:
            return
        pts = np.array(q, dtype=np.int32)
        overlay = im.copy()
        cv2.fillPoly(overlay, [pts], color)
        cv2.addWeighted(overlay, 0.22, im, 0.78, 0, im)
        cv2.polylines(im, [pts], True, color, thick)
        cv2.putText(im, label, (int(q[3][0]) + 12, int(q[3][1]) + 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, color, 5, cv2.LINE_AA)

    def rect(im, r, color, label, thick=6):
        if not r:
            return
        cv2.rectangle(im, (int(r[0]), int(r[1])), (int(r[2]), int(r[3])), color, thick)
        cv2.putText(im, label, (int(r[0]) + 12, int(r[1]) + 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, color, 4, cv2.LINE_AA)

    # 主選候選疊圖
    ov = img.copy()
    rect(ov, c.get("door_bbox"), (0, 165, 255), "DOOR")
    rect(ov, c.get("door_clear"), (0, 0, 220), "DOOR_CLEAR")
    rect(ov, c.get("walkway"), (60, 60, 230), "WALKWAY")
    for bl in (c.get("baseline_left"), c.get("baseline_right")):
        if bl:
            cv2.line(ov, tuple(map(int, bl[0])), tuple(map(int, bl[1])), (200, 200, 0), 5)
    poly(ov, c.get("sofa_quad"), (60, 200, 60), "SOFA")
    poly(ov, c.get("tv_quad"), (220, 130, 40), "TV")
    if c.get("sofa_quad") and c.get("tv_quad"):
        sc = np.mean(np.array(c["sofa_quad"]), axis=0).astype(int)
        tc = np.mean(np.array(c["tv_quad"]), axis=0).astype(int)
        cv2.arrowedLine(ov, tuple(sc), tuple(tc), (60, 200, 60), 10, tipLength=0.06)
    cb = ct["crop_box"]
    cv2.rectangle(ov, (cb[0], cb[1]), (cb[2], cb[3]), (255, 255, 255), 4)
    title = f"{tag} chosen={c.get('chosen')} safe={c.get('safe_layout')} {c.get('disposition')}"
    cv2.putText(ov, title[:90], (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 4, cv2.LINE_AA)
    p1 = os.path.join(out_dir, f"overlay_{tag}_original.jpg")
    _imwrite_unicode(p1, ov)

    crop = img[cb[1]:cb[3], cb[0]:cb[2]].copy()
    poly(crop, ct.get("sofa_quad_crop"), (60, 200, 60), "SOFA", 6)
    poly(crop, ct.get("tv_quad_crop"), (220, 130, 40), "TV", 6)
    rect(crop, ct.get("walkway_crop"), (60, 60, 230), "KEEP CLEAR", 4)
    rect(crop, ct.get("door_clear_crop"), (0, 0, 220), "DOOR_CLEAR", 4)
    p2 = os.path.join(out_dir, f"overlay_{tag}_crop.jpg")
    _imwrite_unicode(p2, crop)

    # 每個候選單獨疊圖，方便比 A/B
    paths = {"chosen_original": p1, "chosen_crop": p2, "candidates": {}}
    for cand in c.get("candidates") or []:
        ov2 = img.copy()
        rect(ov2, c.get("door_bbox"), (0, 165, 255), "DOOR")
        rect(ov2, c.get("door_clear"), (0, 0, 220), "DOOR_CLEAR")
        rect(ov2, c.get("walkway"), (60, 60, 230), "WALKWAY")
        for bl in (c.get("baseline_left"), c.get("baseline_right")):
            if bl:
                cv2.line(ov2, tuple(map(int, bl[0])), tuple(map(int, bl[1])), (200, 200, 0), 5)
        poly(ov2, cand.get("sofa_quad"), (60, 200, 60), "SOFA")
        poly(ov2, cand.get("tv_quad"), (220, 130, 40), "TV")
        if cand.get("sofa_quad") and cand.get("tv_quad"):
            sc = np.mean(np.array(cand["sofa_quad"]), axis=0).astype(int)
            tc = np.mean(np.array(cand["tv_quad"]), axis=0).astype(int)
            cv2.arrowedLine(ov2, tuple(sc), tuple(tc), (60, 200, 60), 10, tipLength=0.06)
        label = f"{cand['id']} pass={cand['pass']} score={cand['score']}"
        cv2.putText(ov2, label[:100], (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 255), 4, cv2.LINE_AA)
        if cand.get("fail_reasons"):
            cv2.putText(ov2, "FAIL:" + ",".join(cand["fail_reasons"])[:80],
                        (40, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3, cv2.LINE_AA)
        cp = os.path.join(out_dir, f"overlay_{tag}_{cand['id']}.jpg")
        _imwrite_unicode(cp, ov2)
        paths["candidates"][cand["id"]] = cp
    return paths


def run_case(fixture_json: str, photo_path: str, out_dir: str, tag: str,
             sofa_mode: str = "free", can_float: bool = False) -> dict:
    payload = json.loads(Path(fixture_json).read_text(encoding="utf-8"))
    from PIL import Image
    with Image.open(photo_path) as im:
        W, H = im.size
    contract = build_contract_with_crop(
        payload["zoning_v2"], W, H,
        struct_keypoints=payload.get("struct_keypoints"),
        sofa_mode=sofa_mode,
        can_float=can_float,
    )
    os.makedirs(out_dir, exist_ok=True)
    paths = render_overlays(photo_path, contract, contract["crop"], out_dir, tag)
    out_json = Path(out_dir) / f"contract_{tag}.json"
    # candidates 含 list，可直接 dump
    serializable = json.loads(json.dumps(contract, default=list))
    serializable["overlay_paths"] = paths
    out_json.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    return serializable


if __name__ == "__main__":
    root = Path(__file__).parent / "_phase0_layout_contract"
    fixture = root / "fixtures" / "E72F4ADB.json"
    photo = root / "fixtures" / "E72F4ADB.jpg"
    out = root / "output"
    result = run_case(str(fixture), str(photo), str(out), "E72F4ADB_v3")
    print("=== free default ===")
    print(json.dumps({
        "chosen": result.get("chosen"),
        "safe_layout": result.get("safe_layout"),
        "disposition": result.get("disposition"),
        "candidates": [
            {"id": x["id"], "pass": x["pass"], "score": x["score"],
             "fail_reasons": x["fail_reasons"], "sofa_side": x["sofa_side"]}
            for x in result.get("candidates") or []
        ],
    }, ensure_ascii=False, indent=2))
    r_left = run_case(str(fixture), str(photo), str(out), "E72F4ADB_left", sofa_mode="left")
    r_right = run_case(str(fixture), str(photo), str(out), "E72F4ADB_right", sofa_mode="right")
    r_float = run_case(str(fixture), str(photo), str(out), "E72F4ADB_float", sofa_mode="free", can_float=True)
    print("=== left ===", r_left.get("chosen"), r_left.get("safe_layout"), [(c["id"], c["pass"]) for c in r_left["candidates"]])
    print("=== right ===", r_right.get("chosen"), r_right.get("safe_layout"), [(c["id"], c["pass"]) for c in r_right["candidates"]])
    print("=== free+float ===", r_float.get("chosen"), r_float.get("safe_layout"), [(c["id"], c["pass"], c["score"]) for c in r_float["candidates"]])

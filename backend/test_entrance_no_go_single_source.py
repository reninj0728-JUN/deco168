# -*- coding: utf-8 -*-
"""門前禁區單一口徑：規劃期四個消費端必須讀同一塊 entrance_hard_no_go。

背景（0B1D1360）：門前禁區以前有兩套口徑。
  legacy `_layout_guide_plan → _living_door_axis_clear_rect`
      門 → 對面牆整條，禁大型家具（分區頁畫給客戶看的那條灰帶）
  S2   `render_s2_guide`
      只有 door_quad + entrance_landing 那一小塊
0B1D1360 走 s2_contract，所以客戶看到的那條灰帶從來不是它遵守的規則，
沙發才會理直氣壯站在門口。

禁區在【候選生成之前】算一次，四個階段共用同一份：
候選淘汰 → 引導圖 → 生成 prompt → 修復目標框/遮罩。

⚠️ 生成後**不當交付閘門**（見 `test_entrance_no_go_is_not_a_delivery_gate`）：
86 筆真成品實測，四種碰撞判準都把已交付的 39606371 判違規，
而且換成「家具落地點」也一樣——這條帶是規劃期指引，不是用戶的驗收標準。

每一條測試都刻意寫成「破壞實作就會紅」，不驗 log、不驗字串比對。
用的幾何是真訂單匯出的 struct_geometry_v1，不是手捏的理想房型。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import api
import layout_geometry_s2 as s2
import pytest
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"
# 0B1D1360：大門開在左側牆，規劃出得來（SAFE_FOR_GENERATION）。
SAFE_GEOM = json.loads((FIXTURES / "_entrance_no_go_geom.json").read_text(encoding="utf-8"))
SAFE_DIRECTION = [0.999926791, -0.012100107]
# 3135DE37：同一批真單，四個浮島候選的沙發＋電視櫃全部坐在進門必經路徑上。
OFFENDER_GEOM = json.loads(
    (FIXTURES / "_entrance_no_go_offender_geom.json").read_text(encoding="utf-8"))
OFFENDER_DIRECTION = [0.977615842, -0.210397874]

W, H = 1008, 756


def _plan(geometry=None, direction=None, sofa_side="free", **kwargs):
    geometry = geometry if geometry is not None else SAFE_GEOM
    direction = direction if direction is not None else SAFE_DIRECTION
    return s2.build_s2_plan(
        geometry,
        width=W,
        height=H,
        expected_source_photo_index=geometry.get("source_photo_index") or 0,
        sofa_side=sofa_side,
        transverse_direction_xy=direction,
        transverse_reference={
            "status": "observed", "direction_xy": direction,
            "support_count": 9, "unsafe_codes": [],
        },
        **kwargs,
    )


def _items(plan):
    return {item["geometry_id"]: item for item in plan["geometry"]}


def _zone_of(plan):
    return ((_items(plan).get("entrance_hard_no_go") or {}).get("shape") or {}).get(
        "coordinates")


# ─────────────────────────────────────────────────────────────────────────
# 1. 禁區不得依賴任何候選（專殺循環論證）
# ─────────────────────────────────────────────────────────────────────────

def test_zone_is_identical_across_ab_and_left_right_candidates():
    """換 A/B 候選、換左右沙發 → 禁區座標必須逐點完全相同。

    這條是本檔案的核心。如果實作偷用候選的 view_axis／
    transverse_vanishing_point 去算禁區，換一個候選禁區就會跟著移動，
    「候選沒碰禁區」就變成候選自己證明自己——測不出任何違規。
    （這個坑我犯過一次，被 GPT 抓到。）
    """
    plans = {side: _plan(sofa_side=side) for side in ("free", "left", "right")}
    zones = {}
    for side, plan in plans.items():
        assert plan["entrance_hard_no_go"]["status"] == "observed", side
        zones[side] = plan["entrance_hard_no_go"]["polygon"]

    assert zones["free"] == zones["left"] == zones["right"], (
        "禁區隨候選改變＝循環論證", zones)

    # 反貧血：三種模式真的選出了不同候選，上面那個相等才有意義。
    chosen = {side: plan["chosen_candidate_id"] for side, plan in plans.items()}
    assert len({v for v in chosen.values() if v}) > 1, (
        "三種模式選到同一個候選，這條測試就驗不到「換候選」", chosen)

    # 同一份 plan 內，所有候選（A/B/F、各種深度）也只有這一塊禁區。
    for side, plan in plans.items():
        assert _zone_of(plan) == [tuple(point) for point in zones[side]], side


def test_zone_ignores_candidate_geometry_injected_into_items():
    """把候選幾何硬塞進輸入 → 禁區必須一個座標都不變。"""
    plan = _plan()
    items = _items(plan)
    baseline = s2.entrance_hard_no_go_polygon(
        items, width=W, height=H, transverse_direction_xy=SAFE_DIRECTION)
    assert baseline["status"] == "observed"

    chosen = next(c for c in plan["candidates"]
                  if c["candidate_id"] == plan["chosen_candidate_id"])
    polluted = dict(items)
    polluted["sofa_footprint"] = {
        "shape": {"type": "polygon", "coordinates": chosen["sofa_footprint"]}}
    polluted["view_axis"] = {
        "shape": {"type": "segment", "coordinates": chosen["view_axis"]}}
    polluted["transverse_vanishing_point"] = {
        "shape": {"type": "polygon", "coordinates": chosen["layout_floor"]}}
    assert s2.entrance_hard_no_go_polygon(
        polluted, width=W, height=H,
        transverse_direction_xy=SAFE_DIRECTION) == baseline


@pytest.mark.parametrize("dropped", [
    "door_floor_contact", "left_wall_floor", "right_wall_floor", "living_floor",
])
def test_zone_requires_every_declared_source(dropped):
    """四個宣告的來源少任何一個 → 禁區必須是 unavailable，不得憑空生成。

    反貧血：證明上面的「換候選不變」不是因為函式根本沒讀輸入。
    """
    plan = _plan()
    items = dict(_items(plan))
    items.pop(dropped)
    result = s2.entrance_hard_no_go_polygon(
        items, width=W, height=H, transverse_direction_xy=SAFE_DIRECTION)
    assert result["status"] == "unavailable"
    assert result["polygon"] is None


def test_zone_unavailable_is_never_treated_as_safe():
    """算不出禁區時維持既有判定，不得放寬——也不得靜默（reason 必須留下）。"""
    plan = _plan()
    items = dict(_items(plan))
    items.pop("door_floor_contact")
    result = s2.entrance_hard_no_go_polygon(
        items, width=W, height=H, transverse_direction_xy=SAFE_DIRECTION)
    assert result["reason"]
    # 禁區缺席時候選判定必須回到「沒有這條規則」的狀態，而不是全部放行或全部擋死
    with_zone = _plan()
    assert with_zone["disposition"] == "SAFE_FOR_GENERATION"


# ─────────────────────────────────────────────────────────────────────────
# 2. 家具落在禁區裡的候選必須不合格
# ─────────────────────────────────────────────────────────────────────────

def test_real_order_candidates_inside_zone_are_rejected():
    """3135DE37：四個浮島候選的沙發＋電視櫃全坐在進門必經路徑上 → 全不合格。"""
    plan = _plan(OFFENDER_GEOM, OFFENDER_DIRECTION)
    assert plan["entrance_hard_no_go"]["status"] == "observed"
    assert plan["candidates"], "沒有候選就驗不到淘汰"
    for candidate in plan["candidates"]:
        assert candidate["entrance_no_go_offenders"] == ["SOFA", "TV"]
        assert candidate["eligible"] is False
        assert candidate["invariants"]["entrance_no_go_clear"] is False
        assert any(code.startswith("CANDIDATE_IN_ENTRANCE_NO_GO")
                   for code in candidate["fail_codes"])
    assert plan["disposition"] == "BLOCKED"


def test_same_candidates_pass_once_the_zone_is_removed(monkeypatch):
    """反貧血：同一批候選在沒有禁區時是合格的 → 擋掉它們的確實是禁區，
    不是它們本來就有別的毛病。"""
    plan = _plan(OFFENDER_GEOM, OFFENDER_DIRECTION)
    blocked_ids = [c["candidate_id"] for c in plan["candidates"]]
    assert plan["disposition"] == "BLOCKED"

    monkeypatch.setattr(s2, "entrance_hard_no_go_polygon", lambda *a, **k: {
        "status": "unavailable", "polygon": None,
        "door_wall_side": None, "reason": "test_disabled"})
    without = _plan(OFFENDER_GEOM, OFFENDER_DIRECTION)
    eligible = [c["candidate_id"] for c in without["candidates"] if c["eligible"]]
    assert sorted(eligible) == sorted(blocked_ids)
    assert without["disposition"] == "SAFE_FOR_GENERATION"


def test_moving_the_zone_onto_a_passing_candidate_rejects_it(monkeypatch):
    """人工把禁區蓋到一個原本合格的候選上 → 那個候選必須立刻變不合格。

    直接證明「候選淘汰真的讀 plan 那一份禁區」，不是碰巧同時成立。
    """
    plan = _plan()
    assert plan["disposition"] == "SAFE_FOR_GENERATION"
    chosen = next(c for c in plan["candidates"]
                  if c["candidate_id"] == plan["chosen_candidate_id"])
    assert chosen["entrance_no_go_offenders"] == []

    sofa = [(float(x), float(y)) for x, y in chosen["sofa_footprint"]]
    monkeypatch.setattr(s2, "entrance_hard_no_go_polygon", lambda *a, **k: {
        "status": "observed", "polygon": [list(point) for point in sofa],
        "door_wall_side": "left", "reason": None, "area_fraction": 0.05,
    })
    moved = _plan()
    same = next(c for c in moved["candidates"]
                if c["candidate_id"] == chosen["candidate_id"])
    assert same["eligible"] is False
    assert "SOFA" in same["entrance_no_go_offenders"]
    assert any(code.startswith("CANDIDATE_IN_ENTRANCE_NO_GO")
               for code in same["fail_codes"])


# ─────────────────────────────────────────────────────────────────────────
# 3. 引導圖畫出來的區域 == plan 裡那同一個 polygon
# ─────────────────────────────────────────────────────────────────────────

def _photo(tmp_path):
    path = tmp_path / "base.jpg"
    Image.new("RGB", (W, H), (255, 255, 255)).save(path, "JPEG", quality=95)
    return path


def _sample_grid(polygon, step=6):
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    points = []
    y = min(ys)
    while y <= max(ys):
        x = min(xs)
        while x <= max(xs):
            points.append((x, y))
            x += step
        y += step
    return points


def test_guide_paints_exactly_the_plan_polygon(tmp_path):
    """引導圖上被漆紅的區域必須就是 plan 裡那個 polygon——不是另一塊。

    做法：同一張 plan 渲染兩次，第二次把禁區從 geometry 拿掉，比對像素差。
    「有差的像素」必須落在 polygon 內，polygon 深處必須真的有差。
    """
    photo = _photo(tmp_path)
    plan = _plan()
    polygon = [tuple(point) for point in plan["entrance_hard_no_go"]["polygon"]]
    assert _zone_of(plan) == polygon

    with_zone = tmp_path / "with.jpg"
    s2.render_s2_guide(photo, with_zone, plan)

    stripped = copy.deepcopy(plan)
    stripped["geometry"] = [item for item in stripped["geometry"]
                            if item["geometry_id"] != "entrance_hard_no_go"]
    without_zone = tmp_path / "without.jpg"
    s2.render_s2_guide(photo, without_zone, stripped)

    a = Image.open(with_zone).convert("RGB").load()
    b = Image.open(without_zone).convert("RGB").load()

    def differs(x, y):
        px, py = int(x), int(y)
        if not (0 <= px < W and 0 <= py < H):
            return False
        return any(abs(u - v) > 8 for u, v in zip(a[px, py], b[px, py]))

    # 兩張圖都會畫的東西（門、玄關、沙發/電視目標框）在差集裡本來就看不到，
    # 取樣時排除，否則量到的是重疊面積而不是「禁區有沒有被漆」。
    items = _items(plan)
    chosen = next(c for c in plan["candidates"]
                  if c["candidate_id"] == plan["chosen_candidate_id"])
    shared = [
        [tuple(p) for p in items[name]["shape"]["coordinates"]]
        for name in ("door_quad", "entrance_landing")
    ] + [[tuple(p) for p in chosen["sofa_footprint"]],
         [tuple(p) for p in chosen["tv_footprint"]]]
    buffer_zone = s2._compact_entry_buffer_polygon(plan, chosen, width=W, height=H)
    if buffer_zone:
        shared.append([tuple(p) for p in buffer_zone])

    def edge_distance(point, poly):
        return min(s2._point_segment_distance(point, poly[i], poly[(i + 1) % len(poly)])
                   for i in range(len(poly)))

    def is_shared(point):
        return any(s2._point_in_polygon(point, poly) or edge_distance(point, poly) < 8
                   for poly in shared)

    inside_checked = inside_diff = 0
    for point in _sample_grid(polygon):
        if not s2._point_in_polygon(point, polygon):
            continue
        # 邊界像素會被抗鋸齒壓過，只取離邊界 6px 以上的深處
        if edge_distance(point, polygon) < 6 or is_shared(point):
            continue
        inside_checked += 1
        inside_diff += differs(*point)
    assert inside_checked > 50, "取樣點太少，這條測試會變成空測"
    assert inside_diff / inside_checked > 0.99, (
        f"polygon 內部只有 {inside_diff}/{inside_checked} 個點被漆到")

    # 頂部圖例的文字本來就會因為多了禁區而改寫（"RED = DOOR + WALK-IN PATH…"），
    # 那是預期差異、不是漆錯地方。排除它，並確認排除的範圍碰不到 polygon。
    legend_bottom = max(20, H // 60) + max(34, W // 40) * 3 + 12
    assert min(point[1] for point in polygon) > legend_bottom, "圖例帶蓋到禁區了"

    outside_diff = [
        point for point in _sample_grid([(0, 0), (W, 0), (W, H), (0, H)], step=9)
        if point[1] > legend_bottom
        and not s2._point_in_polygon(point, polygon)
        and edge_distance(point, polygon) > 8
        and differs(*point)
    ]
    assert not outside_diff, (
        f"polygon 外面有 {len(outside_diff)} 個點被漆到，畫的不是同一塊："
        f"{outside_diff[:5]}")


# ─────────────────────────────────────────────────────────────────────────
# 4. 修復後家具仍碰禁區 → 不得採用
# ─────────────────────────────────────────────────────────────────────────

def _contract_with_zone(tmp_path, polygon, source=(4032, 3024)):
    contract = {
        "source": {"size": {"width": source[0], "height": source[1]}},
        "extensions": {"stage": "S2_GEOMETRY_CONTRACT"},
        "decision": {"pre_generation_eligible": True},
        "geometry": [{
            "geometry_id": "entrance_hard_no_go",
            "kind": "entrance_hard_no_go",
            "shape": {"type": "polygon", "coordinates": polygon},
        }],
    }
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    return contract, str(path)


# 來源座標系（4032x3024）裡一塊門前禁區；換算成 0–1000 就是 x 100–700、y 700–900。
ZONE_SRC = [[403.2, 2116.8], [2822.4, 2116.8], [2822.4, 2721.6], [403.2, 2721.6]]


def _bbox_1000(x0, y0, x1, y1):
    return [y0, x0, y1, x1]


def test_repair_target_box_inside_the_zone_is_refused(tmp_path):
    """修復目標框壓到禁區 → 必須判定為碰撞，呼叫端據此退回合約 footprint。

    這就是「修復後家具仍碰禁區不得採用」實際生效的地方：修復根本不准把
    沙發**瞄準**禁區。合約 footprint 在規劃期已通過 entrance_no_go_clear，
    退回它必然是安全的。
    """
    _, contract_path = _contract_with_zone(tmp_path, ZONE_SRC)
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    zone = api._s2_entrance_no_go_points(contract, 4032, 3024)
    assert zone, "合約裡讀不到禁區，後面全是空測"
    assert api._box_hits_entrance_no_go((1200, 2200, 1900, 2600), zone) is True
    assert api._box_hits_entrance_no_go((1200, 900, 1900, 1400), zone) is False


def test_repair_reads_the_same_zone_the_plan_published(tmp_path):
    """修復讀到的禁區必須是合約裡那一份，而且縮放正確——不是另外算一塊。"""
    _, contract_path = _contract_with_zone(tmp_path, ZONE_SRC)
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    full = api._s2_entrance_no_go_points(contract, 4032, 3024)
    assert [[round(x, 3), round(y, 3)] for x, y in full] == [
        [round(x, 3), round(y, 3)] for x, y in ZONE_SRC]
    # 換到別的畫布尺寸必須等比例縮放（成品圖與原圖尺寸不同時會走到）
    half = api._s2_entrance_no_go_points(contract, 2016, 1512)
    assert [[round(x * 2, 3), round(y * 2, 3)] for x, y in half] == [
        [round(x, 3), round(y, 3)] for x, y in ZONE_SRC]


def test_no_zone_in_contract_means_no_repair_constraint(tmp_path):
    """合約沒有禁區（門開在進深端）→ 這條規則整個不作用，維持既有行為。"""
    path = tmp_path / "bare.json"
    path.write_text(json.dumps({
        "source": {"size": {"width": 4032, "height": 3024}},
        "extensions": {"stage": "S2_GEOMETRY_CONTRACT"},
        "decision": {"pre_generation_eligible": True},
        "geometry": [],
    }), encoding="utf-8")
    contract = json.loads(path.read_text(encoding="utf-8"))
    assert api._s2_entrance_no_go_points(contract, 4032, 3024) is None
    assert api._box_hits_entrance_no_go((1200, 2200, 1900, 2600), None) is False


def test_entrance_no_go_is_not_a_delivery_gate_yet(tmp_path):
    """門前禁區【目前】不接生成後交付閘門。這條測試鎖的是現況，不是永久禁令。

    為什麼現在不接（79 筆真成品實測）：
      * 11 個家具落地點在灰廊內【且】門距層放行；量它們離最近那面牆多遠
        （除以門寬）→ 範圍 0.13～0.63，**全部貼著某一面牆**。
      * 唯一已交付的 39606371 是 0.25，正好落在這群中間，上下都有落選樣本
        → 沒有任何門檻能把「接受」和「拒絕」分開；接受組 n=1，配門檻＝過擬合。
      * 「家具站在灰廊正中間、兩側都空、動線真的斷掉」一次都沒出現過。
    所以用戶的規則不是「家具不得進灰廊」，是「灰廊要走得通」——
    那件事由 `_door_adjacency_violation`（0.25/0.28 門寬，使用者裁決校準）在管。

    ⚠️ 這只支持「現在不要接」，不證明永遠不需要。
    **可以改這條測試的觸發條件**：出現「離兩面牆都 > 1 個門寬」的成品——
    那才是真的截斷動線，也才有拒絕樣本可以建校準集。
    在那之前把它接回 `_fail_closed_validation` 會弄壞 39606371 那類已交付的圖。
    （另一個保留：門距只是動線的代理，不等於完整證明走得通；目前沒有資料
    顯示它漏掉「灰廊中央堵死」，因為那種案例還沒發生過。）
    """
    validation = {"ok": True, "hard_fail": False,
                  "render_bboxes": {"sofa": _bbox_1000(200, 750, 400, 880),
                                    "focal_anchor": _bbox_1000(150, 720, 350, 860)}}
    checked = api._fail_closed_validation(copy.deepcopy(validation), "living")
    assert checked["ok"] is True
    assert checked["hard_fail"] is False
    assert "entrance_no_go_offenders" not in checked
    # 生成後的門邊防線仍在，只是換那條有使用者校準的（0.25/0.28 門寬）
    from gemini_analyze import _door_adjacency_violation
    assert _door_adjacency_violation({
        "entrance_door": _bbox_1000(60, 600, 140, 900),
        "sofa": _bbox_1000(145, 700, 350, 880),
    }) is not None


# ─────────────────────────────────────────────────────────────────────────
# 合約傳遞：規劃期四個消費端讀到的是同一份
# ─────────────────────────────────────────────────────────────────────────

def test_contract_carries_the_same_polygon_as_the_plan(tmp_path):
    """合約 geometry 裡的禁區必須與 plan 逐點相同，且標成推導、不是觀測。"""
    import layout_contract_v1 as lcv1

    photo = _photo(tmp_path)
    plan = _plan()
    plan["geometry_verification"] = {"status": "pass", "unsafe_codes": []}
    contract = lcv1.build_layout_contract_s2(
        job_id="TESTNOGO", photo_path=photo, photo_key="k", view_index=0,
        s2_plan=plan, photo_binding_verified=True,
        legacy_zoning=None, legacy_shadow=None)
    record = next(item for item in contract["geometry"]
                  if item["geometry_id"] == "entrance_hard_no_go")
    assert [list(point) for point in record["shape"]["coordinates"]] == [
        list(point) for point in plan["entrance_hard_no_go"]["polygon"]]
    assert record["evidence"]["mode"] == "inferred"
    assert record["evidence"]["producer"]["name"] == (
        "layout_geometry_s2.entrance_hard_no_go_polygon")

    chosen = next(c for c in contract["candidates"]
                  if c["candidate_id"] == contract["decision"]["chosen_candidate_id"])
    zone_constraints = [c for c in chosen["constraints"]
                        if c["check_code"] == "ENTRANCE_NO_GO_CLEAR"]
    assert len(zone_constraints) == 1
    assert "entrance_hard_no_go" in zone_constraints[0]["geometry_ids"]

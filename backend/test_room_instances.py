# -*- coding: utf-8 -*-
"""一個 room_key ＝一間房，不是一個 room_type 一間。

A559DD2B（2026-08-08）：客戶標了 2 張臥室 + 1 客廳 + 1 書房，只交付 3 個空間——
第二間臥室被 `rts_seen`（按 room_type 去重）吃掉，而且客戶到結果頁才發現。
三房兩廳、四房、透天都會撞這個。

設計：`room_type`（哪一類）與 `room_key`（哪一間）分開。
  · room_type — 家具配對規則、判官驗收、裁切適用房型都吃它。臥室族一律 bedroom。
  · room_key  — 分頁、金額、去重、空間數上限吃它。臥室 1／2／3 各自一間。
上傳頁的臥室下拉展開成「臥室 1／2／3」：同一間房的多張照片選同一個號碼＝合併；
不同臥室選不同號碼＝各自出圖。客戶不必分辨主臥次臥，也不用自由命名。

⚠️ 向下相容：舊訂單的 target_zone 是 `bedroom`，room_key 也是 `bedroom`
   → 照舊當一間，行為完全不變。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import api
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_HTML = (ROOT / "upload.html").read_text(encoding="utf-8")


def _photo(tmp_path, name, colour):
    p = tmp_path / name
    Image.new("RGB", (400, 300), colour).save(p, "JPEG", quality=92)
    return str(p)


def _meta(pairs):
    """把「客戶選了哪個選項」轉成後端收到的 PhotoMeta 形狀。

    ⚠️ `target_zone` 只能是 ZONE_ENUM 的值（bedroom），房間身分走獨立的
    `room_key`。第一版把 bedroom_1 塞進 target_zone，真實請求被 400 擋死。
    """
    out = {}
    for p, z in pairs:
        base = z.split("_")[0] if re.fullmatch(r"[a-z]+_\d+", z) else z
        m = {"photo_key": Path(p).name, "target_zone": base}
        if base != z:
            m["room_key"] = z
        out[Path(p).name] = m
    return out


@pytest.fixture
def a559(tmp_path):
    """A559DD2B 的真實形狀：2 臥室 + 1 客廳 + 1 書房。"""
    b1 = _photo(tmp_path, "photo_02.jpg", (230, 228, 220))
    b2 = _photo(tmp_path, "photo_03.jpg", (210, 208, 200))
    lv = _photo(tmp_path, "photo_04.jpg", (190, 188, 180))
    st = _photo(tmp_path, "photo_05.jpg", (170, 168, 160))
    return b1, b2, lv, st


# ── ① 兩間臥室要各出一間 ────────────────────────────────────────────

def test_two_bedrooms_produce_two_rooms(a559):
    """🔴 A559DD2B 的核心缺陷：兩間臥室只交付一間。"""
    b1, b2, lv, st = a559
    paths = [b1, b2, lv, st]
    regions = api._build_user_regions_whole(paths, _meta([
        (b1, "bedroom_1"), (b2, "bedroom_2"), (lv, "living"), (st, "study")]))
    keys = [r["room_key"] for r in regions]
    assert keys == ["living", "bedroom_1", "bedroom_2", "study"], keys
    assert len(regions) == 4, f"應交付 4 個空間，實際 {len(regions)}"
    # 底圖各自不同——兩間臥室不能共用同一張
    bases = [paths[r["best_photo_index"]] for r in regions]
    assert len(set(bases)) == 4, f"有房間共用底圖：{bases}"


def test_room_type_stays_bedroom_for_every_instance(a559):
    """家具規則／判官都吃 room_type，臥室族一律 bedroom，不得被實例污染。"""
    b1, b2, lv, st = a559
    regions = api._build_user_regions_whole([b1, b2, lv, st], _meta([
        (b1, "bedroom_1"), (b2, "bedroom_2"), (lv, "living"), (st, "study")]))
    for r in regions:
        if r["room_key"].startswith("bedroom"):
            assert r["room_type"] == "bedroom", f"{r['room_key']} 的 room_type 被改壞了"


def test_display_name_carries_the_instance_number(a559):
    b1, b2, lv, st = a559
    regions = api._build_user_regions_whole([b1, b2, lv, st], _meta([
        (b1, "bedroom_1"), (b2, "bedroom_2"), (lv, "living"), (st, "study")]))
    names = [r["name"] for r in regions]
    assert names == ["客廳", "臥室 1", "臥室 2", "書房"], names


# ── ② 同一間房的多張照片仍然合併 ────────────────────────────────────

def test_same_bedroom_multiple_angles_still_merge(a559):
    """🔴 這是「一張照片＝一間」會壞掉、而號碼制不會壞的地方。

    同一間臥室拍兩個角度 → 都選「臥室 1」→ 合併成一間，第二張退成備援底圖。
    """
    b1, b2, lv, st = a559
    regions = api._build_user_regions_whole([b1, b2, lv, st], _meta([
        (b1, "bedroom_1"), (b2, "bedroom_1"), (lv, "living"), (st, "study")]))
    keys = [r["room_key"] for r in regions]
    assert keys == ["living", "bedroom_1", "study"], keys
    bed = next(r for r in regions if r["room_key"] == "bedroom_1")
    assert len(bed["alt_photo_indices"]) == 1, "第二張沒有退成備援底圖"


# ── ③ 舊訂單：行為完全不變 ──────────────────────────────────────────

def test_legacy_bedroom_tag_behaves_exactly_as_before(a559):
    """舊訂單的 target_zone 是 `bedroom`（沒有號碼）→ 照舊合併成一間。"""
    b1, b2, lv, st = a559
    regions = api._build_user_regions_whole([b1, b2, lv, st], _meta([
        (b1, "bedroom"), (b2, "bedroom"), (lv, "living"), (st, "study")]))
    keys = [r["room_key"] for r in regions]
    assert keys == ["living", "bedroom", "study"], keys
    assert next(r for r in regions if r["room_key"] == "bedroom")["name"] == "主臥室"


def test_kitchen_still_folds_into_dining(a559):
    """`kitchen` 一直都併進餐廳，這個既有行為不得被實例化改掉。"""
    b1, _b2, lv, st = a559
    regions = api._build_user_regions_whole([lv, b1, st], _meta([
        (lv, "living"), (b1, "kitchen"), (st, "dining")]))
    assert [r["room_key"] for r in regions] == ["living", "dining"]


# ── ④ 排序：客廳第一，臥室按號碼 ────────────────────────────────────

def test_living_first_then_instance_number(tmp_path):
    ps = [_photo(tmp_path, f"p{i}.jpg", (200 - i * 10,) * 3) for i in range(5)]
    regions = api._build_user_regions_whole(ps, _meta([
        (ps[0], "bedroom_3"), (ps[1], "study"), (ps[2], "bedroom_1"),
        (ps[3], "living"), (ps[4], "bedroom_2")]))
    assert [r["room_key"] for r in regions] == [
        "living", "bedroom_1", "bedroom_2", "bedroom_3", "study"]


# ── ⑤ 總上限 5 ──────────────────────────────────────────────────────

def test_over_cap_is_recorded_not_silently_dropped(tmp_path, capsys):
    """🔴 後端不得替客戶決定哪間房不做。

    上限的把關點在上傳頁（擋住送出，讓客戶自己刪）。真的走到後端＝前端被繞過
    或壞了——這時保留完整標記並大聲記錄，客服才查得出客戶原本要幾間。
    第一版由後端直接砍掉排序最後那間（書房），那是「安靜丟掉客戶的房間」，
    正是 A559DD2B 的客訴形狀。
    """
    ps = [_photo(tmp_path, f"q{i}.jpg", (200 - i * 8,) * 3) for i in range(6)]
    regions = api._build_user_regions_whole(ps, _meta([
        (ps[0], "living"), (ps[1], "dining"), (ps[2], "bedroom_1"),
        (ps[3], "bedroom_2"), (ps[4], "bedroom_3"), (ps[5], "study")]))
    assert len(regions) == 6, "後端自行丟棄了客戶標的房間"
    assert regions[0]["room_key"] == "living", "客廳仍要排第一"
    out = capsys.readouterr().out
    assert "超過上限" in out and "書房" in out, f"超上限沒有留下紀錄：{out}"
    assert "前端應已擋下" in out, "沒說明這是前端漏擋的異常路徑"


def test_upload_page_blocks_submission_over_the_cap():
    """🔴 上限要在【送出前】擋住，不能只轉紅然後放行。"""
    i = UPLOAD_HTML.index("async function nextStep()")
    seg = UPLOAD_HTML[i:i + 1200]
    code = chr(10).join(l for l in seg.splitlines() if not l.strip().startswith("//"))
    assert "countRoomInstances()" in code, "送出前沒有計算空間數"
    assert "> MAX_ROOM_INSTANCES" in code and "return;" in code, (
        "超過上限沒有擋住送出")
    assert "不會替你決定" in seg, "沒告訴客戶要自己決定刪哪張"


def test_under_the_cap_nothing_is_dropped(tmp_path, capsys):
    ps = [_photo(tmp_path, f"r{i}.jpg", (200 - i * 8,) * 3) for i in range(4)]
    regions = api._build_user_regions_whole(ps, _meta([
        (ps[0], "living"), (ps[1], "bedroom_1"),
        (ps[2], "bedroom_2"), (ps[3], "study")]))
    assert len(regions) == 4
    assert "超過上限" not in capsys.readouterr().out


# ── ⑥ 前後端的常數與選項必須一致 ────────────────────────────────────

def test_upload_page_offers_numbered_bedrooms():
    m = re.search(r"var BEDROOM_ZONE_OPTIONS[\s\S]*?\}\)\(\);", UPLOAD_HTML)
    assert m, "上傳頁沒有臥室實例選項"
    assert "'bedroom_' + i" in m.group(0)


def test_frontend_and_backend_caps_match():
    """🔴 上傳頁的計數器與後端的截斷用同一組數字，否則客戶被告知的跟實際不同。"""
    fe_rooms = int(re.search(r"var MAX_ROOM_INSTANCES = (\d+);", UPLOAD_HTML).group(1))
    fe_beds = int(re.search(r"var MAX_BEDROOM_INSTANCES = (\d+);", UPLOAD_HTML).group(1))
    assert fe_rooms == api.MAX_ROOM_INSTANCES, "空間上限前後端不一致"
    assert fe_beds == api.MAX_BEDROOM_INSTANCES, "臥室上限前後端不一致"


def test_every_upload_zone_value_maps_to_a_legal_backend_zone():
    """🔴 上傳頁的下拉 value 帶號碼（UI 用），但送出前必須拆成 ZONE_ENUM 的 base。

    第一版直接把 `bedroom_1` 送進 target_zone，`_normalize_photo_meta_for_room`
    直接 400 整單退回——單元測試全綠、真實請求全滅。
    """
    m = re.search(r"var ZONE_OPTIONS = \[([\s\S]*?)\]\);", UPLOAD_HTML)
    assert m, "找不到 ZONE_OPTIONS"
    values = set(re.findall(r"value: '([a-z0-9_]+)'", m.group(1)))
    values |= {f"bedroom_{i}" for i in range(1, api.MAX_BEDROOM_INSTANCES + 1)}
    for v in values:
        base = v.split("_")[0] if re.fullmatch(r"[a-z]+_\d+", v) else v
        assert base in api.ZONE_ENUM, f"上傳頁的 {v} 拆出來的 {base} 不在 ZONE_ENUM"


def test_secondary_options_never_carry_instance_numbers():
    """「也包含」講的是空間類型，不是哪一間臥室——photo_contains 送 ZONE_ENUM。"""
    m = re.search(r"var SECONDARY_ZONE_OPTIONS = \[([\s\S]*?)\];", UPLOAD_HTML)
    assert m, "找不到 SECONDARY_ZONE_OPTIONS"
    for v in re.findall(r"value: '([a-z0-9_]+)'", m.group(1)):
        assert not re.fullmatch(r"[a-z]+_\d+", v), f"SECONDARY 帶了房間號碼：{v}"
        assert v in api.ZONE_ENUM, f"SECONDARY 的 {v} 不在 ZONE_ENUM"


# ── ⑦ 正規化函式本身 ────────────────────────────────────────────────

@pytest.mark.parametrize("zone,rk,room_key,room_type,display", [
    ("living", None, "living", "living", "客廳"),
    ("dining", None, "dining", "dining", "餐廳"),
    ("kitchen", None, "dining", "dining", "餐廳"),
    ("bedroom", None, "bedroom", "bedroom", "主臥室"),
    ("bedroom", "bedroom_1", "bedroom_1", "bedroom", "臥室 1"),
    ("bedroom", "bedroom_4", "bedroom_4", "bedroom", "臥室 4"),
    ("study", None, "study", "study", "書房"),
])
def test_zone_normalisation(zone, rk, room_key, room_type, display):
    meta = {"target_zone": zone}
    if rk:
        meta["room_key"] = rk
    assert api._photo_room_key(meta) == room_key
    assert api._ZONE_TO_RT[zone] == room_type
    assert api._room_key_display(room_key) == display


@pytest.mark.parametrize("meta", [
    None, {}, {"target_zone": ""}, {"target_zone": "other"},
    {"target_zone": "garage"}, {"target_zone": "balcony"},
])
def test_unknown_zones_are_not_invented(meta):
    """認不得的標記要回 None，不得亂猜成某一間房。"""
    assert api._photo_room_key(meta) is None


@pytest.mark.parametrize("bad_rk", ["bedroom_9", "bedroom_0", "living_1",
                                    "bedroom_x", "bedroom_", "  "])
def test_bad_room_key_falls_back_to_the_room_type(bad_rk):
    """room_key 不合法時退回 room_type，不得亂猜成別間房。"""
    assert api._photo_room_key(
        {"target_zone": "bedroom", "room_key": bad_rk}) == "bedroom"


# ── ⑧ 交付端：分頁、金額、空間數都要按「間」算 ──────────────────────

import json
import shutil
import subprocess
import tempfile

RESULT_HTML = (ROOT / "result.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_node_is_available_so_the_delivery_layer_is_actually_verified():
    """🔴 這是交付端（金額／空間數／分頁）唯一的行為驗證層，不得靜默跳過。

    我又寫了一次 `pytest.skip` 保護傘（2026-08-09 被抓到）：沒有 node 就整組
    行為測試消失、而測試仍然全綠。這正是這個專案一路在對抗的失敗模式——
    假綠比沒有測試更危險。要放棄就得改程式，不給環境變數後門。
    """
    assert NODE, "找不到 node——交付端的行為驗證層無法執行（不提供跳過選項）"


_STRIP = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|//.*$""")


def _js(marker: str) -> str:
    """數大括號切出完整函式（靠縮排猜邊界會切錯，node 直接語法錯）。"""
    lines = RESULT_HTML.splitlines()
    i = next(k for k, l in enumerate(lines) if marker in l)
    depth, started, out = 0, False, []
    for line in lines[i:]:
        out.append(line)
        bare = _STRIP.sub("", line)
        depth += bare.count("{") - bare.count("}")
        if "{" in bare:
            started = True
        if started and depth <= 0:
            break
    return "\n".join(out)


def _delivery(renders, rooms=None):
    src = "\n".join([
        _js("var ROOM_TYPE_TO_ZONE"), _js("function normalizeRoomType("),
        _js("function roomKeyOf("), _js("function roomKeyDisplay("),
        _js("var _RT_ORDER = {"), _js("function _roomRank("),
        _js("function planTotals("), _js("function updateMetaSummary("),
    ]) + r"""
var _groups = [], _els = {};
global.localStorage = { getItem: function(){ return 'A'; } };
global.document = { getElementById: function(id){
  if (!_els[id]) _els[id] = { textContent:"", innerHTML:"" }; return _els[id]; } };
var IN = JSON.parse(process.argv[2]);
var pt = planTotals(IN.renders);
var out = { totals: {} };
Object.keys(pt.byStyle).forEach(function(st){
  out.totals[st] = { rooms: Object.keys(pt.byStyle[st].rooms).length,
                     items: pt.byStyle[st].order.length };
});
out.order = IN.renders.slice().sort(function(a,b){ return _roomRank(a) - _roomRank(b); })
                      .map(function(r){ return r.angle_label; });
updateMetaSummary({ renders: IN.renders, rooms: IN.rooms || [] });
out.metaPlan = _els['metaPlan'].textContent;
process.stdout.write(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, str(p), json.dumps({"renders": renders,
                                                      "rooms": rooms or []})],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"node 執行失敗：{r.stderr[:800]}"
    return json.loads(r.stdout)


def _r(room_key, room_type, label, item):
    return {"style": "modern", "style_label": "都會簡約",
            "room_type": room_type, "room_key": room_key, "angle_label": label,
            "matched_furniture": [{"name_zh": item, "price_twd": 20000,
                                   "purchase_url": f"https://s/{item}"}],
            "soft_furnishing": []}


A559_RENDERS = [
    _r("living", "living", "客廳", "沙發"),
    _r("bedroom_1", "bedroom", "臥室 1", "雙人床"),
    _r("bedroom_2", "bedroom", "臥室 2", "單人床"),
    _r("study", "study", "書房", "書桌"),
]


def test_two_bedrooms_count_as_two_spaces_in_the_money():
    """🔴 用 room_type 分組會把兩間床算成一間，金額與「使用於 N 個空間」少算。"""
    out = _delivery(A559_RENDERS)
    assert out["totals"]["modern"]["rooms"] == 4, (
        f"空間數算成 {out['totals']['modern']['rooms']}，兩間臥室被併了")
    assert out["totals"]["modern"]["items"] == 4, "商品數不對（兩張床應各自計）"


def test_header_space_count_is_four():
    out = _delivery(A559_RENDERS, rooms=[{"room_type": "living_room"}])
    assert "4 個空間" in out["metaPlan"], f"頁首空間數不對：{out['metaPlan']}"


def test_rooms_list_does_not_double_count_bedrooms():
    """🔴 rooms[] 只有 room_type=bedroom，不得跟 bedroom_1／2 疊加成三間。"""
    out = _delivery(A559_RENDERS, rooms=[{"room_type": "living_room"},
                                         {"room_type": "bedroom"}])
    assert "4 個空間" in out["metaPlan"], (
        f"rooms 的 bedroom 被重複計數：{out['metaPlan']}")


def test_tab_order_is_living_then_bedroom_1_then_2():
    out = _delivery(list(reversed(A559_RENDERS)))
    assert out["order"] == ["客廳", "臥室 1", "臥室 2", "書房"], out["order"]


def test_legacy_renders_without_room_key_still_work():
    """舊訂單的 render 沒有 room_key → 退回 room_type，行為完全不變。"""
    legacy = [{k: v for k, v in r.items() if k != "room_key"} for r in A559_RENDERS[:1]]
    legacy.append({k: v for k, v in A559_RENDERS[1].items() if k != "room_key"})
    out = _delivery(legacy)
    assert out["totals"]["modern"]["rooms"] == 2, "舊訂單的房間計數被改壞了"


# ── ⑨ 端到端：真實前端 payload → 後端 normalize → regions ────────────
#
# 🔴 這一節是上一版**整個缺掉**的那一關。第一版把 `bedroom_1` 塞進
#    `target_zone`，31 條單元測試全綠——因為它們直接呼叫
#    `_build_user_regions_whole`，跳過了 `_normalize_photo_meta_for_room`。
#    而真實請求會先過那一關，實測結果是：
#        photo_meta[0].photo_contains 含非法 Zone: 'bedroom_1'  → HTTP 400
#    「單元測試綠、真實請求整單退回」比不做更糟。

def _frontend_payload(pairs):
    """模擬 upload.html `buildPhotoMeta` 真正送出的形狀。

    上傳頁的下拉 value 帶號碼（UI 用），送出前用 zoneBase / zoneInstanceKey
    拆成 ZONE_ENUM 的 target_zone ＋ 獨立的 room_key。
    """
    metas = []
    for key, ui_value in pairs:
        base = ui_value.split("_")[0] if re.fullmatch(r"[a-z]+_\d+", ui_value) else ui_value
        m = {"photo_key": key, "photo_contains": [base], "target_zone": base,
             "target_location_hint": "unspecified", "avoid_zones": [], "target_note": ""}
        if base != ui_value:
            m["room_key"] = ui_value
        metas.append(m)
    return {"room_id": "r1", "room_type": "living_room",
            "photo_keys": [k for k, _ in pairs], "photo_meta": metas}


def test_real_frontend_payload_passes_the_photo_meta_contract():
    """🔴 真實 payload 必須通過 PhotoMeta 驗證，不能被 400 擋掉。"""
    room = _frontend_payload([
        ("uploads/X/p1.jpg", "living"), ("uploads/X/p2.jpg", "bedroom_1"),
        ("uploads/X/p3.jpg", "bedroom_2"), ("uploads/X/p4.jpg", "study")])
    normalized, err = api._normalize_photo_meta_for_room(room)
    assert not err, f"真實 payload 被後端拒絕：{err}"
    assert [m["room_key"] for m in normalized] == [
        "living", "bedroom_1", "bedroom_2", "study"]
    # target_zone 一律是 ZONE_ENUM 的值，不帶號碼
    for m in normalized:
        assert m["target_zone"] in api.ZONE_ENUM, m["target_zone"]
        for z in m["photo_contains"]:
            assert z in api.ZONE_ENUM, f"photo_contains 混進非法 zone：{z}"


def test_end_to_end_two_bedrooms_survive_normalisation(tmp_path):
    """payload → normalize → regions：兩間臥室要一路活到 regions。"""
    paths = [_photo(tmp_path, f"e{i}.jpg", (220 - i * 12,) * 3) for i in range(4)]
    room = _frontend_payload([
        (Path(paths[0]).name, "living"), (Path(paths[1]).name, "bedroom_1"),
        (Path(paths[2]).name, "bedroom_2"), (Path(paths[3]).name, "study")])
    normalized, err = api._normalize_photo_meta_for_room(room)
    assert not err, err
    by_key = {m["photo_key"]: m for m in normalized}
    regions = api._build_user_regions_whole(paths, by_key)
    assert [r["room_key"] for r in regions] == [
        "living", "bedroom_1", "bedroom_2", "study"]
    assert [r["name"] for r in regions] == ["客廳", "臥室 1", "臥室 2", "書房"]


def test_end_to_end_four_bedrooms_plus_living_is_exactly_five(tmp_path):
    """四房＋客廳＝5，剛好在上限內——這是你要支援的透天／四房格局。"""
    paths = [_photo(tmp_path, f"f{i}.jpg", (220 - i * 10,) * 3) for i in range(5)]
    room = _frontend_payload(
        [(Path(paths[0]).name, "living")]
        + [(Path(paths[i]).name, f"bedroom_{i}") for i in range(1, 5)])
    normalized, err = api._normalize_photo_meta_for_room(room)
    assert not err, f"四間臥室被拒絕：{err}"
    regions = api._build_user_regions_whole(
        paths, {m["photo_key"]: m for m in normalized})
    assert len(regions) == 5
    assert [r["name"] for r in regions] == [
        "客廳", "臥室 1", "臥室 2", "臥室 3", "臥室 4"]


def test_legacy_payload_without_room_key_still_passes():
    """老 client 不送 room_key → 通過驗證，退化成一種房型一間。"""
    room = _frontend_payload([("uploads/X/p1.jpg", "living"),
                              ("uploads/X/p2.jpg", "bedroom")])
    normalized, err = api._normalize_photo_meta_for_room(room)
    assert not err, err
    assert [m["room_key"] for m in normalized] == ["living", "bedroom"]


@pytest.mark.parametrize("bad", [
    {"room_key": "bedroom_1", "target_zone": "living"},   # 號碼掛錯房型
    {"room_key": "bedroom_9", "target_zone": "bedroom"},  # 超出範圍
    {"room_key": "bedroom_x", "target_zone": "bedroom"},  # 格式錯
    {"room_key": 123, "target_zone": "bedroom"},          # 型別錯
])
def test_malformed_room_key_is_rejected_not_guessed(bad):
    """🔴 fail-closed：room_key 不合法就明講，不得猜成某一間房。"""
    m = {"photo_key": "uploads/X/p.jpg", "photo_contains": [bad["target_zone"]],
         "target_zone": bad["target_zone"], "room_key": bad["room_key"],
         "target_location_hint": "unspecified", "avoid_zones": [], "target_note": ""}
    _n, err = api._normalize_photo_meta_for_room(
        {"room_id": "r", "room_type": "living_room",
         "photo_keys": ["uploads/X/p.jpg"], "photo_meta": [m]})
    assert err and "room_key" in err, f"不合法的 room_key 被放行了：{err!r}"


# ── ⑩ room_key 必須貫穿修復鏈與三層存檔 ─────────────────────────────
#
# 🔴 第一版只接了主路徑：第一輪成功的圖有 room_key，但只要走到修復或降級存檔，
#    room_key 就掉了 → 交付端把兩間臥室重新合併成一間。
#    「第一次對、重試後錯」比「一直錯」更難查。

API_SRC = Path(api.__file__).read_text(encoding="utf-8")


def _code_only(seg: str) -> str:
    return chr(10).join(l for l in seg.splitlines() if not l.strip().startswith("#"))


@pytest.mark.parametrize("marker,what", [
    ('new_r["room_type"]    = entry.get("_room_type", "living")', "Phase2 硬修接受圖"),
    ('"room_type":         fixed.get("room_type", "living"),', "Phase3 補寫 DB"),
    ('"room_type":   r.get("room_type"),', "落選紀錄 dropped_renders"),
    ('"room_type": r.get("room_type") or r.get("_room_type"),', "needs_regen"),
])
def test_room_key_travels_with_room_type_everywhere(marker, what):
    """每一個寫 room_type 的出口，旁邊都要有 room_key。"""
    i = API_SRC.index(marker)
    seg = _code_only(API_SRC[i:i + 420])
    assert "room_key" in seg, f"{what} 沒帶 room_key——兩間臥室會在這裡被併回一間"


@pytest.mark.parametrize("tier", ["slim_result_json", "minimal_result_json"])
def test_both_degraded_storage_tiers_keep_room_key(tier):
    """🔴 大 payload 寫入失敗時會降級存檔，那兩層也不能弄丟 room_key。"""
    i = API_SRC.index(f"{tier} = {{")
    seg = _code_only(API_SRC[i:i + 1400])
    assert '"room_key"' in seg or '"room_key":' in seg, (
        f"{tier} 沒保存 room_key——存檔降級後兩間臥室會併回一間")


def test_phase3_clears_needs_regen_by_room_key_not_room_type():
    """🔴 修好臥室 1 不得把臥室 2 的待修紀錄一起刪掉。

    兩者 room_type 都是 bedroom；只比房型的話臥室 2 會永遠不被補生。
    """
    i = API_SRC.index('rj["needs_regen"] = [')
    seg = _code_only(API_SRC[i - 400:i + 500])
    assert "_fixed_key" in seg and "room_key" in seg, "Phase3 仍只比 room_type"
    assert 'n.get("room_key") or n.get("room_type")' in seg, (
        "舊紀錄沒有 room_key 時要退回 room_type，不能直接不匹配")


# ── ⑪ 真的跑上傳頁的 JS，把它產出的 payload 餵進後端驗證 ─────────────
#
# 🔴 這一節補的是「⑨ 端到端」還漏掉的那半：⑨ 是我在 Python 裡**照我以為的樣子**
#    組 payload。蓄意破壞證明那不夠——把 upload.html 改回送 `bedroom_1` 進
#    target_zone（原本的 P0），54 條測試照樣全綠。
#    只有真的執行 upload.html 的 zoneBase / computePhotoContains / buildPhotoMeta，
#    再把結果交給 `_normalize_photo_meta_for_room`，才驗得到那條路。

def _upload_js(marker: str) -> str:
    lines = UPLOAD_HTML.splitlines()
    i = next(k for k, l in enumerate(lines) if marker in l)
    depth, started, out = 0, False, []
    for line in lines[i:]:
        out.append(line)
        bare = _STRIP.sub("", line)
        depth += bare.count("{") - bare.count("}")
        if "{" in bare:
            started = True
        if started and depth <= 0:
            break
    return "\n".join(out)


def _payload_from_upload_page(ui_choices):
    """執行 upload.html 真正的 payload 組裝，回傳它會送出的 photo_meta。"""
    src = chr(10).join([
        "var DEFAULT_TARGET_ZONE = 'living';",
        "var SECONDARY_NONE = 'none';",
        "var TARGET_NOTE_MAX = 100;",
        "var DEFAULT_TARGET_LOCATION_HINT = 'unspecified';",
        _upload_js("function zoneBase("),
        _upload_js("function zoneInstanceKey("),
        _upload_js("function computePhotoContains("),
        # ⚠️ 抽**真的** buildPhotoMeta，不是照抄一份。照抄的話改壞真函式測試照樣
        #    全綠——2026-08-09 蓄意破壞就是這樣漏掉原本那個 P0 的。
        _upload_js("function buildPhotoMeta(photo_keys) {"),
    ]) + r"""
var IN = JSON.parse(process.argv[2]);
var photos = IN.map(function(p){
  return { key: p.key, target_zone: p.zone, secondary_zone: 'none', target_note: '' };
});
process.stdout.write(JSON.stringify(
  buildPhotoMeta(IN.map(function(p){ return p.key; }))));
"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, str(p), json.dumps(ui_choices)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"node 執行失敗：{r.stderr[:600]}"
    return json.loads(r.stdout)


def test_upload_page_payload_is_accepted_by_the_backend_contract():
    """🔴 原 P0 的回歸鎖：上傳頁真正送出的東西必須通過 PhotoMeta 驗證。

    第一版送 `target_zone: 'bedroom_1'`，實測：
        photo_meta[0].photo_contains 含非法 Zone: 'bedroom_1' → HTTP 400 整單退回
    """
    metas = _payload_from_upload_page([
        {"key": "uploads/X/p1.jpg", "zone": "living"},
        {"key": "uploads/X/p2.jpg", "zone": "bedroom_1"},
        {"key": "uploads/X/p3.jpg", "zone": "bedroom_2"},
        {"key": "uploads/X/p4.jpg", "zone": "study"},
    ])
    # 上傳頁自己就不該把號碼放進 zone 欄位
    for m in metas:
        assert m["target_zone"] in api.ZONE_ENUM, (
            f"上傳頁把 {m['target_zone']!r} 送進 target_zone，後端會 400")
        for z in m["photo_contains"]:
            assert z in api.ZONE_ENUM, (
                f"上傳頁把 {z!r} 送進 photo_contains，後端會 400")
    # 而且要真的通過後端那一關
    _n, err = api._normalize_photo_meta_for_room({
        "room_id": "r1", "room_type": "living_room",
        "photo_keys": [m["photo_key"] for m in metas],
        "photo_meta": [dict(m, avoid_zones=[]) for m in metas]})
    assert not err, f"上傳頁的真實 payload 被後端拒絕：{err}"


def test_upload_page_payload_preserves_the_room_identity():
    """反貧血：拆掉號碼之後，房間身分不能跟著消失。"""
    metas = _payload_from_upload_page([
        {"key": "a.jpg", "zone": "bedroom_1"},
        {"key": "b.jpg", "zone": "bedroom_2"},
        {"key": "c.jpg", "zone": "bedroom"},      # 沒號碼＝老流程
    ])
    assert metas[0]["room_key"] == "bedroom_1"
    assert metas[1]["room_key"] == "bedroom_2"
    assert "room_key" not in metas[2], "沒選號碼時不該憑空生出 room_key"
    # 三張的 room_type 來源都是 bedroom
    assert [m["target_zone"] for m in metas] == ["bedroom"] * 3


# ── ⑫ 修好一間臥室，另一間不得跟著消失 ──────────────────────────────

def _phase3_cleanup(dropped, needs_regen, fixed):
    """重現 Phase3 補生成功後對 result_json 的那段清理（api.py:8186 附近）。

    ⚠️ 這裡照抄的是**判斷條件**，所以下面另有一條原始碼測試確認 api.py 真的
    用同一套；只靠這個模擬會變成「測我自己寫的邏輯」。
    """
    _fixed_rk = fixed.get("room_key") or fixed.get("room_type") or ""
    kept_dropped = [d for d in dropped
                    if not (d.get("style") == fixed.get("style")
                            and (d.get("room_key") or d.get("room_type") or "") == _fixed_rk)]
    kept_regen = [n for n in needs_regen
                  if not (n.get("style") == fixed.get("style")
                          and ((n.get("room_key") or n.get("room_type") or "")
                               in ("", _fixed_rk)))]
    return kept_dropped, kept_regen, len(kept_dropped)


def test_fixing_bedroom_one_leaves_bedroom_two_pending():
    """🔴 兩間臥室同時落選，補生修好臥室 1 之後，臥室 2 必須還在待修清單。

    只比 `style + room_type` 的話兩筆會一起被刪（room_type 都是 bedroom），
    臥室 2 從此不會被補生，而且 `dropped` 只減 1、清單卻少 2 筆，統計自相矛盾。
    """
    dropped = [
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1"},
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2"},
    ]
    regen = [
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1"},
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2"},
    ]
    fixed = {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1"}
    kept_d, kept_r, count = _phase3_cleanup(dropped, regen, fixed)
    assert [d["room_key"] for d in kept_d] == ["bedroom_2"], "臥室 2 的落選紀錄被誤刪"
    assert [n["room_key"] for n in kept_r] == ["bedroom_2"], "臥室 2 的待修紀錄被誤刪"
    assert count == 1, f"dropped 計數與清單長度不一致：{count}"


def test_legacy_records_without_room_key_still_clear():
    """舊紀錄沒有 room_key → 退回 room_type，照舊清得掉。"""
    dropped = [{"style": "modern", "room_type": "bedroom"}]
    kept_d, _kr, count = _phase3_cleanup(
        dropped, [], {"style": "modern", "room_type": "bedroom"})
    assert kept_d == [] and count == 0


def test_phase3_dropped_cleanup_matches_by_room_key_in_source():
    """原始碼層：api.py 真的用 room_key 比對，而且計數用清單長度回填。"""
    i = API_SRC.index('vs["dropped_renders"] = [')
    seg = _code_only(API_SRC[i - 600:i + 600])
    assert "_fixed_rk" in seg, "Phase3 清 dropped_renders 仍只比 room_type"
    assert 'd.get("room_key") or d.get("room_type")' in seg, "舊紀錄沒有退回 room_type"
    assert 'vs["dropped"] = len(vs["dropped_renders"])' in seg, (
        "dropped 計數還在做加減法，會跟清單長度對不上")
    assert 'vs["dropped"] = max(0, int(vs.get("dropped") or 0) - 1)' not in seg, (
        "舊的 -1 算法還在，會出現兩套計數")


# ── ⑬ 精簡 validation_summary 不得弄丟 room_key ─────────────────────

def test_slim_validation_summary_keeps_room_key():
    """🔴 大 payload 寫不進 Supabase 時會退到精簡版，那一層也要分得開兩間臥室。"""
    summary = {"delivered": 2, "dropped": 2, "dropped_renders": [
        {"style": "modern", "style_label": "都會簡約", "angle_label": "臥室 1",
         "room_type": "bedroom", "room_key": "bedroom_1", "reason": "x",
         "failure_class": "render_quality", "validation_final": {"ok": False}},
        {"style": "modern", "style_label": "都會簡約", "angle_label": "臥室 2",
         "room_type": "bedroom", "room_key": "bedroom_2", "reason": "y",
         "failure_class": "render_quality", "validation_final": {"ok": False}},
    ]}
    slim = api._slim_validation_summary(summary)
    keys = [d.get("room_key") for d in slim["dropped_renders"]]
    assert keys == ["bedroom_1", "bedroom_2"], (
        f"精簡版弄丟了房間身分：{keys}——兩間臥室的落選紀錄會分不開")


def test_slim_validation_summary_survives_legacy_records():
    """舊紀錄沒有 room_key → 精簡後是 None，不得炸掉。"""
    slim = api._slim_validation_summary({"dropped": 1, "dropped_renders": [
        {"style": "modern", "room_type": "bedroom", "reason": "x"}]})
    assert slim["dropped_renders"][0]["room_key"] is None


# ── ⑭ 超過上限：收單時就 400，不是印 log 後照樣生前 5 間 ─────────────

def _post_job(zones, monkeypatch, tmp_path):
    """真的打 /api/job（TestClient），不是掃原始碼。

    ⚠️ 只掃「原始碼裡有沒有那幾個字」抓不到 `if False:` 這種破壞——
    2026-08-09 蓄意破壞實測漏掉。要驗就要真的送一個請求進去。

    🔴 收單成功會有三個副作用，全部要擋掉，否則測試會動到真東西：
       ① `background_tasks.add_task(run_pipeline, ...)`——TestClient 會**真的執行**
          背景任務，那會打 fal 與 Gemini，花真錢。
       ② `sb_upsert()` 建 queued 訂單、`write_status()` 再寫一次——api.py 內建
          正式 Supabase URL 與 key，即使沒設環境變數也會嘗試連線。在帶
          SUPABASE_SERVICE_KEY 的環境跑，會在正式庫留下永遠停在 queued 的假訂單，
          而且 `sb_upsert` 失敗不影響回應，所以測試照樣 200——看不出來。
       ③ 在 backend/jobs、backend/uploads 留下目錄。
       ①②③ 都不會讓測試變紅，所以只能靠這裡主動隔離。

    🔴 `monkeypatch` / `tmp_path` 是**必填**，不給預設值。
       第一版做成選填，超上限那條測試就忘了傳——即使 API 早早回 400、
       沒碰 Supabase，helper 仍已在**正式**的 backend/uploads 建好
       PYTEST_CAP/paths.json（GPT 2026-08-09 實際重現）。
       留一個「忘記隔離也能跑」的入口，遲早有人走進去。
    """
    monkeypatch.setattr(api, "run_pipeline", lambda *a, **k: None)
    monkeypatch.setattr(api, "sb_upsert", lambda *a, **k: True)
    monkeypatch.setattr(api, "sb_save_upload", lambda *a, **k: True)
    monkeypatch.setattr(api, "JOBS_DIR", Path(tmp_path) / "jobs")
    monkeypatch.setattr(api, "UPLOADS_DIR", Path(tmp_path) / "uploads")
    (Path(tmp_path) / "jobs").mkdir(parents=True, exist_ok=True)
    # 導向真的生效了才准往下走——monkeypatch 寫錯名字時會靜默無效
    assert Path(tmp_path) in api.UPLOADS_DIR.parents, "UPLOADS_DIR 沒被導向 tmp"
    assert Path(tmp_path) in api.JOBS_DIR.parents, "JOBS_DIR 沒被導向 tmp"

    from fastapi.testclient import TestClient
    # create_job 會先確認 uploads/<id>/paths.json 存在（不存在→404），
    # 再用 canonical_photo_key 把 rooms 的 photo_keys 對到 paths.json。
    # ⚠️ 兩邊要對得起來：paths.json 存本機絕對路徑，photo_key 必須是
    #    canonical 形式。對不上就 400「主空間照片資料配對失敗」——
    #    我第一版的 fixture 就是這樣死在配對，卻被當成「5 間放行」（GPT 抓到）。
    up = api.UPLOADS_DIR / "PYTEST_CAP"
    up.mkdir(parents=True, exist_ok=True)
    local_paths = [str(up / f"p{i}.jpg") for i in range(len(zones))]
    (up / "paths.json").write_text(json.dumps(local_paths), encoding="utf-8")
    photo_keys = [api.canonical_photo_key(p) for p in local_paths]

    metas = []
    for i, z in enumerate(zones):
        base = z.split("_")[0] if re.fullmatch(r"[a-z]+_\d+", z) else z
        m = {"photo_key": photo_keys[i], "photo_contains": [base],
             "target_zone": base, "target_location_hint": "unspecified",
             "avoid_zones": [], "target_note": ""}
        if base != z:
            m["room_key"] = z
        metas.append(m)
    rooms = [{"room_id": "living_room", "room_type": "living_room",
              "room_label": "客廳", "is_primary": True, "room_notes": "",
              "photo_keys": photo_keys, "video_keys": [], "photo_meta": metas}]
    return TestClient(api.app).post("/api/job", data={
        "upload_id": "PYTEST_CAP", "styles": "modern", "plan": "B",
        "space_type": "whole", "render_angle": "multi",
        "rooms_json": json.dumps(rooms, ensure_ascii=False)})


def test_over_cap_is_rejected_at_job_creation_not_silently_trimmed(
        monkeypatch, tmp_path):
    """🔴 前端擋住只是第一道。請求被直接送來時，後端必須 400。

    第一版只在 regions 那層印 log，然後 `n_views = min(5, len(regions))`
    照樣只生前 5 間——那不是「不丟房」，只是把丟棄位置往後移，
    客戶付了錢才發現有房間沒做。
    """
    r = _post_job(["living", "dining", "bedroom_1", "bedroom_2",
                   "bedroom_3", "study"], monkeypatch, tmp_path)   # 6 個空間
    assert r.status_code == 400, f"超過上限沒被擋，回了 {r.status_code}"
    err = r.json().get("error", "")
    assert "最多設計" in err and "6 個" in err, f"訊息沒講清楚：{err}"
    assert "不會替你決定" in err, "沒告訴客戶要自己決定刪哪一間"
    # 客戶標了哪幾間要列出來，他才知道刪哪張
    for name in ("客廳", "餐廳", "臥室 1", "臥室 2", "臥室 3", "書房"):
        assert name in err, f"訊息沒列出「{name}」"


def test_exactly_five_rooms_is_accepted(monkeypatch, tmp_path):
    """🔴 反貧血：剛好 5 間（四房＋客廳）必須**真的收單成功**。

    第一版寫成「只要不是『因上限而 400』就算成功」——結果 fixture 的
    photo_key 跟 paths.json 對不上，實際回的是 400「主空間照片資料配對失敗」，
    測試卻通過（GPT 2026-08-09 直接呼叫 helper 重現）。
    **排除某一種錯誤 ≠ 成功**，要斷言真正的成功狀態與 job_id。
    """
    r = _post_job(["living", "bedroom_1", "bedroom_2", "bedroom_3", "bedroom_4"],
                  monkeypatch, tmp_path)
    assert r.status_code == 200, f"剛好 5 間沒有收單成功：{r.status_code} {r.text[:200]}"
    body = r.json()
    assert body.get("job_id"), f"收單成功卻沒有 job_id：{body}"


def test_four_rooms_is_accepted(monkeypatch, tmp_path):
    """A559DD2B 的形狀（客廳＋兩間臥室＋書房）必須收得進來。"""
    r = _post_job(["living", "bedroom_1", "bedroom_2", "study"], monkeypatch, tmp_path)
    assert r.status_code == 200, f"四間被擋：{r.status_code} {r.text[:200]}"
    assert r.json().get("job_id")


def test_fail_closed_message_lists_every_room():
    """400 訊息要列出客戶標了哪幾間，他才知道要刪哪張。"""
    seg = API_SRC[API_SRC.index("_rk_seen = []"):][:1200]
    assert "_room_key_display(k) for k in _rk_seen" in seg, (
        "沒有把客戶標的房間名列出來")


# ── ⑮ Phase3 診斷不得串錯房間 ───────────────────────────────────────

def _final(room_key, angle, style="modern"):
    return {"style": style, "room_type": "bedroom", "room_key": room_key,
            "angle_label": angle, "render_path": f"/x/{room_key}.jpg"}


def test_diagnostics_never_attach_to_the_wrong_bedroom():
    """🔴 落選的臥室 2 不得被匹配到臥室 1。

    `find_dropped_render_match` 原本只比 style + room_type + angle_label。
    兩間臥室的 room_type 都是 bedroom；顯示名一旦撞（文案改一次就會撞），
    臥室 2 的 validation_history / guide_trace 會被寫成臥室 1 的紀錄——
    診斷指錯房，後面就修錯對象。這正是這個函式「不明確就不寫」要防的事。
    """
    finals = [_final("bedroom_1", "臥室"), _final("bedroom_2", "臥室")]
    got = api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2",
         "angle_label": "臥室"}, finals)
    assert got is not None and got["room_key"] == "bedroom_2", (
        f"診斷被寫到 {got and got.get('room_key')} 身上")


def test_diagnostics_prefer_room_key_over_angle_label():
    """顯示名不同也一樣：room_key 才是身分，angle_label 只是文案。"""
    finals = [_final("bedroom_1", "臥室 1"), _final("bedroom_2", "臥室 2")]
    for key in ("bedroom_1", "bedroom_2"):
        got = api.find_dropped_render_match(
            {"style": "modern", "room_type": "bedroom", "room_key": key,
             "angle_label": "完全不同的舊文案"}, finals)
        assert got and got["room_key"] == key, f"{key} 配錯了"


def test_no_matching_room_key_writes_nothing():
    """🔴 對不上就不寫——不得退回 room_type 比對，那會寫到另一間臥室身上。

    ⚠️ 視角也要一樣，否則「對不上 → 退回 room_type + 視角」還是會配到別間。
    第一版只驗了視角不同的情況，把 fallback 拿掉照樣全綠（蓄意破壞抓到）。
    """
    # 視角不同
    assert api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2",
         "angle_label": "臥室 2"}, [_final("bedroom_1", "臥室 1")]) is None
    # 視角相同（文案撞名）——這才是 fallback 會咬人的情況
    assert api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2",
         "angle_label": "臥室"}, [_final("bedroom_1", "臥室")]) is None, (
        "對不上 room_key 卻退回 room_type+視角，診斷被寫到另一間臥室身上")
    # 沒有視角資訊、候選唯一——舊規則會直接採用，有 room_key 時不准
    assert api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2"},
        [_final("bedroom_1", "")]) is None


def test_same_room_multiple_angles_still_needs_the_angle_to_match():
    """同一間房有多個視角時，仍要視角對得上才寫——不得抓第一個。

    這個函式的原始理念就是「不明確就不寫」，room_key 相同但視角有兩個時，
    直接取 [0] 等於把 A 視角的診斷寫到 B 視角身上。
    """
    two_angles = [_final("bedroom_1", "主視角"), _final("bedroom_1", "另一角度")]
    # 視角對得上 → 寫
    got = api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1",
         "angle_label": "另一角度"}, two_angles)
    assert got and got["angle_label"] == "另一角度"
    # 視角對不上 → 不寫
    assert api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1",
         "angle_label": "第三個角度"}, two_angles) is None
    # 沒有視角資訊 → 不明確，不寫
    assert api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1"},
        two_angles) is None


def test_legacy_dropped_without_room_key_still_matches():
    """舊資料沒有 room_key → 退回既有的 room_type + 視角規則，行為不變。"""
    finals = [{"style": "modern", "room_type": "bedroom", "angle_label": "主臥室"}]
    got = api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom", "angle_label": "主臥室"}, finals)
    assert got is finals[0]
    # 候選不唯一且沒視角資訊 → 仍然不猜
    two = [{"style": "modern", "room_type": "bedroom"},
           {"style": "modern", "room_type": "bedroom"}]
    assert api.find_dropped_render_match(
        {"style": "modern", "room_type": "bedroom"}, two) is None


# ── ⑯ 編號只掛臥室；上傳頁文案講真話 ────────────────────────────────

@pytest.mark.parametrize("zone", ["living", "dining", "study"])
def test_only_bedrooms_may_carry_an_instance_number(zone):
    """🔴 產品只有臥室支援多間。`living_1` 放行只會產生沒人處理的空間。"""
    assert api._photo_room_key({"target_zone": zone, "room_key": f"{zone}_1"}) == zone
    m = {"photo_key": "uploads/X/p.jpg", "photo_contains": [zone],
         "target_zone": zone, "room_key": f"{zone}_1",
         "target_location_hint": "unspecified", "avoid_zones": [], "target_note": ""}
    _n, err = api._normalize_photo_meta_for_room(
        {"room_id": "r", "room_type": "living_room",
         "photo_keys": ["uploads/X/p.jpg"], "photo_meta": [m]})
    assert err and "只有臥室支援多間" in err, f"{zone}_1 被放行了：{err!r}"


def test_bedroom_numbering_is_still_accepted():
    """反貧血：臥室的編號不能一起被擋掉。"""
    assert api._photo_room_key(
        {"target_zone": "bedroom", "room_key": "bedroom_2"}) == "bedroom_2"


def test_upload_counter_copy_matches_the_real_behaviour():
    """🔴 文案說「超過的部分不會產生設計圖」，真實行為是整單擋住送出。

    講錯行為比不講更糟：客戶會以為按下去至少能拿到前 5 個。
    """
    i = UPLOAD_HTML.index("function renderRoomCount()")
    seg = UPLOAD_HTML[i:i + 900]
    assert "無法送出" in seg, "沒說清楚超過上限是擋住送出"
    assert "超過的部分不會產生設計圖" not in seg, "還在講不成立的行為"


def test_numbering_rule_is_visible_to_the_customer():
    """🔴 臥室編號的操作規則不能只活在程式註解裡。

    標錯會直接改變空間數與生成成本：
      · 同一間房選了兩個號碼 → 被拆成兩間，多花一次生成
      · 兩間房選同一個號碼   → 合成一間，少拿一張設計圖
    客戶看不到規則就會標錯，而這是付費行為。
    """
    i = UPLOAD_HTML.index("這張照片主要是？")
    seg = UPLOAD_HTML[i:i + 1600]
    assert "同一個號碼" in seg and "不同號碼" in seg, (
        "下拉選單旁沒有說明編號規則，客戶會標錯")
    assert "同一間房" in seg


# ── ⑰ 測試不得碰正式資料庫、不得留下本機訂單目錄 ────────────────────

def test_successful_job_never_touches_the_real_database(monkeypatch, tmp_path):
    """🔴 收單成功會 sb_upsert 建 queued 訂單、write_status 再寫一次。

    api.py 內建正式 Supabase URL 與 key，即使本機沒設環境變數也會嘗試連線；
    在帶 SUPABASE_SERVICE_KEY 的環境跑，會在**正式庫**留下永遠停在 queued 的
    假訂單。而 `sb_upsert` 失敗不影響回應，所以測試照樣 200——完全看不出來。
    這條直接把網路呼叫換成會炸的樁：真的打出去就紅。
    """
    calls = []

    def _boom(*a, **k):
        calls.append(a[:1])
        raise AssertionError("測試期間打了真的網路請求")

    monkeypatch.setattr(api._req, "post", _boom)
    monkeypatch.setattr(api._req, "patch", _boom)
    monkeypatch.setattr(api._req, "put", _boom)

    r = _post_job(["living", "bedroom_1", "bedroom_2"], monkeypatch, tmp_path)
    assert r.status_code == 200, f"收單失敗：{r.status_code} {r.text[:200]}"
    assert r.json().get("job_id")
    assert not calls, f"測試期間打了 {len(calls)} 個真網路請求"


def test_successful_job_writes_only_inside_tmp(monkeypatch, tmp_path):
    """訂單目錄要落在 tmp，不得污染 backend/jobs 與 backend/uploads。"""
    before = {p.name for p in (Path(api.__file__).parent / "jobs").glob("*")} \
        if (Path(api.__file__).parent / "jobs").exists() else set()
    r = _post_job(["living", "bedroom_1"], monkeypatch, tmp_path)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert (Path(tmp_path) / "jobs" / job_id).exists(), "訂單目錄沒落在 tmp"
    after = {p.name for p in (Path(api.__file__).parent / "jobs").glob("*")} \
        if (Path(api.__file__).parent / "jobs").exists() else set()
    assert after == before, f"污染了正式工作目錄：{after - before}"


# ── ⑱ Phase3 插回順序：PDF 直接吃陣列順序 ───────────────────────────

def test_phase3_insert_order_respects_the_instance_number():
    """🔴 兩間臥室的 room_type 都是 bedroom，只比房型會讓臥室 2 插在臥室 1 前。

    結果頁自己會再排一次，但 **PDF 直接沿用 renders 的原始順序**——
    客戶下載的設計手冊裡房間順序就顛倒了。
    """
    k = api._render_room_order_key
    assert k({"room_type": "living", "room_key": "living"}) < \
           k({"room_type": "bedroom", "room_key": "bedroom_1"})
    assert k({"room_type": "bedroom", "room_key": "bedroom_1"}) < \
           k({"room_type": "bedroom", "room_key": "bedroom_2"})
    assert k({"room_type": "bedroom", "room_key": "bedroom_4"}) < \
           k({"room_type": "study", "room_key": "study"})
    # 舊資料沒有 room_key → 退回房型序，行為不變
    assert k({"room_type": "bedroom"}) == k({"room_type": "bedroom", "room_key": "bedroom"})


def _run_phase3_insert(existing, new_render):
    """真的跑 api.py 那段插回位置的計算（不是照抄）。

    ⚠️ 只掃「原始碼有沒有出現 _rt_ord」的話，換個變數名就繞過了——
    2026-08-09 蓄意破壞實測漏掉。用 exec 執行真正那幾行才擋得住。
    """
    i = API_SRC.index('_st = _new_render.get("style")')
    i = API_SRC.rfind(chr(10), 0, i) + 1        # 退到行首，dedent 才找得到共同縮排
    j = API_SRC.index("rj_renders.insert(_pos, _new_render)", i)
    j = API_SRC.rfind(chr(10), 0, j) + 1
    import textwrap
    snippet = textwrap.dedent(API_SRC[i:j])
    ns = {"_new_render": new_render, "rj_renders": list(existing),
          "_render_room_order_key": api._render_room_order_key, "len": len,
          "enumerate": enumerate}
    exec(snippet, ns)   # noqa: S102 — 執行的是 api.py 自己的程式碼
    return ns["_pos"]


def test_phase3_insert_position_is_computed_with_the_instance_number():
    """🔴 直接執行 api.py 的插回計算：臥室 2 不得插在臥室 1 前面。"""
    existing = [
        {"style": "modern", "room_type": "living", "room_key": "living"},
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1"},
        {"style": "modern", "room_type": "study", "room_key": "study"},
    ]
    pos = _run_phase3_insert(existing, {"style": "modern", "room_type": "bedroom",
                                        "room_key": "bedroom_2"})
    assert pos == 2, f"臥室 2 被插到位置 {pos}（應在臥室 1 之後、書房之前）"
    # 反向：臥室 1 補回來時要插在臥室 2 前面
    existing2 = [
        {"style": "modern", "room_type": "living", "room_key": "living"},
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_2"},
    ]
    pos2 = _run_phase3_insert(existing2, {"style": "modern", "room_type": "bedroom",
                                          "room_key": "bedroom_1"})
    assert pos2 == 1, f"臥室 1 被插到位置 {pos2}（應在臥室 2 前面）"


def test_phase3_insert_still_puts_living_first():
    """反貧血：補回的客廳仍要排在最前（2A520C25 修過的那個行為）。"""
    existing = [
        {"style": "modern", "room_type": "bedroom", "room_key": "bedroom_1"},
        {"style": "modern", "room_type": "study", "room_key": "study"},
    ]
    assert _run_phase3_insert(
        existing, {"style": "modern", "room_type": "living", "room_key": "living"}) == 0


def test_backend_and_frontend_order_keys_agree():
    """前後端排序必須同一套：前端顯示、後端決定陣列順序（PDF 吃它）。"""
    m = re.search(r"return base \* (\d+) \+ \(m \? parseInt\(m\[1\], 10\) : 0\);",
                  (ROOT / "result.html").read_text(encoding="utf-8"))
    assert m, "前端 _roomRank 的算法變了"
    assert int(m.group(1)) == 100, "前後端排序乘數不一致"
    src = inspect.getsource(api._render_room_order_key)
    assert "base * 100" in src, "後端排序乘數跟前端對不上"


def test_the_helper_cannot_be_called_without_isolation():
    """🔴 `_post_job` 的隔離參數必須是必填，不給預設值。

    做成選填就會有人忘記傳——超上限那條測試就是這樣在**正式**的
    backend/uploads 留下 PYTEST_CAP/paths.json（2026-08-09 實際重現）。
    留一個「忘記隔離也能跑」的入口，遲早有人走進去。
    """
    sig = inspect.signature(_post_job)
    for name in ("monkeypatch", "tmp_path"):
        assert sig.parameters[name].default is inspect.Parameter.empty, (
            f"{name} 有預設值——會出現忘記隔離的呼叫")


def test_no_test_ever_pollutes_the_real_uploads_dir():
    """反貧血：跑完這一檔，正式 backend/uploads 不得多出 PYTEST_CAP。"""
    real = Path(api.__file__).parent / "uploads" / "PYTEST_CAP"
    assert not real.exists(), (
        f"測試在正式工作目錄留下 {real}——有呼叫端沒走隔離")


# ── ⑲ 兩間臥室不得拿到一模一樣的購物清單 ────────────────────────────

def test_two_bedrooms_get_different_furniture():
    """🔴 D4001755（2026-08-10）：兩間臥室四件商品一字不差，客戶一眼看破。

    根因是家具按 room_type 配一次再複用——第二間根本沒有自己的配對。
    改成逐 room_key 各配一次，並把同單已選過的商品排掉：排序是決定性的，
    不排除的話「各配一次」還是會挑到同一批。
    """
    from furniture_match import enrich_renders
    renders = [{"style": "modern", "style_label": "現代簡約", "flux_prompt": "bedroom"}]
    a = enrich_renders(renders, room_type="bedroom")
    used = {f["id"] for f in a[0]["matched_furniture"] if f.get("id")}
    assert used, "第一間就沒配到東西，前提不成立"
    b = enrich_renders(renders, room_type="bedroom",
                       exclude_ids_by_style={"modern": used})
    b_ids = {f["id"] for f in b[0]["matched_furniture"] if f.get("id")}
    assert b_ids, "第二間完全配不到東西——排除排過頭了"
    assert b_ids != used, "兩間臥室拿到一模一樣的清單"
    # 主件（床）一定要不同——軟裝可以共用，床不行
    def _bed(lst):
        return next((f["id"] for f in lst if "bed" in str(f.get("category_en") or "")), None)
    if _bed(a[0]["matched_furniture"]) and _bed(b[0]["matched_furniture"]):
        assert _bed(a[0]["matched_furniture"]) != _bed(b[0]["matched_furniture"]), (
            "兩間臥室推同一張床")


def test_exclusion_also_removes_duplicate_listings_of_the_same_product():
    """🔴 只比 id 不夠：目錄有同商品重複上架（id 不同、name_zh 一樣）。

    D4001755 修完第一版後實測，兩間臥室的「重複商品」是 0 件，但床的名字
    一模一樣——第二間挑到了另一個 id 的同一張床。客戶看到的還是同一件。
    """
    from furniture_match import _catalog_without
    cat = [
        {"id": "a1", "category": "床架", "name_zh": "輕奢牛皮床台 雙人5尺"},
        {"id": "a2", "category": "床架", "name_zh": "輕奢牛皮床台 雙人5尺"},  # 重複上架
        {"id": "b1", "category": "床架", "name_zh": "亞麻標準雙人5尺床架組"},
    ]
    kept = _catalog_without(cat, {"a1"})
    ids = {i["id"] for i in kept}
    assert "a2" not in ids, "同商品的另一個 id 沒被排掉——客戶看到的還是同一張床"
    assert "b1" in ids, "把不同商品也排掉了"


def test_exclusion_never_empties_a_category():
    """🔴 排除不得讓某個品類一件不剩——寧可共用一張地毯，不能沒有地毯可推。"""
    from furniture_match import _catalog_without
    cat = [{"id": "a", "category": "地毯"}, {"id": "b", "category": "床架"}]
    kept = _catalog_without(cat, {"a"})
    cats = {i["category"] for i in kept}
    assert "地毯" in cats, "唯一的地毯被排掉了，那個品類會開天窗"
    assert _catalog_without(cat, set()) is cat, "沒有排除清單時不該複製目錄"


def test_matching_is_per_room_key_not_per_room_type():
    """原始碼層：配對迴圈吃 room_key，不是 room_type。"""
    code = _code_only(API_SRC)
    assert "enriched_by_rk" in code and "distinct_rks" in code, "配對還是按房型做一次"
    assert "enriched_by_rt" not in code, "舊的按房型配對還在"
    assert "exclude_ids_by_style=_used_ids_by_style" in code, "沒有把已選商品排掉"


# ── ⑳ 幾何要算在屋主指定的那張照片上（真根因）────────────────────────

ZONING_SRC = (Path(api.__file__).parent / "zoning_v2.py").read_text(encoding="utf-8")


def test_zoning_accepts_a_preferred_photo_index():
    """🔴 真根因：幾何算在哪張，後面整條鏈都跟著。

    底圖不是幾何那張時 guide 建不出來（api.py「底圖不是 zoning 主視角，
    禁止跨照片套 bbox」）→ 格局前檢以「auto 客廳無 guide」擋死 → 客廳零圖。
    293BDE11 與 D4001755 都死在這條鏈。

    所以要在**呼叫模型之前**就把屋主指定的那張告訴它——不是事後改
    best_photo_index（座標焊死在模型看的那張上，事後改＝拿另一張照片的門
    去擺這張的家具，比擋死更危險）。
    """
    import inspect as _i
    sig = _i.signature(__import__("zoning_v2").compute_zoning_v2)
    assert "prefer_index" in sig.parameters
    assert sig.parameters["prefer_index"].default is None, "必須可省略（單房不受影響）"


def _build_prefer_note(prefer_index, n_photos=4):
    """執行 zoning_v2 真正建 prefer_note 的那段碼。"""
    import textwrap
    seg = ZONING_SRC[ZONING_SRC.index('    prefer_note = ""'):
                     ZONING_SRC.index("    prompt_text = PROMPT.format")]
    ns = {"prefer_index": prefer_index, "valid_photos": list(range(n_photos)),
          "isinstance": isinstance, "int": int, "len": len, "bool": bool}
    exec(textwrap.dedent(seg), ns)      # noqa: S102 — 執行的是專案自己的程式碼
    return ns["prefer_note"]


def test_the_owner_choice_actually_reaches_the_prompt():
    """🔴 建出來≠送到模型。

    `str.format()` 對**多傳的 kwarg 是靜默忽略**——模板沒有 `{prefer_note}`
    佔位時，整段指定會被吞掉，Gemini 收到的 prompt 跟沒修一字不差。
    我第一版就是這樣：note 建了、也傳進 format 了，但模板缺佔位，
    整刀完全空轉，而 940 條測試全綠（2026-08-10 抓到）。

    所以要斷言 **format 的輸出**，不是斷言 note 變數。
    """
    import zoning_v2 as Z
    assert "{prefer_note}" in Z.PROMPT, "模板沒有 prefer_note 佔位——指定會被靜默吞掉"
    out = Z.PROMPT.format(photo_count=4, video_note="",
                          prefer_note=_build_prefer_note(2))
    assert "【主視角已由屋主指定】" in out, "屋主指定沒有出現在送給模型的 prompt 裡"
    assert "best_photo_index 必須等於 2" in out
    # 沒有指定時，模板不得多出空白區塊以外的東西
    plain = Z.PROMPT.format(photo_count=4, video_note="", prefer_note="")
    assert "屋主指定" not in plain


def test_prompt_hard_requires_the_owner_choice():
    """prompt 要講死：不得改選、看不見的元素照實標 missing，不准換照片補。"""
    note = _build_prefer_note(2)
    assert "best_photo_index 必須等於 2" in note, "沒有硬性指定索引"
    assert "不得改選" in note, "沒有禁止模型自己換照片"
    assert "not_visible" in note and "不要**改用別張照片補" in note, (
        "沒有交代看不見的結構要照實標 missing——否則模型會偷偷換照片")
    assert "第 3 張" in note, "給人看的序號要從 1 開始"


def test_no_prefer_note_when_owner_did_not_tag_living():
    """沒有客廳標記時（單房／老 client）prompt 不得多出這段。"""
    for bad in (None, -1, 99, True):
        assert _build_prefer_note(bad, n_photos=2) == "", (
            f"prefer_index={bad!r} 不該注入指定段")


def test_zoning_endpoint_computes_prefer_before_calling_the_model():
    """🔴 prefer_idx 必須在 compute_zoning_v2 **之前**算好並傳進去。

    算在後面就只能改標籤，那正是被否決的做法。
    """
    i = API_SRC.index("zoning = compute_zoning_v2(")
    before = _code_only(API_SRC[max(0, i - 2200):i])
    assert "prefer_idx = None" in before, "prefer_idx 沒有在呼叫前算"
    # ⚠️ 不能只驗「有那幾行」——`if False:` 會讓整段變成死碼而測試照樣綠
    #    （2026-08-10 蓄意破壞抓到）。要驗那段真的會執行。
    assert 'if photo_meta_json:' in before, "屋主標記的解析被關掉了"
    assert 'if _z == "living"' in before, "沒有從屋主標記找出客廳那張"
    assert "if False" not in before, "解析被 if False 關掉了"
    call = API_SRC[i:i + 220]
    assert "prefer_index=prefer_idx" in call, "沒有把屋主指定傳進模型"


def test_model_disobedience_is_recorded_not_silently_relabelled():
    """模型仍改選別張時：沿用它的座標並記錄不一致，不得事後改標籤。"""
    i = API_SRC.index("_prefer_index_ignored")
    seg = _code_only(API_SRC[i - 700:i + 300])
    assert "沿用它並記錄不一致" in API_SRC[i - 700:i + 300], "沒有說明為什麼不改標籤"
    assert "requested" in seg and "model_chose" in seg, "沒有記下雙方的選擇"
    assert 'zoning["best_photo_index"] = prefer_idx' not in seg, (
        "事後把 best_photo_index 改成屋主那張——座標會對不上")

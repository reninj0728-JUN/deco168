# -*- coding: utf-8 -*-
"""客廳的幾何算在哪張，就畫在哪張——用 sha256 釘死，不比 index。

293BDE11 / 18D682A1 / 60F540A4（同一組照片，重跑三次，3/3 零客廳）的根因：

  客戶標記      photo_01 = 客廳、photo_02 = 餐廳
  zoning binding  **photo_02**（sha 180aa7a7…）
  客廳渲染底圖    **photo_01**

幾何綁 A、出圖用 B → `MISSING_PHOTO_BINDING` → S2 綁不上 → waive
→ zoom 拿 A 的 bbox 去裁 B → 天花板守門放棄裁切 → guide mode 留在 `auto_*`
→ 格局前檢「auto 客廳無 guide」擋死 → 客廳零圖。

為什麼 Gemini 選 photo_02 而不是客戶標的 photo_01：zoning prompt
（zoning_v2.py:59）要的是「最能同時看見大門落地處、入口地面、主要走道、
客廳地板與左右牆腳線」的照片。實際看過兩張原圖：photo_01 拍不到大門
（大門在鏡頭後面），photo_02 五項全中。**開放式客餐廳的房子，「客戶心中的
客廳」和「幾何算得出的照片」本來就可以不是同一張。**

⚠️ 方向是「底圖去就幾何」，不是反過來。座標焊死在 Gemini 選的那張上
   （zoning_v2.py:84-85「不可跨照片拼接座標」），事後改 binding 標籤＝拿另一張
   照片的門去擺這張的家具，比擋死更危險。

⚠️ 誠實的殘餘風險：這一刀只保證「幾何與出圖同一張」、消除 MISSING_PHOTO_BINDING。
   **不保證 S2 產得出 guide，也不保證客廳一定交付**——photo_02 上仍可能
   GEOM_NOT_ELIGIBLE。那種情況是誠實的擋，不是綁定錯亂，屬於另一個問題。

波及面（2026-08-07 線上實查）：58 單有 binding＋標記，55 單本來就同一張（no-op），
會被改到的只有這 3 單（同一個 upload），其中已交付客廳的是 0。
"""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import api
import pytest
from PIL import Image


def _photo(tmp_path, name, colour):
    p = tmp_path / name
    Image.new("RGB", (400, 300), colour).save(p, "JPEG", quality=92)
    return str(p)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _zoning(binding_path):
    return {"_source_binding": {"photo_key": f"uploads/JOB/{Path(binding_path).name}",
                                "sha256": _sha(binding_path)}}


def _meta(paths_zones):
    return {Path(p).name: {"photo_key": Path(p).name, "target_zone": z}
            for p, z in paths_zones}


@pytest.fixture
def shots(tmp_path):
    """重現 293 的形狀：photo_01 標客廳、photo_02 標餐廳、幾何綁 photo_02。"""
    p1 = _photo(tmp_path, "photo_01.jpg", (230, 228, 220))
    p2 = _photo(tmp_path, "photo_02.jpg", (200, 196, 188))
    p3 = _photo(tmp_path, "photo_03.jpg", (170, 168, 160))
    return p1, p2, p3


# ── ① 不一致時，客廳底圖改跟 binding ────────────────────────────────

def test_living_base_follows_the_geometry_binding(shots):
    """幾何那張**沒有被客戶指派給別間房**時，客廳底圖才跟著它走。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    # p2 沒被標任何房型 → 是「自由」的，可以拿來給客廳用
    meta = _meta([(p1, "living"), (p3, "bedroom")])
    regions = api._build_user_regions_whole(paths, meta, _zoning(p2))
    living = next(r for r in regions if r["room_type"] == "living")
    assert paths[living["best_photo_index"]] == p2, (
        "客廳底圖沒有跟著幾何綁定那張——MISSING_PHOTO_BINDING 會再發生")
    # 用 sha 再確認一次身分，不是靠 index 巧合
    assert _sha(paths[living["best_photo_index"]]) == _zoning(p2)["_source_binding"]["sha256"]


def test_customer_tag_wins_when_the_binding_photo_belongs_to_another_room(shots):
    """🔴 D4001755（2026-08-10）：客戶標 photo_01=客廳、photo_02=餐廳，
    幾何綁在 photo_02——第一版**無條件**改綁，結果客廳餐廳共用同一張照片，
    客戶標成客廳的那張整個沒被設計。

    錯的是優先級：客戶的明確標記被內部幾何規則蓋掉。
    fallback 可以存在，不能變成 default。
    """
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    meta = _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")])
    regions = api._build_user_regions_whole(paths, meta, _zoning(p2))
    living = next(r for r in regions if r["room_type"] == "living")
    dining = next(r for r in regions if r["room_type"] == "dining")
    assert paths[living["best_photo_index"]] == p1, (
        "客廳被改去用客戶標成餐廳的那張——客戶標的客廳沒被設計")
    assert paths[dining["best_photo_index"]] == p2
    assert living["best_photo_index"] != dining["best_photo_index"], (
        "客廳與餐廳共用同一張底圖")


def test_customer_tagged_photo_is_kept_as_an_alternate(shots):
    """改綁時客戶標的那張不能被丟掉——保真失敗時要能換回去。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    # 兩張都標客廳，幾何綁在 p2 → 改綁成立，p1 要退成備援
    regions = api._build_user_regions_whole(
        paths, _meta([(p1, "living"), (p2, "living"), (p3, "bedroom")]), _zoning(p2))
    living = next(r for r in regions if r["room_type"] == "living")
    assert paths[living["best_photo_index"]] == p2
    assert paths.index(p1) in living["alt_photo_indices"], "客戶標的客廳照片被丟掉了"


def test_binding_photo_need_not_be_tagged_living_but_must_be_unclaimed(shots):
    """🔴 binding 那張**不一定在客廳候選清單裡**——293 它就被標成「餐廳」。

    我第一版寫成「binding 必須已在客廳候選中」才接線，那條件在真實案例
    永遠不成立，等於這一刀完全沒作用。
    """
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    # p2 未被指派給任何房間 → 不在客廳候選裡，但仍可被改綁過去
    meta = _meta([(p1, "living"), (p3, "bedroom")])
    cands = api._list_room_photo_candidates(paths, meta, "living")
    assert paths.index(p2) not in [c["idx"] for c in cands], "前提變了：p2 已被當客廳候選"
    regions = api._build_user_regions_whole(paths, meta, _zoning(p2))
    living = next(r for r in regions if r["room_type"] == "living")
    assert paths[living["best_photo_index"]] == p2


# ── ② 已一致的 55 單：行為不得改變 ──────────────────────────────────

def test_already_consistent_orders_are_untouched(shots):
    """binding 本來就等於客戶標的客廳照 → 完全 no-op（線上 55/58 是這種）。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    meta = _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")])
    before = api._build_user_regions_whole(paths, meta, None)
    after = api._build_user_regions_whole(paths, meta, _zoning(p1))
    assert before == after, f"一致的單被動到了：{before} vs {after}"


def test_other_rooms_are_never_redirected(shots):
    """只動客廳。餐廳／臥室的底圖選擇不得被 binding 影響。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    meta = _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")])
    base = api._build_user_regions_whole(paths, meta, None)
    moved = api._build_user_regions_whole(paths, meta, _zoning(p2))
    for rt in ("dining", "bedroom"):
        b = next(r for r in base if r["room_type"] == rt)
        m = next(r for r in moved if r["room_type"] == rt)
        assert b == m, f"{rt} 被改到了：{b} vs {m}"


# ── ③ 沒有／無效 binding：維持現行邏輯，不猜 ────────────────────────

@pytest.mark.parametrize("zoning", [
    None,
    {},
    {"_source_binding": None},
    {"_source_binding": {}},
    {"_source_binding": {"photo_key": "uploads/JOB/x.jpg"}},          # 缺 sha
    {"_source_binding": {"photo_key": "uploads/JOB/x.jpg", "sha256": "zz"}},  # sha 不合法
    {"_source_binding": {"photo_key": "uploads/JOB/x.jpg",
                         "sha256": "0" * 64}},                        # sha 不屬於本次上傳
])
def test_missing_or_invalid_binding_keeps_current_behaviour(shots, zoning):
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    meta = _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")])
    expected = api._build_user_regions_whole(paths, meta, None)
    assert api._build_user_regions_whole(paths, meta, zoning) == expected, (
        "binding 無效時應維持現行邏輯，不得亂猜")


def test_binding_index_is_resolved_by_bytes_not_by_position(shots, tmp_path):
    """🔴 index 會因照片篩選／重排漂移，只有 sha 是證據。

    造一張「檔名一樣但內容不同」的照片：靠檔名或位置比對會誤判，比 sha 才對。
    """
    p1, p2, _p3 = shots
    decoy = tmp_path / "sub"
    decoy.mkdir()
    d = decoy / Path(p2).name          # 同檔名、不同內容
    Image.new("RGB", (400, 300), (10, 10, 10)).save(d, "JPEG", quality=92)
    paths = [p1, str(d)]
    meta = _meta([(p1, "living"), (str(d), "dining")])
    # binding 的 sha 是真正的 p2，不在 paths 裡 → 找不到 → 不得改
    expected = api._build_user_regions_whole(paths, meta, None)
    assert api._build_user_regions_whole(paths, meta, _zoning(p2)) == expected, (
        "靠檔名比對誤判成同一張了")


# ── ④ 接線本身：呼叫端真的把 zoning 傳進去 ──────────────────────────

def test_pipeline_actually_passes_zoning_into_region_builder():
    """🔴 函式改對但呼叫端沒傳＝整條線休眠，而單元測試照樣全綠。

    door_gap 守門那次就是「缺套件→守門靜默消失」，同一種失效形狀。
    """
    src = Path(api.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "_build_user_regions_whole(\n                    image_paths, photo_meta_by_key_early, user_zoning_v2)" in code \
        or "_build_user_regions_whole(image_paths, photo_meta_by_key_early, user_zoning_v2)" in code, (
        "run_pipeline 沒有把 user_zoning_v2 傳進 _build_user_regions_whole")


def test_region_builder_signature_takes_zoning():
    sig = inspect.signature(api._build_user_regions_whole)
    assert "zoning" in sig.parameters
    assert sig.parameters["zoning"].default is None, "zoning 必須可省略（維持舊呼叫相容）"


# ── ⑤ 最終效果：S2 contract photo 與客廳 render base 同一個檔案 ──────

def test_contract_photo_and_living_base_end_up_identical(shots):
    """S2 只在未裁切原圖上工作（api.py:6181 `_contract_photo`），
    所以客廳 region 選出來的那張，就是 S2 要綁的那張——兩者必須同一個檔案。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    z = _zoning(p2)
    # 幾何那張未被客戶指派給別間房 → 改綁成立，兩者必須是同一個檔案
    regions = api._build_user_regions_whole(
        paths, _meta([(p1, "living"), (p3, "bedroom")]), z)
    living_base = paths[next(r for r in regions if r["room_type"] == "living")["best_photo_index"]]
    # 這正是 _zoning_bbox_matches_source 在 pipeline 裡會做的判斷
    assert api._zoning_bbox_matches_source(living_base, paths, z) is True, (
        "客廳底圖仍然綁不上幾何——MISSING_PHOTO_BINDING 會再發生")

    # ⚠️ 幾何那張已被指派給餐廳時，客戶優先——這時綁不上是**預期**的，
    #    S2 會走 waive 退 legacy，不是回頭去搶客戶的照片（D4001755）。
    regions2 = api._build_user_regions_whole(
        paths, _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")]), z)
    base2 = paths[next(r for r in regions2 if r["room_type"] == "living")["best_photo_index"]]
    assert base2 == p1, "客戶標的客廳被搶走了"


def test_before_the_fix_the_binding_would_have_failed(shots):
    """反證：不接 binding（舊行為）時，客廳底圖確實綁不上——證明這條測試有效。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    z = _zoning(p2)
    regions = api._build_user_regions_whole(
        paths, _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")]), None)
    living_base = paths[next(r for r in regions if r["room_type"] == "living")["best_photo_index"]]
    assert living_base == p1
    assert api._zoning_bbox_matches_source(living_base, paths, z) is False


# ── ⑥ 客廳不在 index 0 時，無效 binding 不得把它拉走 ────────────────

def test_invalid_binding_does_not_default_to_index_zero(shots):
    """🔴 `_zoning_binding_photo_index` 找不到時必須回 None，不是 0。

    我第一版所有「無效 binding」的案例，客廳剛好都在 index 0，所以把 early return
    從 None 改成 0，17 條測試照樣全綠（2026-08-07 蓄意破壞抓到）。
    這裡把客廳放在 index 1，硬回 0 就會把底圖拉到別張照片上。
    """
    p1, p2, p3 = shots
    paths = [p1, p2, p3]                     # 客廳標在 p2 = index 1
    meta = _meta([(p1, "dining"), (p2, "living"), (p3, "bedroom")])
    expected = api._build_user_regions_whole(paths, meta, None)
    assert paths[next(r for r in expected if r["room_type"] == "living")
                 ["best_photo_index"]] == p2, "fixture 前提不成立"
    for bad in (None, {}, {"_source_binding": None}, {"_source_binding": {}},
                {"_source_binding": {"photo_key": "x.jpg", "sha256": "0" * 64}}):
        got = api._build_user_regions_whole(paths, meta, bad)
        assert got == expected, f"無效 binding（{bad}）把客廳底圖拉走了：{got}"


def test_binding_index_returns_none_not_zero_when_unresolvable(shots):
    """直接打那個函式：無法解析時只能回 None。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    for bad in (None, {}, {"_source_binding": None}, {"_source_binding": {}},
                {"_source_binding": {"photo_key": "x.jpg"}},
                {"_source_binding": {"photo_key": "x.jpg", "sha256": "0" * 64}}):
        assert api._zoning_binding_photo_index(paths, bad) is None, (
            f"{bad} 應該回 None（不猜），回了 {api._zoning_binding_photo_index(paths, bad)}")
    # 反貧血：解析得出來時要回正確的 index
    assert api._zoning_binding_photo_index(paths, _zoning(p2)) == 1


# ── ⑦ 重試階段不得換回綁不上幾何的照片 ──────────────────────────────

def test_alt_bases_exclude_photos_that_cannot_bind(shots):
    """🔴 初次生成綁對了，重試換底圖又換回去＝原地復發。

    `_switch_entry_to_next_living_base` 的保護只擋 `auto_*` **且有 guide**；
    S2 waive／無 guide 的路徑照樣會換底圖，而重試階段**不會重算 zoning**。
    換回客戶標的 photo_01 → 幾何仍算在 photo_02 → MISSING_PHOTO_BINDING 復發。
    Z3／Phase2／Phase3 三條重試路都吃這份清單。
    """
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    meta = _meta([(p1, "living"), (p2, "dining"), (p3, "bedroom")])
    alts = api._living_alt_base_paths(paths, meta, p2, _zoning(p2))
    assert p1 not in alts, "重試時會換回綁不上幾何的照片，MISSING_PHOTO_BINDING 會復發"
    assert all(api._zoning_bbox_matches_source(a, paths, _zoning(p2)) for a in alts)


def test_alt_bases_unchanged_when_no_binding(shots):
    """沒有 binding 的單：既有多底圖備援完全不受影響。"""
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    # 兩張都標客廳 → 本來就有備援
    meta = _meta([(p1, "living"), (p2, "living"), (p3, "bedroom")])
    assert api._living_alt_base_paths(paths, meta, p1, None) == [p2]
    assert api._living_alt_base_paths(paths, meta, p1, {}) == [p2]


def test_alt_bases_narrow_even_when_primary_was_already_consistent(shots):
    """⚠️ 名稱要準：初次底圖是 no-op，但**重試備援會變窄**。

    線上 55/58 單的 binding 本來就等於主底圖，所以主底圖不動（真 no-op）。
    但如果那些單還有第二張標成客廳的照片，而它綁不上幾何——以前保真失敗會換
    過去（換過去必然再失敗，白燒一次 fal），現在直接排除、改成同圖重試。
    方向是對的（停止無效重試），但這不叫「行為不變」，別用 no-op 描述它。
    """
    p1, p2, p3 = shots
    paths = [p1, p2, p3]
    meta = _meta([(p1, "living"), (p2, "living"), (p3, "bedroom")])
    # 幾何綁 p1＝主底圖；p2 也標客廳但綁不上 → 依規則排除
    alts = api._living_alt_base_paths(paths, meta, p1, _zoning(p1))
    assert alts == [], "綁不上幾何的備援沒有被排除"
    # 反貧血：不鎖定時它仍是合法備援
    assert api._living_alt_base_paths(paths, meta, p1, None) == [p2]


def test_alt_base_selection_is_a_named_function_not_inline():
    """🔴 這段原本 inline 在 run_pipeline，行為測試打不到——刪掉過濾也不會變紅。

    抽成具名函式之後才驗得到（shouldBuildUI 那次同款教訓）。
    """
    src = Path(api.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "_living_alt_paths = _living_alt_base_paths(" in code, (
        "run_pipeline 沒有走具名函式，過濾邏輯又回到 inline")
    # ⚠️ 只驗「有呼叫」不夠：呼叫端漏傳 zoning 時整個過濾靜默失效，
    #    而測試照樣全綠（2026-08-08 蓄意破壞抓到）。要驗**傳了什麼**。
    i = code.index("_living_alt_paths = _living_alt_base_paths(")
    call = code[i:i + 260]
    assert "user_zoning_v2" in call, (
        "呼叫端沒把 user_zoning_v2 傳給備援選擇——過濾會靜默失效")


def test_alt_photo_indices_is_a_record_only(shots):
    """`alt_photo_indices` 只被寫、沒有被讀（純紀錄客戶原本標了哪張）。

    真正驅動換底圖的是 `_alt_bases`／`_living_alt_base_paths`。
    這條測試鎖住這個事實：哪天有人開始讀它，就得回來看有沒有同樣的復發風險。
    """
    src = Path(api.__file__).read_text(encoding="utf-8")
    assert src.count("alt_photo_indices") == 1, (
        "alt_photo_indices 開始被讀了——它含客戶標的那張，可能繞不上幾何")

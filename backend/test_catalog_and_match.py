"""真正的 pytest 測試（無網路、秒級）：家具目錄健全性 + 9 風格 × 4 房型配對迴歸。

這是商業化的 CI 基線：任何人改了 furniture_match.py 或目錄資料，
跑 `pytest backend/ -q` 就能在 push 前抓到「配對爆掉 / 目錄壞掉」。
"""
import json
from pathlib import Path

import pytest

import furniture_match as fm

CATALOG_PATH = Path(__file__).parent / "furniture_catalog_real.json"
CURRENT_STYLES = ["modern", "cream", "nordic", "japanese", "wood", "luxury",
                  "french", "muji", "chinese-modern"]
ROOMS = ["living", "bedroom", "dining", "kitchen"]

REQUIRED_FIELDS = ["id", "name_zh", "category", "style_tags",
                   "price_twd", "image_url", "purchase_url"]


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_catalog_loads_and_size(catalog):
    assert len(catalog) >= 3000, f"目錄只剩 {len(catalog)} 件，疑似被覆寫（merge 腳本事故？）"


def test_catalog_schema(catalog):
    bad = [it.get("id", "?") for it in catalog
           if any(not it.get(f) for f in REQUIRED_FIELDS)]
    assert not bad, f"{len(bad)} 件缺必要欄位，例如: {bad[:5]}"


def test_no_duplicate_ids(catalog):
    ids = [it["id"] for it in catalog]
    assert len(ids) == len(set(ids)), "目錄有重複 id"


def test_urls_look_real(catalog):
    bad_img = [it["id"] for it in catalog if not str(it["image_url"]).startswith("http")]
    assert not bad_img, f"image_url 非 http: {bad_img[:5]}"
    homepages = {"https://www.momoshop.com.tw", "https://24h.pchome.com.tw",
                 "https://www.ikea.com.tw", "https://www.hola.com.tw",
                 "https://www.nitori-net.tw"}
    fake = [it["id"] for it in catalog
            if str(it["purchase_url"]).rstrip("/") in homepages]
    assert not fake, f"purchase_url 是首頁假連結: {fake[:5]}"


def test_only_current_styles(catalog):
    stray = {}
    for it in catalog:
        s = (it.get("style_tags") or [None])[0]
        if s not in CURRENT_STYLES:
            stray[s] = stray.get(s, 0) + 1
    assert not stray, f"有商品掛著已停售風格標籤（永遠配對不到）: {stray}"


def test_match_all_style_room_combos(catalog):
    """9 風格 × 4 房型 = 36 組合：不噴例外、不回空清單。"""
    fails, empties = [], []
    for style in CURRENT_STYLES:
        for room in ROOMS:
            prompt = f"{style} style {room} room with sofa bed table chair"
            try:
                items = fm.match_furniture(style, prompt, catalog, top_n=5, mode=room)
            except Exception as e:  # noqa: BLE001
                fails.append((style, room, f"{type(e).__name__}: {e}"))
                continue
            if not items:
                empties.append((style, room))
    assert not fails, f"配對噴例外: {fails}"
    assert not empties, f"配對回空清單: {empties}"


def test_no_consumable_junk_in_catalog(catalog):
    """耗材（貼膜/保護膜/貼紙/免洗墊）不是家具也不是軟裝，不准進目錄。
    20A8220A：簡體字「家具贴膜」被當法式茶几畫進渲染圖。"""
    consumable = ["貼膜", "贴膜", "保護膜", "保护膜", "貼紙", "贴纸",
                  "軟玻璃", "软玻璃", "免洗"]
    bad = [it["id"] for it in catalog
           if any(k in it["name_zh"] for k in consumable)]
    assert not bad, f"目錄混入耗材商品: {bad[:5]}"


def test_no_cloth_junk_in_furniture_categories(catalog):
    """桌布/餐墊/防燙類商品不准掛在家具本體類目（茶几/桌子/地毯…），
    只能是裝飾——否則會被當家具本體配對進客戶方案。"""
    cloth = ["桌布", "桌墊", "桌旗", "餐墊", "防燙", "防烫", "隔熱墊",
             "桌巾", "臺布", "台布"]
    furn_cats = ("茶几", "桌子", "地毯", "沙發", "床架", "收納", "椅子")
    bad = [(it["id"], it["category"]) for it in catalog
           if it["category"] in furn_cats
           and any(k in it["name_zh"] for k in cloth)]
    assert not bad, f"保護布/墊類商品掛在家具類目: {bad[:5]}"


def test_multi_piece_bundle_detection():
    """套組偵測：真套組要抓到、單件複合命名不能誤傷（20A8220A 集集客案例）。"""
    positives = [
        "集集客 客廳桌櫃組 岩板茶几電視櫃70+180（客廳桌 電視櫃組 子母桌 沙發桌",
        "深色木製方桌、長條桌、座椅套組",
        "茶几130cm+電視櫃180cm 客廳組合",
    ]
    negatives = [
        "組合式書桌 可調高度",
        "北歐風實木茶几",
        "L型沙發 三人座 貴妃椅",
        "現代極簡黑色長方形茶几",
    ]
    for name in positives:
        assert fm.is_multi_piece_bundle(name), f"漏抓套組: {name}"
    for name in negatives:
        assert not fm.is_multi_piece_bundle(name), f"誤傷單件: {name}"


def test_french_coffee_table_is_real_furniture(catalog):
    """20A8220A 迴歸：法式客廳的茶几配對結果必須是真家具，不能是貼膜/桌布/套組。"""
    prompt = "french style living room with curved sofa, elegant coffee table, rug"
    items = fm.match_furniture("french", prompt, catalog, top_n=5, mode="living")
    ct = next((it for it in items if fm.resolve_category(it) == "coffee_table"), None)
    assert ct is not None, "法式客廳配不到茶几"
    nm = ct["name_zh"]
    junk = ["貼膜", "贴膜", "桌布", "桌墊", "防燙", "防烫", "免洗"]
    assert not any(k in nm for k in junk), f"法式茶几配到垃圾商品: {nm}"
    assert not fm.is_multi_piece_bundle(nm), f"法式茶几配到多件套組: {nm}"


def test_spatial_fidelity_gate_logic():
    """2A520C25 迴歸：保真四訊號 → spatial_fidelity_fail 的純邏輯（不打 API）。"""
    import gemini_analyze as ga

    # 全部保留 → 不失守
    good = {"camera_axis_preserved": True, "main_window_region_match": True,
            "passage_openings_preserved": True, "offframe_room_invaded": False}
    fail, problems = ga.compute_spatial_fidelity(good)
    assert fail is False and problems == []

    # 法式那張：四訊號全紅 → 失守，四條原因都在
    bad = {"camera_axis_preserved": False, "main_window_region_match": False,
           "passage_openings_preserved": False, "offframe_room_invaded": True}
    fail, problems = ga.compute_spatial_fidelity(bad)
    assert fail is True and len(problems) == 4

    # 只走道門洞消失一項 → 也算失守
    one = {"camera_axis_preserved": True, "main_window_region_match": True,
           "passage_openings_preserved": False, "offframe_room_invaded": False}
    fail, problems = ga.compute_spatial_fidelity(one)
    assert fail is True and "原有走道門洞消失" in problems

    # 缺欄位 → 用保守預設（保留=True / 入侵=False）→ 不失守（缺欄的 fail-closed
    # 是在 validate_render 裡對 living 另外判，不在這個純函數）
    fail, problems = ga.compute_spatial_fidelity({})
    assert fail is False


def test_spatial_fidelity_is_hard_fail_flag():
    """spatial_fidelity_fail 必須在硬傷清單，否則閘門形同虛設。"""
    import gemini_analyze as ga
    assert "spatial_fidelity_fail" in ga.HARD_FAIL_FLAGS


def test_no_bunk_bed_in_bedroom(catalog):
    """9871F294 迴歸：臥室的床配對不能出現雙層床/兒童床（主臥不擺小孩房家具）。"""
    for style in CURRENT_STYLES:
        prompt = f"{style} style bedroom with double bed, wardrobe, soft lighting"
        items = fm.match_furniture(style, prompt, catalog, top_n=5, mode="bedroom")
        bed = next((it for it in items if fm.resolve_category(it) == "bed"), None)
        if bed is None:
            continue
        nm = bed["name_zh"]
        bad = [k for k in fm.BED_KIDS_KW if k in nm]
        assert not bad, f"{style} 臥室配到兒童/雙層床: {nm}（命中 {bad}）"


def test_must_have_categories_never_empty(catalog):
    """每個房型的 must-have 品類在每種風格下都配得到（跨風格保命網有效）。"""
    missing = []
    for style in CURRENT_STYLES:
        for room, rule in fm.ROOM_RULES.items():
            prompt = f"{style} style {room} room"
            items = fm.match_furniture(style, prompt, catalog, top_n=5, mode=room)
            got_cats = {fm.resolve_category(it) for it in items}
            for must_cat in rule["must"]:
                if must_cat not in got_cats:
                    missing.append((style, room, must_cat))
    assert not missing, f"must-have 品類缺配: {missing}"

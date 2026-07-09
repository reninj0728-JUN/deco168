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


def test_offframe_room_scrub():
    """2A520C25 拔管線：全屋 layout 文字裡的畫面外廚房不得進單房 prompt。"""
    import prompt_builder as pb
    # 真實案例：客廳牆面描述帶「通往廚房」→ 廚房必須被抹掉，開口/家具資訊保留
    out = pb._scrub_offframe_rooms("擺放金色大理石邊几的牆面，中段有通往廚房與臥室走道的開口")
    assert "廚房" not in out
    assert "金色大理石邊几" in out and "開口" in out
    # 空字串安全
    assert pb._scrub_offframe_rooms("") == ""
    # 沒有畫面外房間的描述原樣保留主要內容
    plain = pb._scrub_offframe_rooms("完整大白牆，掛花畫，無開口")
    assert "大白牆" in plain and "廚房" not in plain
    # 擴充黑名單：中島 / 英文 kitchen
    assert "中島" not in pb._scrub_offframe_rooms("客廳旁設中島與灶台")
    assert "kitchen" not in pb._scrub_offframe_rooms("open kitchen at the back").lower()


def test_layout_structure_whitelist_no_wall_prose():
    """結構段白名單：牆只輸出 solid/opening，不塞自由 description；不含 room_shape 廚房敘事。"""
    import prompt_builder as pb
    zoning = {
        "confidence": "high",
        "spatial_synthesis": {
            "room_shape": "長方形格局，後端延伸至廚房與大門",
            "main_window_wall": "畫面左側落地窗",
            "wall_inventory": [
                {
                    "name": "右側長實牆",
                    "description": "完整大白牆，中段有通往廚房的開口",
                    "has_opening": True,
                }
            ],
        },
        "zones": {"living_zone": {"where": "靠窗區域"}, "walkway": {"where": "側向走道"}},
        "furniture_placement_rules": {},
    }
    sec = pb._build_layout_section(zoning)
    assert "後端延伸至廚房" not in sec
    assert "廚房" not in sec
    assert "通往廚房的開口" not in sec  # description 不得進 prompt
    assert "has opening" in sec
    assert "Window location" in sec
    assert "FRAME BOUNDARY" in sec


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


def test_slot_name_guards_logic():
    """50873CF0 槽位鐵則純邏輯：品名宣告的主類型不可混進衝突槽位。"""
    # 客廳兩個電視櫃的根因：落地電視機櫃進了茶几槽
    assert fm.violates_slot_guard("coffee_table", "living", "新款輕奢高級落地電視機櫃")
    # 玄關七層鞋櫃不可當客廳電視櫃
    assert fm.violates_slot_guard("media_console", "living", "TZUMii 七層鞋櫃+電視櫃")
    # 邊几不可當主茶几
    assert fm.violates_slot_guard("coffee_table", "living", "雙層木質白色邊几")
    # 摺疊電腦桌不可當床頭 side_table
    assert fm.violates_slot_guard("side_table", "bedroom", "Style 100cm 摺疊書桌電腦桌")
    # L型電視中空櫃不可進主臥收納
    assert fm.violates_slot_guard("storage", "bedroom", "STYLEHOUSE 北歐法雪 9.7尺 L型電視中空櫃")
    # 合法品不誤殺
    assert not fm.violates_slot_guard("coffee_table", "living", "北歐風實木茶几")
    assert not fm.violates_slot_guard("media_console", "living", "文創集 瑪西法5尺二門二抽電視櫃")
    assert not fm.violates_slot_guard("storage", "living", "L型電視中空櫃")   # 客廳收納不禁電視櫃
    assert not fm.violates_slot_guard("side_table", "living", "U型腿胡桃木邊桌")


def test_no_tv_cabinet_in_coffee_table_slot(catalog):
    """50873CF0 端到端迴歸：任何風格的客廳茶几槽不得出現電視櫃/鞋櫃/邊几。"""
    for style in CURRENT_STYLES:
        prompt = f"{style} style living room with sofa, coffee table, rug, media console"
        items = fm.match_furniture(style, prompt, catalog, top_n=5, mode="living")
        ct = next((it for it in items if fm.resolve_category(it) == "coffee_table"), None)
        if ct is None:
            continue
        nm = ct["name_zh"]
        assert not fm.violates_slot_guard("coffee_table", "living", nm), \
            f"{style} 茶几槽配到衝突商品: {nm}"
        mc = next((it for it in items if fm.resolve_category(it) == "media_console"), None)
        if mc is not None:
            assert not fm.violates_slot_guard("media_console", "living", mc["name_zh"]), \
                f"{style} 電視櫃槽配到玄關櫃: {mc['name_zh']}"


def test_width_extraction_from_name():
    """50873CF0 尺寸感：尺/裸cm 只寫在品名也要能讀出來。"""
    # 台尺（dimensions 欄）
    assert fm._extract_width_cm("9.7尺") == 294
    assert fm._extract_width_cm("5尺") == 152
    # 品名 fallback（寬度關鍵品類）
    big = {"name_zh": "北歐法雪 9.7尺 L型電視中空櫃", "dimensions": "", "category": "收納"}
    w = fm.extract_item_width_cm(big)
    assert w is not None and w > 280, f"9.7尺 應解析為 ~294cm，得到 {w}"
    desk = {"name_zh": "亮面書桌 131CM", "dimensions": "", "category": "桌子"}
    assert fm.extract_item_width_cm(desk) == 131
    # 非寬度關鍵品類（窗簾）名字裡的數字不可當寬度
    curt = {"name_zh": "遮光窗簾 270cm", "dimensions": "", "category": "窗簾"}
    assert fm.extract_item_width_cm(curt) is None


def test_sheer_curtain_double_ref():
    """50873CF0 雙層窗簾：厚簾+紗簾要各佔一個參考圖名額；同類第三件不收。"""
    import prompt_builder as pb
    soft = [
        {"category_en": "curtain", "image_url": "http://x/1.jpg", "name_zh": "純色簡約遮光窗簾"},
        {"category_en": "curtain", "image_url": "http://x/2.jpg", "name_zh": "白色輕薄紗簾"},
        {"category_en": "curtain", "image_url": "http://x/3.jpg", "name_zh": "第二件遮光簾"},
        {"category_en": "lighting", "image_url": "http://x/4.jpg", "name_zh": "桌燈"},
    ]
    picked = pb._select_soft_ref_candidates(soft)
    names = [p["name_zh"] for p in picked]
    assert "純色簡約遮光窗簾" in names and "白色輕薄紗簾" in names, f"雙層窗簾沒都進: {names}"
    assert "第二件遮光簾" not in names
    # 只有一件窗簾時行為不變（每類一件）
    picked2 = pb._select_soft_ref_candidates(soft[:1] + soft[3:])
    assert len([p for p in picked2 if p["category_en"] == "curtain"]) == 1

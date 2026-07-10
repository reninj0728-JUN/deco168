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


# ══ B1/A1/C 一次全修（B0CDF6A0）迴歸 ═══════════════════════════════════════

def test_json_repair_inner_quotes():
    """B1：字串內未跳脫引號（B0CDF6A0 兩張客廳驗證崩的實際型態）要能修復。"""
    import gemini_analyze as ga
    broken = '{"ok": false, "reason": "沙發"正面"朝向走道", "walls_changed": false}'
    out = ga._json_loads_lenient(broken)
    assert out["ok"] is False and out["walls_changed"] is False
    assert "正面" in out["reason"]


def test_json_repair_truncated_and_trailing_comma():
    """B1：截斷缺右括號、尾逗號都要能修。"""
    import gemini_analyze as ga
    out = ga._json_loads_lenient('{"ok": true, "flags": [1, 2,], }')
    assert out["ok"] is True
    out2 = ga._json_loads_lenient('{"ok": true, "reason": "被截斷了')
    assert out2["ok"] is True


def test_living_validation_fail_closed():
    """B1：客廳驗證崩潰（ok=None）不得當通過——hard_fail 進補生；非客廳保留標記不阻斷。"""
    import api
    v = api._fail_closed_validation({"ok": None, "error": "boom"}, "living")
    assert v["hard_fail"] is True and v["ok"] is False and v["validation_unavailable"] is True
    v2 = api._fail_closed_validation({"ok": None, "error": "boom"}, "bedroom")
    assert v2.get("hard_fail") is not True and v2["validation_unavailable"] is True
    # 正常結果原樣通過
    v3 = api._fail_closed_validation({"ok": True, "hard_fail": False}, "living")
    assert v3["ok"] is True and not v3.get("validation_unavailable")


def test_face_gate_without_user_zoning():
    """A1：沒有用戶確認分區時，face=false 也必須是硬傷（北歐電視櫃跑餐廳位）。"""
    import gemini_analyze as ga
    r = ga._enforce_sofa_focal_orientation(
        {"sofa_focal_face_each_other": False}, has_layout_ctx=False)
    assert r["focal_anchor_misaligned_with_sofa"] is True
    assert r["ok"] is False
    # 無 ctx 且模型答不出（None）→ 不硬扣（避免誤殺）
    r2 = ga._enforce_sofa_focal_orientation(
        {"sofa_focal_face_each_other": None}, has_layout_ctx=False)
    assert not r2.get("focal_anchor_misaligned_with_sofa")


def test_weak_contract_without_confirmation():
    """A1：無用戶確認但系統 zoning 有 living zone → prompt 出現弱合約；有確認則不出現弱合約字樣。"""
    import prompt_builder as pb
    zoning = {
        "confidence": "high",
        "spatial_synthesis": {"main_window_wall": "畫面左側落地窗"},
        "zones": {"living_zone": {"where": "靠窗前段區域"}, "walkway": {"where": "側向走道"}},
        "furniture_placement_rules": {},
    }
    sec = pb._build_layout_section(zoning)
    assert "SYSTEM-INFERRED LAYOUT" in sec
    assert "FACING the sofa" in sec
    confirmed = dict(zoning)
    confirmed["_origin"] = "user_confirmed_v2"
    sec2 = pb._build_layout_section(confirmed)
    assert "SYSTEM-INFERRED LAYOUT" not in sec2  # 用戶確認路徑行為完全不變


def test_product_visibility_hard_flag():
    """C：product_visibility_fail 必須在硬傷清單（否則可見性形同虛設）。"""
    import gemini_analyze as ga
    assert "product_visibility_fail" in ga.HARD_FAIL_FLAGS


def test_door_on_tv_wall_protocol():
    """1A3B0C68 迴歸：大門與電視同側牆 → 生成 prompt 必須帶避門協議；不同側則不帶。"""
    import prompt_builder as pb
    base = {
        "_origin": "user_confirmed_v2",
        "confidence": "high",
        "_layout_choice": "A",
        "spatial_synthesis": {"entrance_position": "主視角左前方（深色金邊大門）"},
        "zones": {"living_zone": {"where": "前端客廳區"}, "walkway": {"where": "中央"}},
        "furniture_placement_rules": {"sofa_side": "right", "tv_side": "left"},
    }
    sec = pb._build_layout_section(base)
    assert "DOOR-ON-TV-WALL PROTOCOL" in sec
    # 門在右、電視在左 → 不同牆，不觸發
    other = {**base, "spatial_synthesis": {"entrance_position": "主視角右前方"}}
    sec2 = pb._build_layout_section(other)
    assert "DOOR-ON-TV-WALL PROTOCOL" not in sec2


def test_sofa_facing_entrance_is_hard():
    """1A3B0C68：沙發視線對大門必須是硬傷 + retry 有專屬修正指令。"""
    import gemini_analyze as ga
    import prompt_builder as pb
    assert "sofa_facing_entrance_door" in ga.HARD_FAIL_FLAGS
    assert "sofa_facing_entrance_door" in pb._RETRY_FLAG_FIX_EN


def test_budget_band_delta():
    """刀1（預算靠攏）純邏輯：帶內加分、遠低扣分、tier3 無帶、幅度小於風格分。"""
    assert fm.budget_band_delta("tier2", "rug", 8000) == fm.BUDGET_BAND_BONUS      # 帶內
    assert fm.budget_band_delta("tier2", "rug", 599) == fm.BUDGET_BAND_FAR_BELOW_PENALTY  # 遠低
    assert fm.budget_band_delta("tier2", "rug", 3000) == fm.BUDGET_BAND_BELOW_PENALTY     # 略低
    assert fm.budget_band_delta("tier2", "rug", 99999) == 0.0   # 超帶頂交給 CAP，不重複罰
    assert fm.budget_band_delta("tier3", "rug", 599) == 0.0     # tier3 無帶
    assert fm.budget_band_delta("tier2", "rug", None) == 0.0    # 沒標價不罰
    # 幅度必須壓在風格分(+3)之下，避免預算分反客為主
    assert abs(fm.BUDGET_BAND_BONUS) < 3 and abs(fm.BUDGET_BAND_FAR_BELOW_PENALTY) < 3


def test_tier2_totals_land_in_band(catalog):
    """刀1 端到端：tier2 全室三房主家具合計應 >= 10萬（1A3B0C68 曾配出 7.4萬）。"""
    for style in ("nordic", "muji"):
        total = 0
        for room in ("living", "bedroom", "study"):
            items = fm.match_furniture(style, f"{style} style {room}", catalog,
                                       top_n=5, mode=room, budget_tier="tier2")
            total += sum(int(it.get("price_twd") or 0) for it in items)
        assert total >= 100000, f"{style} tier2 三房合計僅 NT${total:,}，低於 10萬下限"


def test_related_styles_symmetric_union():
    """相近風格必須取「所有群組聯集」且對稱——舊 first-match 寫法讓 muji
    借不到 wood/japanese（素橡木品被視覺改標 wood 後 fallback 撈空）。"""
    rel_muji = fm._get_related_styles("muji")
    assert "wood" in rel_muji and "japanese" in rel_muji and "nordic" in rel_muji
    # 對稱性：a 的相近含 b ⇔ b 的相近含 a
    all_styles = ["modern", "nordic", "japanese", "muji", "luxury", "art-deco",
                  "boho", "industrial", "cream", "french", "chinese-modern", "wood"]
    for a in all_styles:
        for b in fm._get_related_styles(a):
            assert a in fm._get_related_styles(b), f"{a}->{b} 相近不對稱"


def test_study_storage_slot_guard():
    """6F1BFC19：書房 storage 槽必須擋電視櫃/玄關櫃（跟 bedroom 一致）。"""
    assert fm.violates_slot_guard("storage", "study", "STYLEHOUSE 北歐法雪9.7尺L型電視中空櫃DU10")
    assert fm.violates_slot_guard("storage", "study", "玄關櫃 雙門鞋櫃")
    assert not fm.violates_slot_guard("storage", "study", "五層開放式書櫃 橡木色")
    assert fm.violates_slot_guard("storage", "bedroom", "岩板電視櫃")  # 原有行為不動


def test_blocks_door_hard_and_retry_wired():
    """6F1BFC19：家具擋門/貼門旁必須是硬傷且 retry 有專屬修正指令；
    驗收條文必須含「開啟弧形/一個門寬」淨空判定。"""
    import gemini_analyze as ga
    import prompt_builder as pb
    assert "furniture_blocks_door" in ga.HARD_FAIL_FLAGS
    assert "furniture_blocks_door" in pb._RETRY_FLAG_FIX_EN


def test_product_visibility_covers_all_rooms():
    """6F1BFC19 客戶鐵則：購物清單=渲染圖——可見性 must_products 不再只蓋客廳。
    書房收納櫃清單有、圖上畫成第二張書桌，以前這裡直接跳過不檢。"""
    import api
    study_entry = {
        "room_type": "study",
        "matched_furniture": [
            {"category_en": "table",   "name_zh": "實木簡約書桌", "flux_descriptor": "oak desk"},
            {"category_en": "chair",   "name_zh": "原木繩編方凳", "flux_descriptor": "stool"},
            {"category_en": "storage", "name_zh": "輕奢收納櫃",   "flux_descriptor": "cabinet"},
            {"category_en": "storage", "name_zh": "第二件收納",   "flux_descriptor": "dup"},
            {"category_en": "rug",     "name_zh": "綠色地毯",     "flux_descriptor": "rug"},
        ],
    }
    ctx = api._product_fidelity_into_layout_ctx(None, study_entry)
    cats = [p["cat"] for p in ctx["must_products"]]
    assert cats == ["table", "chair", "storage", "rug"]  # 同 cat 去重、順序照清單
    # 客廳原行為不變：sofa/coffee_table/rug/media_console 都在
    living_entry = {
        "room_type": "living",
        "matched_furniture": [
            {"category_en": c, "name_zh": c, "flux_descriptor": c}
            for c in ("sofa", "coffee_table", "rug", "media_console")
        ],
    }
    ctx2 = api._product_fidelity_into_layout_ctx(None, living_entry)
    assert [p["cat"] for p in ctx2["must_products"]] == ["sofa", "coffee_table", "rug", "media_console"]


def test_visibility_tiering_nice_items_dont_kill_render():
    """46F1B2B5 誤擋教訓：可見性分級——must 缺漏才硬傷；加分品項(燈具/單椅)
    沒入圖只記 visibility_nice_bad，交付層從清單移除該品項、不殺圖。"""
    import inspect
    import gemini_analyze as ga
    import api
    src = inspect.getsource(ga.validate_render)
    assert "visibility_nice_bad" in src
    assert "_pv_bad_must" in src          # fail 只由 must 品項觸發
    # 交付層有清單移除邏輯
    import io, tokenize
    api_src = open(api.__file__, encoding="utf-8").read()
    assert "visibility_nice_bad" in api_src and "自清單移除" in api_src


def test_video_processing_wait_is_bounded():
    """D50FC472 根治：影片 PROCESSING 等待必須有上限——照片+影片路徑逾時退回
    純照片；純影片路徑逾時明確報錯。不准無限迴圈吊死整單。"""
    import inspect
    import gemini_analyze as ga
    import test_full_pipeline as tfp
    src_pipeline = inspect.getsource(tfp.analyze_image)
    assert "PROCESSING 超過 120s" in src_pipeline   # 退回純照片
    src_space = inspect.getsource(ga.analyze_space)
    assert "PROCESSING 超過 180s" in src_space       # 明確逾時報錯


def test_door_adjacency_geometry():
    """BB034AB8 根治：門邊淨空改幾何量測——判官 bbox 準但布林會漏答，
    程式直接算重疊/間距，抓到強制 blocks_door 進重試鏈。"""
    import gemini_analyze as ga
    # 北歐實測 bbox：高櫃(focal x94-369) 與門(約 x80-250) 重疊 → 違規
    rb_nordic = {"entrance_door": [300, 80, 860, 250],
                 "focal_anchor": [422, 94, 840, 369],
                 "sofa": [512, 625, 906, 981]}
    v = ga._door_adjacency_violation(rb_nordic)
    assert v and v[0] == "focal_anchor"
    # 無印實測：櫃 x262 起、門 xmax~240 → 間距 22 < 0.25*180 → 違規
    rb_muji = {"entrance_door": [330, 60, 830, 240],
               "focal_anchor": [513, 262, 674, 381],
               "sofa": [482, 584, 826, 856]}
    assert ga._door_adjacency_violation(rb_muji)
    # 合格構圖：櫃離門 0.5 門寬以上 → 放行
    rb_ok = {"entrance_door": [330, 60, 830, 240],
             "focal_anchor": [500, 360, 700, 560],
             "sofa": [480, 700, 850, 980]}
    assert ga._door_adjacency_violation(rb_ok) is None
    # 沒標到門 bbox → 不誤判（退回判官布林）
    assert ga._door_adjacency_violation({"focal_anchor": [1, 1, 9, 9]}) is None


def test_door_gap_retry_carries_measurement():
    """FE964758：擋門重試必須帶量測數字（差多少門寬、該放哪半段），
    且憑空窗簾要進 windows_changed 定義、生成鐵則禁止實牆掛簾。"""
    import inspect
    import prompt_builder as pb
    import gemini_analyze as ga
    sec = pb._build_retry_context_section(
        {"failed_flags": ["furniture_blocks_door"],
         "door_gap": {"target": "focal_anchor", "gap": 15, "door_w": 145}},
        room_type="living")
    assert "MEASURED VIOLATION" in sec and "0.1 door-widths" in sec
    # 非客廳不得注入沙發/櫃指令
    sec2 = pb._build_retry_context_section(
        {"door_gap": {"target": "focal_anchor", "gap": 15, "door_w": 145},
         "reason": "x"}, room_type="bedroom")
    assert "MEASURED VIOLATION" not in sec2
    assert "fakes a" in pb.CRITICAL_RULES        # 生成端禁憑空窗簾
    src = inspect.getsource(ga.validate_render)
    assert "憑空窗簾" in src                      # 驗收端定義已含

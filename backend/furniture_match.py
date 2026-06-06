"""
傢俱配對模組
輸入：Gemini 分析的 renders[] (含 flux_prompt)
輸出：每個 render 附帶 3~5 件真實傢俱推薦 + 產品圖片 URL
"""
import json
import os
import re
from pathlib import Path

# 真實商品目錄（momo 爬取，有真實圖片和購買連結）
CATALOG_REAL_PATH = Path(__file__).parent / "furniture_catalog_real.json"
# 舊目錄作為備用（AI 生成，無真實圖片）
CATALOG_FALLBACK_PATH = Path(__file__).parent / "furniture_catalog.json"

# 中文類別 → 英文類別對照（配合 CATEGORY_KEYWORDS）
# 茶几獨立成 coffee_table（不再跟餐桌混在 table）
# 桌子保守處理為 table（不細分餐桌/邊几，避免影響其他空間邏輯）
# 椅子細分由 refine_subcategory() 按 name_zh 處理，bucket 仍是 chair
CATEGORY_ZH_TO_EN = {
    '沙發': 'sofa',
    '茶几': 'coffee_table',
    '桌子': 'table',
    '椅子': 'chair',
    '床架': 'bed',
    '收納': 'storage',
    '燈具': 'lighting',
    '地毯': 'rug',
    '窗簾': 'curtain',
    '裝飾': 'mirror',
    '抱枕': 'pillow',
    '寢具': 'bedding',
    '傢俱': 'other',
}

# ── 客廳模式品類規則 ──
# 必撈（fallback 只能在同 category 內換相近風格，不准跨 category 替代）
LIVING_MUST_HAVE = ['sofa', 'coffee_table', 'rug']

# 加分（補滿 top_n 用，同類各取 1）
LIVING_NICE_TO_HAVE = [
    'accent_chair', 'lighting', 'curtain',
    'side_table', 'plant', 'mirror', 'chair',
]

# 客廳模式排除（不准進主家具清單）
LIVING_EXCLUDED = ['bar_stool', 'dining_chair', 'dining_table', 'bed', 'bedding']

# 椅子細分關鍵字（name_zh 命中即覆蓋 chair 為子類）
CHAIR_SUBCAT_RULES = [
    ('bar_stool', ['吧台', '吧檯', '吧椅', '高腳椅', 'bar stool', 'STIG', 'barstool']),
    ('dining_chair', ['餐椅', 'dining chair']),
    ('accent_chair', ['單人沙發', '單椅', '搖椅', '躺椅', '休閒椅', 'lounge', 'accent chair', '皮革椅', '扶手椅', 'armchair']),
]

# 桌子細分（保守：只在 LIVING_EXCLUDED 過濾與 NICE_TO_HAVE 配對時才用）
TABLE_SUBCAT_RULES = [
    ('dining_table', ['餐桌', 'dining table']),
    ('side_table', ['邊几', '邊桌', '床頭櫃', 'side table', '小茶桌', '角几']),
]


def refine_subcategory(en_cat: str, name_zh: str) -> str:
    """按品名細分 chair / table，其他類別維持原樣"""
    name_lower = (name_zh or '').lower()
    if en_cat == 'chair':
        for sub, kws in CHAIR_SUBCAT_RULES:
            if any(kw.lower() in name_lower for kw in kws):
                return sub
        return 'chair'
    if en_cat == 'table':
        for sub, kws in TABLE_SUBCAT_RULES:
            if any(kw.lower() in name_lower for kw in kws):
                return sub
        return 'table'
    return en_cat


def resolve_category(item: dict) -> str:
    """取得家具最終解析後的英文類別（含細分）"""
    raw = item.get('category', '')
    en_cat = CATEGORY_ZH_TO_EN.get(raw, raw.lower() if isinstance(raw, str) else 'other')
    return refine_subcategory(en_cat, item.get('name_zh', ''))

# 家具類別關鍵字 → category
CATEGORY_KEYWORDS = {
    "sofa":     ["sofa", "couch", "沙發", "loveseat", "sectional"],
    "chair":    ["chair", "stool", "椅", "seat", "armchair", "recliner"],
    "table":    ["table", "desk", "桌", "coffee table", "dining table", "side table"],
    "bed":      ["bed", "bed frame", "床", "headboard", "platform bed"],
    "shelving": ["shelf", "shelving", "shelves", "bookcase", "架", "收納架", "書架"],
    "storage":  ["storage", "cabinet", "dresser", "收納", "抽屜", "衣櫃", "buffet"],
    "lighting": ["lamp", "pendant", "sconce", "燈", "lighting", "chandelier", "lantern"],
    "rug":      ["rug", "carpet", "mat", "地毯", "kilim"],
    "curtain":  ["curtain", "drape", "blind", "窗簾"],
    "mirror":   ["mirror", "鏡"],
    "plant":    ["plant", "pot", "planter", "植物", "花器", "盆栽"],
}


def load_catalog() -> list[dict]:
    """載入傢俱目錄：優先用真實商品，不足時補舊目錄"""
    catalog = []
    if CATALOG_REAL_PATH.exists():
        with open(CATALOG_REAL_PATH, "r", encoding="utf-8") as f:
            catalog = json.load(f)
        print(f"[furniture_match] 真實目錄: {len(catalog)} 件 (momo)")
    if not catalog and CATALOG_FALLBACK_PATH.exists():
        with open(CATALOG_FALLBACK_PATH, "r", encoding="utf-8") as f:
            catalog = json.load(f)
        print(f"[furniture_match] 備用目錄: {len(catalog)} 件 (AI生成)")
    return catalog


def extract_categories_from_prompt(flux_prompt: str) -> list[str]:
    """從 flux_prompt 關鍵字提取需要的家具類別"""
    prompt_lower = flux_prompt.lower()
    found = []
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            found.append(cat)
    return found or ["sofa", "table", "lighting"]  # 默認推薦三大類


# ── 長條型客廳：擺位敏感沙發降權規則（root-cause fix for L-sofa-in-narrow-room）──
# aspect_ratio = max(L,W) / min(L,W)
# 跨所有風格，不只 nordic
LONG_ROOM_ASPECT_THRESHOLD = 2.0      # >= 2.0 才觸發
LONG_ROOM_SHAPE_PENALTY    = -5.0     # 大降權但不硬排除（catalog 無直線替代時仍可保命）
LONG_ROOM_BAD_SHAPE_KW = [
    # L 型
    "l型", "l形", "l 型", "l 形", "l-shape", "l shape", "l shaped",
    # U 型
    "u型", "u形", "u 型", "u 形", "u-shape", "u shape", "u shaped",
    # 沙發床
    "沙發床", "sofa bed", "sofabed", "sleeper sofa",
    # 貴妃沙發 / chaise（非對稱單側延伸，本質與 L 沙發同類問題）
    "貴妃", "貴妃椅", "貴妃座", "chaise", "chaise lounge", "chaise longue",
    # 過深沙發（次要，加分排除）
    "加深", "超深",
]


def compute_room_aspect_ratio(room_dims: dict | None) -> float:
    """從 analysis.room_dimensions 算長寬比 = max(L,W)/min(L,W)。沒資料回 0。"""
    if not isinstance(room_dims, dict):
        return 0.0
    try:
        L = float(room_dims.get("length_m") or 0)
        W = float(room_dims.get("width_m") or 0)
    except (TypeError, ValueError):
        return 0.0
    if L <= 0 or W <= 0:
        return 0.0
    return max(L, W) / min(L, W)


def _long_room_sofa_penalty(item: dict, is_long_room: bool) -> float:
    """
    長條型客廳對「擺位敏感沙發」降權：
      L 型 / U 型 / 沙發床 → -5 分
    不影響其他家具（茶几/地毯/燈等）。
    回 0 或 LONG_ROOM_SHAPE_PENALTY。
    """
    if not is_long_room:
        return 0.0
    if resolve_category(item) != "sofa":
        return 0.0
    name = (item.get("name_zh") or "").lower()
    descriptor = (item.get("flux_descriptor") or "").lower()
    blob = name + " " + descriptor
    for kw in LONG_ROOM_BAD_SHAPE_KW:
        if kw in blob:
            return LONG_ROOM_SHAPE_PENALTY
    return 0.0


def score_item(item: dict, style: str, prompt_keywords: list[str],
               match_style: bool = True, is_long_room: bool = False) -> float:
    """
    評分一件家具（不含類別加分，類別由外層篩選控制）

    - 風格 tag 命中 +3，相近風格 +1（match_style=False 時跳過，用於 fallback）
    - flux_prompt 關鍵字命中（descriptor 或 keywords 或 name）+1/個
    - 顏色命中 +0.5/個
    - 有圖片 +1，有購買連結 +0.5
    - 長條型客廳 + L/U/沙發床 → -5（避免 Nano Banana 跟著 ref 圖做 L 形擋走道）
    """
    score = 0.0

    if match_style:
        item_styles = item.get("style_tags", [])
        if style in item_styles:
            score += 3
        elif any(s in item_styles for s in _get_related_styles(style)):
            score += 1

    item_keywords = [kw.lower() for kw in item.get("keywords", [])]
    item_descriptor = (item.get("flux_descriptor") or "").lower()
    item_name = (item.get("name_zh") or "").lower()
    search_text = item_descriptor or item_name
    for pkw in prompt_keywords:
        if pkw and (any(pkw in ikw for ikw in item_keywords) or pkw in search_text):
            score += 1

    item_colors = [c.lower() for c in item.get("colors", [])]
    for pkw in prompt_keywords:
        if pkw and any(pkw in color for color in item_colors):
            score += 0.5

    if item.get("image_url"):
        score += 1
    if item.get("purchase_url"):
        score += 0.5

    # 長條型客廳沙發形狀降權
    score += _long_room_sofa_penalty(item, is_long_room)

    return score


def _pick_best_in_category(
    target_cat: str,
    style: str,
    prompt_keywords: list[str],
    catalog: list[dict],
    is_long_room: bool = False,
) -> dict | None:
    """
    在指定 category 中，先撈同風格 → 再 fallback 相近風格 → 否則 None。
    Fallback 只放寬風格，category 鎖死（不准用 chair 替代 sofa）。
    is_long_room=True 時，sofa 撈取會對 L/U/沙發床降權（降權不硬排除，保命撈直線）。
    """
    # Stage A: 嚴格同風格
    primary = [
        it for it in catalog
        if resolve_category(it) == target_cat
        and style in it.get("style_tags", [])
    ]
    if primary:
        scored = [(score_item(it, style, prompt_keywords, is_long_room=is_long_room), it) for it in primary]
        scored.sort(key=lambda x: -x[0])
        return scored[0][1]

    # Stage B: fallback 到相近風格（同 category）
    related = _get_related_styles(style)
    if related:
        fallback = [
            it for it in catalog
            if resolve_category(it) == target_cat
            and any(s in it.get("style_tags", []) for s in related)
        ]
        if fallback:
            scored = [(score_item(it, style, prompt_keywords, match_style=False, is_long_room=is_long_room), it) for it in fallback]
            scored.sort(key=lambda x: -x[0])
            return scored[0][1]

    return None


def match_furniture(
    style: str,
    flux_prompt: str,
    catalog: list[dict],
    top_n: int = 5,
    mode: str = 'living',
    is_long_room: bool = False,
) -> list[dict]:
    """
    兩階段配對（mode='living' 預設）：
      Stage 1: LIVING_MUST_HAVE 每類保證撈 1 件（同風格 → fallback 風格）
      Stage 2: LIVING_NICE_TO_HAVE 補滿 top_n（依分數）
      全程排除 LIVING_EXCLUDED

    is_long_room=True 時，sofa 撈取會避開 L/U/沙發床（跨所有風格）

    其他 mode 暫沿用「全分類混評分 + 每類 1 件」舊邏輯（未來再擴）
    """
    prompt_keywords = [kw.strip().lower() for kw in flux_prompt.split(",")]

    if mode != 'living':
        return _legacy_match(style, prompt_keywords, catalog, top_n)

    must = LIVING_MUST_HAVE
    nice = LIVING_NICE_TO_HAVE
    excluded = set(LIVING_EXCLUDED)

    # 先剔除 EXCLUDED 品類
    pool = [it for it in catalog if resolve_category(it) not in excluded]

    selected_by_cat: dict[str, dict] = {}

    # Stage 1: MUST_HAVE
    for cat in must:
        best = _pick_best_in_category(cat, style, prompt_keywords, pool, is_long_room=is_long_room)
        if best is not None:
            selected_by_cat[cat] = best

    # Stage 2: NICE_TO_HAVE
    remaining = top_n - len(selected_by_cat)
    if remaining > 0:
        nice_pool = [
            it for it in pool
            if resolve_category(it) in nice
            and resolve_category(it) not in selected_by_cat
        ]
        scored = [(score_item(it, style, prompt_keywords, is_long_room=is_long_room), it) for it in nice_pool]
        scored.sort(key=lambda x: -x[0])
        for _, it in scored:
            cat = resolve_category(it)
            if cat in selected_by_cat:
                continue
            selected_by_cat[cat] = it
            if len(selected_by_cat) >= top_n:
                break

    # 以 must 順序優先，nice 依後續加入順序
    ordered: list[dict] = []
    for cat in must:
        if cat in selected_by_cat:
            ordered.append(selected_by_cat[cat])
    for cat, it in selected_by_cat.items():
        if cat not in must:
            ordered.append(it)

    return ordered


def _legacy_match(style: str, prompt_keywords: list[str], catalog: list[dict], top_n: int) -> list[dict]:
    """舊邏輯（非 living 模式 fallback 用）：混評分 + 每類 1 件"""
    scored = []
    for item in catalog:
        sc = score_item(item, style, prompt_keywords)
        if sc > 0:
            scored.append((sc, item))
    scored.sort(key=lambda x: -x[0])
    selected = []
    cat_seen: set[str] = set()
    for _, item in scored:
        cat = item.get("category", "other")
        if cat in cat_seen:
            continue
        cat_seen.add(cat)
        selected.append(item)
        if len(selected) >= top_n:
            break
    return selected


def _get_related_styles(style: str) -> list[str]:
    """風格相近群組，用於寬鬆配對"""
    groups = [
        ["modern", "muji", "nordic", "cream"],
        ["japanese", "muji", "wood"],
        ["luxury", "art-deco", "french"],
        ["cream", "nordic", "muji", "wood"],
        ["wood", "japanese", "muji", "nordic"],
        ["french", "luxury", "art-deco"],
        ["chinese-modern", "luxury"],
        # 舊風格保留相性（catalog 舊品項仍有這些 tag）
        ["boho", "wood"],
        ["industrial", "modern"],
    ]
    for group in groups:
        if style in group:
            return [s for s in group if s != style]
    return []


def parse_max_width_cm(estimated_size: str, room_dims: dict | None = None) -> int:
    """
    根據坪數或實際尺寸，返回家具最大允許寬度（cm）。
    避免 8 坪小空間配到 W210cm 大沙發。
    """
    if room_dims:
        width_m = room_dims.get("width_m", 0)
        length_m = room_dims.get("length_m", 0)
        short_side = min(width_m, length_m) if width_m and length_m else 0
        if short_side > 0:
            # 家具寬度最多不超過短邊 55%
            return int(short_side * 100 * 0.55)

    # 從坪數字串提取數字範圍的下限
    import re
    nums = re.findall(r'\d+', str(estimated_size))
    if nums:
        sqping = int(nums[0])  # 取下限
        sqm = sqping * 3.305
        short_side_est = (sqm ** 0.5) * 0.85  # 估算短邊
        return int(short_side_est * 100 * 0.55)
    return 300  # 無法判斷時不過濾


def filter_by_dimensions(items: list[dict], max_width_cm: int) -> list[dict]:
    """過濾掉寬度超過 max_width_cm 的家具"""
    if max_width_cm >= 280:  # 大空間不過濾
        return items
    result = []
    for item in items:
        dims = item.get("dimensions", "")
        import re
        w_match = re.search(r'W(\d+)', dims)
        if w_match:
            w = int(w_match.group(1))
            if w > max_width_cm:
                continue  # 家具太大，跳過
        result.append(item)
    return result


def enrich_renders(renders: list[dict], analysis: dict | None = None) -> list[dict]:
    """
    主入口：為每個 render 加上配對家具

    輸入格式（來自 gemini_analyze.py）：
    [{"style": "modern", "style_label": "現代簡約", "flux_prompt": "..."}]

    analysis: Gemini 分析結果（含 estimated_size 和 room_dimensions）

    輸出：每個 render 加上 "matched_furniture" 欄位
    """
    catalog = load_catalog()
    if not catalog:
        print("[furniture_match] 警告：目錄為空，跳過配對")
        return renders

    # 根據空間大小決定家具最大寬度
    estimated_size = analysis.get("estimated_size", "") if analysis else ""
    room_dims = analysis.get("room_dimensions") if analysis else None
    max_w = parse_max_width_cm(estimated_size, room_dims)
    print(f"[furniture_match] 空間: {estimated_size} → 傢俱寬度上限: {max_w}cm")

    # 長條型客廳判定（root cause fix for L sofa-in-narrow-room）
    aspect = compute_room_aspect_ratio(room_dims)
    is_long_room = aspect >= LONG_ROOM_ASPECT_THRESHOLD
    if aspect > 0:
        print(f"[furniture_match] 客廳長寬比={aspect:.2f}  is_long_room={is_long_room}"
              + (f"  → L/U/沙發床 降權 {LONG_ROOM_SHAPE_PENALTY}" if is_long_room else ""))

    enriched = []
    for render in renders:
        style = render.get("style", "")
        flux_prompt = render.get("flux_prompt", "")
        # 多撈一些供尺寸過濾，再切到 5。但客廳 must_have 已在 stage1 鎖住，不會被擠掉。
        matched = match_furniture(style, flux_prompt, catalog, top_n=8, mode='living',
                                  is_long_room=is_long_room)
        matched = filter_by_dimensions(matched, max_w)[:5]

        render_copy = dict(render)
        render_copy["matched_furniture"] = [
            {
                "id": item.get("id", ""),
                "name_zh": item.get("name_zh", ""),
                "brand": item.get("brand", ""),
                # 原始中文類別保留，另加 category_en 反映細分結果
                "category": item.get("category", ""),
                "category_en": resolve_category(item),
                "price_twd": item.get("price_twd", 0),
                "image_url": item.get("image_url", ""),
                "purchase_url": item.get("purchase_url", ""),
                "flux_descriptor": item.get("flux_descriptor", "") or item.get("name_zh", ""),
                "dimensions": item.get("dimensions", ""),
                "colors": item.get("colors", []),
            }
            for item in matched
        ]
        enriched.append(render_copy)

    return enriched


def build_reference_image_list(matched_furniture: list[dict]) -> list[str]:
    """提取有圖片的家具 URL，用於 Flux reference image"""
    return [
        item["image_url"]
        for item in matched_furniture
        if item.get("image_url") and item["image_url"].startswith("http")
    ]


def print_furniture_table(renders: list[dict]):
    """印出家具配對結果表格（CLI 用）"""
    import sys
    out = sys.stdout
    enc = getattr(out, "encoding", "utf-8") or "utf-8"

    def safe_print(text: str):
        print(text.encode(enc, errors="replace").decode(enc))

    for render in renders:
        style = render.get("style_label", render.get("style", ""))
        furniture = render.get("matched_furniture", [])
        safe_print(f"\n{'='*80}")
        safe_print(f"Style: {style}")
        safe_print(f"{'='*80}")
        safe_print(f"{'Name':<30} {'Brand':<10} {'Cat':<10} {'Price(TWD)':<12} {'Dims':<20}")
        safe_print("-" * 84)
        for item in furniture:
            safe_print(
                f"{item['name_zh'][:28]:<30} "
                f"{item['brand'][:8]:<10} "
                f"{item['category']:<10} "
                f"{item['price_twd']:<12} "
                f"{item.get('dimensions','')[:18]:<20}"
            )


if __name__ == "__main__":
    # 測試：用假 renders 跑配對
    test_renders = [
        {
            "style": "modern",
            "style_label": "現代簡約",
            "flux_prompt": "white oak panels, minimalist linen sofa, recessed LED ceiling, floating TV console, light greige palette",
        },
        {
            "style": "nordic",
            "style_label": "北歐 Scandinavian",
            "flux_prompt": "white birch, wool armchair, pine dining table, oversized pendant, cotton linen curtain, sheepskin rug",
        },
        {
            "style": "boho",
            "style_label": "波希米亞 Boho",
            "flux_prompt": "kilim rug, macrame wall hanging, rattan hanging chair, string fairy lights, floor cushion, monstera plant",
        },
    ]

    enriched = enrich_renders(test_renders)
    print_furniture_table(enriched)

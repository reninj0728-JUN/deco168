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
CATEGORY_ZH_TO_EN = {
    '沙發': 'sofa',
    '桌子': 'table',
    '椅子': 'chair',
    '床架': 'bed',
    '茶几': 'table',
    '收納': 'storage',
    '燈具': 'lighting',
    '地毯': 'rug',
    '窗簾': 'curtain',
    '裝飾': 'mirror',
    '抱枕': 'pillow',
    '寢具': 'bedding',
    '傢俱': 'other',
}

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


def match_furniture(style: str, flux_prompt: str, catalog: list[dict], top_n: int = 5) -> list[dict]:
    """
    根據風格 + flux_prompt 從目錄配對最適合的家具

    評分邏輯：
    - 風格 tag 命中 +3 分
    - flux_prompt 關鍵字命中 +1 分（每個）
    - 有圖片 +1 分
    - 有購買連結 +0.5 分
    """
    needed_cats = extract_categories_from_prompt(flux_prompt)
    prompt_keywords = [kw.strip().lower() for kw in flux_prompt.split(",")]

    scored = []
    for item in catalog:
        score = 0.0

        # 風格 tag 命中
        item_styles = item.get("style_tags", [])
        if style in item_styles:
            score += 3
        elif any(s in item_styles for s in _get_related_styles(style)):
            score += 1

        # 類別命中（prompt 需要的類別）—— 支援中英文類別
        item_cat_raw = item.get("category", "")
        item_cat = CATEGORY_ZH_TO_EN.get(item_cat_raw, item_cat_raw.lower())
        if item_cat in needed_cats:
            score += 2

        # flux_prompt 關鍵字命中
        # flux_descriptor 可能為空（momo 未跑 Gemini），改以 name_zh 補充
        item_keywords = [kw.lower() for kw in item.get("keywords", [])]
        item_descriptor = item.get("flux_descriptor", "").lower()
        item_name = item.get("name_zh", "").lower()
        search_text = item_descriptor or item_name  # 優先用 descriptor，沒有就用名稱
        for pkw in prompt_keywords:
            if any(pkw in ikw for ikw in item_keywords) or pkw in search_text:
                score += 1

        # 顏色符合
        item_colors = [c.lower() for c in item.get("colors", [])]
        for pkw in prompt_keywords:
            if any(pkw in color for color in item_colors):
                score += 0.5

        # 有圖片 bonus
        if item.get("image_url"):
            score += 1
        if item.get("purchase_url"):
            score += 0.5

        if score > 0:
            scored.append((score, item))

    # 依分數排序，每個類別最多取 1 件（避免同一風格出現 2 個沙發）
    scored.sort(key=lambda x: -x[0])
    selected = []
    cat_seen: set[str] = set()

    for score, item in scored:
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

    enriched = []
    for render in renders:
        style = render.get("style", "")
        flux_prompt = render.get("flux_prompt", "")
        matched = match_furniture(style, flux_prompt, catalog, top_n=8)  # 多撈幾件供過濾
        matched = filter_by_dimensions(matched, max_w)[:5]  # 過濾大件後取前 5

        render_copy = dict(render)
        render_copy["matched_furniture"] = [
            {
                "id": item.get("id", ""),
                "name_zh": item.get("name_zh", ""),
                "brand": item.get("brand", ""),
                "category": item.get("category", ""),
                "price_twd": item.get("price_twd", 0),
                "image_url": item.get("image_url", ""),
                "purchase_url": item.get("purchase_url", ""),
                # flux_descriptor 空時退回 name_zh，確保 Flux 有可用描述
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

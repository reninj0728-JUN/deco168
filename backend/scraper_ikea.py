"""
IKEA Taiwan 家具資料爬蟲
抓取：品名、價格、尺寸、顏色、圖片、購買連結
輸出：JSON 表格 + 印出 CSV 格式
"""
import json
import time
import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

# 9 種風格對應的 IKEA 搜尋關鍵字
STYLE_SEARCH_MAP = {
    "modern":        ["現代沙發", "簡約書桌", "白色收納", "電視櫃"],
    "japanese":      ["日式矮桌", "原木收納", "竹簾", "和風燈具"],
    "luxury":        ["天鵝絨沙發", "大理石桌", "黃銅燈", "絨布椅"],
    "nordic":        ["北歐椅子", "木質餐桌", "棉麻窗簾", "編織地毯"],
    "industrial":    ["金屬書架", "工業風燈", "黑色層架", "皮革椅"],
    "mediterranean": ["藤編椅", "地中海燈", "陶瓷花器", "拱形鏡"],
    "muji":          ["收納盒", "簡約床架", "原木層架", "紙質燈罩"],
    "art-deco":      ["幾何地毯", "鏡面桌", "金色燭臺", "絲絨椅"],
    "boho":          ["流蘇地毯", "藤編燈", "植物架", "波西米亞抱枕"],
}

BASE_URL = "https://www.ikea.com/tw/zh"


def search_ikea(keyword: str, max_items: int = 5) -> list[dict]:
    """搜尋 IKEA Taiwan，回傳產品列表"""
    url = f"{BASE_URL}/search/?q={requests.utils.quote(keyword)}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [ERROR] 搜尋 '{keyword}' 失敗: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    # IKEA 產品卡片的 JSON-LD 結構化資料
    json_ld_tags = soup.find_all("script", type="application/ld+json")
    for tag in json_ld_tags:
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                for item in data:
                    if item.get("@type") == "Product":
                        parsed = _parse_jsonld_product(item)
                        if parsed:
                            items.append(parsed)
                            if len(items) >= max_items:
                                break
            elif data.get("@type") == "Product":
                parsed = _parse_jsonld_product(data)
                if parsed:
                    items.append(parsed)
        except Exception:
            continue

    # 備用：從 HTML 卡片抓
    if not items:
        items = _parse_product_cards(soup, keyword, max_items)

    return items[:max_items]


def _parse_jsonld_product(data: dict) -> dict | None:
    """解析 JSON-LD Product 結構"""
    try:
        name = data.get("name", "")
        if not name:
            return None

        # 價格
        offers = data.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price_str = offers.get("price", "0")
        try:
            price_twd = int(float(price_str))
        except (ValueError, TypeError):
            price_twd = 0

        # 圖片
        image = data.get("image", "")
        if isinstance(image, list):
            image = image[0] if image else ""

        # URL
        product_url = data.get("url", "")
        if product_url and not product_url.startswith("http"):
            product_url = BASE_URL + product_url

        return {
            "name_zh": name,
            "brand": "IKEA",
            "price_twd": price_twd,
            "image_url": image,
            "purchase_url": product_url,
            "source": "ikea_jsonld",
        }
    except Exception:
        return None


def _parse_product_cards(soup: BeautifulSoup, keyword: str, max_items: int) -> list[dict]:
    """備用：從 HTML 卡片解析"""
    items = []

    # IKEA 常見的產品卡片 selector（可能隨改版更新）
    selectors = [
        "article[data-ref-id]",
        ".pip-product-compact",
        "[class*='product-card']",
        "[class*='ProductCard']",
    ]

    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards[:max_items]:
        try:
            name_el = card.select_one("[class*='name'], h2, h3, [class*='title']")
            price_el = card.select_one("[class*='price'] [class*='integer'], [class*='Price']")
            img_el = card.select_one("img[src]")
            link_el = card.select_one("a[href]")

            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue

            price_text = price_el.get_text(strip=True) if price_el else "0"
            price_nums = re.findall(r"\d+", price_text.replace(",", ""))
            price_twd = int(price_nums[0]) if price_nums else 0

            img_url = img_el.get("src", "") if img_el else ""
            if img_url and img_url.startswith("//"):
                img_url = "https:" + img_url

            href = link_el.get("href", "") if link_el else ""
            if href and not href.startswith("http"):
                href = BASE_URL + href

            items.append({
                "name_zh": name,
                "brand": "IKEA",
                "price_twd": price_twd,
                "image_url": img_url,
                "purchase_url": href,
                "source": "ikea_html",
            })
        except Exception:
            continue

    return items


def classify_style_by_keyword(product_name: str, search_keyword: str, style: str) -> dict:
    """根據搜尋關鍵字和商品名推斷家具屬性"""
    name_lower = product_name.lower()

    # 類別推斷
    category_map = {
        "沙發": "sofa", "sofa": "sofa", "couch": "sofa",
        "桌": "table", "table": "table", "desk": "table",
        "椅": "chair", "chair": "chair", "stool": "chair",
        "燈": "lighting", "lamp": "lighting", "light": "lighting",
        "架": "shelving", "shelf": "shelving", "shelv": "shelving",
        "收納": "storage", "storage": "storage", "box": "storage",
        "床": "bed", "bed": "bed",
        "地毯": "rug", "rug": "rug", "mat": "rug",
        "窗簾": "curtain", "curtain": "curtain",
        "鏡": "mirror", "mirror": "mirror",
    }

    category = "accessory"
    for kw, cat in category_map.items():
        if kw in product_name or kw in name_lower:
            category = cat
            break

    return {
        "category": category,
        "style_tags": [style],
    }


def build_furniture_table(styles: list[str] | None = None, max_per_keyword: int = 3) -> list[dict]:
    """
    爬取 IKEA Taiwan 家具資料，建立風格化清單
    回傳格式適合存入 furniture_catalog.json
    """
    target_styles = styles or list(STYLE_SEARCH_MAP.keys())
    all_items = []
    seen_names = set()

    for style in target_styles:
        keywords = STYLE_SEARCH_MAP.get(style, [])
        print(f"\n[{style}] 搜尋 {len(keywords)} 個關鍵字...")

        for keyword in keywords:
            print(f"  搜尋: {keyword}")
            products = search_ikea(keyword, max_items=max_per_keyword)
            print(f"  → 找到 {len(products)} 件")

            for p in products:
                name = p.get("name_zh", "")
                if not name or name in seen_names:
                    continue
                seen_names.add(name)

                extra = classify_style_by_keyword(name, keyword, style)
                item = {
                    "id": f"ikea-{len(all_items)+1:03d}",
                    "name_zh": name,
                    "brand": "IKEA",
                    "category": extra["category"],
                    "style_tags": extra["style_tags"],
                    "price_twd": p.get("price_twd", 0),
                    "image_url": p.get("image_url", ""),
                    "purchase_url": p.get("purchase_url", ""),
                    "source": p.get("source", "ikea"),
                    "search_keyword": keyword,
                }
                all_items.append(item)

            time.sleep(1.5)  # 避免被封

    return all_items


def print_table(items: list[dict]):
    """印出 CSV 格式的家具表格"""
    print("\n" + "=" * 100)
    print(f"{'品名':<30} {'風格':<12} {'類別':<10} {'價格(TWD)':<10} {'來源':<10}")
    print("=" * 100)
    for item in items:
        styles = ", ".join(item.get("style_tags", []))
        print(
            f"{item['name_zh'][:28]:<30} "
            f"{styles:<12} "
            f"{item['category']:<10} "
            f"{item['price_twd']:<10} "
            f"{item.get('source',''):<10}"
        )
    print(f"\n共 {len(items)} 件產品")


if __name__ == "__main__":
    import sys

    # 快速測試：只跑 2 種風格 x 2 關鍵字
    test_styles = sys.argv[1].split(",") if len(sys.argv) > 1 else ["modern", "nordic"]

    print(f"開始爬取 IKEA Taiwan，風格：{test_styles}")
    items = build_furniture_table(styles=test_styles, max_per_keyword=3)

    print_table(items)

    # 儲存結果
    out_path = "ikea_scrape_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"\n已儲存到 {out_path}")

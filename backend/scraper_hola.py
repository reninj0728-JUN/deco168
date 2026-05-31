"""
HOLA 特力和樂 傢俱爬蟲（Playwright + JSON-LD）
策略：搜尋關鍵字 → 抓商品 ID → 爬商品頁 JSON-LD
結果存到 furniture_raw_hola.json
用法：python3.11 scraper_hola.py
"""
import asyncio, json, sys, io, re, time
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RAW_PATH  = Path(__file__).parent / "furniture_raw_hola.json"
BASE_URL  = "https://www.hola.com.tw"

# (搜尋關鍵字, zh_category, style_hint)
SEARCHES = [
    ("沙發",       "沙發",  "cream"),
    ("布沙發",     "沙發",  "cream"),
    ("L型沙發",    "沙發",  "modern"),
    ("餐桌",       "桌子",  "wood"),
    ("茶几",       "茶几",  "modern"),
    ("床架",       "床架",  "nordic"),
    ("雙人床",     "床架",  "muji"),
    ("收納架",     "收納",  "muji"),
    ("書架",       "收納",  "modern"),
    ("地毯",       "地毯",  "cream"),
    ("窗簾",       "窗簾",  "nordic"),
    ("吊燈",       "燈具",  "modern"),
    ("立燈",       "燈具",  "modern"),
    ("抱枕",       "抱枕",  "cream"),
    ("餐椅",       "椅子",  "wood"),
    ("單人椅",     "椅子",  "modern"),
    ("鏡子",       "裝飾",  "luxury"),
    ("花瓶",       "裝飾",  "japanese"),
    ("置物架",     "收納",  "modern"),
    ("電視櫃",     "收納",  "modern"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/124"}


async def get_product_ids_from_search(page, keyword: str) -> list[str]:
    """搜尋頁面，從 HTML 抓商品 ID"""
    url = f"{BASE_URL}/search/?text={keyword}"
    try:
        await page.goto(url, wait_until="load", timeout=30000)
        await asyncio.sleep(6)
        content = await page.content()
        ids = list(set(re.findall(r'/p/(\d{8,12})', content)))
        return ids
    except Exception as e:
        print(f"  Search [{keyword}] ERROR: {e}")
        return []


async def get_product_detail(page, item_id: str) -> dict | None:
    """爬商品頁，從 JSON-LD 取名稱/圖片/價格"""
    url = f"{BASE_URL}/p/{item_id}"
    try:
        await page.goto(url, wait_until="load", timeout=25000)
        await asyncio.sleep(2)
        content = await page.content()

        # 抓 JSON-LD
        jsonld_blocks = re.findall(
            r'application/ld\+json[^>]*>(.*?)</script>',
            content, re.DOTALL
        )
        for block in jsonld_blocks:
            try:
                raw = json.loads(block.strip())
                # JSON-LD 可能是 dict 或 list
                items = raw if isinstance(raw, list) else [raw]
                for data in items:
                    if not isinstance(data, dict) or data.get("@type") != "Product":
                        continue
                    name = data.get("name", "")
                    images = data.get("image", [])
                    if isinstance(images, list):
                        img_url = images[0] if images else ""
                    else:
                        img_url = images or ""
                    offers = data.get("offers", {})
                    price_str = offers.get("price", "0") if isinstance(offers, dict) else "0"
                    try:
                        price_twd = int(float(str(price_str).replace(",", "")))
                    except:
                        price_twd = 0
                    if name and img_url:
                        return {
                            "id": f"hola_{item_id}",
                            "name_zh": name[:40],
                            "price_twd": price_twd,
                            "image_url": img_url,
                            "purchase_url": url,
                            "source": "hola",
                        }
            except:
                continue
        return None
    except Exception as e:
        return None


async def main():
    all_items = []
    seen_ids = set()
    search_found_ids: dict[str, str] = {}  # item_id -> (category, style_hint)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124",
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        # Step 1: 搜尋收集商品 ID
        print("=== Step 1: 搜尋商品 ID ===")
        for keyword, zh_cat, style_hint in SEARCHES:
            ids = await get_product_ids_from_search(page, keyword)
            new = 0
            for item_id in ids:
                if item_id not in search_found_ids:
                    search_found_ids[item_id] = (zh_cat, style_hint)
                    new += 1
            print(f"  [{keyword}] {len(ids)} 件 (+{new} 新)")
            await asyncio.sleep(1)

        print(f"\n總共找到 {len(search_found_ids)} 個不重複商品 ID")

        # Step 2: 爬各商品頁詳細資料
        print("\n=== Step 2: 爬商品詳細頁 ===")
        id_list = list(search_found_ids.items())
        for i, (item_id, (zh_cat, style_hint)) in enumerate(id_list):
            detail = await get_product_detail(page, item_id)
            if detail:
                detail["category"] = zh_cat
                detail["style_hint"] = style_hint
                if detail["id"] not in seen_ids:
                    seen_ids.add(detail["id"])
                    all_items.append(detail)
                    if (i + 1) % 10 == 0:
                        print(f"  進度: {i+1}/{len(id_list)} | 已抓: {len(all_items)}")
            await asyncio.sleep(0.4)

        await browser.close()

    print(f"\n總計: {len(all_items)} 件")
    RAW_PATH.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"已存 → {RAW_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

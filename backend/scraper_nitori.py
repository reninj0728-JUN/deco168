"""
宜得利 Nitori Taiwan 傢俱爬蟲（Playwright）
策略：分類頁 → 抓商品 ID → 爬商品頁
結果存到 furniture_raw_nitori.json
用法：python3.11 scraper_nitori.py
"""
import asyncio, json, sys, io, re, time
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RAW_PATH = Path(__file__).parent / "furniture_raw_nitori.json"
BASE_URL  = "https://www.nitori-net.tw"

# (category_id, zh_category, style_hint)
# 從實際網站導覽得出的分類 ID
CATEGORIES = [
    (3,   "沙發",  "cream"),    # 客廳家具
    (15,  "寢具",  "muji"),     # 臥室寢具
    (5,   "床架",  "muji"),     # 床・化妝台
    (1,   "桌子",  "modern"),   # 餐廳家具
    (4,   "收納",  "muji"),     # 衣櫃・鞋櫃
    (11,  "收納",  "modern"),   # 收納用品
    (7,   "裝飾",  "japanese"), # 餐廚用品
    (8,   "裝飾",  "muji"),     # 洗曬・衛浴
]

JS_EXTRACT_IDS = """
() => {
    const content = document.documentElement.innerHTML;
    const matches = content.match(/\/product\/([A-Za-z0-9]+s)/g) || [];
    return [...new Set(matches.map(m => m.replace('/product/', '')))];
}
"""

JS_EXTRACT_DETAIL = """
() => {
    // 商品名稱
    const nameEl = document.querySelector('h1, [class*="product-name"], [class*="item-name"], [itemprop="name"]');
    const name = nameEl ? nameEl.innerText.trim() : '';

    // 價格
    const priceEl = document.querySelector('[class*="price"], [itemprop="price"], [class*="Price"]');
    const priceText = priceEl ? priceEl.innerText.replace(/[^0-9]/g, '') : '0';

    // 圖片
    const imgEl = document.querySelector('[class*="product"] img, [class*="item"] img, main img');
    const img = imgEl ? (imgEl.dataset.src || imgEl.src || '') : '';

    // 也從 JSON-LD 抓
    const jsonlds = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of jsonlds) {
        try {
            const d = JSON.parse(s.innerText);
            if (d['@type'] === 'Product') {
                return {
                    name: d.name || name,
                    price: parseInt((d.offers && d.offers.price) || priceText || '0'),
                    image: (d.image && (Array.isArray(d.image) ? d.image[0] : d.image)) || img,
                };
            }
        } catch(e) {}
    }
    return { name, price: parseInt(priceText) || 0, image: img };
}
"""


async def get_ids_from_category(page, cat_id: int) -> list[str]:
    url = f"{BASE_URL}/category/{cat_id}"
    try:
        await page.goto(url, wait_until="load", timeout=30000)
        await asyncio.sleep(5)
        # scroll 載入更多
        for y in range(0, 5000, 800):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await asyncio.sleep(0.3)
        await asyncio.sleep(2)
        ids = await page.evaluate(JS_EXTRACT_IDS)
        return ids or []
    except Exception as e:
        print(f"  Category {cat_id}: ERROR {e}")
        return []


async def get_product_detail(page, product_id: str) -> dict | None:
    url = f"{BASE_URL}/product/{product_id}"
    try:
        await page.goto(url, wait_until="load", timeout=25000)
        await asyncio.sleep(3)
        detail = await page.evaluate(JS_EXTRACT_DETAIL)
        if detail and detail.get("name"):
            return {
                "id": f"nitori_{product_id}",
                "name_zh": detail["name"][:40],
                "price_twd": detail.get("price", 0),
                "image_url": detail.get("image", ""),
                "purchase_url": url,
                "source": "nitori",
            }
        return None
    except Exception as e:
        return None


async def main():
    all_items = []
    seen_ids = set()
    id_to_meta: dict[str, tuple] = {}  # product_id -> (zh_cat, style_hint)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124",
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        # Step 1: 各分類取商品 ID
        print("=== Step 1: 收集商品 ID ===")
        for cat_id, zh_cat, style_hint in CATEGORIES:
            ids = await get_ids_from_category(page, cat_id)
            new = 0
            for pid in ids:
                if pid not in id_to_meta:
                    id_to_meta[pid] = (zh_cat, style_hint)
                    new += 1
            print(f"  category/{cat_id} [{zh_cat}]: {len(ids)} 件 (+{new} 新)")
            await asyncio.sleep(1)

        print(f"\n總共找到 {len(id_to_meta)} 個不重複商品")

        # Step 2: 爬各商品頁
        print("\n=== Step 2: 爬商品詳細頁 ===")
        id_list = list(id_to_meta.items())
        for i, (product_id, (zh_cat, style_hint)) in enumerate(id_list):
            detail = await get_product_detail(page, product_id)
            if detail:
                detail["category"] = zh_cat
                detail["style_hint"] = style_hint
                if detail["id"] not in seen_ids:
                    seen_ids.add(detail["id"])
                    all_items.append(detail)
            if (i + 1) % 20 == 0:
                print(f"  進度: {i+1}/{len(id_list)} | 已抓: {len(all_items)}")
            await asyncio.sleep(0.7)

        await browser.close()

    print(f"\n總計: {len(all_items)} 件")
    RAW_PATH.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"已存 → {RAW_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

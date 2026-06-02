"""
IKEA 台灣 (www.ikea.com.tw) 真實傢俱爬蟲
結果存到 furniture_raw_ikea.json
"""
import asyncio, json, sys, io, re, time
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RAW_PATH = Path(__file__).parent / "furniture_raw_ikea.json"
BASE_URL = "https://www.ikea.com.tw"

# 類別頁 → (url_path, zh_category, default_style_hint)
# 全部使用子類別 URL（父類別頁面沒有商品列表）
CATEGORIES = [
    # 沙發類
    ("zh/products/sofas/fabric-sofas",                          "沙發",  "nordic"),
    ("zh/products/sofas/leather-sofas",                         "沙發",  "modern"),
    ("zh/products/sofas/sofa-beds",                             "沙發",  "modern"),
    ("zh/products/sofas/armchairs",                             "椅子",  "nordic"),
    ("zh/products/sofas/footstools",                            "椅子",  "nordic"),
    # 椅子類
    ("zh/products/dining-seating",                              "椅子",  "nordic"),
    ("zh/products/dining-seating/bar-stools",                   "椅子",  "modern"),
    # 桌子類
    ("zh/products/coffee-and-side-table/sofa-tables",           "茶几",  "nordic"),
    ("zh/products/dining-tables/tables",                        "桌子",  "nordic"),
    ("zh/products/dining-tables/gateleg-tables",                "桌子",  "modern"),
    ("zh/products/work-desks/home-desks",                       "桌子",  "modern"),
    ("zh/products/work-desks",                                  "桌子",  "modern"),
    # 收納類
    ("zh/products/media-furniture/tv-and-media-furniture",      "收納",  "modern"),
    ("zh/products/tv-furniture/system-cabinets",                "收納",  "modern"),
    ("zh/products/display-furniture/solitaire-cabinets",        "收納",  "modern"),
    ("zh/products/shelving-units/open-shelving-units",          "收納",  "modern"),
    ("zh/products/bookcases-and-box/bookcases",                 "收納",  "nordic"),
    ("zh/products/sideboards",                                  "收納",  "modern"),
    ("zh/products/chests-and-other-furniture/bedside-tables",   "收納",  "nordic"),
    # 床類
    ("zh/products/beds/double-beds",                            "床架",  "nordic"),
    ("zh/products/beds/single-beds",                            "床架",  "nordic"),
    # 燈具類
    ("zh/products/luminaires/table-lamps",                      "燈具",  "modern"),
    ("zh/products/luminaires/floor-lamps",                      "燈具",  "modern"),
    ("zh/products/luminaires/ceiling-lamps-and-spotlights",     "燈具",  "nordic"),
    # 軟件類
    ("zh/products/home-furnishing-rugs",                        "地毯",  "boho"),
    ("zh/products/window-solutions/curtains-and-window-panels", "窗簾",  "nordic"),
    ("zh/products/cushions-throws-and-chairpads/cushions",      "抱枕",  "nordic"),
    # 裝飾類
    ("zh/products/home-decoration/plant-pots",                  "裝飾",  "nordic"),
    ("zh/products/home-decoration/vases-bowls-and-accessories", "裝飾",  "modern"),
    ("zh/products/home-decoration/frames-and-wall-decoration",  "裝飾",  "modern"),
    ("zh/products/home-decoration/clocks",                      "裝飾",  "modern"),
    # 衣櫃 / 衣物收納
    ("zh/products/wardrobes",                                    "收納",  "modern"),
    ("zh/products/wardrobes/pax-wardrobes",                      "收納",  "modern"),
    ("zh/products/storage-furniture/chests-of-drawers",          "收納",  "modern"),
    # 寢具（muji 感最強的品類）
    ("zh/products/bedtextiles/duvet-covers-and-pillowcases",     "寢具",  "muji"),
    ("zh/products/bedtextiles/fitted-sheets-and-bed-linen",      "寢具",  "muji"),
    ("zh/products/bedtextiles/comforters-and-duvets",            "寢具",  "muji"),
    # 鏡子（部分金框款可分到 luxury）
    ("zh/products/mirrors",                                      "裝飾",  "modern"),
    ("zh/products/mirrors/wall-mirrors",                         "裝飾",  "luxury"),
    # 更多地毯（補 boho 數量）
    ("zh/products/home-furnishing-rugs/large-rugs",              "地毯",  "boho"),
    ("zh/products/home-furnishing-rugs/small-rugs",              "地毯",  "nordic"),
    # 辦公 / 工作椅
    ("zh/products/work-chairs",                                  "椅子",  "modern"),
    # 床墊（獨立品類，有日式低床墊）
    ("zh/products/mattresses",                                   "寢具",  "japanese"),
    ("zh/products/mattresses/foam-mattresses",                   "寢具",  "muji"),
    # 浴室收納（部分可當裝飾用）
    ("zh/products/bathroom-furniture/bathroom-storage",          "收納",  "modern"),
]

JS_EXTRACT = """
() => {
    const results = [];
    const blocks = document.querySelectorAll(".itemBlock.itemPlp");
    for (const block of blocks) {
        const card = block.querySelector(".card.product-event-tracker");
        if (!card) continue;

        const productId = card.getAttribute("data-product-id") || "";
        const priceAttr = card.getAttribute("data-product-price") || "0";

        const seriesEl = card.querySelector(".itemName h6, .itemName");
        const factsEl = card.querySelector(".itemFacts");
        const imgEl = card.querySelector(".card-img-top");
        const linkEl = card.querySelector("a.itemName");

        const series = seriesEl ? seriesEl.innerText.trim() : "";
        const facts = factsEl ? factsEl.innerText.trim() : "";
        const name = facts ? series + " " + facts : series;
        const img = imgEl ? imgEl.src : "";
        const href = linkEl ? linkEl.getAttribute("href") : "";

        if (name && img) {
            results.push({
                id: productId,
                name_zh: name,
                series: series,
                price_twd: parseInt(priceAttr, 10) || 0,
                image_url: img,
                purchase_url: href ? ("https://www.ikea.com.tw" + href) : "",
            });
        }
    }
    return results;
}
"""


async def scrape_category(page, url_path: str, zh_cat: str, style_hint: str) -> list:
    url = f"{BASE_URL}/{url_path}"
    results = []
    try:
        r = await page.goto(url, timeout=30000, wait_until="networkidle")
        await asyncio.sleep(4)

        if r.status >= 400:
            print(f"  [{zh_cat}] {url_path}: HTTP {r.status}, skip")
            return []

        # scroll 觸發懶載入
        for y in range(0, 3000, 600):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await asyncio.sleep(0.3)
        await asyncio.sleep(2)

        items = await page.evaluate(JS_EXTRACT)
        for it in items:
            it["category"] = zh_cat
            it["style_hint"] = style_hint
            it["source"] = "ikea"
        print(f"  [{zh_cat}] {url_path}: {len(items)} 件")
        results.extend(items)
    except Exception as e:
        print(f"  [{zh_cat}] {url_path}: ERROR {e}")

    return results


async def main():
    all_items = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        for url_path, zh_cat, style_hint in CATEGORIES:
            items = await scrape_category(page, url_path, zh_cat, style_hint)
            all_items.extend(items)
            await asyncio.sleep(1.5)

        await browser.close()

    # 全域去重（同 purchase_url）
    seen = set()
    unique = []
    for it in all_items:
        if it["purchase_url"] and it["purchase_url"] not in seen:
            seen.add(it["purchase_url"])
            unique.append(it)

    print(f"\n總計 {len(unique)} 件（去重後）")

    with open(RAW_PATH, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)
    print(f"存至 {RAW_PATH}")

    from collections import Counter
    cats = Counter(it["category"] for it in unique)
    print("\n類別分布:")
    for cat, cnt in cats.most_common():
        print(f"  {cat}: {cnt}")

    # 顯示前3件
    print("\n前3件範例:")
    for ex in unique[:3]:
        print(f"  {ex['name_zh'][:40]} | NT${ex['price_twd']:,}")
        print(f"  圖: {ex['image_url'][:70]}")
        print(f"  URL: {ex['purchase_url'][:70]}")

if __name__ == "__main__":
    asyncio.run(main())

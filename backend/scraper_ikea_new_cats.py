"""
只爬 IKEA 台灣新增的 12 個品類，合併進現有 furniture_raw_ikea.json
"""
import asyncio, json, sys, io
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RAW_PATH = Path(__file__).parent / "furniture_raw_ikea.json"
BASE_URL  = "https://www.ikea.com.tw"

NEW_CATEGORIES = [
    # 衣櫃
    ("zh/products/wardrobes/solitaire-wardrobes",                "收納",  "modern"),
    ("zh/products/wardrobes/wardrobe-pax-system",                "收納",  "modern"),
    ("zh/products/wardrobes/wardrobe-lastare-system",            "收納",  "nordic"),
    # 寢具
    ("zh/products/duvets-pillows-and-protectors/duvets",         "寢具",  "muji"),
    ("zh/products/bedlinen/bed-sheets",                          "寢具",  "muji"),
    # 鏡子（部分金框款可分到 luxury）
    ("zh/products/home-decoration/mirrors",                      "裝飾",  "luxury"),
    # 辦公椅
    ("zh/products/work-chairs",                                  "椅子",  "modern"),
    # 床墊
    ("zh/products/mattresses-and-accessories/double-mattresses", "寢具",  "japanese"),
    ("zh/products/mattresses-and-accessories/single-mattresses", "寢具",  "muji"),
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
        const factsEl  = card.querySelector(".itemFacts");
        const imgEl    = card.querySelector(".card-img-top");
        const linkEl   = card.querySelector("a.itemName");
        const series = seriesEl ? seriesEl.innerText.trim() : "";
        const facts  = factsEl  ? factsEl.innerText.trim()  : "";
        const name   = facts ? series + " " + facts : series;
        const img    = imgEl  ? imgEl.src : "";
        const href   = linkEl ? linkEl.getAttribute("href") : "";
        if (name && img) results.push({
            id: productId,
            name_zh: name,
            series: series,
            price_twd: parseInt(priceAttr, 10) || 0,
            image_url: img,
            purchase_url: href ? ("https://www.ikea.com.tw" + href) : "",
        });
    }
    return results;
}
"""

async def scrape_category(page, url_path, zh_cat, style_hint):
    url = f"{BASE_URL}/{url_path}"
    try:
        r = await page.goto(url, timeout=30000, wait_until="networkidle")
        await asyncio.sleep(4)
        if r.status >= 400:
            print(f"  [{zh_cat}] {url_path}: HTTP {r.status}, skip")
            return []
        for y in range(0, 3000, 600):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await asyncio.sleep(0.3)
        await asyncio.sleep(2)
        items = await page.evaluate(JS_EXTRACT)
        for it in items:
            it["category"]   = zh_cat
            it["style_hint"] = style_hint
            it["source"]     = "ikea"
        print(f"  [{zh_cat}] {url_path}: {len(items)} 件")
        return items
    except Exception as e:
        print(f"  [{zh_cat}] {url_path}: ERROR {e}")
        return []


async def main():
    # 載入現有 raw 資料
    existing = []
    if RAW_PATH.exists():
        existing = json.loads(RAW_PATH.read_text(encoding='utf-8'))
    existing_urls = {it.get("purchase_url", "") for it in existing}
    print(f"現有 IKEA raw: {len(existing)} 件")

    new_items = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        for url_path, zh_cat, style_hint in NEW_CATEGORIES:
            items = await scrape_category(page, url_path, zh_cat, style_hint)
            for it in items:
                url = it.get("purchase_url", "")
                if url and url not in existing_urls:
                    existing_urls.add(url)
                    new_items.append(it)
            await asyncio.sleep(1.5)
        await browser.close()

    print(f"\n新增: {len(new_items)} 件（去重後）")
    merged = existing + new_items
    RAW_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"合併後總計: {len(merged)} 件 → 存至 {RAW_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

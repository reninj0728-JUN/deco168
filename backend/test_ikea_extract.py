import asyncio, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

JS = """
() => {
    const results = [];
    const cards = document.querySelectorAll(".card");
    for (const card of cards) {
        const img = card.querySelector(".card-img-top, img");
        const nameEl = card.querySelector(".itemBlock, .itemDetails, h3, h4, [class*='itemName']");
        const priceEl = card.querySelector(".itemInfo, [class*='price'], [class*='Price']");
        const linkEl = card.querySelector("a[href*='product'], a[href*='catalog']") || card.querySelector("a");
        if (img || nameEl) {
            results.push({
                name: nameEl ? nameEl.innerText.trim() : "",
                price: priceEl ? priceEl.innerText.trim() : "",
                img: img ? (img.src || img.dataset.src || img.getAttribute("data-lazy-src") || "") : "",
                url: linkEl ? linkEl.href : "",
            });
        }
    }
    return { total: cards.length, items: results.slice(0, 12) };
}
"""

async def test():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        await page.goto("https://www.ikea.com.tw/products/sofas/fabric-sofas", timeout=30000, wait_until="networkidle")
        await asyncio.sleep(5)
        # scroll 觸發懶載入
        for y in [500, 1000, 1500, 2000]:
            await page.evaluate(f"window.scrollTo(0, {y})")
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)

        items = await page.evaluate(JS)
        print(f"card 元素總數: {items['total']}")
        for it in items['items']:
            if it['name'] or it['img']:
                print(f"\n  名稱: {it['name'][:60]}")
                print(f"  價格: {it['price'][:40]}")
                print(f"  圖片: {it['img'][:80]}")
                print(f"  連結: {it['url'][:80]}")

        await browser.close()

asyncio.run(test())

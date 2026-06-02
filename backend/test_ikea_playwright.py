import asyncio, sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            },
        )
        page = await ctx.new_page()

        # 試多個可能的 IKEA 台灣 URL
        urls = [
            "https://www.ikea.com/tw/zh/",
            "https://www.ikea.com/tw/zh/cat/sofas-fu003/",
        ]

        for url in urls:
            print(f"\n=== {url} ===")
            try:
                r = await page.goto(url, timeout=30000, wait_until="networkidle")
                await asyncio.sleep(3)
                title = await page.title()
                final_url = page.url
                print(f"Status: {r.status if r else 'none'}")
                print(f"Final URL: {final_url}")
                print(f"Title: {title}")

                # 試抓商品
                items = await page.evaluate('''() => {
                    const results = [];
                    const cards = document.querySelectorAll(
                        '[data-testid*="product"], [class*="plp-product"], [class*="product-card"], ' +
                        '[class*="pip-"], article, [class*="ProductCard"]'
                    );
                    for (const card of cards) {
                        const nameEl = card.querySelector(
                            '[class*="name"], [class*="heading"], [data-testid*="name"], h2, h3, h4'
                        );
                        const priceEl = card.querySelector(
                            '[class*="price"], [data-testid*="price"]'
                        );
                        const imgEl = card.querySelector('img');
                        const linkEl = card.querySelector('a[href]');
                        if (nameEl || imgEl) {
                            results.push({
                                name: nameEl ? nameEl.innerText.trim() : '',
                                price: priceEl ? priceEl.innerText.trim() : '',
                                img: imgEl ? (imgEl.src || imgEl.dataset.src || '') : '',
                                url: linkEl ? linkEl.href : '',
                            });
                        }
                    }
                    return results.slice(0, 10);
                }''')
                print(f"找到 {len(items)} 件商品")
                for item in items[:3]:
                    print(f"  名稱: {item['name'][:50]}")
                    print(f"  圖片: {item['img'][:80]}")

                if not items:
                    # 看 HTML 片段
                    snippet = await page.evaluate('() => document.body.innerText.slice(0, 500)')
                    print(f"頁面文字: {snippet}")

            except Exception as e:
                print(f"ERROR: {e}")

        await browser.close()

asyncio.run(test())

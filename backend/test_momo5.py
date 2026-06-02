import asyncio, sys, io, re, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

async def test_momo():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://www.momoshop.com.tw/search/searchShop.jsp?keyword=%E5%8C%97%E6%AD%90%E6%B2%99%E7%99%BC&size=12&page=1', timeout=30000)
        await asyncio.sleep(5)

        # 直接用 Playwright 抓 DOM 元素
        # 找商品卡片的 li 或 div 元素
        items = await page.evaluate('''() => {
            const results = [];
            // 試各種 selector
            const candidates = [
                ...document.querySelectorAll('li[id*="prdList"] a'),
                ...document.querySelectorAll('.listArea li'),
                ...document.querySelectorAll('[class*="goodsList"] li'),
            ];
            // 找含有圖片和連結的商品區塊
            const containers = document.querySelectorAll('li');
            for (const li of containers) {
                const img = li.querySelector('img');
                const a = li.querySelector('a[href*="goods"]');
                const priceEl = li.querySelector('[class*="price"], [class*="Price"]');
                if (img && a && img.src && img.src.includes('momoshop')) {
                    results.push({
                        name: li.querySelector('[class*="name"], [class*="Name"], h3, h4')?.innerText?.trim() || '',
                        img: img.src || img.dataset.src || '',
                        url: a.href || '',
                        price: priceEl?.innerText?.trim() || '',
                    });
                }
            }
            return results.slice(0, 15);
        }''')
        print(f'找到 {len(items)} 件商品')
        for item in items[:5]:
            print(f"\n名稱: {item['name'][:50]}")
            print(f"圖片: {item['img'][:80]}")
            print(f"連結: {item['url'][:80]}")
            print(f"價格: {item['price']}")

        await browser.close()

asyncio.run(test_momo())

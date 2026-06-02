import asyncio, sys, io, re, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

async def test_momo():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://www.momoshop.com.tw/search/searchShop.jsp?keyword=%E5%8C%97%E6%AD%90%E6%B2%99%E7%99%BC&size=12&page=1', timeout=30000)
        await asyncio.sleep(5)
        html = await page.content()

        # 找 Offer block 的完整上下文
        offers = re.findall(r'\{[^{}]*"@type"\s*:\s*"Offer"[^{}]*\}', html, re.DOTALL)
        print(f'Offer 數量: {len(offers)}')

        # 找產品名稱 - 各種可能的 pattern
        names = re.findall(r'"name"\s*:\s*"([^"]{5,80})"', html)
        print(f'name 數量: {len(names)}')
        print('前5個:', names[:5])

        # 找圖片
        imgs_cdn = re.findall(r'https://[a-z0-9.]+(?:momoshop|momo)[a-z0-9./-]*\.(?:jpg|jpeg|png|webp)', html, re.IGNORECASE)
        print(f'\nmomo CDN 圖片: {len(imgs_cdn)}')
        if imgs_cdn:
            print('範例:', imgs_cdn[:3])

        # 找 productId 或 goodsNo
        ids = re.findall(r'goodsNo["\s:]+(\d{7,})', html)
        print(f'\ngoodsNo: {ids[:5]}')

        # 存 HTML 片段
        with open('momo_sample.html', 'w', encoding='utf-8') as f:
            # 找含 price 的段落
            idx = html.find('"price": 22138')
            if idx > 0:
                f.write(html[max(0,idx-500):idx+500])
                print('\n已存含 price 的 HTML 片段')

        await browser.close()

asyncio.run(test_momo())

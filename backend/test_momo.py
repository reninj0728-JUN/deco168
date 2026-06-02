import asyncio, sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

async def test_momo():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://www.momoshop.com.tw/search/searchShop.jsp?keyword=%E5%8C%97%E6%AD%90%E6%B2%99%E7%99%BC&searchType=1&size=12&page=1', timeout=30000)
        await asyncio.sleep(5)

        for sel in ['.prdListArea li', '.goodsItemLi', '[class*=prd]', 'li.goodsItem']:
            els = await page.query_selector_all(sel)
            print(f'{sel}: {len(els)}')

        html = await page.content()
        print('HTML 長度:', len(html))
        prices = re.findall(r'NT\$[\d,]+', html)
        print('找到價格:', prices[:5])
        img_urls = re.findall(r'https://i[12]\.momoshop\.com\.tw[^"\']+\.jpg', html)
        print('找到圖片:', len(img_urls), img_urls[:2] if img_urls else '')
        names = re.findall(r'"productName"\s*:\s*"([^"]+)"', html)
        print('找到品名:', names[:3])
        await browser.close()

asyncio.run(test_momo())

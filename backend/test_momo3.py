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

        # 抓所有 schema.org JSON-LD
        ld_blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
        print(f'找到 LD+JSON 區塊: {len(ld_blocks)}')

        products = []
        for block in ld_blocks:
            try:
                data = json.loads(block.strip())
                # 可能是 array 或單一物件
                if isinstance(data, list):
                    items = data
                else:
                    items = [data]
                for item in items:
                    if item.get('@type') == 'Product':
                        products.append(item)
                    elif item.get('@type') == 'ItemList':
                        for el in item.get('itemListElement', []):
                            if el.get('item', {}).get('@type') == 'Product':
                                products.append(el['item'])
            except:
                pass

        print(f'找到 Product: {len(products)}')
        for p in products[:3]:
            print(f"\n名稱: {p.get('name','')}")
            print(f"圖片: {p.get('image','')[:80]}")
            offers = p.get('offers', {})
            if isinstance(offers, list): offers = offers[0]
            print(f"價格: {offers.get('price','')} {offers.get('priceCurrency','')}")
            print(f"URL: {p.get('url','')[:80]}")

        await browser.close()

asyncio.run(test_momo())

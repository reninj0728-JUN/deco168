import asyncio, sys, io, re, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

async def test_momo():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        # 攔截 API 回應
        api_data = []
        async def handle_response(response):
            if 'searchShop' in response.url or 'search' in response.url:
                if 'json' in response.headers.get('content-type', ''):
                    try:
                        body = await response.json()
                        api_data.append({'url': response.url[:80], 'data': body})
                        print(f'API: {response.url[:80]}')
                    except:
                        pass

        page.on('response', handle_response)

        await page.goto('https://www.momoshop.com.tw/search/searchShop.jsp?keyword=%E5%8C%97%E6%AD%90%E6%B2%99%E7%99%BC&size=12&page=1', timeout=30000)
        await asyncio.sleep(6)

        # 看看抓到什麼 API
        print(f'\n攔截到 {len(api_data)} 個 API')
        for a in api_data[:3]:
            print(f'  URL: {a["url"]}')
            print(f'  Keys: {list(a["data"].keys())[:5] if isinstance(a["data"], dict) else type(a["data"])}')

        # 直接找 script 裡的 JSON
        html = await page.content()
        # 找 window.__INITIAL_STATE__ 或類似
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.{100,}}?);\s*</script>', html)
        if m:
            print('\n找到 __INITIAL_STATE__')
            try:
                state = json.loads(m.group(1))
                print('Keys:', list(state.keys())[:10])
            except:
                print('JSON 解析失敗')

        # 找任何含 NT$ 或圖片的部分
        chunks = re.findall(r'\{[^{}]{50,500}"price[^{}]{10,300}\}', html)
        print(f'\n找到 price chunks: {len(chunks)}')
        if chunks:
            print('範例:', chunks[0][:200])

        await browser.close()

asyncio.run(test_momo())

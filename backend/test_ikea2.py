import requests
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "application/json, */*",
}

# IKEA 的內部搜尋 API（可從瀏覽器 Network tab 找到）
tests = [
    # 搜尋 API v1
    "https://sik.search.blue.cdtapps.com/tw/zh/search-result-page?q=%E6%B2%99%E7%99%BC&size=5",
    # 舊版搜尋
    "https://www.ikea.com/tw/zh/search/products/?q=%E6%B2%99%E7%99%BC",
    # Product listing page（沙發類別）
    "https://www.ikea.com/tw/zh/cat/sofas-fu003/",
]

for url in tests:
    print(f"\nGET {url[:80]}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        print(f"  Status: {resp.status_code}, Size: {len(resp.text)} chars")
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            data = resp.json()
            print(f"  JSON keys: {list(data.keys())[:5]}")
        elif resp.status_code == 200:
            # 找產品連結
            import re
            links = re.findall(r'href="(/tw/zh/p/[^"]+)"', resp.text)
            print(f"  Product links: {len(links)}")
            for l in links[:3]:
                print(f"    {l}")
    except Exception as e:
        print(f"  ERROR: {e}")

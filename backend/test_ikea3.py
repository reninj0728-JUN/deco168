import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 先試首頁看是否能連上
tests = [
    "https://www.ikea.com/tw/zh/",
    "https://www.ikea.com/",
]

for url in tests:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        print(f"URL: {url}")
        print(f"  Final URL: {resp.url}")
        print(f"  Status: {resp.status_code}")
        print(f"  Size: {len(resp.text)}")
        print(f"  Title: {resp.text[resp.text.find('<title>')+7:resp.text.find('</title>')][:60]}")
        print()
    except Exception as e:
        print(f"ERROR {url}: {e}")

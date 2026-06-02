import requests
from bs4 import BeautifulSoup
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 測試搜尋「沙發」
url = "https://www.ikea.com/tw/zh/search/?q=%E6%B2%99%E7%99%BC"
print(f"GET {url}")
resp = requests.get(url, headers=HEADERS, timeout=15)
print(f"Status: {resp.status_code}")
print(f"Page size: {len(resp.text)} chars")

soup = BeautifulSoup(resp.text, "html.parser")

# 找 JSON-LD
json_ld = soup.find_all("script", type="application/ld+json")
print(f"JSON-LD blocks: {len(json_ld)}")
for i, tag in enumerate(json_ld[:3]):
    try:
        data = json.loads(tag.string or "")
        t = data.get("@type", "?") if isinstance(data, dict) else f"list[{len(data)}]"
        print(f"  [{i}] type={t}")
        if isinstance(data, dict) and data.get("@type") == "Product":
            print(f"       name={data.get('name','')[:40]}")
    except Exception:
        pass

# 找各種可能的產品容器
for sel in ["article", "[data-ref-id]", ".pip-product-compact", "[class*='product']"]:
    found = soup.select(sel)
    if found:
        print(f"Selector '{sel}': {len(found)} elements")

# 找所有 <a> 含 /p/ 的連結（IKEA 產品 URL 格式）
product_links = [a["href"] for a in soup.find_all("a", href=True) if "/p/" in a.get("href", "")]
print(f"\nProduct links (/p/): {len(product_links)}")
for link in product_links[:5]:
    print(f"  {link}")

# 印出 HTML 開頭結構
print("\n--- HTML STRUCTURE (first 1000 chars) ---")
print(resp.text[:1000])

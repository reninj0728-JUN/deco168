"""
PChome 24h 傢俱爬蟲（Playwright + JSON-LD）
目標：補充法式/新中式/地中海 等稀少風格商品
結果存到 furniture_raw_pchome.json
用法：python3.11 scraper_pchome.py
"""
import asyncio, json, sys, io, re
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RAW_PATH = Path(__file__).parent / "furniture_raw_pchome.json"
BASE_URL  = "https://24h.pchome.com.tw"

# 針對性補充稀少風格
SEARCHES = [
    # 法式浪漫
    ("法式沙發",     "沙發",  "french"),
    ("法式床架",     "床架",  "french"),
    ("弧形沙發",     "沙發",  "french"),
    ("絲絨沙發",     "沙發",  "french"),
    ("歐式傢俱",     "傢俱",  "french"),
    ("藤編椅",       "椅子",  "french"),
    ("法式茶几",     "茶几",  "french"),
    # 新中式
    ("新中式傢俱",   "傢俱",  "chinese-modern"),
    ("新中式沙發",   "沙發",  "chinese-modern"),
    ("新中式床架",   "床架",  "chinese-modern"),
    ("禪風茶几",     "茶几",  "chinese-modern"),
    ("格柵電視櫃",   "收納",  "chinese-modern"),
    ("實木電視櫃",   "收納",  "chinese-modern"),
    ("新中式餐桌",   "桌子",  "chinese-modern"),
    # 地中海
    ("地中海傢俱",   "傢俱",  "mediterranean"),
    ("地中海風格",   "傢俱",  "mediterranean"),
    ("藍白傢俱",     "傢俱",  "mediterranean"),
    # 其他稀少
    ("奶油風沙發",   "沙發",  "cream"),
    ("布克萊沙發",   "沙發",  "cream"),
    ("原木傢俱",     "傢俱",  "wood"),
]


async def get_ids_from_search(page, keyword: str) -> list[str]:
    all_ids = set()
    for pg in range(1, 4):  # 最多抓3頁
        url = f"{BASE_URL}/search/?q={keyword}&page={pg}"
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            await asyncio.sleep(4)
            for y in range(0, 4000, 700):
                await page.evaluate(f"window.scrollTo(0, {y})")
                await asyncio.sleep(0.2)
            await asyncio.sleep(2)
            content = await page.content()
            ids = set(re.findall(r'/prod/([A-Z0-9\-]{10,})', content))
            before = len(all_ids)
            all_ids |= ids
            if len(all_ids) == before:  # 沒有新增代表到底了
                break
        except Exception as e:
            print(f"  Search [{keyword}] p{pg} ERROR: {e}")
            break
    return list(all_ids)


async def get_product_detail(page, prod_id: str) -> dict | None:
    url = f"{BASE_URL}/prod/{prod_id}"
    try:
        await page.goto(url, wait_until="load", timeout=25000)
        await asyncio.sleep(2)
        content = await page.content()

        jsonld_blocks = re.findall(
            r'application/ld\+json[^>]*>(.*?)</script>',
            content, re.DOTALL
        )
        for block in jsonld_blocks:
            try:
                raw = json.loads(block.strip())
                items = raw if isinstance(raw, list) else [raw]
                for data in items:
                    if not isinstance(data, dict) or data.get("@type") != "Product":
                        continue
                    name = data.get("name", "")
                    images = data.get("image", [])
                    img_url = images[0] if isinstance(images, list) and images else (images or "")
                    offers = data.get("offers", {})
                    price_str = offers.get("price", "0") if isinstance(offers, dict) else "0"
                    try:
                        price_twd = int(float(str(price_str).replace(",", "")))
                    except:
                        price_twd = 0
                    if name and img_url:
                        return {
                            "id": f"pchome_{prod_id}",
                            "name_zh": name[:40],
                            "price_twd": price_twd,
                            "image_url": img_url,
                            "purchase_url": url,
                            "source": "pchome",
                        }
            except:
                continue
        return None
    except Exception as e:
        return None


async def main():
    all_items = []
    seen_ids = set()
    found: dict[str, tuple] = {}  # prod_id -> (category, style_hint)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124",
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        print("=== Step 1: 搜尋商品 ID ===")
        for keyword, zh_cat, style_hint in SEARCHES:
            ids = await get_ids_from_search(page, keyword)
            new = 0
            for pid in ids:
                if pid not in found:
                    found[pid] = (zh_cat, style_hint)
                    new += 1
            print(f"  [{keyword}] {len(ids)} 件 (+{new} 新)")
            await asyncio.sleep(1)

        print(f"\n總共找到 {len(found)} 個不重複商品 ID")

        print("\n=== Step 2: 爬商品詳細頁 ===")
        id_list = list(found.items())
        for i, (prod_id, (zh_cat, style_hint)) in enumerate(id_list):
            detail = await get_product_detail(page, prod_id)
            if detail:
                detail["category"] = zh_cat
                detail["style_hint"] = style_hint
                if detail["id"] not in seen_ids:
                    seen_ids.add(detail["id"])
                    all_items.append(detail)
            if (i + 1) % 20 == 0:
                print(f"  進度: {i+1}/{len(id_list)} | 已抓: {len(all_items)}")
            await asyncio.sleep(0.5)

        await browser.close()

    print(f"\n總計: {len(all_items)} 件")
    RAW_PATH.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"已存 → {RAW_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

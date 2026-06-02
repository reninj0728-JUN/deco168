"""
1688 真實傢俱爬蟲 + Gemini 視覺風格分類
用法：python3.11 scraper_1688.py
結果：furniture_real.json（真實商品，含圖片網址）
"""
import asyncio, json, os, sys, io, time, re, base64
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

CATALOG_PATH  = Path(__file__).parent / "furniture_catalog.json"
REAL_OUT_PATH = Path(__file__).parent / "furniture_real.json"

VALID_STYLES = ["modern","japanese","luxury","nordic","industrial","mediterranean","muji","art-deco","boho"]

# 9種風格的1688搜尋關鍵字
STYLE_KEYWORDS = {
    "modern":        ["现代简约沙发","现代简约餐桌","现代简约床架","现代简约茶几","现代简约电视柜","现代简约书桌","现代简约餐椅","极简风格沙发"],
    "nordic":        ["北欧风沙发","北欧原木餐桌","北欧风床架","北欧实木茶几","北欧风餐椅","北欧风台灯","北欧风装饰画","北欧布艺沙发"],
    "japanese":      ["日式榻榻米床","日式矮桌","日式实木床架","日式茶几","日式原木餐椅","日式竹帘","侘寂风沙发","日式木质收纳"],
    "muji":          ["无印风布艺沙发","原木简约餐桌","棉麻沙发","实木茶几简约","无印风床架","原木储物柜","棉麻窗帘","简约原木书架"],
    "luxury":        ["轻奢沙发","轻奢餐桌","大理石茶几","轻奢床架","轻奢电视柜","轻奢餐椅","金属边几","轻奢吊灯"],
    "art-deco":      ["艺术装饰镜子","黄铜装饰灯","复古艺术沙发","几何图案地毯","Art Deco灯具","黄铜茶几","艺术装饰书柜","丝绒沙发"],
    "boho":          ["波西米亚沙发","藤编椅子","编织挂毯","波西米亚地毯","藤编茶几","麻绳装饰","流苏靠枕","编织落地灯"],
    "industrial":    ["工业风铁艺餐桌","工业风书架","工业风铁艺灯","复古铁艺茶几","工业风餐椅","仿旧木板桌","铁艺置物架","工业风床架"],
    "mediterranean": ["地中海风沙发","蓝白风格床","地中海风餐桌","藤编户外椅","地中海装饰","蓝色陶瓷花器","白色藤编灯","地中海风窗帘"],
}

# 類別對應
CATEGORY_MAP = {
    "沙发": "沙發", "餐桌": "餐桌", "床": "床架", "茶几": "茶几",
    "电视柜": "電視櫃", "书桌": "書桌", "餐椅": "餐椅", "台灯": "燈具",
    "吊灯": "燈具", "落地灯": "燈具", "地毯": "地毯", "窗帘": "窗簾",
    "书架": "書架", "置物架": "收納", "储物柜": "收納", "镜子": "裝飾",
    "装饰画": "裝飾", "花器": "裝飾", "靠枕": "抱枕", "挂毯": "裝飾",
}

def guess_category(title: str) -> str:
    for kw, cat in CATEGORY_MAP.items():
        if kw in title:
            return cat
    return "傢俱"

def clean_price(text: str) -> float:
    nums = re.findall(r'[\d.]+', text.replace(',',''))
    return float(nums[0]) if nums else 0.0

async def scrape_1688_keyword(page, keyword: str, style: str, max_items: int = 15) -> list:
    results = []
    url = f"https://s.1688.com/selloffer/offerlist.htm?keywords={keyword}&n=y&sortType=pr_score"
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # 等商品卡片出現
        try:
            await page.wait_for_selector('.offer-list-row, .sm-offer-item, [class*="offer"]', timeout=10000)
        except:
            pass

        # 抓產品卡片 - 試多種 selector
        cards = await page.query_selector_all('.offer-list-row .sm-offer-item')
        if not cards:
            cards = await page.query_selector_all('[data-spm-click*="offer"], .offer-item, .sm-offer-item')
        if not cards:
            # 試抓所有含圖片+價格的區塊
            cards = await page.query_selector_all('li[class*="offer"]')

        print(f"    [{style}] {keyword}: 找到 {len(cards)} 個卡片")

        for card in cards[:max_items]:
            try:
                # 標題
                title_el = await card.query_selector('[class*="title"], h2, .title')
                title = (await title_el.inner_text()).strip() if title_el else ""
                if not title or len(title) < 2:
                    continue

                # 價格
                price_el = await card.query_selector('[class*="price"], .price, em')
                price_text = (await price_el.inner_text()).strip() if price_el else "0"
                price_rmb = clean_price(price_text)

                # 圖片
                img_el = await card.query_selector('img[src*="alicdn"], img[data-src*="alicdn"], img[src*="1688"]')
                if not img_el:
                    img_el = await card.query_selector('img')
                img_url = ""
                if img_el:
                    img_url = await img_el.get_attribute('src') or await img_el.get_attribute('data-src') or ""
                    # 轉成 https
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    # 去掉尺寸限制，取高解析
                    img_url = re.sub(r'_\d+x\d+\.jpg', '_400x400.jpg', img_url)

                # 連結
                link_el = await card.query_selector('a[href*="detail.1688"], a[href*="offer"]')
                product_url = ""
                if link_el:
                    product_url = await link_el.get_attribute('href') or ""
                    if product_url.startswith('//'):
                        product_url = 'https:' + product_url

                if not img_url or not product_url:
                    continue

                category = guess_category(title)
                # 轉換台幣（約 1 RMB = 4.5 TWD，批發×2.5 零售估算）
                price_twd = int(price_rmb * 4.5 * 2) if price_rmb > 0 else 0

                results.append({
                    "title_zh": title,
                    "style": style,
                    "category": category,
                    "price_rmb": price_rmb,
                    "price_twd": price_twd,
                    "image_url": img_url,
                    "purchase_url": product_url,
                    "keyword": keyword,
                })
            except Exception as e:
                continue

    except Exception as e:
        print(f"    ERROR {keyword}: {e}")
    return results


async def main_scrape():
    all_items = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            locale="zh-TW",
        )
        page = await ctx.new_page()

        # 先進 1688 首頁讓 cookie 初始化
        await page.goto("https://www.1688.com/", timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        for style, keywords in STYLE_KEYWORDS.items():
            print(f"\n[{style}] 開始爬取...")
            style_items = []
            for kw in keywords[:4]:  # 每種風格先抓 4 個關鍵字
                items = await scrape_1688_keyword(page, kw, style, max_items=12)
                style_items.extend(items)
                await asyncio.sleep(2)  # 避免太快被擋
            print(f"  [{style}] 共抓到 {len(style_items)} 件")
            all_items.extend(style_items)

        await browser.close()

    # 去重（依 purchase_url）
    seen = set()
    unique = []
    for item in all_items:
        key = item['purchase_url']
        if key not in seen:
            seen.add(key)
            unique.append(item)

    print(f"\n總計抓到 {len(unique)} 件（去重後）")

    # 存暫存檔
    with open(REAL_OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)
    print(f"已存至 {REAL_OUT_PATH}")
    return unique


# ─── Gemini 視覺分類 ────────────────────────────────────────────────────────────

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')

CLASSIFY_PROMPT = """你是專業室內設計師，請分析這張傢俱產品圖。

回傳 JSON（嚴格照格式，不要多餘文字）：
{
  "name_zh": "簡潔繁體中文品名，15字以內",
  "style_tags": ["主風格"],
  "colors": ["主色1","主色2"],
  "category": "沙發/床架/餐桌/茶几/餐椅/書桌/燈具/地毯/窗簾/收納/裝飾/抱枕 之一",
  "dimensions_note": "尺寸備註（若圖片看得出來）",
  "flux_descriptor": "英文逗號分隔，描述這件傢俱外觀給AI渲染用，30字以內"
}

style_tags 只能選一個，從以下選：modern / nordic / japanese / muji / luxury / art-deco / boho / industrial / mediterranean"""


async def classify_with_gemini(items: list) -> list:
    if not GEMINI_KEY:
        print("警告：沒有 GEMINI_API_KEY，跳過視覺分類")
        return items

    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_KEY)

    import requests as req_lib
    enriched = []
    for i, item in enumerate(items):
        print(f"  Gemini 分類 [{i+1}/{len(items)}] {item['title_zh'][:20]}...")
        try:
            # 抓圖片
            img_resp = req_lib.get(item['image_url'], timeout=10)
            if not img_resp.ok:
                raise Exception(f"圖片抓取失敗 {img_resp.status_code}")
            img_b64 = base64.b64encode(img_resp.content).decode()

            resp = client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=[
                    types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg"),
                    CLASSIFY_PROMPT,
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            classified = json.loads(resp.text)

            enriched.append({
                "id": f"real-{item['style'][:3]}-{i+1:04d}",
                "name_zh":      classified.get("name_zh", item["title_zh"][:15]),
                "brand":        "1688",
                "category":     classified.get("category", item["category"]),
                "style_tags":   classified.get("style_tags", [item["style"]]),
                "keywords":     [item["keyword"]],
                "colors":       classified.get("colors", []),
                "price_twd":    item["price_twd"],
                "price_rmb":    item["price_rmb"],
                "image_url":    item["image_url"],
                "purchase_url": item["purchase_url"],
                "dimensions":   classified.get("dimensions_note", ""),
                "flux_descriptor": classified.get("flux_descriptor", ""),
            })
            time.sleep(0.5)  # rate limit

        except Exception as e:
            print(f"    Gemini 失敗：{e}，用原始資料")
            enriched.append({
                "id": f"real-{item['style'][:3]}-{i+1:04d}",
                "name_zh":      item["title_zh"][:15],
                "brand":        "1688",
                "category":     item["category"],
                "style_tags":   [item["style"]],
                "keywords":     [item["keyword"]],
                "colors":       [],
                "price_twd":    item["price_twd"],
                "price_rmb":    item["price_rmb"],
                "image_url":    item["image_url"],
                "purchase_url": item["purchase_url"],
                "dimensions":   "",
                "flux_descriptor": item["title_zh"][:30],
            })

    return enriched


async def main():
    print("=" * 56)
    print("Step 1: 爬取 1688 真實傢俱商品")
    print("=" * 56)
    raw_items = await main_scrape()

    if not raw_items:
        print("沒有抓到任何商品，請檢查網路或重試")
        return

    print(f"\n{'='*56}")
    print(f"Step 2: Gemini 視覺分類（共 {len(raw_items)} 件）")
    print("=" * 56)
    # 每次最多分類 200 件，避免 API 費用過高
    to_classify = raw_items[:200]
    classified = await classify_with_gemini(to_classify)

    # 存分類結果
    classified_path = Path(__file__).parent / "furniture_classified.json"
    with open(classified_path, 'w', encoding='utf-8') as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)
    print(f"\nGemini 分類完成，共 {len(classified)} 件")
    print(f"結果存至 {classified_path}")

    # 統計
    from collections import Counter
    styles = Counter(item['style_tags'][0] for item in classified if item.get('style_tags'))
    print("\n風格分布：")
    for s, c in sorted(styles.items()):
        print(f"  {s:<15}: {c} 件")


if __name__ == "__main__":
    asyncio.run(main())

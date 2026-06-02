"""
momo 購物網 真實傢俱爬蟲 + Gemini 視覺風格分類
用法：python3.11 scraper_momo.py
結果：furniture_real.json → Gemini 分類後寫入 furniture_catalog_real.json
"""
import asyncio, json, os, sys, io, time, re, base64
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 載入 .env
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

CATALOG_REAL_PATH = Path(__file__).parent / "furniture_catalog_real.json"
RAW_PATH          = Path(__file__).parent / "furniture_raw_momo.json"

VALID_STYLES = ["modern","japanese","luxury","nordic","industrial","mediterranean","muji","art-deco","boho"]

# 每種風格的 momo 搜尋關鍵字
STYLE_KEYWORDS = {
    "modern":        ["現代簡約沙發","現代簡約餐桌","現代風床架","極簡茶几","現代簡約電視櫃"],
    "nordic":        ["北歐風沙發","北歐原木餐桌","北歐風床架","北歐風燈具","北歐風裝飾"],
    "japanese":      ["日式沙發","日式矮桌","日式床架","日式茶几","日式風格傢俱"],
    "muji":          ["無印風沙發","原木簡約餐桌","棉麻沙發","原木收納","簡約床架"],
    "luxury":        ["輕奢沙發","輕奢餐桌","大理石茶几","輕奢床架","輕奢電視櫃"],
    "art-deco":      ["復古沙發","黃銅燈具","絲絨沙發","幾何地毯","藝術裝飾鏡"],
    "boho":          ["波西米亞地毯","藤編椅子","編織燈具","波西米亞沙發","藤編茶几"],
    "industrial":    ["工業風沙發","工業風餐桌","鐵藝燈具","工業風書架","復古工業風椅"],
    "mediterranean": ["地中海風沙發","藤編戶外椅","白色藤編燈","地中海風裝飾","海洋風寢具"],
}

def is_product_image(url: str) -> bool:
    """過濾掉 badge/banner，只留真實商品圖"""
    if not url:
        return False
    # 真實商品圖：i[0-9].momoshop.com.tw/.../goodsimg/...
    if 'goodsimg' in url and re.search(r'i[0-9]\.momoshop\.com\.tw', url):
        return True
    return False

def clean_price(text: str) -> int:
    nums = re.findall(r'[\d,]+', text.replace(',',''))
    return int(nums[0]) if nums else 0

def clean_url(url: str) -> str:
    """移除 momo 追蹤參數，保留核心連結"""
    return re.sub(r'&(Area|md[^&]*)=[^&]*', '', url)

def guess_category(name: str) -> str:
    mapping = [
        (['沙發','sofa','單人椅','布椅'], '沙發'),
        (['餐桌','書桌','工作桌','咖啡桌'], '桌子'),
        (['餐椅','椅子','椅','凳'], '椅子'),
        (['床架','床組','床頭板'], '床架'),
        (['茶几','邊几','角几'], '茶几'),
        (['電視櫃','書架','收納','置物'], '收納'),
        (['燈','吊燈','台燈','落地燈'], '燈具'),
        (['地毯','毯'], '地毯'),
        (['窗簾','遮光'], '窗簾'),
        (['鏡','裝飾','擺件','花器'], '裝飾'),
        (['抱枕','靠枕'], '抱枕'),
        (['寢具','被','枕'], '寢具'),
    ]
    for keywords, cat in mapping:
        if any(kw in name for kw in keywords):
            return cat
    return '傢俱'


async def scrape_momo_keyword(page, keyword: str, style: str, pages: int = 2) -> list:
    results = []
    for pg in range(1, pages + 1):
        url = f"https://www.momoshop.com.tw/search/searchShop.jsp?keyword={keyword}&size=24&page={pg}"
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            items = await page.evaluate('''() => {
                const results = [];
                const lis = document.querySelectorAll("li");
                for (const li of lis) {
                    const img = li.querySelector("img");
                    const a = li.querySelector('a[href*="GoodsDetail"]');
                    const priceEl = li.querySelector('[class*="price"], [class*="Price"]');
                    const nameEl = li.querySelector('[class*="name"], [class*="Name"], h3, h4, p');
                    if (img && a && img.src) {
                        results.push({
                            name: nameEl ? nameEl.innerText.trim() : "",
                            img:  img.src || img.dataset.src || "",
                            url:  a.href || "",
                            price: priceEl ? priceEl.innerText.trim() : "",
                        });
                    }
                }
                return results;
            }''')

            for item in items:
                if not is_product_image(item['img']):
                    continue
                name = item['name'].strip()
                if len(name) < 3:
                    continue
                price_twd = clean_price(item['price'])
                results.append({
                    "title_zh":    name,
                    "style":       style,
                    "keyword":     keyword,
                    "category":    guess_category(name),
                    "price_twd":   price_twd,
                    "image_url":   item['img'],
                    "purchase_url": clean_url(item['url']),
                })

            print(f"    [{style}] '{keyword}' p{pg}: +{len(items)} → 有效 {len(results)}")
            await asyncio.sleep(1.5)

        except Exception as e:
            print(f"    ERROR [{style}] '{keyword}' p{pg}: {e}")

    return results


async def main_scrape() -> list:
    all_items = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            locale="zh-TW",
        )
        page = await ctx.new_page()

        for style, keywords in STYLE_KEYWORDS.items():
            print(f"\n[{style}] 開始爬取...")
            style_items = []
            for kw in keywords:
                items = await scrape_momo_keyword(page, kw, style, pages=2)
                style_items.extend(items)

            # 去重（同一 style 內依 purchase_url）
            seen = set()
            unique = []
            for it in style_items:
                if it['purchase_url'] not in seen:
                    seen.add(it['purchase_url'])
                    unique.append(it)
            print(f"  [{style}] 共 {len(unique)} 件（去重後）")
            all_items.extend(unique)

        await browser.close()

    # 全域去重
    seen = set()
    unique_all = []
    for it in all_items:
        if it['purchase_url'] not in seen:
            seen.add(it['purchase_url'])
            unique_all.append(it)

    print(f"\n總計 {len(unique_all)} 件（全域去重後）")
    with open(RAW_PATH, 'w', encoding='utf-8') as f:
        json.dump(unique_all, f, ensure_ascii=False, indent=2)
    print(f"原始資料存至 {RAW_PATH}")
    return unique_all


# ─── Gemini 視覺分類 ────────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """你是專業室內設計師，請仔細分析這張傢俱產品圖。

回傳 JSON（嚴格照格式，不要多餘文字）：
{
  "name_zh": "繁體中文品名，精簡15字以內",
  "style_tags": ["最符合的風格"],
  "colors": ["主色1","主色2"],
  "category": "沙發/床架/桌子/椅子/茶几/收納/燈具/地毯/窗簾/裝飾/抱枕/寢具/傢俱 之一",
  "dimensions": "若圖中可見尺寸標示則填入，否則留空",
  "flux_descriptor": "英文，描述此傢俱給AI圖像生成用，30字以內，包含材質顏色形狀"
}

style_tags 只能填一個，從以下選擇：
modern / nordic / japanese / muji / luxury / art-deco / boho / industrial / mediterranean

判斷標準：
- modern：線條簡潔、灰白黑色系、金屬腳
- nordic：原木、白色、簡約溫暖
- japanese：低矮、原木、禪意、侘寂
- muji：棉麻、原木、無印良品感
- luxury：大理石、金屬邊框、絲絨、輕奢
- art-deco：幾何圖案、黃銅、絲絨、復古摩登
- boho：藤編、流蘇、編織、波西米亞
- industrial：鐵件、原木、仿舊、工業感
- mediterranean：藤編、白色、藍色、海洋感"""


async def classify_with_gemini(items: list) -> list:
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if not gemini_key:
        print("警告：沒有 GEMINI_API_KEY，跳過視覺分類，使用關鍵字分類")
        return [_fallback_item(it, i) for i, it in enumerate(items)]

    from google import genai
    from google.genai import types
    import requests as req_lib

    client = genai.Client(api_key=gemini_key)
    enriched = []
    failed = 0

    for i, item in enumerate(items):
        print(f"  [{i+1}/{len(items)}] Gemini 分析: {item['title_zh'][:25]}...")
        try:
            img_resp = req_lib.get(item['image_url'], timeout=10)
            if not img_resp.ok:
                raise Exception(f"圖片抓取失敗")

            mime = "image/webp" if item['image_url'].endswith('.webp') else "image/jpeg"
            resp = client.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=[
                    types.Part.from_bytes(data=img_resp.content, mime_type=mime),
                    CLASSIFY_PROMPT,
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            classified = json.loads(resp.text)
            enriched.append(_build_item(item, classified, i))
            time.sleep(0.4)

        except Exception as e:
            print(f"    Gemini 失敗：{e}")
            enriched.append(_fallback_item(item, i))
            failed += 1

    print(f"\nGemini 分類完成：{len(enriched)} 件，失敗 {failed} 件")
    return enriched


def _build_item(raw: dict, classified: dict, idx: int) -> dict:
    style = classified.get('style_tags', [raw['style']])[0]
    if style not in VALID_STYLES:
        style = raw['style']
    return {
        "id":           f"momo-{style[:3]}-{idx+1:04d}",
        "name_zh":      classified.get('name_zh', raw['title_zh'])[:20],
        "brand":        "momo",
        "category":     classified.get('category', raw['category']),
        "style_tags":   [style],
        "keywords":     [raw['keyword']],
        "colors":       classified.get('colors', []),
        "price_twd":    raw['price_twd'],
        "image_url":    raw['image_url'],
        "purchase_url": raw['purchase_url'],
        "dimensions":   classified.get('dimensions', ''),
        "flux_descriptor": classified.get('flux_descriptor', ''),
    }


def _fallback_item(raw: dict, idx: int) -> dict:
    return {
        "id":           f"momo-{raw['style'][:3]}-{idx+1:04d}",
        "name_zh":      raw['title_zh'][:20],
        "brand":        "momo",
        "category":     raw['category'],
        "style_tags":   [raw['style']],
        "keywords":     [raw['keyword']],
        "colors":       [],
        "price_twd":    raw['price_twd'],
        "image_url":    raw['image_url'],
        "purchase_url": raw['purchase_url'],
        "dimensions":   "",
        "flux_descriptor": raw['title_zh'][:30],
    }


async def main():
    print("=" * 60)
    print("DECO168 真實傢俱目錄建立工具 — momo 台灣")
    print("=" * 60)

    # Step 1: 爬取
    print("\n[Step 1] 爬取 momo 真實商品...")
    raw = await main_scrape()
    if not raw:
        print("沒有抓到任何商品，請檢查網路")
        return

    # Step 2: Gemini 分類 — 每種風格最多取 35 件，確保 9 種都有
    per_style = 35
    to_classify = []
    from collections import defaultdict
    by_style = defaultdict(list)
    for it in raw:
        by_style[it['style']].append(it)
    for style in VALID_STYLES:
        to_classify.extend(by_style[style][:per_style])

    print(f"\n[Step 2] Gemini 視覺分類（{len(to_classify)} 件，每風格最多 {per_style} 件）...")
    classified = await classify_with_gemini(to_classify)

    # Step 3: 存檔
    with open(CATALOG_REAL_PATH, 'w', encoding='utf-8') as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)

    # 統計
    from collections import Counter
    styles = Counter(it['style_tags'][0] for it in classified if it.get('style_tags'))
    cats   = Counter(it['category'] for it in classified)

    print(f"\n{'='*60}")
    print(f"完成！共 {len(classified)} 件真實商品")
    print(f"存至 {CATALOG_REAL_PATH}")
    print("\n風格分布：")
    for s in VALID_STYLES:
        print(f"  {s:<15}: {styles.get(s, 0)} 件")
    print("\n類別分布（前5）：")
    for cat, cnt in cats.most_common(5):
        print(f"  {cat}: {cnt}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

"""
從已爬取的 furniture_raw_momo.json 重新清洗 + Gemini 分類
只跑分類，不重新爬蟲（省時間）
"""
import asyncio, json, os, sys, io, time, re
from pathlib import Path
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 載入 .env
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

RAW_PATH  = Path(__file__).parent / "furniture_raw_momo.json"
OUT_PATH  = Path(__file__).parent / "furniture_catalog_real.json"
VALID_STYLES = ["modern","japanese","luxury","nordic","industrial","mediterranean","muji","art-deco","boho"]

# 促銷/垃圾文字過濾
JUNK_PATTERNS = [
    r'^滿\d',           # 滿12000折600 / 滿5000折300
    r'^買\d+送\d+',
    r'^\d+折',
    r'^\d+%',
    r'^限時',
    r'^特價',
    r'^最低',
    r'^現省',
    r'^\[',
    r'^【限',
    r'^MIT\s',          # MIT台灣製造
    r'^\d+CM',          # 120CM中古風
    r'^部分地區',
    r'^台灣製造可',
    r'^台灣與歐美',
    r'^下單\d+折',
    r'^一般地區',
    r'^適用於\d+',
    r'^小戶型',
    r'^單人即可',
    r'^高穩定',
    r'^高彈力',
    r'^高回彈',
    r'^磨毛',
    r'^六角',
    r'^疊摞',
    r'^拿取方便',
    r'^折疊收納',
    r'^可收納',
]

def is_junk_name(name: str) -> bool:
    if len(name) < 5:
        return True
    for pat in JUNK_PATTERNS:
        if re.match(pat, name):
            return True
    # 含太多數字折扣字眼
    if re.search(r'折\d+|折300|折600|折100', name):
        return True
    return False

CATEGORY_RULES = [
    (['L型沙發','三人沙發','雙人沙發','單人沙發','沙發床','貓抓布沙發','布沙發','皮沙發','沙發椅','懶人沙發'], '沙發'),
    (['餐桌','書桌','工作桌','電腦桌','咖啡桌','飯桌','摺疊桌'], '桌子'),
    (['餐椅','辦公椅','電腦椅','休閒椅','搖椅','藤椅','單椅','吧椅','吧台椅'], '椅子'),
    (['床架','床組','床頭板','掀床','收納床','雙人床','單人床架'], '床架'),
    (['茶几','邊几','角几','矮桌'], '茶几'),
    (['電視櫃','書架','書櫃','收納櫃','置物架','衣櫃','斗櫃','抽屜'], '收納'),
    (['吊燈','台燈','落地燈','壁燈','燈具','燈飾'], '燈具'),
    (['地毯','毯子'], '地毯'),
    (['窗簾','遮光簾','捲簾','百葉'], '窗簾'),
    (['掛鏡','裝飾鏡','掛畫','壁畫','花器','擺件','時鐘'], '裝飾'),
    (['抱枕','靠枕','枕頭'], '抱枕'),
    (['被','寢具','床單','床包','枕套'], '寢具'),
]

def detect_category(name: str) -> str:
    for keywords, cat in CATEGORY_RULES:
        if any(kw in name for kw in keywords):
            return cat
    return '傢俱'

CLASSIFY_PROMPT = """你是專業室內設計師，請仔細分析這張傢俱產品圖。

回傳 JSON（嚴格照格式，不要多餘文字）：
{
  "name_zh": "繁體中文品名，精簡15字以內，只描述產品本身",
  "style_tags": ["最符合的一個風格"],
  "colors": ["主色1","主色2"],
  "category": "沙發/床架/桌子/椅子/茶几/收納/燈具/地毯/窗簾/裝飾/抱枕/寢具/傢俱",
  "dimensions": "尺寸（若圖中可見則填，否則留空字串）",
  "flux_descriptor": "英文，30字以內，描述此傢俱外觀顏色材質給AI圖像生成用"
}

style_tags 只能選一個：modern/nordic/japanese/muji/luxury/art-deco/boho/industrial/mediterranean"""

async def classify_all(raw_items: list) -> list:
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if not gemini_key:
        print("ERROR: GEMINI_API_KEY 未設定")
        sys.exit(1)

    from google import genai
    from google.genai import types
    import requests as req_lib

    client = genai.Client(api_key=gemini_key)
    enriched = []
    ok_count = 0
    fail_count = 0

    for i, item in enumerate(raw_items):
        name = item['title_zh'].strip()
        if is_junk_name(name):
            continue

        style_hint = item['style']
        print(f"  [{i+1}/{len(raw_items)}] {name[:28]:<28} [{style_hint}]", end='  ')
        try:
            img_resp = req_lib.get(item['image_url'], timeout=10)
            if not img_resp.ok:
                raise Exception(f"圖片 {img_resp.status_code}")
            mime = "image/webp" if item['image_url'].endswith('.webp') else "image/jpeg"

            # Retry with backoff on rate limit
            for attempt in range(4):
                try:
                    resp = client.models.generate_content(
                        model="gemini-2.0-flash-lite",
                        contents=[
                            types.Part.from_bytes(data=img_resp.content, mime_type=mime),
                            CLASSIFY_PROMPT,
                        ],
                        config=types.GenerateContentConfig(response_mime_type="application/json"),
                    )
                    break
                except Exception as ex:
                    if '429' in str(ex) and attempt < 3:
                        wait = 15 * (attempt + 1)
                        print(f'\n    rate limit, wait {wait}s...', end='')
                        time.sleep(wait)
                    else:
                        raise
            c = json.loads(resp.text)
            style = c.get('style_tags', [style_hint])[0]
            if style not in VALID_STYLES:
                style = style_hint

            enriched.append({
                "id":            f"momo-{style[:3]}-{i+1:04d}",
                "name_zh":       c.get('name_zh', name)[:20],
                "brand":         "momo",
                "category":      c.get('category', detect_category(name)),
                "style_tags":    [style],
                "keywords":      [item['keyword']],
                "colors":        c.get('colors', []),
                "price_twd":     item['price_twd'],
                "image_url":     item['image_url'],
                "purchase_url":  item['purchase_url'],
                "dimensions":    c.get('dimensions', ''),
                "flux_descriptor": c.get('flux_descriptor', ''),
            })
            ok_count += 1
            print('✓')
            time.sleep(2.5)  # 每秒最多約0.4次，遠低於30 RPM限制

        except Exception as e:
            # fallback：不丟棄，用原始資料
            cat = detect_category(name)
            enriched.append({
                "id":            f"momo-{style_hint[:3]}-{i+1:04d}",
                "name_zh":       name[:20],
                "brand":         "momo",
                "category":      cat,
                "style_tags":    [style_hint],
                "keywords":      [item['keyword']],
                "colors":        [],
                "price_twd":     item['price_twd'],
                "image_url":     item['image_url'],
                "purchase_url":  item['purchase_url'],
                "dimensions":    "",
                "flux_descriptor": "",
            })
            fail_count += 1
            print(f'✗ ({str(e)[:30]})')

    print(f"\nGemini OK: {ok_count}  Fallback: {fail_count}")
    return enriched


async def main():
    with open(RAW_PATH, encoding='utf-8') as f:
        raw = json.load(f)
    print(f"載入原始資料: {len(raw)} 件")

    # 每種風格最多 40 件，確保平均分布
    by_style = defaultdict(list)
    for it in raw:
        by_style[it['style']].append(it)

    to_classify = []
    for style in VALID_STYLES:
        items = by_style[style][:40]
        print(f"  {style:<15}: {len(items)} 件")
        to_classify.extend(items)
    print(f"共 {len(to_classify)} 件送 Gemini 分類\n")

    classified = await classify_all(to_classify)

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)

    styles = Counter(it['style_tags'][0] for it in classified if it.get('style_tags'))
    cats   = Counter(it['category'] for it in classified)
    has_flux = sum(1 for it in classified if it.get('flux_descriptor') and len(it.get('flux_descriptor','')) > 10)

    print(f"\n{'='*60}")
    print(f"完成！共 {len(classified)} 件  Gemini 分類: {has_flux} 件")
    print(f"存至 {OUT_PATH}")
    print("\n風格分布:")
    for s in VALID_STYLES:
        print(f"  {s:<15}: {styles.get(s,0)}")
    print("\n類別分布(前8):")
    for cat, cnt in cats.most_common(8):
        print(f"  {cat}: {cnt}")

asyncio.run(main())

"""
合併 momo + IKEA 真實商品為 furniture_catalog_real.json
"""
import json, re, sys, io
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

MOMO_RAW   = Path(__file__).parent / "furniture_raw_momo.json"
IKEA_RAW   = Path(__file__).parent / "furniture_raw_ikea.json"
OUT_PATH   = Path(__file__).parent / "furniture_catalog_real.json"

VALID_STYLES = ["modern","japanese","luxury","nordic","industrial","mediterranean","muji","art-deco","boho"]

JUNK_PATTERNS = [
    r'^滿\d', r'^買\d+送\d+', r'^\d+折', r'^\d+%',
    r'^限時', r'^特價', r'^最低', r'^現省',
    r'^\[', r'^【限', r'^MIT\s', r'^\d+CM',
    r'^部分地區', r'^台灣製造可', r'^台灣與歐美', r'^下單\d+折',
    r'^一般地區', r'^適用於\d+', r'^小戶型', r'^單人即可',
    r'^高穩定', r'^高彈力', r'^高回彈', r'^磨毛',
    r'^六角', r'^疊摞', r'^拿取方便',
]

def is_junk(name: str) -> bool:
    if len(name) < 5:
        return True
    for pat in JUNK_PATTERNS:
        if re.match(pat, name):
            return True
    if re.search(r'折\d+|折300|折600|折100', name):
        return True
    return False

CATEGORY_RULES = [
    (['L型沙發','三人座沙發','雙人座沙發','單人座沙發','三人沙發','雙人沙發','單人沙發',
      '沙發床','貓抓布沙發','布沙發','皮沙發','沙發椅','懶人沙發','沙發'], '沙發'),
    (['餐桌','書桌','工作桌','電腦桌','咖啡桌','飯桌','摺疊桌','書桌','辦公桌'], '桌子'),
    (['餐椅','辦公椅','電腦椅','休閒椅','搖椅','藤椅','單椅','吧椅','吧台椅','椅'], '椅子'),
    (['床架','床組','床頭板','掀床','收納床','雙人床','單人床架'], '床架'),
    (['茶几','邊几','角几','矮桌','邊桌'], '茶几'),
    (['電視櫃','書架','書櫃','收納櫃','置物架','衣櫃','斗櫃','抽屜','書案','層架'], '收納'),
    (['吊燈','台燈','落地燈','壁燈','燈具','燈飾'], '燈具'),
    (['地毯','毯子','毯'], '地毯'),
    (['窗簾','遮光簾','捲簾','百葉','窗簾'], '窗簾'),
    (['掛鏡','裝飾鏡','掛畫','壁畫','花器','擺件','時鐘'], '裝飾'),
    (['抱枕','靠枕','枕頭'], '抱枕'),
    (['被','寢具','床單','床包','枕套'], '寢具'),
]

def detect_category(name: str) -> str:
    for keywords, cat in CATEGORY_RULES:
        if any(kw in name for kw in keywords):
            return cat
    return '傢俱'

def process_momo(raw_items: list, start_idx: int = 0) -> list:
    from collections import defaultdict
    by_style = defaultdict(list)
    for it in raw_items:
        by_style[it['style']].append(it)

    items_to_use = []
    for style in VALID_STYLES:
        items_to_use.extend(by_style[style][:40])

    seen_url = set()
    catalog = []
    for i, it in enumerate(items_to_use):
        name = it['title_zh'].strip()
        if is_junk(name):
            continue
        url = it.get('purchase_url', '')
        if not url or url in seen_url:
            continue
        seen_url.add(url)
        style = it['style']
        cat = detect_category(name)
        if cat == '傢俱' and it.get('category') and it['category'] != '傢俱':
            cat = it['category']
        catalog.append({
            "id":           f"momo-{style[:3]}-{start_idx+i+1:04d}",
            "name_zh":      name[:25],
            "brand":        "momo",
            "category":     cat,
            "style_tags":   [style],
            "keywords":     [it.get('keyword','')],
            "colors":       [],
            "price_twd":    it['price_twd'],
            "image_url":    it['image_url'],
            "purchase_url": url,
            "dimensions":   "",
            "flux_descriptor": "",
            "source":       "momo",
        })
    return catalog

def process_ikea(raw_items: list, start_idx: int = 0) -> list:
    seen_url = set()
    catalog = []
    for i, it in enumerate(raw_items):
        name = it.get('name_zh', '').strip()
        if len(name) < 3:
            continue
        url = it.get('purchase_url', '')
        if not url or url in seen_url:
            continue
        seen_url.add(url)

        style_hint = it.get('style_hint', 'nordic')
        cat = it.get('category', '傢俱')
        # 用名稱再試一次
        if cat == '傢俱':
            cat = detect_category(name)

        catalog.append({
            "id":           f"ikea-{style_hint[:3]}-{start_idx+i+1:04d}",
            "name_zh":      name[:30],
            "brand":        "IKEA",
            "category":     cat,
            "style_tags":   [style_hint],
            "keywords":     ["IKEA", it.get('series', '')],
            "colors":       [],
            "price_twd":    it.get('price_twd', 0),
            "image_url":    it.get('image_url', ''),
            "purchase_url": url,
            "dimensions":   "",
            "flux_descriptor": "",
            "source":       "ikea",
        })
    return catalog

def main():
    momo_catalog = []
    if MOMO_RAW.exists():
        raw = json.loads(MOMO_RAW.read_text(encoding='utf-8'))
        momo_catalog = process_momo(raw)
        print(f"momo: {len(momo_catalog)} 件")
    else:
        print("momo raw 不存在，跳過")

    ikea_catalog = []
    if IKEA_RAW.exists():
        raw = json.loads(IKEA_RAW.read_text(encoding='utf-8'))
        ikea_catalog = process_ikea(raw, start_idx=len(momo_catalog))
        print(f"IKEA: {len(ikea_catalog)} 件")
    else:
        print("IKEA raw 不存在，跳過")

    combined = momo_catalog + ikea_catalog

    # 全域 URL 去重
    seen = set()
    final = []
    for it in combined:
        url = it.get('purchase_url', '')
        if url and url not in seen:
            seen.add(url)
            final.append(it)

    print(f"\n合併後: {len(final)} 件")
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    print(f"存至 {OUT_PATH}")

    styles = Counter(it['style_tags'][0] for it in final if it.get('style_tags'))
    cats = Counter(it['category'] for it in final)
    sources = Counter(it.get('source','?') for it in final)
    has_img = sum(1 for it in final if it.get('image_url'))

    print(f"\n來源: {dict(sources)}")
    print(f"有圖片: {has_img}")
    print("\n類別分布:")
    for cat, cnt in cats.most_common(10):
        print(f"  {cat}: {cnt}")
    print("\n風格分布:")
    for s in VALID_STYLES:
        print(f"  {s:<15}: {styles.get(s,0)}")

if __name__ == "__main__":
    main()

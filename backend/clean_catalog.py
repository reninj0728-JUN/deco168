"""
不呼叫 Gemini，直接從 furniture_raw_momo.json 重建乾淨的 furniture_catalog_real.json
用途：Gemini 配額耗盡時先清理垃圾名稱
"""
import json, re, sys, io
from pathlib import Path
from collections import defaultdict, Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RAW_PATH = Path(__file__).parent / "furniture_raw_momo.json"
OUT_PATH = Path(__file__).parent / "furniture_catalog_real.json"

VALID_STYLES = ["modern","japanese","luxury","nordic","industrial","mediterranean","muji","art-deco","boho"]

JUNK_PATTERNS = [
    r'^滿\d',
    r'^買\d+送\d+',
    r'^\d+折',
    r'^\d+%',
    r'^限時',
    r'^特價',
    r'^最低',
    r'^現省',
    r'^\[',
    r'^【限',
    r'^MIT\s',
    r'^\d+CM',
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
    if re.search(r'折\d+|折300|折600|折100', name):
        return True
    return False

CATEGORY_RULES = [
    (['L型沙發','三人沙發','雙人沙發','單人沙發','沙發床','貓抓布沙發','布沙發','皮沙發','沙發椅','懶人沙發','沙發'], '沙發'),
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

def main():
    with open(RAW_PATH, encoding='utf-8') as f:
        raw = json.load(f)
    print(f"原始資料: {len(raw)} 件")

    # 每種風格最多 40 件
    by_style = defaultdict(list)
    for it in raw:
        by_style[it['style']].append(it)

    items = []
    for style in VALID_STYLES:
        items.extend(by_style[style][:40])
    print(f"每風格40件上限後: {len(items)} 件")

    # 全域去重（同 purchase_url）
    seen_url = set()
    unique = []
    for it in items:
        url = it.get('purchase_url', '')
        if url and url not in seen_url:
            seen_url.add(url)
            unique.append(it)
    print(f"去重後: {len(unique)} 件")

    catalog = []
    junk_count = 0
    for i, it in enumerate(unique):
        name = it['title_zh'].strip()
        if is_junk_name(name):
            junk_count += 1
            continue
        style = it['style']
        # 先用名稱偵測類別，偵測不到才用 raw 的 category 欄位
        cat = detect_category(name)
        if cat == '傢俱' and it.get('category') and it['category'] != '傢俱':
            cat = it['category']

        catalog.append({
            "id":           f"momo-{style[:3]}-{i+1:04d}",
            "name_zh":      name[:20],
            "brand":        "momo",
            "category":     cat,
            "style_tags":   [style],
            "keywords":     [it['keyword']],
            "colors":       [],
            "price_twd":    it['price_twd'],
            "image_url":    it['image_url'],
            "purchase_url": it['purchase_url'],
            "dimensions":   "",
            "flux_descriptor": "",
        })

    print(f"過濾垃圾後: {len(catalog)} 件 (移除 {junk_count} 件垃圾)")

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    styles = Counter(it['style_tags'][0] for it in catalog)
    cats = Counter(it['category'] for it in catalog)
    print(f"\n存至 {OUT_PATH}")
    print("\n風格分布:")
    for s in VALID_STYLES:
        print(f"  {s:<15}: {styles.get(s,0)}")
    print("\n類別分布(前8):")
    for cat, cnt in cats.most_common(8):
        print(f"  {cat}: {cnt}")

    # 顯示幾個範例
    print("\n前3件範例:")
    for ex in catalog[:3]:
        print(f"  {ex['name_zh'][:25]} | {ex['style_tags'][0]} | {ex['category']} | NT${ex['price_twd']:,}")
        print(f"    圖: {ex['image_url'][:60]}")

if __name__ == "__main__":
    main()

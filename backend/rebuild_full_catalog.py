"""
把 Gemini 已分類的 112 件 momo catalog + raw IKEA 資料
合併成完整的 furniture_catalog_real.json（~1016 件）
同時保留 momo 件的 Gemini 分類結果
"""
import json
from pathlib import Path
from collections import Counter

BASE = Path(__file__).parent
CLASSIFIED_PATH = BASE / "furniture_catalog_real.json"   # 已被分類的 112 件 momo
IKEA_RAW_PATH   = BASE / "furniture_raw_ikea.json"
OUT_PATH        = BASE / "furniture_catalog_real.json"

VALID_STYLES = ["modern","japanese","luxury","nordic","industrial","mediterranean","muji","art-deco","boho"]

CATEGORY_RULES = [
    (['L型沙發','三人座沙發','雙人座沙發','單人座沙發','三人沙發','雙人沙發','單人沙發',
      '沙發床','貓抓布沙發','布沙發','皮沙發','沙發椅','懶人沙發','沙發'], '沙發'),
    (['餐桌','書桌','工作桌','電腦桌','咖啡桌','飯桌','摺疊桌','辦公桌'], '桌子'),
    (['餐椅','辦公椅','電腦椅','休閒椅','搖椅','藤椅','單椅','吧椅','吧台椅','椅'], '椅子'),
    (['床架','床組','床頭板','掀床','收納床','雙人床','單人床架'], '床架'),
    (['茶几','邊几','角几','矮桌','邊桌'], '茶几'),
    (['電視櫃','書架','書櫃','收納櫃','置物架','衣櫃','斗櫃','抽屜','層架'], '收納'),
    (['吊燈','台燈','落地燈','壁燈','燈具','燈飾'], '燈具'),
    (['地毯','毯子'], '地毯'),
    (['窗簾','遮光簾','捲簾','百葉'], '窗簾'),
    (['掛鏡','裝飾鏡','掛畫','壁畫','花器','擺件','時鐘'], '裝飾'),
    (['抱枕','靠枕'], '抱枕'),
    (['被','寢具','床單','床包','枕套'], '寢具'),
]

def detect_category(name: str) -> str:
    for keywords, cat in CATEGORY_RULES:
        if any(kw in name for kw in keywords):
            return cat
    return '傢俱'

def process_ikea(raw_items: list, start_idx: int) -> list:
    seen_url = set()
    result = []
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
        if cat == '傢俱':
            cat = detect_category(name)
        result.append({
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
    return result

def main():
    # 載入已 Gemini 分類的 momo 件
    momo_items = json.loads(CLASSIFIED_PATH.read_text(encoding='utf-8'))
    # 補上 source 欄位（舊版沒有）
    for it in momo_items:
        it.setdefault('source', 'momo')
    print(f"已分類 momo 件數: {len(momo_items)}")

    # 載入 IKEA raw
    ikea_raw = json.loads(IKEA_RAW_PATH.read_text(encoding='utf-8'))
    ikea_items = process_ikea(ikea_raw, start_idx=len(momo_items))
    print(f"IKEA 件數: {len(ikea_items)}")

    # 合併（momo URL 已在 momo_items 裡，IKEA URL 去重）
    momo_urls = {it.get('purchase_url','') for it in momo_items}
    ikea_deduped = [it for it in ikea_items if it['purchase_url'] not in momo_urls]

    final = momo_items + ikea_deduped
    print(f"合併後總件數: {len(final)}")

    OUT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"存至 {OUT_PATH}")

    styles  = Counter(it['style_tags'][0] for it in final if it.get('style_tags'))
    cats    = Counter(it['category'] for it in final)
    sources = Counter(it.get('source','?') for it in final)
    has_flux = sum(1 for it in final if it.get('flux_descriptor') and len(it['flux_descriptor']) > 10)
    print(f"\n來源: {dict(sources)}")
    print(f"有 flux_descriptor: {has_flux}")
    print("\n類別分布:")
    for cat, cnt in cats.most_common(10):
        print(f"  {cat}: {cnt}")
    print("\n風格分布 (momo 已分類):")
    for s in VALID_STYLES:
        print(f"  {s:<15}: {styles.get(s,0)}")

if __name__ == "__main__":
    main()

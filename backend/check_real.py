import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
with open('furniture_catalog_real.json', encoding='utf-8') as f:
    items = json.load(f)
print(f'總件數: {len(items)}')
from collections import Counter
styles = Counter(it['style_tags'][0] for it in items if it.get('style_tags'))
cats = Counter(it['category'] for it in items)
has_img = sum(1 for it in items if it.get('image_url'))
has_flux = sum(1 for it in items if it.get('flux_descriptor') and len(it.get('flux_descriptor','')) > 10)
print(f'有圖片: {has_img}')
print(f'有 flux_descriptor (Gemini生成): {has_flux}')
print('\n風格分布:')
for s in ['modern','nordic','japanese','muji','luxury','art-deco','boho','industrial','mediterranean']:
    print(f'  {s:<15}: {styles.get(s,0)}')
print('\n類別分布(前8):')
for cat, cnt in cats.most_common(8):
    print(f'  {cat}: {cnt}')
ex = items[0]
print('\n範例商品:')
print(f'  名稱: {ex["name_zh"]}')
print(f'  品牌: {ex["brand"]}')
print(f'  風格: {ex["style_tags"]}')
print(f'  圖片: {ex["image_url"][:70]}')
print(f'  連結: {ex["purchase_url"][:70]}')
print(f'  台幣: NT${ex["price_twd"]:,}')
print(f'  flux: {ex["flux_descriptor"][:60]}')

import json
from pathlib import Path

catalog = json.loads(Path('furniture_catalog_real.json').read_text(encoding='utf-8'))
jap = [it for it in catalog if it.get('style_tags', [''])[0] == 'japanese']
print(f"共 {len(jap)} 件 japanese\n")
for it in jap:
    name = it['name_zh'][:32]
    cat  = it.get('category', '?')
    flux = it.get('flux_descriptor', '')[:70]
    src  = it.get('source', '?')
    print(f"{name:<34} [{cat}] [{src}]")
    print(f"  {flux}")

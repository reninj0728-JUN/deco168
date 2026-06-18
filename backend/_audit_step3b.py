"""
Step 3B 軟裝分類規則修正後 — 重新盤點 + match_soft_furnishing 測試.
不打 fal / Gemini / 不跑 render.

輸出寫到 backend/_audit_step3b.txt.
"""

import json
import sys
from pathlib import Path
from collections import Counter

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from furniture_match import (  # noqa: E402
    resolve_category,
    match_soft_furnishing,
    SOFT_FURNISHING_CATS,
)

REAL = json.loads((BASE / "furniture_catalog_real.json").read_text(encoding="utf-8"))
DECOR_CAT_ITEMS = [it for it in REAL if it.get("category") == "裝飾"]

lines: list[str] = []
def w(s=""):
    lines.append(str(s))


# ────────────────────────────────────────────────────────────────────
# 驗收 1: 「裝飾」278 件分類分布
# ────────────────────────────────────────────────────────────────────
w("=" * 70)
w("驗收 1: 「裝飾」cat 278 件重新分類 (Step 3B 規則)")
w("=" * 70)
w(f"裝飾 cat 總件數: {len(DECOR_CAT_ITEMS)}")
w("")

buckets: dict[str, list[dict]] = {}
for it in DECOR_CAT_ITEMS:
    sub = resolve_category(it)
    buckets.setdefault(sub, []).append(it)

w(f"{'子類':<18} {'件數':>5}")
for k in sorted(buckets, key=lambda x: -len(buckets[x])):
    w(f"  {k:<18} {len(buckets[k]):>5}")
w("")

mirror_count = len(buckets.get("mirror", []))
wall_art_count = len(buckets.get("wall_art", []))
vase_count = len(buckets.get("vase", []))
textile_count = len(buckets.get("textile", []))
unknown_count = len(buckets.get("decor_unknown", []))

w(f"預期: mirror 接近 39 (真鏡子), wall_art > 16, vase 包含花盆, textile > 0")
w(f"      實際 mirror={mirror_count}, wall_art={wall_art_count}, "
  f"vase={vase_count}, textile={textile_count}, decor_unknown={unknown_count}")

assert mirror_count <= 50, f"mirror 仍過多 ({mirror_count}), 規則沒修對"
assert wall_art_count > 16, f"wall_art 沒擴增 ({wall_art_count})"
assert vase_count >= 60, f"vase 沒納入花盆/盆器 ({vase_count})"
assert textile_count >= 5, f"textile 子類沒接到 ({textile_count})"
w("  → 驗收 1 PASS")


# ────────────────────────────────────────────────────────────────────
# 驗收 2: 各 cat 詳細表 (數量 / image_url / purchase_url / 前 10 件)
# ────────────────────────────────────────────────────────────────────
w("\n" + "=" * 70)
w("驗收 2: 各軟裝 cat 詳細表 (全 catalog, 不限「裝飾」)")
w("=" * 70)

REPORT_CATS = ['wall_art', 'vase', 'plant', 'decor', 'textile',
               'mirror', 'lighting', 'decor_unknown', 'pillow', 'curtain']

for cat in REPORT_CATS:
    items = [it for it in REAL if resolve_category(it) == cat]
    n_img = sum(1 for it in items if (it.get("image_url") or "").startswith("http"))
    n_buy = sum(1 for it in items if (it.get("purchase_url") or "").startswith("http"))
    w("")
    w(f"[{cat}]  total={len(items)}  image_url={n_img}  purchase_url={n_buy}")
    for it in items[:10]:
        w(f"  ({it.get('category', '')}) {it.get('name_zh', '')}")


# ────────────────────────────────────────────────────────────────────
# 驗收 3: match_soft_furnishing 三 case
# ────────────────────────────────────────────────────────────────────
w("\n" + "=" * 70)
w("驗收 3: match_soft_furnishing — 三個 style × tier 場景")
w("=" * 70)

def check(style: str, tier: str):
    w(f"\n[Case] style={style!r}  tier={tier!r}")
    soft = match_soft_furnishing(style, REAL, budget_tier=tier)
    w(f"  總撈 {len(soft)} 件")
    cats = [resolve_category(it) for it in soft]
    cat_counter = Counter(cats)
    w(f"  cat 分布: {dict(cat_counter)}")
    multi_cat = len(cat_counter) == len(soft)
    w(f"  類別多樣性 (每件不同 cat): {multi_cat}")
    all_img = all((it.get("image_url") or "").startswith("http") for it in soft)
    all_buy = all((it.get("purchase_url") or "").startswith("http") for it in soft)
    w(f"  全部帶 image_url: {all_img}")
    w(f"  全部帶 purchase_url: {all_buy}")
    w("  品項:")
    for it in soft:
        w(f"    [{resolve_category(it)}] {it.get('name_zh')}  "
          f"NT${it.get('price_twd')}  brand={it.get('brand')}")
    assert all_img, f"{style}/{tier}: 有 item 缺 image_url"
    assert all_buy, f"{style}/{tier}: 有 item 缺 purchase_url"
    # mirror 不該出現在 soft (該被 fallback 修掉)
    assert "mirror" not in cats, f"{style}/{tier}: mirror 不該進 soft"
    return soft


s1 = check("modern", "tier2")
s2 = check("nordic", "tier3")
s3 = check("muji", "tier1")

# 確認三個 case 都涵蓋至少 5 個不同 cat
for label, soft in [("modern", s1), ("nordic", s2), ("muji", s3)]:
    cats = {resolve_category(it) for it in soft}
    w(f"\n  {label} 涵蓋 {len(cats)} 個 cat: {sorted(cats)}")
    assert len(cats) >= 5, f"{label}: 應涵蓋至少 5 cat"

w("\n驗收 3 PASS — 三 case 都 PASS")


# ────────────────────────────────────────────────────────────────────
# 寫出
# ────────────────────────────────────────────────────────────────────
out_path = BASE / "_audit_step3b.txt"
out_path.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {out_path}")
print("\nALL ASSERTS PASS")

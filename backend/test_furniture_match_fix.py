"""
驗證 furniture_match.py 修正後：
  modern + nordic 兩個風格都必須撈到 sofa + coffee_table + rug

來源：32E5CC13（estimated_size=8, dims=3.5x2.7x7.5）

不接 production，不改模型，不改 catalog。
純跑 enrich_renders() 看輸出。

執行：
    python test_furniture_match_fix.py
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from furniture_match import (
    enrich_renders,
    resolve_category,
    LIVING_MUST_HAVE,
    LIVING_EXCLUDED,
)


# 32E5CC13 真實 estimated_size 與 room_dimensions
ANALYSIS = {
    "estimated_size": "8",
    "room_dimensions": {
        "width_m": 3.5,
        "height_m": 2.7,
        "length_m": 7.5,
        "confidence": "high",
    },
}

# DB 沒存 flux_prompt（job 32E5CC13 該欄位為 null）
# 構造兩組：A) 空 prompt — 驗最壞情況 must_have 仍生效
#          B) 有 prompt — 驗加分品類有正常吃到 prompt 關鍵字
RENDERS_EMPTY_PROMPT = [
    {"style": "modern", "style_label": "都會簡約", "flux_prompt": ""},
    {"style": "nordic", "style_label": "北歐清簡", "flux_prompt": ""},
]

RENDERS_WITH_PROMPT = [
    {
        "style": "modern",
        "style_label": "都會簡約",
        "flux_prompt": (
            "charcoal grey fabric sofa, walnut coffee table, "
            "low-pile greige rug, arc floor lamp, tan accent chair, "
            "abstract canvas, snake plant"
        ),
    },
    {
        "style": "nordic",
        "style_label": "北歐清簡",
        "flux_prompt": (
            "cream boucle sofa, oak coffee table, jute rug, "
            "rattan accent chair, paper pendant, dried pampas, linen curtain"
        ),
    },
]


def safe(s: str) -> str:
    enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
    return (s or "").encode(enc, errors="replace").decode(enc)


def assert_must_have(label: str, render: dict) -> bool:
    furniture = render.get("matched_furniture", [])
    found_cats = {f.get("category_en") for f in furniture}

    missing = [c for c in LIVING_MUST_HAVE if c not in found_cats]
    bad = [
        f for f in furniture
        if f.get("category_en") in LIVING_EXCLUDED
    ]

    ok = (not missing) and (not bad)
    mark = "PASS" if ok else "FAIL"
    print(safe(f"\n[{mark}] {label}"))

    for f in furniture:
        cat_en = f.get("category_en", "?")
        is_must = cat_en in LIVING_MUST_HAVE
        prefix = "  v" if is_must else "  +"
        if cat_en in LIVING_EXCLUDED:
            prefix = "  X"
        print(safe(
            f"{prefix} [{cat_en:<14}] {f.get('name_zh','')[:32]:<34} "
            f"{f.get('brand','')[:8]:<10} ${f.get('price_twd',0)}"
        ))

    if missing:
        print(safe(f"  >> 缺品類: {missing}"))
    if bad:
        print(safe(f"  >> 出現排除品類: {[b.get('name_zh','') for b in bad]}"))

    return ok


def run(label: str, renders: list[dict]) -> bool:
    print(safe(f"\n{'='*70}"))
    print(safe(f"  {label}"))
    print(safe(f"{'='*70}"))
    enriched = enrich_renders(renders, analysis=ANALYSIS)
    all_ok = True
    for r in enriched:
        ok = assert_must_have(f"{r.get('style')} / {r.get('style_label')}", r)
        all_ok = all_ok and ok
    return all_ok


def main():
    print(safe("=== 32E5CC13 furniture_match 修正驗證 ==="))
    print(safe(f"空間: 8 坪 / 3.5x2.7x7.5m"))
    print(safe(f"客廳必撈品類: {LIVING_MUST_HAVE}"))
    print(safe(f"客廳排除品類: {LIVING_EXCLUDED}"))

    r1 = run("Case A: 空 flux_prompt（最壞情況，僅靠 style + must_have）", RENDERS_EMPTY_PROMPT)
    r2 = run("Case B: 有 flux_prompt（正常情況）", RENDERS_WITH_PROMPT)

    print(safe(f"\n{'='*70}"))
    print(safe(f"  驗收結果"))
    print(safe(f"{'='*70}"))
    print(safe(f"  Case A: {'PASS' if r1 else 'FAIL'}"))
    print(safe(f"  Case B: {'PASS' if r2 else 'FAIL'}"))
    sys.exit(0 if (r1 and r2) else 1)


if __name__ == "__main__":
    main()

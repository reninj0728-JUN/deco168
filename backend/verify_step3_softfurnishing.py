"""
Step 3 驗收 (軟裝接入 + plan-A mapping 已移除), 不打 fal/Gemini/不跑新圖.

Case A:  api.py run_pipeline 已不含 plan-A → rear_near_window 硬 mapping
         (Step 3 dropped 2026-06-19; 從 inspect.getsource 抽源碼確認)

Case B:  build_nano_banana_inputs 拿到含 soft_furnishing 的 entry
         → prompt 內應出現 SOFT FURNISHING SUGGESTIONS + 對應品類描述

Case C:  enrich_renders 跑一張 modern 風 render entry
         → 回傳的 render 該帶 soft_furnishing[] 非空
         → soft_furnishing 內含 pillow / curtain 至少一項
         → matched_furniture 仍維持原行為 (含 sofa/coffee_table/rug)

Case D:  細分驗證: '裝飾' cat + name_zh='水墨牡丹國畫掛畫' → resolve_category 回 'wall_art'
         '裝飾' cat + name_zh='復古琥珀色條紋玻璃花瓶' → 'vase'
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def case_a_hint_auto_inject():
    """
    Step 3 dropped (2026-06-19): plan A → rear_near_window 硬 mapping 已從 api.py 移除.
    本 case 改為驗證「不論 layout_choice 為何, 用戶提交的 hint 不會被後端自動改」.
    用戶想 nudge → 自己填 hint 或 target_note. zoning_result 仍透過
    flatten_zoning_v2_to_v1 → _build_layout_section 表達方案語意, 不再經 PhotoMeta.
    """
    print("\n[Case A] (Step 3 dropped) layout_choice 不再自動 mapping 成 rear_near_window")

    # 直接從 api.py 抓真實程式碼確認沒有「= 'rear_near_window'」這種強制覆蓋句
    import inspect
    from api import run_pipeline
    src = inspect.getsource(run_pipeline)
    assert "_best_pm_location_hint = \"rear_near_window\"" not in src \
        and "_best_pm_location_hint = 'rear_near_window'" not in src, \
        "api.py run_pipeline 仍有 rear_near_window 硬 mapping — 移除沒完成"
    print("  api.py 已無 rear_near_window 硬 mapping  PASS")

    # 確認註解有寫「Step 3 dropped」, 給未來閱讀的人線索
    assert "Step 3 dropped" in src, "新行為說明缺少 Step 3 dropped 註記"
    print("  程式碼註解保留歷史軌跡  PASS")


def case_b_soft_section_in_prompt():
    print("\n[Case B] build_nano_banana_inputs 含 soft_furnishing → prompt 帶 SOFT FURNISHING 段")
    from prompt_builder import build_nano_banana_inputs

    entry = {
        "style": "modern",
        "style_label": "現代簡約",
        "flux_prompt": "modern living",
        "matched_furniture": [],
        "soft_furnishing": [
            {"category_en": "pillow", "name_zh": "亞麻質感灰色方型抱枕"},
            {"category_en": "curtain", "name_zh": "純色簡約遮光窗簾"},
            {"category_en": "wall_art", "name_zh": "水墨牡丹國畫掛畫"},
            {"category_en": "vase", "name_zh": "復古琥珀色條紋玻璃花瓶"},
            {"category_en": "plant", "name_zh": "白色簡約植栽盆"},
        ],
    }
    out = build_nano_banana_inputs(
        entry=entry,
        zoning={"confidence": "none"},
        room_image_url="https://example.test/r.jpg",
    )
    prompt = out["prompt"]
    must_contain = [
        "SOFT FURNISHING SUGGESTIONS",
        "throw pillows",
        "window curtains",
        "wall art",
        "decorative vase",
        "potted plant",
        "NOT primary furniture",
    ]
    for needle in must_contain:
        assert needle in prompt, f"prompt 缺 {needle!r}"
        print(f"  PASS  contains {needle!r}")

    # 反向: soft_furnishing 空 → 段不出現
    print("\n  反向: 空 soft_furnishing → 段不出現")
    entry2 = {"style": "modern", "style_label": "x", "flux_prompt": "x", "matched_furniture": []}
    out2 = build_nano_banana_inputs(entry=entry2, zoning={"confidence": "none"},
                                    room_image_url="https://example.test/r.jpg")
    assert "SOFT FURNISHING SUGGESTIONS" not in out2["prompt"]
    print("  PASS  空 list 不注入 SOFT FURNISHING 段")


def case_c_enrich_soft():
    print("\n[Case C] enrich_renders 對一張 render entry → 回傳含 soft_furnishing[]")
    from furniture_match import enrich_renders

    renders = [{
        "style": "modern", "style_label": "現代簡約",
        "flux_prompt": "modern living room, sofa, coffee table, area rug",
    }]
    analysis = {
        "estimated_size": "15 坪",
        "room_dimensions": {"length_m": 5.0, "width_m": 3.5, "height_m": 2.7},
    }
    enriched = enrich_renders(renders, analysis=analysis, budget_tier="tier2")
    assert len(enriched) == 1
    r = enriched[0]

    matched = r.get("matched_furniture") or []
    soft = r.get("soft_furnishing") or []
    matched_cats = sorted({it.get("category_en") for it in matched})
    soft_cats = sorted({it.get("category_en") for it in soft})
    print(f"  matched_furniture cats={matched_cats} ({len(matched)} 件)")
    print(f"  soft_furnishing  cats={soft_cats} ({len(soft)} 件)")
    print("  soft 樣本:")
    for it in soft:
        print(f"    [{it.get('category_en')}] {it.get('name_zh')} brand={it.get('brand')} "
              f"price=NT${it.get('price_twd')} img={'有' if it.get('image_url') else '無'}")

    assert len(soft) >= 2, "soft_furnishing 至少要有 2 件 (pillow / curtain)"
    soft_cat_set = set(soft_cats)
    assert "pillow" in soft_cat_set or "curtain" in soft_cat_set, \
        "soft_furnishing 至少應撈到 pillow 或 curtain"
    for it in soft:
        assert it.get("image_url", "").startswith("http"), \
            f"soft 物件缺 image_url: {it}"
    # 主家具不該被軟裝亂入
    forbidden = {"pillow", "curtain", "wall_art", "vase", "plant"}
    overlap = set(matched_cats) & forbidden
    assert not overlap, f"主家具不該含軟裝 cat: {overlap}"
    print("  PASS")


def case_d_decor_refine():
    print("\n[Case D] 裝飾雜燴 → name_zh 細分")
    from furniture_match import resolve_category
    samples = [
        ({"category": "裝飾", "name_zh": "水墨牡丹國畫掛畫"}, "wall_art"),
        ({"category": "裝飾", "name_zh": "簡約細邊框長方掛畫框"}, "wall_art"),
        ({"category": "裝飾", "name_zh": "復古琥珀色條紋玻璃花瓶"}, "vase"),
        ({"category": "裝飾", "name_zh": "燻灰錘紋玻璃造型花器"}, "vase"),
        ({"category": "裝飾", "name_zh": "工業風水泥圓柱盆栽"}, "plant"),
        ({"category": "裝飾", "name_zh": "白色簡約植栽盆"}, "plant"),
        ({"category": "裝飾", "name_zh": "陶瓷仙人掌造型裝飾擺件三件組"}, "decor"),
        # 沒命中關鍵字 → 維持 mirror
        ({"category": "裝飾", "name_zh": "圓形黑框掛鏡（大）"}, "mirror"),
    ]
    for it, expect in samples:
        got = resolve_category(it)
        ok = got == expect
        print(f"  {'PASS' if ok else 'FAIL'}  {it['name_zh']!r} → got={got}  expect={expect}")
        assert ok


def main():
    print("=" * 70)
    print("Step 3 驗收 (位置修正 + 軟裝接入)")
    print("=" * 70)
    case_a_hint_auto_inject()
    case_d_decor_refine()
    case_b_soft_section_in_prompt()
    case_c_enrich_soft()
    print("\nALL PASS")


if __name__ == "__main__":
    main()

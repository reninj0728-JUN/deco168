"""
DECO168 AI Pipeline — MVP v2
流程：
  影片 + 用戶選擇風格
    → [1] Gemini 分析（空間描述 + 3個風格的 Flux prompt）
    → [2] 抽最佳畫面
    → [3] 家具配對（從 furniture_catalog.json 選出符合的家具）
    → [4] Flux Kontext Pro 生成渲染圖（×3 種風格）
    → 輸出 result.json（含分析、家具清單、渲染圖路徑）

.env 需要：
  GEMINI_API_KEY
  FAL_KEY
"""
import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from gemini_analyze  import analyze_space
from depth_map       import extract_best_frame
from furniture_match import enrich_renders
from flux_generate   import generate_all_renders


def run(video_path: str, user_styles: list[str] | None = None, output_dir: str = "output") -> dict:
    """
    執行完整 AI pipeline。

    user_styles: 用戶選擇的風格 ID 列表，例如 ["modern", "nordic"]
                 傳 None 或空列表表示讓 Gemini 自動推薦 3 種
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"找不到影片：{video_path}")

    os.makedirs(output_dir, exist_ok=True)
    frames_dir  = os.path.join(output_dir, "frames")
    renders_dir = os.path.join(output_dir, "renders")

    print("=" * 56)
    print("DECO168 AI Pipeline v2")
    print("=" * 56)

    # Step 1 — Gemini 分析
    print("\n[1/4] Gemini Flash-Lite 分析影片...")
    analysis = analyze_space(str(video_path), user_styles=user_styles)

    print(f"      空間：{analysis.get('space_type')} {analysis.get('estimated_size')}")
    print(f"      採光：{analysis.get('lighting')}")
    print(f"      屋主需求：{analysis.get('owner_requests')}")
    mode = analysis.get("_mode", "")
    recommended = analysis.get("recommended_styles", [])
    print(f"      推薦風格：{', '.join(recommended)}  [{mode}]")

    renders = analysis.get("renders", [])
    print(f"      生成風格數：{len(renders)}")

    # Step 2 — 抽最佳畫面
    print("\n[2/4] 提取最佳畫面...")
    frame_path = extract_best_frame(str(video_path), frames_dir)
    print(f"      畫面：{frame_path}")

    # Step 3 — 家具配對
    print("\n[3/4] 從家具目錄配對（225 件）...")
    enriched_renders = enrich_renders(renders)
    for r in enriched_renders:
        furniture_count = len(r.get("matched_furniture", []))
        print(f"      {r.get('style_label', r['style'])}: {furniture_count} 件家具配對")

    # Step 4 — Flux 渲染
    print("\n[4/4] Flux Kontext Pro 生成渲染圖...")
    rendered = generate_all_renders(frame_path, enriched_renders, renders_dir)

    # 組合最終結果
    result = {
        "video":             str(video_path),
        "space_type":        analysis.get("space_type"),
        "estimated_size":    analysis.get("estimated_size"),
        "layout_notes":      analysis.get("layout_notes"),
        "lighting":          analysis.get("lighting"),
        "current_style":     analysis.get("current_style"),
        "owner_requests":    analysis.get("owner_requests"),
        "design_analysis":   analysis.get("design_analysis"),
        "recommended_styles": recommended,
        "recommend_reason":  analysis.get("recommend_reason"),
        "mode":              mode,
        "frame":             frame_path,
        "renders":           rendered,  # [{style, style_label, flux_prompt, matched_furniture, render_path}]
    }

    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 印出家具清單摘要
    print("\n" + "=" * 56)
    print("✓ 完成！")
    print(f"  渲染圖：{renders_dir}/")
    print(f"  結果JSON：{result_path}")
    print("\n家具推薦清單：")
    for render in rendered:
        style_label = render.get("style_label", render.get("style", ""))
        furniture = render.get("matched_furniture", [])
        print(f"\n  【{style_label}】")
        for item in furniture:
            price = f"NT${item['price_twd']:,}" if item.get("price_twd") else ""
            dims  = item.get("dimensions", "")
            print(f"    ・{item['name_zh']:<28} {price:<12} {dims}")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python main.py <影片路徑> [風格1,風格2,...] [輸出資料夾]")
        print("範例：python main.py living_room.mp4 modern,nordic")
        print("範例：python main.py living_room.mp4  (Gemini 自動推薦風格)")
        sys.exit(1)

    video   = sys.argv[1]
    styles  = sys.argv[2].split(",") if len(sys.argv) > 2 and "," in sys.argv[2] else None
    out_dir = sys.argv[3] if len(sys.argv) > 3 else (sys.argv[2] if len(sys.argv) > 2 and "," not in sys.argv[2] else "output")

    run(video, user_styles=styles, output_dir=out_dir)

"""
完整 pipeline 測試腳本
用法：python3.11 test_full_pipeline.py <房間照片路徑> [風格1,風格2]
範例：python3.11 test_full_pipeline.py room.jpg modern,nordic

不需要影片，一張房間照片就能測試完整流程：
  Gemini 看圖分析 → 家具配對 → Flux 生成渲染圖
"""
import os, sys, json, base64, time, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

VALID_STYLES = ["modern","japanese","luxury","nordic","muji","cream","wood","french","chinese-modern"]

_client = None
_SYSTEM_PROMPT = None

def _get_client():
    global _client
    if _client is None:
        key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_AI_KEY')
        if not key:
            raise RuntimeError("GEMINI_API_KEY 未設定，請在 Railway Variables 設定")
        from google import genai
        _client = genai.Client(api_key=key)
    return _client

def _get_system_prompt():
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        base = Path(__file__).parent
        txt = (base / "gemini_analyze.py").read_text(encoding="utf-8")
        _SYSTEM_PROMPT = txt.split('SYSTEM_PROMPT = """')[1].split('"""')[0]
    return _SYSTEM_PROMPT

from google.genai import types
from furniture_match import enrich_renders
import fal_client
import requests

# ─── Step 1: Gemini 分析照片（支援多張）─────────────────────────────────────

def analyze_image(image_path: str, user_styles: list[str] | None = None,
                  extra_photos: list[str] | None = None) -> dict:
    """
    image_path   : 主要照片（最廣角，作為 Flux 輸入基底）
    extra_photos : 補充角度照片清單（只給 Gemini 分析用，不送 Flux）
                   建議：[北牆, 南牆, 東牆, 西牆] 四個方向
    """
    all_paths = [image_path] + (extra_photos or [])
    print(f"\n{'='*56}")
    print(f"[Step 1] Gemini 分析 {len(all_paths)} 張照片")
    for p in all_paths:
        print(f"         · {p}")
    print(f"{'='*56}")

    # 讀全部照片轉 base64
    def load_img(path):
        ext = Path(path).suffix.lower()
        mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode(), mime

    imgs = [load_img(p) for p in all_paths]
    img_b64, mime = imgs[0]  # 主照片（給 Flux 用）

    if user_styles and all(s in VALID_STYLES for s in user_styles):
        style_instruction = f"用戶已選擇風格：{', '.join(user_styles[:3])}，必須包含在 renders 中。"
        if len(user_styles) < 3:
            style_instruction += "不足 3 個請 AI 補齊。"
    else:
        style_instruction = "用戶未指定風格，請從 9 種風格推薦最適合的 3 種。"

    photo_count_note = (
        f"你現在看到 {len(all_paths)} 張照片：第1張是主視角（Flux 生成用基底），"
        f"第2張起是補充角度（幫助你理解完整格局）。"
        if len(all_paths) > 1 else "你現在看到 1 張照片。"
    )

    prompt = f"""
{photo_count_note}
分析這{'些' if len(all_paths) > 1 else '張'}空間照片，理解完整格局。

【空間量測步驟 — 必須先做】
1. 找出畫面中可見的基準物：門框（高200cm/寬90cm）、窗台（距地90cm）、插座（距地30cm）、標準沙發（高85cm）
2. 交叉比對所有照片中的基準物，推算房間長度、寬度、天花板高度（公尺）
3. 找出各張照片拍攝方向，確認哪些牆面/角落已被覆蓋
4. 用長×寬計算坪數（1坪=3.305㎡），給保守估計
5. 特別記錄：天花板結構（明管/梁柱/灑水頭）、門的位置、窗戶位置——這些在渲染時必須保留

{style_instruction}

回傳以下 JSON（嚴格照格式）：
{{
  "space_type": "空間類型",
  "estimated_size": "估計坪數",
  "room_dimensions": {{
    "length_m": 數字,
    "width_m": 數字,
    "height_m": 數字,
    "confidence": "high/medium/low",
    "reference_used": "用了哪些基準物"
  }},
  "layout_notes": "格局描述",
  "lighting": "採光條件",
  "current_style": "目前裝潢風格",
  "owner_requests": "未提及",
  "design_analysis": "空間分析摘要，繁體中文，80字以內",
  "recommended_styles": ["style1","style2","style3"],
  "recommend_reason": "推薦原因，50字以內",
  "renders": [
    {{"style":"style_id","style_label":"中文名稱","flux_prompt":"逗號分隔keyword，結尾必須是 professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts"}},
    {{"style":"style_id","style_label":"中文名稱","flux_prompt":"..."}},
    {{"style":"style_id","style_label":"中文名稱","flux_prompt":"..."}}
  ]
}}
"""

    # 組合所有照片 + prompt 送給 Gemini
    contents = [
        types.Part.from_bytes(data=base64.b64decode(b64), mime_type=m)
        for b64, m in imgs
    ] + [prompt]

    t0 = time.time()
    resp = _get_client().models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_get_system_prompt(),
            response_mime_type="application/json",
        ),
    )
    elapsed = time.time() - t0

    result = json.loads(resp.text)
    dims = result.get('room_dimensions', {})
    print(f"  耗時：{elapsed:.1f}s")
    print(f"  空間：{result.get('space_type')} {result.get('estimated_size')}")
    if dims:
        print(f"  實測：{dims.get('length_m')}m × {dims.get('width_m')}m × H{dims.get('height_m')}m  [{dims.get('confidence')}]")
        print(f"  基準：{dims.get('reference_used')}")
    print(f"  採光：{result.get('lighting')}")
    print(f"  分析：{result.get('design_analysis')}")
    print(f"  推薦風格：{result.get('recommended_styles')}")
    print(f"  推薦原因：{result.get('recommend_reason')}")
    return result


# ─── Step 2: 家具配對 ─────────────────────────────────────────────────────────

def show_furniture(enriched_renders: list[dict]):
    print(f"\n{'='*56}")
    print("[Step 2] 家具配對結果")
    print(f"{'='*56}")
    for render in enriched_renders:
        label = render.get("style_label", render.get("style", ""))
        furniture = render.get("matched_furniture", [])
        print(f"\n  【{label}】 配對 {len(furniture)} 件")
        print(f"  {'品名':<30} {'品牌':<8} {'類別':<8} {'價格':>10}  {'尺寸'}")
        print(f"  {'-'*75}")
        for item in furniture:
            name = item['name_zh'][:28]
            price = f"NT${item['price_twd']:,}" if item.get('price_twd') else '-'
            dims = item.get('dimensions','')[:18]
            print(f"  {name:<30} {item['brand']:<8} {item['category']:<8} {price:>10}  {dims}")


# ─── Step 3: Flux 生成渲染圖 ─────────────────────────────────────────────────

def generate_renders(image_path: str, enriched_renders: list[dict], output_dir: str = "output"):
    print(f"\n{'='*56}")
    print("[Step 3] Flux Kontext Pro 生成渲染圖")
    print(f"{'='*56}")

    os.makedirs(output_dir, exist_ok=True)

    # 圖片轉 data URL
    ext = Path(image_path).suffix.lower()
    mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    image_url = f"data:{mime};base64,{b64}"

    results = []
    for render in enriched_renders:
        style = render.get("style", "unknown")
        label = render.get("style_label", style)
        flux_prompt = render.get("flux_prompt", "")

        # 把家具 flux_descriptor 加進 prompt
        furniture_desc = ", ".join(
            item.get("flux_descriptor", "")
            for item in render.get("matched_furniture", [])[:3]
            if item.get("flux_descriptor")
        )
        if furniture_desc:
            full_prompt = f"{flux_prompt}, {furniture_desc}"
        else:
            full_prompt = flux_prompt

        print(f"\n  生成【{label}】...")
        print(f"  Prompt: {full_prompt[:80]}...")

        t0 = time.time()
        try:
            result = fal_client.subscribe(
                "fal-ai/flux-pro/kontext",
                arguments={
                    "image_url": image_url,
                    "prompt": (
                        "PRESERVE EXACTLY: ceiling height, room shape, all walls, window positions, door openings, "
                        "corridor layout, ceiling fixtures (sprinklers, lights, beams), floor plan structure. "
                        "DO NOT move or remove any architectural element. "
                        "ONLY change: surface finishes, furniture, materials, color palette, lighting mood. "
                        f"Apply this interior style: {full_prompt}"
                    ),
                    "guidance_scale": 6.5,
                    "num_inference_steps": 35,
                    "output_format": "jpeg",
                },
                with_logs=False,
            )
            elapsed = time.time() - t0

            img_url = (result.get("images") or [{}])[0].get("url")
            if not img_url:
                raise ValueError(f"fal.ai 未回傳圖片 URL，result keys: {list(result.keys())}")
            resp = requests.get(img_url, timeout=60)
            resp.raise_for_status()
            out_path = os.path.join(output_dir, f"render_{style}.jpg")
            with open(out_path, "wb") as f:
                f.write(resp.content)

            print(f"  ✓ 完成 ({elapsed:.1f}s) → {out_path}")
            results.append({**render, "render_path": out_path})

        except Exception as e:
            print(f"  ✗ 失敗: {e}")
            results.append({**render, "render_path": None, "error": str(e)})

    return results


# ─── 主程式 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
用法（單張）：python3.11 test_full_pipeline.py room.jpg [風格1,風格2]
用法（多張）：python3.11 test_full_pipeline.py room.jpg room2.jpg room3.jpg [風格1,風格2]
  第1張：主視角（Flux 渲染基底，選最廣角那張）
  第2-4張：補充角度（只給 Gemini 看，不渲染）
  最後一個參數若包含逗號則視為風格指定，否則視為額外照片
""")
    if len(sys.argv) < 2:
        sys.exit(1)

    # 解析參數：把含逗號的參數視為風格，其餘視為照片路徑
    image_paths = []
    user_styles = None
    for arg in sys.argv[1:]:
        if "," in arg or arg in VALID_STYLES:
            user_styles = arg.split(",")
        else:
            image_paths.append(arg)

    if not image_paths:
        print("請至少提供一張照片")
        sys.exit(1)

    image_path = image_paths[0]
    extra_photos = [p for p in image_paths[1:] if Path(p).exists()]

    if not Path(image_path).exists():
        print(f"找不到照片：{image_path}")
        sys.exit(1)

    if extra_photos:
        print(f"✓ 主照片：{image_path}")
        print(f"✓ 補充角度：{extra_photos}")
    else:
        print(f"✓ 單張模式：{image_path}")

    total_start = time.time()

    # Step 1
    analysis = analyze_image(image_path, user_styles, extra_photos=extra_photos or None)

    # Step 2
    enriched = enrich_renders(analysis.get("renders", []), analysis=analysis)
    show_furniture(enriched)

    # Step 3
    final = generate_renders(image_path, enriched)

    # 儲存完整結果
    os.makedirs("output", exist_ok=True)
    result_path = "output/test_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({
            "analysis": analysis,
            "renders": final,
        }, f, ensure_ascii=False, indent=2)

    total = time.time() - total_start
    print(f"\n{'='*56}")
    print(f"完成！總耗時 {total:.1f}s")
    print(f"渲染圖 → output/render_*.jpg")
    print(f"完整結果 → {result_path}")
    print(f"{'='*56}")

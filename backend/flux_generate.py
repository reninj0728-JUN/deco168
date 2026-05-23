"""
Step 3 — fal.ai Flux Kontext Pro 生成渲染圖
端點：fal-ai/flux-pro/kontext

Kontext 直接輸入房間照片 + prompt，輸出保留空間結構的風格渲染圖。
不需要另外計算深度圖，比 ControlNet 更簡單，品質一樣好。

費用：$0.04/張，3 張 = $0.12/單（NT$4）

需要環境變數：FAL_KEY
"""
import os
import json
import base64
import requests
import fal_client
from pathlib import Path


STYLE_SUFFIX = (
    "photorealistic interior design rendering, architectural visualization, "
    "natural soft lighting, magazine quality, sharp details, "
    "no people, no text, professional photography"
)


def image_to_data_url(image_path: str) -> str:
    ext  = Path(image_path).suffix.lower()
    mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def generate_render(
    room_image_path: str,
    prompt: str,
    style_name: str,
    output_dir: str = "renders",
) -> str:
    """
    用 Flux Kontext Pro 生成一張渲染圖。
    輸入：屋主房間照片 + 設計風格 prompt
    輸出：保留空間結構的風格渲染圖
    """
    os.makedirs(output_dir, exist_ok=True)

    room_url    = image_to_data_url(room_image_path)
    full_prompt = (
        f"Strictly follow the photo's dimensions, proportions, and lines "
        f"to apply material finishes to this interior design image. "
        f"{prompt}, {STYLE_SUFFIX}"
    )

    print(f"[Flux Kontext] 生成 {style_name}...")
    result = fal_client.subscribe(
        "fal-ai/flux-pro/kontext",
        arguments={
            "image_url": room_url,
            "prompt": full_prompt,
            "guidance_scale": 3.5,
            "num_inference_steps": 28,
            "output_format": "jpeg",
        },
        with_logs=False,
    )

    image_url = result["images"][0]["url"]
    resp      = requests.get(image_url, timeout=60)

    out_path = os.path.join(output_dir, f"render_{style_name}.jpg")
    with open(out_path, "wb") as f:
        f.write(resp.content)

    print(f"[Flux Kontext] 儲存：{out_path}")
    return out_path


def generate_all_renders(
    room_image_path: str,
    renders: list[dict],
    output_dir: str = "renders",
) -> list[dict]:
    """
    依 Gemini 分析結果（renders[] 格式）生成渲染圖。
    renders 格式：[{"style": "modern", "style_label": "現代簡約", "flux_prompt": "..."}]
    回傳：每個 render dict 加上 "render_path" 欄位
    """
    results = []
    for render in renders:
        style = render.get("style", "unknown")
        prompt = render.get("flux_prompt", "")
        path = generate_render(room_image_path, prompt, style, output_dir)
        results.append({**render, "render_path": path})
    return results


if __name__ == "__main__":
    import sys
    room  = sys.argv[1]
    data  = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    paths = generate_all_renders(room, data)
    for p in paths:
        print(p)

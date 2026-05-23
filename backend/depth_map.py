"""
Step 2 — 從影片抽最佳畫面 + 生成深度圖
深度圖用 fal.ai Depth Anything V2
（MVP 階段先測試 OpenRouter Flux，此步可暫時跳過）
"""
import os
import cv2
import base64
import requests
import numpy as np


def extract_best_frame(video_path: str, output_dir: str = "frames") -> str:
    """
    從影片抽出最佳畫面（最亮 + 最清晰的那張）。
    回傳儲存路徑。
    """
    os.makedirs(output_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片：{video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    sample_ratios = [0.15, 0.30, 0.50, 0.65, 0.80]

    best_frame = None
    best_score = -1

    for ratio in sample_ratios:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * ratio))
        ret, frame = cap.read()
        if not ret:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = np.mean(gray)
        sharpness  = cv2.Laplacian(gray, cv2.CV_64F).var()
        score = brightness * 0.3 + sharpness * 0.7

        if score > best_score:
            best_score = score
            best_frame = frame

    cap.release()

    if best_frame is None:
        raise RuntimeError("無法從影片提取畫面，請確認影片檔案完整")

    out_path = os.path.join(output_dir, "best_frame.jpg")
    cv2.imwrite(out_path, best_frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"[Frame] 最佳畫面儲存：{out_path}（score={best_score:.1f}）")
    return out_path


def generate_depth_map(image_path: str, output_dir: str = "frames") -> str:
    """
    用 fal.ai Depth Anything V2 生成深度圖。
    需要設定 FAL_KEY 環境變數。
    """
    try:
        import fal_client
    except ImportError:
        raise ImportError("請安裝 fal-client：pip install fal-client")

    os.makedirs(output_dir, exist_ok=True)

    with open(image_path, "rb") as f:
        data_url = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()

    print("[Depth] 生成深度圖...")
    result = fal_client.subscribe(
        "fal-ai/depth-anything-v2",
        arguments={"image_url": data_url},
        with_logs=False,
    )

    depth_url = result["image"]["url"]
    resp = requests.get(depth_url, timeout=60)

    out_path = os.path.join(output_dir, "depth_map.png")
    with open(out_path, "wb") as f:
        f.write(resp.content)

    print(f"[Depth] 深度圖儲存：{out_path}")
    return out_path


if __name__ == "__main__":
    import sys
    video = sys.argv[1]
    frame = extract_best_frame(video)
    depth = generate_depth_map(frame)
    print(f"Frame: {frame}")
    print(f"Depth: {depth}")

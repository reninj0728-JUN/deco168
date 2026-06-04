"""
Gemini 空間 zoning v2 模組（production 可用版）

抽自 poc_zoning_v2.py，邏輯不變。提供：
    compute_zoning_v2(photo_paths, video_keyframes=None) -> dict
    draw_overlay(best_photo, zones, title, out_path)

v2 vs v1（zoning.py）差異：
- 兩層輸出：existing_zones（推測原意）vs proposed_zones（AI 建議）
- 每個 zone 帶 evidence / confidence / uncertainty_notes
- bounding_box（0–1000 normalized）給後續畫 overlay 用
- 接受 video_keyframes（可選），photos 路徑下也獨立可用

設計原則：
- 純函式、無 side effect（不寫 DB、不寫狀態檔）
- 失敗回 {"error": "...", "overall_confidence": "none"}
- GEMINI_API_KEY 或 GOOGLE_AI_KEY 任一可用
"""
import os
import json
import time
from pathlib import Path


GEMINI_KEY = (os.environ.get("GEMINI_API_KEY")
              or os.environ.get("GOOGLE_AI_KEY")
              or "").strip()


PROMPT = """\
這 {photo_count} 張是同一個空屋的不同角度照片{video_note}。
請以「同一空間」的角度合成理解，並輸出嚴格 JSON 描述功能分區。

【任務分兩層】
這個任務跟一般 zoning 不同：你必須區分

  Layer 1 — existing_zones（「原意用途推測」）:
    根據照片中可看出的建築線索（隔間、開口、地坪材質、燈具配線、窗戶面向、出入動線、
    抽油煙機/冷氣孔/水管預埋位置等）推測「設計師/建商最可能規劃的原始用途」。
    這不是 AI 想擺什麼，而是空間本身暗示了什麼。
    每個 zone 必須有 evidence（看到的具體線索）+ confidence + uncertainty_notes。
    如果空屋線索不足以判斷某區用途 → 不要硬判，標 "confidence": "low" + 在 uncertainty_notes 寫明缺什麼證據。

  Layer 2 — proposed_zones（「AI 建議擺位」）:
    在 existing_zones 的基礎上，AI 建議客廳/餐廳怎麼擺。
    如果 existing_zones 已經明確（例如有抽油煙機 → 廚房；有插座群集靠某一面 → TV 牆），
    proposed_zones 應該尊重 existing 判斷。
    如果 existing 模糊（例如空屋長條沒有任何家具線索），proposed_zones 要在 evidence 註明
    「此區屋主可選 X 或 Y」並把雙方案都列出來。

【bounding box（畫 overlay 用）】
請對「主視角照片」（index 0，第 1 張）每個 zone 給一個 normalized bounding box，
格式 [ymin, xmin, ymax, xmax]，數值 0–1000。
這是給後續 cv2 在主視角上畫透明色塊用，所以 bbox 應該標在「該 zone 在主視角畫面中佔的區域」。

【輸出 JSON】
{{
  "best_photo_index": 0,
  "spatial_synthesis": {{
    "room_shape": "...",
    "main_window_wall": "...",
    "entrance_position": "...",
    "wall_inventory": [
      {{"name": "...", "description": "...", "has_opening": true/false}}
    ]
  }},
  "existing_zones": {{
    "living_zone": {{
      "where": "...",
      "evidence": "...",
      "confidence": "high/medium/low",
      "uncertainty_notes": "...",
      "bbox_on_best_photo": [ymin, xmin, ymax, xmax]
    }},
    "dining_zone": {{...}},
    "kitchen_zone": {{...}},
    "entrance_zone": {{...}},
    "walkway": {{...}}
  }},
  "proposed_zones": {{
    "living_zone": {{
      "where": "...",
      "rationale": "為什麼 AI 建議擺這（要明確說「我尊重 existing X」或「existing 模糊，這是 AI 的選擇」）",
      "alt_option": "如果屋主想反過來擺（例：把客廳跟餐廳對調），這裡描述 alt 方案",
      "bbox_on_best_photo": [ymin, xmin, ymax, xmax]
    }},
    "dining_zone": {{...}},
    "walkway": {{...}},
    "no_large_furniture_zone": {{
      "where": "...",
      "reason": "...",
      "bbox_on_best_photo": [ymin, xmin, ymax, xmax]
    }}
  }},
  "overall_confidence": "high/medium/low",
  "overall_uncertainty": "整體上有哪些是 AI 不能保證的事（例：空屋無家具線索 → 客餐廳分配是建議而非事實）",
  "needs_user_input": ["如果需要屋主回答的問題，列在這裡。例：『請問您希望靠窗那端做客廳還是餐廳？』"]
}}

只回 JSON，不要多話。
"""


def _resolve_mime(p: Path) -> str:
    ext = p.suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".webp": "image/webp",
    }.get(ext, "image/jpeg")


def compute_zoning_v2(photo_paths: list, video_keyframes: list | None = None) -> dict:
    """
    跨多張同房間照片（+ 可選影片擷幀）合成 zoning v2 JSON。
    失敗回 {"error": "...", "overall_confidence": "none"}。
    """
    if not GEMINI_KEY:
        return {"error": "missing GEMINI_API_KEY / GOOGLE_AI_KEY", "overall_confidence": "none"}

    valid_photos: list[Path] = []
    for p in photo_paths or []:
        path = Path(p)
        if not path.exists() or path.stat().st_size < 1024:
            continue
        valid_photos.append(path)

    valid_videos: list[Path] = []
    if video_keyframes:
        for p in video_keyframes:
            path = Path(p)
            if path.exists() and path.stat().st_size >= 1024:
                valid_videos.append(path)

    if not valid_photos:
        return {"error": "no valid photos", "overall_confidence": "none"}

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        return {"error": f"google-genai not installed: {e}", "overall_confidence": "none"}

    client = genai.Client(api_key=GEMINI_KEY)

    parts: list = []
    for path in valid_photos:
        with open(path, "rb") as f:
            parts.append(types.Part.from_bytes(data=f.read(), mime_type=_resolve_mime(path)))
    for path in valid_videos:
        with open(path, "rb") as f:
            parts.append(types.Part.from_bytes(data=f.read(), mime_type=_resolve_mime(path)))

    video_note = (
        f"，外加 {len(valid_videos)} 張影片擷取畫面（給你看更全面動線）"
        if valid_videos else ""
    )
    parts.append(PROMPT.format(photo_count=len(valid_photos), video_note=video_note))

    print(f"[zoning_v2] photos={len(valid_photos)} video_frames={len(valid_videos)}")
    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=parts,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        elapsed = time.time() - t0
        print(f"[zoning_v2] Gemini 耗時 {elapsed:.1f}s")
        text = (resp.text or "").strip()
        if not text:
            return {"error": "empty response", "overall_confidence": "none"}
        return json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"json decode: {str(e)[:200]}", "overall_confidence": "none"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}", "overall_confidence": "none"}


# ── Overlay 繪製 ─────────────────────────────────────────────────────────────

ZONE_COLORS = {
    # BGR
    "living_zone":            (0, 200, 0),      # 綠
    "dining_zone":            (0, 165, 255),    # 橘
    "kitchen_zone":           (0, 0, 200),      # 紅
    "entrance_zone":          (200, 200, 0),    # 青
    "walkway":                (255, 0, 255),    # 紫
    "no_large_furniture_zone": (80, 80, 80),    # 灰
}

# cv2 預設字型不支援中文 → 用英文 label
ZONE_LABEL_EN = {
    "living_zone":             "LIVING",
    "dining_zone":             "DINING",
    "kitchen_zone":            "KITCHEN",
    "entrance_zone":           "ENTRANCE",
    "walkway":                 "WALKWAY",
    "no_large_furniture_zone": "NO BIG FURN",
}


def draw_overlay(best_photo: Path, zones: dict, title: str, out_path: Path):
    """在 best_photo 上畫透明色塊 + 文字標籤，輸出 PNG 到 out_path"""
    import cv2
    import numpy as np

    raw = best_photo.read_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    H, W = img.shape[:2]

    overlay = img.copy()
    labels: list = []

    for zone_name, zone_data in (zones or {}).items():
        if not isinstance(zone_data, dict):
            continue
        bbox = zone_data.get("bbox_on_best_photo")
        if not bbox or len(bbox) != 4:
            continue
        color = ZONE_COLORS.get(zone_name, (128, 128, 128))
        try:
            y0, x0, y1, x1 = bbox
            y0 = int(y0 / 1000.0 * H); x0 = int(x0 / 1000.0 * W)
            y1 = int(y1 / 1000.0 * H); x1 = int(x1 / 1000.0 * W)
            y0, y1 = max(0, min(y0, y1)), min(H, max(y0, y1))
            x0, x1 = max(0, min(x0, x1)), min(W, max(x0, x1))
            if y1 <= y0 or x1 <= x0:
                continue
            cv2.rectangle(overlay, (x0, y0), (x1, y1), color, -1)
            cv2.rectangle(img,     (x0, y0), (x1, y1), color, 3)
            label_text = ZONE_LABEL_EN.get(zone_name, zone_name.upper())
            labels.append((label_text, (x0 + 8, max(y0 + 28, 30)), color))
        except Exception as e:
            print(f"  bbox 解析失敗 {zone_name}: {e}")

    blended = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)

    for text, pos, color in labels:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(blended, (pos[0] - 2, pos[1] - th - 4), (pos[0] + tw + 4, pos[1] + 4), (240, 240, 240), -1)
        cv2.putText(blended, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

    title_bar = np.full((50, blended.shape[1], 3), 30, dtype=np.uint8)
    cv2.putText(title_bar, title, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 215, 215), 2, cv2.LINE_AA)
    final = np.vstack([title_bar, blended])

    ok, buf = cv2.imencode(".png", final)
    out_path.write_bytes(buf.tobytes())
    print(f"  {out_path.name} ({final.shape[1]}x{final.shape[0]})")

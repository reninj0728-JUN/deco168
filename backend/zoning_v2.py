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

【語言鐵則】
**所有 JSON 文字欄位（where / evidence / rationale / alt_option / why_here /
 uncertainty_notes / message / needs_user_input 等等）必須使用繁體中文（zh-TW）。
 嚴禁使用英文、簡體中文。連 needs_user_input 裡的問題也必須是繁體中文。**

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

【沙發靠哪一面長牆（sofa_side）】
先選一張最能同時看見大門落地處、入口地面、主要走道、客廳地板與左右牆腳線的照片，
把它的索引寫入 best_photo_index。後續所有 bbox 與 struct_geometry_v1 座標都只能屬於這張照片。
針對 best_photo_index 的觀看者視角（畫面左 = left，畫面右 = right）判斷:
  - sofa_side: 沙發椅背應該貼「左側長牆」還是「右側長牆」。回 "left" 或 "right"。
    判斷依據（綜合考量，不要只看單一條件）:
      1. 哪一面長牆有「最長的連續實牆」可以完整靠一張沙發的背（沙發不該擋到門/開口）。
      2. 把沙發放這面後，對面長牆要能放電視櫃/視覺焦點，且人坐沙發時面向焦點而非走道。
      3. 門/通道淨空: 沙發不可擋住任何房門、玄關或主動線開口。
      4. 大門迴避（1A3B0C68 根治）: 若某側長牆上有大門/玄關門，人坐沙發時的視線
         不可以朝著大門。電視/焦點牆若不得不跟大門同一側牆（對側是唯一實牆時），
         必須在 sofa_wall 文字中明確註明：「電視櫃放在離大門較遠的深段實牆、
         整組客廳往內移，沙發視線對電視不對大門」。
    若兩面長牆條件接近、難以決定 → sofa_side 仍給最佳猜測，但 sofa_side_confidence 標 "low"。
  - tv_side: 電視/視覺焦點牆，必為 sofa_side 的「對面」("left"↔"right")。
  - sofa_side_confidence: "high"/"medium"/"low" — 你對 sofa_side 的信心。
  - sofa_side_reason: 一句話說明為什麼選這面（繁體中文）。
  - alt_sofa_side / alt_tv_side: 若屋主選方案 B（客餐廳對調），沙發/電視各自該靠哪面（"left"/"right"）。
    若方案 B 的沙發側與方案 A 相同就照填相同值。

【bounding box（畫 overlay 用）】
請對 best_photo_index 指定的同一張照片，每個 zone 給一個 normalized bounding box，
格式 [ymin, xmin, ymax, xmax]，數值 0–1000。
這是給後續 cv2 在同一張照片上畫透明色塊用。

【S2 結構觀測｜不可猜、不可跨照片】
輸出 struct_geometry_v1。source_photo_index 必須等於 best_photo_index，所有座標都使用
該照片的 [y, x] 0–1000；不可跨照片拼接座標，也不可把另一張照片看見的門套到這張。
每一項都要有 status="observed/inferred/missing"、confidence="high/medium/low"、
visibility="full/partial/occluded/not_visible"。只有邊界在該照片中真的看得見才能標 observed；
看不見就標 missing，不可用常識補線。

elements 必須逐項回覆：
  - door_quad.polygon_yx1000：大門可見四邊形
  - door_floor_contact.segment_yx1000：大門與地板接觸的可見線段
  - entrance_landing.polygon_yx1000：門內第一個不可擺家具的入口落腳地面
  - walkway.polygon_yx1000：從大門通往室內的主要通行地面
  - living_floor.polygon_yx1000：客廳可用地板範圍
  - left_wall_floor.segment_yx1000：左牆與地板交線，第一點近鏡頭、第二點深處
  - right_wall_floor.segment_yx1000：右牆與地板交線，第一點近鏡頭、第二點深處

usable_wall_segments 只能列 observed 的連續實牆。每段包含 id、side="left/right"、
status、confidence、visibility、t_start、t_end。t 是沿同側 wall_floor 線由近端 0 到深端 1；
門、窗、走道開口所在範圍不可列為 usable。若任何必需結構看不清，struct_geometry_v1.status
必須是 partial 或 missing，並在 uncertainty_notes 說明需要補拍哪個角度。

【輸出 JSON】
{{
  "best_photo_index": 整數（你選出的單一結構主視角索引）,
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
      "sofa_side": "left/right（主視角觀看者視角，沙發椅背貼哪面長牆）",
      "tv_side": "left/right（sofa_side 的對面，電視/焦點牆）",
      "sofa_side_confidence": "high/medium/low",
      "sofa_side_reason": "一句話說明為什麼沙發靠這面（繁體中文）",
      "alt_sofa_side": "left/right（方案 B 的沙發側）",
      "alt_tv_side": "left/right（方案 B 的電視側）",
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
  "struct_geometry_v1": {{
    "schema_version": "struct-geometry-v1",
    "source_photo_index": 與 best_photo_index 完全相同的整數,
    "status": "observed/partial/missing",
    "elements": {{
      "door_quad": {{"kind":"door_quad","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","polygon_yx1000":[[y,x], ...]}},
      "door_floor_contact": {{"kind":"door_floor_contact_edge","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","segment_yx1000":[[y,x],[y,x]]}},
      "entrance_landing": {{"kind":"entrance_landing","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","polygon_yx1000":[[y,x], ...]}},
      "walkway": {{"kind":"walkway","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","polygon_yx1000":[[y,x], ...]}},
      "living_floor": {{"kind":"living_floor","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","polygon_yx1000":[[y,x], ...]}},
      "left_wall_floor": {{"kind":"wall_floor_boundary","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","segment_yx1000":[[y,x],[y,x]]}},
      "right_wall_floor": {{"kind":"wall_floor_boundary","status":"observed/inferred/missing","confidence":"high/medium/low","visibility":"full/partial/occluded/not_visible","segment_yx1000":[[y,x],[y,x]]}}
    }},
    "usable_wall_segments": [{{"id":"...","side":"left/right","status":"observed","confidence":"high/medium","visibility":"full/partial","t_start":0.0,"t_end":1.0}}],
    "uncertainty_notes": "..."
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


def normalize_struct_geometry_payload(result: dict, photo_count: int) -> dict:
    """Fail-closed normalization for Gemini's S2 geometry envelope.

    This function never creates geometry. It only validates photo binding and
    makes a missing/invalid state explicit so downstream code cannot mistake an
    absent model field for usable evidence.
    """
    normalized = dict(result or {})
    best = normalized.get("best_photo_index")
    best_valid = (
        not isinstance(best, bool)
        and isinstance(best, int)
        and 0 <= best < max(0, int(photo_count))
    )
    if not best_valid:
        best = None
        normalized["best_photo_index"] = None

    raw = normalized.get("struct_geometry_v1")
    if not isinstance(raw, dict):
        normalized["struct_geometry_v1"] = {
            "schema_version": "struct-geometry-v1",
            "source_photo_index": best,
            "status": "missing" if best is not None else "invalid",
            "elements": {},
            "usable_wall_segments": [],
            "uncertainty_notes": "Gemini 未回傳 S2 結構觀測。",
            "validation_errors": [] if best is not None else ["INVALID_BEST_PHOTO_INDEX"],
        }
        return normalized

    struct = dict(raw)
    struct.setdefault("schema_version", "struct-geometry-v1")
    struct.setdefault("elements", {})
    struct.setdefault("usable_wall_segments", [])
    errors = list(struct.get("validation_errors") or [])
    source_index = struct.get("source_photo_index")
    source_valid = (
        not isinstance(source_index, bool)
        and isinstance(source_index, int)
        and 0 <= source_index < max(0, int(photo_count))
    )
    if not source_valid:
        errors.append("INVALID_SOURCE_PHOTO_INDEX")
    if best is None:
        errors.append("INVALID_BEST_PHOTO_INDEX")
    elif source_index != best:
        errors.append("CROSS_PHOTO_COORDS")
    if struct.get("schema_version") != "struct-geometry-v1":
        errors.append("INVALID_STRUCT_SCHEMA_VERSION")
    if errors:
        struct["status"] = "invalid"
    elif struct.get("status") not in ("observed", "partial", "missing"):
        struct["status"] = "partial"
    struct["validation_errors"] = list(dict.fromkeys(errors))
    normalized["struct_geometry_v1"] = struct
    return normalized


def _generate_json_with_retry(
    *, client, types_module, parts: list, model: str, max_attempts: int = 2,
) -> dict:
    """Request strict JSON, retrying once only for malformed/empty model output."""
    last_error = None
    for attempt in range(max(1, int(max_attempts))):
        contents = list(parts)
        if attempt:
            contents.append(
                "Your previous response was malformed JSON. Return the same answer again as one "
                "complete strict JSON object only. Do not add markdown or commentary."
            )
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types_module.GenerateContentConfig(response_mime_type="application/json"),
        )
        text = (response.text or "").strip()
        if not text:
            last_error = json.JSONDecodeError("empty response", "", 0)
            continue
        try:
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                result, _ = json.JSONDecoder().raw_decode(text)
            if not isinstance(result, dict):
                raise json.JSONDecodeError("top-level JSON must be an object", text, 0)
            return result
        except json.JSONDecodeError as exc:
            last_error = exc
    raise last_error or json.JSONDecodeError("invalid JSON response", "", 0)


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

    # 縮圖省 Gemini 視覺 token（分區 bbox 用 0–1000 normalized，跟解析度無關 → 品質不受影響）
    try:
        from gemini_analyze import _downscale_for_vision
    except Exception:
        def _downscale_for_vision(d, m, **kw): return d, m
    parts: list = []
    for path in valid_photos:
        with open(path, "rb") as f:
            _d, _m = _downscale_for_vision(f.read(), _resolve_mime(path))
            parts.append(types.Part.from_bytes(data=_d, mime_type=_m))
    for path in valid_videos:
        with open(path, "rb") as f:
            _d, _m = _downscale_for_vision(f.read(), _resolve_mime(path))
            parts.append(types.Part.from_bytes(data=_d, mime_type=_m))

    video_note = (
        f"，外加 {len(valid_videos)} 張影片擷取畫面（給你看更全面動線）"
        if valid_videos else ""
    )
    parts.append(PROMPT.format(photo_count=len(valid_photos), video_note=video_note))

    print(f"[zoning_v2] photos={len(valid_photos)} video_frames={len(valid_videos)}")
    t0 = time.time()
    try:
        result = _generate_json_with_retry(
            client=client,
            types_module=types,
            parts=parts,
            model="gemini-3.5-flash",
            max_attempts=2,
        )
        elapsed = time.time() - t0
        print(f"[zoning_v2] Gemini 耗時 {elapsed:.1f}s")
        return normalize_struct_geometry_payload(result, photo_count=len(valid_photos))
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
    # overlay 是給手機/網頁看的確認圖，原尺寸 12MP PNG 高達 17MB——
    # 上傳慢、前端載入更慢。縮到長邊 1600px（bbox 是 0-1000 正規化，不受影響）。
    H0, W0 = img.shape[:2]
    _max_side = 1600
    if max(H0, W0) > _max_side:
        _sc = _max_side / max(H0, W0)
        img = cv2.resize(img, (int(W0 * _sc), int(H0 * _sc)), interpolation=cv2.INTER_AREA)
    H, W = img.shape[:2]

    overlay = img.copy()

    # 純色塊 overlay（文字 label 由前端 HTML 圖例顯示，不在圖上打英文字）
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
        except Exception as e:
            print(f"  bbox 解析失敗 {zone_name}: {e}")

    blended = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)
    final = blended  # 不再加標題列（前端有頁面標題）

    ok, buf = cv2.imencode(".png", final)
    out_path.write_bytes(buf.tobytes())
    print(f"  {out_path.name} ({final.shape[1]}x{final.shape[0]})")

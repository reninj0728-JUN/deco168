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
import hashlib
from datetime import datetime, timezone
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

usable_wall_segments 只能列 observed 的實牆。每段包含 id、side="left/right"、
status、confidence、visibility、t_start、t_end。t 是沿同側 wall_floor 線由近端 0 到深端 1；
門、窗、走道開口「所在的那一段範圍」不可列為 usable。

**同一側可以、而且應該列出多段。**牆被門或開口切斷時，請把開口之間剩下的每一段實牆
各列一段，不要因為「不是一整片連續實牆」就整側留空。臥室門與通道之間的隔間牆垛、
兩個開口中間的短牆，只要牆面本身是實的、看得見牆與地板的交線，就要列出來——
那種牆垛常常正是唯一能放電視櫃的位置。整側完全沒有任何實牆段時才留空。

若任何必需結構看不清，struct_geometry_v1.status
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

{prefer_note}
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


_RETRY_INSTRUCTION = (
    "Your previous response was malformed JSON. Return the same answer again as one "
    "complete strict JSON object only. Do not add markdown or commentary."
)

# 解析／正規化契約的版本。**改動 normalize_struct_geometry_payload 的行為時要手動 bump。**
# 這是幾何快取該失效的訊號；拿 code_revision 當訊號會讓每次無關部署都清空整份快取。
NORMALIZER_VERSION = "2026-07-30"


# 送模設定的**唯一來源**：送出與 provenance 都走這一份，改了指紋就會跟著變。
_GENERATION_CONFIG_KWARGS = {"response_mime_type": "application/json"}
_GENERATION_CONFIG_OBSERVED = ("response_mime_type", "temperature", "seed",
                               "top_p", "top_k", "candidate_count")


def _generation_config_snapshot(types_module) -> dict:
    """從實際送模用的同一份設定取值，不是另抄一份字面值。

    未設的欄位讀出來是 None＝用模型預設；哪天有人鎖了 temperature 或 seed，
    request_fingerprint 會自己跟著變，不會拿舊設定的幾何誤命中。
    """
    try:
        cfg = types_module.GenerateContentConfig(**_GENERATION_CONFIG_KWARGS)
        snapshot = {k: getattr(cfg, k, None) for k in _GENERATION_CONFIG_OBSERVED}
    except Exception:
        snapshot = dict(_GENERATION_CONFIG_KWARGS)
    return {k: (v if v is None or isinstance(v, (str, int, float, bool)) else str(v))
            for k, v in snapshot.items()}


def _canonical_sha256(payload) -> str:
    """對結構做 canonical JSON（排序鍵、無空白）後雜湊——欄位順序不得影響指紋。"""
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _generate_json_with_retry(
    *, client, types_module, parts: list, model: str, max_attempts: int = 2,
    attempt_trace: list | None = None,
) -> dict:
    """Request strict JSON, retrying once only for malformed/empty model output.

    `attempt_trace` 為純觀測輸出：每次實際送出都追加一筆，讓 provenance 能說出
    「最終這份幾何是第幾次嘗試、那次的 request 有沒有被追加修復提示詞」。
    只記雜湊與結果，不記模型回應內容。
    """
    last_error = None
    for attempt in range(max(1, int(max_attempts))):
        contents = list(parts)
        extra_sha = None
        if attempt:
            contents.append(_RETRY_INSTRUCTION)
            extra_sha = hashlib.sha256(_RETRY_INSTRUCTION.encode("utf-8")).hexdigest()

        def _record(outcome: str) -> None:
            if attempt_trace is not None:
                attempt_trace.append({
                    "attempt": attempt + 1,
                    "extra_prompt_sha256": extra_sha,
                    "outcome": outcome,
                })

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types_module.GenerateContentConfig(**_GENERATION_CONFIG_KWARGS),
        )
        text = (response.text or "").strip()
        if not text:
            last_error = json.JSONDecodeError("empty response", "", 0)
            _record("empty_response")
            continue
        try:
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                result, _ = json.JSONDecoder().raw_decode(text)
            if not isinstance(result, dict):
                raise json.JSONDecodeError("top-level JSON must be an object", text, 0)
            _record("ok")
            return result
        except json.JSONDecodeError as exc:
            last_error = exc
            _record("invalid_json")
    raise last_error or json.JSONDecodeError("invalid JSON response", "", 0)


def _build_provenance(*, model_id: str, prompt_text: str, sent_media: list[dict],
                      normalized: dict, generation_config: dict | None = None,
                      attempt_trace: list[dict] | None = None) -> dict:
    """這一次 zoning 到底是「哪張影像、哪個模型、哪份 prompt、哪版程式」跑出來的。

    2026-07-30 受控三跑證實：同一份請求會產生結構不同的幾何（living_floor 4↔6 點、
    候選數 7/6/5、連沙發左右都不同）。要做版本化幾何快取，就得先有可信的 key 材料；
    而在此之前 result_json 只有寫死的 build_tag（十天沒變過），model 與 schema
    根本沒被記錄，歷史單連「同一天是不是同一版」都無法確認。

    **純觀測欄位：只寫不讀。** pipeline 任何判斷都不得依賴它。
    只存雜湊與版本，不存 prompt 原文、簽名網址或金鑰。

    兩個指紋刻意分開，因為它們失效的時機不同：
      request_fingerprint    ── 送進模型的東西（模型／送模設定／已格式化 prompt／
                                實際送出的影像位元組與 MIME）。canonical JSON 後雜湊，
                                欄位順序不影響結果。
      interpreter_fingerprint ── 回應被怎麼解讀（schema 版本＋normalizer 版本）。
    快取 key 兩個都要看：請求一樣但解析器換了，舊幾何一樣不能用。
    `code_revision` **故意不進任何指紋**——這專案天天推 master，把 commit sha 綁進
    key 會讓每次無關部署都清空整份快取；它只作為觀測欄位，要失效請 bump
    NORMALIZER_VERSION。
    """
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    gen_cfg = dict(generation_config or {})
    struct = (normalized or {}).get("struct_geometry_v1") or {}
    schema_version = struct.get("schema_version") or "struct-geometry-v1"
    attempts = [
        {
            "attempt": a.get("attempt"),
            "retry_suffix": bool(a.get("extra_prompt_sha256")),
            "outcome": a.get("outcome"),
            # 該次「實際送出」的 request：base 請求 ＋ 有沒有被追加修復提示詞
            "request_fingerprint": _canonical_sha256({
                "base": _canonical_sha256({
                    "model": model_id, "generation_config": gen_cfg,
                    "prompt_sha256": prompt_sha,
                    "sent_media": [{"kind": m["kind"], "index": m["index"],
                                    "sha256": m["sha256"], "mime": m.get("mime")}
                                   for m in sent_media],
                }),
                "extra_prompt_sha256": a.get("extra_prompt_sha256"),
            }),
        }
        for a in (attempt_trace or [])
    ]
    return {
        "model": model_id,
        # /health 用的同一個來源；本機或取不到時記 unknown，不可拿來當快取 key
        "code_revision": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:8] or "unknown",
        "schema_version": schema_version,
        "normalizer_version": NORMALIZER_VERSION,
        "prompt_sha256": prompt_sha,
        "prompt_chars": len(prompt_text),
        # 送模設定：目前只設 response_mime_type，temperature/seed 未設＝用預設
        "generation_config": gen_cfg,
        "sent_media": sent_media,
        "source_photo_index": struct.get("source_photo_index"),
        "request_fingerprint": _canonical_sha256({
            "model": model_id,
            "generation_config": gen_cfg,
            "prompt_sha256": prompt_sha,
            "sent_media": [{"kind": m["kind"], "index": m["index"],
                            "sha256": m["sha256"], "mime": m.get("mime")}
                           for m in sent_media],
        }),
        "fingerprint_inputs": ["model", "generation_config", "prompt_sha256",
                               "sent_media[].kind", "sent_media[].index",
                               "sent_media[].sha256", "sent_media[].mime"],
        "interpreter_fingerprint": _canonical_sha256({
            "schema_version": schema_version,
            "normalizer_version": NORMALIZER_VERSION,
        }),
        # 最終這份幾何是第幾次嘗試回來的；retry 會追加修復提示詞＝送出的 request 不同
        "attempt_count": len(attempts),
        "used_retry": any(a["retry_suffix"] for a in attempts),
        "attempts": attempts,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def compute_zoning_v2(photo_paths: list, video_keyframes: list | None = None,
                      prefer_index: int | None = None) -> dict:
    """
    跨多張同房間照片（+ 可選影片擷幀）合成 zoning v2 JSON。
    失敗回 {"error": "...", "overall_confidence": "none"}。

    prefer_index：客戶在上傳頁明確標成「客廳」的那張照片索引。

    🔴 為什麼要在**呼叫模型之前**指定，而不是事後改 best_photo_index：
       所有 bbox 與 struct_geometry_v1 的座標都焊死在模型選的那張照片上
       （見下方 prompt 的「不可跨照片拼接座標」）。事後改索引＝拿另一張照片的
       門與牆腳去擺這張的家具，比擋死更危險。

       而幾何算在哪張，後面整條鏈都跟著：底圖不是幾何那張時 guide 建不出來
       （api.py「底圖不是 zoning 主視角，禁止跨照片套 bbox」）→ 格局前檢擋死
       → 客廳零圖。D4001755 / 293BDE11 都是這個死鏈。
       讓幾何一開始就算在客戶標的那張，整條鏈自然通。
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
    # 記錄「實際送出」的影像指紋——不是原始檔，縮圖後的位元組才是模型看到的東西。
    sent_media: list[dict] = []
    for kind, paths in (("photo", valid_photos), ("video_frame", valid_videos)):
        for index, path in enumerate(paths):
            with open(path, "rb") as f:
                _d, _m = _downscale_for_vision(f.read(), _resolve_mime(path))
            parts.append(types.Part.from_bytes(data=_d, mime_type=_m))
            sent_media.append({
                "kind": kind,
                "index": index,
                "sha256": hashlib.sha256(_d).hexdigest(),
                "bytes": len(_d),
                "mime": _m,
            })

    video_note = (
        f"，外加 {len(valid_videos)} 張影片擷取畫面（給你看更全面動線）"
        if valid_videos else ""
    )
    # 客戶已明確指定客廳主視角時，寫進 prompt 硬性要求——不是事後改索引。
    prefer_note = ""
    # ⚠️ bool 是 int 的子類：True 會被當成索引 1，把幾何算到錯的照片上。
    if (isinstance(prefer_index, int) and not isinstance(prefer_index, bool)
            and 0 <= prefer_index < len(valid_photos)):
        prefer_note = (
            f"\n\n【主視角已由屋主指定】\n"
            f"屋主已明確指出**第 {prefer_index + 1} 張**（索引 {prefer_index}）是客廳。\n"
            f"best_photo_index 必須等於 {prefer_index}，所有 bbox 與 "
            f"struct_geometry_v1 座標都只能屬於這張照片。\n"
            f"即使你認為別張照片的結構線索更完整，也不得改選——屋主指定的是"
            f"「要設計哪個空間」，那是需求不是建議。\n"
            f"若這張照片看不見某些結構元素（例如大門落地處不在畫面內），"
            f"照實把該元素標成 status=\"missing\"／visibility=\"not_visible\"，"
            f"**不要**改用別張照片補。"
        )
    prompt_text = PROMPT.format(photo_count=len(valid_photos), video_note=video_note,
                                prefer_note=prefer_note)
    parts.append(prompt_text)
    model_id = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

    print(f"[zoning_v2] photos={len(valid_photos)} video_frames={len(valid_videos)}")
    t0 = time.time()
    attempt_trace: list[dict] = []
    try:
        result = _generate_json_with_retry(
            client=client,
            types_module=types,
            parts=parts,
            model=model_id,
            max_attempts=2,
            attempt_trace=attempt_trace,
        )
        elapsed = time.time() - t0
        print(f"[zoning_v2] Gemini 耗時 {elapsed:.1f}s")
        normalized = normalize_struct_geometry_payload(
            result, photo_count=len(valid_photos))
        normalized["_provenance"] = _build_provenance(
            model_id=model_id, prompt_text=prompt_text, sent_media=sent_media,
            normalized=normalized, generation_config=_generation_config_snapshot(types),
            attempt_trace=attempt_trace)
        return normalized
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


def entrance_no_go_polygon_for_overlay(best_photo: Path, struct_geometry_v1):
    """分區確認頁要畫的「門→對面牆」禁區，**讀規劃器那同一份幾何**。

    ⚠️ 刻意直接呼叫 `layout_geometry_s2.entrance_hard_no_go_polygon()`，不在這裡
    另外拉一個灰色 bbox——多畫一份就是第三套口徑，那正是這條帶當初出問題的原因
    （legacy 有門軸淨空帶、S2 沒繼承、分區頁又畫另一種方框）。

    回 None＝這張算不出禁區（門開在進深端／標記退化），照舊只畫 zones，不騙客戶。
    整個包在 try 裡：分區頁在關鍵路徑上，畫不出來絕不能讓它 500。
    """
    try:
        import cv2
        import numpy as np

        import layout_floor_reference_s2 as lfr
        import layout_geometry_s2 as lgs2

        elements = (struct_geometry_v1 or {}).get("elements")
        if not isinstance(elements, dict):
            return None
        arr = np.frombuffer(Path(best_photo).read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        height, width = img.shape[:2]

        def shape_of(name):
            raw = elements.get(name) or {}
            seg = raw.get("segment_yx1000")
            poly = raw.get("polygon_yx1000")
            pts = seg or poly
            if not pts:
                return None
            return {"shape": {"coordinates": [
                (p[1] / 1000.0 * width, p[0] / 1000.0 * height) for p in pts]}}

        items = {}
        for name in ("door_floor_contact", "left_wall_floor",
                     "right_wall_floor", "living_floor"):
            got = shape_of(name)
            if got is None:
                return None
            items[name] = got

        living_raw = (elements.get("living_floor") or {}).get("polygon_yx1000")
        reference = lfr.estimate_transverse_floor_reference(best_photo, living_raw)
        direction = (reference.get("direction_xy")
                     if isinstance(reference, dict)
                     and reference.get("status") == "observed" else None)
        result = lgs2.entrance_hard_no_go_polygon(
            items, width=width, height=height, transverse_direction_xy=direction)
        if result.get("status") != "observed" or not result.get("polygon"):
            print(f"[zoning-overlay] 門前禁區算不出來（{result.get('reason')}），只畫 zones")
            return None
        # 回正規化座標，讓 draw_overlay 用它自己的縮放後尺寸換算
        return [[x / width, y / height] for x, y in result["polygon"]]
    except Exception as exc:
        print(f"[zoning-overlay] 門前禁區略過: {type(exc).__name__}: {str(exc)[:90]}")
        return None


def draw_overlay(best_photo: Path, zones: dict, title: str, out_path: Path,
                 entrance_no_go_norm=None):
    """在 best_photo 上畫透明色塊 + 文字標籤，輸出 PNG 到 out_path

    entrance_no_go_norm：門→對面牆禁區的正規化多邊形（0~1），由
    `entrance_no_go_polygon_for_overlay()` 產生。給了就用「不放大型家具」同一個
    灰色畫成多邊形——那條帶跟著地板透視走，不是軸對齊方框，畫成矩形會失真。
    """
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

    # 門→對面牆的禁區帶。用「不放大型家具」同一個灰，客戶不需要學新圖例；
    # 畫成多邊形而不是矩形，因為它跟著地板透視走（矩形會蓋到牆面與家具區）。
    if entrance_no_go_norm:
        try:
            pts = np.array([[int(x * W), int(y * H)] for x, y in entrance_no_go_norm],
                           dtype=np.int32)
            if len(pts) >= 3:
                grey = ZONE_COLORS["no_large_furniture_zone"]
                cv2.fillPoly(overlay, [pts], grey)
                cv2.polylines(img, [pts], True, grey, 3)
        except Exception as e:
            print(f"  門前禁區繪製失敗（略過）: {type(e).__name__}: {e}")

    blended = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)
    final = blended  # 不再加標題列（前端有頁面標題）

    # 分區確認頁只是給客戶「看」的確認圖，不提供下載、不是成品。
    # PNG 每張約 1.9MB（78 張佔 Storage 146MB、53%）；JPEG q88 約 0.2MB，
    # 肉眼看不出差別。成品渲染圖早就是 JPEG 了，這裡是漏網的最後一處。
    ok, buf = cv2.imencode(".jpg", final, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    out_path.write_bytes(buf.tobytes())
    print(f"  {out_path.name} ({final.shape[1]}x{final.shape[0]})")

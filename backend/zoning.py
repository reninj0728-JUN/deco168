"""
Gemini 空間 zoning 模組（production 可用版）

抽自 poc_gemini_zoning.py，prompt 沿用 POC 已驗證版本（confidence: high）。

提供：
    compute_zoning(photo_paths, timeout_sec=30) -> dict

設計原則：
- 純函式、無 side effect（不寫檔、不寫 DB、不寫 log file）
- 失敗回 {"error": "...", "confidence": "none"}，pipeline 看 confidence 決定要不要用
- 最多吃 3 張本地圖片
- mime_type 依副檔名判別（jpg/jpeg/png/webp）
- GEMINI_API_KEY 或 GOOGLE_AI_KEY 任一可用
- key 缺失直接回 error，不 crash
"""
import os
import json
from pathlib import Path


# 與 poc_gemini_zoning.py 同份 prompt（已驗證 confidence: high）
PROMPT = """\
這 3 張是同一個房間的不同角度照片。請以「同一空間」的角度合成理解，並輸出嚴格 JSON 描述功能分區。

【判定原則】
- 必須跨 3 張照片交叉比對：哪面牆有窗？哪面牆有走廊開口？哪面牆是長邊/短邊？哪面牆是純白沒有開口？
- 必須根據建築線索（窗戶位置決定景觀朝向、走廊開口決定入口、牆面長度決定坐向）來判斷功能分區
- 不能憑空臆測。每個 zone 要附上「evidence」說明你看到什麼證據

【輸出嚴格 JSON】
{
  "spatial_synthesis": {
    "room_shape": "形狀描述（長型/方型，長寬比）",
    "main_window_wall": "窗戶在哪面牆（北/南/東/西 或 短邊/長邊/特徵描述）",
    "main_window_size": "窗戶尺寸與類型（落地窗/中型窗/拉門等）",
    "entrance_position": "主要入口/走廊開口位置（哪面牆、哪一端）",
    "exposed_ceiling": "天花板特徵（明管/灑水/燈具）",
    "wall_inventory": [
      {"name": "wall_A", "description": "...", "length": "近似公尺", "has_opening": true/false}
    ]
  },
  "zones": {
    "living_zone": {
      "where": "具體位置描述（例：靠窗那一半，從窗算起 0~3m 範圍）",
      "why_here": "為什麼這裡是客廳（採光/景觀/與入口距離）",
      "evidence": "從照片看到什麼支持這判斷"
    },
    "dining_zone": {
      "where": "...或 'none' 如果空間不夠或不需要",
      "why_here": "...",
      "evidence": "..."
    },
    "entrance_zone": {
      "where": "入口/玄關落腳區（沙發、家具不該擺這）",
      "why_here": "...",
      "evidence": "..."
    },
    "walkway": {
      "where": "主動線（從入口到窗的走道，必須留 0.8-1m 寬）",
      "evidence": "..."
    }
  },
  "furniture_placement_rules": {
    "sofa_wall": "沙發背靠哪面牆，朝哪個方向（具體到牆名）",
    "sofa_alternative": "如果無法靠牆，沙發可以怎麼擺（例如背對 walkway）",
    "tv_wall": "電視/視覺焦點該放哪面牆",
    "coffee_table_position": "茶几放哪",
    "rug_anchor": "地毯錨點（沙發前還是中央）",
    "accent_chair_position": "單椅該擺哪",
    "no_large_furniture_zones": [
      "區域 1：例如入口落腳區",
      "區域 2：例如走廊動線"
    ]
  },
  "confidence": "high/medium/low — 你對這個 zoning 判斷的信心",
  "ambiguity_notes": "如果有不確定的地方，列出來"
}

只回 JSON，不要多話。
"""


_MIME_BY_EXT = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
}


def _resolve_mime(path: Path) -> str:
    return _MIME_BY_EXT.get(path.suffix.lower(), "image/jpeg")


def _err(msg: str) -> dict:
    return {"error": msg, "confidence": "none"}


def compute_zoning(photo_paths: list[str], timeout_sec: int = 30) -> dict:
    """
    用 Gemini 跨多張同房間照片合成 zoning JSON。

    參數:
        photo_paths: 本地圖片路徑列表（最多取前 3 張）
        timeout_sec: 預留，目前 Gemini SDK 沒直接 timeout 參數，
                     交由上層 watchdog 控制（一般 10 秒返回）

    回傳:
        zoning dict（含 spatial_synthesis / zones / furniture_placement_rules / confidence）
        失敗回 {"error": "...", "confidence": "none"}
    """
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_AI_KEY")
        or ""
    ).strip()
    if not api_key:
        return _err("missing GEMINI_API_KEY / GOOGLE_AI_KEY")

    if not photo_paths:
        return _err("no photo_paths provided")

    valid_parts_sources: list[tuple[Path, str]] = []
    for p in photo_paths[:3]:
        path = Path(p)
        if not path.exists() or path.stat().st_size < 1024:
            continue
        valid_parts_sources.append((path, _resolve_mime(path)))

    if not valid_parts_sources:
        return _err("no readable photos (all paths missing or too small)")

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        return _err(f"google-genai not installed: {e}")

    try:
        client = genai.Client(api_key=api_key)
        parts = []
        for path, mime in valid_parts_sources:
            with open(path, "rb") as f:
                parts.append(types.Part.from_bytes(data=f.read(), mime_type=mime))
        parts.append(PROMPT)

        resp = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
            contents=parts,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        text = (resp.text or "").strip()
        if not text:
            return _err("empty response from gemini")
        return json.loads(text)
    except json.JSONDecodeError as e:
        return _err(f"json decode failed: {str(e)[:200]}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {str(e)[:300]}")

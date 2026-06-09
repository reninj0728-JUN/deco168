# DECO168 FastAPI Backend
# 啟動: cd backend && python3.11 -m uvicorn api:app --reload --port 8000
import os, sys, json, uuid, shutil, traceback

# 清除環境變數可能的換行符（Railway 有時會多帶 \n）
for _k in ("FAL_KEY", "GEMINI_API_KEY", "GOOGLE_AI_KEY", "SUPABASE_KEY", "FLUX_API_KEY"):
    if os.environ.get(_k):
        os.environ[_k] = os.environ[_k].strip()
from pathlib import Path
from typing import List
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import requests as _req

# C2.7 C1: Supabase / R2 / write_status helpers 已搬到 db_helpers.py
# api.py 透過 import 取用; route handler 行為 0 變化.
from db_helpers import (
    SUPABASE_URL, SUPABASE_KEY, _SB_HEADERS,
    sb_upsert, sb_get,
    sb_upload_file, sb_save_upload,
    sb_download_object, sb_get_upload, sb_upload_render,
    r2_presign_put, r2_download_object, r2_delete_object,
    write_status,
)

BASE_DIR    = Path(__file__).parent.resolve()
UPLOADS_DIR = BASE_DIR / "uploads"
JOBS_DIR    = BASE_DIR / "jobs"
UPLOADS_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="DECO168 API", version="1.0.2")

# 啟動時只 print True/False，不洩漏值
print(f"[startup] R2 access_key set: {bool(os.environ.get('CF_R2_ACCESS_KEY_ID') or os.environ.get('R2_ACCESS_KEY_ID'))}")
print(f"[startup] R2 secret set: {bool(os.environ.get('CF_R2_SECRET_ACCESS_KEY') or os.environ.get('R2_SECRET_ACCESS_KEY'))}")
print(f"[startup] R2 endpoint set: {bool(os.environ.get('CF_R2_ENDPOINT') or os.environ.get('R2_ENDPOINT'))}")
print(f"[startup] R2 bucket set: {bool(os.environ.get('CF_R2_BUCKET') or os.environ.get('R2_BUCKET'))}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/jobs", StaticFiles(directory=str(JOBS_DIR), html=False), name="jobs")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def flatten_zoning_v2_to_v1(zoning_v2: dict, layout_choice: str) -> dict:
    """
    Z2: 使用者確認過的 v2 zoning（existing_zones / proposed_zones）攤平成 v1 結構，
    讓既有 prompt_builder._build_layout_section() 不用改。
    layout_choice='B' 時，把 living/dining 對調（用 alt_option）。
    """
    ez = zoning_v2.get("existing_zones") or {}
    pz = zoning_v2.get("proposed_zones") or {}

    if layout_choice == "B":
        living = {
            "where": (pz.get("living_zone") or {}).get("alt_option") or (pz.get("dining_zone") or {}).get("where", ""),
            "why_here": "使用者選擇方案 B（替代佈局）",
            "evidence": "user choice",
        }
        dining = {
            "where": (pz.get("dining_zone") or {}).get("alt_option") or (pz.get("living_zone") or {}).get("where", ""),
        }
        sofa_wall_hint = (pz.get("living_zone") or {}).get("alt_option") or "the longest solid wall"
    else:
        # 'A' 或空字串都當 A 處理（預設）
        living = {
            "where": (pz.get("living_zone") or {}).get("where", ""),
            "why_here": (pz.get("living_zone") or {}).get("rationale", ""),
            "evidence": "user-confirmed AI recommendation",
        }
        dining = {
            "where": (pz.get("dining_zone") or {}).get("where", ""),
        }
        sofa_wall_hint = (pz.get("living_zone") or {}).get("rationale", "") or living["where"] or "the longest solid wall"

    no_go = []
    if pz.get("no_large_furniture_zone"):
        where = (pz["no_large_furniture_zone"] or {}).get("where", "")
        if where:
            no_go.append(where)

    return {
        "confidence":        zoning_v2.get("overall_confidence", "medium"),
        "spatial_synthesis": zoning_v2.get("spatial_synthesis") or {},
        "zones": {
            "living_zone":   living,
            "dining_zone":   dining,
            "walkway":       ez.get("walkway") or {},
            "entrance_zone": ez.get("entrance_zone") or {},
        },
        "furniture_placement_rules": {
            "sofa_wall":                sofa_wall_hint,
            "tv_wall":                  "",
            "coffee_table_position":    "in front of the sofa, on top of the rug",
            "rug_anchor":               "anchored under the coffee table in the living zone",
            "accent_chair_position":    "",
            "no_large_furniture_zones": no_go,
        },
        "_origin": "user_confirmed_v2",
        "_layout_choice": layout_choice or "A",
    }


def canonical_photo_key(s: str | None) -> str:
    """
    把 photo key 字串 canonical 化，用於 primary photo_keys vs paths.json 精確比對。

    處理：
      - None / 非字串 / 全空白 → ""
      - 去 scheme (supabase:// / r2:// / gemini://)
      - 去 query string (? 之後)
      - URL decode（前端有時送 %20 等）
      - 反斜線 → 正斜線
      - 去開頭斜線
      - 反覆剝離 directory 前綴: app/uploads/, uploads/
        （順序：較長先；避免 "app/" 殘留導致 mismatch）

    回傳格式典型為：<upload_id>/<filename>

    精確比對保證：保留 <upload_id>/<filename> 兩段，不退化到 basename-only,
    所以不同 upload_id 同名照片不會誤配。
    """
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if not s:
        return ""
    for prefix in ("supabase://", "r2://", "gemini://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    qmark = s.find("?")
    if qmark >= 0:
        s = s[:qmark]
    try:
        from urllib.parse import unquote
        s = unquote(s)
    except Exception:
        pass
    s = s.replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]
    # 較長前綴先剝, 否則 "app/uploads/" 會被 "uploads/" 部分匹配漏掉
    for prefix in ("app/uploads/", "uploads/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


def z3_needs_retry(validation: dict | None) -> tuple[bool, str]:
    """
    Z3: 判斷一張 render 是否需要重試。
    觸發條件（任一）：
      - validation.ok is False AND 有結構類 flag
        (walls/recessed/windows_changed, furniture_blocks_walkway)
      - reason 含結構/動線/家具擋路關鍵字
    回傳 (should_retry, reason_text)
    """
    if not isinstance(validation, dict):
        return False, ""
    if validation.get("ok") is not False:
        return False, ""

    bad_flags = []
    for k in ("walls_changed", "recessed_space_added", "windows_changed",
              "furniture_blocks_walkway", "sofa_faces_walkway",
              "sofa_outside_living_zone",
              "focal_anchor_misaligned_with_sofa"):
        if validation.get(k):
            bad_flags.append(k)

    reason = (validation.get("reason") or "").strip()
    bad_kw = [
        # 結構幻想（既有）
        "開口被封", "走廊消失", "牆面改變", "填平", "封閉", "通道",
        "封住", "被封", "封死",
        # 家具擋動線
        "家具擋", "沙發擋", "茶几擋", "地毯擋",
        "擋住走道", "擋住動線", "擋住通道", "擋住開口", "擋住走廊",
        "阻擋通道", "阻擋走道", "阻擋動線", "阻擋走廊",
        "動線不順", "動線受阻", "走道被擋", "通道被擋",
        "走廊開口被擋", "開口被擋",
        "浮在中間", "擋在中間", "沙發浮", "繞行",
        # 沙發朝向錯誤
        "沙發朝向走道", "沙發朝向通道", "沙發朝向走廊", "沙發朝向房門", "沙發朝向開口",
        "沙發面對走道", "沙發面對通道", "沙發面對走廊", "沙發面對房門", "沙發面對開口",
        "朝向走道", "朝向通道", "朝向走廊", "朝向房門",
        "面對走道", "面對通道", "面對走廊", "面對房門",
        # 沙發未在確認 living zone（Commit A 新）
        "未在確認", "違反確認分區", "違反 living zone", "違反客戶確認",
        "未在客戶確認", "未在 living zone", "未在客廳區",
        "沙發跑到", "沙發放錯區", "沙發位置不對",
        # 沙發位置 / 靠窗深度不對（C2.1 新）
        "沙發偏前", "沙發在前段", "沙發在中段", "沙發偏中段",
        "沙發在前中段", "沙發在前半段", "沙發在中間",
        # 動詞接續的位置描述（Gemini 常見句型）
        "放在中段", "擺在中段", "放在前段", "擺在前段",
        "放在中間", "擺在中間", "放在前半段", "擺在前半段",
        "中段而非", "前段而非", "中間而非",
        "未靠近窗邊", "不在靠窗區", "未在靠窗", "沒有靠窗",
        "偏離客戶確認區", "偏離確認區", "偏離 living zone", "偏離客廳區",
        "位於入口側", "位於入口", "位於餐廳區", "位於餐廳",
        "位於主動線", "位於走道", "位於前段", "位於中段", "位於中間",
        "深度位置不對", "深度位置錯", "靠窗深度不對",
        # 英文 fallback（Gemini 偶爾回英文）
        "walkway blocked", "corridor blocked",
        "blocks the walkway", "blocking the walkway",
        "blocks the corridor", "blocking the corridor",
        "sofa faces the corridor", "sofa faces the walkway",
        "sofa facing the corridor", "sofa facing the walkway",
        "sofa faces the doorway", "sofa facing the doorway",
        "sofa outside the confirmed",
        "outside the confirmed living zone",
        "violates the confirmed zone",
        "violates the confirmed layout",
        "not in the confirmed living zone",
        # C2.1 英文新（depth position 描述）
        "sofa is in the front half", "sofa in the front half",
        "sofa is in the middle zone", "sofa in the middle zone",
        "sofa is not near the window", "sofa not near the window",
        "sofa is away from the confirmed living zone",
        "sofa away from the confirmed living zone",
        "sofa placed near the entrance",
        "sofa placed in transition zone",
        "sofa placed in dining zone",
        "violates window-side living zone",
        "violates the window-side",
        "sofa is in the front", "sofa in the front",
        "sofa is too far from the window",
        # focal_anchor / TV 櫃對位錯誤（C2.2 新）
        "主牆家具未對齊沙發", "主牆家具未對齊", "主牆家具不對齊",
        "電視櫃未對齊沙發", "電視櫃未對齊", "電視櫃不對齊",
        "媒體櫃未對齊", "矮櫃未對齊", "邊櫃未對齊",
        "電視櫃位於前段", "電視櫃位於中段", "電視櫃位於前中段",
        "電視櫃在前段", "電視櫃在中段", "電視櫃在入口側",
        "媒體櫃位於前段", "媒體櫃位於中段", "媒體櫃在入口側",
        "焦點家具位於前段", "焦點家具位於中段", "焦點家具位於入口",
        "主牆家具位於餐廳", "主牆家具位於入口", "主牆家具位於走道",
        "電視櫃位於餐廳", "電視櫃位於入口側", "電視櫃位於主動線",
        "媒體櫃位於餐廳", "媒體櫃位於入口", "媒體櫃位於主動線",
        "焦點家具不存在", "焦點家具缺席", "沒有焦點家具",
        "只有壁畫沒有實體家具", "只有壁畫", "主牆只有壁畫",
        "客廳組合被拉散", "客廳被拉散", "客廳組合分散",
        "沙發與電視櫃距離過遠", "沙發與媒體櫃距離過遠",
        "沙發與主牆家具距離過遠",
        # focal_anchor 英文
        "focal anchor misaligned with sofa",
        "focal anchor is misaligned with the sofa",
        "main wall furniture is misaligned",
        "TV cabinet is too far from the sofa",
        "TV cabinet too far from the sofa",
        "media console is in the front zone",
        "media console in the front zone",
        "focal anchor is in the dining zone",
        "focal anchor in the dining zone",
        "focal anchor is in the entrance zone",
        "focal anchor in the entrance zone",
        "focal anchor not present",
        "no focal anchor present",
        "only wall art without furniture",
        "wall art only, no real furniture",
        "living group is stretched apart",
        "living group is stretched",
        "TV cabinet in the front zone",
        "TV cabinet in the dining zone",
        "TV cabinet in the entrance",
        "TV cabinet is in front",
    ]
    matched_kw = [kw for kw in bad_kw if kw in reason]
    if matched_kw:
        bad_flags.append(f"kw:{','.join(matched_kw)}")

    if not bad_flags:
        return False, ""
    suffix = f" | reason: {reason[:120]}" if reason else ""
    return True, ",".join(bad_flags) + suffix


VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

def extract_video_keyframes(video_path: str, out_dir: Path, count: int = 6) -> list[str]:
    """
    Phase 1.D: 影片均勻抽 N 個 keyframes，給 analyze_image 補理解用。
    位置 = (i+1)/(count+1) 避免黑頭黑尾。縮到 max 1280 寬。
    回傳成功抽出的檔案路徑 list（可能 < count，若影片有問題會略過壞幀）。
    """
    try:
        import cv2
    except ImportError:
        return []
    try:
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            cap.release()
            return []
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for i in range(count):
            pos = (i + 1) / (count + 1)
            fidx = max(0, min(total - 1, int(total * pos)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            if w > 1280:
                s = 1280 / w
                frame = cv2.resize(frame, (1280, int(h * s)), interpolation=cv2.INTER_AREA)
            out_p = out_dir / f"keyframe_{i:02d}.jpg"
            cv2.imwrite(str(out_p), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if out_p.exists() and out_p.stat().st_size > 1024:
                paths.append(str(out_p))
        cap.release()
        return paths
    except Exception as e:
        print(f"[extract_video_keyframes] 例外: {e}")
        return []


def extract_frame(video_path: str, out_path: str, position: float = 0.33) -> str:
    """從影片指定位置（0.0~1.0）抽一幀，回傳儲存路徑"""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(total * position)))
        ok, frame = cap.read()
        cap.release()
        if ok:
            cv2.imwrite(out_path, frame)
            return out_path
    except Exception:
        pass
    import subprocess
    ts = max(1, int(position * 30))  # 粗估秒數 fallback
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
         "-vframes", "1", "-q:v", "2", out_path],
        capture_output=True
    )
    return out_path


# ── Phase 1.1: ANCHORED upload_id 白名單 (內部測試分流, 非身份驗證) ────
# 流程: 操作員把測試 upload_id 設進 Railway env ANCHORED_TEST_UPLOAD_IDS,
# 等 redeploy 完成, 該訂單在 run_pipeline 內被命中 → force_anchored=True
# 命中後傳給 generate_renders, 由 generate_renders 自行決定 render_mode.
# 任何解析錯誤、env 空、未命中、upload_id 空 → fail-safe 走 legacy.
def _parse_anchored_uid_whitelist() -> set[str]:
    raw = os.environ.get("ANCHORED_TEST_UPLOAD_IDS", "") or ""
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _mask_upload_id(uid: str) -> str:
    if not uid:
        return "***"
    u = uid.strip()
    if len(u) < 5:
        return "*" * len(u)
    return f"{u[:2]}**{u[-3:]}"


# ── C2.6: 生成可靠性安全鎖 ──────────────────────────────────────
class AnchoredValidationFailed(Exception):
    """
    force_anchored=True 訂單在 retry 上限內仍未通過 validation.
    extras 用來帶 failed_render_styles + validation_reasons 給 result_json.
    """
    def __init__(self, message: str, extras: dict | None = None):
        super().__init__(message)
        self.extras = extras or {}


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _emit_pipeline_log(outcome: str, **fields):
    """run_pipeline 內部 structured log (與 [fal] 分開命名空間)"""
    parts = [f"outcome={outcome}"]
    for k in ("job_id", "upload_id_masked", "render_mode", "stage", "error_type"):
        v = fields.get(k)
        if v is not None and v != "":
            parts.append(f"{k}={v}")
    print("[pipeline] " + " ".join(parts))


def run_pipeline(job_id: str, photo_paths: list, styles: list, plan: str,
                 space_type: str = "living", render_angle: str = "single",
                 design_mode: str = "furnish",
                 user_zoning_v2: dict | None = None,
                 user_layout_choice: str = "",
                 budget_tier: str = "tier3",
                 customer_notes: str = "",
                 preferred_store: str = "none",
                 upload_id: str = ""):
    job_dir = JOBS_DIR / job_id
    os.chdir(str(BASE_DIR))

    # C2.6 失敗收尾追蹤狀態
    completed_flag = False
    failed_stage: str = "init"
    last_progress: int = 0
    last_render_mode: str | None = None
    uid_masked = _mask_upload_id((upload_id or "").strip().upper())

    try:
        failed_stage = "import"
        sys.path.insert(0, str(BASE_DIR))
        from test_full_pipeline import (
            analyze_image, generate_renders,
            FalGenerationTimeout, FalResultDownloadError,
        )
        from furniture_match import enrich_renders

        # Phase 1.1: 判定本訂單是否走 anchored 路徑 (僅內部測試)
        failed_stage = "anchored_decision"
        uid_norm = (upload_id or "").strip().upper()
        _anchored_wl = _parse_anchored_uid_whitelist()
        force_anchored = bool(uid_norm and _anchored_wl and uid_norm in _anchored_wl)
        if force_anchored:
            print(f"[render_mode] anchored whitelist matched upload_id={uid_masked}")
            last_render_mode = "anchored"
        else:
            print(f"[render_mode] legacy default upload_id={uid_masked}")
            last_render_mode = "legacy"

        # 先把 r2:// 或 supabase:// 影片從雲端下載到本機 job_dir
        # r2_keys_to_delete: pipeline 跑完後要清掉的 R2 物件
        r2_keys_to_delete: list[str] = []
        resolved_paths: list[str] = []
        for p in photo_paths:
            if p.startswith("r2://"):
                key = p[len("r2://"):]
                fname = key.split("/")[-1] or f"video_{uuid.uuid4().hex[:6]}.mp4"
                dest = job_dir / fname
                write_status(job_id, job_dir, "downloading", 8, "正在讀取你的空間影片…")
                local = r2_download_object(key, dest)
                if local:
                    resolved_paths.append(local)
                    r2_keys_to_delete.append(key)
                else:
                    print(f"[pipeline] R2 影片 {key} 下載失敗，跳過")
            elif p.startswith("supabase://"):
                # 舊版相容
                key = p[len("supabase://"):]
                fname = key.split("/")[-1] or f"video_{uuid.uuid4().hex[:6]}.mp4"
                dest = job_dir / fname
                write_status(job_id, job_dir, "downloading", 8, "正在讀取你的空間影片…")
                local = sb_download_object(key, dest)
                if local:
                    resolved_paths.append(local)
                else:
                    print(f"[pipeline] Supabase 影片 {key} 下載失敗，跳過")
            else:
                resolved_paths.append(p)
        photo_paths = resolved_paths

        gemini_uris = [p[len("gemini://"):] for p in photo_paths if p.startswith("gemini://")]
        video_paths = [p for p in photo_paths if not p.startswith("gemini://") and Path(p).suffix.lower() in VIDEO_EXTS]
        image_paths = [p for p in photo_paths if not p.startswith("gemini://") and Path(p).suffix.lower() not in VIDEO_EXTS]

        # Phase B (DEV)：USE_VIDEO_KEYFRAMES=1 時，影片用 cv2 抽 keyframes 併入 analyze_image
        # 預設關（=0），生產環境走原本 analyze_space 老路徑
        use_video_kf = os.environ.get("USE_VIDEO_KEYFRAMES", "0").strip() == "1"

        if (video_paths and use_video_kf and image_paths):
            # NEW path：影片本身上傳 Gemini Files API（理解材料）
            #          + 抽 keyframes 當 render 候選 base
            write_status(job_id, job_dir, "analyzing", 12, "抽影片關鍵幀…")
            kf_dir = job_dir / "video_keyframes"
            keyframes = extract_video_keyframes(video_paths[0], kf_dir, count=6)
            print(f"[pipeline] USE_VIDEO_KEYFRAMES=1 → 影片 + {len(keyframes)} keyframes 一起送 Gemini")
            augmented_paths = list(image_paths) + keyframes
            sources = (["photo"] * len(image_paths)) + (["video_keyframe"] * len(keyframes))
            write_status(job_id, job_dir, "analyzing", 15,
                         f"分析影片 + {len(image_paths)} 照 + {len(keyframes)} keyframes…")
            extra = augmented_paths[1:] if len(augmented_paths) > 1 else None
            analysis = analyze_image(augmented_paths[0], styles or None, extra_photos=extra,
                                     space_type=space_type, render_angle=render_angle,
                                     photo_sources=sources,
                                     video_path=video_paths[0])
            # 把 augmented_paths 寫回 image_paths 給後續 _resolve_region_base / zoning_photos 使用
            image_paths = augmented_paths
        elif gemini_uris or video_paths:
            from gemini_analyze import analyze_space
            if gemini_uris:
                write_status(job_id, job_dir, "analyzing", 15, "解析影片與照片，理解整體格局…")
                analysis = analyze_space(gemini_uris[0], user_styles=styles or None,
                                         is_uri=True, extra_photos=image_paths or None,
                                         space_type=space_type)
            else:
                write_status(job_id, job_dir, "analyzing", 10, "正在解析你的空間影片（大檔案需要幾分鐘）…")
                analysis = analyze_space(video_paths[0], user_styles=styles or None,
                                         extra_photos=image_paths or None,
                                         space_type=space_type)
        else:
            write_status(job_id, job_dir, "analyzing", 15, "理解空間格局中…")
            extra = image_paths[1:] if len(image_paths) > 1 else None
            analysis = analyze_image(image_paths[0], styles or None, extra_photos=extra,
                                     space_type=space_type, render_angle=render_angle)

        # Phase 1: 照片不足以滿足 (space_type, render_angle) 需求 → 早期失敗，不 render
        insufficient = analysis.get("insufficient_photos") if isinstance(analysis, dict) else None
        if insufficient and isinstance(insufficient, dict):
            req = insufficient.get("required")
            found = insufficient.get("found", 0)
            rt = insufficient.get("room_type", space_type)
            msg = insufficient.get("message") or f"本方案需 {req} 張 {rt} 空間照片，目前只有 {found} 張，請補上傳。"
            print(f"[pipeline] 早期失敗：insufficient_photos required={req} found={found} room_type={rt}")
            write_status(job_id, job_dir, "failed", 100, msg)
            sb_upsert({
                "job_id": job_id, "status": "failed", "message": msg,
                "result_json": {
                    "analysis": analysis,
                    "insufficient_photos": insufficient,
                    "error_code": "INSUFFICIENT_PHOTOS",
                },
            })
            return

        # ── 決定 Flux 輸入角度 ──
        # multi：用 Gemini regions[]（全室=不同房間 / 單房=同房不同角度）
        # single：Gemini best_photo_index 挑 1 張最美
        base_video = video_paths[0] if video_paths else None
        flux_bases: list[str] = []
        angle_labels: list[str] = []

        def _resolve_region_base(region: dict, idx: int) -> tuple[str | None, str]:
            """從 region 元素挑出一張 Flux 基底，回傳 (path, label)"""
            label = region.get("name") or f"角度{idx+1}"
            # 1. 優先用 Gemini 指定的 photo index
            ph_idx = region.get("best_photo_index")
            if image_paths and isinstance(ph_idx, int) and 0 <= ph_idx < len(image_paths):
                return image_paths[ph_idx], label
            # 2. 備案：用 video_position 抽幀
            if base_video:
                pos = region.get("video_position")
                if isinstance(pos, (int, float)) and 0 <= pos <= 1:
                    frame_path = str(job_dir / f"region_{idx:02d}.jpg")
                    extract_frame(base_video, frame_path, position=float(pos))
                    if Path(frame_path).exists():
                        return frame_path, label
            # 3. 最後 fallback：均勻抽影片 / 取照片
            if image_paths:
                return image_paths[idx % len(image_paths)], label
            if base_video:
                frame_path = str(job_dir / f"region_{idx:02d}_fallback.jpg")
                extract_frame(base_video, frame_path, position=(idx + 1) / 4)
                if Path(frame_path).exists():
                    return frame_path, label
            return None, label

        if render_angle == "multi":
            regions = analysis.get("regions") or []
            # Gemini 應該回 3 個；不足就補
            for i in range(3):
                region = regions[i] if i < len(regions) else {}
                path, label = _resolve_region_base(region, i)
                if path:
                    flux_bases.append(path)
                    angle_labels.append(label)
        else:
            # single：Gemini 挑最美 1 張
            if image_paths:
                best_idx = analysis.get("best_photo_index")
                if not isinstance(best_idx, int) or not (0 <= best_idx < len(image_paths)):
                    best_idx = 0
                flux_bases.append(image_paths[best_idx])
                angle_labels.append("主視角")
            elif base_video:
                frame_path = str(job_dir / "frame_main.jpg")
                extract_frame(base_video, frame_path, position=0.5)
                flux_bases.append(frame_path)
                angle_labels.append("主視角")

        if not flux_bases:
            raise RuntimeError("沒有可用的照片或影片幀作為渲染基底")

        print(f"[pipeline] 渲染基底 {len(flux_bases)} 張：{list(zip(angle_labels, [Path(p).name for p in flux_bases]))}")

        # ── Gemini zoning（給 Nano Banana prompt 用，失敗不阻斷） ──
        # 規則：best_photo_index 那張一定包含，再補同 upload 其他照片到最多 3 張
        zoning_photos: list[str] = []
        if image_paths:
            zb = analysis.get("best_photo_index")
            if not isinstance(zb, int) or not (0 <= zb < len(image_paths)):
                zb = 0
            zoning_photos.append(image_paths[zb])
            for i, p in enumerate(image_paths):
                if i != zb and len(zoning_photos) < 3:
                    zoning_photos.append(p)

        failed_stage = "zoning"
        last_progress = 40
        zoning_result: dict = {"confidence": "none", "error": "not computed"}
        if user_zoning_v2:
            # ── Z2: 使用者已在 zoning-confirm 確認 v2 分區，跳過重跑 ──
            write_status(job_id, job_dir, "zoning", 40, "套用您確認的分區設定…")
            try:
                zoning_result = flatten_zoning_v2_to_v1(user_zoning_v2, user_layout_choice or "A")
                print(f"[pipeline] 使用 user-confirmed zoning v2, layout_choice={user_layout_choice or 'A'}")
            except Exception as fe:
                print(f"[pipeline] flatten v2→v1 失敗，fallback compute_zoning: {fe}")
                user_zoning_v2 = None  # 失敗 → 走原本路徑
        if not user_zoning_v2:
            write_status(job_id, job_dir, "zoning", 40, "判讀空間動線中…")
            if zoning_photos:
                try:
                    from zoning import compute_zoning
                    zoning_result = compute_zoning(zoning_photos)
                except Exception as ze:
                    print(f"[pipeline] zoning 例外（不阻斷）: {ze}")
                    zoning_result = {"error": str(ze)[:300], "confidence": "none"}
        print(f"[pipeline] zoning confidence={zoning_result.get('confidence')} "
              f"error={zoning_result.get('error', '(none)')[:80]}")

        failed_stage = "matching"
        last_progress = 45
        write_status(job_id, job_dir, "matching", 45, "搭配風格家具中…")
        enriched = enrich_renders(analysis.get("renders", []), analysis=analysis,
                                  budget_tier=budget_tier,
                                  preferred_store=preferred_store)

        # ── 2 風格 × N 角度 = 多張渲染 ──
        # 為每個風格、每個角度產生一個 render entry
        expanded: list[dict] = []
        for style_entry in enriched:
            for base, label in zip(flux_bases, angle_labels):
                copy = dict(style_entry)
                copy["_angle_label"] = label
                copy["_base_path"] = base
                expanded.append(copy)

        total = len(expanded)
        write_status(job_id, job_dir, "rendering", 60,
                     f"生成 {total} 張設計提案中（{len(enriched)} 風格 × {len(flux_bases)} 視角）…")

        failed_stage = "render_main"
        last_progress = 60
        # 一次渲染一張：對應 base 不同（analysis + design_mode 傳進去）
        final = []
        for idx, entry in enumerate(expanded):
            single_result = generate_renders(entry["_base_path"], [entry],
                                             output_dir=str(job_dir),
                                             analysis=analysis, design_mode=design_mode,
                                             zoning=zoning_result,
                                             customer_notes=customer_notes,
                                             budget_tier=budget_tier,
                                             force_anchored=force_anchored,
                                             job_id=job_id,
                                             upload_id_masked=uid_masked,
                                             attempt=1,
                                             stage="initial")
            if single_result:
                r = single_result[0]
                r["angle_label"] = entry["_angle_label"]
                # 用 style + angle 區分檔名
                if r.get("render_path"):
                    src = Path(r["render_path"])
                    new_name = f"render_{entry.get('style','x')}_{idx:02d}{src.suffix}"
                    new_path = src.parent / new_name
                    try:
                        src.rename(new_path)
                        r["render_path"] = str(new_path)
                    except Exception:
                        pass
                final.append(r)

        result = {"analysis": analysis, "renders": final}
        with open(job_dir / "result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # ── 結構保留驗證（純評估、不重跑、不過濾、不影響前端）──
        failed_stage = "validate"
        last_progress = 85
        write_status(job_id, job_dir, "validating", 85, "確認設計品質中…")

        # Commit A：把 user_confirmed_v2 的 layout 資訊送給 validate_render
        # 讓 Gemini 多回一個 sofa_outside_living_zone flag
        def _build_layout_ctx(zr: dict | None) -> dict | None:
            if not isinstance(zr, dict):
                return None
            if zr.get("_origin") != "user_confirmed_v2":
                return None
            zones = zr.get("zones") or {}
            living = (zones.get("living_zone") or {}).get("where", "")
            if not living:
                return None
            walkway = (zones.get("walkway") or {}).get("where", "")
            rules = zr.get("furniture_placement_rules") or {}
            return {
                "layout_choice":  zr.get("_layout_choice") or "A",
                "living_where":   living,
                "sofa_wall_rule": rules.get("sofa_wall", ""),
                "walkway":        walkway,
            }

        layout_ctx = _build_layout_ctx(zoning_result)

        try:
            from gemini_analyze import validate_render
            for r in final:
                bpath = r.get("_base_path") or ""
                rpath = r.get("render_path") or ""
                if bpath and rpath and Path(bpath).exists() and Path(rpath).exists():
                    try:
                        v = validate_render(bpath, rpath, r.get("_angle_label", ""),
                                            layout_context=layout_ctx)
                    except Exception as ve:
                        v = {"ok": None, "error": str(ve)[:200]}
                else:
                    v = {"ok": None, "error": "missing base or render path"}
                r["validation"] = v
        except Exception as outer:
            print(f"[pipeline] 驗證階段例外: {outer}")
            for r in final:
                r.setdefault("validation", {"ok": None, "error": "validation step crashed"})

        # ── Z3: 結構失敗自動重試 1 次（僅 Nano Banana）──
        use_nano = os.environ.get("USE_NANO_BANANA", "0").strip() == "1"
        retry_n = 0
        # C2.3：高嚴重度 layout flag → 允許第 2 次 retry。一般 fail 維持 1 次。
        # 每張 render 最多 retry 2 次（總共 3 次生成）
        HIGH_SEVERITY_FLAGS = (
            "sofa_outside_living_zone",
            "focal_anchor_misaligned_with_sofa",
            "furniture_blocks_walkway",
            "sofa_faces_walkway",
        )
        def _has_high_severity(v: dict) -> bool:
            return isinstance(v, dict) and any(v.get(f) for f in HIGH_SEVERITY_FLAGS)

        def _build_retry_ctx_from_validation(v: dict) -> dict | None:
            """從前一次 validation 抽出 sofa_pct / anchor_pct 給 retry prompt。"""
            if not isinstance(v, dict):
                return None
            ctx = {}
            sp = v.get("sofa_depth_percent_estimate")
            ap = v.get("focal_anchor_depth_percent_estimate")
            if isinstance(sp, (int, float)):
                ctx["sofa_pct"] = sp
            if isinstance(ap, (int, float)) and ap >= 0:
                ctx["anchor_pct"] = ap
            return ctx or None

        if use_nano:
            failed_stage = "z3_retry"
            last_progress = 92
            # C2.6: anchored 白名單測試 retry 上限 = 1, legacy 維持 2
            MAX_RETRY = 1 if force_anchored else 2
            for idx in range(len(final)):
                # 每張 render 自己跑 retry loop（最多 MAX_RETRY 次）
                while True:
                    r = final[idx]
                    current_rc = int(r.get("retry_count") or 0)
                    if current_rc >= MAX_RETRY:
                        break  # 硬上限
                    v = r.get("validation") or {}
                    should_retry, retry_reason = z3_needs_retry(v)
                    if not should_retry:
                        break  # 已通過
                    # 第 2 次 retry 只允許高嚴重度 flag
                    if current_rc >= 1 and not _has_high_severity(v):
                        print(f"[pipeline] Z3 skip 2nd retry render[{idx}] — 非高嚴重度 flag")
                        break
                    if idx >= len(expanded):
                        break
                    entry = expanded[idx]
                    attempt_label = f"#{current_rc + 1}"
                    print(f"[pipeline] Z3 retry {attempt_label} render[{idx}] "
                          f"style={r.get('style')} — {retry_reason}")
                    write_status(job_id, job_dir, "rendering", 92, "修正結構問題的設計圖中…")
                    # 第 2 次 retry：帶入前次失敗的 depth_percent 給 retry prompt
                    retry_ctx = _build_retry_ctx_from_validation(v) if current_rc >= 1 else None
                    # C2.6 Patch B: Z3 retry 過程中, fal 明確失敗保留原 root cause
                    failed_stage = "z3_retry_generate_renders"
                    try:
                        retry_results = generate_renders(
                            entry["_base_path"], [entry],
                            output_dir=str(job_dir),
                            analysis=analysis, design_mode=design_mode,
                            zoning=zoning_result,
                            customer_notes=customer_notes,
                            budget_tier=budget_tier,
                            retry_context=retry_ctx,
                            force_anchored=force_anchored,
                            job_id=job_id,
                            upload_id_masked=uid_masked,
                            attempt=current_rc + 2,   # 初次=1, 1st retry=2, 2nd retry=3
                            stage="z3_retry",
                        )
                    except (FalGenerationTimeout, FalResultDownloadError):
                        # C2.6 Patch B: 不被後續 anchored validation collapse 改寫.
                        # 直接讓 outer except 把原始 error_type 寫進 result_json.
                        raise
                    except Exception as re_e:
                        print(f"[pipeline] Z3 retry 例外: {re_e}")
                        r["retry_count"] = current_rc + 1
                        r["retry_reason"] = f"retry exception: {str(re_e)[:200]}"
                        break
                    if not retry_results:
                        r["retry_count"] = current_rc + 1
                        r["retry_reason"] = f"{retry_reason} | retry returned empty"
                        break
                    new_r = retry_results[0]
                    # 改名加 _retry / _retry2
                    if new_r.get("render_path"):
                        src_p = Path(new_r["render_path"])
                        suffix_tag = "_retry" if current_rc == 0 else f"_retry{current_rc + 1}"
                        new_name = f"render_{entry.get('style','x')}_{idx:02d}{suffix_tag}{src_p.suffix}"
                        new_p = src_p.parent / new_name
                        try:
                            src_p.rename(new_p)
                            new_r["render_path"] = str(new_p)
                        except Exception:
                            pass
                    # 重新 validate（沿用同一個 layout_ctx）
                    try:
                        from gemini_analyze import validate_render
                        bpath = entry["_base_path"]
                        rpath = new_r.get("render_path") or ""
                        if rpath and Path(bpath).exists() and Path(rpath).exists():
                            new_v = validate_render(bpath, rpath, entry["_angle_label"],
                                                    layout_context=layout_ctx)
                        else:
                            new_v = {"ok": None, "error": "missing base or render path after retry"}
                    except Exception as ve:
                        new_v = {"ok": None, "error": f"revalidate failed: {str(ve)[:200]}"}
                    new_r["validation"]   = new_v
                    new_r["angle_label"]  = entry["_angle_label"]
                    new_r["retry_count"]  = current_rc + 1
                    new_r["retry_reason"] = retry_reason
                    final[idx] = new_r
                    retry_n += 1
                    # while loop 會再判一次：若新 v 仍 fail 且 current_rc+1 < MAX_RETRY 且高嚴重度 → 再 retry
        if retry_n:
            print(f"[pipeline] Z3 重試 {retry_n} 張")

        # 統計
        ok_n  = sum(1 for r in final if (r.get("validation") or {}).get("ok") is True)
        ng_n  = sum(1 for r in final if (r.get("validation") or {}).get("ok") is False)
        ng_reasons = [
            (r["validation"] or {}).get("reason") for r in final
            if (r.get("validation") or {}).get("ok") is False
            and (r["validation"] or {}).get("reason")
        ]
        validation_summary = {
            "total":      len(final),
            "ok":         ok_n,
            "ng":         ng_n,
            "ng_reasons": ng_reasons,
            "retry_count": retry_n,
        }
        print(f"[pipeline] 驗證統計 total={len(final)} ok={ok_n} ng={ng_n} retried={retry_n}")

        # 上傳渲染圖到 Supabase Storage
        slim_renders = []
        for r in final:
            raw_path = r.get("render_path") or ""
            render_path = Path(raw_path) if raw_path else None
            render_url = None
            if render_path and render_path.exists():
                render_url = sb_upload_render(job_id, render_path)
            slim_renders.append({
                "style":             r.get("style"),
                "style_label":       r.get("style_label"),
                "angle_label":       r.get("angle_label", "主視角"),
                "render_filename":   render_path.name if render_path else None,
                "render_url":        render_url,
                "render_error":      r.get("error"),
                "matched_furniture": r.get("matched_furniture", [])[:3],
                "validation":        r.get("validation"),
                # ── T4 新增：Nano Banana 路徑會帶；Flux 路徑用預設值 ──
                "pipeline_version":      r.get("pipeline_version", "flux-v1"),
                "reference_map":         r.get("reference_map", []),
                "notes":                 r.get("notes", ""),
                "unmatched_visual_items": r.get("unmatched_visual_items", []),
                # ── Z3 新增 ──
                "retry_count":   r.get("retry_count", 0),
                "retry_reason":  r.get("retry_reason"),
            })

        # Phase A：把客戶輸入寫入 result_json 給 result.html 顯示
        from furniture_match import BUDGET_LABEL_ZH, STORE_LABEL_ZH
        customer_inputs = {
            "budget_tier":              budget_tier,
            "budget_label_zh":          BUDGET_LABEL_ZH.get(budget_tier, ""),
            "customer_notes":           (customer_notes or "")[:300],
            "preferred_store":          preferred_store,
            "preferred_store_label_zh": STORE_LABEL_ZH.get(preferred_store, ""),
        }

        # ── P2-MVP-0: 把 /api/job 傳過來的 rooms_meta.json 補進 result_json ──
        # 沒檔案 = 沒 rooms = 等同 Phase A 原行為，不寫 rooms 欄位
        rooms_for_json: list = []
        primary_room_notes_for_json: str = ""
        rooms_meta_file = job_dir / "rooms_meta.json"
        if rooms_meta_file.exists():
            try:
                with open(rooms_meta_file, encoding="utf-8") as f:
                    rm = json.load(f)
                if isinstance(rm, dict):
                    if isinstance(rm.get("rooms"), list):
                        rooms_for_json = rm["rooms"]
                    if isinstance(rm.get("primary_room_notes"), str):
                        primary_room_notes_for_json = rm["primary_room_notes"]
            except Exception as me:
                print(f"[pipeline] rooms_meta 讀取失敗，忽略: {me}")

        if primary_room_notes_for_json:
            customer_inputs["primary_room_notes"] = primary_room_notes_for_json

        # Phase 1.1: 把每張 render 實際採用的 render_mode 滙集成 top-level
        # 由 generate_renders() 標示, api.py 不重新推測。
        # 全部相同 → 該值; 混合 → "mixed"; 全 None → 不寫.
        failed_stage = "result_build"
        _modes = {r.get("render_mode") for r in final if r.get("render_mode")}
        top_render_mode: str | None = None
        if len(_modes) == 1:
            top_render_mode = next(iter(_modes))
        elif len(_modes) > 1:
            top_render_mode = "mixed"
        last_render_mode = top_render_mode or last_render_mode

        # C2.6: anchored 路徑下若仍有 render validation.ok=False, 不可交付
        if force_anchored:
            bad_renders = [
                r for r in final
                if (r.get("validation") or {}).get("ok") is False
            ]
            if bad_renders:
                failed_styles = [r.get("style") for r in bad_renders if r.get("style")]
                reasons = []
                for r in bad_renders:
                    v = r.get("validation") or {}
                    reason = v.get("reason") or v.get("error") or ""
                    if reason:
                        reasons.append({"style": r.get("style"),
                                        "reason": str(reason)[:200]})
                raise AnchoredValidationFailed(
                    f"anchored validation failed on {len(bad_renders)} render(s) after retries",
                    extras={
                        "failed_render_styles": failed_styles,
                        "validation_reasons":   reasons,
                    },
                )

        result_json_payload = {
            "analysis":           analysis,
            "zoning":             zoning_result,
            "zoning_v2":          user_zoning_v2,             # Z2: 保留原始 v2（未轉換）
            "layout_choice":      user_layout_choice or None,
            "renders":            slim_renders,
            "validation_summary": validation_summary,
            "customer_inputs":    customer_inputs,            # Phase A
        }
        if top_render_mode:
            result_json_payload["render_mode"] = top_render_mode
        if rooms_for_json:
            result_json_payload["rooms"] = rooms_for_json     # P2-MVP-0

        # C2.6: completed DB write 需驗證, 否則不可設 completed_flag
        failed_stage = "result_upsert"
        sb_upsert({"job_id": job_id, "status": "completed", "progress": 100,
                   "message": "設計方案生成完畢！",
                   "result_json": result_json_payload})
        verify_row = sb_get(job_id) or {}
        if verify_row.get("status") != "completed":
            raise RuntimeError(
                f"completed DB write verification failed; "
                f"current status={verify_row.get('status')!r}"
            )
        completed_flag = True

        # 跑完自動清掉 R2 上的影片（隱私 + 省空間）
        for key in r2_keys_to_delete:
            ok = r2_delete_object(key)
            print(f"[pipeline] R2 清除 {key}: {'OK' if ok else 'FAIL'}")

    except Exception as e:
        # C2.6 失敗收尾: merge 現有 result_json 不蓋既有 analysis / zoning / partial renders
        err_txt = traceback.format_exc()
        try:
            existing_row = sb_get(job_id) or {}
            existing_rj = existing_row.get("result_json")
            if not isinstance(existing_rj, dict):
                existing_rj = {}
            diagnostic = {
                "error":         str(e)[:300],
                "error_type":    type(e).__name__,
                "failed_stage":  failed_stage,
                "render_mode":   last_render_mode,
                "last_progress": last_progress,
                "failed_at":     _utc_now_iso(),
                "traceback":     err_txt[-2000:],
            }
            if isinstance(e, AnchoredValidationFailed):
                diagnostic.update(e.extras)
            merged = {**existing_rj, **diagnostic}
            sb_upsert({"job_id": job_id, "status": "failed", "progress": 0,
                       "message": "生成逾時或處理失敗，請聯絡客服",
                       "result_json": merged})
            write_status(job_id, job_dir, "failed", 0, "處理失敗，請聯絡客服")
        except Exception as fe:
            _emit_pipeline_log("exception", job_id=job_id,
                               upload_id_masked=uid_masked,
                               render_mode=last_render_mode,
                               stage="failure_db_write",
                               error_type=type(fe).__name__)
        try:
            with open(job_dir / "error.log", "w", encoding="utf-8") as f:
                f.write(err_txt)
        except Exception:
            pass

    finally:
        # C2.6 防呆: 主要失敗處理由上方 except 負責, finally 只當補強
        # SIGKILL / OOM 不會走到這裡, 須由下一輪 watchdog 處理
        if not completed_flag:
            try:
                cur = sb_get(job_id) or {}
                cur_status = cur.get("status")
                if cur_status not in ("completed", "failed"):
                    cur_rj = cur.get("result_json") if isinstance(cur.get("result_json"), dict) else {}
                    merged_finally = {
                        **(cur_rj or {}),
                        "error":         "pipeline finally fallback (no exception caught)",
                        "error_type":    "FinallySafetyNet",
                        "failed_stage":  failed_stage,
                        "render_mode":   last_render_mode,
                        "last_progress": last_progress,
                        "failed_at":     _utc_now_iso(),
                    }
                    sb_upsert({"job_id": job_id, "status": "failed", "progress": 0,
                               "message": "處理失敗，請聯絡客服",
                               "result_json": merged_finally})
                    _emit_pipeline_log("finally_safety_net", job_id=job_id,
                                       upload_id_masked=uid_masked,
                                       render_mode=last_render_mode,
                                       stage=failed_stage)
            except Exception as fe:
                _emit_pipeline_log("exception", job_id=job_id,
                                   upload_id_masked=uid_masked,
                                   render_mode=last_render_mode,
                                   stage="finally_safety_net_db_write",
                                   error_type=type(fe).__name__)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.post("/api/r2/presign")
async def r2_presign(
    upload_id: str = Form(...),
    filename:  str = Form(...),
    content_type: str = Form(default="video/mp4"),
):
    """給前端一個 presigned PUT URL，讓影片直接 PUT 到 R2（繞過 Railway 5min timeout）"""
    safe = filename.replace("/", "_").replace("\\", "_")
    key = f"{upload_id}/{safe}"
    url = r2_presign_put(key, content_type=content_type, expires_in=3600)
    if not url:
        return JSONResponse(status_code=500, content={"error": "R2 未配置或 presign 失敗"})
    return {"url": url, "key": key}


@app.post("/api/upload")
async def upload_register(
    upload_id:  str = Form(...),
    photo_keys: str = Form(default="[]"),   # 新版：照片走前端直傳 Supabase Storage
    video_keys: str = Form(default="[]"),   # 影片走前端直傳 R2
):
    """
    上傳註冊端點（純 metadata，不接收檔案本體）：

    - 影片：前端用 presigned PUT 直傳 R2，這裡只收 R2 object key（r2://<key>）
    - 照片：前端用 anon key 直傳 Supabase Storage，這裡只收 storage key（supabase://<key>）

    返回 200 立即（無大檔案傳輸，不會 Failed to fetch）。
    """
    upload_dir = UPLOADS_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    local_paths: list[str] = []
    photo_urls:  list[str] = []

    # 影片 keys → r2://<key>
    try:
        vkeys = json.loads(video_keys or "[]")
        if not isinstance(vkeys, list):
            vkeys = []
    except Exception:
        vkeys = []
    for k in vkeys:
        if isinstance(k, str) and k.strip():
            local_paths.append(f"r2://{k.strip()}")

    # 照片 keys → supabase://<key>，pipeline 跑時會從 Supabase 下載
    try:
        pkeys = json.loads(photo_keys or "[]")
        if not isinstance(pkeys, list):
            pkeys = []
    except Exception:
        pkeys = []
    for k in pkeys:
        if isinstance(k, str) and k.strip():
            key_clean = k.strip()
            local_paths.append(f"supabase://{key_clean}")
            # 也建一個公開 URL 給 uploads table 紀錄（恢復用）
            photo_urls.append(f"{SUPABASE_URL}/storage/v1/object/{key_clean}")

    with open(upload_dir / "paths.json", "w", encoding="utf-8") as f:
        json.dump(local_paths, f)
    sb_save_upload(upload_id, photo_urls, "", vkeys)

    return {"upload_id": upload_id, "count": len(local_paths),
            "photos": len(pkeys), "videos": len(vkeys)}


@app.post("/api/job")
async def create_job(
    background_tasks: BackgroundTasks,
    upload_id: str    = Form(...),
    styles: str       = Form(default=""),
    plan: str         = Form(default="A"),
    space_type: str   = Form(default="living"),    # living/dining/bedroom/study/whole
    render_angle: str = Form(default="single"),    # single/multi
    design_mode: str  = Form(default="furnish"),   # furnish (只動家具) / full (含裝潢)
    layout_choice: str = Form(default=""),         # Z2: 'A'/'B'/'' (空字串=未確認)
    zoning_json: str   = Form(default=""),         # Z2: v2 zoning JSON 字串（前端從 localStorage 帶回）
    budget_tier: str       = Form(default="tier3"),  # Phase A: tier1/tier2/tier3
    customer_notes: str    = Form(default=""),       # Phase A: 客戶補充需求（後端硬截 300）
    preferred_store: str   = Form(default="none"),   # Phase A: none/momo/ikea/hola/trplus
    rooms_json: str        = Form(default=""),       # P2-MVP-0: 多空間 metadata（前端 localStorage 帶回）
):
    """建立 AI Job，在背景執行完整 pipeline"""
    paths_file = UPLOADS_DIR / upload_id / "paths.json"
    upload_dir = UPLOADS_DIR / upload_id

    # 本機找不到時，從 Supabase 恢復
    if not paths_file.exists():
        record = sb_get_upload(upload_id)
        if not record:
            return JSONResponse(status_code=404, content={"error": "upload_id not found，請重新上傳"})
        upload_dir.mkdir(parents=True, exist_ok=True)
        recovered: list[str] = []
        # 影片：用 R2 keys 重建 r2:// 虛擬路徑
        for k in (record.get("video_keys") or []):
            if isinstance(k, str) and k.strip():
                recovered.append(f"r2://{k.strip()}")
        # 照片：從 Supabase URL 下載回本機
        for url in (record.get("photo_urls") or []):
            fname = url.split("/")[-1]
            dest  = upload_dir / fname
            try:
                r = _req.get(url, headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}"
                }, timeout=30)
                if r.ok:
                    dest.write_bytes(r.content)
                    recovered.append(str(dest))
            except Exception:
                pass
        with open(upload_dir / "paths.json", "w", encoding="utf-8") as f:
            json.dump(recovered, f)

    with open(paths_file, encoding="utf-8") as f:
        photo_paths: list[str] = json.load(f)

    if not photo_paths:
        return JSONResponse(status_code=400, content={"error": "no photos found"})

    # ── P2-MVP-0 (C1+C2-back): rooms_json 嚴格解析 + 照片白名單過濾 ────────────
    # 規則：
    #   rooms_json 完全未送 / 空字串       → 舊 flat flow（向下相容 DAF4D135 那種訂單）
    #   rooms_json 非空但 JSON 壞掉        → fail closed（HTTP 400）
    #   rooms_json 不是 list / 空 list     → fail closed
    #   rooms_json 沒有有效 room           → fail closed
    #   primary 沒 photo_keys              → fail closed
    #   primary keys 完全對不上 paths.json → fail closed
    #   至少對上一張                       → 只把對上的傳給 pipeline，其他保存在 rooms[]
    # 核心鐵則：legacy 沒送 rooms_json 才用 flat。一旦明示多空間，絕不退回混合。
    rooms_data: list = []
    primary_room_notes: str = ""
    primary_obj: dict | None = None

    rooms_json_str = (rooms_json or "").strip()
    if rooms_json_str:
        fail_reason: str = ""
        try:
            parsed = json.loads(rooms_json_str)
        except Exception as je:
            fail_reason = f"rooms_json 格式錯誤：{str(je)[:80]}"
        else:
            if not isinstance(parsed, list):
                fail_reason = "rooms_json 必須是陣列"
            elif len(parsed) == 0:
                fail_reason = "rooms_json 為空陣列，至少需要主空間"
            else:
                cleaned: list = []
                for r in parsed:
                    if not isinstance(r, dict):
                        continue
                    rt = (r.get("room_type") or "").strip()
                    if not rt:
                        continue
                    cleaned.append({
                        "room_id":    str(r.get("room_id") or "")[:32],
                        "room_type":  rt[:32],
                        "room_label": str(r.get("room_label") or "")[:32],
                        "is_primary": bool(r.get("is_primary")),
                        "room_notes": str(r.get("room_notes") or "")[:100],
                        "photo_keys": [str(k)[:200] for k in (r.get("photo_keys") or []) if isinstance(k, str)],
                        "video_keys": [str(k)[:200] for k in (r.get("video_keys") or []) if isinstance(k, str)],
                    })
                if not cleaned:
                    fail_reason = "rooms_json 沒有有效的空間資料"
                else:
                    primary = next((r for r in cleaned if r["is_primary"]), cleaned[0])
                    if not primary["photo_keys"]:
                        fail_reason = (f"主空間「{primary['room_label'] or primary['room_type']}」"
                                       f"必須至少上傳一張照片")
                    else:
                        rooms_data = cleaned
                        primary_room_notes = primary["room_notes"]
                        primary_obj = primary
        if fail_reason:
            print(f"[/api/job] FAIL_CLOSED (rooms_json): {fail_reason}")
            return JSONResponse(status_code=400, content={"error": fail_reason})

    # 照片白名單過濾（只在 primary_obj 設好時做）
    if primary_obj is not None:
        primary_canon = {canonical_photo_key(k) for k in primary_obj["photo_keys"]}
        primary_canon.discard("")

        matched: list[str] = []
        excluded_photos: list[str] = []
        excluded_videos: list[str] = []
        for p in photo_paths:
            if not isinstance(p, str):
                continue
            if p.startswith("r2://"):
                # 影片這輪不混進 primary
                # （USE_VIDEO_KEYFRAMES=0；未來 per-room video 才開啟）
                excluded_videos.append(p)
                continue
            if canonical_photo_key(p) in primary_canon:
                matched.append(p)
            else:
                excluded_photos.append(p)

        if not matched:
            msg = "主空間照片資料配對失敗，請重新上傳"
            print(f"[/api/job] FAIL_CLOSED (no match): {msg}  "
                  f"primary_canon_sample={list(primary_canon)[:3]}  "
                  f"paths_canon_sample={[canonical_photo_key(p) for p in photo_paths[:3]]}")
            return JSONResponse(status_code=400, content={"error": msg})

        print(f"[/api/job] rooms_json 分流成功: "
              f"primary={primary_obj['room_label']}({primary_obj['room_type']})  "
              f"primary_keys={len(primary_obj['photo_keys'])}  "
              f"matched={len(matched)}  excluded_photos={len(excluded_photos)}  "
              f"excluded_videos={len(excluded_videos)}")
        photo_paths = matched

    job_id  = uuid.uuid4().hex[:8].upper()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)

    new_paths: list[str] = []
    for path in photo_paths:
        if path.startswith(("gemini://", "supabase://", "r2://")):
            new_paths.append(path)  # 虛擬路徑保留，pipeline 內處理
            continue
        src = Path(path)
        if src.exists():
            dst = job_dir / src.name
            shutil.copy2(src, dst)
            new_paths.append(str(dst))

    styles_list = [s.strip() for s in styles.split(",") if s.strip()]

    # Phase A：欄位 normalize + 後端保險
    if budget_tier not in ("tier1", "tier2", "tier3"):
        budget_tier = "tier3"
    if preferred_store not in ("none", "momo", "ikea", "hola", "trplus"):
        preferred_store = "none"
    customer_notes = (customer_notes or "")[:300]

    # 把 primary_room_notes 拼進 customer_notes（仍走既有 _NOTES_WRAPPER）
    # primary_room_notes 由本函式上面的「rooms_json 嚴格解析 + 照片白名單過濾」block 設定
    # 沒 primary_room_notes 時 = customer_notes 不變 = 舊行為
    if primary_room_notes:
        if customer_notes:
            customer_notes = (customer_notes + "\n房間用途備註：" + primary_room_notes)
        else:
            customer_notes = "房間用途備註：" + primary_room_notes
        customer_notes = customer_notes[:300]  # 沿用既有上限

    with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"job_id": job_id, "plan": plan, "styles": styles_list,
                   "space_type": space_type, "render_angle": render_angle,
                   "design_mode": design_mode,
                   "budget_tier": budget_tier,
                   "preferred_store": preferred_store,
                   "customer_notes": customer_notes,
                   "photo_count": len(new_paths)}, f, ensure_ascii=False)

    # ── P2-MVP-0: 把 rooms[] + primary_room_notes 寫進 side file 給 run_pipeline ──
    # 不改 run_pipeline 簽名；run_pipeline 自己在 sb_upsert 前讀回
    if rooms_data:
        with open(job_dir / "rooms_meta.json", "w", encoding="utf-8") as f:
            json.dump({"rooms": rooms_data,
                       "primary_room_notes": primary_room_notes}, f, ensure_ascii=False)

    sb_upsert({"job_id": job_id, "plan": plan, "styles": styles_list,
               "photo_count": len(new_paths), "status": "queued",
               "progress": 5, "message": "訂單已成立，即將開始解析空間…"})

    # Z2: parse 使用者已確認的 v2 zoning（可選）
    user_zoning_v2 = None
    if zoning_json:
        try:
            parsed = json.loads(zoning_json)
            if isinstance(parsed, dict):
                user_zoning_v2 = parsed
        except Exception as je:
            print(f"[/api/job] zoning_json parse 失敗, 忽略: {je}")

    write_status(job_id, job_dir, "queued", 5, "訂單已成立，即將開始解析空間…")
    background_tasks.add_task(run_pipeline, job_id, new_paths, styles_list, plan,
                              space_type, render_angle, design_mode,
                              user_zoning_v2, layout_choice,
                              budget_tier, customer_notes, preferred_store,
                              upload_id)

    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def get_status(job_id: str):
    # 優先讀 Supabase
    row = sb_get(job_id)
    if row:
        return {"status": row["status"], "progress": row["progress"], "message": row["message"]}
    # fallback: 本機檔案
    status_file = JOBS_DIR / job_id / "status.json"
    if not status_file.exists():
        return JSONResponse(status_code=404, content={"error": "job not found"})
    with open(status_file, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/job/{job_id}/result")
def get_result(job_id: str):
    # 優先讀 Supabase result_json
    row = sb_get(job_id)
    if row and row.get("result_json"):
        result = row["result_json"]
        # render_filename 已在寫入時存好，直接回傳
        return result

    # fallback: 本機 result.json
    result_file = JOBS_DIR / job_id / "result.json"
    if not result_file.exists():
        status_file = JOBS_DIR / job_id / "status.json"
        if status_file.exists():
            with open(status_file, encoding="utf-8") as f:
                st = json.load(f)
            return JSONResponse(status_code=202, content={"error": "result not ready", "status": st})
        return JSONResponse(status_code=404, content={"error": "job not found"})

    with open(result_file, encoding="utf-8") as f:
        result = json.load(f)
    for render in result.get("renders", []):
        path = render.get("render_path", "")
        render["render_filename"] = Path(path).name if path else None
    return result


@app.get("/api/job/{job_id}/error")
def get_error(job_id: str):
    error_file = JOBS_DIR / job_id / "error.log"
    if not error_file.exists():
        return {"error": "no error log"}
    return {"log": error_file.read_text(encoding="utf-8", errors="replace")}


# ── Z2.1: 付款前分區確認用 ────────────────────────────────────────────────
@app.post("/api/zoning")
async def api_zoning(upload_id: str = Form(...)):
    """
    付款前分區確認：讀 upload 紀錄的照片 → Gemini zoning v2 → 產 overlay PNG
    回 v2 zoning JSON + 兩張 overlay public URL，給 zoning-confirm.html 用。
    """
    # 1. 拿 upload 紀錄
    upload = sb_get_upload(upload_id)
    if not upload:
        return JSONResponse(status_code=404, content={"error": "upload_id not found"})

    photo_urls = upload.get("photo_urls") or []
    if not photo_urls:
        return JSONResponse(status_code=400, content={"error": "no photos in this upload"})

    # 2. 下載到本機 temp
    tmp_dir = UPLOADS_DIR / upload_id / "zoning_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    local_photos: list[Path] = []
    for i, url in enumerate(photo_urls[:3]):
        fname = (url.rsplit("/", 1)[-1] or f"photo_{i}.jpg")
        dest = tmp_dir / fname
        if not dest.exists() or dest.stat().st_size < 1024:
            try:
                r = _req.get(
                    url,
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                    timeout=30,
                )
                if r.ok:
                    dest.write_bytes(r.content)
                else:
                    print(f"[/api/zoning] 下載 {url} 失敗 HTTP {r.status_code}")
                    continue
            except Exception as e:
                print(f"[/api/zoning] 下載例外 {url}: {e}")
                continue
        if dest.exists() and dest.stat().st_size > 1024:
            local_photos.append(dest)

    if not local_photos:
        return JSONResponse(status_code=500, content={"error": "failed to download any photo from supabase"})

    # 3. Gemini zoning v2（Phase 1 不傳影片）
    try:
        from zoning_v2 import compute_zoning_v2, draw_overlay
    except ImportError as e:
        return JSONResponse(status_code=500, content={"error": f"zoning module missing: {e}"})

    zoning = compute_zoning_v2(local_photos, video_keyframes=None)
    if zoning.get("error"):
        return JSONResponse(status_code=500, content={"error": f"gemini zoning failed: {zoning['error']}"})

    # 4. 畫 overlay
    best_idx = zoning.get("best_photo_index", 0)
    best_photo = local_photos[best_idx] if 0 <= best_idx < len(local_photos) else local_photos[0]
    existing_path = tmp_dir / "z_overlay_existing.png"
    proposed_path = tmp_dir / "z_overlay_proposed.png"
    try:
        draw_overlay(best_photo, zoning.get("existing_zones", {}),
                     "EXISTING ZONES (AI inferred original use)", existing_path)
        draw_overlay(best_photo, zoning.get("proposed_zones", {}),
                     "PROPOSED ZONES (AI suggested layout)", proposed_path)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"overlay generation failed: {e}"})

    # 5. 上傳 overlay 到 Supabase Storage（renders bucket — 已是 public，回 public URL）
    #    （uploads bucket 不允許 anon SELECT，所以前端 <img> 會 400；改用 renders bucket 就 OK）
    def _upload_overlay(local: Path, name: str) -> str | None:
        try:
            data = local.read_bytes()
            storage_path = f"zoning/{upload_id}/{name}"
            r = _req.post(
                f"{SUPABASE_URL}/storage/v1/object/renders/{storage_path}",
                data=data,
                headers={
                    "apikey":        SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type":  "image/png",
                    "x-upsert":      "true",
                },
                timeout=30,
            )
            if r.status_code in (200, 201):
                return f"{SUPABASE_URL}/storage/v1/object/public/renders/{storage_path}"
            print(f"[/api/zoning] overlay 上傳 {name} 失敗 HTTP {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"[/api/zoning] overlay 上傳 {name} 例外: {e}")
            return None

    overlay_existing_url = _upload_overlay(existing_path, "zoning_overlay_existing.png")
    overlay_proposed_url = _upload_overlay(proposed_path, "zoning_overlay_proposed.png")

    return {
        "upload_id":            upload_id,
        "zoning":               zoning,
        "overlay_existing_url": overlay_existing_url,
        "overlay_proposed_url": overlay_proposed_url,
    }


@app.get("/health")
def health():
    ak, sk, ep, bucket = _r2_cfg()

    # 診斷：實際讀到的 Gemini key 來源與前後碼（不洩漏中間內容）
    g_env = (os.environ.get("GEMINI_API_KEY") or "").strip()
    ga_env = (os.environ.get("GOOGLE_AI_KEY") or "").strip()
    used_key = g_env or ga_env
    used_source = "GEMINI_API_KEY" if g_env else ("GOOGLE_AI_KEY" if ga_env else None)
    if used_key and len(used_key) >= 10:
        key_prefix = used_key[:6]
        key_suffix = used_key[-4:]
        key_len = len(used_key)
    else:
        key_prefix = None
        key_suffix = None
        key_len = len(used_key) if used_key else 0

    return {
        "status": "ok",
        "base_dir": str(BASE_DIR),
        "gemini_key": "set" if used_key else "MISSING",
        "gemini_key_source": used_source,
        "gemini_key_prefix": key_prefix,
        "gemini_key_suffix": key_suffix,
        "gemini_key_len":    key_len,
        "fal_key":    "set" if os.environ.get("FAL_KEY") else "MISSING",
        "r2_access_key": "set" if ak else "MISSING",
        "r2_secret":     "set" if sk else "MISSING",
        "r2_endpoint":   "set" if ep else "MISSING",
        "r2_bucket":     bucket or "MISSING",  # bucket 名稱本來就在 R2 可見，不算 secret
    }

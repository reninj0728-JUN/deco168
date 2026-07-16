# DECO168 FastAPI Backend
# 啟動: cd backend && python3.11 -m uvicorn api:app --reload --port 8000
import os, re, sys, json, uuid, shutil, traceback

# 清除環境變數可能的換行符（Railway 有時會多帶 \n）
for _k in ("FAL_KEY", "GEMINI_API_KEY", "GOOGLE_AI_KEY", "SUPABASE_KEY", "FLUX_API_KEY"):
    if os.environ.get(_k):
        os.environ[_k] = os.environ[_k].strip()
from pathlib import Path
from typing import List
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import requests as _req

SUPABASE_URL = "https://cjezgczjjsxfoeifduaj.supabase.co"
# 優先用 Railway 環境變數的 service_role key（開 RLS 後 anon 會被鎖、只有 service key 能寫）；
# 沒設時退回 anon key（RLS 開啟前的既有行為，部署不中斷）
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip() or \
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNqZXpnY3pqanN4Zm9laWZkdWFqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzk0NjE3NDYsImV4cCI6MjA5NTAzNzc0Nn0.K8zAdT5U3ApWCe4T-noBY5mrseCUSi2-A6Sn8JLU5X4"
_SB_HEADERS  = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

BASE_DIR    = Path(__file__).parent.resolve()
UPLOADS_DIR = BASE_DIR / "uploads"
JOBS_DIR    = BASE_DIR / "jobs"
UPLOADS_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

# ─── R2 (Cloudflare) 設定 ─────────────────────────────────────────────────────
# 只讀 Railway env vars，CF_R2_* 優先，R2_* 為舊版備援

def _r2_cfg():
    return (
        (os.environ.get("CF_R2_ACCESS_KEY_ID")     or os.environ.get("R2_ACCESS_KEY_ID")     or "").strip(),
        (os.environ.get("CF_R2_SECRET_ACCESS_KEY") or os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip(),
        (os.environ.get("CF_R2_ENDPOINT")          or os.environ.get("R2_ENDPOINT")          or "").strip(),
        (os.environ.get("CF_R2_BUCKET")            or os.environ.get("R2_BUCKET")            or "deco168-uploads").strip(),
    )

def _r2_client():
    """惰性建立 R2 boto3 client，每次都即時讀 env vars"""
    import boto3
    ak, sk, ep, _ = _r2_cfg()
    return boto3.client(
        "s3",
        endpoint_url=ep,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        region_name="auto",
    )

def r2_presign_put(key: str, content_type: str = "video/mp4", expires_in: int = 3600) -> str | None:
    ak, sk, ep, bucket = _r2_cfg()
    if not (ak and sk and ep):
        print(f"[r2_presign_put] env vars 缺：ak={bool(ak)} sk={bool(sk)} ep={bool(ep)}")
        return None
    try:
        return _r2_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
    except Exception as e:
        print(f"[r2_presign_put] 失敗: {e}")
        return None

def r2_download_object(key: str, dest: Path) -> str | None:
    ak, sk, ep, bucket = _r2_cfg()
    if not (ak and sk and ep):
        return None
    try:
        _r2_client().download_file(bucket, key, str(dest))
        return str(dest)
    except Exception as e:
        print(f"[r2_download] {key} 失敗: {e}")
        return None

def r2_delete_object(key: str) -> bool:
    ak, sk, ep, bucket = _r2_cfg()
    if not (ak and sk and ep):
        return False
    try:
        _r2_client().delete_object(Bucket=bucket, Key=key)
        return True
    except Exception as e:
        print(f"[r2_delete] {key} 失敗: {e}")
        return False

app = FastAPI(title="DECO168 API", version="1.0.2")

# 啟動時只 print True/False，不洩漏值
print(f"[startup] R2 access_key set: {bool(os.environ.get('CF_R2_ACCESS_KEY_ID') or os.environ.get('R2_ACCESS_KEY_ID'))}")
print(f"[startup] R2 secret set: {bool(os.environ.get('CF_R2_SECRET_ACCESS_KEY') or os.environ.get('R2_SECRET_ACCESS_KEY'))}")
print(f"[startup] R2 endpoint set: {bool(os.environ.get('CF_R2_ENDPOINT') or os.environ.get('R2_ENDPOINT'))}")
print(f"[startup] R2 bucket set: {bool(os.environ.get('CF_R2_BUCKET') or os.environ.get('R2_BUCKET'))}")

# CORS：預設只允許正式前端；未來接自訂網域時在 Railway 設
# ALLOWED_ORIGINS=https://deco168.vercel.app,https://deco168.com（逗號分隔）即可，不用改 code
_allowed_origins = [
    o.strip() for o in
    (os.environ.get("ALLOWED_ORIGINS") or "https://deco168.vercel.app").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /jobs 只准拿渲染圖（render_*.png/jpg）。不能整個目錄掛 StaticFiles：
# job 目錄裡還有客戶原始照片、meta.json、result.json、error.log（含 traceback），
# 拿到 job_id 的任何人（例如客戶分享結果頁連結）都能整包抓走。
_RENDER_FILE_RE = re.compile(r"^render_[A-Za-z0-9_\-]+\.(png|jpe?g|webp)$")

@app.get("/jobs/{job_id}/{filename}")
def serve_render_file(job_id: str, filename: str):
    from fastapi.responses import FileResponse
    if not _RENDER_FILE_RE.match(filename) or "/" in job_id or "\\" in job_id or ".." in job_id:
        return JSONResponse(status_code=404, content={"error": "not found"})
    fpath = JOBS_DIR / job_id / filename
    if not fpath.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(str(fpath))


# ── Watchdog：Railway redeploy 會殺掉 in-process BackgroundTasks，
#    否則被殺的單永遠卡在「處理中」。啟動時掃一次 + get_status 輪詢時懶檢查。 ──
STALE_JOB_MINUTES = 30

def _sweep_stale_jobs() -> int:
    """把非終態、超過 STALE_JOB_MINUTES 沒任何進度更新的單標成 failed。
    進行中的單每個 stage 都會 sb_upsert 更新 updated_at，30 分鐘無更新＝確定死了。"""
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_JOB_MINUTES)).isoformat()
        r = _req.patch(
            f"{SUPABASE_URL}/rest/v1/orders",
            params={"status": "not.in.(completed,failed,incomplete)", "updated_at": f"lt.{cutoff}"},
            json={"status": "failed", "progress": 0,
                  "message": "生成中斷（系統重啟或逾時），請聯絡客服協助重新處理"},
            headers={**_SB_HEADERS, "Prefer": "return=representation"},
            timeout=10,
        )
        if r.ok:
            n = len(r.json()) if r.text else 0
            if n:
                print(f"[watchdog] 啟動掃描：{n} 筆卡死單已標 failed")
            return n
        print(f"[watchdog] 掃描非 2xx: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[watchdog] 掃描失敗（不阻斷啟動）: {type(e).__name__}: {str(e)[:150]}")
    return 0

# ── Storage 保留期自動清理：renders/uploads 超過 STORAGE_RETENTION_DAYS 的檔案
#    自動刪（2026-07 超額 4.3GB 被 Supabase 停權事故的根治）。每次部署啟動時跑，
#    在背景 thread 執行避免拖慢啟動健康檢查。 ──
STORAGE_RETENTION_DAYS = int(os.environ.get("STORAGE_RETENTION_DAYS") or "14")

def _storage_list_prefix(bucket: str, prefix: str) -> list:
    r = _req.post(f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
                  json={"prefix": prefix, "limit": 1000,
                        "sortBy": {"column": "name", "order": "asc"}},
                  headers=_SB_HEADERS, timeout=30)
    return r.json() if r.ok else []

def _storage_walk_old(bucket: str, prefix: str, cutoff, depth: int = 0) -> list[str]:
    from datetime import datetime
    if depth > 4:
        return []
    old: list[str] = []
    for entry in _storage_list_prefix(bucket, prefix):
        name = entry.get("name")
        if not name:
            continue
        full = f"{prefix.rstrip('/')}/{name}" if prefix else name
        if entry.get("id") is None:           # 資料夾 → 遞迴
            old += _storage_walk_old(bucket, full + "/", cutoff, depth + 1)
            continue
        created = entry.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            old.append(full)
    return old

def _purge_expired_storage():
    from datetime import datetime, timedelta, timezone
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=STORAGE_RETENTION_DAYS)
        total = 0
        for bucket in ("renders", "uploads"):
            old = _storage_walk_old(bucket, "", cutoff)
            for i in range(0, len(old), 100):
                chunk = old[i:i + 100]
                r = _req.delete(f"{SUPABASE_URL}/storage/v1/object/{bucket}",
                                json={"prefixes": chunk}, headers=_SB_HEADERS, timeout=60)
                if r.ok:
                    total += len(chunk)
        if total:
            print(f"[storage-cleanup] 已清 {total} 個超過 {STORAGE_RETENTION_DAYS} 天的檔案")
    except Exception as e:
        print(f"[storage-cleanup] 清理失敗（不影響服務）: {type(e).__name__}: {str(e)[:150]}")

@app.on_event("startup")
def _startup_watchdog():
    _sweep_stale_jobs()
    import threading
    threading.Thread(target=_purge_expired_storage, daemon=True).start()


# ─── Supabase helpers ─────────────────────────────────────────────────────────

def sb_upsert(data: dict, timeout: int = 8) -> bool:
    """寫 orders。回傳是否成功（HTTP 2xx）。大 payload（completed result_json）可調高 timeout。"""
    try:
        r = _req.post(f"{SUPABASE_URL}/rest/v1/orders", json=data,
                      headers=_SB_HEADERS, timeout=timeout)
        if r.status_code not in (200, 201, 204):
            # 把真正的錯誤印出來（之前被吞掉，全室大 payload 寫失敗時無從得知原因）
            try:
                _body = r.text[:400]
            except Exception:
                _body = "(no body)"
            print(f"[sb_upsert] 非 2xx：status={r.status_code} body={_body}")
        return r.status_code in (200, 201, 204)
    except Exception as e:
        print(f"[sb_upsert] 例外：{type(e).__name__}: {str(e)[:200]}")
        return False

def sb_get(job_id: str) -> dict | None:
    try:
        r = _req.get(f"{SUPABASE_URL}/rest/v1/orders",
                     params={"job_id": f"eq.{job_id}", "select": "*"},
                     headers=_SB_HEADERS, timeout=8)
        rows = r.json()
        return rows[0] if rows else None
    except Exception:
        return None

def sb_upload_file(upload_id: str, filename: str, data: bytes, content_type: str) -> str | None:
    """照片上傳到 Supabase Storage uploads bucket"""
    try:
        storage_path = f"{upload_id}/{filename}"
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  content_type,
        }
        r = _req.post(
            f"{SUPABASE_URL}/storage/v1/object/uploads/{storage_path}",
            data=data, headers=headers, timeout=60
        )
        if r.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/uploads/{storage_path}"
        return None
    except Exception:
        return None

def sb_save_upload(upload_id: str, photo_urls: list, video_uri: str = "", video_keys: list | None = None):
    """把上傳紀錄存到 Supabase uploads table"""
    try:
        _req.post(
            f"{SUPABASE_URL}/rest/v1/uploads",
            json={"upload_id": upload_id, "photo_urls": photo_urls,
                  "video_uri": video_uri, "video_keys": video_keys or []},
            headers=_SB_HEADERS, timeout=8
        )
    except Exception:
        pass


def _normalize_photo_orientation(path: str) -> None:
    """手機直拍照片常帶 EXIF Orientation（物理像素仍是橫的，靠 tag 標記要轉正）。
    Gemini vision 縮圖 (_downscale_for_vision) 與 fal 渲染輸入都是從這個本機檔案
    讀出後再處理/重新編碼，若不在下載當下轉正，後續 resize/重新編碼會把 tag 弄丟、
    永久留下「橫躺」像素 —— 直拍照片會被誤判方向（沙發左右、分區 bbox 全反）。
    只處理圖片副檔名；失敗（非圖片/PIL 缺席/檔案壞）一律忽略，不擋下載流程。"""
    if Path(path).suffix.lower() in VIDEO_EXTS:
        return
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            orientation = im.getexif().get(0x0112, 1)
            if orientation in (1, None):
                return   # 已經是正的，不用重新編碼
            fixed = ImageOps.exif_transpose(im)
        fixed.convert("RGB").save(path, quality=95)
    except Exception as e:
        print(f"[normalize_orientation] {path} 略過: {type(e).__name__}: {e}")


def sb_download_object(key: str, dest: Path) -> str | None:
    """從 Supabase Storage 下載物件到本機（key 格式：bucket/obj/path）"""
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/{key}"
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        with _req.get(url, headers=headers, timeout=300, stream=True) as r:
            if not r.ok:
                print(f"[sb_download] {key} 失敗 HTTP {r.status_code}")
                return None
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return str(dest)
    except Exception as e:
        print(f"[sb_download] {key} 例外: {e}")
        return None

def sb_get_upload(upload_id: str) -> dict | None:
    """從 Supabase 取回上傳紀錄"""
    try:
        r = _req.get(
            f"{SUPABASE_URL}/rest/v1/uploads",
            params={"upload_id": f"eq.{upload_id}", "select": "*"},
            headers=_SB_HEADERS, timeout=8
        )
        rows = r.json()
        return rows[0] if rows else None
    except Exception:
        return None

def sb_upload_render(job_id: str, file_path: Path) -> str | None:
    """上傳渲染圖到 Supabase Storage，回傳公開 URL"""
    try:
        storage_path = f"{job_id}/{file_path.name}"
        with open(file_path, "rb") as f:
            data = f.read()
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            # 2026-07 起 generate_renders 統一寫 render_*.jpg（省 90% 儲存）；
            # 按副檔名標 MIME，舊 .png 重跑單也正確
            "Content-Type":  "image/png" if file_path.suffix.lower() == ".png" else "image/jpeg",
        }
        r = _req.post(
            f"{SUPABASE_URL}/storage/v1/object/renders/{storage_path}",
            data=data, headers=headers, timeout=30
        )
        if r.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/renders/{storage_path}"
        return None
    except Exception:
        return None


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _entrance_side_from_zoning(zoning: dict | None) -> str:
    """大門左右真相｜bbox 優先，文字只做 fallback。

    bbox 格式為 normalized 0-1000 [ymin, xmin, ymax, xmax]。舊流程只讀
    entrance_position/where 文字，Gemini 文字一飄就左右反轉；這裡把影像座標升為
    單一真相，供 flatten、版面 guide 與 prompt 共用。
    """
    if not isinstance(zoning, dict):
        return ""
    explicit = str(zoning.get("_entrance_side") or "").strip().lower()
    if explicit in ("left", "right", "center"):
        return explicit
    zone_sets = [zoning.get("existing_zones") or {}, zoning.get("zones") or {}]
    entrance = {}
    for zones in zone_sets:
        candidate = zones.get("entrance_zone") or {}
        if candidate:
            entrance = candidate
            break
    bbox = entrance.get("bbox_on_best_photo")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            cx = (float(bbox[1]) + float(bbox[3])) / 2.0
            if cx < 350:
                return "left"
            if cx > 650:
                return "right"
            return "center"
        except (TypeError, ValueError):
            pass
    syn = zoning.get("spatial_synthesis") or {}
    text = str(syn.get("entrance_position") or "") + " " + str(entrance.get("where") or "")
    if "左" in text or "left" in text.lower():
        return "left"
    if "右" in text or "right" in text.lower():
        return "right"
    return ""


def _window_side_from_zoning(zoning: dict | None) -> str:
    """主窗所在側；沒有直接窗或文字不明時回空，不亂猜。"""
    if not isinstance(zoning, dict):
        return ""
    explicit = str(zoning.get("_window_side") or "").strip().lower()
    if explicit in ("left", "right", "front", "back"):
        return explicit
    syn = zoning.get("spatial_synthesis") or {}
    text = str(syn.get("main_window_wall") or "")
    low = text.lower()
    if any(k in text for k in ("無直接", "沒有直接", "無大窗", "沒有大窗")) or "no direct" in low:
        return ""
    if "左" in text or "left" in low:
        return "left"
    if "右" in text or "right" in low:
        return "right"
    if any(k in text for k in ("正前", "前方")) or "front" in low:
        return "front"
    if any(k in text for k in ("後方", "深處", "盡頭")) or "back" in low or "rear" in low:
        return "back"
    return ""


def _preferred_focal_side(zoning: dict | None) -> str:
    """AI 自動配置的 TV／焦點牆｜完整實牆優先，避開主窗與入口側。"""
    z = zoning or {}
    syn = z.get("spatial_synthesis") or {}
    entrance = _entrance_side_from_zoning(z)
    window = _window_side_from_zoning(z)
    # 左右兩側一邊是入口、一邊是主窗時，沒有安全的左右焦點牆；
    # 交回 AI 改找前／後實牆或斜向配置，不硬猜其中一邊。
    if entrance in ("left", "right") and window in ("left", "right") and entrance != window:
        return ""
    # 一側是入口、對側是無開口完整實牆時：【憲法配置】完整牆給沙發當穩定背牆，
    # focal/TV 留在入口側「過門後的實牆段」（TV-門間距由 0.28 門寬閘門把關）。
    # 依據＝用戶裁決庫：接受組全部是此配置（21CCB9AF/1164DFC6/A08E612D，
    # 間距 0.29-0.42）；反向配置（沙發放門牆過門）被 2879173D 明確拒絕
    # （沙發吃掉進門落腳區），且與 _auto_layout_safety_check 相斥——
    # 先前反轉導致「決策選B→守門擋B→保守模式→無引導框→沙發貼門」連鎖（48B75FBF）。
    if entrance in ("left", "right") and window not in ("left", "right"):
        opposite = "right" if entrance == "left" else "left"
        for wall in syn.get("wall_inventory") or []:
            txt = f"{wall.get('name', '')} {wall.get('description', '')}"
            side = "left" if ("左" in txt or "left" in txt.lower()) else (
                "right" if ("右" in txt or "right" in txt.lower()) else "")
            if side == opposite and wall.get("has_opening") is False:
                return entrance
    scores = {"left": 0, "right": 0}
    found = False
    for wall in syn.get("wall_inventory") or []:
        if not isinstance(wall, dict):
            continue
        name = str(wall.get("name") or "")
        low = name.lower()
        side = "left" if ("左" in name or "left" in low) else (
            "right" if ("右" in name or "right" in low) else "")
        if not side:
            continue
        found = True
        scores[side] += 4 if wall.get("has_opening") is False else -4
    for side in ("left", "right"):
        if side == window:
            scores[side] -= 6
        if side == entrance:
            scores[side] -= 2
    if found or any(scores.values()):
        return "right" if scores["right"] >= scores["left"] else "left"
    if entrance == "left":
        return "right"
    if entrance == "right":
        return "left"
    if window == "left":
        return "right"
    if window == "right":
        return "left"
    return "right"


def _room_can_float_sofa(analysis: dict | None, zoning: dict | None) -> bool:
    """只有高信心、單一客廳且真的夠寬時才開放浮置。"""
    a = analysis or {}
    dims = a.get("room_dimensions") or {}
    if str(dims.get("confidence") or "").strip().lower() != "high":
        return False
    space = str(a.get("space_type") or "").strip().lower()
    if not space or any(k in space for k in ("whole", "全室", "整戶", "全屋")):
        return False
    if not any(k in space for k in ("living", "客廳", "起居")):
        return False
    try:
        length = float(dims.get("length_m") or dims.get("estimated_length_m") or 0)
        width = float(dims.get("width_m") or dims.get("estimated_width_m") or 0)
    except (TypeError, ValueError):
        return False
    if length <= 0 or width <= 0:
        return False
    short, long = min(length, width), max(length, width)
    shape = str(((zoning or {}).get("spatial_synthesis") or {}).get("room_shape") or "").lower()
    if any(k in shape for k in ("狹長", "窄", "narrow")):
        return False
    return short >= 4.2 and (length * width) >= 24.0 and (long / short) <= 1.8


def _rects_intersect(a, b) -> bool:
    if not a or not b:
        return False
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _full_frame_3_2_crop_box(W: int, H: int,
                             preserve_bbox: tuple | None = None) -> tuple[int, int, int, int]:
    """只裁不補、精確 3:2；寬圖水平裁切時必須保留指定門 bbox。"""
    if W <= 0 or H <= 0:
        return (0, 0, max(0, W), max(0, H))
    target = 1.5
    if W / H < target:
        need_h = min(H, int(round(W / target)))
        # 窄圖只裁上下，保留完整左右門；優先保留地板與入口門腳。
        return (0, H - need_h, W, H)
    need_w = min(W, int(round(H * target)))
    centered = max(0, (W - need_w) // 2)
    x0 = centered
    if preserve_bbox and len(preserve_bbox) == 4 and need_w < W:
        bx0, _by0, bx1, _by1 = [int(v) for v in preserve_bbox]
        lo = max(0, bx1 - need_w)
        hi = min(W - need_w, bx0)
        if lo <= hi:
            x0 = min(max(centered, lo), hi)
        else:
            x0 = max(0, min(W - need_w, (bx0 + bx1 - need_w) // 2))
    return (x0, 0, x0 + need_w, H)


def _zoning_bbox_matches_source(source_path: str, image_paths: list,
                                 zoning: dict | None) -> bool:
    """zoning bbox 只屬於 best_photo_index 指向的那張原圖。"""
    if not source_path or not image_paths or not isinstance(zoning, dict):
        return False
    idx = zoning.get("best_photo_index")
    if not isinstance(idx, int) or not (0 <= idx < len(image_paths)):
        return False
    try:
        src = os.path.normcase(os.path.abspath(str(source_path)))
        truth = os.path.normcase(os.path.abspath(str(image_paths[idx])))
        return src == truth
    except Exception:
        return str(source_path) == str(image_paths[idx])


def flatten_zoning_v2_to_v1(zoning_v2: dict, layout_choice: str) -> dict:
    """
    Z2: 使用者確認過的 v2 zoning（existing_zones / proposed_zones）攤平成 v1 結構，
    讓既有 prompt_builder._build_layout_section() 不用改。
    layout_choice='B' 時，把 living/dining 對調（用 alt_option）。
    """
    ez = zoning_v2.get("existing_zones") or {}
    pz = zoning_v2.get("proposed_zones") or {}
    pz_living = pz.get("living_zone") or {}

    # sofa_side / tv_side ground truth (2026-06-21): 沙發左右邊由 Gemini 在 zoning 階段決定，
    # render prompt 與 validation 共用同一份，不再各自重猜。方案 B 用 alt_* 對調。
    def _norm_side(s):
        s = str(s or "").strip().lower()
        return s if s in ("left", "right") else ""
    # 「沙發不靠牆」（大客廳設計創意選項）：side 綁定與左右驗收全部關閉，
    # 由 prompt 的 FREE-STANDING SOFA 段接手（走道/焦點牆/客廳區鐵則不放寬）。
    _sofa_free = str(pz_living.get("sofa_side") or "").strip().lower() == "free"
    if layout_choice == "B":
        sofa_side = _norm_side(pz_living.get("alt_sofa_side")) or _norm_side(pz_living.get("sofa_side"))
        tv_side   = _norm_side(pz_living.get("alt_tv_side"))   or _norm_side(pz_living.get("tv_side"))
    else:
        sofa_side = _norm_side(pz_living.get("sofa_side"))
        tv_side   = _norm_side(pz_living.get("tv_side"))
    # tv_side 缺值時用 sofa_side 的對面補上
    if sofa_side and not tv_side:
        tv_side = "right" if sofa_side == "left" else "left"
    sofa_side_confidence = str(pz_living.get("sofa_side_confidence") or "").strip().lower()

    if layout_choice == "B":
        living = {
            "where": pz_living.get("alt_option") or (pz.get("dining_zone") or {}).get("where", ""),
            "why_here": "使用者選擇方案 B（替代佈局）",
            "evidence": "user choice",
            "bbox_on_best_photo": pz_living.get("bbox_on_best_photo"),
        }
        dining = {
            "where": (pz.get("dining_zone") or {}).get("alt_option") or pz_living.get("where", ""),
        }
        sofa_wall_hint = pz_living.get("alt_option") or "the longest solid wall"
    else:
        # 'A' 或空字串都當 A 處理（預設）
        living = {
            "where": pz_living.get("where", ""),
            "why_here": pz_living.get("rationale", ""),
            "evidence": "user-confirmed AI recommendation",
            "bbox_on_best_photo": pz_living.get("bbox_on_best_photo"),
        }
        dining = {
            "where": (pz.get("dining_zone") or {}).get("where", ""),
        }
        sofa_wall_hint = pz_living.get("rationale", "") or living["where"] or "the longest solid wall"

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
            "no_go_zone":    pz.get("no_large_furniture_zone") or {},
        },
        "furniture_placement_rules": {
            "sofa_wall":                "" if _sofa_free else sofa_wall_hint,
            "tv_wall":                  "",
            "sofa_side":                sofa_side,             # "left"/"right"/"" — 共用 ground truth
            "tv_side":                  tv_side,               # sofa_side 的對面
            "sofa_side_confidence":     sofa_side_confidence,  # "high"/"medium"/"low"/""
            "coffee_table_position":    "in front of the sofa, on top of the rug",
            "rug_anchor":               "anchored under the coffee table in the living zone",
            "accent_chair_position":    "",
            "no_large_furniture_zones": no_go,
        },
        "_origin": "user_confirmed_v2",
        "_layout_choice": layout_choice or "A",
        "_zoning_best_photo_index": zoning_v2.get("best_photo_index"),
        "_entrance_side": _entrance_side_from_zoning(zoning_v2),
        "_window_side": _window_side_from_zoning(zoning_v2),
        **({"_sofa_layout": "free"} if _sofa_free else {}),
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


# ─── PhotoMeta v1 vocabulary + normalization ──────────────────────────────────
# 規格簽核版本: PhotoMeta v1 (2026-06-13).
# 本輪 (Step 1) 僅做資料接收 + 驗證 + 退化 + result_json 保存,
# 不改 AI 行為 (analyze_image / prompt_builder / render path / zoning_json 不動).
ZONE_ENUM: tuple[str, ...] = (
    "living", "dining", "walkway", "entrance",
    "kitchen", "bedroom", "study", "balcony", "other",
)
LOCATION_HINT_ENUM: tuple[str, ...] = (
    "rear_near_window", "front_near_entrance",
    "left_side", "right_side", "center", "unspecified",
)
# PhotoMeta v1 Step 2 (補完): 補充說明 target_note 上限 100 字 (前後端都擋一次)
TARGET_NOTE_MAX_LEN = 100
# 從 legacy room_type (single-select per photo) 退化為 v1 Zone
ROOM_TYPE_TO_ZONE: dict[str, str] = {
    "living":          "living",
    "living_room":     "living",
    "dining":          "dining",
    "dining_room":     "dining",
    "bedroom":         "bedroom",
    "study":           "study",
    "study_workspace": "study",
    "other_room":      "other",
}


def _normalize_photo_meta_for_room(room: dict) -> tuple[list[dict], str]:
    """
    PhotoMeta v1 per-room normalization + validation.

    輸入: room dict (含 room_type + photo_keys + 可選 photo_meta).
    回傳: (normalized_photo_meta_list, error_str).
          error_str 非空 → caller 應回 400.

    退化規則 (老 client / 沒傳 photo_meta):
      - photo_contains:       [default_zone]
      - target_zone:          default_zone (from ROOM_TYPE_TO_ZONE)
      - target_location_hint: "unspecified"
      - avoid_zones:          []

    驗證規則:
      - photo_meta 必須是 array
      - 每筆 photo_key 必須屬於 room.photo_keys
      - target_zone 必須包含於 photo_contains
      - avoid_zones 不可包含 target_zone
      - avoid_zones 內每個 zone 必須包含於 photo_contains
      - 所有 Zone 必須在 ZONE_ENUM 內
      - target_location_hint 必須在 LOCATION_HINT_ENUM 內
      - photo_contains 非空

    room.photo_keys 內若 photo_meta 沒涵蓋的 key, 自動補退化值.
    """
    room_type = (room.get("room_type") or "").strip()
    default_zone = ROOM_TYPE_TO_ZONE.get(room_type, "other")
    photo_keys = [str(k) for k in (room.get("photo_keys") or []) if isinstance(k, str)]
    photo_keys_set = set(photo_keys)

    raw_meta = room.get("photo_meta")

    def _degrade(pk: str) -> dict:
        return {
            "photo_key":            pk,
            "photo_contains":       [default_zone],
            "target_zone":          default_zone,
            "target_location_hint": "unspecified",
            "avoid_zones":          [],
            "target_note":          "",
        }

    # 老 client / 缺值 → 全部退化
    if raw_meta is None or raw_meta == []:
        return [_degrade(pk) for pk in photo_keys], ""

    if not isinstance(raw_meta, list):
        return [], "photo_meta 必須是 array"

    out: list[dict] = []
    for i, m in enumerate(raw_meta):
        if not isinstance(m, dict):
            return [], f"photo_meta[{i}] 必須是 object"
        pk = (m.get("photo_key") or "").strip()
        if not pk:
            return [], f"photo_meta[{i}].photo_key 必填"
        if pk not in photo_keys_set:
            return [], (f"photo_meta[{i}].photo_key={pk!r} "
                        f"不屬於該 room.photo_keys")

        # photo_contains
        contains = m.get("photo_contains")
        if contains is None:
            contains = [default_zone]
        if not isinstance(contains, list) or len(contains) == 0:
            return [], f"photo_meta[{i}].photo_contains 必須是非空 array"
        contains = [str(z) for z in contains]
        for z in contains:
            if z not in ZONE_ENUM:
                return [], f"photo_meta[{i}].photo_contains 含非法 Zone: {z!r}"

        # target_zone
        target = m.get("target_zone")
        if target is None:
            target = contains[0]
        target = str(target)
        if target not in ZONE_ENUM:
            return [], f"photo_meta[{i}].target_zone 非法 Zone: {target!r}"
        if target not in contains:
            return [], (f"photo_meta[{i}].target_zone={target!r} "
                        f"必須包含於 photo_contains={contains!r}")

        # target_location_hint
        hint = m.get("target_location_hint")
        if hint is None:
            hint = "unspecified"
        hint = str(hint)
        if hint not in LOCATION_HINT_ENUM:
            return [], f"photo_meta[{i}].target_location_hint 非法: {hint!r}"

        # avoid_zones
        avoid = m.get("avoid_zones")
        if avoid is None:
            avoid = []
        if not isinstance(avoid, list):
            return [], f"photo_meta[{i}].avoid_zones 必須是 array"
        avoid = [str(z) for z in avoid]
        for z in avoid:
            if z not in ZONE_ENUM:
                return [], f"photo_meta[{i}].avoid_zones 含非法 Zone: {z!r}"
            if z not in contains:
                return [], (f"photo_meta[{i}].avoid_zones 含 {z!r}, "
                            f"但 photo_contains 不含")
        if target in avoid:
            return [], (f"photo_meta[{i}].avoid_zones 不可包含 "
                        f"target_zone={target!r}")

        # target_note (PhotoMeta v1 Step 2 補完): optional, ≤100 字.
        # 規格: 結構化欄位優先, target_note 只是補充 — 超過就直接 400 不做 truncate,
        # 避免雜訊進 prompt.
        note_raw = m.get("target_note")
        if note_raw is None:
            note = ""
        else:
            if not isinstance(note_raw, str):
                return [], f"photo_meta[{i}].target_note 必須是字串"
            note = note_raw.strip()
            if len(note) > TARGET_NOTE_MAX_LEN:
                return [], (f"photo_meta[{i}].target_note 超過 "
                            f"{TARGET_NOTE_MAX_LEN} 字 (目前 {len(note)} 字)")

        out.append({
            "photo_key":            pk,
            "photo_contains":       contains,
            "target_zone":          target,
            "target_location_hint": hint,
            "avoid_zones":          avoid,
            "target_note":          note,
        })

    # room.photo_keys 內未被 photo_meta 涵蓋的, 補退化值
    covered = {m["photo_key"] for m in out}
    for pk in photo_keys:
        if pk not in covered:
            out.append(_degrade(pk))

    return out, ""


def _build_photo_meta_list(paths: list, photo_meta_by_key: dict | None) -> list | None:
    """
    PhotoMeta v1 Step 2: 依 paths 順序生成對齊的 photo_meta list.
    每張 path 取 canonical_photo_key 後到 photo_meta_by_key 查詢; 找不到放 None.
    全 None / dict 空 → 回 None (signal 給 analyze_image 完全不注入).
    """
    if not photo_meta_by_key or not paths:
        return None
    out: list = []
    any_hit = False
    for p in paths:
        if not isinstance(p, str):
            out.append(None)
            continue
        ck = canonical_photo_key(p)
        m = photo_meta_by_key.get(ck)
        if m:
            any_hit = True
        out.append(m)
    return out if any_hit else None


def _note_implies_rear_near_window(note: str | None) -> bool:
    """User note can promote an unspecified hint only when clearly window-side."""
    if not isinstance(note, str):
        return False
    s = note.strip().lower()
    if not s:
        return False
    negative_markers = ("不要靠窗", "不靠窗", "不要窗邊", "不在窗邊", "not near window")
    if any(k in s for k in negative_markers):
        return False
    positive_markers = (
        "客廳靠窗", "靠窗做客廳", "客廳窗邊", "窗邊客廳",
        "靠窗那邊是客廳", "靠窗的那空間是客廳",
        "near window", "by the window", "window-side",
    )
    return any(k in s for k in positive_markers)


def _note_implies_dining_middle(note: str | None) -> bool:
    """Common user shorthand: '餐廳中段' means reserve the middle zone for dining."""
    if not isinstance(note, str):
        return False
    s = note.strip().lower()
    if not s:
        return False
    dining_markers = ("餐廳", "用餐", "dining")
    middle_markers = ("中段", "中間", "中央", "中部", "middle", "center", "centre")
    return any(k in s for k in dining_markers) and any(k in s for k in middle_markers)


def _apply_target_note_layout_constraints(zoning: dict | None,
                                          target_note: str | None,
                                          target_zone: str | None,
                                          location_hint: str | None) -> dict | None:
    """
    Turn short natural-language photo notes into the same zoning contract used by
    render + validation. Customers should not need prompt-engineering wording.
    """
    if not isinstance(zoning, dict):
        return zoning
    note = (target_note or "").strip()
    if not note:
        return zoning

    zones = zoning.setdefault("zones", {})
    if not isinstance(zones, dict):
        return zoning
    rules = zoning.setdefault("furniture_placement_rules", {})
    if not isinstance(rules, dict):
        return zoning

    if target_zone == "living" and (
        location_hint == "rear_near_window" or _note_implies_rear_near_window(note)
    ):
        living_zone = zones.setdefault("living_zone", {})
        if isinstance(living_zone, dict):
            where = (living_zone.get("where") or "").strip()
            note_clause = "使用者補充指定：客廳靠窗端／窗邊後段。"
            if note_clause not in where:
                living_zone["where"] = (where + " " + note_clause).strip()

    if _note_implies_dining_middle(note):
        dining_zone = zones.setdefault("dining_zone", {})
        if isinstance(dining_zone, dict):
            where = (dining_zone.get("where") or "").strip()
            note_clause = "使用者補充指定：餐廳位於空間中段。"
            if note_clause not in where:
                dining_zone["where"] = (where + " " + note_clause).strip()

        no_go = rules.get("no_large_furniture_zones")
        if not isinstance(no_go, list):
            no_go = []
        no_go_clause = (
            "空間中段餐廳區需保留給餐桌與通行；沙發、客廳地毯、茶几、電視櫃等"
            "大型客廳家具不得佔用此中段餐廳區。"
        )
        if no_go_clause not in no_go:
            no_go.append(no_go_clause)
        rules["no_large_furniture_zones"] = no_go

    return zoning


def _select_render_photo_meta(photo_meta_by_key: dict | None,
                              image_paths: list,
                              analysis: dict | None) -> tuple[str | None, str | None, str | None, int | None]:
    """
    Pick PhotoMeta for render prompt.

    Baseline: use analysis.best_photo_index. If that photo has no target_note but
    another uploaded photo does, prefer the note-bearing meta. User notes are
    explicit render intent and should not be dropped because Gemini picked a
    different best angle.
    """
    if not photo_meta_by_key or not image_paths or not isinstance(analysis, dict):
        return None, None, None, None

    best_idx = analysis.get("best_photo_index")
    if not isinstance(best_idx, int) or not (0 <= best_idx < len(image_paths)):
        best_idx = 0

    def _meta_for_idx(idx: int) -> dict:
        path = image_paths[idx]
        if not isinstance(path, str):
            return {}
        ck = canonical_photo_key(path)
        direct = (
            photo_meta_by_key.get(ck)
            or photo_meta_by_key.get(path)
            or photo_meta_by_key.get(f"uploads/{ck}")
        )
        if direct:
            return direct

        # image_paths may be local temp paths while photo_meta_by_key uses upload keys.
        # Within one job, filename fallback preserves the user's per-photo note better
        # than dropping PhotoMeta entirely.
        filename = Path(path).name
        if filename:
            for key, meta in photo_meta_by_key.items():
                if not isinstance(meta, dict):
                    continue
                candidates = [key, meta.get("photo_key", "")]
                if any(isinstance(c, str) and Path(c.replace("\\", "/")).name == filename
                       for c in candidates):
                    return meta
        return {}

    selected_idx = best_idx
    selected_meta = _meta_for_idx(best_idx)
    selected_note = (selected_meta.get("target_note") or "").strip()

    if not selected_note:
        noted: list[tuple[int, dict, str]] = []
        for idx, _ in enumerate(image_paths):
            m = _meta_for_idx(idx)
            note = (m.get("target_note") or "").strip()
            if note:
                noted.append((idx, m, note))
        if noted:
            living_noted = [x for x in noted if x[1].get("target_zone") == "living"]
            selected_idx, selected_meta, selected_note = (living_noted or noted)[0]

    target_zone = selected_meta.get("target_zone") or None
    location_hint = selected_meta.get("target_location_hint") or None
    target_note = selected_note or None

    if (
        target_zone == "living"
        and (not location_hint or location_hint == "unspecified")
        and _note_implies_rear_near_window(target_note)
    ):
        location_hint = "rear_near_window"

    return target_zone, location_hint, target_note, selected_idx


# target_zone 是 PhotoMeta 英文 enum；直接映成 step-2 房型，
# 千萬不要丟進 normalize_room_type（它只認中文，"dining" 會被判成 living）。
_ZONE_TO_RT: dict[str, str] = {
    "living": "living", "dining": "dining", "bedroom": "bedroom",
    "study": "study", "kitchen": "dining",
}
_RT_ZH_DISPLAY: dict[str, str] = {
    "living": "客廳", "dining": "餐廳", "bedroom": "主臥室", "study": "書房",
}


def _photo_meta_for_path(path: str, photo_meta_by_key: dict | None) -> dict:
    """把（可能是本機 job_dir 的）image path 對到它的 PhotoMeta。
    先試 canonical / upload-key 直配，再退化用檔名比對（同一 job 內檔名唯一）。"""
    if not photo_meta_by_key or not isinstance(path, str):
        return {}
    ck = canonical_photo_key(path)
    direct = (photo_meta_by_key.get(ck) or photo_meta_by_key.get(path)
              or photo_meta_by_key.get(f"uploads/{ck}"))
    if isinstance(direct, dict):
        return direct
    filename = Path(path).name
    if filename:
        for key, meta in photo_meta_by_key.items():
            if not isinstance(meta, dict):
                continue
            candidates = [key, meta.get("photo_key", "")]
            if any(isinstance(c, str) and Path(c.replace("\\", "/")).name == filename
                   for c in candidates):
                return meta
    return {}


def _score_photo_for_room(meta: dict | None, rt: str) -> int:
    """同房型多張候選時的底圖評分（越高越好）。
    C79C7ECC 根因：舊邏輯 first-wins 永遠拿 photo_01 走廊角當客廳 base，
    忽略 photo_03「客廳靠窗」——難角 + 錯底圖 → 三風格客廳全被保真擋下。"""
    if not isinstance(meta, dict):
        return 0
    score = 0
    note = (meta.get("target_note") or "").strip()
    hint = (meta.get("target_location_hint") or "").strip()
    contains = meta.get("photo_contains") or []
    if not isinstance(contains, list):
        contains = []

    if note:
        score += 40

    if rt == "living":
        # 靠窗／窗邊 note = 最強信號（使用者明確指定客廳主圖意圖）
        if _note_implies_rear_near_window(note) or any(
            k in note for k in ("靠窗", "窗邊", "窗戶", "後段", "深處", "底端", "靠窗端")
        ):
            score += 100
        if hint == "rear_near_window":
            score += 50
        # 純客廳略優於客餐廳合照廣角（合照常是往廚／玄關長軸，結構更難保真）
        if "living" in contains and "dining" not in contains:
            score += 20
        elif "living" in contains:
            score += 5
        # 無 note 的客餐廳合照略降（常是過道角；有 note 的不受罰）
        if not note and "dining" in contains and "living" in contains:
            score -= 10
    else:
        if rt in contains:
            score += 15
        if note:
            score += 10
    return score


def _list_room_photo_candidates(
    image_paths: list,
    photo_meta_by_key: dict | None,
    rt: str,
) -> list[dict]:
    """同房型底圖候選，已按分數由高到低排序。
    每項: {idx, path, score, note}。供選主底圖 + 保真失敗換底圖。"""
    if not image_paths or not photo_meta_by_key:
        return []
    out: list[dict] = []
    for idx, p in enumerate(image_paths):
        meta = _photo_meta_for_path(p, photo_meta_by_key)
        tz = (meta.get("target_zone") or "").strip().lower() if isinstance(meta, dict) else ""
        if _ZONE_TO_RT.get(tz) != rt:
            continue
        sc = _score_photo_for_room(meta if isinstance(meta, dict) else {}, rt)
        note_pv = ((meta.get("target_note") or "") if isinstance(meta, dict) else "")[:40]
        out.append({"idx": idx, "path": p, "score": sc, "note": note_pv})
    out.sort(key=lambda x: (-x["score"], x["idx"]))
    return out


def _should_try_alt_living_base(v: dict | None) -> bool:
    """客廳保真／結構失敗 → 值得換另一張 living 底圖（比同底圖乾抽更穩）。"""
    if not isinstance(v, dict):
        return False
    if v.get("spatial_fidelity_fail"):
        return True
    if v.get("main_window_region_match") is False:
        return True
    if v.get("passage_openings_preserved") is False:
        return True
    if v.get("offframe_room_invaded"):
        return True
    if v.get("windows_changed") or v.get("kitchen_added"):
        return True
    reason = v.get("reason") or ""
    return any(k in reason for k in ("空間保真", "主窗", "走道門洞", "畫面外", "廚房"))


def _switch_entry_to_next_living_base(entry: dict) -> str | None:
    """把 entry 切到下一張尚未用過的 living 備援底圖。成功回新 path，否則 None。
    不改家具／風格，只換結構真相來源——商業上比無限同圖重抽穩。"""
    if not isinstance(entry, dict):
        return None
    if (entry.get("_room_type") or "living") != "living":
        return None
    # AI auto 的門窗／走道 guide 綁在目前底圖；換底圖會把最重要的幾何契約清掉。
    # 保真失敗時寧可沿用同底圖重試，也不准退化成沒有 guide 的自由生成。
    if str(entry.get("_layout_guide_mode") or "").startswith("auto_") and entry.get("_layout_guide"):
        return None
    alts = entry.get("_alt_bases") or []
    used = list(entry.get("_used_bases") or [])
    cur = entry.get("_base_path")
    if cur and cur not in used:
        used.append(cur)
    for p in alts:
        if not p or p in used:
            continue
        if not Path(str(p)).exists():
            continue
        used.append(p)
        entry["_used_bases"] = used
        entry["_base_path"] = p
        entry["_cropped"] = False
        entry["_zone_cropped"] = False
        entry["_crop_note"] = "alt living base after fidelity fail"
        entry["_door_excluded"] = False   # 換回的原圖大門可能在鏡內
        entry["_layout_guide"] = None     # 引導框是畫在原裁切圖上的，換底圖即失效
        entry["_uncropped_base"] = p
        print(f"[pipeline] living 換底圖 → {Path(str(p)).name} (used={len(used)})")
        return p
    entry["_used_bases"] = used
    return None


def _phase3_base_strategies(entry: dict) -> list[tuple[str, str, None]]:
    """Phase3 底圖策略；AI-auto 有 guide 時只能沿用同一底圖。"""
    current = entry.get("_base_path")
    if not current:
        return []
    if (str(entry.get("_layout_guide_mode") or "").startswith("auto_")
            and entry.get("_layout_guide")):
        return [("門感知同底圖修正", current, None)]
    strategies: list[tuple[str, str, None]] = []
    if (entry.get("_room_type") or "living") == "living":
        used = set(entry.get("_used_bases") or [])
        used.add(current)
        for alt in entry.get("_alt_bases") or []:
            if alt and alt not in used and Path(alt).exists():
                strategies.append((f"換客廳底圖:{Path(alt).name}", alt, None))
                used.add(alt)
                if len(strategies) >= 2:
                    break
    uncropped = entry.get("_uncropped_base")
    if uncropped and Path(uncropped).exists():
        strategies.append(("原圖重生", uncropped, None))
    if not strategies:
        strategies.append(("修正重生", current, None))
    return strategies[:3]


def _sofa_alignment_edit_base(validation: dict | None, render: dict | None,
                               room_type: str = "living") -> str | None:
    """沙發對門但結構與 TV 已正確時，回傳上一張 render 做局部位移底圖。"""
    v = validation or {}
    r = render or {}
    if (room_type or "living") != "living":
        return None
    if not (v.get("sofa_facing_entrance_door") is True
            or v.get("focal_anchor_misaligned_with_sofa") is True):
        return None
    if v.get("focal_anchor_past_door_in_depth") is not True:
        return None
    if v.get("camera_axis_preserved") is False or v.get("passage_openings_preserved") is False:
        return None
    if any(v.get(k) for k in (
        "spatial_fidelity_fail", "windows_changed", "walls_changed", "ceiling_changed",
        "floor_changed", "offframe_room_invaded",
    )):
        return None
    rb = v.get("render_bboxes") or {}
    if not rb.get("sofa") or not rb.get("focal_anchor"):
        return None
    path = str(r.get("render_path") or "")
    return path if path and Path(path).exists() else None


def _build_user_regions_whole(image_paths: list, photo_meta_by_key: dict | None) -> list[dict]:
    """全室：以使用者『這張照片主要是』(target_zone) 建 regions，一張照片＝一個房間。
    同房型多張候選時用 _score_photo_for_room 選最佳底圖（不再 first-wins）。
    回傳 [] → 沒有可用標註，交回 Gemini regions。
    修：302D6ED2 重複客廳；C79C7ECC 客廳用錯走廊角 base。"""
    if not image_paths or not photo_meta_by_key:
        return []
    # 收集所有有標註的房型
    rts_seen: list[str] = []
    for idx, p in enumerate(image_paths):
        meta = _photo_meta_for_path(p, photo_meta_by_key)
        tz = (meta.get("target_zone") or "").strip().lower() if isinstance(meta, dict) else ""
        rt = _ZONE_TO_RT.get(tz)
        if rt and rt not in rts_seen:
            rts_seen.append(rt)

    out: list[dict] = []
    for rt in rts_seen:
        lst = _list_room_photo_candidates(image_paths, photo_meta_by_key, rt)
        if not lst:
            continue
        best = lst[0]
        if len(lst) > 1:
            print(f"[pipeline] 全室 {rt} 底圖候選 {len(lst)} 張 → 選 idx={best['idx']} "
                  f"score={best['score']} note={best['note']!r} "
                  f"(candidates={[(c['idx'], c['score']) for c in lst]})")
        out.append({
            "room_type": rt,
            "name": _RT_ZH_DISPLAY.get(rt, rt),
            "best_photo_index": best["idx"],
            # 備援底圖 idx（已排序，不含主選）— pipeline 轉成 path 掛上 entry
            "alt_photo_indices": [c["idx"] for c in lst[1:]],
        })
    # 客廳永遠排第一（結果頁第一個視角＝客廳），其餘餐廳→主臥→書房
    _RT_ORDER = {"living": 0, "dining": 1, "bedroom": 2, "study": 3}
    out.sort(key=lambda r: _RT_ORDER.get(r["room_type"], 9))
    return out


# (i) 廣角裁單房：把多區廣角底圖裁成「該房聚焦視角」，去掉鄰房的門/雜物。
# 保守原則：任何不確定 → 回原圖（最壞＝跟現在一樣，不會更差）。只處理 living/dining
# （會共用廣角合照的房型）；bedroom/study 多為專屬單張，不裁。
_RT_TO_ZONE_KEY = {"living": "living_zone", "dining": "dining_zone"}


def _bbox1000_to_crop_px(bbox1000, W: int, H: int,
                         crop_box: tuple[int, int, int, int]) -> tuple | None:
    """把原圖 normalized bbox 映射到實際裁切圖像素座標。"""
    if not isinstance(bbox1000, (list, tuple)) or len(bbox1000) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = [float(v) for v in bbox1000]
    except (TypeError, ValueError):
        return None
    cx0, cy0, cx1, cy1 = crop_box
    x0 = max(0, min(cx1 - cx0, int(xmin / 1000.0 * W) - cx0))
    y0 = max(0, min(cy1 - cy0, int(ymin / 1000.0 * H) - cy0))
    x1 = max(0, min(cx1 - cx0, int(xmax / 1000.0 * W) - cx0))
    y1 = max(0, min(cy1 - cy0, int(ymax / 1000.0 * H) - cy0))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _crop_full_frame_3_2_base(base_path: str, job_dir, idx: int,
                               entrance_bbox1000=None):
    """free／自動配置專用｜保留入口門證據，只裁成精確 3:2。"""
    try:
        import cv2
        img = cv2.imread(base_path)
        if img is None:
            return base_path, False, "底圖讀取失敗", None
        H, W = img.shape[:2]
        door_px = None
        if isinstance(entrance_bbox1000, (list, tuple)) and len(entrance_bbox1000) == 4:
            try:
                ymin, xmin, ymax, xmax = [float(v) for v in entrance_bbox1000]
                door_px = (
                    int(xmin / 1000.0 * W), int(ymin / 1000.0 * H),
                    int(xmax / 1000.0 * W), int(ymax / 1000.0 * H),
                )
            except (TypeError, ValueError):
                door_px = None
        crop_box = _full_frame_3_2_crop_box(W, H, preserve_bbox=door_px)
        x0, y0, x1, y1 = crop_box
        crop = img[y0:y1, x0:x1]
        out_path = str(Path(job_dir) / f"crop_living_free_{idx:02d}.jpg")
        if crop.size == 0 or not cv2.imwrite(out_path, crop):
            return base_path, False, "free 3:2 裁切寫入失敗", None
        return out_path, True, "free 保留大門精確 3:2", crop_box
    except Exception as e:
        return base_path, False, f"free 裁切例外: {type(e).__name__}", None


def _door_exclusion_limits(W: int, door_x0: int, door_x1: int) -> tuple[int, int]:
    """回測定案（12/18，六客廳全因門邊間距陣亡）：模型對「離門半門寬」的服從率
    只有一два成，調字句到不了商用交付率。根治＝門不入鏡——客廳裁切邊界直接推到
    門框內緣＋半門寬緩衝，可見範圍內放家具「物理上必然過門」，交付圖沒有大門
    就永遠不存在「沙發對門」體感（室內攝影本來就不把大門拍進客廳照）。

    回傳 (允許的最小 x0, 允許的最大 x1)。門在中央（端景門）不處理；
    排除上限吃掉半張圖為止，剩餘不足由呼叫端守門退回原圖。"""
    d_w = max(1, door_x1 - door_x0)
    d_cx = (door_x0 + door_x1) / 2
    # 緩衝取小值：門在前景時像素寬度被透視放大（B525E1E2 的門佔 32% 畫寬，
    # 0.5 門寬緩衝=砍掉半張圖，剩右牆+地板的廢底圖）。0.1 門寬與 3% 畫寬取小，
    # 實測（demo_living_original 4032px、門 0-1290）留 65% 畫面、左右牆皆在。
    pad = min(int(d_w * 0.1), int(W * 0.03))
    if d_cx < W * 0.35:      # 門在左 → 左緣推到門右緣+緩衝
        return (min(door_x1 + pad, int(W * 0.5)), W)
    if d_cx > W * 0.65:      # 門在右
        return (0, max(door_x0 - pad, int(W * 0.5)))
    return (0, W)


def _crop_region_base(base_path: str, room_type: str, job_dir, idx: int) -> tuple[str, bool, str, bool]:
    """回傳 (要用的底圖路徑, 是否有裁切, 沒裁時的具體原因, 大門是否已排除出鏡)。"""
    zone_key = _RT_TO_ZONE_KEY.get(room_type)
    if not zone_key or not base_path:
        return base_path, False, "房型不適用或缺底圖", False
    try:
        import cv2
        from zoning_v2 import compute_zoning_v2
        img = cv2.imread(base_path)
        if img is None:
            return base_path, False, "底圖讀取失敗", False
        H, W = img.shape[:2]
        # 單張重跑 zoning → bbox 必落在這張上（零跨元件對齊風險）
        zres = compute_zoning_v2([Path(base_path)], video_keyframes=None)
        if not isinstance(zres, dict) or zres.get("error"):
            return base_path, False, f"zoning 失敗: {str((zres or {}).get('error'))[:60]}", False
        zones = zres.get("proposed_zones") or {}
        bbox = (zones.get(zone_key) or {}).get("bbox_on_best_photo")
        if not bbox or len(bbox) != 4:
            return base_path, False, f"{zone_key} 無 bbox", False
        ymin, xmin, ymax, xmax = [float(v) for v in bbox]
        fy0, fx0, fy1, fx1 = ymin / 1000.0, xmin / 1000.0, ymax / 1000.0, xmax / 1000.0
        bw, bh = (fx1 - fx0), (fy1 - fy0)
        if bw <= 0 or bh <= 0:
            return base_path, False, f"bbox 退化 ({bw:.2f}x{bh:.2f})", False
        area = bw * bh
        if area < 0.25:   # zone 太小＝不可靠，不裁
            print(f"[pipeline] (i) {room_type} zone 太小 area={area:.2f}，用整張")
            return base_path, False, f"zone 面積 {area:.2f} < 0.25", False
        # 動態外擴：大區小擴(6%)、小區多擴(12%)，保留一點鄰接感又不切到家具。
        # 63B7B5C9 回饋：原本 10%/20% 裁完還剩大半張，客戶感覺不到「特寫」——
        # 收緊外擴讓單房聚焦真的看得出來；面積/比例守門不變，不確定仍回原圖。
        margin = 0.06 if area > 0.50 else 0.12
        fx0 = max(0.0, fx0 - margin); fy0 = max(0.0, fy0 - margin)
        fx1 = min(1.0, fx1 + margin); fy1 = min(1.0, fy1 + margin)
        x0, y0, x1, y1 = int(fx0 * W), int(fy0 * H), int(fx1 * W), int(fy1 * H)
        # 客廳門排除：大門 bbox 在側邊 → 裁切邊界推過門框+半門寬，門不入鏡
        door_excluded = False
        _dlim_x0, _dlim_x1 = 0, W
        if room_type == "living":
            _ez = (zres.get("existing_zones") or {}).get("entrance_zone") or {}
            _dbb = _ez.get("bbox_on_best_photo")
            if _dbb and len(_dbb) == 4:
                _d_x0 = int(float(_dbb[1]) / 1000.0 * W)
                _d_x1 = int(float(_dbb[3]) / 1000.0 * W)
                _dlim_x0, _dlim_x1 = _door_exclusion_limits(W, _d_x0, _d_x1)
                _nx0, _nx1 = max(x0, _dlim_x0), min(x1, _dlim_x1)
                if (_nx0, _nx1) != (x0, x1) and (_nx1 - _nx0) >= W * 0.30:
                    print(f"[pipeline] (i) living 門排除出鏡: x0 {x0}->{_nx0}, x1 {x1}->{_nx1}"
                          f"（門 px {_d_x0}-{_d_x1}）")
                    x0, x1 = _nx0, _nx1
                    door_excluded = True
                elif (_nx0, _nx1) != (x0, x1):
                    print(f"[pipeline] (i) living 門排除後過窄（{_nx1-_nx0}px），放棄排除維持原裁切")
                    _dlim_x0, _dlim_x1 = 0, W
        if (x1 - x0) < W * 0.30 or (y1 - y0) < H * 0.30:
            return base_path, False, f"裁切框過小 ({x1-x0}x{y1-y0} on {W}x{H})", False
        # 比例鎖定（F87A75BB：客廳 zone 裁出 2.3:1 超寬框 → gpt-image-2 auto
        # 跟著輸出 1248x544 怪比例）。目標 3:2，太寬就垂直外擴補高、太高就水平
        # 外擴補寬；原圖不夠補 → 放棄裁切回原圖（最壞=跟沒裁一樣，不會更差）。
        # 比例鎖定 v2（用戶抓漏：1.29 底圖被模型輸出成 1.5，多出的寬度是模型
        # 自己補畫、補畫區恰好蓋到門的位置=失真）。改「只裁不補」：在現有框內
        # 收斂成精確 3:2——太寬置中裁寬（框已在門界內，安全）、太高偏下裁高
        # （多裁天花板、保留家具/地板），模型拿到與輸出同比例底圖=零補邊空間。
        _TARGET_AR = 1.5
        cw, ch = (x1 - x0), (y1 - y0)
        ar = cw / max(1, ch)
        if ar > _TARGET_AR + 0.02:
            need_w = int(ch * _TARGET_AR)
            _cx = (x0 + x1) // 2
            x0 = max(x0, min(_cx - need_w // 2, x1 - need_w))
            x1 = x0 + need_w
        elif ar < _TARGET_AR - 0.02:
            need_h = int(cw / _TARGET_AR)
            _trim = ch - need_h
            y0 = y0 + int(_trim * 0.25)   # 少裁上緣（保留天花板/間照），多裁前景地板
            y1 = y0 + need_h
        if (x1 - x0) < W * 0.28 or (y1 - y0) < H * 0.28:
            print(f"[pipeline] (i) {room_type} 3:2 收斂後過小，用整張")
            return base_path, False, "3:2 收斂後過小", False
        crop = img[y0:y1, x0:x1]
        out_path = str(Path(job_dir) / f"crop_{room_type}_{idx:02d}.jpg")
        if not cv2.imwrite(out_path, crop):
            return base_path, False, "裁切檔寫入失敗", False
        print(f"[pipeline] (i) {room_type} 裁成單房視角 area={area:.2f} margin={margin} "
              f"box=({x0},{y0},{x1},{y1})")
        return out_path, True, "", door_excluded
    except Exception as e:
        print(f"[pipeline] (i) {room_type} 裁切例外，用整張: {e}")
        return base_path, False, f"例外: {type(e).__name__}", False


def _layout_guide_plan(W: int, H: int, sofa_side: str,
                       entrance_side: str = "",
                       entrance_bbox: tuple | None = None,
                       focal_side: str = "",
                       auto_float: bool = False,
                       blocked_rects: list | None = None,
                       living_bbox: tuple | None = None) -> dict:
    """產生可驗證的家具配置；找不到不碰門／走道的矩形就不畫 binding guide。"""
    side = sofa_side if sofa_side in ("left", "right", "free") else "free"
    ent = entrance_side if entrance_side in ("left", "right") else ""
    focal = focal_side if focal_side in ("left", "right") else (
        "right" if ent == "left" else "left" if ent == "right" else "right")
    margin_x = int(W * 0.02)
    door_clear = None
    if entrance_bbox:
        dx0, dy0, dx1, _dy1 = [int(v) for v in entrance_bbox]
        door_w = max(1, dx1 - dx0)
        clear_x0 = dx0 - margin_x
        clear_x1 = dx1 + margin_x
        # TV 與入口同牆時保留完整 entrance bbox 寬。10AAED25 實圖證明，
        # 把禁區縮成半寬雖能讓固定螢幕矩形通過，TV 框卻會浮在中央走道，
        # 並非可驗證的入口側實牆段。沒有牆面 polygon / usable segment 時，
        # 寧可讓 planner 無解並在付費前 fail closed，不得靠縮禁區硬湊 valid。
        # 沙發與入口同牆、門留在沙發背後時，使用者已確認只需過外門框與開門弧；
        # 再多延伸會把 2879173D 的合法沙發位整段吃掉。
        if focal == ent == "left":
            clear_x1 += door_w
        elif focal == ent == "right":
            clear_x0 -= door_w
        door_clear = (
            max(0, clear_x0), max(0, dy0 - int(H * 0.04)),
            min(W, clear_x1), H,
        )
    elif ent == "left":
        door_clear = (0, int(H * 0.28), int(W * 0.30), H)
    elif ent == "right":
        door_clear = (int(W * 0.70), int(H * 0.28), W, H)

    def _ordered(rect):
        if not rect or len(rect) != 4:
            return None
        x0, y0, x1, y1 = [int(v) for v in rect]
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H or x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1, y1)

    blocked = []
    for rect in list(blocked_rects or []):
        clean = _ordered(rect)
        if clean:
            blocked.append(clean)
    is_auto = side == "free"
    # AI-auto 的 sofa/TV 框是牆邊家具「視覺外框」，walkway/no-go 是地面投影。
    # 透視圖中兩者 2D bbox 重疊不等於實體擋路；2879173D 的合法左牆沙發因此被誤殺。
    # auto 只用門框／開門弧與 living-zone 中心約束選位，地面走道仍交給紅區與驗收。
    forbidden = ([door_clear] if door_clear else []) if is_auto else (
        blocked + ([door_clear] if door_clear else []))
    allowed = _ordered(living_bbox)

    def _safe(rect, require_living=False):
        clean = _ordered(rect)
        if not clean or any(_rects_intersect(clean, bad) for bad in forbidden):
            return False
        if require_living and allowed:
            cx = (clean[0] + clean[2]) / 2
            cy = (clean[1] + clean[3]) / 2
            if not (allowed[0] <= cx <= allowed[2] and allowed[1] <= cy <= allowed[3]):
                return False
        return True

    mode = ("auto_float" if auto_float else "auto_compact") if is_auto else "bound"
    preferred = "left" if focal == "right" else "right"
    # focal_side 已由完整牆／門窗資料決定。若該對向找不到安全矩形就略過 guide，
    # 不可在 planner 內偷偷翻邊，否則 prompt 與 guide 會使用兩套配置。
    side_candidates = [preferred] if is_auto else [side]
    if mode == "auto_float":
        sofa_w, sofa_h = 0.25, 0.34
        y_starts = (0.48, 0.12, 0.62)
    elif mode == "auto_compact":
        sofa_w, sofa_h = 0.18, 0.24
        # 2879173D 已接受沙發約在畫面 y-centre 0.60；先試 0.48 起點，
        # 讓 sofa/TV 中心同在 0.60，再退到其他安全帶。
        y_starts = (0.48, 0.36, 0.22, 0.08, 0.70)
    else:
        sofa_w, sofa_h = 0.38, 0.48
        y_starts = (0.38, 0.08)

    sofa = tv = None
    chosen = side_candidates[0]
    for candidate_side in side_candidates:
        sw, sh = int(W * sofa_w), int(H * sofa_h)
        if candidate_side == "left":
            # 門在左時，沙發候選必須從 door_clear 終點之後開始，
            # 不能再從門框旁抽樣。
            min_left = 0.08
            if door_clear and ent == "left":
                min_left = max(min_left, door_clear[2] / max(1, W) + 0.01)
            sx_starts = tuple(x for x in (min_left, 0.32, 0.50) if x + sofa_w <= 0.98)
            if not sx_starts:
                sx_starts = (min(0.70, min_left),)
            tx_starts = (0.72, 0.82, 0.52)
            facing = "right"
        else:
            max_right = 1 - 0.08 - sofa_w
            if door_clear and ent == "right":
                max_right = min(max_right, door_clear[0] / max(1, W) - sofa_w - 0.01)
            sx_starts = tuple(x for x in (max_right, 1 - 0.32 - sofa_w, 1 - 0.50 - sofa_w) if x >= 0.02)
            if not sx_starts:
                sx_starts = (max(0.02, max_right),)
            tx_starts = (0.04, 0.18, 0.28)
            facing = "left"
        for yf in y_starts:
            sy0 = int(H * yf)
            sy1 = min(H, sy0 + sh)
            tv_h = int(H * min(0.26, sofa_h))
            # 沙發與 TV 目標框的垂直中心必須一致，形成真正同一條 cross-axis。
            ty0 = max(0, sy0 + (sh - tv_h) // 2)
            ty1 = min(H, ty0 + tv_h)
            for sxf in sx_starts:
                sx0 = int(W * sxf)
                srect = (sx0, sy0, sx0 + sw, sy1)
                if not _safe(srect, require_living=True):
                    continue
                for txf in tx_starts:
                    tx0 = int(W * txf)
                    trect = (tx0, ty0, min(W, tx0 + int(W * 0.24)), ty1)
                    if _safe(trect) and not _rects_intersect(srect, trect):
                        sofa, tv, chosen = srect, trect, candidate_side
                        break
                if sofa:
                    break
            if sofa:
                break
        if sofa:
            break

    return {
        "valid": bool(sofa and tv),
        "mode": mode,
        "chosen_sofa_side": chosen,
        "sofa_facing": "right" if chosen == "left" else "left",
        "sofa": sofa,
        "tv": tv,
        "door_clear": door_clear,
        "blocked": blocked,
        "keep_clear": None,
        "reason": "" if sofa and tv else "no safe furniture rectangles outside door/walkway/no-go zones",
    }


def _build_layout_guide_image(crop_path: str, job_dir, idx: int, sofa_side: str,
                              entrance_side: str = "",
                              entrance_bbox: tuple | None = None,
                              focal_side: str = "",
                              auto_float: bool = False,
                              blocked_rects: list | None = None,
                              living_bbox: tuple | None = None) -> str | None:
    """在實際渲染底圖上畫可驗證的配置；無安全方案就不輸出 guide。"""
    try:
        import cv2
        import numpy as np
        img = cv2.imread(crop_path)
        if img is None:
            return None
        H, W = img.shape[:2]
        plan = _layout_guide_plan(
            W, H, sofa_side, entrance_side, entrance_bbox,
            focal_side=focal_side, auto_float=auto_float,
            blocked_rects=blocked_rects, living_bbox=living_bbox,
        )
        if not plan.get("valid"):
            # docstring 承諾「無安全方案就不輸出」但先前照樣輸出退化圖——
            # 只剩 ENTRANCE DOOR 箭頭的「引導」等於指著門叫模型看
            # （10AAED25 六連燒的實際輸入）。寧可沒有 guide 也不給反引導。
            print(f"[pipeline] (i) living 版面引導圖略過（無安全配置）: "
                  f"{plan.get('reason', '')[:80]}")
            return None

        def _mark_entrance(rect):
            if not rect:
                return None
            x0, y0, x1, y1 = [int(v) for v in rect]
            red = (50, 50, 230)
            target = ((x0 + x1) // 2, (y0 + y1) // 2)
            label_x = max(20, min(W - int(W * 0.38), x0 + 15))
            label_y = max(int(H * 0.10), y0 - int(H * 0.05))
            cv2.putText(img, "ENTRANCE DOOR", (label_x, label_y),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.8, W / 1500),
                        red, max(3, W // 550), cv2.LINE_AA)
            cv2.arrowedLine(img, (label_x, label_y + 15), target, red,
                            max(4, W // 500), cv2.LINE_AA, tipLength=0.08)
            cv2.circle(img, target, max(10, W // 180), red, max(4, W // 600), cv2.LINE_AA)
            return target

        def _floor_zone(rect, label):
            if not rect:
                return
            x0, y0, x1, y1 = [int(v) for v in rect]
            inset = int((x1 - x0) * 0.28)
            poly = np.array([
                (x0 + inset, y0), (x1 - inset, y0),
                (x1, y1), (x0, y1),
            ], dtype=np.int32)
            red = (50, 50, 230)
            overlay = img.copy()
            cv2.fillPoly(overlay, [poly], red)
            cv2.addWeighted(overlay, 0.22, img, 0.78, 0, img)
            cv2.polylines(img, [poly], True, red, max(5, W // 350), cv2.LINE_AA)
            cv2.putText(img, label, (x0 + inset + 12, y0 + max(45, H // 24)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.7, W / 1700),
                        red, max(3, W // 600), cv2.LINE_AA)

        entrance_point = _mark_entrance(plan["door_clear"])

        def _door_keep_clear(rect):
            # door_clear 由門框 bbox 直接推得（非地面投影），且 planner 已保證
            # sofa/tv 目標框不與它相交——畫成紅色禁區不會與綠/藍框自打架。
            # 標題寫「KEEP ALL RED ZONES EMPTY」卻沒畫紅區＝模型看不見門邊禁區，
            # 電視櫃因此一再貼門（10AAED25 主視角 gap 0 教訓）。
            if not rect:
                return
            x0, y0, x1, y1 = [int(v) for v in rect]
            red = (50, 50, 230)
            overlay = img.copy()
            cv2.rectangle(overlay, (x0, y0), (x1, y1), red, -1)
            cv2.addWeighted(overlay, 0.20, img, 0.80, 0, img)
            cv2.rectangle(img, (x0, y0), (x1, y1), red,
                          max(6, W // 320), cv2.LINE_AA)
            cv2.putText(img, "RED DOOR ZONE - NO FURNITURE",
                        (x0 + 12, min(H - 25, y1 - 30)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.7, W / 1700),
                        red, max(3, W // 600), cv2.LINE_AA)

        _door_keep_clear(plan["door_clear"])
        # auto 的 walkway/no-go bbox 是地面投影，與牆邊家具視覺框會在透視圖上假重疊；
        # 不畫成 binding 紅框，避免同一 guide 同時要求「放這裡」與「這裡禁放」。
        if not str(plan.get("mode") or "").startswith("auto_"):
            for _blocked in plan.get("blocked") or []:
                _floor_zone(_blocked, "ENTRANCE APPROACH / WALKWAY")

        def _target_box(rect, colour, label):
            if not rect:
                return None
            x0, y0, x1, y1 = [int(v) for v in rect]
            overlay = img.copy()
            cv2.rectangle(overlay, (x0, y0), (x1, y1), colour, -1)
            cv2.addWeighted(overlay, 0.16, img, 0.84, 0, img)
            cv2.rectangle(img, (x0, y0), (x1, y1), colour,
                          max(6, W // 320), cv2.LINE_AA)
            cv2.putText(img, label, (x0 + 12, max(35, y0 + 42)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.75, W / 1600),
                        colour, max(3, W // 520), cv2.LINE_AA)
            return ((x0 + x1) // 2, (y0 + y1) // 2)

        # 不能只計算不畫出來：模型必須看到成對的精確位置與共同中心軸。
        sofa_c = _target_box(plan.get("sofa"), (40, 210, 60), "GREEN SOFA TARGET")
        tv_c = _target_box(plan.get("tv"), (230, 110, 40), "BLUE TV / MEDIA-CONSOLE TARGET")
        if sofa_c and tv_c:
            axis_colour = (0, 215, 255)
            cv2.line(img, sofa_c, tv_c, axis_colour,
                     max(5, W // 380), cv2.LINE_AA)
            cv2.circle(img, sofa_c, max(8, W // 220), axis_colour, -1, cv2.LINE_AA)
            cv2.circle(img, tv_c, max(8, W // 220), axis_colour, -1, cv2.LINE_AA)
            mid = ((sofa_c[0] + tv_c[0]) // 2, (sofa_c[1] + tv_c[1]) // 2)
            cv2.putText(img, "BINDING FACE-TO-FACE CENTRELINE",
                        (max(15, mid[0] - int(W * 0.18)), max(45, mid[1] - 18)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.62, W / 1900),
                        axis_colour, max(3, W // 650), cv2.LINE_AA)
        if entrance_point and plan.get("blocked"):
            bx0, by0, bx1, by1 = plan["blocked"][0]
            if entrance_point[0] < W // 2:
                flow_target = (bx0 + int((bx1 - bx0) * 0.12), by1 - int((by1 - by0) * 0.08))
            else:
                flow_target = (bx1 - int((bx1 - bx0) * 0.12), by1 - int((by1 - by0) * 0.08))
            red = (50, 50, 230)
            cv2.arrowedLine(img, entrance_point, flow_target, red,
                            max(4, W // 500), cv2.LINE_AA, tipLength=0.06)
            mid = ((entrance_point[0] + flow_target[0]) // 2,
                   (entrance_point[1] + flow_target[1]) // 2)
            cv2.putText(img, "ENTRY FLOW", (mid[0] + 10, mid[1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.65, W / 1850),
                        red, max(3, W // 650), cv2.LINE_AA)
        cv2.putText(
            img, "CONSTRAINT MAP: KEEP ALL RED ZONES EMPTY",
            (max(20, W // 50), max(55, H // 24)),
            cv2.FONT_HERSHEY_SIMPLEX, max(0.7, W / 1800),
            (50, 50, 230), max(3, W // 600), cv2.LINE_AA,
        )
        out = str(Path(job_dir) / f"guide_living_{idx:02d}.jpg")
        if not cv2.imwrite(out, img):
            return None
        print(f"[pipeline] (i) living 版面引導圖: mode={plan['mode']} "
              f"sofa={plan['chosen_sofa_side']} entrance={entrance_side or 'unknown'} "
              f"→ {Path(out).name}")
        return out
    except Exception as e:
        print(f"[pipeline] (i) 版面引導圖失敗（略過）: {type(e).__name__}: {str(e)[:80]}")
        return None


PAIR_CENTER_TOLERANCE = 25
# 極端錯位門檻（合憲：用戶接受組中心差最高 88,拒絕組有 106/110）
PAIR_CENTER_EXTREME = 95


def _pair_center_delta(validation: dict | None,
                       tolerance: int = PAIR_CENTER_TOLERANCE) -> dict | None:
    """以驗收 bbox 做確定性中心差檢查；座標格式 [ymin,xmin,ymax,xmax] / 0..1000。"""
    if not isinstance(validation, dict):
        return None
    boxes = validation.get("render_bboxes") or {}
    sofa = boxes.get("sofa")
    focal = boxes.get("focal_anchor")
    if not (isinstance(sofa, (list, tuple)) and len(sofa) == 4
            and isinstance(focal, (list, tuple)) and len(focal) == 4):
        return None
    try:
        sy0, sx0, sy1, sx1 = [float(v) for v in sofa]
        fy0, fx0, fy1, fx1 = [float(v) for v in focal]
    except (TypeError, ValueError):
        return None
    if not (0 <= sy0 < sy1 <= 1000 and 0 <= sx0 < sx1 <= 1000
            and 0 <= fy0 < fy1 <= 1000 and 0 <= fx0 < fx1 <= 1000):
        return None
    sofa_cy = (sy0 + sy1) / 2
    focal_cy = (fy0 + fy1) / 2
    raw_delta = sofa_cy - focal_cy
    delta = int(raw_delta + 0.5) if raw_delta >= 0 else int(raw_delta - 0.5)
    if abs(delta) <= int(tolerance):
        return None
    return {
        "delta_y": delta,
        "abs_delta_y": abs(delta),
        "sofa_center_y": round(sofa_cy, 1),
        "focal_center_y": round(focal_cy, 1),
        "sofa_bbox": [sy0, sx0, sy1, sx1],
        "focal_bbox": [fy0, fx0, fy1, fx1],
    }


def _build_pair_alignment_guide_image(base_path: str, job_dir: str, idx: int,
                                      validation: dict | None) -> str | None:
    """依上一張實圖 bbox 畫校正圖：綠框鎖沙發、紅框是舊 TV、藍框是同軸新 TV。"""
    pair = _pair_center_delta(validation)
    if not pair:
        return None
    try:
        import cv2
        img = cv2.imread(base_path)
        if img is None:
            return None
        H, W = img.shape[:2]

        def _px(box):
            y0, x0, y1, x1 = box
            return [int(x0 / 1000 * W), int(y0 / 1000 * H),
                    int(x1 / 1000 * W), int(y1 / 1000 * H)]

        sofa_rect = _px(pair["sofa_bbox"])
        old_tv = _px(pair["focal_bbox"])
        shift_px = int(pair["delta_y"] / 1000 * H)
        target_tv = [old_tv[0], old_tv[1] + shift_px,
                     old_tv[2], old_tv[3] + shift_px]
        if target_tv[1] < 0:
            adjust = -target_tv[1]
            target_tv[1] += adjust
            target_tv[3] += adjust
        if target_tv[3] > H:
            adjust = target_tv[3] - H
            target_tv[1] -= adjust
            target_tv[3] -= adjust

        def _box(rect, colour, label, fill=False):
            x0, y0, x1, y1 = rect
            if fill:
                overlay = img.copy()
                cv2.rectangle(overlay, (x0, y0), (x1, y1), colour, -1)
                cv2.addWeighted(overlay, 0.16, img, 0.84, 0, img)
            cv2.rectangle(img, (x0, y0), (x1, y1), colour,
                          max(6, W // 320), cv2.LINE_AA)
            cv2.putText(img, label, (x0 + 10, max(40, y0 + 38)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.68, W / 1800),
                        colour, max(3, W // 580), cv2.LINE_AA)
            return ((x0 + x1) // 2, (y0 + y1) // 2)

        sofa_c = _box(sofa_rect, (40, 210, 60), "GREEN SOFA TARGET - LOCK", True)
        _box(old_tv, (45, 45, 230), "OLD TV - REMOVE", False)
        tv_c = _box(target_tv, (230, 110, 40), "BLUE TV TARGET - MOVE HERE", True)
        axis = (0, 215, 255)
        cv2.line(img, sofa_c, tv_c, axis, max(5, W // 380), cv2.LINE_AA)
        cv2.circle(img, sofa_c, max(8, W // 220), axis, -1, cv2.LINE_AA)
        cv2.circle(img, tv_c, max(8, W // 220), axis, -1, cv2.LINE_AA)
        cv2.putText(img, "MOVE ONLY TV + CONSOLE; KEEP SOFA FIXED",
                    (max(20, W // 40), max(55, H // 22)),
                    cv2.FONT_HERSHEY_SIMPLEX, max(0.72, W / 1750),
                    axis, max(3, W // 600), cv2.LINE_AA)
        out = str(Path(job_dir) / f"guide_pair_align_{idx:02d}.jpg")
        if not cv2.imwrite(out, img):
            return None
        print(f"[pipeline] pair alignment guide: delta_y={pair['delta_y']} → {Path(out).name}")
        return out
    except Exception as e:
        print(f"[pipeline] pair alignment guide 失敗: {type(e).__name__}: {str(e)[:100]}")
        return None


def _activate_pair_alignment_edit(validation: dict | None, render: dict | None,
                                  entry: dict | None, job_dir: str,
                                  idx: int) -> str | None:
    """中心差超標且房間結構正常時，切換成「上一張成品 + 校正 guide」局部 TV 修正。"""
    v = validation or {}
    r = render or {}
    e = entry or {}
    if (e.get("_room_type") or "living") != "living":
        return None
    if v.get("sofa_facing_entrance_door") is True:
        return None
    if v.get("camera_axis_preserved") is False or v.get("passage_openings_preserved") is False:
        return None
    if any(v.get(k) for k in (
            "walls_changed", "windows_changed", "spatial_fidelity_fail",
            "recessed_space_added", "offframe_room_invaded")):
        return None
    if not _pair_center_delta(v):
        return None
    base = r.get("render_path")
    if not base or not Path(str(base)).exists():
        return None
    guide = _build_pair_alignment_guide_image(str(base), job_dir, idx, v)
    if not guide:
        return None
    e["_layout_guide"] = guide
    e["_layout_guide_mode"] = "pair_alignment"
    return str(base)


def _auto_layout_safety_check(zoning_result: dict | None,
                              sofa_side: str, focal_side: str) -> str:
    """鐵則守門（auto/未綁邊限定）：沙發正對電視櫃、兩者永不對門不對窗。
    回不安全原因字串；空字串=安全。用戶明確綁邊時不呼叫（用戶選擇是法律）。

    - 無安全焦點牆（_preferred_focal_side 回空）→ 保守，不准預設值偷補（Hermes 洞①）
    - 沙發牆=主窗牆 → 沙發背窗，保守
    - 沙發牆=大門牆 → 2879173D 裁決（沙發過門框仍吃落腳區），保守
    """
    if sofa_side != "free":
        return ""
    entrance = _entrance_side_from_zoning(zoning_result)
    window = _window_side_from_zoning(zoning_result)
    sofa_wall = ("left" if focal_side == "right"
                 else "right" if focal_side == "left" else "")
    if not focal_side:
        return "無安全焦點牆（入口與主窗分占兩側或牆面資料不足）"
    if sofa_wall and sofa_wall == window:
        return f"沙發牆({sofa_wall})即主窗牆——沙發不可背窗"
    if sofa_wall and sofa_wall == entrance:
        return f"沙發牆({sofa_wall})即大門牆——依 2879173D 裁決不自動採用"
    return ""


def _guide_sofa_side(zoning_result: dict | None) -> str:
    """明確 left/right 照用戶；free 保持 free；其餘才依門側給舊預設。"""
    z = zoning_result or {}
    if z.get("_sofa_layout") == "free":
        return "free"
    rules = z.get("furniture_placement_rules") or {}
    bound = str(rules.get("sofa_side") or "").strip().lower()
    if bound in ("left", "right"):
        return bound
    entrance_side = _entrance_side_from_zoning(z)
    if entrance_side == "left":
        return "right"
    if entrance_side == "right":
        return "left"
    return "right"


def _zoning_payload_for_layout_contract(
    zoning_result: dict | None,
    user_zoning_v2: dict | None,
) -> dict:
    """把正式 zoning 轉成 Phase0 契約需要的 v2 形狀（不改正式 zoning）。"""
    if isinstance(user_zoning_v2, dict) and (
        user_zoning_v2.get("existing_zones") or user_zoning_v2.get("proposed_zones")
    ):
        return user_zoning_v2
    z = zoning_result or {}
    zones = z.get("zones") or {}
    return {
        "existing_zones": {
            "entrance_zone": zones.get("entrance_zone") or {},
            "walkway": zones.get("walkway") or {},
            "living_zone": zones.get("living_zone") or {},
        },
        "proposed_zones": {
            "living_zone": zones.get("living_zone") or {},
            "no_large_furniture_zone": zones.get("no_go_zone") or {},
        },
        "spatial_synthesis": z.get("spatial_synthesis") or {},
        "overall_confidence": z.get("confidence") or "medium",
    }


def _run_layout_contract_shadow(
    *,
    job_id: str,
    job_dir: Path,
    photo_path: str,
    view_index: int,
    zoning_result: dict | None,
    user_zoning_v2: dict | None,
    analysis: dict | None,
    sofa_mode: str,
    can_float: bool,
) -> dict | None:
    """Shadow mode：只算契約、存檔、回傳摘要。不擋生圖、不改交付。

    LAYOUT_CONTRACT_SHADOW=0 可關。
    """
    if os.environ.get("LAYOUT_CONTRACT_SHADOW", "1").strip() == "0":
        return None
    if not photo_path or not Path(photo_path).exists():
        return {
            "view_index": view_index,
            "status": "skipped",
            "reason": "photo_missing",
            "affects_delivery": False,
        }
    try:
        import _proto_layout_contract as plc
        from PIL import Image
        with Image.open(photo_path) as im:
            W, H = im.size
        payload = _zoning_payload_for_layout_contract(zoning_result, user_zoning_v2)
        # 若上游未來帶 struct_keypoints 就用；沒有則走 walkway fallback
        kp = None
        if isinstance(user_zoning_v2, dict):
            kp = user_zoning_v2.get("struct_keypoints")
        if not kp and isinstance(zoning_result, dict):
            kp = zoning_result.get("struct_keypoints")
        mode = str(sofa_mode or "free").strip().lower()
        if mode not in ("left", "right", "free"):
            mode = "free"
        out_dir = Path(job_dir) / "layout_contract_shadow"
        tag = f"{job_id}_v{view_index:02d}_{mode}"
        contract = plc.build_contract_with_crop(
            payload, W, H,
            struct_keypoints=kp,
            sofa_mode=mode,
            can_float=bool(can_float),
        )
        paths = {}
        try:
            paths = plc.render_overlays(
                photo_path, contract, contract.get("crop") or {},
                str(out_dir), tag,
            )
        except Exception as re:
            print(f"[pipeline] layout_contract shadow overlay 失敗: {type(re).__name__}: {re}")
        slim_candidates = [
            {
                "id": c.get("id"),
                "pass": bool(c.get("pass")),
                "score": c.get("score"),
                "sofa_side": c.get("sofa_side"),
                "tv_side": c.get("tv_side"),
                "fail_reasons": list(c.get("fail_reasons") or []),
                "depth_delta": c.get("depth_delta"),
            }
            for c in (contract.get("candidates") or [])
        ]
        summary = {
            "view_index": view_index,
            "photo": Path(photo_path).name,
            "sofa_mode": mode,
            "can_float": bool(can_float),
            "chosen": contract.get("chosen"),
            "safe_layout": bool(contract.get("safe_layout")),
            "disposition": contract.get("disposition"),
            "door_side": contract.get("door_side"),
            "candidates": slim_candidates,
            "crop_invariants": contract.get("crop_invariants"),
            "notes": list(contract.get("notes") or [])[:12],
            "overlay_paths": {
                "chosen_original": paths.get("chosen_original") if isinstance(paths, dict) else None,
                "chosen_crop": paths.get("chosen_crop") if isinstance(paths, dict) else None,
            },
            "affects_delivery": False,
            "status": "ok",
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        out_json = out_dir / f"contract_{tag}.json"
        # 完整契約另存，result_json 只帶 summary 避免膨脹
        full = dict(contract)
        full["shadow_summary"] = summary
        try:
            out_json.write_text(
                json.dumps(full, ensure_ascii=False, indent=2, default=list),
                encoding="utf-8",
            )
            summary["contract_json"] = str(out_json)
        except Exception as we:
            print(f"[pipeline] layout_contract shadow 寫檔失敗: {we}")
        print(
            f"[pipeline] layout_contract shadow[{view_index}] "
            f"mode={mode} float={bool(can_float)} chosen={summary.get('chosen')} "
            f"safe={summary.get('safe_layout')} disp={summary.get('disposition')} "
            f"(delivery untouched)"
        )
        return summary
    except Exception as e:
        print(f"[pipeline] layout_contract shadow 例外（不阻斷）: {type(e).__name__}: {e}")
        return {
            "view_index": view_index,
            "status": "error",
            "reason": f"{type(e).__name__}: {str(e)[:160]}",
            "affects_delivery": False,
        }


def _product_fidelity_into_layout_ctx(layout_ctx: dict | None, entry_or_render: dict | None) -> dict | None:
    """把清單主沙發座位數寫進 layout_ctx，供 validate_render 產品一致驗收（護城河）。"""
    if not isinstance(entry_or_render, dict):
        return layout_ctx
    sofa_seat = None
    sofa_name = ""
    for it in (entry_or_render.get("matched_furniture") or []):
        if not isinstance(it, dict):
            continue
        if (it.get("category_en") or "") == "sofa":
            sofa_seat = it.get("sofa_seating") or None
            sofa_name = (it.get("name_zh") or "")[:80]
            if not sofa_seat:
                try:
                    from furniture_match import infer_sofa_seating
                    sofa_seat = infer_sofa_seating(
                        it.get("name_zh") or "", it.get("flux_descriptor") or "")
                except Exception:
                    sofa_seat = "unknown"
            break
    # C（50873CF0/B0CDF6A0 書房櫃「圖與清單完全不同」）：把商品清單一併帶給驗收，
    # 做「圖上有沒有大致出現」的可見性檢查。
    # 6F1BFC19 升級（客戶鐵則）：購物清單=渲染圖，**所有房型**的清單主家具全檢，
    # 不再只蓋客廳 4 類——書房收納櫃清單有、圖上畫成第二張書桌，以前完全沒人管。
    # 軟裝建議區（curtain/pillow…獨立區塊）本來就不在 matched_furniture，不受此檢。
    must_products = []
    _seen_cats = set()
    for it in (entry_or_render.get("matched_furniture") or []):
        _cat = (it.get("category_en") or "") if isinstance(it, dict) else ""
        if _cat and _cat not in _seen_cats:  # product_visibility 以 cat 為 key，同類取第一件
            _seen_cats.add(_cat)
            must_products.append({
                "cat": _cat,
                "name": (it.get("name_zh") or "")[:60],
                "desc": (it.get("flux_descriptor") or "")[:120],
            })
    must_products = must_products[:6]  # 防 prompt 膨脹；清單本來就 top 4-5
    # 只有依 living-zone bbox 裁成單房視角，才可讓 C2.4 深度門檻讓位。
    # free 模式的全幅 3:2 只是比例裁切，不代表整張都是客廳區。
    _is_zone_crop = bool(
        entry_or_render.get("_zone_cropped") or entry_or_render.get("zone_cropped"))
    if (not sofa_seat or sofa_seat == "unknown") and not must_products and not _is_zone_crop:
        return layout_ctx
    out = dict(layout_ctx) if isinstance(layout_ctx, dict) else {}
    if _is_zone_crop:
        out["base_is_room_crop"] = True
    if sofa_seat and sofa_seat != "unknown":
        out["expected_sofa_seating"] = sofa_seat
        out["expected_sofa_name"] = sofa_name
    if must_products:
        out["must_products"] = must_products
    return out or layout_ctx


def _is_quota_outage(err: str | None) -> bool:
    """Gemini 額度耗盡/限流（429 RESOURCE_EXHAUSTED）＝判官基礎設施斷線，
    不是這張圖的問題——重畫一百次也沒人能驗，重試純燒 fal 錢。"""
    e = (err or "").lower()
    return ("resource_exhausted" in e or "429" in e
            or "credits are depleted" in e or "quota" in e)


def _fail_closed_validation(v: dict | None, room_type: str) -> dict:
    """B1（B0CDF6A0 根治）：驗證崩潰（ok=None/缺失）不得當通過。
    客廳 → 標 hard_fail 進 Z3/Phase2/Phase3 補生鏈，寧可誤擋不裸奔交付；
    非客廳 → 保留原狀但帶 validation_unavailable 標記（不阻斷，風險較低）。
    正常解析出 ok=true/false 的結果原樣通過。
    額度斷線（429）另掛 validation_outage：交付層照樣擋，但重試鏈跳過
    ——判官斷線時燒 fal 重畫是純浪費（三單回測教訓）。"""
    if isinstance(v, dict) and v.get("ok") is not None:
        if (room_type or "living") == "living":
            # 對齊閘門的合憲形態（31E341CF 用戶裁決復活）：中心差在「中間值」
            # 無分類力（接受 10-88 與拒絕 60-106 重疊,25 門檻曾殺掉接受組 4/5），
            # 但「極端值」有——接受組史上最高 88,拒絕組有 106 與 110。
            # 門檻 95：接受組全放、極端錯位（電視在沙發斜前方掃向門）必擋。
            # 中間值一律只記診斷，交給門距閘門與判官分工。
            pair = _pair_center_delta(v, tolerance=0)  # tolerance=0 → 永遠回量測值
            if pair:
                v = dict(v)
                v["pair_center_delta_y"] = pair["delta_y"]
                if pair["abs_delta_y"] > PAIR_CENTER_EXTREME:
                    v["ok"] = False
                    v["hard_fail"] = True
                    v["focal_anchor_misaligned_with_sofa"] = True
                    _tag = (f"沙發與電視櫃深度錯位達極端值：中心差 {pair['abs_delta_y']}/1000"
                            f"（合憲門檻 {PAIR_CENTER_EXTREME}；用戶接受組史上最高 88）")
                    _prev = (v.get("reason") or "").strip()
                    v["reason"] = f"{_tag}；{_prev}" if _prev and "皆合理" not in _prev else _tag
        return v
    base = dict(v or {})
    base.setdefault("error", "validation crashed")
    base["validation_unavailable"] = True
    if _is_quota_outage(base.get("error")):
        base["validation_outage"] = True
    if (room_type or "living") == "living":
        base["ok"] = False
        base["hard_fail"] = True
        base["reason"] = "[驗證異常] 客廳驗證未完成，保守重生（不裸奔交付）"
    return base


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

    # 硬傷分級 (2026-06-21)：hard_fail 是交付/重生的單一判準。
    # 只有硬傷才重生；純軟傷（深度小偏差、茶几略偏、軟裝不齊）照交付、不重生。
    if validation.get("hard_fail"):
        reason = (validation.get("reason") or "").strip()
        return True, (reason or "hard fail (結構/動線/錯邊/錯區)")
    # 明確位置案但 bbox 量不到客觀深度 → 重試嘗試取得 bbox 再驗（不靠自述直接放行）。
    # 注意：不在 HARD_FAIL_FLAGS，所以持續量不到也不會被交付閘門 drop，只是重試後帶標記交付。
    if validation.get("sofa_depth_unverified"):
        return True, "depth unverified (bbox 缺失，重試以取得客觀量測)"
    # hard_fail=False 但 ok=False（僅軟傷）→ 不重生，直接交付
    if validation.get("ok") is not False:
        return False, ""
    if "hard_fail" in validation and not validation.get("hard_fail"):
        return False, ""

    bad_flags = []
    for k in ("walls_changed", "recessed_space_added", "windows_changed",
              "furniture_blocks_walkway", "sofa_faces_walkway",
              "sofa_outside_living_zone",
              "focal_anchor_misaligned_with_sofa",
              "sofa_back_against_window",
              "sofa_intrudes_walkway",
              "coffee_table_in_walkway"):
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


def write_status(job_id: str, job_dir: Path, status: str, progress: int, message: str):
    # 同步更新 Supabase
    sb_upsert({"job_id": job_id, "status": status, "progress": progress, "message": message})
    # 本機備份（查詢 fallback 用）
    data = {"status": status, "progress": progress, "message": message}
    tmp  = job_dir / "status.tmp.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(job_dir / "status.json")


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
                 upload_id: str = "",
                 palettes: dict | None = None):
    job_dir = JOBS_DIR / job_id
    palettes = palettes or {}   # {style_id: 色系中文名}；使用者選的色盤，注入生成 prompt
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
        from furniture_match import enrich_renders, normalize_room_type
        ROOM_DISPLAY_ZH = {"living": "客廳", "dining": "餐廳", "bedroom": "主臥室", "study": "書房"}

        # PhotoMeta v1 Step 2: 早期讀回 photo_meta_by_key, 後面 analyze + render
        # 都可消費. 沒檔案 / 空 → 空 dict, 等同現況行為.
        photo_meta_by_key_early: dict = {}
        try:
            rms_file_early = job_dir / "rooms_meta.json"
            if rms_file_early.exists():
                with open(rms_file_early, encoding="utf-8") as f:
                    rm_early = json.load(f)
                if isinstance(rm_early, dict) and isinstance(rm_early.get("photo_meta_by_key"), dict):
                    photo_meta_by_key_early = rm_early["photo_meta_by_key"]
        except Exception as me:
            print(f"[pipeline] PhotoMeta v1 early-read 失敗, 忽略: {me}")

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
                    _normalize_photo_orientation(local)
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
                    _normalize_photo_orientation(local)
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
                                     video_path=video_paths[0],
                                     photo_meta_list=_build_photo_meta_list(augmented_paths, photo_meta_by_key_early),
                                     user_notes=customer_notes)
            # 把 augmented_paths 寫回 image_paths 給後續 _resolve_region_base / zoning_photos 使用
            image_paths = augmented_paths
        elif gemini_uris:
            from gemini_analyze import analyze_space
            write_status(job_id, job_dir, "analyzing", 15, "解析影片與照片，理解整體格局…")
            analysis = analyze_space(gemini_uris[0], user_styles=styles or None,
                                     is_uri=True, extra_photos=image_paths or None,
                                     space_type=space_type, user_notes=customer_notes)
        elif video_paths and image_paths:
            # 照片為主、影片為輔（2026-07-08 定案）：照片是渲染底圖與房間標籤
            # (photo_meta) 的載體，一律是主要輸入；影片只是「加分的理解素材」，
            # 由 analyze_image 上傳 Gemini 輔助判斷動線/房間連接/方向，
            # 影片上傳失敗會自動退回純照片模式，不會卡單——影片永遠不是必要條件。
            # （舊版走 analyze_space(影片為主)，照片的房間標籤整批被忽略）
            write_status(job_id, job_dir, "analyzing", 12,
                         f"分析 {len(image_paths)} 張照片 + 影片輔助理解空間…")
            extra = image_paths[1:] if len(image_paths) > 1 else None
            analysis = analyze_image(image_paths[0], styles or None, extra_photos=extra,
                                     space_type=space_type, render_angle=render_angle,
                                     video_path=video_paths[0],
                                     photo_meta_list=_build_photo_meta_list(image_paths, photo_meta_by_key_early),
                                     user_notes=customer_notes)
        elif video_paths:
            # 只有影片、完全沒照片的單：影片是唯一素材，維持 analyze_space 老路徑
            from gemini_analyze import analyze_space
            write_status(job_id, job_dir, "analyzing", 10, "正在解析你的空間影片（大檔案需要幾分鐘）…")
            analysis = analyze_space(video_paths[0], user_styles=styles or None,
                                     extra_photos=None,
                                     space_type=space_type, user_notes=customer_notes)
        else:
            write_status(job_id, job_dir, "analyzing", 15, "理解空間格局中…")
            extra = image_paths[1:] if len(image_paths) > 1 else None
            analysis = analyze_image(image_paths[0], styles or None, extra_photos=extra,
                                     space_type=space_type, render_angle=render_angle,
                                     photo_meta_list=_build_photo_meta_list(image_paths, photo_meta_by_key_early),
                                     user_notes=customer_notes)

        # PhotoMeta v1 Step 2: 抽 target_zone + target_location_hint + target_note,
        # 給後面 generate_renders → build_nano_banana_inputs 用. 預設沿用 best_photo,
        # 但若 best_photo 沒 note、其他同批照片有 target_note, 以有 note 的照片為準;
        # 使用者自由文字是明確 render 意圖, 不能因 best_photo 換角度而遺失.
        #
        # 三個值各自決定 prompt 行為:
        #   - target_zone: 主要設計區域 (UI 預設 'living')
        #   - target_location_hint:
        #       != 'unspecified' → prompt_builder 注入 PHOTO TARGET 段, 鎖位置
        #       == 'unspecified' → 不注入 PHOTO TARGET; 若 target_note 非空, 改走 USER PHOTO
        #         DIRECTIVE 段升格成主要照片理解指引 (見 prompt_builder._build_target_note_section)
        #       若 target_note 明確寫「客廳靠窗 / 靠窗做客廳」, 後端升格為
        #       rear_near_window; 這是使用者文字, 不是 plan A/B 代號 mapping.
        #   - target_note: 補充說明 (≤100 字, optional)
        # photo_meta_by_key_early 空 / 沒對到 / best_idx 不合法 → 三個值都 None, 等於不啟用 PhotoMeta.
        _best_pm_target_zone: str | None = None
        _best_pm_location_hint: str | None = None
        _best_pm_target_note: str | None = None
        _best_pm_idx: int | None = None
        (_best_pm_target_zone,
         _best_pm_location_hint,
         _best_pm_target_note,
         _best_pm_idx) = _select_render_photo_meta(photo_meta_by_key_early, image_paths, analysis)
        if _best_pm_target_zone or _best_pm_location_hint or _best_pm_target_note:
            print(f"[pipeline] PhotoMeta v1 render_meta[{_best_pm_idx}] "
                  f"target_zone={_best_pm_target_zone} "
                  f"target_location_hint={_best_pm_location_hint} "
                  f"target_note={(_best_pm_target_note or '')[:30]!r}")
        # Step 3 dropped (2026-06-19): plan A → rear_near_window 的硬 mapping 已移除.
        # 原因: 'A'/'B' 是 zoning-confirm 頁的方案代號, 不代表「靠窗」語意; 客廳不一定有窗;
        # 硬注入 PHOTO TARGET=BACK/WINDOW-SIDE/DEEP 會在無窗或非靠窗格局誤導 model.
        # 替代: zoning_result.flatten_zoning_v2_to_v1 仍把 layout_choice 帶進 prompt 的
        # LAYOUT 段 (USER-CONFIRMED LAYOUT binding), 提供合理的方位約束; PhotoMeta v1 維持
        # 「用戶有自己填 hint / target_note 才 nudge」的行為.

        # Phase 1: 照片不足以滿足 (space_type, render_angle) 需求 → 早期失敗，不 render
        insufficient = analysis.get("insufficient_photos") if isinstance(analysis, dict) else None
        if insufficient and isinstance(insufficient, dict):
            req = insufficient.get("required")
            found = insufficient.get("found", 0) or 0
            rt = insufficient.get("room_type", space_type)
            # 全室(多房間)優雅降級：找到幾房就生幾房，不整單失敗。
            # 只有單空間缺對應房型、或完全沒有可用空間 (found<1) 才硬失敗。
            degrade_ok = (space_type == "whole" or render_angle == "multi") and found >= 1
            if degrade_ok:
                print(f"[pipeline] insufficient_photos 全室降級：found={found}/{req}，只生 {found} 個空間（不整單失敗）")
            else:
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
        angle_room_types: list[str] = []   # step-2：每個視角對應的標準房型（逐房配家具/prompt用）

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
            # 全室：優先用「使用者每張照片標的房型」(photo_meta target_zone) 建 regions，
            # 一張照片＝一個房間，不讓 Gemini 重猜（修餐廳/書房消失、重複客廳；job 302D6ED2）。
            # 沒有可用標註（老 client）→ 退回 Gemini regions（原行為）。
            if space_type == "whole":
                user_regions = _build_user_regions_whole(image_paths, photo_meta_by_key_early)
                if user_regions:
                    regions = user_regions
                    print(f"[pipeline] 全室 regions 採用使用者照片標註: "
                          f"{[r['room_type'] for r in regions]}")
            # 全室：找到幾房生幾房（房型最多 4 種=客廳/餐廳/主臥/書房，去重後自然封頂）；
            # 單房 multi 維持 3 角度。
            n_cap = 4 if space_type == "whole" else 3
            n_views = min(n_cap, max(1, len(regions))) if space_type == "whole" else 3
            for i in range(n_views):
                region = regions[i] if i < len(regions) else {}
                path, label = _resolve_region_base(region, i)
                if path:
                    flux_bases.append(path)
                    # 全室：user_regions 的 room_type 已是乾淨房型，直接用；
                    # Gemini regions 才需 room_type+名稱「合併」判房型（避免『玄關餐廚區』被誤判客廳）。
                    # 單房多角度：同一房型（= space_type）。
                    if space_type == "whole":
                        rt_raw = str(region.get("room_type") or "").strip()
                        if rt_raw in ("living", "dining", "bedroom", "study"):
                            rt = rt_raw
                        else:
                            rt = normalize_room_type(
                                (rt_raw + " " + str(region.get("name") or "")).strip()
                                or space_type)
                        # 顯示名統一成乾淨房名（客廳/餐廳/主臥/書房）
                        label = ROOM_DISPLAY_ZH.get(rt, label)
                    else:
                        rt = normalize_room_type(space_type)
                    angle_labels.append(label)
                    angle_room_types.append(rt)
        else:
            # single：Gemini 挑最美 1 張
            if image_paths:
                best_idx = analysis.get("best_photo_index")
                if not isinstance(best_idx, int) or not (0 <= best_idx < len(image_paths)):
                    best_idx = 0
                flux_bases.append(image_paths[best_idx])
                angle_labels.append("主視角")
                # 單視角也以該照片的 PhotoMeta target_zone 為房型真相；
                # space_type / rooms.room_type 只在 PhotoMeta 缺席時 fallback。
                _single_rt_source = _best_pm_target_zone or space_type
                angle_room_types.append(normalize_room_type(_single_rt_source))
            elif base_video:
                frame_path = str(job_dir / "frame_main.jpg")
                extract_frame(base_video, frame_path, position=0.5)
                flux_bases.append(frame_path)
                angle_labels.append("主視角")
                angle_room_types.append(normalize_room_type(space_type))

        if not flux_bases:
            raise RuntimeError("沒有可用的照片或影片幀作為渲染基底")

        # (i) 廣角裁單房：若某房(客廳/餐廳)底圖是「多區廣角合照」(photo_contains≥2)，
        # 裁成該房聚焦視角去掉鄰房門/雜物。保守：不確定就用整張(crop_flags=False)。
        # 適用：全室多視角 + 單一空間(客廳/餐廳)。單一空間尤其重要——客戶付錢買
        # 「客廳設計」，給的是客餐廳廣角照，成品必須是客廳特寫，不是原封不動的
        # 廣角照（4C3560A2 回饋：拿到跟上傳一樣的視角會覺得受騙）。
        crop_flags: list[bool] = [False] * len(flux_bases)
        zone_crop_flags: list[bool] = [False] * len(flux_bases)
        # free／自動配置不能把大門裁掉；保留每張實際 crop_box，讓門 bbox 與 guide 同座標。
        crop_source_paths: list[str] = list(flux_bases)
        crop_boxes: list[tuple | None] = [None] * len(flux_bases)
        _early_living = (((user_zoning_v2 or {}).get("proposed_zones") or {}).get("living_zone") or {})
        _early_entrance = (((user_zoning_v2 or {}).get("existing_zones") or {}).get("entrance_zone") or {})
        _early_entrance_bbox = _early_entrance.get("bbox_on_best_photo")
        _free_layout_requested = str(_early_living.get("sofa_side") or "").strip().lower() == "free"
        # 裁切決策軌跡：沒裁時記下「為什麼」，不然只看 cropped=false 無從診斷
        # （8BEAE3AD 查了半天才發現是部署沒跟上，不是守門擋掉）
        crop_notes: list[str] = [""] * len(flux_bases)
        door_excluded_flags: list[bool] = [False] * len(flux_bases)
        # Phase3 自動補生用：記錄裁切前的原圖路徑（index 對齊 flux_bases）
        uncropped_bases: dict[int, str] = {}
        _crop_eligible = (
            (space_type == "whole" and render_angle == "multi")
            or normalize_room_type(space_type) in _RT_TO_ZONE_KEY   # 單一空間: living/dining
        )
        if _crop_eligible:
            for _i in range(len(flux_bases)):
                _rt = angle_room_types[_i]
                if _rt not in _RT_TO_ZONE_KEY:
                    crop_notes[_i] = f"room_type={_rt} 不在裁切適用房型"
                    continue
                _pre_crop_base = flux_bases[_i]
                # free／自動配置：大門是擺位與驗收證據，禁止走「門排除出鏡」。
                # 只做全幅精確 3:2，後續把同一 door bbox 映射到 guide。
                if _rt == "living" and _free_layout_requested:
                    _bbox_source_matches = _zoning_bbox_matches_source(
                        _pre_crop_base, image_paths, user_zoning_v2 or {})
                    if user_zoning_v2 and not _bbox_source_matches:
                        crop_notes[_i] = "AI auto 非 zoning 主視角：保留原圖，不裁門"
                        crop_boxes[_i] = None
                        continue
                    _bbox_for_this_source = (
                        _early_entrance_bbox if _bbox_source_matches else None
                    )
                    _new_base, _did, _why, _crop_box = _crop_full_frame_3_2_base(
                        _pre_crop_base, job_dir, _i,
                        entrance_bbox1000=_bbox_for_this_source,
                    )
                    flux_bases[_i] = _new_base
                    crop_flags[_i] = _did
                    crop_boxes[_i] = _crop_box
                    crop_notes[_i] = _why or "free 保留大門"
                    if _did:
                        uncropped_bases[_i] = _pre_crop_base
                    continue
                _meta = _photo_meta_for_path(flux_bases[_i], photo_meta_by_key_early)
                _contains = _meta.get("photo_contains") if isinstance(_meta, dict) else None
                if not (isinstance(_contains, list) and len(_contains) >= 2):
                    crop_notes[_i] = f"photo_contains={_contains} 非多區廣角照"
                    continue   # 專屬單房照片，不裁
                _new_base, _did, _why, _door_ex = _crop_region_base(flux_bases[_i], _rt, job_dir, _i)
                flux_bases[_i] = _new_base
                crop_flags[_i] = _did
                zone_crop_flags[_i] = _did
                if _did:
                    uncropped_bases[_i] = _pre_crop_base
                if _door_ex:
                    door_excluded_flags[_i] = True
                if not _did:
                    crop_notes[_i] = _why or "守門未過"
        else:
            crop_notes = [f"space_type={space_type} 不適用裁切"] * len(flux_bases)

        print(f"[pipeline] 渲染基底 {len(flux_bases)} 張：{list(zip(angle_labels, [Path(p).name for p in flux_bases]))} "
              f"cropped={crop_flags} notes={crop_notes}")

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
        zoning_result = _apply_target_note_layout_constraints(
            zoning_result,
            _best_pm_target_note,
            _best_pm_target_zone,
            _best_pm_location_hint,
        )
        if zoning_result.get("_sofa_layout") == "free":
            zoning_result["_auto_focal_side"] = _preferred_focal_side(zoning_result)
            zoning_result["_auto_can_float"] = _room_can_float_sofa(analysis, zoning_result)

        failed_stage = "matching"
        last_progress = 45
        write_status(job_id, job_dir, "matching", 45, "搭配風格家具中…")
        # step-2：逐房型各配一次家具（不同房間用不同必備品；客廳/單空間行為不變）。
        # angle_room_types 已標好每個視角的標準房型；同房型只配一次再複用。
        renders_in = analysis.get("renders", [])
        distinct_rts = list(dict.fromkeys(angle_room_types)) or ["living"]
        enriched_by_rt = {
            rt: enrich_renders(renders_in, analysis=analysis,
                               budget_tier=budget_tier,
                               preferred_store=preferred_store,
                               room_type=rt,
                               palettes=palettes)
            for rt in distinct_rts
        }
        n_styles = len(enriched_by_rt[distinct_rts[0]]) if distinct_rts else 0
        print(f"[pipeline] 逐房型配對 room_types={distinct_rts} styles={n_styles}")

        # 客廳備援底圖（分數次高的 living 照片 path 列表）— 保真失敗時換底圖再抽
        _living_alt_paths: list[str] = []
        if photo_meta_by_key_early and image_paths:
            _lcands = _list_room_photo_candidates(
                image_paths, photo_meta_by_key_early, "living")
            # 主選之後的 path；path 須存在
            if _lcands:
                _primary_living = None
                for vi, rt0 in enumerate(angle_room_types):
                    if rt0 == "living" and vi < len(flux_bases):
                        _primary_living = flux_bases[vi]
                        break
                for c in _lcands:
                    pth = c["path"]
                    if _primary_living and Path(pth).resolve() == Path(_primary_living).resolve():
                        continue
                    if Path(pth).exists():
                        _living_alt_paths.append(pth)
                if _living_alt_paths:
                    print(f"[pipeline] living 備援底圖 {len(_living_alt_paths)} 張: "
                          f"{[Path(p).name for p in _living_alt_paths]}")

        # 版面引導圖：free 保持 free，門 bbox／門側用同一份 zoning 真相。
        _sofa_side_for_guide = _guide_sofa_side(zoning_result)
        _entrance_side_for_guide = _entrance_side_from_zoning(zoning_result)
        _focal_side_for_guide = _preferred_focal_side(zoning_result)
        _auto_float_for_guide = (
            _sofa_side_for_guide == "free" and _room_can_float_sofa(analysis, zoning_result)
        )
        # ── 鐵則守門（用戶最終目標：沙發正對電視櫃；沙發/電視櫃永不對門、不對窗）──
        # auto（未綁邊）時逐項驗證，任何一項不安全 → 保守模式：不畫 binding guide、
        # 不硬猜。用戶明確綁邊 = 法律，照舊不動。
        _conservative_layout_reason = _auto_layout_safety_check(
            zoning_result, _sofa_side_for_guide, _focal_side_for_guide)
        if _conservative_layout_reason:
            print(f"[pipeline] living 佈局保守模式：{_conservative_layout_reason}"
                  "——不畫 binding guide，交由保守文字合約+驗收閘門把關")
            # 決策是唯一主人：prompt 的 auto 分支也必須跟著轉保守，
            # 不得再用文字指示被裁決否決的配置（沙發上門牆等）
            if isinstance(zoning_result, dict):
                zoning_result["_layout_conservative"] = _conservative_layout_reason
        _entrance_zone_for_guide = ((zoning_result.get("zones") or {}).get("entrance_zone") or {})
        _entrance_bbox_1000 = _entrance_zone_for_guide.get("bbox_on_best_photo")
        layout_guide_paths: dict[int, str | None] = {}
        layout_guide_modes: dict[int, str] = {}
        for _vi, (_bp, _rt) in enumerate(zip(flux_bases, angle_room_types)):
            if _rt == "living" and os.environ.get("LAYOUT_GUIDE", "1").strip() != "0":
                if _conservative_layout_reason:
                    layout_guide_paths[_vi] = None
                    layout_guide_modes[_vi] = "conservative_no_binding"
                    continue
                layout_guide_modes[_vi] = (
                    "auto_float" if _sofa_side_for_guide == "free" and _auto_float_for_guide
                    else "auto_constraints" if _sofa_side_for_guide == "free"
                    else "bound_constraints"
                )
                if zone_crop_flags[_vi]:
                    print(f"[pipeline] guide[{_vi}] 略過：zone crop 尚無可驗證座標轉換")
                    layout_guide_paths[_vi] = None
                    continue
                _source_matches_zoning = _zoning_bbox_matches_source(
                    crop_source_paths[_vi], image_paths, user_zoning_v2 or {})
                if user_zoning_v2 and not _source_matches_zoning:
                    print(f"[pipeline] guide[{_vi}] 略過：底圖不是 zoning 主視角，禁止跨照片套 bbox")
                    layout_guide_paths[_vi] = None
                    continue
                _door_bbox_crop = None
                _blocked_crop: list[tuple] = []
                _living_bbox_crop = None
                try:
                    import cv2
                    _src_img = cv2.imread(crop_source_paths[_vi])
                    if _src_img is not None:
                        _oh, _ow = _src_img.shape[:2]
                        _cb = crop_boxes[_vi] or (0, 0, _ow, _oh)
                        if _entrance_bbox_1000 and not door_excluded_flags[_vi]:
                            _door_bbox_crop = _bbox1000_to_crop_px(
                                _entrance_bbox_1000, _ow, _oh, _cb)
                        _zones = zoning_result.get("zones") or {}
                        for _zk in ("walkway", "no_go_zone"):
                            _bb = ((_zones.get(_zk) or {}).get("bbox_on_best_photo"))
                            _mapped = _bbox1000_to_crop_px(_bb, _ow, _oh, _cb) if _bb else None
                            if _mapped:
                                if (_zk == "no_go_zone" and _door_bbox_crop
                                        and _rects_intersect(_mapped, _door_bbox_crop)):
                                    continue
                                _blocked_crop.append(_mapped)
                        _lbb = ((_zones.get("living_zone") or {}).get("bbox_on_best_photo"))
                        if _lbb:
                            _living_bbox_crop = _bbox1000_to_crop_px(_lbb, _ow, _oh, _cb)
                except Exception as _map_err:
                    print(f"[pipeline] zoning bbox→guide 映射失敗: {_map_err}")
                    layout_guide_paths[_vi] = None
                    continue
                layout_guide_paths[_vi] = _build_layout_guide_image(
                    _bp, job_dir, _vi, _sofa_side_for_guide,
                    entrance_side=_entrance_side_for_guide,
                    entrance_bbox=_door_bbox_crop,
                    focal_side=_focal_side_for_guide,
                    auto_float=_auto_float_for_guide,
                    blocked_rects=_blocked_crop,
                    living_bbox=_living_bbox_crop,
                )

        # ── Phase0 格局契約 shadow（只記錄，不擋生圖、不改交付）──
        layout_contract_shadows: list[dict] = []
        try:
            _sofa_mode_shadow = _guide_sofa_side(zoning_result)
            _can_float_shadow = bool(
                zoning_result.get("_auto_can_float")
                if isinstance(zoning_result, dict) and "_auto_can_float" in zoning_result
                else (
                    _sofa_mode_shadow == "free"
                    and _room_can_float_sofa(analysis, zoning_result)
                )
            )
            for _vi, (_bp, _rt) in enumerate(zip(flux_bases, angle_room_types)):
                if _rt != "living":
                    continue
                # 優先用裁切前原圖做契約（門／牆腳完整）；沒有就用當前底圖
                _shadow_photo = uncropped_bases.get(_vi) or crop_source_paths[_vi] or _bp
                # 照片來源綁定（Hermes 洞③）：zoning bbox 只屬於 best_photo 那張，
                # 不得跨照片套座標——guide 路徑已有此護欄，shadow 補上同一條。
                if user_zoning_v2 and not _zoning_bbox_matches_source(
                        _shadow_photo, image_paths, user_zoning_v2 or {}):
                    layout_contract_shadows.append({
                        "view_index": _vi, "status": "skipped",
                        "reason": "photo_not_zoning_best_photo",
                        "affects_delivery": False,
                    })
                    continue
                _sum = _run_layout_contract_shadow(
                    job_id=job_id,
                    job_dir=job_dir,
                    photo_path=_shadow_photo,
                    view_index=_vi,
                    zoning_result=zoning_result,
                    user_zoning_v2=user_zoning_v2,
                    analysis=analysis,
                    sofa_mode=_sofa_mode_shadow,
                    can_float=_can_float_shadow,
                )
                if _sum:
                    layout_contract_shadows.append(_sum)
        except Exception as _shadow_err:
            print(f"[pipeline] layout_contract shadow 批次例外（不阻斷）: {_shadow_err}")
            layout_contract_shadows = []

        # ── 風格 × 視角(房間) = 多張渲染；每張用「該房間房型」配出的家具 ──
        expanded: list[dict] = []
        for si in range(n_styles):
            for vi, (base, label, rt, cropped, zone_cropped, cnote) in enumerate(zip(
                    flux_bases, angle_labels, angle_room_types,
                    crop_flags, zone_crop_flags, crop_notes)):
                copy = dict(enriched_by_rt[rt][si])
                copy["_angle_label"] = label
                copy["_base_path"] = base
                copy["_room_type"] = rt
                copy["_cropped"] = cropped
                copy["_zone_cropped"] = zone_cropped  # 只有真 living-zone 裁切才可放寬深度驗收
                copy["_crop_note"] = cnote   # 沒裁時的原因（診斷用）
                copy["_door_excluded"] = bool(door_excluded_flags[vi])  # 大門已裁出鏡
                copy["_layout_guide"] = layout_guide_paths.get(vi)      # 版面引導參考圖
                copy["_layout_guide_mode"] = layout_guide_modes.get(vi, "bound")
                copy["_uncropped_base"] = uncropped_bases.get(vi)  # Phase3 補生退回原圖用
                copy["_palette"] = palettes.get(copy.get("style") or "")  # 使用者選的色系→注入 prompt
                if rt == "living" and _living_alt_paths:
                    copy["_alt_bases"] = list(_living_alt_paths)
                    copy["_used_bases"] = [base]
                expanded.append(copy)

        total = len(expanded)
        write_status(job_id, job_dir, "rendering", 60,
                     f"生成 {total} 張設計提案中（{n_styles} 風格 × {len(flux_bases)} 視角）…")

        failed_stage = "render_main"
        last_progress = 60
        # 跨房一致性（343FFAE7 回饋：餐廳照背景拍得到客廳共享牆，各畫各的會穿幫）：
        # 同風格的客廳先生成（expanded 依 living→dining 排序），成品圖掛給餐廳當
        # 背景一致性參考。CROSS_ROOM_CONSISTENCY=0 可關（免部署開關）。
        _cross_room_on = os.environ.get("CROSS_ROOM_CONSISTENCY", "1").strip() != "0"
        _living_render_by_style: dict = {}
        # 一次渲染一張：對應 base 不同（analysis + design_mode 傳進去）
        final = []
        for idx, entry in enumerate(expanded):
            if (_cross_room_on and entry.get("_room_type") == "dining"
                    and _living_render_by_style.get(entry.get("style"))):
                entry["_consistency_ref_path"] = _living_render_by_style[entry.get("style")]
            try:
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
                                             stage="initial",
                                             target_zone=_best_pm_target_zone,
                                             target_location_hint=_best_pm_location_hint,
                                             target_note=_best_pm_target_note,
                                             room_type=entry.get("_room_type", "living"))
            except (FalGenerationTimeout, FalResultDownloadError) as _fe:
                # 單張 fal 超時/下載失敗 → 只丟這張，其餘照常交付（部分交付）。
                # 以前這裡沒接，一張掛掉整單 8 張全失敗（job 65BDC60C）。
                print(f"[pipeline] render[{idx}] style={entry.get('style')} fal 失敗，跳過該張: "
                      f"{type(_fe).__name__}")
                final.append({**entry, "render_path": None,
                              "error": str(_fe)[:200], "error_type": type(_fe).__name__,
                              "angle_label": entry.get("_angle_label", "主視角"),
                              "room_type": entry.get("_room_type", "living"),
                              "cropped": bool(entry.get("_cropped")),
                              "door_excluded": bool(entry.get("_door_excluded"))})
                continue
            if single_result:
                r = single_result[0]
                r["angle_label"] = entry["_angle_label"]
                r["room_type"] = entry.get("_room_type", "living")
                r["cropped"] = bool(entry.get("_cropped"))   # (i) 標記：此圖底圖已裁成單房視角
                r["door_excluded"] = bool(entry.get("_door_excluded"))  # 大門在鏡頭外（前端誠實揭露）
                r["crop_note"] = entry.get("_crop_note") or None   # 沒裁的原因（診斷）
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
                # 客廳成品記下來 → 同風格餐廳的跨房一致性參考
                if (entry.get("_room_type") == "living" and r.get("render_path")
                        and not r.get("error")):
                    _living_render_by_style[entry.get("style")] = r["render_path"]
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
            syn = zr.get("spatial_synthesis") or {}
            return {
                "layout_choice":            zr.get("_layout_choice") or "A",
                "room_shape":               syn.get("room_shape", ""),
                "main_window_wall":          syn.get("main_window_wall", ""),
                "entrance_side":             zr.get("_entrance_side", ""),
                "window_side":               zr.get("_window_side", ""),
                "auto_layout":               zr.get("_sofa_layout") == "free",
                "auto_can_float":            bool(zr.get("_auto_can_float")),
                "auto_focal_side":           zr.get("_auto_focal_side", ""),
                "living_where":             living,
                "living_bbox":              (zones.get("living_zone") or {}).get("bbox_on_best_photo"),
                "sofa_wall_rule":           rules.get("sofa_wall", ""),
                "sofa_side":                rules.get("sofa_side", ""),
                "tv_side":                  rules.get("tv_side", ""),
                "walkway":                  walkway,
                "no_large_furniture_zones": rules.get("no_large_furniture_zones", []),
                "target_zone":              _best_pm_target_zone or "",
                "target_location_hint":     _best_pm_location_hint or "",
                "target_note":              _best_pm_target_note or "",
            }

        layout_ctx = _build_layout_ctx(zoning_result)

        try:
            from gemini_analyze import validate_render
            for r in final:
                bpath = r.get("_base_path") or ""
                rpath = r.get("render_path") or ""
                if bpath and rpath and Path(bpath).exists() and Path(rpath).exists():
                    # 非客廳房型不傳 living 的 layout_context（sofa_side/living_where），
                    # 否則 judge 會被問沙發 → 餐廳/書房 reason 冒沙發語言 → 髒重試(Grok 根治)。
                    _lc = layout_ctx if (r.get("room_type") or "living") == "living" else None
                    _lc = _product_fidelity_into_layout_ctx(_lc, r)
                    # B1（B0CDF6A0 根治）：驗證崩潰不得裸奔——當場重驗一次；仍崩 →
                    # _fail_closed_validation 把客廳標 hard_fail 進補生，非客廳保留標記。
                    v = None
                    for _v_try in range(2):
                        try:
                            v = validate_render(bpath, rpath, r.get("_angle_label", ""),
                                                layout_context=_lc,
                                                room_type=r.get("room_type", "living"),
                                                design_mode=design_mode)
                            break
                        except Exception as ve:
                            v = {"ok": None, "error": str(ve)[:200]}
                            print(f"[pipeline] 驗證崩潰（第 {_v_try+1} 次）"
                                  f"{r.get('style')}/{r.get('room_type')}: {str(ve)[:100]}")
                else:
                    v = {"ok": None, "error": "missing base or render path"}
                r["validation"] = _fail_closed_validation(v, r.get("room_type", "living"))
        except Exception as outer:
            print(f"[pipeline] 驗證階段例外: {outer}")
            for r in final:
                r.setdefault("validation", _fail_closed_validation(
                    {"ok": None, "error": "validation step crashed"},
                    r.get("room_type", "living")))

        # ── Z3: 結構失敗自動重試 1 次（僅 Nano Banana）──
        use_nano = os.environ.get("USE_NANO_BANANA", "0").strip() == "1"
        retry_n = 0
        # C2.3：高嚴重度 layout flag → 允許第 2 次 retry。一般 fail 維持 1 次。
        # 每張 render 最多 retry 2 次（總共 3 次生成）
        HIGH_SEVERITY_FLAGS = (
            "sofa_outside_living_zone",
            "focal_anchor_misaligned_with_sofa",
            "sofa_back_against_window",
            "sofa_intrudes_walkway",
            "coffee_table_in_walkway",
            "furniture_blocks_walkway",
            "furniture_blocks_door",     # F87A75BB：電視櫃擋大門
            "sofa_faces_walkway",
            "sofa_on_wrong_side",
            "spatial_fidelity_fail",     # 2A520C25：整間房被重畫成別的空間
            "product_sofa_seating_mismatch",  # 1FC382CA：清單單人圖上雙人
            "product_visibility_fail",   # 50873CF0：清單商品圖上沒畫/畫成別件
            "sofa_facing_entrance_door",  # 1A3B0C68：沙發視線正對大門
            "sofa_facing_window",         # 客戶鐵則：沙發正面不得對主窗／落地窗
            "sofa_facing_window_unverified",  # 判官漏答也不得交付
        )
        def _has_high_severity(v: dict) -> bool:
            return isinstance(v, dict) and any(v.get(f) for f in HIGH_SEVERITY_FLAGS)

        def _build_retry_ctx_from_validation(v: dict) -> dict | None:
            """從前一次 validation 抽出完整失敗回饋給 retry prompt。

            不只帶深度數字 (sofa_pct / anchor_pct)，也帶具體 high-severity flag
            (沙發背窗 / 未貼長牆 / 侵入走道 / 未正對焦點…) 與 validation reason，
            讓重試 prompt 真的針對上次的錯誤修正，而不是擲骰子重生同一張。
            """
            if not isinstance(v, dict):
                return None
            ctx = {}
            sp = v.get("sofa_depth_percent_estimate")
            ap = v.get("focal_anchor_depth_percent_estimate")
            if isinstance(sp, (int, float)):
                ctx["sofa_pct"] = sp
            if isinstance(ap, (int, float)) and ap >= 0:
                ctx["anchor_pct"] = ap
            failed_flags = [f for f in HIGH_SEVERITY_FLAGS if v.get(f)]
            if failed_flags:
                ctx["failed_flags"] = failed_flags
            reason = (v.get("reason") or "").strip()
            if reason:
                ctx["reason"] = reason[:240]
            # FE964758：擋門重試帶「量測數字」——靜態指令模型聽不懂多遠才算開
            # （muji 重試後仍貼門 15/1000）。把幾何檢查量到的間距/門寬餵給 retry prompt。
            if v.get("furniture_blocks_door"):
                try:
                    from gemini_analyze import _door_adjacency_violation
                    _viol = _door_adjacency_violation(v.get("render_bboxes") or {})
                    if _viol:
                        ctx["door_gap"] = {"target": _viol[0], "gap": round(_viol[1]),
                                           "door_w": round(_viol[2])}
                except Exception:
                    pass
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
                    if v.get("validation_outage"):
                        print(f"[pipeline] Gemini 額度斷線（429）——跳過 Z3 重試，不燒 fal（{r.get('style')}/{r.get('room_type')}）")
                        break
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
                    # 每次 retry 都帶完整失敗回饋 (flag + reason + depth) 給 retry prompt。
                    # 舊版只在 current_rc>=1 才帶，導致 anchored 訂單 (MAX_RETRY=1) 永遠
                    # 拿不到任何回饋，重試 prompt 跟初次一字不差 → 救不回失敗圖。
                    retry_ctx = _build_retry_ctx_from_validation(v)
                    # 6DA08412 後：翻面已是門在長牆的「預設」佈局（見 prompt_builder
                    # DOOR-ON-A-LONG-WALL LAYOUT），不再需要重試才升級。
                    # 客廳保真失敗 → 優先換另一張 living 底圖（比同圖乾抽更穩、不失真）
                    base_for_gen = entry["_base_path"]
                    pair_alignment_base = _activate_pair_alignment_edit(
                        v, r, entry, str(job_dir), idx)
                    alignment_base = None
                    if pair_alignment_base:
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["tv_alignment_edit"] = True
                        base_for_gen = pair_alignment_base
                        retry_reason = (
                            f"TV/sofa pair centre correction "
                            f"(delta={v.get('pair_center_delta_y')}/1000)"
                        )
                    else:
                        alignment_base = _sofa_alignment_edit_base(
                            v, r, entry.get("_room_type", "living"))
                    if alignment_base:
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["sofa_alignment_edit"] = True
                        base_for_gen = alignment_base
                    elif (not pair_alignment_base
                          and (entry.get("_room_type") or "living") == "living"
                          and _should_try_alt_living_base(v)):
                        _nb = _switch_entry_to_next_living_base(entry)
                        if _nb:
                            base_for_gen = _nb
                            retry_reason = f"{retry_reason} | switch living base"
                    # C2.6 Patch B: Z3 retry 過程中, fal 明確失敗保留原 root cause
                    failed_stage = "z3_retry_generate_renders"
                    try:
                        retry_results = generate_renders(
                            base_for_gen, [entry],
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
                            target_zone=_best_pm_target_zone,
                            target_location_hint=_best_pm_location_hint,
                            target_note=_best_pm_target_note,
                            room_type=entry.get("_room_type", "living"),
                        )
                    except (FalGenerationTimeout, FalResultDownloadError) as re_e:
                        # 522FBC37 根治：重試逾時不准炸整單——舊版 raise 讓其他五張
                        # 好圖全陪葬成「處理失敗」。root cause 記在該 render 上
                        # （dropped_renders 的 timeout 標記吃 error_type），此張保留
                        # 原 hard_fail 驗證 → 交付層自然走 needs_regen，其餘照常交付。
                        print(f"[pipeline] Z3 retry fal 逾時/下載失敗（只犧牲此張）: "
                              f"{type(re_e).__name__}")
                        r["retry_count"] = current_rc + 1
                        r["retry_reason"] = f"retry fal timeout: {str(re_e)[:160]}"
                        r["error_type"] = type(re_e).__name__
                        break
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
                            _lc = layout_ctx if (entry.get("_room_type") or "living") == "living" else None
                            _lc = _product_fidelity_into_layout_ctx(_lc, entry)
                            new_v = validate_render(bpath, rpath, entry["_angle_label"],
                                                    layout_context=_lc,
                                                    room_type=entry.get("_room_type", "living"),
                                                    design_mode=design_mode)
                        else:
                            new_v = {"ok": None, "error": "missing base or render path after retry"}
                    except Exception as ve:
                        new_v = {"ok": None, "error": f"revalidate failed: {str(ve)[:200]}"}
                    new_r["validation"]   = _fail_closed_validation(new_v, entry.get("_room_type", "living"))
                    new_r["angle_label"]  = entry["_angle_label"]
                    # 重試換掉 r 時務必補回房型，否則 new_r 帶的是 Gemini 廣角圖的 living，
                    # 害結果頁用 living 顯示類別濾掉餐桌/床等 → 「圖上有、清單沒有」(job 23EF5810)。
                    new_r["room_type"]    = entry.get("_room_type", "living")
                    new_r["_room_type"]   = entry.get("_room_type", "living")
                    new_r["_base_path"]   = entry.get("_base_path")
                    new_r["cropped"]      = bool(entry.get("_cropped"))
                    new_r["door_excluded"] = bool(entry.get("_door_excluded"))
                    new_r["crop_note"]    = entry.get("_crop_note") or None
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
        # shadow 契約摘要：只觀測，不影響 ok/ng/交付
        if layout_contract_shadows:
            validation_summary["layout_contract_shadow"] = {
                "count": len(layout_contract_shadows),
                "items": layout_contract_shadows,
                "affects_delivery": False,
            }
        print(f"[pipeline] 驗證統計 total={len(final)} ok={ok_n} ng={ng_n} retried={retry_n}")

        # ── Phase 2 硬傷補生 (2026-06-21)：只對「硬傷」風格做一次帶完整錯誤原因的補生。
        # 不重跑已通過 / 軟傷的風格；補生成功就納入交付，仍硬傷則部分交付並記 needs_regen。
        # 不重構 pipeline，附加在交付閘門之前；非 nano 路徑不動。
        if use_nano:
            for idx in range(len(final)):
                r = final[idx]
                v = r.get("validation") or {}
                if not v.get("hard_fail"):
                    continue
                if v.get("validation_outage"):
                    print("[pipeline] Gemini 額度斷線（429）——跳過 Phase2 補生，不燒 fal")
                    continue
                if idx >= len(expanded):
                    continue
                entry = expanded[idx]
                retry_ctx = _build_retry_ctx_from_validation(v)
                print(f"[pipeline] Phase2 硬傷補生 render[{idx}] style={r.get('style')} "
                      f"— {(v.get('reason') or '')[:120]}")
                write_status(job_id, job_dir, "rendering", 93, "為未通過的風格再生成一次…")
                failed_stage = "phase2_hardfix_generate_renders"
                base_for_gen = entry["_base_path"]
                pair_alignment_base = _activate_pair_alignment_edit(
                    v, r, entry, str(job_dir), idx)
                alignment_base = None
                if pair_alignment_base:
                    retry_ctx = dict(retry_ctx or {})
                    retry_ctx["tv_alignment_edit"] = True
                    base_for_gen = pair_alignment_base
                else:
                    alignment_base = _sofa_alignment_edit_base(
                        v, r, entry.get("_room_type", "living"))
                if alignment_base:
                    retry_ctx = dict(retry_ctx or {})
                    retry_ctx["sofa_alignment_edit"] = True
                    base_for_gen = alignment_base
                elif (not pair_alignment_base
                      and (entry.get("_room_type") or "living") == "living"
                      and _should_try_alt_living_base(v)):
                    _nb = _switch_entry_to_next_living_base(entry)
                    if _nb:
                        base_for_gen = _nb
                try:
                    fix_results = generate_renders(
                        base_for_gen, [entry],
                        output_dir=str(job_dir),
                        analysis=analysis, design_mode=design_mode,
                        zoning=zoning_result,
                        customer_notes=customer_notes,
                        budget_tier=budget_tier,
                        retry_context=retry_ctx,
                        force_anchored=force_anchored,
                        job_id=job_id,
                        upload_id_masked=uid_masked,
                        attempt=int(r.get("retry_count") or 0) + 2,
                        stage="phase2_hardfix",
                        target_zone=_best_pm_target_zone,
                        target_location_hint=_best_pm_location_hint,
                        target_note=_best_pm_target_note,
                        room_type=entry.get("_room_type", "living"),
                    )
                except (FalGenerationTimeout, FalResultDownloadError) as fx_e:
                    # 522FBC37 根治（同 Z3）：補生逾時只犧牲此張，不殺整單。
                    print(f"[pipeline] Phase2 補生 fal 逾時/下載失敗（只犧牲此張）: "
                          f"{type(fx_e).__name__}")
                    entry["error_type"] = type(fx_e).__name__
                    continue
                except Exception as fx_e:
                    print(f"[pipeline] Phase2 補生例外: {fx_e}")
                    continue
                if not fix_results:
                    continue
                new_r = fix_results[0]
                if new_r.get("render_path"):
                    src_p = Path(new_r["render_path"])
                    new_p = src_p.parent / f"render_{entry.get('style','x')}_{idx:02d}_hardfix{src_p.suffix}"
                    try:
                        src_p.rename(new_p)
                        new_r["render_path"] = str(new_p)
                    except Exception:
                        pass
                try:
                    from gemini_analyze import validate_render
                    bpath = entry["_base_path"]
                    rpath = new_r.get("render_path") or ""
                    if rpath and Path(bpath).exists() and Path(rpath).exists():
                        _lc = layout_ctx if (entry.get("_room_type") or "living") == "living" else None
                        _lc = _product_fidelity_into_layout_ctx(_lc, entry)
                        new_v = validate_render(bpath, rpath, entry["_angle_label"],
                                                layout_context=_lc,
                                                room_type=entry.get("_room_type", "living"),
                                                design_mode=design_mode)
                    else:
                        new_v = {"ok": None, "error": "missing path after hardfix"}
                except Exception as ve:
                    new_v = {"ok": None, "error": f"revalidate hardfix failed: {str(ve)[:200]}"}
                new_r["validation"]   = _fail_closed_validation(new_v, entry.get("_room_type", "living"))
                new_r["angle_label"]  = entry["_angle_label"]
                # 同 Z3 retry：補回房型，避免結果頁用 living 濾掉該房家具。
                new_r["room_type"]    = entry.get("_room_type", "living")
                new_r["_room_type"]   = entry.get("_room_type", "living")
                new_r["_base_path"]   = entry.get("_base_path")
                new_r["cropped"]      = bool(entry.get("_cropped"))
                new_r["door_excluded"] = bool(entry.get("_door_excluded"))
                new_r["crop_note"]    = entry.get("_crop_note") or None
                new_r["retry_count"]  = int(r.get("retry_count") or 0) + 1
                new_r["retry_reason"] = "phase2 hardfix"
                # 補生後不再是硬傷才取代；仍硬傷則保留原狀（後續 needs_regen 記錄）
                if not (new_v or {}).get("hard_fail"):
                    final[idx] = new_r
                    print(f"[pipeline] Phase2 補生成功 render[{idx}] style={new_r.get('style')}")
                else:
                    print(f"[pipeline] Phase2 補生仍硬傷 render[{idx}] style={r.get('style')}")

        # Delivery gate (2026-06-21, partial delivery + 硬傷分級):
        # 只有「硬傷」(hard_fail=結構破壞/動線阻塞/沙發錯邊/跑錯分區/背窗/完全沒對向) 才不交付。
        # 軟傷 (深度小偏差、茶几略偏、軟裝不齊) 照常交付 → 客戶幾乎一定拿到所有風格。
        # 部分交付：有任何可交付的就交付，被移除的 style + 原因記進 result_json。
        # 只有「全部都硬傷」時才讓 job failed，避免 result 頁展示已知壞圖。
        def _is_hard_fail(r: dict) -> bool:
            # 硬傷（驗收）或 render 本身失敗（沒產出圖）都不可交付。
            # 後者修「奶油暖居 沒圖卻被當已交付 → 前端卡『生成中』」：fal 失敗、
            # render_path 不存在、或帶 error 的 render，視為不可交付。
            if (r.get("validation") or {}).get("hard_fail"):
                return True
            if r.get("error") or r.get("render_error"):
                return True
            rp = r.get("render_path") or ""
            if not rp or not Path(rp).exists():
                return True
            return False
        delivery_final = [r for r in final if not _is_hard_fail(r)]
        dropped_failed_renders = [r for r in final if _is_hard_fail(r)]

        # 46F1B2B5 分級交付：加分品項（燈具/單椅…非 must）沒入圖不殺圖——
        # 從該房購物清單移除該品項，清單=圖從清單端成立。must 缺漏仍在上面硬傷擋。
        for r in delivery_final:
            _nice_bad = set((r.get("validation") or {}).get("visibility_nice_bad") or [])
            if not _nice_bad:
                continue
            _mf = r.get("matched_furniture") or []
            _kept = [it for it in _mf
                     if not (isinstance(it, dict) and (it.get("category_en") or "") in _nice_bad)]
            _removed = [f"{(it.get('category_en') or '?')}:{(it.get('name_zh') or '')[:24]}"
                        for it in _mf
                        if isinstance(it, dict) and (it.get("category_en") or "") in _nice_bad]
            if _removed:
                r["matched_furniture"] = _kept
                print(f"[visibility] {r.get('style')}/{r.get('room_type','living')} "
                      f"加分品項未入圖，自清單移除：{'、'.join(_removed)}")
        dropped_validation_reasons = []
        for r in dropped_failed_renders:
            v = r.get("validation") or {}
            _is_timeout = (r.get("error_type") in ("FalGenerationTimeout", "FalResultDownloadError")) \
                          or ("exceeded" in str(r.get("error") or "").lower())
            # 1164DFC6 修正：有「真的跑完的驗收判定」時（ok 非 None 且有 reason），
            # 判定優先——舊寫法 r.error 無條件優先，fal 暫時性假鎖（User is locked）
            # 的過期字串蓋掉真正落選原因（幾何擋門），誤導排查方向整整一輪。
            # 驗收沒真的跑（ok=None / 沒圖可驗）時，才輪到 r.error 保住 fal 根因
            # （原教訓：別被 "missing base" 蓋住真實 render 錯誤）。
            _v_reason = (v.get("reason") or "").strip()
            if v.get("ok") is not None and _v_reason:
                reason = _v_reason
            else:
                reason = r.get("error") or _v_reason or v.get("error") or "render 未產出"
            dropped_validation_reasons.append({
                "style":       r.get("style"),
                "style_label": r.get("style_label"),
                "angle_label": r.get("angle_label"),     # 哪個房間/視角失敗
                "room_type":   r.get("room_type"),
                "timeout":     bool(_is_timeout),         # 前端可顯示友善「生成逾時」文案
                "reason":      str(reason)[:240],
            })

        # 全部硬傷時：不再打成 failed（客戶不該看到「處理失敗」）。
        # 改標 repairing：訂單仍 completed，但帶 repairing 旗標 + needs_regen，
        # result 頁顯示「設計仍在優化中，會盡快補上」，由後續/人工補生交付。
        all_failed_repairing = (len(delivery_final) == 0)
        if all_failed_repairing:
            print("[pipeline] 全部硬傷 → 標 repairing（不打 failed），記 needs_regen 待補生")

        if dropped_failed_renders:
            # 部分交付：交付通過的，被移除的記錄起來給前端 + summary，不讓整單消失。
            print(
                "[pipeline] partial delivery — dropped failed render(s): "
                + ",".join(str(r.get("style") or "?") for r in dropped_failed_renders)
                + f"; delivering {len(delivery_final)} render(s)"
            )
        validation_summary["delivered"]       = len(delivery_final)
        validation_summary["dropped"]         = len(dropped_failed_renders)
        validation_summary["dropped_renders"] = dropped_validation_reasons

        # 客戶清單只顯示「圖中真有的核心家具」(2026-06-21)：
        # render 只畫 sofa/coffee_table/rug（參考圖）+ media_console（強制 focal anchor）。
        # 單椅/邊几等 nice-to-have 從不渲染 → 不可出現在「為你搭配的家具」清單，
        # 否則客戶會看到圖上沒有的家具（且還掛價格）。
        from furniture_match import LIVING_MUST_HAVE
        _RENDERED_CORE_CATS = set(LIVING_MUST_HAVE)
        # 各房型「圖中真的會畫出的主家具」品類 → 清單只顯示這些（與 prompt 參考對齊，原則跟客廳統一）。
        # 不含 lighting（燈具歸軟裝獨立區，避免主清單與軟裝重複）。
        _DISPLAY_CATS_BY_ROOM = {
            "living":  set(LIVING_MUST_HAVE),                       # sofa/coffee_table/rug/media_console
            "bedroom": {"bed", "storage", "side_table", "rug"},     # 床/衣櫃/床頭櫃/地毯
            "dining":  {"dining_table", "dining_chair", "rug"},  # 餐桌/餐椅/地毯（不含邊桌，渲染常沒畫）
            "study":   {"table", "chair", "storage", "rug"},        # 書桌/椅/書櫃/地毯
        }

        def _rendered_core_only(mf: list, room_type: str = "living") -> list:
            mf = mf or []
            cats = _DISPLAY_CATS_BY_ROOM.get(room_type, _RENDERED_CORE_CATS)
            items = [it for it in mf if (it.get("category_en") or "") in cats]
            # category_en 缺失 / 全空 → 退回原清單前幾件，避免整列消失（defensive）
            return (items or list(mf))[:5]

        # reference_map 進 DB 前去掉 base64 data URL：
        # 房間底圖是以 data:image/jpeg;base64 形式進 reference_map 的，一張 render
        # 可以塞 4MB+（C15719C5 實測 result_json 高達 8.6MB）——這正是單房訂單也
        # 觸發 payload_trimmed、完整 zoning/validation 被裁掉、結果頁要下載 8.6MB
        # 的根因。result.html 只讀 kind/id/cat_en/name_zh，從不讀 url；pipeline
        # 內部（Z3 重試/Phase2）用的是記憶體中的原始 dict，不經過這裡。
        def _slim_refmap(refs):
            out = []
            for ref in (refs or []):
                if not isinstance(ref, dict):
                    continue
                ref2 = dict(ref)
                if str(ref2.get("url") or "").startswith("data:"):
                    ref2["url"] = None   # http 商品圖 URL 很小，保留；base64 一律去掉
                out.append(ref2)
            return out

        # 上傳渲染圖到 Supabase Storage
        slim_renders = []
        for r in delivery_final:
            raw_path = r.get("render_path") or ""
            render_path = Path(raw_path) if raw_path else None
            render_url = None
            if render_path and render_path.exists():
                render_url = sb_upload_render(job_id, render_path)
            slim_renders.append({
                "style":             r.get("style"),
                "style_label":       r.get("style_label"),
                "angle_label":       r.get("angle_label", "主視角"),
                "room_type":         r.get("room_type", "living"),   # step-2：結果頁按房間分頁/驗收用
                "cropped":           bool(r.get("cropped")),         # (i) 此圖底圖已裁成單房視角
                "door_excluded":     bool(r.get("door_excluded")),    # 大門在鏡頭外
                "crop_note":         r.get("crop_note"),             # 沒裁的原因（診斷）
                "render_model":      r.get("render_model"),          # debug：banana / gpt-image-2
                "render_filename":   render_path.name if render_path else None,
                "render_url":        render_url,
                "render_error":      r.get("error"),
                "matched_furniture": _rendered_core_only(r.get("matched_furniture"), r.get("room_type", "living")),
                # 軟裝接入 (2026-06-18): 結果頁獨立區塊顯示, 不併入主總計
                "soft_furnishing":   r.get("soft_furnishing", []),
                "validation":        r.get("validation"),
                # ── T4 新增：Nano Banana 路徑會帶；Flux 路徑用預設值 ──
                "pipeline_version":      r.get("pipeline_version", "flux-v1"),
                "reference_map":         _slim_refmap(r.get("reference_map")),
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
            "design_mode":              design_mode,   # furnish / full：方便驗證 full 有沒有真的傳到
            "palettes":                 palettes,      # 使用者選的色系 {style:色系}，驗證有沒有送到
        }

        # ── P2-MVP-0: 把 /api/job 傳過來的 rooms_meta.json 補進 result_json ──
        # 沒檔案 = 沒 rooms = 等同 Phase A 原行為，不寫 rooms 欄位
        # PhotoMeta v1 (Step 1): 把 photo_meta_by_key 也讀回, 寫進 customer_inputs.
        # 注意: Step 1 不消費這個欄位 (analyze_image / prompt_builder / render 不動),
        #       只是落地保存. Step 2+ 才開始注入 AI prompt.
        rooms_for_json: list = []
        primary_room_notes_for_json: str = ""
        photo_meta_by_key_for_json: dict = {}
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
                    if isinstance(rm.get("photo_meta_by_key"), dict):
                        photo_meta_by_key_for_json = rm["photo_meta_by_key"]
            except Exception as me:
                print(f"[pipeline] rooms_meta 讀取失敗，忽略: {me}")

        if primary_room_notes_for_json:
            customer_inputs["primary_room_notes"] = primary_room_notes_for_json
        if photo_meta_by_key_for_json:
            customer_inputs["photo_meta_by_key"] = photo_meta_by_key_for_json

        # Phase 1.1: 把每張 render 實際採用的 render_mode 滙集成 top-level
        # 由 generate_renders() 標示, api.py 不重新推測。
        # 全部相同 → 該值; 混合 → "mixed"; 全 None → 不寫.
        failed_stage = "result_build"
        _modes = {r.get("render_mode") for r in delivery_final if r.get("render_mode")}
        top_render_mode: str | None = None
        if len(_modes) == 1:
            top_render_mode = next(iter(_modes))
        elif len(_modes) > 1:
            top_render_mode = "mixed"
        last_render_mode = top_render_mode or last_render_mode

        # 空間分析保底：design_analysis 是前端「你會拿到」承諾的項目，但 Gemini 偶爾回空。
        # 空的話用 space_type + lighting + zoning 湊一段安全文字，避免「承諾了卻沒拿到」(Grok #1)。
        try:
            if isinstance(analysis, dict):
                analysis["design_analysis_source"] = (
                    "gemini" if (analysis.get("design_analysis") or "").strip() else "fallback")
            if isinstance(analysis, dict) and not (analysis.get("design_analysis") or "").strip():
                _sp = {"living": "客廳", "dining": "餐廳", "bedroom": "主臥室",
                       "study": "書房", "whole": "全室多空間"}.get(analysis.get("space_type") or "", "此空間")
                _lt = (analysis.get("lighting") or "").strip()
                _zc = ""
                if isinstance(zoning_result, dict):
                    _zc = ((zoning_result.get("spatial_synthesis") or {}).get("room_shape")
                           or zoning_result.get("summary") or "")
                _parts = [f"已依照片為{_sp}規劃家具與動線配置"]
                if _zc:
                    _parts.append(str(_zc)[:40])
                _parts.append(_lt if _lt else "並依採光條件安排明亮度與燈光氛圍")
                analysis["design_analysis"] = "；".join(p for p in _parts if p) + "。"
                print("[pipeline] design_analysis 空 → 已套用保底文字")
        except Exception as _ae:
            print(f"[pipeline] design_analysis 保底失敗，忽略: {_ae}")

        # C2.6 → partial delivery (2026-06-21): 上方 delivery gate 已把 validation.ok=False
        # 的圖從 delivery_final / slim_renders 移除，並在「全部失敗」時 raise。anchored 路徑
        # 不再額外因「有任一張失敗」整單 raise — 否則一過一不過時整單仍會消失，違背部分交付。
        # 被移除的 style 由 validation_summary.dropped_renders 帶給前端標示。

        result_json_payload = {
            "build_tag":          "fullmode-rewrite-v2",      # 部署版本標記（確認最新碼有跑）
            "analysis":           analysis,
            "zoning":             zoning_result,
            "zoning_v2":          user_zoning_v2,             # Z2: 保留原始 v2（未轉換）
            "layout_choice":      user_layout_choice or None,
            "renders":            slim_renders,
            "validation_summary": validation_summary,
            "customer_inputs":    customer_inputs,            # Phase A
        }
        # Phase0 格局契約 shadow：完整摘要進 result_json，前端可忽略
        if layout_contract_shadows:
            result_json_payload["layout_contract_shadow"] = {
                "version": "phase0_v3",
                "affects_delivery": False,
                "items": layout_contract_shadows,
            }
        if top_render_mode:
            result_json_payload["render_mode"] = top_render_mode
        if rooms_for_json:
            result_json_payload["rooms"] = rooms_for_json     # P2-MVP-0
        # Phase 2: 補生後仍硬傷的風格 → 記 needs_regen（額度不消失，待人工/後續補件）
        # 9871F294 抓漏：多房間訂單只記風格名，前端顯示「新中式已交付」+「新中式細修中」
        # 自相矛盾——帶上房間標籤讓前端能顯示「新中式美學 · 客廳」。
        if dropped_failed_renders:
            result_json_payload["needs_regen"] = [
                {"style": r.get("style"), "style_label": r.get("style_label"),
                 "room_type": r.get("room_type") or r.get("_room_type"),
                 "angle_label": r.get("angle_label") or r.get("_angle_label")}
                for r in dropped_failed_renders if r.get("style")
            ]
        # 全部硬傷 → repairing：訂單仍 completed，但前端顯示「優化中」而非失敗
        if all_failed_repairing:
            result_json_payload["repairing"] = True

        # P0（C79C7ECC）：客廳是全室／主視覺主菜——若客廳全被擋、一張都沒交付，
        # 不可顯示「設計方案生成完畢」（客人只拿到主臥書房會覺得被騙）。
        _living_delivered = any(
            (r.get("room_type") or r.get("_room_type") or "living") == "living"
            for r in delivery_final
        )
        _living_dropped = any(
            (r.get("room_type") or r.get("_room_type") or "") == "living"
            for r in dropped_failed_renders
        )
        living_incomplete = bool(_living_dropped and not _living_delivered)
        _auto_repair_enabled = bool(
            dropped_failed_renders
            and os.environ.get("AUTO_REPAIR", "1").strip() != "0"
        )
        if living_incomplete:
            result_json_payload["living_incomplete"] = True
            print("[pipeline] living_incomplete=True — 客廳未交付，進入 repairing，不得標 completed")
        if _auto_repair_enabled:
            result_json_payload["repairing"] = True
        if dropped_failed_renders and not all_failed_repairing:
            result_json_payload["partial_delivery"] = True

        # C2.6: completed DB write 需驗證, 否則不可設 completed_flag。
        # 大 payload（result_json 含 analysis/zoning/renders…）寫入可能 >8s 逾時被吞掉，
        # 導致狀態沒更新成 completed → 圖明明生好了卻被打成 failed。
        # 對策：拉長 timeout + 重試多次（寫入→讀回驗證），全部失敗才 raise。
        failed_stage = "result_upsert"
        if all_failed_repairing:
            completed_msg = "部分設計仍在為你優化中，我們會盡快補上"
        elif living_incomplete:
            completed_msg = "主空間（客廳）設計仍在優化中；其他房間已先交付"
        elif dropped_failed_renders:
            completed_msg = "部分設計仍在為你優化中，我們會盡快補上"
        else:
            completed_msg = "設計方案生成完畢！"
        if _auto_repair_enabled:
            _delivery_status = "repairing"
        elif living_incomplete or all_failed_repairing:
            _delivery_status = "incomplete"
        else:
            _delivery_status = "completed"
        completed_payload = {"job_id": job_id, "status": _delivery_status, "progress": 100,
                             "message": completed_msg,
                             "result_json": result_json_payload}
        # 全室等大 payload 可能寫不進去（之前 ED3B66EF 渲染到 92% 卻卡在 result_upsert）。
        # 對策：前 2 次寫完整版；仍失敗就改寫「精簡版」——只留結果頁必要欄位（渲染圖 URL +
        # 家具 + 基本空間摘要），捨棄 zoning/逐圖 validation/完整 analysis，確保訂單能完成、圖能交付。
        # 精簡 validation：只留沙發/動線相關關鍵欄位（很小），讓 trimmed 時仍能事後診斷擺位問題。
        def _tiny_val(v):
            if not isinstance(v, dict):
                return None
            return {kk: v.get(kk) for kk in (
                "ok", "hard_fail", "room_type", "reason", "soft_issues",
                "sofa_depth_percent_estimate", "sofa_depth_grounded_pct",
                "sofa_outside_living_zone", "sofa_on_wrong_side", "sofa_back_against_window",
                "focal_anchor_misaligned_with_sofa", "furniture_blocks_walkway") if kk in v}

        slim_result_json = {
            "build_tag": "fullmode-rewrite-v2",
            "renders": [
                {**{k: rr.get(k) for k in
                    ("style", "style_label", "angle_label", "room_type", "cropped", "render_url",
                     "render_filename", "matched_furniture", "soft_furnishing", "reference_map",
                     "render_model")},
                 "validation": _tiny_val(rr.get("validation"))}
                for rr in slim_renders
            ],
            "analysis": {k: (analysis or {}).get(k) for k in
                         ("design_analysis", "design_analysis_source", "space_type", "lighting", "layout_notes")},
            "customer_inputs": customer_inputs,
            "payload_trimmed": True,
        }
        if rooms_for_json:
            slim_result_json["rooms"] = rooms_for_json
        if result_json_payload.get("repairing"):
            slim_result_json["repairing"] = True
        if dropped_failed_renders:
            slim_result_json["needs_regen"] = result_json_payload.get("needs_regen", [])
        if living_incomplete:
            slim_result_json["living_incomplete"] = True
        if result_json_payload.get("partial_delivery"):
            slim_result_json["partial_delivery"] = True

        # 極簡 payload（第三層 fallback）：留「結果頁必要欄位 + 精簡家具」，仍夠小一定寫得進。
        # 家具只留顯示必要欄位（去掉 flux_descriptor/dimensions/colors/id 等），確保最小層也有清單。
        def _tiny_furn(items):
            return [
                {kk: it.get(kk) for kk in
                 ("name_zh", "brand", "price_twd", "category", "category_en", "purchase_url", "image_url")}
                for it in (items or [])
            ]
        minimal_result_json = {
            "renders": [
                {"style": rr.get("style"), "style_label": rr.get("style_label"),
                 "angle_label": rr.get("angle_label"), "room_type": rr.get("room_type", "living"),
                 "cropped": bool(rr.get("cropped")), "render_model": rr.get("render_model"),
                 "render_url": rr.get("render_url"), "render_filename": rr.get("render_filename"),
                 "matched_furniture": _tiny_furn(rr.get("matched_furniture")),
                 "soft_furnishing": _tiny_furn(rr.get("soft_furnishing")),
                 "validation": _tiny_val(rr.get("validation"))}
                for rr in slim_renders
            ],
            # debug 欄位也留在最小 payload，避免「裁切/保底/版本」看不到而一直靠猜
            "analysis": {"space_type": (analysis or {}).get("space_type"),
                         "design_analysis_source": (analysis or {}).get("design_analysis_source")},
            "customer_inputs": customer_inputs,
            "build_tag": "fullmode-rewrite-v2",
            "payload_trimmed": True,
        }
        if result_json_payload.get("repairing"):
            minimal_result_json["repairing"] = True
        if living_incomplete:
            minimal_result_json["living_incomplete"] = True
        if result_json_payload.get("partial_delivery"):
            minimal_result_json["partial_delivery"] = True
        if dropped_failed_renders:
            minimal_result_json["needs_regen"] = result_json_payload.get("needs_regen", [])

        # 事前估算大小：完整 payload 太大就直接從精簡版起跳。
        try:
            _full_kb = len(json.dumps(result_json_payload, ensure_ascii=False).encode("utf-8")) // 1024
        except Exception:
            _full_kb = 0
        if _full_kb >= 700:
            print(f"[pipeline] result_json 約 {_full_kb}KB 偏大 → 從精簡版起跳")
            _tiers = [slim_result_json, slim_result_json, minimal_result_json, minimal_result_json]
        else:
            _tiers = [result_json_payload, result_json_payload, slim_result_json, minimal_result_json]

        for _attempt in range(4):
            payload = {"job_id": job_id, "status": _delivery_status, "progress": 100,
                       "message": completed_msg, "result_json": _tiers[_attempt]}
            # 根因修復：信任 POST 的 2xx 回傳。大 row 的 sb_get 讀回常逾時 → 過去誤判
            # 「寫入未生效」→ 外層 except 把『其實已完成』的單標成 failed。寫入成功就收工。
            if sb_upsert(payload, timeout=25):
                completed_flag = True
                break
            # POST 非 2xx（可能逾時卻已寫入）→ 讀回確認目標狀態。
            verify_row = sb_get(job_id) or {}
            if verify_row.get("status") == _delivery_status:
                completed_flag = True
                break
            print(f"[pipeline] {_delivery_status} 寫入未生效（第 {_attempt + 1} 次，tier{_attempt}），"
                  f"狀態={verify_row.get('status')!r}，重試…")
        if not completed_flag:
            raise RuntimeError(
                f"{_delivery_status} DB write verification failed after retries; "
                f"current status={(sb_get(job_id) or {}).get('status')!r}"
            )

        # 跑完自動清掉 R2 上的影片（隱私 + 省空間）
        for key in r2_keys_to_delete:
            ok = r2_delete_object(key)
            print(f"[pipeline] R2 清除 {key}: {'OK' if ok else 'FAIL'}")

        # ── Phase 3（2026-07-08）：自動補到好 ────────────────────────────────
        # 部分交付對客人＝沒拿到貨（用戶定調：客人會覺得受騙，商業化不可接受）。
        # 客人已先拿到通過的圖（completed 已寫入），這裡在同一個背景任務裡對被扣
        # 的 render 換策略續生：
        #   策略 A：退回未裁切原圖重生（裁切放大狹長房的空間誤導，9871F294 主因假設）
        #   策略 B：換渲染模型（gpt-image-2 ↔ nano-banana，卡死時最後一招）
        # 任一張過驗證 → 立即補寫 result_json（結果頁輪詢自動出現）。
        # 全程 best-effort：任何例外不影響已交付內容。
        if _auto_repair_enabled:
            try:
                for idx in range(len(final)):
                    r = final[idx]
                    if not _is_hard_fail(r) or idx >= len(expanded):
                        continue
                    entry = expanded[idx]
                    v0 = r.get("validation") or {}
                    if v0.get("validation_outage"):
                        print("[pipeline] Gemini 額度斷線（429）——跳過 Phase3 補生，不燒 fal")
                        continue
                    retry_ctx = _build_retry_ctx_from_validation(v0)
                    # AI-auto guide 綁在同一底圖，Phase3 不得換圖或改用未裁切原圖。
                    pair_alignment_base = _activate_pair_alignment_edit(
                        v0, r, entry, str(job_dir), idx)
                    alignment_base = None
                    if pair_alignment_base:
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["tv_alignment_edit"] = True
                        strategies = [("TV中心軸局部校正", pair_alignment_base, None)]
                    else:
                        alignment_base = _sofa_alignment_edit_base(
                            v0, r, entry.get("_room_type", "living"))
                        if alignment_base:
                            retry_ctx = dict(retry_ctx or {})
                            retry_ctx["sofa_alignment_edit"] = True
                            strategies = [("沙發局部位移", alignment_base, None)]
                        else:
                            strategies = _phase3_base_strategies(entry)
                    fixed = None
                    for tag, base_p, model_ov in strategies:
                        print(f"[pipeline] Phase3 自動補生 render[{idx}] "
                              f"style={r.get('style')} 策略={tag}")
                        try:
                            p3 = generate_renders(
                                base_p, [entry], output_dir=str(job_dir),
                                analysis=analysis, design_mode=design_mode,
                                zoning=zoning_result, customer_notes=customer_notes,
                                budget_tier=budget_tier, retry_context=retry_ctx,
                                force_anchored=force_anchored, job_id=job_id,
                                upload_id_masked=uid_masked,
                                attempt=int(r.get("retry_count") or 0) + 3,
                                stage="phase3_auto_repair",
                                target_zone=_best_pm_target_zone,
                                target_location_hint=_best_pm_location_hint,
                                target_note=_best_pm_target_note,
                                room_type=entry.get("_room_type", "living"),
                                render_model_override=model_ov,
                            )
                        except Exception as g_e:
                            print(f"[pipeline] Phase3 生成例外（{tag}）: {str(g_e)[:150]}")
                            continue
                        cand = (p3 or [{}])[0]
                        rpath = cand.get("render_path")
                        if not rpath:
                            continue
                        try:
                            from gemini_analyze import validate_render
                            _lc = layout_ctx if (entry.get("_room_type") or "living") == "living" else None
                            _lc = _product_fidelity_into_layout_ctx(_lc, entry)
                            validation_base = (
                                entry["_base_path"]
                                if (pair_alignment_base or alignment_base) else base_p
                            )
                            v3 = validate_render(validation_base, rpath, entry["_angle_label"],
                                                 layout_context=_lc,
                                                 room_type=entry.get("_room_type", "living"),
                                                 design_mode=design_mode)
                            v3 = _fail_closed_validation(
                                v3, entry.get("_room_type", "living"))
                        except Exception as v_e:
                            print(f"[pipeline] Phase3 驗證例外（{tag}）: {str(v_e)[:120]}")
                            continue
                        if (v3 or {}).get("hard_fail"):
                            print(f"[pipeline] Phase3 {tag} 仍硬傷: {(v3.get('reason') or '')[:100]}")
                            continue
                        # 通過 → 改名 + 上傳 + 組交付欄位
                        src_p = Path(rpath)
                        new_p = src_p.parent / f"render_{entry.get('style','x')}_{idx:02d}_repair{src_p.suffix}"
                        try:
                            src_p.rename(new_p)
                            rpath = str(new_p)
                        except Exception:
                            pass
                        cand["render_path"] = rpath
                        cand["validation"] = v3
                        cand["angle_label"] = entry["_angle_label"]
                        cand["room_type"] = entry.get("_room_type", "living")
                        cand["cropped"] = base_p == entry.get("_base_path") and bool(entry.get("_cropped"))
                        cand["crop_note"] = None if cand["cropped"] else "Phase3 補生（未裁切原圖）"
                        cand["retry_count"] = int(r.get("retry_count") or 0) + 2
                        cand["retry_reason"] = f"phase3 auto repair ({tag})"
                        rurl = sb_upload_render(job_id, Path(rpath))
                        cand["render_url"] = rurl
                        cand["render_filename"] = Path(rpath).name
                        fixed = cand
                        break
                    if fixed is None:
                        continue
                    # 補寫 DB：讀回目前 result_json，append render、更新統計與 needs_regen
                    try:
                        row = sb_get(job_id) or {}
                        rj = row.get("result_json") if isinstance(row.get("result_json"), dict) else {}
                        rj_renders = rj.get("renders") or []
                        _new_render = {
                            "style":             fixed.get("style"),
                            "style_label":       fixed.get("style_label"),
                            "angle_label":       fixed.get("angle_label", "主視角"),
                            "room_type":         fixed.get("room_type", "living"),
                            "cropped":           bool(fixed.get("cropped")),
                            "crop_note":         fixed.get("crop_note"),
                            "render_model":      fixed.get("render_model"),
                            "render_filename":   fixed.get("render_filename"),
                            "render_url":        fixed.get("render_url"),
                            "render_error":      None,
                            "matched_furniture": _rendered_core_only(fixed.get("matched_furniture"),
                                                                     fixed.get("room_type", "living")),
                            "soft_furnishing":   fixed.get("soft_furnishing", []),
                            "validation":        fixed.get("validation"),
                            "pipeline_version":  fixed.get("pipeline_version", "flux-v1"),
                            "reference_map":     _slim_refmap(fixed.get("reference_map")),
                            "notes":             fixed.get("notes", ""),
                            "unmatched_visual_items": fixed.get("unmatched_visual_items", []),
                            "retry_count":       fixed.get("retry_count", 0),
                            "retry_reason":      fixed.get("retry_reason"),
                        }
                        # 插回正確位置：同風格內依房型序（客廳→餐廳→主臥→書房），
                        # 不 append 到最後（否則補上的客廳排在主臥/書房後，2A520C25）。
                        _rt_ord = {"living": 0, "dining": 1, "bedroom": 2, "study": 3}
                        _st = _new_render.get("style")
                        _rk = _rt_ord.get(_new_render.get("room_type") or "living", 9)
                        _pos = len(rj_renders)
                        for _j, _rr in enumerate(rj_renders):
                            if _rr.get("style") == _st and \
                               _rt_ord.get(_rr.get("room_type") or "living", 9) > _rk:
                                _pos = _j
                                break
                        rj_renders.insert(_pos, _new_render)
                        rj["renders"] = rj_renders
                        vs = rj.get("validation_summary") or {}
                        vs["delivered"] = int(vs.get("delivered") or 0) + 1
                        vs["dropped"] = max(0, int(vs.get("dropped") or 0) - 1)
                        vs["dropped_renders"] = [
                            d for d in (vs.get("dropped_renders") or [])
                            if not (d.get("style") == fixed.get("style")
                                    and d.get("room_type") == fixed.get("room_type"))
                        ]
                        rj["validation_summary"] = vs
                        if fixed.get("room_type") == "living":
                            rj.pop("living_incomplete", None)
                        if int(vs.get("dropped") or 0) == 0:
                            rj.pop("partial_delivery", None)
                        rj["needs_regen"] = [
                            n for n in (rj.get("needs_regen") or [])
                            if not (n.get("style") == fixed.get("style")
                                    and (n.get("room_type") or "") in ("", fixed.get("room_type")))
                        ]
                        if not rj["needs_regen"]:
                            rj.pop("repairing", None)
                            rj.pop("needs_regen", None)
                        _repair_status = "repairing" if rj.get("needs_regen") else "completed"
                        sb_upsert({"job_id": job_id, "status": _repair_status, "progress": 100,
                                   "message": "設計方案生成完畢！" if not rj.get("needs_regen")
                                              else "部分設計仍在為你優化中，我們會盡快補上",
                                   "result_json": rj}, timeout=25)
                        print(f"[pipeline] Phase3 補生成功並已補寫 render[{idx}] "
                              f"style={fixed.get('style')} room={fixed.get('room_type')}")
                    except Exception as db_e:
                        print(f"[pipeline] Phase3 補寫 DB 失敗: {str(db_e)[:150]}")
            except Exception as p3_outer:
                print(f"[pipeline] Phase3 例外（不影響已交付）: {str(p3_outer)[:200]}")

            # Phase3 已跑完仍有缺圖，就明確收斂成 incomplete；不可永遠 repairing，
            # 更不可把主客廳缺圖寫成 completed。
            try:
                _post_repair_row = sb_get(job_id) or {}
                if _post_repair_row.get("status") == "repairing":
                    _post_rj = (_post_repair_row.get("result_json")
                                if isinstance(_post_repair_row.get("result_json"), dict) else {})
                    _post_rj.pop("repairing", None)
                    _post_rj["repair_incomplete"] = True
                    sb_upsert({
                        "job_id": job_id,
                        "status": "incomplete",
                        "progress": 100,
                        "message": "主空間仍未通過配置驗收，請聯絡客服重新處理",
                        "result_json": _post_rj,
                    }, timeout=25)
            except Exception as _finalize_repair_error:
                print(f"[pipeline] Phase3 incomplete 收尾失敗: {str(_finalize_repair_error)[:150]}")

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
                if cur_status not in ("completed", "failed", "incomplete"):
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
    palettes_json: str     = Form(default=""),       # 使用者每個風格選的色系 {style_id: 色系中文名}
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
                    # Railway ephemeral storage 蒸發後的恢復路徑也要轉正 EXIF，
                    # 否則直拍照片只有「新單」正常、「恢復單」方向全錯（GPT 抓漏）
                    _normalize_photo_orientation(str(dest))
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
    photo_meta_by_key: dict[str, dict] = {}   # PhotoMeta v1

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
                        # PhotoMeta v1 raw (在下方 normalization block 驗證)
                        "_raw_photo_meta": r.get("photo_meta"),
                    })
                if not cleaned:
                    fail_reason = "rooms_json 沒有有效的空間資料"
                else:
                    primary = next((r for r in cleaned if r["is_primary"]), cleaned[0])
                    if not primary["photo_keys"]:
                        fail_reason = (f"主空間「{primary['room_label'] or primary['room_type']}」"
                                       f"必須至少上傳一張照片")
                    else:
                        # ── PhotoMeta v1: per-room normalize + validate ──
                        # 老 client 沒 photo_meta → 退化為現況行為.
                        # 新 client 有 photo_meta → 驗 5 條規則 (見 _normalize_photo_meta_for_room).
                        # 任一 room 驗證失敗 → 全單 fail-closed 400.
                        for room in cleaned:
                            # 把 _raw_photo_meta 重新映到 photo_meta 給 normalize 用
                            tmp = {**room, "photo_meta": room.pop("_raw_photo_meta")}
                            normalized, pm_err = _normalize_photo_meta_for_room(tmp)
                            if pm_err:
                                label = room.get("room_label") or room.get("room_type") or "?"
                                fail_reason = f"PhotoMeta v1: room「{label}」: {pm_err}"
                                break
                            for m in normalized:
                                photo_meta_by_key[m["photo_key"]] = m

                        if not fail_reason:
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
        kept_videos: list[str] = []
        for p in photo_paths:
            if not isinstance(p, str):
                continue
            if p.startswith("r2://"):
                # 影片是「全屋空間理解」素材，不屬於任何單一房間的照片白名單，
                # 一律保留給 pipeline → analyze_space(影片為主、照片為輔)。
                # 2026-07-08（job 20A8220A 抓漏）：這裡原本直接排除 = 全室方案
                # 客戶上傳的影片被默默丟掉，「影片輔助理解」完全沒發生。
                # 渲染底圖仍只用照片（run_pipeline 內 video/image 分流），
                # 影片只進理解層，不影響各房底圖選擇。
                kept_videos.append(p)
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
              f"kept_videos={len(kept_videos)}")
        photo_paths = matched + kept_videos

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
    # PhotoMeta v1 (Step 1): photo_meta_by_key 也走同一個 side file
    if rooms_data:
        with open(job_dir / "rooms_meta.json", "w", encoding="utf-8") as f:
            json.dump({
                "rooms":              rooms_data,
                "primary_room_notes": primary_room_notes,
                "photo_meta_by_key":  photo_meta_by_key,
            }, f, ensure_ascii=False)

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
    # 解析使用者選的色系 {style_id: 色系中文名}（前端從 deco_directions 帶來）
    _palettes: dict = {}
    if palettes_json.strip():
        try:
            _p = json.loads(palettes_json)
            if isinstance(_p, dict):
                _palettes = {str(k): str(v)[:40] for k, v in _p.items() if v}
        except Exception as _pe:
            print(f"[/api/job] palettes_json 解析失敗，忽略: {_pe}")

    background_tasks.add_task(run_pipeline, job_id, new_paths, styles_list, plan,
                              space_type, render_angle, design_mode,
                              user_zoning_v2, layout_choice,
                              budget_tier, customer_notes, preferred_store,
                              upload_id, palettes=_palettes)

    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def get_status(job_id: str):
    # 優先讀 Supabase
    row = sb_get(job_id)
    if row:
        # 懶 watchdog：非終態但太久沒進度更新（跨過啟動掃描後才卡死的單）→ 當場標 failed，
        # 讓前端 polling 拿到明確失敗而不是永遠轉圈
        if row.get("status") not in ("completed", "failed", "incomplete"):
            try:
                from datetime import datetime, timezone
                upd = row.get("updated_at") or ""
                ts = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                if age_min > STALE_JOB_MINUTES:
                    msg = "生成中斷（系統重啟或逾時），請聯絡客服協助重新處理"
                    sb_upsert({"job_id": job_id, "status": "failed", "progress": 0, "message": msg})
                    return {"status": "failed", "progress": 0, "message": msg}
            except Exception:
                pass
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
def get_error(job_id: str, token: str = ""):
    """內部除錯用：error.log 含完整 traceback（路徑/內部細節），不能公開。
    跟 /debug-health 共用 HEALTH_DEBUG_TOKEN；沒設 env 或 token 不對一律 404。"""
    expected = (os.environ.get("HEALTH_DEBUG_TOKEN") or "").strip()
    if not expected or token != expected:
        return JSONResponse(status_code=404, content={"error": "not found"})
    error_file = JOBS_DIR / job_id / "error.log"
    if not error_file.exists():
        return {"error": "no error log"}
    return {"log": error_file.read_text(encoding="utf-8", errors="replace")}


# ── Z2.1: 付款前分區確認用 ────────────────────────────────────────────────
@app.post("/api/zoning")
async def api_zoning(upload_id: str = Form(...),
                     photo_meta_json: str = Form(default="")):
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

    # #2: 把使用者標「客廳」的照片排到最前面，確保它一定被下載到 —— 否則客廳放第 4 張時
    # 會被 [:N] 截掉，分區圖只能落在前幾張(餐廳/臥室)。排序後客廳成為 best photo。
    ordered_urls = list(photo_urls)
    if photo_meta_json:
        try:
            _zmap0 = json.loads(photo_meta_json)
            if isinstance(_zmap0, dict):
                def _u_is_living(u: str) -> bool:
                    bn = (u.rsplit("/", 1)[-1] or "").split("?")[0]
                    return _zmap0.get(bn) == "living"
                living_first = [u for u in ordered_urls if _u_is_living(u)]
                rest = [u for u in ordered_urls if not _u_is_living(u)]
                ordered_urls = living_first + rest
        except Exception as _e0:
            print(f"[/api/zoning] photo 排序解析失敗，忽略: {_e0}")

    local_photos: list[Path] = []
    for i, url in enumerate(ordered_urls[:4]):
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
                    # zoning overlay / bbox 都以這份檔案為準，直拍不轉正 → 分區圖方向全錯
                    _normalize_photo_orientation(str(dest))
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

    # 3. Gemini zoning v2（2026-07-08 起：有影片就抽關鍵幀一起送——影片是拿來
    #    理解結構/方向/動線的，分區判讀是客戶第一眼看到的「AI 理解你家」，
    #    不能只看照片。全程 best-effort：任何失敗都退回純照片，不擋分區頁。）
    try:
        from zoning_v2 import compute_zoning_v2, draw_overlay
    except ImportError as e:
        return JSONResponse(status_code=500, content={"error": f"zoning module missing: {e}"})

    zoning_kf: list = []
    try:
        _vkeys = upload.get("video_keys") or []
        if _vkeys and isinstance(_vkeys[0], str) and _vkeys[0].strip():
            _vdest = tmp_dir / ("zv_" + (_vkeys[0].split("/")[-1] or "video.mp4"))
            if not _vdest.exists():
                r2_download_object(_vkeys[0].strip(), _vdest)
            if _vdest.exists() and _vdest.stat().st_size > 10240:
                kf_dir = tmp_dir / "zoning_kf"
                zoning_kf = extract_video_keyframes(str(_vdest), kf_dir, count=4)
                print(f"[/api/zoning] 影片關鍵幀 {len(zoning_kf)} 張加入 zoning 判讀")
    except Exception as _ve:
        print(f"[/api/zoning] 影片擷幀失敗（退回純照片）: {type(_ve).__name__}: {str(_ve)[:100]}")
        zoning_kf = []

    zoning = compute_zoning_v2(local_photos, video_keyframes=zoning_kf or None)
    if zoning.get("error"):
        return JSONResponse(status_code=500, content={"error": f"gemini zoning failed: {zoning['error']}"})

    # 4. 畫 overlay
    # #1/#5: 分區圖優先畫在使用者標「客廳(living)」那張，不要用第一張(可能是餐廳)。
    # photo_meta_json: {檔名: target_zone}，由前端從 deco_rooms 帶來。
    prefer_idx = None
    if photo_meta_json:
        try:
            _zmap = json.loads(photo_meta_json)
            if isinstance(_zmap, dict):
                for _i, _ph in enumerate(local_photos):
                    _z = _zmap.get(_ph.name) or _zmap.get(canonical_photo_key(str(_ph)))
                    if _z == "living":
                        prefer_idx = _i
                        break
        except Exception as _e:
            print(f"[/api/zoning] photo_meta_json 解析失敗，忽略: {_e}")
    best_idx = prefer_idx if prefer_idx is not None else zoning.get("best_photo_index", 0)
    if prefer_idx is not None:
        print(f"[/api/zoning] 分區圖採用使用者標的客廳照片 idx={prefer_idx}")
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
        storage_path = f"zoning/{upload_id}/{name}"
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/renders/{storage_path}"
        try:
            data = local.read_bytes()
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
                return public_url
            print(f"[/api/zoning] overlay 上傳 {name} 失敗 HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[/api/zoning] overlay 上傳 {name} 例外: {e}")
        # 上傳失敗最常見原因：renders bucket 的 RLS 只允許 anon INSERT、不允許 UPDATE，
        # 而 /api/zoning 每次頁面載入都會重打 → 同 upload_id 第二次起 upsert 一律 403，
        # 分區圖就「消失」（3ACB0DF4 抓漏）。既有檔案還在的話直接回舊 URL——
        # 同一 upload 的照片沒變，第一次畫的 overlay 依然正確。
        try:
            chk = _req.head(public_url, timeout=8)
            if chk.status_code == 200:
                print(f"[/api/zoning] overlay {name} 覆蓋被拒但舊檔存在 → 沿用舊 URL")
                return public_url
        except Exception:
            pass
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
    """公開端點（無驗證，任何人可打）。商業化收斂：只回 status 與 build 短碼，
    不回任何 env 名稱/bucket/key 是否設定等部署細節。診斷走 /debug-health。

    build 短碼（29ECD0B1 教訓）：push 後 /health 回 ok 的是「舊容器」——新版還在
    build，這時送進來的單會在容器切換時被殺。等部署要等 build 值變成新 commit，
    不能只看 ok。短碼 8 碼非機密（repo 私有，短 hash 無法反推程式碼）。"""
    sha = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:8]
    return {"status": "ok", "build": sha or "unknown"}


@app.get("/debug-health")
def debug_health(token: str = ""):
    """內部診斷端點。需要 Railway 設 HEALTH_DEBUG_TOKEN 且帶 ?token=<值> 才回內容；
    沒設 env 時一律 404（公開網路上等同不存在）。"""
    expected = (os.environ.get("HEALTH_DEBUG_TOKEN") or "").strip()
    if not expected or token != expected:
        return JSONResponse(status_code=404, content={"error": "not found"})

    ak, sk, ep, bucket = _r2_cfg()
    g_env = (os.environ.get("GEMINI_API_KEY") or "").strip()
    ga_env = (os.environ.get("GOOGLE_AI_KEY") or "").strip()
    used_key = g_env or ga_env
    used_source = "GEMINI_API_KEY" if g_env else ("GOOGLE_AI_KEY" if ga_env else None)

    return {
        "status": "ok",
        "gemini_key": "set" if used_key else "MISSING",
        "gemini_key_source": used_source,
        "fal_key":    "set" if os.environ.get("FAL_KEY") else "MISSING",
        "r2_access_key": "set" if ak else "MISSING",
        "r2_secret":     "set" if sk else "MISSING",
        "r2_endpoint":   "set" if ep else "MISSING",
        "r2_bucket":     bucket or "MISSING",
    }

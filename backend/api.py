# DECO168 FastAPI Backend
# 啟動: cd backend && python3.11 -m uvicorn api:app --reload --port 8000
import os, re, sys, json, uuid, shutil, traceback, hashlib, math

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

# ── 保留期自動清理：storage 檔案與訂單內容都只留 RETENTION_DAYS 天
#    （2026-07 超額 4.3GB 被 Supabase 停權事故的根治）。每次部署啟動時跑，
#    在背景 thread 執行避免拖慢啟動健康檢查。
#
#    2026-08-03 由 14 改 30 天：實測 30 天保留 121 筆訂單（14 天只留 48 筆），
#    資料庫仍只有 1.8MB。之所以還這麼小，是因為撐爆 DB 的 400MB base64
#    （reference_map 存整張照片）全在 6 月，而 30 天前正好是 7/04，整批都在
#    清理範圍內。再往後放就會踩到 6 月尾巴：45 天 → 230MB、60 天 → 400MB。
#    storage 與 orders 共用同一個天數，不要兩邊各寫各的。 ──
RETENTION_DAYS = int(os.environ.get("STORAGE_RETENTION_DAYS") or "30")
STORAGE_RETENTION_DAYS = RETENTION_DAYS      # 舊名保留，避免其他引用處壞掉

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
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
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
            print(f"[storage-cleanup] 已清 {total} 個超過 {RETENTION_DAYS} 天的檔案")
    except Exception as e:
        print(f"[storage-cleanup] 清理失敗（不影響服務）: {type(e).__name__}: {str(e)[:150]}")

def _purge_expired_orders():
    """訂單內容也只留 RETENTION_DAYS 天——storage 早就在清了，DB 從來沒清過。

    2026-08-03 實測：232 筆訂單佔 450MB（免費版上限 500MB 的 90%），其中
    7/16 前的 175 筆就佔 400MB——幾乎全是 6 月「reference_map 存 base64 整張照片」
    的歷史包袱（6 月底已修，7 月起每筆只剩 14KB）。storage 的 14 天清理只刪檔案，
    orders 這張表沒人清，所以一路累積。

    **只清空 result_json，保留這一列**（job_id/status/created_at）：
      * 清空後每列只剩約 200 bytes，一年幾千單也才幾百 KB，等於不佔空間。
      * 保留列才能讓結果頁回「這張設計已超過保留期」，而不是 404 或壞掉的空白頁。
        客戶把網址存書籤、三週後回來點開，看到「已過期」比看到錯誤好。
    """
    from datetime import datetime, timedelta, timezone
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=RETENTION_DAYS)).isoformat()
        r = _req.patch(
            f"{SUPABASE_URL}/rest/v1/orders",
            params={"created_at": f"lt.{cutoff}", "result_json": "not.is.null"},
            json={"result_json": None,
                  "message": f"此設計已超過 {RETENTION_DAYS} 天保留期，如需重新產出請聯絡客服"},
            headers={**_SB_HEADERS, "Prefer": "return=headers-only,count=exact"},
            timeout=60,
        )
        if r.ok:
            rng = r.headers.get("content-range") or ""
            print(f"[orders-cleanup] 已清空超過 {RETENTION_DAYS} 天的訂單內容 ({rng})")
        else:
            print(f"[orders-cleanup] 清理失敗 HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        print(f"[orders-cleanup] 清理失敗（不影響服務）: {type(e).__name__}: {str(e)[:150]}")


@app.on_event("startup")
def _startup_watchdog():
    _sweep_stale_jobs()
    import threading
    threading.Thread(target=_purge_expired_storage, daemon=True).start()
    threading.Thread(target=_purge_expired_orders, daemon=True).start()


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


def _source_file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _zoning_bbox_matches_source(source_path: str, image_paths: list,
                                 zoning: dict | None) -> bool:
    """Bind S2 geometry to the exact source bytes; array indexes are not evidence."""
    if not source_path or not image_paths or not isinstance(zoning, dict):
        return False
    binding = zoning.get("_source_binding")
    if not isinstance(binding, dict):
        return False
    expected_key = canonical_photo_key(binding.get("photo_key"))
    expected_sha = str(binding.get("sha256") or "").strip().lower()
    if not expected_key or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        return False
    try:
        source = Path(source_path).resolve()
        allowed = {Path(item).resolve() for item in image_paths if isinstance(item, str)}
        return source in allowed and source.is_file() and _source_file_sha256(source) == expected_sha
    except Exception:
        return False


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
    sofa_side_source = str(pz_living.get("sofa_side_source") or "").strip().lower()
    # zoning 頁會預先勾選 Gemini 建議；只有客戶真的點左／右才是硬綁定。
    # 舊資料沒有 source 時維持原行為，避免回溯訂單被偷偷換側。
    _sofa_free = (
        str(pz_living.get("sofa_side") or "").strip().lower() == "free"
        or sofa_side_source == "ai_default"
    )
    if layout_choice == "B":
        sofa_side = _norm_side(pz_living.get("alt_sofa_side")) or _norm_side(pz_living.get("sofa_side"))
        tv_side   = _norm_side(pz_living.get("alt_tv_side"))   or _norm_side(pz_living.get("tv_side"))
    else:
        sofa_side = _norm_side(pz_living.get("sofa_side"))
        tv_side   = _norm_side(pz_living.get("tv_side"))
    recommended_sofa_side = sofa_side
    recommended_tv_side = tv_side
    if sofa_side_source == "ai_default":
        sofa_side = ""
        tv_side = ""
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
            "sofa_side_source":         sofa_side_source,
            "recommended_sofa_side":    recommended_sofa_side,
            "recommended_tv_side":      recommended_tv_side,
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


def _door_block_offender(validation: dict | None) -> str | None:
    """貼門對象：'sofa' | 'focal_anchor' | None。

    只信幾何 `_door_adjacency_violation`（回傳誰貼門）。判官布林
    furniture_blocks_door=True 但 bbox 量不出誰 → None，禁止猜錯對象去修。
    """
    try:
        from gemini_analyze import _door_adjacency_violation
        viol = _door_adjacency_violation((validation or {}).get("render_bboxes") or {})
    except Exception:
        return None
    if not viol:
        return None
    name = viol[0]
    return name if name in ("sofa", "focal_anchor") else None


def _local_edit_structure_ok(validation: dict | None) -> bool:
    """局部位移／遮罩硬修共用的結構保真門檻。"""
    v = validation or {}
    if v.get("camera_axis_preserved") is False or v.get("passage_openings_preserved") is False:
        return False
    if any(v.get(k) for k in (
        "spatial_fidelity_fail", "windows_changed", "walls_changed", "ceiling_changed",
        "floor_changed", "offframe_room_invaded",
    )):
        return False
    return True


def _sofa_alignment_edit_base(validation: dict | None, render: dict | None,
                               room_type: str = "living") -> str | None:
    """沙發位置硬傷時，回傳上一張 render 做局部位移底圖。

    593408CC：furniture_blocks_door 必須再分誰貼門——只有沙發貼門才走這條；
    電視櫃貼門交給 `_console_alignment_edit_base`，絕不可移沙發。
    """
    v = validation or {}
    r = render or {}
    if (room_type or "living") != "living":
        return None
    _door_jam = v.get("furniture_blocks_door") is True
    _offender = _door_block_offender(v) if _door_jam else None
    # 電視櫃貼門 → 沙發路徑明確退出
    if _door_jam and _offender == "focal_anchor":
        return None
    # 貼門但量不出對象 → 不猜，不走沙發位移（避免 593408CC 四次修錯）
    if _door_jam and _offender is None:
        return None
    _sofa_door_jam = _door_jam and _offender == "sofa"
    # 8AD3E711：沙發貼錯邊(sofa_on_wrong_side)之前沒有任何硬修路徑——閘門擋、無救生圈。
    # 加為觸發，讓沙發走遮罩硬修；下游 _s2_repair_target_box 認出 wrong_side 後，
    # 目標改指 contract 對牆 footprint（真跨房搬移），而不是拿當前沙發同牆往深處滑。
    _sofa_wrong_side = v.get("sofa_on_wrong_side") is True
    if not (v.get("sofa_facing_entrance_door") is True
            or v.get("focal_anchor_misaligned_with_sofa") is True
            or _sofa_wrong_side
            or _sofa_door_jam):
        return None
    # 40063497 Fix A 收窄：只有「沙發貼門」時才豁免 TV 深度欄位。
    # 8AD3E711 補洞（Grok 抓到）：沙發貼錯邊也豁免——跨房搬的是沙發、不碰 TV，
    # past_door 缺失或 False 時仍須 engage，否則「code 有、單測綠、真單不走」。
    # 電視櫃真貼門的情況已在上面 offender==focal_anchor 先 return，這裡放行不會誤修 TV。
    if (not _sofa_door_jam and not _sofa_wrong_side
            and v.get("focal_anchor_past_door_in_depth") is not True):
        return None
    if not _local_edit_structure_ok(v):
        return None
    rb = v.get("render_bboxes") or {}
    if not rb.get("sofa") or not rb.get("focal_anchor"):
        return None
    path = str(r.get("render_path") or "")
    return path if path and Path(path).exists() else None


def _console_alignment_edit_base(validation: dict | None, render: dict | None,
                                  room_type: str = "living") -> str | None:
    """593408CC：電視櫃／焦點櫃貼門 → 回傳上一張 render 做「只修電視櫃」底圖。

    觸發條件嚴格：furniture_blocks_door 且幾何 offender=focal_anchor。
    沙發鎖定；不得走沙發位移路徑。
    """
    v = validation or {}
    r = render or {}
    if (room_type or "living") != "living":
        return None
    if v.get("furniture_blocks_door") is not True:
        return None
    if _door_block_offender(v) != "focal_anchor":
        return None
    axis_conflict = isinstance(v.get("focal_door_axis_conflict"), dict)
    if not _local_edit_structure_ok(v):
        return None
    # 沙發本身也貼門／對門／錯邊時，先別鎖沙發硬修櫃——交給完整重生或沙發路徑
    if any(v.get(flag) is True for flag in (
        "sofa_on_wrong_side", "sofa_outside_living_zone",
        "sofa_back_against_window", "sofa_faces_walkway", "sofa_intrudes_walkway",
        "furniture_blocks_walkway",
    )):
        return None
    if v.get("sofa_facing_entrance_door") is True and not axis_conflict:
        return None
    # 一般避門修復只保留已通過的對正；唯一例外是 code 已量到的
    # focal_door_axis_conflict，此時同一個藍框同時解門距與對向，不靠模型猜。
    if (not axis_conflict
            and (v.get("focal_anchor_misaligned_with_sofa") is True
                 or _pair_center_delta(v, tolerance=PAIR_CENTER_EXTREME))):
        return None
    rb = v.get("render_bboxes") or {}
    if not rb.get("sofa") or not rb.get("focal_anchor") or not rb.get("entrance_door"):
        return None
    if _console_door_clearance_target_box(v, 1000, 1000) is None:
        return None
    path = str(r.get("render_path") or "")
    return path if path and Path(path).exists() else None


def _console_door_clearance_target_box(
    validation: dict | None,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """門後 0.28 門寬起算的電視櫃目標框（像素）。純 bbox，不靠 S2 contract。"""
    try:
        from gemini_analyze import DOOR_GAP_MIN_FOCAL
    except Exception:
        DOOR_GAP_MIN_FOCAL = 0.28
    boxes = (validation or {}).get("render_bboxes") or {}
    focal = boxes.get("focal_anchor")
    door = boxes.get("entrance_door")
    if (not isinstance(focal, list) or len(focal) != 4
            or not isinstance(door, list) or len(door) != 4
            or width <= 1 or height <= 1):
        return None
    fy0, fx0, fy1, fx1 = [float(v) for v in focal]
    dy0, dx0, dy1, dx1 = [float(v) for v in door]
    door_w = max(1.0, dx1 - dx0)
    cons_w = max(1.0, fx1 - fx0)
    cons_h = max(1.0, fy1 - fy0)
    original_wall_side = "left" if (fx0 + fx1) / 2.0 < 500.0 else "right"
    gap = DOOR_GAP_MIN_FOCAL * door_w
    door_cx = (dx0 + dx1) / 2.0
    # 門在畫面左半 → 櫃體推到門右緣之後；右半 → 推到門左緣之前。
    # 目標框放不下原櫃寬度就是無解，不縮櫃、不付費抽獎。
    if door_cx <= 500.0:
        new_x0 = dx1 + gap
        new_x1 = new_x0 + cons_w
        if new_x1 > 980.0:
            return None
    else:
        new_x1 = dx0 - gap
        new_x0 = new_x1 - cons_w
        if new_x0 < 20.0:
            return None
    # 同牆修復的最低幾何保證：目標中心不得跨過畫面中線。E4706B43 的
    # correction map 沒真正送進模型後，櫃體從左牆跳到右牆；若目標本身已經
    # 需要跨半邊，代表原牆沒有保住完整櫃寬的安全位置，應在付費前停止。
    target_wall_side = "left" if (new_x0 + new_x1) / 2.0 < 500.0 else "right"
    if target_wall_side != original_wall_side:
        return None
    axis_conflict = isinstance((validation or {}).get("focal_door_axis_conflict"), dict)
    if axis_conflict:
        # C4AA16B8：門距與對向同時失敗時，藍框一次解兩個已量測問題——
        # 櫃體仍在原牆，水平推過門禁區，前後位置則與沙發中心對齊。
        sofa = boxes.get("sofa")
        if not isinstance(sofa, (list, tuple)) or len(sofa) != 4:
            return None
        try:
            sofa_cy = (float(sofa[0]) + float(sofa[2])) / 2.0
        except (TypeError, ValueError):
            return None
        new_y0 = sofa_cy - cons_h / 2.0
        new_y1 = sofa_cy + cons_h / 2.0
        if new_y0 < 20.0:
            new_y1 += 20.0 - new_y0
            new_y0 = 20.0
        if new_y1 > 980.0:
            new_y0 -= new_y1 - 980.0
            new_y1 = 980.0
    else:
        # 單純避門且原本對向已通過：只修 x，既有 y/depth 原封不動。
        new_y0 = fy0
        new_y1 = fy1
    if new_x1 <= new_x0 + 8 or new_y1 <= new_y0 + 8:
        return None
    cons_w_px = max(1, round(cons_w * width / 1000.0))
    if door_cx <= 500.0:
        # 左門的安全邊界要向右取整，不可四捨五入回門禁區 1px。
        out_x0 = max(0, math.ceil(new_x0 * width / 1000.0))
        out_x1 = min(width - 1, out_x0 + cons_w_px)
    else:
        # 右門鏡像：安全邊界向左取整。
        out_x1 = min(width - 1, math.floor(new_x1 * width / 1000.0))
        out_x0 = max(0, out_x1 - cons_w_px)
    return (
        out_x0,
        max(0, round(new_y0 * height / 1000.0)),
        out_x1,
        min(height - 1, round(new_y1 * height / 1000.0)),
    )


def _build_console_door_edit_mask(
    previous_render_path: str,
    validation: dict | None,
    output_path: str,
) -> str | None:
    """Fal 黑白遮罩：白=舊櫃區+門後目標區可編輯；黑=沙發/大門/其餘建築鎖定。"""
    try:
        from PIL import Image, ImageDraw

        previous = Path(previous_render_path)
        boxes = (validation or {}).get("render_bboxes") or {}
        focal = boxes.get("focal_anchor")
        door = boxes.get("entrance_door")
        sofa = boxes.get("sofa")
        if (not previous.is_file()
                or not isinstance(focal, list) or len(focal) != 4
                or not isinstance(door, list) or len(door) != 4):
            return None
        with Image.open(previous) as opened:
            width, height = opened.size
        target_box = _console_door_clearance_target_box(validation, width, height)
        if not target_box:
            return None
        fy0, fx0, fy1, fx1 = [float(v) for v in focal]
        pad_x, pad_y = width * 0.01, height * 0.01
        old_edit = (
            max(0, int(fx0 * width / 1000.0 - pad_x)),
            max(0, int(fy0 * height / 1000.0 - pad_y)),
            min(width, int(fx1 * width / 1000.0 + pad_x)),
            min(height, int(fy1 * height / 1000.0 + pad_y)),
        )
        tx0, ty0, tx1, ty1 = target_box
        target_edit = (
            max(0, int(tx0 - pad_x)), max(0, int(ty0 - pad_y)),
            min(width, int(tx1 + pad_x)), min(height, int(ty1 + pad_y)),
        )
        # 舊櫃→新櫃之間的走廊也打開，避免中間留下幽靈櫃
        corridor = (
            min(old_edit[0], target_edit[0]),
            min(old_edit[1], target_edit[1]),
            max(old_edit[2], target_edit[2]),
            max(old_edit[3], target_edit[3]),
        )
        # fal openai/gpt-image-2/edit 的 mask 規格是「白色可編輯、黑色保留」。
        # 舊版把 RGB 全畫黑、只改 alpha；fal 不以 alpha 當 edit 區，硬鎖實際沒有成立。
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle(corridor, fill=255)
        # 大門必須鎖死（黑色）
        dy0, dx0, dy1, dx1 = [float(v) for v in door]
        door_pad_x, door_pad_y = width * 0.008, height * 0.008
        locked_door = (
            max(0, int(dx0 * width / 1000.0 - door_pad_x)),
            max(0, int(dy0 * height / 1000.0 - door_pad_y)),
            min(width, int(dx1 * width / 1000.0 + door_pad_x)),
            min(height, int(dy1 * height / 1000.0 + door_pad_y)),
        )
        draw.rectangle(locked_door, fill=0)
        # 沙發鎖死
        if isinstance(sofa, list) and len(sofa) == 4:
            sy0, sx0, sy1, sx1 = [float(v) for v in sofa]
            sofa_pad_x, sofa_pad_y = width * 0.006, height * 0.01
            locked_sofa = (
                max(0, int(sx0 * width / 1000.0 - sofa_pad_x)),
                max(0, int(sy0 * height / 1000.0 - sofa_pad_y)),
                min(width, int(sx1 * width / 1000.0 + sofa_pad_x)),
                min(height, int(sy1 * height / 1000.0 + sofa_pad_y)),
            )
            draw.rectangle(locked_sofa, fill=0)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mask.save(target, "PNG")
        return str(target)
    except Exception as exc:
        print(f"[pipeline] console door edit mask failed: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def _build_console_door_repair_guide(
    previous_render_path: str,
    validation: dict | None,
    output_path: str,
) -> str | None:
    """舊電視櫃紅框 + 門後目標藍框，給 GPT Image 2 局部硬修參考。"""
    try:
        from PIL import Image, ImageDraw, ImageFont

        previous = Path(previous_render_path)
        boxes = (validation or {}).get("render_bboxes") or {}
        focal = boxes.get("focal_anchor")
        door = boxes.get("entrance_door")
        if (not previous.is_file()
                or not isinstance(focal, list) or len(focal) != 4
                or not isinstance(door, list) or len(door) != 4):
            return None
        with Image.open(previous) as opened:
            image = opened.convert("RGBA")
        w, h = image.width, image.height
        target_box = _console_door_clearance_target_box(validation, w, h)
        if not target_box:
            return None
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")
        fy0, fx0, fy1, fx1 = [float(v) for v in focal]
        old_box = [
            round(fx0 * w / 1000), round(fy0 * h / 1000),
            round(fx1 * w / 1000), round(fy1 * h / 1000),
        ]
        draw.rectangle(old_box, fill=(220, 25, 25, 120), outline=(240, 20, 20, 255), width=5)
        tx0, ty0, tx1, ty1 = target_box
        draw.rectangle([tx0, ty0, tx1, ty1], fill=(40, 110, 230, 120),
                       outline=(30, 90, 220, 255), width=5)
        dy0, dx0, dy1, dx1 = [float(v) for v in door]
        door_box = [
            round(dx0 * w / 1000), round(dy0 * h / 1000),
            round(dx1 * w / 1000), round(dy1 * h / 1000),
        ]
        draw.rectangle(door_box, outline=(240, 20, 20, 255), width=4)
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        draw.text((old_box[0] + 6, max(4, old_box[1] - 14)),
                  "OLD TV/CONSOLE - REMOVE", fill=(240, 20, 20, 255), font=font)
        draw.text((tx0 + 6, max(4, ty0 - 14)),
                  "BLUE CONSOLE TARGET - PAST DOOR", fill=(30, 90, 220, 255), font=font)
        composed = Image.alpha_composite(image, overlay).convert("RGB")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        composed.save(target, "JPEG", quality=92, optimize=True)
        return str(target)
    except Exception as exc:
        print(f"[pipeline] console door repair guide failed: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def _activate_console_door_edit(
    validation: dict | None,
    render: dict | None,
    entry: dict | None,
    job_dir: str,
    idx: int,
    attempt_tag: str,
) -> str | None:
    """電視櫃貼門 → 底圖 + 遮罩 + 引導圖。成功回傳 base path。"""
    e = entry or {}
    base = _console_alignment_edit_base(validation, render, e.get("_room_type", "living"))
    if not base:
        return None
    mask = _build_console_door_edit_mask(
        base, validation,
        str(Path(job_dir) / f"mask_console_door_{idx:02d}_{attempt_tag}.png"),
    )
    guide = _build_console_door_repair_guide(
        base, validation,
        str(Path(job_dir) / f"guide_console_door_{idx:02d}_{attempt_tag}.jpg"),
    )
    # 任一安全元件缺失都不可退化成純 prompt 付費抽獎。
    if not mask or not guide:
        print("[pipeline] console door edit skipped before generation: safe mask/guide unavailable")
        return None
    boxes = (validation or {}).get("render_bboxes") or {}
    focal = boxes.get("focal_anchor") or []
    if isinstance(focal, (list, tuple)) and len(focal) == 4:
        focal_cx = (float(focal[1]) + float(focal[3])) / 2.0
        e["_console_repair_wall_side"] = "left" if focal_cx < 500.0 else "right"
    e["_edit_mask_path"] = mask
    e["_edit_mask_mode"] = "console_door"
    e["_consistency_ref_path"] = guide
    e["_s2_retry_artifacts_active"] = True
    # S2 未開時也強制這次用可吃 mask 的局部修。
    e["_force_mask_local_edit"] = True
    print(f"[pipeline] console door edit: offender=focal_anchor "
          f"mask={'yes' if mask else 'no'} guide={'yes' if guide else 'no'}")
    return base


def _product_only_edit_base(validation: dict | None, render: dict | None,
                            room_type: str = "living") -> str | None:
    """40063497 銜接漏洞：只有商品保真失敗、幾何（門／走道／錯邊／背窗／結構）全過時，
    用「目前這張幾何已通過的成品」做局部商品修，而不是退回原始底圖全新重生——
    後者會把 Z3 已修好的門距丟掉、沙發又貼門。任何沙發位置／結構硬傷存在就不走這條
    （交給沙發位移路徑），避免在壞幾何上鎖圖。"""
    v = validation or {}
    r = render or {}
    if (room_type or "living") != "living":
        return None
    if v.get("product_visibility_fail") is not True:
        return None
    # 沙發位置類硬傷（含貼門/走道/錯邊/錯區/背窗/對窗/對門）任一存在 → 不鎖，交給沙發位移
    if any(v.get(flag) is True for flag in _PAIR_ALIGNMENT_SOFA_HARD_FAILURES):
        return None
    if any(v.get(k) for k in (
        "spatial_fidelity_fail", "windows_changed", "walls_changed", "ceiling_changed",
        "floor_changed", "offframe_room_invaded", "furniture_blocks_walkway",
        "recessed_space_added", "sofa_faces_walkway", "coffee_table_in_walkway",
        "focal_anchor_misaligned_with_sofa", "guide_overlay_present",
        "sofa_facing_window_unverified",
    )):
        return None
    path = str(r.get("render_path") or "")
    return path if path and Path(path).exists() else None


def _s2_door_clearance_shift_px(validation: dict | None, width: int, sofa_side: str) -> int:
    boxes = (validation or {}).get("render_bboxes") or {}
    sofa = boxes.get("sofa")
    door = boxes.get("entrance_door")
    if (not isinstance(sofa, list) or len(sofa) != 4
            or not isinstance(door, list) or len(door) != 4):
        return 0
    _, sofa_x0, _, sofa_x1 = [float(value) for value in sofa]
    _, door_x0, _, door_x1 = [float(value) for value in door]
    door_width = max(0.0, door_x1 - door_x0)
    if sofa_side == "left":
        current_gap = sofa_x0 - door_x1
        shift_norm = max(0.0, 0.25 * door_width - current_gap) + 10.0
        return round(shift_norm * width / 1000.0)
    current_gap = door_x0 - sofa_x1
    shift_norm = max(0.0, 0.25 * door_width - current_gap) + 10.0
    return -round(shift_norm * width / 1000.0)


def _s2_entrance_no_go_points(contract: dict | None, width: float, height: float):
    """合約裡那**唯一一份**門前禁區，縮放到 width×height 的畫布座標。

    規劃器（layout_geometry_s2.entrance_hard_no_go_polygon）算一次、寫進合約
    geometry；引導圖、修復遮罩、修復目標框、生成後驗收全部讀這一份，沒有人
    自己再算。合約幾何在 source_px_xy，而 S2 的 model_input 是宣告過的 identity
    transform、付費前檢又比對過 base 與 source 的 sha256／size 完全相同，
    所以等比例縮放就對得上成品座標。

    ⚠️ 回 None 只代表「這單算不出禁區」（門開在進深端／標記退化），
    **不代表安全**——呼叫端一律維持既有行為，不得因此放寬任何閘門。
    """
    if not isinstance(contract, dict) or width <= 0 or height <= 0:
        return None
    shape = next(
        ((item or {}).get("shape") or {} for item in contract.get("geometry") or []
         if isinstance(item, dict) and item.get("geometry_id") == "entrance_hard_no_go"),
        None,
    )
    points = (shape or {}).get("coordinates")
    if not points or len(points) < 3:
        return None
    size = (contract.get("source") or {}).get("size") or {}
    try:
        source_w = float(size.get("width") or 0)
        source_h = float(size.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if source_w <= 0 or source_h <= 0:
        return None
    return [(float(x) * width / source_w, float(y) * height / source_h)
            for x, y in points]


def _box_hits_entrance_no_go(box, no_go_points) -> bool:
    """矩形 (x0,y0,x1,y1) 有沒有壓到門前禁區。禁區缺席一律回 False（＝維持現狀）。"""
    if not box or not no_go_points:
        return False
    from layout_geometry_s2 import _polygon_intersects
    x0, y0, x1, y1 = [float(value) for value in box]
    return _polygon_intersects(
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
        [(float(px), float(py)) for px, py in no_go_points],
    )


def _s2_repair_target_box(
    validation: dict | None,
    width: int,
    height: int,
    sofa_side: str,
    contract_target_points: list[tuple[float, float]],
    compact_entry_mode: bool = False,
    prefer_contract_target: bool = False,
) -> tuple[int, int, int, int] | None:
    sofa = ((validation or {}).get("render_bboxes") or {}).get("sofa")
    if not isinstance(sofa, list) or len(sofa) != 4 or not contract_target_points:
        return None
    # 8AD3E711：沙發貼錯邊 → 目標＝contract 對牆 footprint 的外接框（真跨房搬移），
    # 不是拿當前沙發同牆滑。footprint 點已由呼叫端縮放＋門距位移過。
    #
    # prefer_contract_target：呼叫端依「模型實際畫出來的沙發寬度」重挑到另一個
    # 容得下它的候選時，也要走同一條真搬移路徑。否則下面那段只會把**當前**沙發
    # 沿同一面牆推一下（old_w*0.83 + door_clearance_shift），完全不看新候選——
    # 09B924C4 四次修復門距 0→15→145→0 在門邊來回彈，就是因為它從頭到尾
    # 都在同一面牆上滑，從來沒有真的換過位置。
    if (validation or {}).get("sofa_on_wrong_side") is True or prefer_contract_target:
        _xs = [float(pt[0]) for pt in contract_target_points]
        _ys = [float(pt[1]) for pt in contract_target_points]
        _fx0, _fx1, _fy0, _fy1 = min(_xs), max(_xs), min(_ys), max(_ys)
        if _fx1 - _fx0 >= 8 and _fy1 - _fy0 >= 8:
            return (
                max(0, round(_fx0)), max(0, round(_fy0)),
                min(width - 1, round(_fx1)), min(height - 1, round(_fy1)),
            )
    sy0, sx0, sy1, sx1 = [float(value) for value in sofa]
    old_x0, old_y0 = sx0 * width / 1000.0, sy0 * height / 1000.0
    old_x1, old_y1 = sx1 * width / 1000.0, sy1 * height / 1000.0
    old_w, old_h = max(1.0, old_x1 - old_x0), max(1.0, old_y1 - old_y0)
    gain = 4 if compact_entry_mode else 1
    shift_x = _s2_door_clearance_shift_px(validation, width, sofa_side) * gain
    target_w = old_w * 0.83
    target_h = old_h * 0.68
    if sofa_side == "left":
        x0 = old_x0 + max(0, shift_x)
        x1 = x0 + target_w
    else:
        x1 = old_x1 + min(0, shift_x)
        x0 = x1 - target_w
    # Moving deeper along a perspective wall is both horizontal and upward in image space.
    y1 = old_y1 - height * 0.102
    y0 = y1 - target_h
    return (
        max(0, round(x0)), max(0, round(y0)),
        min(width - 1, round(x1)), min(height - 1, round(y1)),
    )


def _measured_sofa_width_px(validation: dict | None, source_width: float) -> float | None:
    """模型這一次實際畫出來的沙發有多寬（換算回合約的原圖座標）。"""
    box = ((validation or {}).get("render_bboxes") or {}).get("sofa")
    if not (isinstance(box, (list, tuple)) and len(box) == 4 and source_width > 0):
        return None
    try:
        width = abs(float(box[3]) - float(box[1]))
    except (TypeError, ValueError):
        return None
    return (width / 1000.0 * source_width) if width > 0 else None


def _scale_polygon_about_centre(points, target_width: float):
    xs = [float(p[0]) for p in points]
    current = max(xs) - min(xs)
    if current <= 0 or target_width <= 0:
        return [[float(p[0]), float(p[1])] for p in points]
    factor = target_width / current
    cx = sum(float(p[0]) for p in points) / len(points)
    cy = sum(float(p[1]) for p in points) / len(points)
    return [[cx + (float(p[0]) - cx) * factor,
             cy + (float(p[1]) - cy) * factor] for p in points]


def _s2_candidate_for_measured_sofa(contract: dict | None,
                                    validation: dict | None) -> str | None:
    """用「模型實際畫出來的沙發寬度」重挑一個容得下它的候選。

    09B924C4：規劃叫模型在 64/1000 寬的框裡畫沙發，模型畫了 176——多出來的
    112 往左溢出，正好蓋在大門上。四次修復在門邊來回震盪（門距 0→15→145→0），
    因為每一次都拿**同一個過小的目標框**重畫。

    24 張真單量到模型一律畫得比目標大（中位 2.93x）。事前不可預測
    （單間 0.94–2.72 倍門寬，用全域係數推會毀掉僅有的成功單），
    事後很穩（單內多次渲染只差 13%）。所以第一張畫完之後才有資格重挑：
    把每個候選的 footprint 放大到實測寬度，挑門距餘裕最大的那個。

    回 None＝沒有更好的位置，維持原候選（不得因為重挑失敗就放寬任何閘門）。
    """
    if not isinstance(contract, dict):
        return None
    chosen_id = (contract.get("decision") or {}).get("chosen_candidate_id")
    source = contract.get("source") or {}
    size = source.get("size") or {}
    try:
        source_w = float(size.get("width") or 0)
        source_h = float(size.get("height") or 0)
    except (TypeError, ValueError):
        return None
    measured = _measured_sofa_width_px(validation, source_w)
    if not measured or source_h <= 0:
        return None
    geometry = {item.get("geometry_id"): item
                for item in contract.get("geometry") or [] if isinstance(item, dict)}

    def _coords(geometry_id):
        return ((geometry.get(geometry_id) or {}).get("shape") or {}).get("coordinates")

    door = next((((item or {}).get("shape") or {}).get("coordinates")
                 for item in contract.get("geometry") or []
                 if isinstance(item, dict) and item.get("kind") == "door_quad"), None)
    if not door:
        return None
    try:
        from layout_geometry_s2 import _shared_door_gap_margin
    except Exception:
        return None
    best_id, best_margin = None, None
    for candidate in contract.get("candidates") or []:
        if not isinstance(candidate, dict) or not candidate.get("eligible"):
            continue
        sofa = _coords(candidate.get("sofa_footprint_geometry_id"))
        tv = _coords(candidate.get("tv_footprint_geometry_id"))
        if not sofa or not tv:
            continue
        try:
            margin = _shared_door_gap_margin(
                _scale_polygon_about_centre(sofa, measured),
                _scale_polygon_about_centre(tv, measured * 0.55),
                door, int(source_w), int(source_h))
        except Exception:
            margin = None
        if margin is None or margin < 1.0:
            continue
        if best_margin is None or margin > best_margin:
            best_id, best_margin = candidate.get("candidate_id"), margin
    if not best_id or best_id == chosen_id:
        return None
    print(f"[repair] 依實測沙發寬 {measured:.0f}px 重挑候選："
          f"{chosen_id} → {best_id}（門距餘裕 {best_margin:.2f}）")
    return best_id


def _build_s2_sofa_repair_guide(
    previous_render_path: str,
    contract_path: str,
    output_path: str,
    validation: dict | None = None,
    compact_entry_mode: bool = False,
    candidate_id: str | None = None,
) -> str | None:
    """Overlay immutable Contract targets on the previous furnished render for local sofa repair."""
    try:
        from PIL import Image, ImageDraw

        previous = Path(previous_render_path)
        contract_file = Path(contract_path)
        if not previous.is_file() or not contract_file.is_file():
            return None
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
        # 第一張畫完之後才知道模型會畫多大；用實測寬度重挑一個容得下它的候選，
        # 否則修復會拿同一個過小的目標框重畫，家具照樣溢到門上（09B924C4）。
        _repicked = candidate_id or _s2_candidate_for_measured_sofa(contract, validation)
        chosen_id = (_repicked
                     or (contract.get("decision") or {}).get("chosen_candidate_id"))
        chosen = next(
            (item for item in contract.get("candidates") or []
             if item.get("candidate_id") == chosen_id),
            None,
        )
        if not chosen:
            return None
        geometry = {
            item.get("geometry_id"): item for item in contract.get("geometry") or []
            if isinstance(item, dict)
        }

        def _coords(geometry_id):
            return ((geometry.get(geometry_id) or {}).get("shape") or {}).get("coordinates")

        sofa = _coords(chosen.get("sofa_footprint_geometry_id"))
        tv = _coords(chosen.get("tv_footprint_geometry_id"))
        axis = _coords(chosen.get("view_axis_geometry_id"))
        landing_item = next(
            (item for item in contract.get("geometry") or []
             if item.get("kind") == "entrance_landing"),
            None,
        )
        landing = ((landing_item or {}).get("shape") or {}).get("coordinates")
        if not sofa or not tv or not axis or not landing:
            return None
        source_size = contract.get("source", {}).get("size") or {}
        source_w = float(source_size.get("width") or 0)
        source_h = float(source_size.get("height") or 0)
        if source_w <= 0 or source_h <= 0:
            return None
        with Image.open(previous) as opened:
            image = opened.convert("RGBA")
        sx, sy = image.width / source_w, image.height / source_h

        def _pts(points):
            return [(round(float(x) * sx), round(float(y) * sy)) for x, y in points]

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")
        landing_pts = _pts(landing)
        sofa_pts = _pts(sofa)
        tv_pts = _pts(tv)
        axis_pts = _pts(axis)
        draw.polygon(landing_pts, fill=(220, 25, 25, 125))
        draw.line(landing_pts + [landing_pts[0]], fill=(240, 20, 20, 255), width=5)
        # 門前禁區跟首渲引導圖畫的是同一塊（合約 geometry 的同一個 id）。
        # 修復時如果只護落腳區，模型就會把沙發推到門正前方那條走進來的路上。
        no_go_pts = _s2_entrance_no_go_points(contract, image.width, image.height)
        if no_go_pts:
            zone = [(round(x), round(y)) for x, y in no_go_pts]
            draw.polygon(zone, fill=(220, 25, 25, 110))
            draw.line(zone + [zone[0]], fill=(240, 20, 20, 255), width=6)
        sofa_side = next(
            (str(note).split("=", 1)[1] for note in chosen.get("notes") or []
             if str(note).startswith("sofa_side=")),
            "",
        )
        shift_x = _s2_door_clearance_shift_px(validation, image.width, sofa_side)
        sofa_pts = [(max(0, min(image.width - 1, x + shift_x)), y) for x, y in sofa_pts]
        current_sofa = ((validation or {}).get("render_bboxes") or {}).get("sofa")
        if isinstance(current_sofa, list) and len(current_sofa) == 4:
            sy0, sx0, sy1, sx1 = [float(value) for value in current_sofa]
            old_box = [
                (round(sx0 * image.width / 1000), round(sy0 * image.height / 1000)),
                (round(sx1 * image.width / 1000), round(sy1 * image.height / 1000)),
            ]
            draw.rectangle(old_box, fill=(220, 25, 25, 115), outline=(240, 20, 20, 255), width=5)
        target_box = _s2_repair_target_box(
            validation, image.width, image.height, sofa_side, sofa_pts,
            compact_entry_mode=compact_entry_mode,
            prefer_contract_target=bool(_repicked))
        # 目標框壓到門前禁區就不能用——那等於叫模型把沙發搬進門口。退回合約
        # footprint（規劃階段已通過 entrance_no_go_clear，必然在禁區外）。
        if target_box and _box_hits_entrance_no_go(target_box, no_go_pts):
            print("[repair] 沙發目標框落在門前禁區 → 改用合約 footprint")
            target_box = None
        if target_box:
            tx0, ty0, tx1, ty1 = target_box
            sofa_pts = [(tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1)]
        draw.polygon(sofa_pts, fill=(30, 190, 80, 125))
        draw.line(sofa_pts + [sofa_pts[0]], fill=(15, 155, 55, 255), width=6)
        composed = Image.alpha_composite(image, overlay).convert("RGB")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        composed.save(target, "JPEG", quality=92, optimize=True)
        return str(target)
    except Exception as exc:
        print(f"[pipeline] S2 sofa repair guide failed: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def _build_s2_sofa_edit_mask(
    previous_render_path: str,
    contract_path: str,
    validation: dict | None,
    output_path: str,
    compact_entry_mode: bool = False,
) -> str | None:
    """Build Fal mask: white sofa corridor, black architecture and entrance door."""
    try:
        from PIL import Image, ImageDraw

        previous = Path(previous_render_path)
        contract_file = Path(contract_path)
        boxes = (validation or {}).get("render_bboxes") or {}
        sofa_box = boxes.get("sofa")
        door_box = boxes.get("entrance_door")
        if (not previous.is_file() or not contract_file.is_file()
                or not isinstance(sofa_box, list) or len(sofa_box) != 4
                or not isinstance(door_box, list) or len(door_box) != 4):
            return None
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
        # ⚠️ 必須跟 _build_s2_sofa_repair_guide 用**完全相同的運算式**挑候選。
        # 8beef63 只讓引導圖依實測尺寸重挑，遮罩仍讀合約原本的 chosen_candidate_id：
        # 引導圖叫沙發搬到新位置，可編輯的白色遮罩卻還開在舊位置——模型想照做也
        # 沒有可畫的範圍。兩邊吃同一份 contract 與 validation，運算式一致就必然一致。
        _repicked = _s2_candidate_for_measured_sofa(contract, validation)
        chosen_id = (_repicked
                     or (contract.get("decision") or {}).get("chosen_candidate_id"))
        chosen = next((item for item in contract.get("candidates") or []
                       if item.get("candidate_id") == chosen_id), None)
        geometry = {item.get("geometry_id"): item for item in contract.get("geometry") or []
                    if isinstance(item, dict)}
        target_shape = ((geometry.get((chosen or {}).get("sofa_footprint_geometry_id")) or {})
                        .get("shape") or {}).get("coordinates")
        source_size = (contract.get("source") or {}).get("size") or {}
        source_w = float(source_size.get("width") or 0)
        source_h = float(source_size.get("height") or 0)
        if not target_shape or source_w <= 0 or source_h <= 0:
            return None
        with Image.open(previous) as opened:
            width, height = opened.size
        sy0, sx0, sy1, sx1 = [float(value) for value in sofa_box]
        target_points = [(float(x) * width / source_w, float(y) * height / source_h)
                         for x, y in target_shape]
        sofa_side = next(
            (str(note).split("=", 1)[1] for note in (chosen or {}).get("notes") or []
             if str(note).startswith("sofa_side=")),
            "",
        )
        shift_x = _s2_door_clearance_shift_px(validation, width, sofa_side)
        target_points = [(max(0, min(width - 1, x + shift_x)), y) for x, y in target_points]
        current_x0, current_y0 = sx0 * width / 1000.0, sy0 * height / 1000.0
        current_x1, current_y1 = sx1 * width / 1000.0, sy1 * height / 1000.0
        target_box = _s2_repair_target_box(
            validation, width, height, sofa_side, target_points,
            compact_entry_mode=compact_entry_mode,
            prefer_contract_target=bool(_repicked))
        # 與 _build_s2_sofa_repair_guide 同一套退回規則：目標框壓到門前禁區時，
        # 兩邊都改用（已位移的）合約 footprint。引導圖畫哪裡、遮罩就開哪裡，
        # 不能一邊叫沙發搬家、另一邊只在舊位置開洞（8beef63 那次的坑）。
        # ⚠️ 禁區**不塗黑**：現在那張沙發很可能正壓在禁區上，把禁區鎖死等於
        # 讓錯的沙發永遠擦不掉，修復必然失敗。
        no_go_pts = _s2_entrance_no_go_points(contract, width, height)
        if target_box and _box_hits_entrance_no_go(target_box, no_go_pts):
            _xs = [float(p[0]) for p in target_points]
            _ys = [float(p[1]) for p in target_points]
            target_box = (max(0, round(min(_xs))), max(0, round(min(_ys))),
                          min(width - 1, round(max(_xs))),
                          min(height - 1, round(max(_ys))))
        if not target_box:
            return None
        pad_x, pad_y = width * 0.006, height * 0.035
        old_edit_box = (
            max(0, int(current_x0 - pad_x)), max(0, int(current_y0 - pad_y)),
            min(width, int(current_x1 + pad_x)), min(height, int(current_y1 + pad_y)),
        )
        tx0, ty0, tx1, ty1 = target_box
        target_edit_box = (
            max(0, int(tx0 - pad_x)), max(0, int(ty0 - pad_y)),
            min(width, int(tx1 + pad_x)), min(height, int(ty1 + pad_y)),
        )
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle(old_edit_box, fill=255)
        draw.rectangle(target_edit_box, fill=255)
        dy0, dx0, dy1, dx1 = [float(value) for value in door_box]
        door_pad_x, door_pad_y = width * 0.008, height * 0.008
        locked_door = (
            max(0, int(dx0 * width / 1000.0 - door_pad_x)),
            max(0, int(dy0 * height / 1000.0 - door_pad_y)),
            min(width, int(dx1 * width / 1000.0 + door_pad_x)),
            min(height, int(dy1 * height / 1000.0 + door_pad_y)),
        )
        draw.rectangle(locked_door, fill=0)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mask.save(target, "PNG")
        return str(target)
    except Exception as exc:
        print(f"[pipeline] S2 sofa edit mask failed: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def _first_render_footprint_target_box(sofa_points_px, door_bbox_1000, width, height):
    """P1 首渲 sofa 目標框：永遠用 contract footprint 外接框（不走同牆滑）。
    門距對『目標 footprint vs 門』計算（Grok must-fix：不是對源照舊沙發），
    目標若太貼門 → 沿遠離門方向平移到 ≥0.25 門寬淨空。回 (x0,y0,x1,y1) 或 None。"""
    if not sofa_points_px:
        return None
    xs = [float(p[0]) for p in sofa_points_px]
    ys = [float(p[1]) for p in sofa_points_px]
    fx0, fx1, fy0, fy1 = min(xs), max(xs), min(ys), max(ys)
    if isinstance(door_bbox_1000, list) and len(door_bbox_1000) == 4:
        _, dx0, _, dx1 = [float(v) for v in door_bbox_1000]
        door_x0 = dx0 * width / 1000.0
        door_x1 = dx1 * width / 1000.0
        door_w = max(1.0, door_x1 - door_x0)
        need = 0.25 * door_w
        door_cx = (door_x0 + door_x1) / 2.0
        tgt_cx = (fx0 + fx1) / 2.0
        if tgt_cx >= door_cx:            # 目標在門右 → fx0 需 ≥ 門右緣 + need
            gap = fx0 - door_x1
            if gap < need:
                fx0 += (need - gap); fx1 += (need - gap)
        else:                            # 目標在門左 → fx1 需 ≤ 門左緣 - need
            gap = door_x0 - fx1
            if gap < need:
                fx0 -= (need - gap); fx1 -= (need - gap)
    fx0 = max(0.0, min(width - 1.0, fx0)); fx1 = max(0.0, min(width - 1.0, fx1))
    if fx1 - fx0 < 8 or fy1 - fy0 < 8:
        return None
    return (round(fx0), round(fy0), round(fx1), round(fy1))


def _build_s2_first_render_mask(base_path: str, contract_path: str,
                                source_furniture: dict | None,
                                output_path: str) -> str | None:
    """P1 首渲硬綁 mask（新函式，不重用 retry 版）：
      白色(可重畫) = 偵測到的舊 sofa + coffee_table（清掉黏著的原物）
                    + S2 sofa footprint 目標區（永遠 footprint，門距對目標算）
      黑色(鎖死) = 偵測到的大門 + 其餘一切
    生成端用 mask_mode='first_render_layout' 保留完整 design/商品 prompt、image_1=源照。
    無 contract sofa footprint / 建不出目標 → 回 None，呼叫端 skip（正常首渲，不 crash）。
    v1 不動 TV：實牆 TV 通常在對位，避免雙櫃；焦點 erase 留 v2。"""
    try:
        from PIL import Image, ImageDraw

        base = Path(base_path)
        cf = Path(contract_path)
        if not base.is_file() or not cf.is_file():
            return None
        contract = json.loads(cf.read_text(encoding="utf-8"))
        chosen_id = (contract.get("decision") or {}).get("chosen_candidate_id")
        chosen = next((it for it in contract.get("candidates") or []
                       if it.get("candidate_id") == chosen_id), None)
        if not chosen:
            return None
        geometry = {it.get("geometry_id"): it for it in contract.get("geometry") or []
                    if isinstance(it, dict)}
        sofa_fp = (((geometry.get(chosen.get("sofa_footprint_geometry_id")) or {})
                    .get("shape") or {}).get("coordinates"))
        src = (contract.get("source") or {}).get("size") or {}
        sw = float(src.get("width") or 0)
        sh = float(src.get("height") or 0)
        if not sofa_fp or sw <= 0 or sh <= 0:
            return None
        with Image.open(base) as im:
            width, height = im.size
        scale_x, scale_y = width / sw, height / sh
        sofa_pts = [(float(x) * scale_x, float(y) * scale_y) for x, y in sofa_fp]
        sf = source_furniture or {}
        target = _first_render_footprint_target_box(
            sofa_pts, sf.get("entrance_door"), width, height)
        if not target:
            return None

        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        pad_x, pad_y = width * 0.006, height * 0.02

        def _erase(bbox_1000):
            if not (isinstance(bbox_1000, list) and len(bbox_1000) == 4):
                return
            y0, x0, y1, x1 = [float(v) for v in bbox_1000]
            draw.rectangle(
                (max(0, int(x0 * width / 1000.0 - pad_x)),
                 max(0, int(y0 * height / 1000.0 - pad_y)),
                 min(width, int(x1 * width / 1000.0 + pad_x)),
                 min(height, int(y1 * height / 1000.0 + pad_y))),
                fill=255)

        # 清掉黏著的原沙發 + 原茶几（外觀+位置一起清）
        _erase(sf.get("sofa"))
        _erase(sf.get("coffee_table"))
        # 重畫區：sofa footprint 目標（門距已對目標調過）
        tx0, ty0, tx1, ty1 = target
        draw.rectangle(
            (max(0, int(tx0 - pad_x)), max(0, int(ty0 - pad_y)),
             min(width, int(tx1 + pad_x)), min(height, int(ty1 + pad_y))),
            fill=255)
        # 鎖死大門（最後畫，overlap 時黑色勝出）
        door = sf.get("entrance_door")
        if isinstance(door, list) and len(door) == 4:
            dy0, dx0, dy1, dx1 = [float(v) for v in door]
            dpx, dpy = width * 0.008, height * 0.008
            draw.rectangle(
                (max(0, int(dx0 * width / 1000.0 - dpx)),
                 max(0, int(dy0 * height / 1000.0 - dpy)),
                 min(width, int(dx1 * width / 1000.0 + dpx)),
                 min(height, int(dy1 * height / 1000.0 + dpy))),
                fill=0)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        mask.save(out, "PNG")
        return str(out)
    except Exception as exc:
        print(f"[pipeline] first-render mask failed: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def _skip_unmodelable_extra_repair(render: dict | None) -> bool:
    """#2 提早失敗：S2 建不了模型的房(斜角/碎牆) 走 legacy，門/對門硬傷靠 legacy 補生
    救不回（90112824 實測門距越補越糟）。Z3 已試過一輪 → 不再燒 Phase2/Phase3 額外補生，
    省 fal，改走誠實「重拍引導」（result.html #1）。"""
    r = render or {}
    v = r.get("validation") or {}
    return bool(r.get("_s2_unmodelable") is True
                and (v.get("furniture_blocks_door") is True
                     or v.get("sofa_facing_entrance_door") is True))


def _clear_s2_retry_edit_artifacts(entry: dict) -> None:
    """Clear only dynamic S2 retry artifacts before selecting a new edit base."""
    if entry.get("_s2_retry_artifacts_active") is not True:
        return
    entry.pop("_edit_mask_path", None)
    entry.pop("_edit_mask_mode", None)
    entry.pop("_force_mask_local_edit", None)
    entry.pop("_consistency_ref_path", None)
    entry.pop("_s2_retry_artifacts_active", None)


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


def _legacy_render_model() -> str:
    """legacy 這條路沒有 S2 contract，模型純由 `RENDER_MODEL` 決定。

    刻意呼叫 `_resolve_render_model(None)` 而不是自己讀 env——連「沒設時預設
    banana」這個行為都必須跟生成端同一份，否則裁切會照著另一個模型的比例裁。"""
    try:
        from test_full_pipeline import _resolve_render_model
        return _resolve_render_model(None)
    except Exception:
        return (os.environ.get("RENDER_MODEL") or "fal-ai/nano-banana-pro/edit").strip()


# 唯一「輸出比例可證」的模型。白名單而非黑名單：不認得的模型一律跳過收斂，
# 而不是猜一個比例去裁——猜錯就是白砍畫面，而且不會有任何錯誤訊息。
_ASPECT_LOCKED_MODEL = "openai/gpt-image-2/edit"


def _model_output_ar_for(cw: int, ch: int) -> float | None:
    """裁切框尺寸 → 模型真的會輸出的寬高比；無法證明時回 None（＝跳過收斂）。

    **只對 gpt-image-2 生效**，因為只有它的輸出比例是可證的：
    `test_full_pipeline._gpt_image_size_for` 明文把 image_size 釘成三種尺寸之一，
    模型別無選擇。回傳的目標是穩定不動點，收斂後仍落在同一檔，不需迭代。

    ⚠️ **nano-banana 刻意不納入**，雖然 `prompt_builder` 有一份十檔 aspect_ratio
    enum 看起來可以用。理由是那份 enum 只有 anchored 路徑會送
    （`build_anchored_inputs` 才組 aspect_ratio）；**非 anchored 的 banana 請求
    只送 image_urls/prompt/system_prompt/resolution/output_format，沒有
    aspect_ratio**。拿 enum 當成「模型會照這個比例輸出」是程式端一廂情願，
    沒有證據。要納入 banana，先確認生成請求真的送了 aspect_ratio，或實測它
    不送參數時的輸出比例——`test_only_proven_model_gets_the_aspect_lock`
    那組測試會在條件成立時提醒。

    跳過收斂＝維持這條路徑一直以來的行為，不會比現在更糟。"""
    model = _legacy_render_model()
    if model != _ASPECT_LOCKED_MODEL:
        print(f"[pipeline] 客廳區特寫：model={model} 的輸出比例無法證明，跳過收斂")
        return None
    try:
        from test_full_pipeline import gpt_output_size_for_ratio
        size = gpt_output_size_for_ratio(cw / max(1, ch))
        return float(size["width"]) / float(size["height"])
    except Exception as e:
        print(f"[pipeline] 目標比例取不到（model={model}）：{type(e).__name__} → 跳過收斂")
        return None


def _converge_box_to_ar(x0: int, y0: int, x1: int, y1: int,
                        target_ar: float, *, tol: float = 0.02):
    """在現有框內收斂到 target_ar——**只裁不補**，回傳的框必定含於輸入框。

    太寬→置中裁寬；太高→偏下裁高（少裁上緣保天花板/間照，多裁前景地板）。
    裁法與 `_crop_region_base` 的比例鎖一致，刻意不另立一套。"""
    cw, ch = x1 - x0, y1 - y0
    ar = cw / max(1, ch)
    if ar > target_ar + tol:
        need_w = max(1, int(ch * target_ar))
        cx = (x0 + x1) // 2
        nx0 = max(x0, min(cx - need_w // 2, x1 - need_w))
        return nx0, y0, nx0 + need_w, y1
    if ar < target_ar - tol:
        need_h = max(1, int(cw / target_ar))
        ny0 = y0 + int((ch - need_h) * 0.25)
        return x0, ny0, x1, ny0 + need_h
    return x0, y0, x1, y1


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


# 綁邊靠牆家具允許越過畫面中線的比例（透視下牆面會內收，留一點寬容）
BOUND_WALL_HALF_TOLERANCE = 0.08
# 門軸走道厚度上限（相對短邊）：禁止把整間房塗灰，只要「門→對面」通道。
_DOOR_AISLE_THICK_MAX_FRAC = 0.28
_DOOR_AISLE_THICK_MIN_FRAC = 0.10


def _living_door_axis_clear_rect(
    W: int,
    H: int,
    entrance_side: str = "",
    entrance_bbox: tuple | None = None,
    living_bbox: tuple | None = None,
) -> tuple[int, int, int, int] | None:
    """客廳大門禁大型家具區：從門沿進出軸延伸到對面牆／客廳邊，不是門框旁一小條。

    產品規則（用戶 2026-07-30）：
      大門前方到對面牆壁 = 灰色通道 = 沙發／電視櫃等大型家具禁止；
      沙發只與電視面對面，左右無所謂。書房／臥室不走這條。

    幾何：
      - 走道厚度 ≈ 門洞較短邊（開門寬），夾在 [10%, 28%] 短邊，避免整圖塗死；
      - 左／右牆門：沿水平伸到客廳對邊；
      - 後牆／畫面上方門：沿垂直伸到客廳下緣；
      - 無 bbox 時用 entrance_side 給保守預設帶。
    """
    if W <= 1 or H <= 1:
        return None
    short = float(min(W, H))
    thick_min = int(short * _DOOR_AISLE_THICK_MIN_FRAC)
    thick_max = int(short * _DOOR_AISLE_THICK_MAX_FRAC)
    living = None
    if isinstance(living_bbox, (list, tuple)) and len(living_bbox) == 4:
        try:
            lx0, ly0, lx1, ly1 = [int(v) for v in living_bbox]
            if lx1 > lx0 and ly1 > ly0:
                living = (max(0, lx0), max(0, ly0), min(W, lx1), min(H, ly1))
        except (TypeError, ValueError):
            living = None
    if living is None:
        living = (0, 0, W, H)
    lx0, ly0, lx1, ly1 = living

    ent = entrance_side if entrance_side in ("left", "right") else ""
    if entrance_bbox and len(entrance_bbox) == 4:
        try:
            dx0, dy0, dx1, dy1 = [int(v) for v in entrance_bbox]
        except (TypeError, ValueError):
            return None
        if dx1 <= dx0 or dy1 <= dy0:
            return None
        door_w = dx1 - dx0
        door_h = dy1 - dy0
        # 門框幾乎蓋滿畫面＝標記垃圾／退化案例 → 整圖禁大型家具，逼 fail closed
        if (door_w * door_h) >= (W * H * 0.55):
            return (0, 0, W, H)
        door_cx = (dx0 + dx1) / 2.0
        door_cy = (dy0 + dy1) / 2.0
        # 走道「寬度」用門洞較短邊，不用整扇門高度——否則左牆高門 bbox 會把
        # 通道 y 拉成半張圖，planner 全滅（與「到對面牆」無關的誤傷）。
        opening = max(1, min(door_w, door_h))
        thick = int(max(thick_min, min(thick_max, opening * 1.05)))
        if not ent:
            if door_cx <= W * 0.40:
                ent = "left"
            elif door_cx >= W * 0.60:
                ent = "right"
            else:
                ent = "back"
        pad = max(2, int(short * 0.01))
        if ent == "left":
            # 門在左牆 → 通道往右到客廳右緣（對面方向）；y 取門中心一帶
            x0 = max(0, min(dx0, lx0) - pad)
            x1 = min(W, max(lx1, dx1))
            y0 = max(0, int(round(door_cy - thick / 2.0)))
            y1 = min(H, int(round(door_cy + thick / 2.0)))
        elif ent == "right":
            x0 = max(0, min(lx0, dx0))
            x1 = min(W, max(dx1, lx1) + pad)
            y0 = max(0, int(round(door_cy - thick / 2.0)))
            y1 = min(H, int(round(door_cy + thick / 2.0)))
        else:
            # 門在進深端／畫面上方：通道往鏡頭方向（+y）到客廳下緣
            x0 = max(0, int(round(door_cx - thick / 2.0)))
            x1 = min(W, int(round(door_cx + thick / 2.0)))
            y0 = max(0, min(dy0, ly0) - pad)
            y1 = min(H, max(ly1, dy1))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return (int(x0), int(y0), int(x1), int(y1))

    # 無門框 bbox：用側別給一條到對面的保守通道
    thick = int(max(thick_min, min(thick_max, short * 0.14)))
    if ent == "left":
        cy = int(H * 0.55)
        return (0, max(0, cy - thick // 2), lx1, min(H, cy + thick // 2))
    if ent == "right":
        cy = int(H * 0.55)
        return (lx0, max(0, cy - thick // 2), W, min(H, cy + thick // 2))
    return None


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
    # 沙發與電視櫃互相正對＝物理上不可能同一面牆。使用者綁邊時焦點牆就已經
    # 被決定成對牆，呼叫端傳進來的 _preferred_focal_side 是「自動模式該選哪面
    # 牆」的判斷，對綁邊訂單不適用，硬套會自打架。
    # 5DDC650F/E401B756 實例：用戶綁沙發左牆、大門也在左，呼叫端傳 focal=left
    # → 觸發「電視櫃與入口同牆」的加寬禁區 → 禁區吃掉 71% 畫面 → planner 無解
    # → 沒有引導圖 → 三次純文字生成、沙發全部貼門（間距 0）。成品判官反而證明
    # 電視櫃真的在右牆。改成綁邊時焦點牆＝對牆後，禁區降到 37%，配置有解。
    if side in ("left", "right"):
        focal = "right" if side == "left" else "left"
    # 客廳：門軸走道從門延伸到對面（禁大型家具），取代「只護門框旁一豎條」。
    door_clear = _living_door_axis_clear_rect(
        W, H,
        entrance_side=ent,
        entrance_bbox=entrance_bbox,
        living_bbox=living_bbox,
    )

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

    def _hugs_bound_wall(rect, wall_side: str) -> bool:
        """綁邊沙發必須真的靠在那面牆上——不得跨過畫面中線飄到房間中央。

        E401B756 教訓：門禁區把沙發起點推到畫面 38%，框子橫跨到 76%，
        「不碰禁區＋中心在客廳區」兩項檢查都過，畫出來卻是一張沙發浮在中央
        走道、還蓋住通往後方房間的門口。框子通過檢查不等於配置正確。
        靠牆家具留在自己那半邊是可驗證的最低要求；做不到就該無解、
        由付費前閘門擋下，不得畫出會誤導模型的引導圖。
        """
        if wall_side not in ("left", "right"):
            return True
        x0, _y0, x1, _y1 = rect
        limit = W * (0.5 + BOUND_WALL_HALF_TOLERANCE)
        if wall_side == "left":
            return x1 <= limit
        return x0 >= W - limit

    def _safe(rect, require_living=False, wall_side: str = ""):
        clean = _ordered(rect)
        if not clean or any(_rects_intersect(clean, bad) for bad in forbidden):
            return False
        if wall_side and not _hugs_bound_wall(clean, wall_side):
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

    # 門軸走道（寬帶橫貫畫面）vs 舊式門旁豎條：抽樣策略不同。
    # 橫貫走道用「不與 door_clear 相交」即可；豎條才用「從 clear 終點後開始」。
    _clear_w_frac = (
        (door_clear[2] - door_clear[0]) / max(1.0, float(W))
        if door_clear else 0.0
    )
    _axis_aisle = bool(door_clear) and _clear_w_frac >= 0.55
    if _axis_aisle and door_clear:
        # 優先抽走道上方／下方的 y，避免沙發-TV 橫軸落在門廊裡（視線掃門）。
        _cy0 = door_clear[1] / max(1.0, float(H))
        _cy1 = door_clear[3] / max(1.0, float(H))
        _prefer_y = []
        if _cy0 > 0.20:
            _prefer_y.append(max(0.06, _cy0 - 0.28))
            _prefer_y.append(max(0.06, _cy0 - 0.16))
        if _cy1 < 0.82:
            _prefer_y.append(min(0.72, _cy1 + 0.02))
            _prefer_y.append(min(0.72, _cy1 + 0.10))
        _seen = set()
        _merged = []
        for _y in list(_prefer_y) + list(y_starts):
            _k = round(float(_y), 3)
            if _k in _seen:
                continue
            _seen.add(_k)
            _merged.append(float(_y))
        y_starts = tuple(_merged)

    sofa = tv = None
    chosen = side_candidates[0]
    tv_w = 0.24
    for candidate_side in side_candidates:
        sw, sh = int(W * sofa_w), int(H * sofa_h)
        if candidate_side == "left":
            # 門在左且禁區是豎條時：沙發從 clear 右緣之後開始。
            # 門軸橫貫走道時：靠牆 x 抽樣，y 已避開走道。
            min_left = 0.08
            if door_clear and ent == "left" and not _axis_aisle:
                min_left = max(min_left, door_clear[2] / max(1, W) + 0.01)
            sx_starts = tuple(x for x in (min_left, 0.32, 0.50) if x + sofa_w <= 0.98)
            if not sx_starts:
                sx_starts = (min(0.70, min_left),)
            if door_clear and focal == ent == "right" and not _axis_aisle:
                # 電視在右側門牆：候選必須完整落在 door_clear 左邊。
                max_tv_start = door_clear[0] / max(1, W) - tv_w - 0.01
                tx_starts = tuple(
                    x for x in (max_tv_start, 0.52, 0.72, 0.82)
                    if x >= 0.02 and x + tv_w <= door_clear[0] / max(1, W) - 0.01
                )
            else:
                tx_starts = (0.72, 0.82, 0.52)
            facing = "right"
        else:
            max_right = 1 - 0.08 - sofa_w
            if door_clear and ent == "right" and not _axis_aisle:
                max_right = min(max_right, door_clear[0] / max(1, W) - sofa_w - 0.01)
            sx_starts = tuple(x for x in (max_right, 1 - 0.32 - sofa_w, 1 - 0.50 - sofa_w) if x >= 0.02)
            if not sx_starts:
                sx_starts = (max(0.02, max_right),)
            if door_clear and focal == ent == "left" and not _axis_aisle:
                # 2CD074F0：固定 4/18/28% 三個電視候選全落在 44% door_clear
                # 內。和沙發避門相同，從禁區終點後才開始嘗試電視框。
                min_tv_start = door_clear[2] / max(1, W) + 0.01
                tx_starts = tuple(
                    x for x in (min_tv_start, 0.28, 0.18, 0.04)
                    if x >= min_tv_start and x + tv_w <= 0.98
                )
            else:
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
                if not _safe(srect, require_living=True,
                             wall_side="" if is_auto else candidate_side):
                    continue
                for txf in tx_starts:
                    tx0 = int(W * txf)
                    trect = (tx0, ty0, min(W, tx0 + int(W * tv_w)), ty1)
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
            cv2.putText(img, "RED DOOR AISLE - NO LARGE FURNITURE",
                        (x0 + 12, min(H - 25, y1 - 30)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.65, W / 1800),
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
# 極端錯位門檻（合憲校準庫實測：接受組中心差 11/32/50/60/89（最高 89）,
# 靠此閘門擋的拒絕組只有 31E341CF=110、6DA08412=106；其餘拒絕案由門距閘門擋。
# → 憲法安全區間是 [90,106)。舊值 95 落在「接受組 89 ↔ 拒絕組 106」這段完全沒有
#   用戶裁決資料的空窗裡,會誤殺該區間的好圖（DD49AF60 客廳：沙發過門、產品全對、
#   中心差 97，只因 >95 被翻成 hard_fail 丟掉）。改 100：交付 97 這種好圖,仍擋
#   106/110,離接受組最高 89 留 11 點餘裕。只擋「用戶真的拒絕過」的錯位,不擋空窗。）
PAIR_CENTER_EXTREME = 100


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


def _focal_door_axis_conflict(validation: dict | None) -> dict | None:
    """電視櫃貼近入口且未與沙發對正時，視線軸會掃向大門。

    全域 pair-center 只攔極端值（>100），以保留校準庫中已接受的 89；但當
    focal_anchor 已進入 0.28 門寬禁區時，25–100 的偏差不再是純美感問題。
    這裡只合併兩個既有量測，不改任一全域門檻，也不影響門外的客廳。
    """
    if not isinstance(validation, dict):
        return None
    boxes = validation.get("render_bboxes") or {}
    try:
        from gemini_analyze import _door_adjacency_violation
        violation = _door_adjacency_violation(boxes)
    except Exception:
        return None
    if not violation or violation[0] != "focal_anchor":
        return None
    pair = _pair_center_delta(validation, tolerance=PAIR_CENTER_TOLERANCE)
    if not pair:
        return None
    sofa = boxes.get("sofa")
    focal = boxes.get("focal_anchor")
    door = boxes.get("entrance_door")
    if not all(isinstance(box, (list, tuple)) and len(box) == 4
               for box in (sofa, focal, door)):
        return None
    try:
        sofa_cx = (float(sofa[1]) + float(sofa[3])) / 2.0
        focal_cx = (float(focal[1]) + float(focal[3])) / 2.0
        door_cx = (float(door[1]) + float(door[3])) / 2.0
    except (TypeError, ValueError):
        return None
    # 大門與電視必須位於沙發的同一對向側；否則不是「沙發視線掃門」此類型。
    if (focal_cx - sofa_cx) * (door_cx - sofa_cx) <= 0:
        return None
    gap_ratio = float(violation[1]) / max(1.0, float(violation[2]))
    return {
        "target": "focal_anchor",
        "pair_delta_y": int(pair["delta_y"]),
        "pair_abs_delta_y": int(pair["abs_delta_y"]),
        "door_gap_ratio": round(gap_ratio, 3),
        "pair_tolerance": PAIR_CENTER_TOLERANCE,
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


_PAIR_ALIGNMENT_SOFA_HARD_FAILURES = (
    "sofa_facing_entrance_door",
    "furniture_blocks_door",
    "furniture_blocks_walkway",
    "sofa_intrudes_walkway",
    "sofa_faces_walkway",
    "sofa_on_wrong_side",
    "sofa_outside_living_zone",
    "sofa_back_against_window",
    "sofa_facing_window",
)


def _activate_pair_alignment_edit(validation: dict | None, render: dict | None,
                                  entry: dict | None, job_dir: str,
                                  idx: int) -> str | None:
    """只有 TV 明確錯位且沙發位置安全時，才鎖沙發做局部 TV 修正。"""
    v = validation or {}
    r = render or {}
    e = entry or {}
    if (e.get("_room_type") or "living") != "living":
        return None
    # AB03C2BE：沙發貼門、TV 並未錯位，但診斷用中心差 -60 仍啟動了
    # 「鎖沙發、只移 TV」，讓 door gap 四輪維持 0。中心差只是量測；必須由
    # 判官明確確認 TV/sofa 錯位，且沒有任何沙發位置／朝向硬傷，才准鎖沙發。
    if v.get("focal_anchor_misaligned_with_sofa") is not True:
        return None
    if any(v.get(flag) is True for flag in _PAIR_ALIGNMENT_SOFA_HARD_FAILURES):
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
    if e.get("_layout_contract_s2_required") is True:
        e["_consistency_ref_path"] = guide
        e["_s2_retry_artifacts_active"] = True
    else:
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
    image_paths: list | None = None,
) -> dict | None:
    """Shadow mode：只算契約、存檔、回傳摘要。不擋生圖、不改交付。

    LAYOUT_CONTRACT_SHADOW=0 可關。
    image_paths：用於證明 shadow 底圖 == zoning best_photo；缺則 v1 bbox 不 map。
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
        # S1｜bbox 只屬於 best_photo。沒有完整 image_paths 可比對 → 預設未驗證（fail closed）。
        _bind_src = user_zoning_v2 if isinstance(user_zoning_v2, dict) else payload
        # best_photo_index 綁定原始陣列位置；不得濾空值或重排 index。
        _paths_for_bind = list(image_paths or [])
        legacy_bbox_binding_verified = bool(
            _paths_for_bind
            and _zoning_bbox_matches_source(photo_path, _paths_for_bind, _bind_src)
        )
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

        # S1｜Shared Geometry Contract v1 dual-write。只觀測，不接 guide / paid gate / delivery。
        if os.environ.get("LAYOUT_CONTRACT_V1_SHADOW", "1").strip() == "0":
            summary["contract_v1"] = {
                "status": "disabled",
                "affects_delivery": False,
            }
        else:
            try:
                import layout_contract_v1 as lcv1
                contract_v1 = lcv1.build_layout_contract(
                    job_id=job_id,
                    photo_path=photo_path,
                    photo_key=canonical_photo_key(str(photo_path)) or Path(photo_path).name,
                    view_index=view_index,
                    legacy_zoning=payload,
                    legacy_shadow=contract,
                    legacy_bbox_binding_verified=legacy_bbox_binding_verified,
                )
                out_v1_json = out_dir / f"contract_v1_{tag}.json"
                out_v1_json.write_text(
                    json.dumps(contract_v1, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                version_chain = contract_v1["version_chain"]
                decision_v1 = contract_v1["decision"]
                summary["contract_v1"] = {
                    "status": "ok",
                    "schema_version": contract_v1["schema_version"],
                    "contract_id": version_chain["contract_id"],
                    "contract_hash": version_chain["contract_hash"],
                    "contract_json": str(out_v1_json),
                    "disposition": decision_v1["disposition"],
                    "pre_generation_eligible": bool(decision_v1["pre_generation_eligible"]),
                    "legacy_bbox_binding_verified": bool(legacy_bbox_binding_verified),
                    "affects_delivery": False,
                }
            except Exception as v1e:
                print(f"[pipeline] layout_contract v1 shadow 例外（不阻斷）: {type(v1e).__name__}: {v1e}")
                summary["contract_v1"] = {
                    "status": "error",
                    "reason": f"{type(v1e).__name__}: {str(v1e)[:160]}",
                    "affects_delivery": False,
                }

        _v1s = (summary.get("contract_v1") or {}).get("status")
        print(
            f"[pipeline] layout_contract shadow[{view_index}] "
            f"mode={mode} float={bool(can_float)} chosen={summary.get('chosen')} "
            f"safe={summary.get('safe_layout')} disp={summary.get('disposition')} "
            f"v1={_v1s} bind={legacy_bbox_binding_verified} (delivery untouched)"
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


def _image_edit_retry_enabled(renders: list[dict] | None) -> bool:
    if os.environ.get("USE_NANO_BANANA", "0").strip() == "1":
        return True
    return any(
        isinstance(render, dict)
        and render.get("_layout_contract_s2_required") is True
        for render in (renders or [])
    )


def _allow_waived_single_shot_without_guide(
    s2_waived: bool,
    room_type: str,
    guide_path: str | None,
    door_excluded: bool,
) -> bool:
    """S2 豁免後的無 guide 單次生成，只能用於已確認大門完全出鏡的客廳。

    門仍可見或門狀態未知時，無 guide 裸生會退回模型自由擺放，正是
    2CD074F0 電視櫃貼門仍付費生成的原因；這種情況必須在付費前擋住。
    """
    return bool(
        s2_waived
        and room_type == "living"
        and not guide_path
        and door_excluded
    )


def _s2_preflight_blocked_result(entry: dict | None) -> dict | None:
    """Build the terminal no-render result for a verifier-blocked S2 candidate.

    This is a deliberate geometry decision, not a missing file or infrastructure
    outage.  Returning it before ``generate_renders`` prevents Z3/Phase2/Phase3
    from repeatedly feeding a known no-render entry back into paid pipelines.
    """
    if not isinstance(entry, dict):
        return None
    if not (
        entry.get("_layout_mode") == "s2_blocked_legacy"
        and entry.get("_layout_contract_s2_required") is True
    ):
        return None
    reason = (
        "[配置前檢] S2 候選未通過牆面貼合與安全幾何驗證；"
        "已在付費生成前停止"
    )
    validation = {
        "ok": False,
        "hard_fail": True,
        "s2_preflight_blocked": True,
        "spatial_fidelity_fail": True,
        "reason": reason,
        "error": reason,
        "exception_type": "S2PreflightBlocked",
    }
    return {
        **entry,
        "render_path": None,
        "error": reason,
        "error_type": "S2PreflightBlocked",
        "render_mode": "preflight_blocked",
        "_s2_preflight_blocked": True,
        "validation": validation,
    }


def _s2_shadow_free_signal(entry: dict | None, reason: str = "") -> dict:
    """影子模式免費信號：S2 前檢擋掉的房，legacy 到底有沒有能用的引導圖。

    這是「legacy 能不能救這房」的必要條件（沒引導圖→門可見禁裸生→legacy 也交不出）。
    純讀 entry 既有欄位，零 fal、零 Gemini、不改任何交付行為。
    """
    e = entry or {}
    guide = e.get("_layout_guide")
    has_guide = bool(guide and Path(str(guide)).exists())
    return {
        "job": e.get("_job_id"),
        "idx": e.get("_view_index"),
        "style": e.get("style"),
        "room_type": e.get("_room_type"),
        "legacy_guide": has_guide,
        "guide_mode": e.get("_layout_guide_mode"),
        "door_excluded": bool(e.get("_door_excluded")),
        "s2_reason": (reason or "")[:80],
    }


def _sync_s2_candidate_sides(zoning_result: dict | None, contract: dict | None) -> dict:
    if not isinstance(zoning_result, dict) or not isinstance(contract, dict):
        return {}
    chosen_id = (contract.get("decision") or {}).get("chosen_candidate_id")
    chosen = next(
        (
            candidate for candidate in (contract.get("candidates") or [])
            if isinstance(candidate, dict) and candidate.get("candidate_id") == chosen_id
        ),
        None,
    )
    if not chosen:
        return {}
    values = {}
    for note in chosen.get("notes") or []:
        if not isinstance(note, str) or "=" not in note:
            continue
        key, value = note.split("=", 1)
        if key in {"sofa_side", "tv_side"}:
            values[key] = value.strip().lower()
    sofa_side = values.get("sofa_side")
    tv_side = values.get("tv_side")
    if sofa_side not in {"left", "right", "free"} or tv_side not in {"left", "right"}:
        return {}
    rules = zoning_result.get("furniture_placement_rules")
    if not isinstance(rules, dict):
        rules = {}
        zoning_result["furniture_placement_rules"] = rules
    zoning_result["_sofa_layout"] = sofa_side
    rules["sofa_side"] = sofa_side
    rules["tv_side"] = tv_side
    return {"sofa_side": sofa_side, "tv_side": tv_side}


def _s2_compact_entry_mode(zoning_result: dict | None, contract: dict | None) -> bool:
    """Enable the compact recipe when a B wall-sofa shares the entrance side."""
    if not isinstance(zoning_result, dict) or not isinstance(contract, dict):
        return False
    chosen_id = (contract.get("decision") or {}).get("chosen_candidate_id")
    chosen = next(
        (candidate for candidate in (contract.get("candidates") or [])
         if isinstance(candidate, dict) and candidate.get("candidate_id") == chosen_id),
        None,
    )
    if not chosen or chosen.get("candidate_type") != "B":
        return False
    sofa_side = ""
    for note in chosen.get("notes") or []:
        if isinstance(note, str) and note.startswith("sofa_side="):
            sofa_side = note.split("=", 1)[1].strip().lower()
            break
    entrance_side = str(zoning_result.get("_entrance_side") or "").strip().lower()
    return bool(sofa_side in {"left", "right"} and sofa_side == entrance_side)


def _layout_contract_s2_enabled() -> bool:
    """S2 is opt-in until real-photo SAFE/BLOCKED calibration is complete."""
    return os.environ.get("LAYOUT_CONTRACT_S2", "0").strip() == "1"


def _run_layout_contract_s2(
    *,
    job_id: str,
    job_dir: Path,
    photo_path: str,
    view_index: int,
    user_zoning_v2: dict | None,
    legacy_zoning: dict | None,
    sofa_mode: str,
    image_paths: list | None,
    can_float: bool = True,
    geometry_verifier=None,
    floor_reference_estimator=None,
) -> tuple[dict, dict]:
    """Build authoritative S2 Contract + guide + reconciliation, fail closed."""
    out_dir = Path(job_dir) / "layout_contract_s2"
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "eligible": False,
        "contract_path": None,
        "contract_sha256": None,
        "guide_path": None,
        "guide_sha256": None,
        "reconciliation_path": None,
        "reconciliation_sha256": None,
        "verification_path": None,
        "verification_sha256": None,
        "verification_history": [],
        "contract": None,
    }
    try:
        import layout_contract_v1 as lcv1
        import layout_geometry_s2 as lgs2
        import layout_geometry_verifier_s2 as lgvs2
        import layout_preflight_s2 as lps2
        from PIL import Image, ImageOps

        photo = Path(photo_path)
        if not photo.is_file():
            raise FileNotFoundError(str(photo))
        with Image.open(photo) as image:
            width, height = ImageOps.exif_transpose(image).size
        zoning = user_zoning_v2 if isinstance(user_zoning_v2, dict) else {}
        raw_struct = zoning.get("struct_geometry_v1")
        mode = str(sofa_mode or "free").strip().lower()
        if mode not in ("left", "right", "free"):
            mode = "free"
        _best_index = zoning.get("best_photo_index")
        expected_source_index = (
            _best_index if isinstance(_best_index, int) and not isinstance(_best_index, bool)
            else view_index
        )
        tag = f"{job_id}_v{view_index:02d}_{mode}"
        plan = lgs2.build_s2_plan(
            raw_struct,
            width=int(width),
            height=int(height),
            expected_source_photo_index=int(expected_source_index),
            sofa_side=mode,
            can_float=bool(can_float),
        )
        paths = list(image_paths or [])
        binding_verified = bool(
            paths and _zoning_bbox_matches_source(str(photo), paths, zoning)
        )
        verified_guide = None
        # E64D1C31 盲區：verify_and_replan_s2 有六個提前 return 分支，以前一個都不留紀錄。
        # 結果是「離線同樣輸入 2 個合格、線上 0 個合格」查不出差在哪——連猜三次全錯。
        # 記三件事就夠：①判官前規劃合不合格 ②走了哪個分支 ③最終 plan 有沒有被覆蓋。
        artifacts["plan_eligible_before_verifier"] = bool(
            plan.get("pre_generation_eligible") is True)
        artifacts["verifier_exit_branch"] = None
        artifacts["plan_overwritten_by_verifier"] = False
        if binding_verified and plan.get("pre_generation_eligible") is True:
            verifier_result = lgvs2.verify_and_replan_s2(
                raw_geometry=raw_struct,
                photo_path=photo,
                output_dir=out_dir / f"verification_{tag}",
                expected_source_photo_index=int(expected_source_index),
                sofa_side=mode,
                verifier=geometry_verifier or lgvs2.verify_s2_guide_gemini,
                floor_reference_estimator=floor_reference_estimator,
                can_float=bool(can_float),
            )
            artifacts["verifier_exit_branch"] = verifier_result.get("exit_branch")
            if verifier_result.get("replan_unsafe_codes"):
                artifacts["replan_unsafe_codes"] = list(
                    verifier_result.get("replan_unsafe_codes") or [])
            # 判官前合格、判官後不合格 = 好的規劃被判官的重新規劃蓋掉（今天查不出來的那件事）
            artifacts["plan_overwritten_by_verifier"] = bool(
                (verifier_result.get("plan") or {}).get("pre_generation_eligible") is not True)
            print(f"[layout-verifier] exit_branch={verifier_result.get('exit_branch')} "
                  f"判官前合格=True 判官後合格="
                  f"{(verifier_result.get('plan') or {}).get('pre_generation_eligible')}")
            plan = verifier_result["plan"]
            verified_guide = verifier_result.get("guide_artifact")
            verification_history = verifier_result.get("verification_history") or []
            verification_path = out_dir / f"geometry_verification_s2_{tag}.json"
            verification_path.write_text(
                json.dumps({
                    "verification": plan.get("geometry_verification"),
                    "history": verification_history,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            artifacts["verification_path"] = str(verification_path)
            artifacts["verification_sha256"] = _source_file_sha256(verification_path)
            artifacts["verification_history"] = verification_history
        source_binding = zoning.get("_source_binding") if isinstance(zoning, dict) else {}
        bound_photo_key = (
            str((source_binding or {}).get("photo_key") or "").strip()
            if binding_verified else ""
        )
        contract = lcv1.build_layout_contract_s2(
            job_id=job_id,
            photo_path=photo,
            photo_key=bound_photo_key or canonical_photo_key(str(photo)) or photo.name,
            view_index=view_index,
            s2_plan=plan,
            photo_binding_verified=binding_verified,
            legacy_zoning=legacy_zoning,
            legacy_shadow=None,
        )
        artifacts["contract"] = contract
        contract_path = out_dir / f"contract_v1_s2_{tag}.json"
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        artifacts["contract_path"] = str(contract_path)
        artifacts["contract_sha256"] = _source_file_sha256(contract_path)

        decision = contract.get("decision") or {}
        eligible = bool(
            binding_verified
            and decision.get("disposition") == "SAFE_FOR_GENERATION"
            and decision.get("pre_generation_eligible") is True
            and (plan.get("geometry_verification") or {}).get("status") == "pass"
            and (plan.get("geometry_verification") or {}).get("unsafe_codes") == []
            and (plan.get("transverse_reference") or {}).get("status") == "observed"
            and bool(artifacts.get("verification_path"))
            and isinstance(verified_guide, dict)
        )
        if eligible:
            guide = verified_guide
            reconciliation_path = out_dir / f"reconciliation_s2_{tag}.json"
            lps2.write_reconciliation_report(
                contract=contract,
                guide_artifact=guide,
                verification_artifact_path=artifacts["verification_path"],
                out_path=reconciliation_path,
            )
            artifacts.update({
                "eligible": True,
                "guide_path": guide["path"],
                "guide_sha256": guide["sha256"],
                "reconciliation_path": str(reconciliation_path),
                "reconciliation_sha256": _source_file_sha256(reconciliation_path),
            })

        verification = plan.get("geometry_verification") or {}
        verification_history = list(artifacts.get("verification_history") or [])
        verification_failed_fields = dict(verification.get("failed_fields") or {})
        summary = {
            "view_index": view_index,
            "photo": photo.name,
            "status": "safe" if eligible else "blocked",
            "contract_v1_disposition": decision.get("disposition"),
            "pre_generation_eligible": bool(decision.get("pre_generation_eligible")),
            "unsafe_codes": list(decision.get("unsafe_codes") or []),
            "contract_id": (contract.get("version_chain") or {}).get("contract_id"),
            "contract_hash": (contract.get("version_chain") or {}).get("contract_hash"),
            "contract_json": str(contract_path),
            "guide_path": artifacts["guide_path"],
            "reconciliation_path": artifacts["reconciliation_path"],
            "photo_binding_verified": binding_verified,
            "verification_status": verification.get("status"),
            "verification_attempt_count": verification.get("attempt_count", 0),
            "verification_corrected": bool(verification.get("corrected")),
            "verification_retry_reason": verification.get("retry_reason"),
            "verification_failed_fields": verification_failed_fields,
            "verification_exception_type": verification.get("exception_type"),
            "verification_unsafe_codes": list(verification.get("unsafe_codes") or []),
            "verification_history": verification_history,
            # E64D1C31 盲區：判官這條路以前完全不留紀錄，事後查不出是誰擋的。
            "plan_eligible_before_verifier": artifacts.get("plan_eligible_before_verifier"),
            "verifier_exit_branch": artifacts.get("verifier_exit_branch"),
            "plan_overwritten_by_verifier": artifacts.get("plan_overwritten_by_verifier"),
            "replan_unsafe_codes": list(artifacts.get("replan_unsafe_codes") or []),
            "affects_delivery": True,
        }
        if verification.get("status") == "fail":
            reason_parts = []
            if verification_failed_fields:
                reason_parts.append(
                    "fields=" + ",".join(
                        f"{field}:{status}"
                        for field, status in verification_failed_fields.items()
                    )
                )
            if verification.get("exception_type"):
                reason_parts.append(f"exception={verification['exception_type']}")
            reason_parts.append(
                f"attempts={verification.get('attempt_count', 0)}"
            )
            summary["reason"] = "layout verifier blocked: " + " ".join(reason_parts)
        print(
            f"[pipeline] layout_contract S2[{view_index}] status={summary['status']} "
            f"disp={summary['contract_v1_disposition']} bind={binding_verified} "
            f"candidate={decision.get('chosen_candidate_id')} "
            f"verify={summary['verification_status']} "
            f"attempts={summary['verification_attempt_count']} "
            f"failed_fields={','.join(f'{field}:{status}' for field, status in verification_failed_fields.items())} "
            f"exception={summary['verification_exception_type']}"
        )
        return summary, artifacts
    except Exception as error:
        print(f"[pipeline] layout_contract S2 fail-closed: {type(error).__name__}: {error}")
        return ({
            "view_index": view_index,
            "status": "blocked",
            "contract_v1_disposition": "BLOCKED",
            "pre_generation_eligible": False,
            "unsafe_codes": ["S2_PIPELINE_ERROR"],
            "reason": f"{type(error).__name__}: {str(error)[:180]}",
            "verification_status": "fail",
            "verification_attempt_count": 0,
            "verification_corrected": False,
            "verification_retry_reason": None,
            "verification_failed_fields": {},
            "verification_exception_type": type(error).__name__,
            "verification_unsafe_codes": ["S2_PIPELINE_ERROR"],
            "verification_history": [],
            "affects_delivery": True,
        }, artifacts)


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


# 門前禁區【刻意不當生成後交付閘門】——這是實測結論，不是還沒做完。
#
# 拿 86 筆真成品判定（raw_verdict 裡有 render_bboxes）比對離線重建的禁區，
# 四種碰撞判準（整個 2D bbox／底邊中點／底邊中間 50%／25%）**全部**都把
# 39606371 這張【已交付】的客廳圖判成違規——而且換成「家具站的那一點」也一樣，
# 代表電視櫃的落地點**真的**落在門掃過去的那條帶裡，不是 2D 外框的假重疊。
# 但那張圖用戶收下了：櫃子好好靠著右牆、進門動線是通的。
#
# 結論：「門→對面牆整條禁大型家具」是**規劃期的指引**，不是交付標準。
# 用戶接受靠在對面牆上、位於該帶深度的家具。硬拿它當閘門＝拿已交付的單
# 換 2–3 張本來就 hard_fail 的落選圖，純虧。
# 生成後的門邊防線維持 `_door_adjacency_violation`（0.25/0.28 門寬，
# 出自使用者裁決校準庫），那一條才是對齊用戶標準的。
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
            # 無分類力（接受 11-89 與拒絕 61-106 重疊,25 門檻曾殺掉接受組 4/5），
            # 但「極端值」有——接受組史上最高 89,靠此閘門擋的拒絕組是 106/110。
            # 門檻 100（見 PAIR_CENTER_EXTREME 註解）：接受組全放、極端錯位
            # （電視在沙發斜前方掃向門）必擋；89↔106 空窗不誤殺（DD49AF60 中心差 97）。
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
                            f"（合憲門檻 {PAIR_CENTER_EXTREME}；用戶接受組史上最高 89）")
                    _prev = (v.get("reason") or "").strip()
                    v["reason"] = f"{_tag}；{_prev}" if _prev and "皆合理" not in _prev else _tag
            axis_conflict = _focal_door_axis_conflict(v)
            if axis_conflict:
                v = dict(v)
                v["focal_door_axis_conflict"] = axis_conflict
                v["sofa_facing_entrance_door"] = True
                v["focal_anchor_misaligned_with_sofa"] = True
                v["ok"] = False
                v["hard_fail"] = True
                _tag = ("門邊電視櫃未與沙發對正："
                        f"門距 {axis_conflict['door_gap_ratio']:.3f} 門寬、"
                        f"對向差 {axis_conflict['pair_abs_delta_y']}/1000；"
                        "沙發視線會掃向大門")
                _prev = (v.get("reason") or "").strip()
                if _tag not in _prev:
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


def _record_validation_attempt(
    render: dict,
    *,
    job_id: str,
    stage: str,
    attempt: int,
    validation: dict | None = None,
    error: Exception | None = None,
) -> dict:
    """保存成品驗證的原始結果，並輸出可供 Railway 搜尋的結構化事件。

    診斷絕不可改變主流程判定：呼叫點多半在 try 區塊內，這裡若自己噴錯就會被
    同一個 except 抓走、記成「驗證失敗」——診斷 bug 偽裝成驗證失敗是最糟的
    除錯體驗。因此整段自保，失敗只印一行、回空 dict。
    """
    try:
        return _record_validation_attempt_inner(
            render, job_id=job_id, stage=stage, attempt=attempt,
            validation=validation, error=error)
    except Exception as _diag_err:      # noqa: BLE001 — 診斷失敗不得影響交付
        print(f"[validation] 診斷記錄失敗（不影響判定）: "
              f"{type(_diag_err).__name__}: {str(_diag_err)[:120]}")
        return {}


def _record_validation_attempt_inner(
    render: dict,
    *,
    job_id: str,
    stage: str,
    attempt: int,
    validation: dict | None = None,
    error: Exception | None = None,
) -> dict:
    raw = dict(validation or {})
    exception_type = type(error).__name__ if error is not None else raw.get("exception_type")
    exception_message = str(error) if error is not None else raw.get("error")
    failure_message = str(exception_message or "")
    failure_text = failure_message.lower()
    if raw.get("s2_preflight_blocked") is True:
        failure_class = "s2_preflight_blocked"
    elif (_is_quota_outage(failure_message)
            or exception_type in {"FalGenerationTimeout", "FalResultDownloadError", "TimeoutError",
                                  "ConnectionError"}
            or any(token in failure_text for token in
                   ("timed out", "timeout", "service unavailable", "bad gateway",
                    "connection reset", "http 500", "http 502", "http 503"))):
        failure_class = "infrastructure"
    elif exception_type or raw.get("ok") is None:
        failure_class = "validator_exception"
    elif raw.get("ok") is False:
        failure_class = "render_quality"
    else:
        failure_class = None
    event = {
        "validation_stage": stage,
        "attempt": int(attempt),
        "ok": raw.get("ok"),
        "hard_fail": raw.get("hard_fail"),
        "failure_class": failure_class,
        "exception_type": exception_type,
        "exception_message": failure_message[:500] or None,
        "raw_verdict": json.loads(json.dumps(raw, ensure_ascii=False, default=str)),
    }
    render.setdefault("validation_history", []).append(event)
    log_event = {
        "event": "render_validation_attempt",
        "job_id": job_id,
        "style": render.get("style"),
        "room_type": render.get("room_type") or render.get("_room_type") or "living",
        **event,
    }
    print("[validation] " + json.dumps(log_event, ensure_ascii=False, default=str))
    return event


# ── guide 觀測（純診斷）──────────────────────────────────────────────
# 2026-07-28：失敗的客廳走 dropped_renders，而那裡不存 reference_map，導致
# 「guide 有沒有真的送進生成請求」在失敗樣本上完全量不到（交付樣本 36 張裡
# 只有 6 張帶 guide，但失敗那 89 張比不了）。這組欄位只寫不讀：不得參與任何
# 交付／驗證／重試判斷，純粹讓下一批真實訂單自己把答案寫進資料庫。
_GUIDE_TRACE_MAX = 12


def _safe_file_sha256(path) -> str | None:
    if not path:
        return None
    try:
        return _source_file_sha256(path)
    except Exception:
        return None


def guide_trace_record(*, stage: str, attempt, layout_mode, guide_path,
                       original_source_path=None, original_source_key=None,
                       guide_canvas_path=None,
                       coordinate_space=None, skip_reason=None,
                       attached=None, reference_count=None) -> dict:
    """單筆 guide 觀測紀錄。

    只存 sha256 / 檔名 / storage key；絕不存本機暫存路徑、data URL、
    簽名網址或任何外部 request 內容（Railway 重啟後本機路徑也無意義）。

    身分刻意拆成兩個，不可合併：
      * original_source_key / original_source_sha256 — 客戶上傳的那張原圖；
      * guide_canvas_sha256 — guide 實際畫在哪張影像上（裁切/zoom 後會不同）。
    合成一組會讓「guide 是不是畫在對的原圖上」變成答不出來的問題。
    """
    guide_sha = _safe_file_sha256(guide_path)
    guide_name = None
    if guide_path:
        try:
            guide_name = Path(str(guide_path)).name
        except Exception:
            guide_name = None
    return {
        "stage": str(stage),
        "attempt": attempt,
        "layout_mode": layout_mode,
        "guide_created": bool(guide_path),
        # 路徑有值不代表檔案讀得到；讀不到的 guide 等於沒有，要分得出來
        "guide_artifact_readable": bool(guide_sha),
        "guide_sha256": guide_sha,
        "guide_basename": guide_name,
        "original_source_key": original_source_key,
        "original_source_sha256": _safe_file_sha256(original_source_path),
        "guide_canvas_sha256": _safe_file_sha256(guide_canvas_path),
        "coordinate_space": coordinate_space,
        "attached_to_generation_request": attached,
        "reference_count": reference_count,
        "skip_reason": skip_reason,
    }


def append_guide_trace(render: dict, record: dict) -> None:
    """把一筆觀測紀錄接到 render 上（就地累積，跨 attempt 不覆蓋）。
    絕不可拋例外影響生成流程。"""
    if not isinstance(render, dict) or not isinstance(record, dict):
        return
    try:
        trace = render.get("_guide_trace")
        if not isinstance(trace, list):
            trace = []
            render["_guide_trace"] = trace
        trace.append(record)
        if len(trace) > _GUIDE_TRACE_MAX:
            del trace[:-_GUIDE_TRACE_MAX]
    except Exception:
        pass


def _render_angle_label(entry: dict) -> str | None:
    return (entry or {}).get("angle_label") or (entry or {}).get("_angle_label")


def find_dropped_render_match(dropped: dict, finals) -> dict | None:
    """替 dropped_renders 條目找出對應的 final render。

    同一風格可能有兩個客廳視角（主視角＋另一角度）。只比 style+room_type
    會把 Phase3 的診斷寫到第一張符合者身上，等於串錯視角——而診斷寫錯張
    比沒寫更糟，這整套觀測就是為了不讓資料說謊。

    規則只有一條：**不明確就不寫**。
      * dropped 標了視角 → 必須有候選的視角完全相同，否則 None；
      * dropped 沒標視角（舊資料）→ 只有候選唯一時才算不明確以外的情況。
    """
    if not isinstance(dropped, dict):
        return None
    candidates = [
        r for r in (finals or [])
        if isinstance(r, dict)
        and dropped.get("style") == r.get("style")
        and dropped.get("room_type") == (r.get("room_type") or r.get("_room_type"))
    ]
    if not candidates:
        return None
    angle = _render_angle_label(dropped)
    if angle:
        # 標了視角就必須對得上；對不上時任何回退都是猜，寧可不寫
        exact = [r for r in candidates if _render_angle_label(r) == angle]
        return exact[0] if exact else None
    # dropped 沒有視角資訊（舊資料）：只有「候選唯一」才不算猜
    return candidates[0] if len(candidates) == 1 else None


def merge_dropped_render_diagnostics(dropped: dict, final_render: dict) -> dict:
    """Phase3 收尾：把最新診斷同步回既有的 dropped_renders 條目。

    dropped_renders 是 Phase3 之前就寫好的，Phase3 之後 render 上會多出新的
    validation_history 與 guide_trace。以前這裡只搬 history，導致 Phase3 的
    guide 使用狀況在資料庫裡完全看不到——而 incomplete 收尾正是這條路徑。
    抽成共用 helper 就不會再各搬各的漏掉欄位。
    只覆蓋診斷欄位，不動 style / room_type / reason 等既有內容。
    """
    if not isinstance(dropped, dict) or not isinstance(final_render, dict):
        return dropped
    history = list(final_render.get("validation_history") or [])
    dropped["validation_history"] = history
    dropped["validation_attempt_count"] = len(history)
    if history:
        dropped["validation_stage"] = history[-1].get("validation_stage")
    trace = final_render.get("_guide_trace")
    # 只有真的有新紀錄才覆蓋，否則會把生成階段留下的 trace 洗掉
    if isinstance(trace, list) and trace:
        dropped["guide_trace"] = list(trace)
    return dropped


def _validation_diagnostics(render: dict) -> dict:
    """整理可安全寫入 result_json 的驗證歷程與最終狀態。"""
    history = list(render.get("validation_history") or [])
    validation = render.get("validation") or {}
    error_type = render.get("error_type")
    error_text = str(render.get("error") or render.get("render_error")
                     or validation.get("error") or render.get("retry_reason") or "")
    if (render.get("_s2_preflight_blocked") is True
            or validation.get("s2_preflight_blocked") is True
            or error_type == "S2PreflightBlocked"):
        failure_class = "s2_preflight_blocked"
    elif (error_type in {"FalGenerationTimeout", "FalResultDownloadError"}
            or render.get("error") or render.get("render_error")
            or _is_quota_outage(error_text)):
        failure_class = "infrastructure"
    elif history:
        failure_class = history[-1].get("failure_class")
    elif validation.get("validation_unavailable"):
        failure_class = "validator_exception"
    elif validation.get("ok") is False:
        failure_class = "render_quality"
    else:
        failure_class = None
    guide_trace = render.get("_guide_trace")
    return {
        "failure_class": failure_class,
        "validation_stage": history[-1].get("validation_stage") if history else None,
        "validation_attempt_count": len(history),
        "validation_history": history,
        # 純診斷：失敗樣本也要看得到 guide 鏈（交付樣本才有的 reference_map 補不了這塊）
        "guide_trace": list(guide_trace) if isinstance(guide_trace, list) else [],
        "validation_final": {
            "ok": validation.get("ok"),
            "hard_fail": validation.get("hard_fail"),
            "validation_unavailable": validation.get("validation_unavailable"),
            "validation_outage": validation.get("validation_outage"),
            "exception_type": validation.get("exception_type") or error_type,
            "exception_message": str(validation.get("error") or error_text)[:500] or None,
        },
    }


# S2 幾何模型「不適用這個房型」的碼——跟「驗過而且不安全」必須分開。
# 這些碼代表規劃器連候選都造不出來（缺相對長牆／結構元素不足／座標無效），
# 判官從來沒被叫起來（verification_attempt_count = 0）。
S2_MODEL_NOT_APPLICABLE_CODES = {
    "NO_USABLE_WALL", "CANDIDATE_GEOMETRY_INCOMPLETE", "NO_VIABLE_LAYOUT",
    "MISSING_DOOR", "MISSING_DOOR_FLOOR_CONTACT", "MISSING_ENTRANCE_LANDING",
    "MISSING_WALKWAY_POLYGON", "MISSING_LIVING_FLOOR",
    "MISSING_WALL_PLANE_EVIDENCE", "INVALID_GEOMETRY",
}


def _s2_model_not_applicable(summary: dict | None) -> bool:
    """S2 是「模型化不了這個房型」還是「驗過判定不安全」。

    只有前者才准回退 legacy 引導——後者是判官真的看過圖並判不安全，必須照擋。
    判準：判官從未執行（attempt_count 0、verification_status 空）且所有 unsafe 碼
    都屬於結構不足類。
    """
    if not isinstance(summary, dict):
        return False
    if summary.get("verification_status") in ("pass", "fail"):
        return False
    if int(summary.get("verification_attempt_count") or 0) > 0:
        return False
    codes = {str(c) for c in (summary.get("unsafe_codes") or [])}
    codes.discard("GEOM_NOT_ELIGIBLE")      # 伴隨碼，本身不表示判官判過
    return bool(codes) and codes <= S2_MODEL_NOT_APPLICABLE_CODES


def _s2_verifier_unstable(summary: dict | None) -> bool:
    """判官 fail、但 fail 欄位跨多次執行不穩定 = 判官對這房型不確定、model 不動，
    不是「真的看過圖判不安全」——穩定的不安全應該給出一致的 fail 欄位。

    173C14C5／D85B8525 同款：sofa_back_contact／left_wall／right_wall／walkway／
    cross_axis 每次亂跳。這種「S2 對此房型算不穩」的訊號，跟「連候選都生不出來」
    一樣代表 S2 模型化不了這房型，該回退 legacy 門感知引導，而不是硬擋成零圖。
    通用規則：任何 verifier 雜訊房型都受惠，不是 173 特例。
    """
    if not isinstance(summary, dict):
        return False
    if summary.get("verification_status") != "fail":
        return False
    field_sets = []
    for entry in summary.get("verification_history") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("outcome") or "") not in ("hard_fail", "fail"):
            continue
        failed = frozenset(
            k for k, v in entry.items()
            if isinstance(v, str) and v == "fail")
        if failed:
            field_sets.append(failed)
    if len(field_sets) < 2:
        return False
    # 2CD074F0：四次完整集合雖交替變動，但 sofa_back_contact 與
    # left_wall_floor_alignment 每次都 fail。只要存在共同 hard fail，核心問題
    # 就是穩定的真不安全，不得因附加欄位抖動而 waive 回 legacy。
    common_failures = set.intersection(*(set(fields) for fields in field_sets))
    if common_failures:
        return False
    # 只有多次判決完全沒有共同 fail，才代表判官沒有一致的幾何死因。
    return len(set(field_sets)) >= 2


# 只核准「多抽 1 次」。這是成本上限，不是預設值——環境變數與函式參數都不得超過。
_S2_ZONING_RESAMPLE_HARD_CAP = 2

# 一看就知道是檔案系統路徑、不是 storage key 的開頭段。
_LOCAL_PATH_FIRST_SEGMENTS = frozenset({
    "app", "tmp", "temp", "var", "jobs", "home", "mnt", "media", "users",
    "opt", "srv", "root", "private",
})


def _is_portable_photo_key(key: str | None) -> bool:
    """這個 key 跨容器／跨部署還對得起來嗎？

    `canonical_photo_key` 只做正規化，**不會**把本機路徑轉成 storage key：
      * `C:\\Users\\...\\photo.jpg` → `C:/Users/.../photo.jpg`
      * `/app/jobs/FD73C48C/photo.jpg` → `app/jobs/FD73C48C/photo.jpg`
    兩者都沒有觸發 basename fallback，就這樣被寫進 result_json。
    它正常的產物是 `<upload_id>/<filename>`（兩段），所以用段數＋系統目錄
    開頭來認出路徑。
    """
    parts = [p for p in str(key or "").strip().split("/") if p]
    if not parts or len(parts) > 2:
        return False
    return parts[0].lower().rstrip(":") not in _LOCAL_PATH_FIRST_SEGMENTS


def _portable_photo_key(photo_path, *, zoning: dict | None = None,
                        key_by_local: dict | None = None) -> str:
    """診斷用的可攜照片識別：上傳綁定 → 本機路徑對照表 → 最後才 basename。"""
    raw = str(photo_path or "")
    bind = (zoning or {}).get("_source_binding") if isinstance(zoning, dict) else None
    for candidate in (
        (bind or {}).get("photo_key") if isinstance(bind, dict) else None,
        (key_by_local or {}).get(raw),
    ):
        key = canonical_photo_key(candidate)
        if _is_portable_photo_key(key):
            return key
    return Path(raw).name


def _struct_geometry_sha256(zoning: dict | None) -> str | None:
    """這次判官到底驗的是哪一份幾何。

    只有 commit（重抽後合格）才會把新幾何寫回訂單；**第 2 抽仍失敗時**，
    判官的判決來自新幾何、訂單存的卻是舊幾何——沒有這個雜湊，下次查死因會
    再一次「線上結果與存檔對不起來」（E64D1C31 那類盲區）。
    """
    struct = (zoning or {}).get("struct_geometry_v1") if isinstance(zoning, dict) else None
    if not isinstance(struct, dict):
        return None
    return hashlib.sha256(json.dumps(
        struct, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _s2_zoning_resample_max() -> int:
    """S2 硬擋時，同一張照片最多跑幾次 zoning（含第一次）。

    2026-07-30：同圖受控三跑證實幾何非確定；FD73C48C 抽到的左牆近端在門口截斷
    （846/128），而受控三跑有 3/3 拉到畫面角（~980/0）——判官咬的正是這個元素。
    所以「付 Fal 前再抽一次」有根據。**但只准多抽 1 次**：每次重抽不只多 1 次
    zoning flash，後面還要再跑整輪 S2 判官（歷史上單輪可到 3 次 Gemini）。
    預設與硬上限都是 2；設 1 = 關閉重抽。要放寬得先量 hard_block 佔比。
    """
    try:
        n = int(os.environ.get("S2_ZONING_RESAMPLE_MAX", str(_S2_ZONING_RESAMPLE_HARD_CAP)))
    except (TypeError, ValueError):
        n = _S2_ZONING_RESAMPLE_HARD_CAP
    return max(1, min(n, _S2_ZONING_RESAMPLE_HARD_CAP))


# 基礎設施性質的碼：重抽只會再錯一次，不該花錢。
_S2_INFRA_UNSAFE_CODES = frozenset({"S2_PIPELINE_ERROR"})


def _s2_would_hard_block(summary: dict | None, artifacts: dict | None) -> bool:
    """這次 S2 結果若繼續走，會變成 s2_blocked_legacy（付費前擋死）嗎？

    **正向認列**：只有「判官真的執行過、給出一致的 fail 欄位、而且沒有例外」
    才算幾何硬擋，值得重抽一份結構觀測。任何不確定都回 False（不花錢）。

    刻意排除：
      * `S2_PIPELINE_ERROR` / `verification_exception_type`——基礎設施壞了，
        重抽只是再壞一次（api.py 的 fail-closed 分支會寫 verification_status="fail"
        且 attempt_count=0，光看 status 會誤判成幾何問題）。
      * 模型化不了／判官不穩 → 本來就走 waive legacy，不擋生成，不必重抽。
      * fail 但講不出失敗欄位 → 不是幾何硬擋。
    """
    if isinstance(artifacts, dict) and artifacts.get("eligible"):
        return False
    if not isinstance(summary, dict) or not summary:
        return False
    if summary.get("verification_exception_type"):
        return False
    codes = {str(c) for c in (summary.get("unsafe_codes") or [])}
    codes |= {str(c) for c in (summary.get("verification_unsafe_codes") or [])}
    if codes & _S2_INFRA_UNSAFE_CODES:
        return False
    if summary.get("verification_status") != "fail":
        return False
    if int(summary.get("verification_attempt_count") or 0) <= 0:
        return False
    if not (summary.get("verification_failed_fields") or {}):
        return False
    if _s2_model_not_applicable(summary) or _s2_verifier_unstable(summary):
        return False
    return True


# 重抽時允許被換掉的欄位＝純結構觀測。其餘一律保留原單。
_S2_RESAMPLE_GEOMETRY_FIELDS = (
    "struct_geometry_v1", "_source_binding", "_provenance", "best_photo_index",
)


def _s2_zoning_with_resampled_geometry(original: dict | None,
                                       resampled: dict | None) -> dict | None:
    """只把幾何換成新抽的那份，客戶在分區頁確認過的東西一律保留。

    重抽的目的只有一個：換一份結構觀測。`proposed_zones` 裡的 `sofa_side` /
    `sofa_side_source` / `alt_*` 是**客戶的決定**，整包覆蓋會讓新一輪 Gemini 的
    AI 建議冒充客戶硬綁左右（e318392 修過同一類問題）。

    附帶好處：沙發側別與 can_float 都不是從幾何推導的
    （`_guide_sofa_side` 讀 furniture_placement_rules / entrance_zone，
    `_room_can_float_sofa` 讀 analysis + zones），所以只換幾何時這兩個訊號
    在各次嘗試之間必然一致，不會出現「新牆線配舊擺法假設」。
    """
    if not isinstance(resampled, dict):
        return None
    merged = dict(original) if isinstance(original, dict) else {}
    for key in _S2_RESAMPLE_GEOMETRY_FIELDS:
        if key in resampled:
            merged[key] = resampled[key]
    return merged


def _recompute_zoning_v2_for_s2_retry(
    photo_path: str,
    *,
    previous_zoning: dict | None = None,
) -> dict | None:
    """同一張合約底圖再跑一次 zoning v2（只送這張，避免換圖換座標）。

    失敗回 None。成功時綁定沿用舊 photo_key（若有），best/source index 鎖 0。
    """
    try:
        from zoning_v2 import compute_zoning_v2
    except ImportError as exc:
        print(f"[pipeline] S2 zoning 重抽：import 失敗 {exc}")
        return None
    path = Path(photo_path)
    if not path.is_file():
        print(f"[pipeline] S2 zoning 重抽：找不到照片 {photo_path}")
        return None
    try:
        zres = compute_zoning_v2([path], video_keyframes=None)
    except Exception as exc:
        print(f"[pipeline] S2 zoning 重抽：compute 例外 {type(exc).__name__}: {exc}")
        return None
    if not isinstance(zres, dict) or zres.get("error"):
        print(f"[pipeline] S2 zoning 重抽：模型錯誤 { (zres or {}).get('error') }")
        return None
    prev_bind = (
        (previous_zoning or {}).get("_source_binding")
        if isinstance(previous_zoning, dict) else None
    ) or {}
    zres["best_photo_index"] = 0
    zres["_source_binding"] = {
        "photo_key": (
            str(prev_bind.get("photo_key") or "").strip()
            or canonical_photo_key(str(path))
            or path.name
        ),
        "sha256": _source_file_sha256(path),
    }
    sg = zres.get("struct_geometry_v1")
    if isinstance(sg, dict):
        sg["source_photo_index"] = 0
    return zres


def _s2_contract_with_zoning_resample(
    *,
    initial_zoning_v2: dict | None,
    photo_path: str,
    max_attempts: int | None = None,
    run_contract,
    rezone=None,
) -> tuple[dict, dict, dict | None, list[dict]]:
    """S2 合約：若會硬擋，重抽 zoning 再規劃，直到合格／改走 waive／用盡次數。

    run_contract(zv2) -> (summary, artifacts)
    rezone(previous_zv2) -> new_zv2 | None   （可注入 mock；預設呼叫 Gemini）

    回傳 (summary, artifacts, zoning_v2_to_commit, attempt_log)
    zoning_v2_to_commit：只有「重抽後變 eligible」才是新幾何；否則 None（保留原單）。
    """
    # 硬上限無條件夾住：`max_attempts` 是為了測試可注入，不是繞過成本核准的後門。
    attempts = max(1, min(_S2_ZONING_RESAMPLE_HARD_CAP, int(
        max_attempts if max_attempts is not None else _s2_zoning_resample_max())))
    current = initial_zoning_v2
    log: list[dict] = []
    summary: dict = {}
    artifacts: dict = {"eligible": False}
    commit: dict | None = None

    for i in range(attempts):
        summary, artifacts = run_contract(current)
        if not isinstance(summary, dict):
            summary = {}
        if not isinstance(artifacts, dict):
            artifacts = {"eligible": False}
        # 觀測指紋：只讀欄位寫進 log，不做任何分支判斷（AST 契約：不得 if _provenance）
        _prov_blob = current.get("_provenance") if isinstance(current, dict) else None
        _prov_fp = (
            _prov_blob.get("request_fingerprint")
            if isinstance(_prov_blob, dict) else None
        )
        entry = {
            "attempt": i + 1,
            "eligible": bool(artifacts.get("eligible")),
            "verification_status": summary.get("verification_status"),
            "unsafe_codes": list(summary.get("unsafe_codes") or []),
            "hard_block": _s2_would_hard_block(summary, artifacts),
            "waive_model": _s2_model_not_applicable(summary),
            "waive_unstable": _s2_verifier_unstable(summary),
            "provenance_fp": _prov_fp,
            # 這一次判官驗的是哪份幾何。第 2 抽仍失敗時訂單存的是舊幾何，
            # 沒有這個雜湊就沒辦法把線上判決對回任何一份幾何。
            "geometry_sha256": _struct_geometry_sha256(current),
        }
        log.append(entry)
        if artifacts.get("eligible"):
            if i > 0 and isinstance(current, dict):
                commit = current
            break
        if not _s2_would_hard_block(summary, artifacts):
            # waive 路徑或非硬擋：不再重抽
            break
        if i + 1 >= attempts:
            break
        rezone_fn = rezone
        if rezone_fn is None:
            rezone_fn = lambda prev: _recompute_zoning_v2_for_s2_retry(
                photo_path, previous_zoning=prev)
        merged = _s2_zoning_with_resampled_geometry(current, rezone_fn(current))
        if not isinstance(merged, dict):
            log.append({
                "attempt": i + 2,
                "eligible": False,
                "hard_block": True,
                "rezone_failed": True,
            })
            break
        new_z = merged
        print(f"[pipeline] S2 硬擋 → 重抽 zoning "
              f"({i + 1}/{attempts} 已用，再試第 {i + 2} 次)")
        current = new_z

    # 重抽過、最後仍被擋 → commit 是 None，訂單留著舊幾何，
    # 但判官其實是對「最後那份重抽幾何」下判決的。把那份留在診斷裡，
    # 否則下次查這張單會再撞一次「線上判決與存檔幾何對不起來」。
    if commit is None and len(log) > 1 and isinstance(current, dict):
        last_struct = current.get("struct_geometry_v1")
        if isinstance(last_struct, dict) and (
                _struct_geometry_sha256(current)
                != _struct_geometry_sha256(initial_zoning_v2)):
            log[-1]["verified_struct_geometry_v1"] = last_struct

    return summary, artifacts, commit, log


def _s2_blocked_fallback_enabled() -> bool:
    """S2 判官擋死時退回 legacy（客廳區特寫、門裁出鏡頭），而不是給客戶零圖。

    2026-08-01 用戶裁決：「零圖傷害最大，客人會覺得被騙」。
    在此之前，判官驗過且判不安全 → `s2_blocked_legacy` → 付費前檢擋死 → 交付 0 張。

    離線唯讀量測（7 張 S2 判官擋死的單，套上 production 的 `_door_exclusion_limits`
    裁切後）：**legacy 規劃器 7/7 都產得出配置**——「S2 擋死」不等於「這房間無解」，
    只等於「S2 這套模型描述不了它」。而 legacy 的解法是把大門裁出鏡頭外
    （見 `_crop_to_living_zone`），門的問題物理上消失。

    ⚠️ 誠實但書：7/7 只證明「規劃階段沒被一起擋死」，**不是救援率**——
    沒生圖、沒過判官、沒交付。legacy 這條路沒有 S2 幾何判官把關，
    生成後改由門距（0.25/0.28 門寬，使用者校準）／走道／可見性等既有閘門負責。
    這是「一張沒有 S2 把關但門已出鏡的圖」對上「零圖」的取捨，不是品質升級。

    `S2_BLOCKED_FALLBACK=0`（Railway env）可即時關回「擋死＝零圖」。
    """
    return os.environ.get("S2_BLOCKED_FALLBACK", "1").strip() != "0"


def _crop_to_living_zone(base_path: str, job_dir, idx: int,
                         living_bbox1000, pad: float = 0.04,
                         entrance_bbox1000=None):
    """裁到分區層認出來的客廳區，**並把大門推出鏡頭外**——S2 描述不了房型時的最後一招。

    用戶提案：既然分區層能準確認出「哪一塊是客廳」，就把那一塊特寫裁出來再擺家具。
    928AD8B4 實測有效：那張斜角照裁出客廳區後畫面剩左右兩面實牆＋後方落地窗。

    ⚠️ 2D212624 教訓（2026-08-01）：舊版**只**裁客廳區、完全沒碰門。
    928AD8B4 之所以「大門出鏡」是運氣——那張的 living_zone 剛好不含門。
    這張照片的 living_zone 幾乎蓋滿整張（含大門），裁完門還在畫面裡 →
    電視櫃緊貼大門（門距 0.050 門寬，需 0.28）→ 落選。
    docstring 當初承諾「門的問題物理上消失」，實作卻沒有保證它，這裡補上：
    沿用 `_crop_region_base` 同一個 `_door_exclusion_limits`，把裁切邊界推過門框。
    （刻意共用同一個函式，不另寫一套緩衝算法——兩套門排除口徑就是這系列問題的病根。）

    回傳 (裁切後路徑, crop_box)；裁不動就回 None。
    """
    if not (isinstance(living_bbox1000, (list, tuple)) and len(living_bbox1000) == 4):
        return None
    try:
        import cv2
        img = cv2.imread(str(base_path))
        if img is None:
            return None
        H, W = img.shape[:2]
        ly0, lx0, ly1, lx1 = [float(v) for v in living_bbox1000]
        x0 = int(max(0, (lx0 / 1000.0 - pad) * W))
        y0 = int(max(0, (ly0 / 1000.0 - pad) * H))
        x1 = int(min(W, (lx1 / 1000.0 + pad) * W))
        y1 = int(min(H, (ly1 / 1000.0 + pad) * H))
        if x1 - x0 < W * 0.30 or y1 - y0 < H * 0.30:
            return None          # 裁得太小＝分區可能抓錯，寧可不裁
        # 大門推出鏡頭外。推完太窄就放棄推（寧可門在畫面裡，也不要一張只剩牆的廢底圖）。
        door_excluded = False
        if isinstance(entrance_bbox1000, (list, tuple)) and len(entrance_bbox1000) == 4:
            try:
                _d_x0 = int(float(entrance_bbox1000[1]) / 1000.0 * W)
                _d_x1 = int(float(entrance_bbox1000[3]) / 1000.0 * W)
                _lo, _hi = _door_exclusion_limits(W, _d_x0, _d_x1)
                _nx0, _nx1 = max(x0, _lo), min(x1, _hi)
                if (_nx0, _nx1) != (x0, x1) and (_nx1 - _nx0) >= W * 0.30:
                    print(f"[pipeline] 客廳區特寫：大門推出鏡頭 x {x0}->{_nx0}, {x1}->{_nx1}"
                          f"（門 px {_d_x0}-{_d_x1}）")
                    x0, x1 = _nx0, _nx1
                    door_excluded = True
                elif (_nx0, _nx1) != (x0, x1):
                    print(f"[pipeline] 客廳區特寫：門排除後過窄（{_nx1-_nx0}px），維持原裁切")
            except (TypeError, ValueError):
                pass
        # ── 比例鎖 ──────────────────────────────────────────────────────
        # 29C70C03 教訓（2026-08-03）：門排除後是 2561x1905=1.344，但 gpt-image-2
        # 只輸出固定三種尺寸、這張會拿到 1536x1024=1.500。比例不一致時模型**必須**
        # 對畫面做裁切／重構／補畫才能填滿輸出框——差多少就有多少畫面不是原照片的。
        # ⚠️ 比例差只證明「一定會發生重構」，**不能證明它會補在哪一側**；客戶回報
        # 「感覺不是裁掉是移除掉、空間感也不對」與這 11.6% 的落差時間吻合、方向
        # 合理，但那是推論不是量測。這道鎖要消除的是**重構壓力本身**。
        # `_crop_region_base` 早就有這道鎖（F87A75BB 修的），這條客廳區特寫路徑
        # 一直漏掉，補上。
        # 目標比例向【實際那個模型】取、不寫死 1.5，兩個理由：
        #   a) 直式的客廳區若被硬拉成橫式要砍掉一半高度，比留著比例差更糟；
        #   b) gpt-image-2 與 nano-banana 的比例桶完全不同（見 `_model_output_ar_for`），
        #      寫死等於賭 RENDER_MODEL 永遠不會被翻回去。
        # ⚠️ 收斂方向會隨落在哪個桶而變：落 1.5 桶時裁【高】（與門排除的裁寬互不
        # 干擾），落 1.0 桶時同樣裁【寬】、與門排除**同向疊加**。所以兩道守門
        # 都要檢查，不能只看其中一邊。
        _t_ar = _model_output_ar_for(x1 - x0, y1 - y0)
        if _t_ar is None:
            print("[pipeline] 客廳區特寫：判不出模型輸出比例，跳過收斂（維持現況）")
        else:
            _cx0, _cy0, _cx1, _cy1 = _converge_box_to_ar(x0, y0, x1, y1, _t_ar)
            if (_cx1 - _cx0) >= W * 0.28 and (_cy1 - _cy0) >= H * 0.28:
                if (_cx0, _cy0, _cx1, _cy1) != (x0, y0, x1, y1):
                    print(f"[pipeline] 客廳區特寫：比例收斂 {x1-x0}x{y1-y0} → "
                          f"{_cx1-_cx0}x{_cy1-_cy0}（目標 {_t_ar:.3f}，"
                          f"model={_legacy_render_model()}）")
                x0, y0, x1, y1 = _cx0, _cy0, _cx1, _cy1
            else:
                print(f"[pipeline] 客廳區特寫：比例收斂後過小（{_cx1-_cx0}x{_cy1-_cy0}），"
                      f"維持未收斂裁切（比例差仍在，但總比廢底圖好）")
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        out = str(Path(job_dir) / f"crop_living_zone_{idx:02d}.jpg")
        if not cv2.imwrite(out, crop):
            return None
        print(f"[pipeline] 客廳區特寫裁切 {W}x{H} → {x1-x0}x{y1-y0}"
              f"（大門{'已' if door_excluded else '未'}出鏡）")
        return out, (x0, y0, x1, y1)
    except Exception as e:
        print(f"[pipeline] 客廳區裁切失敗（略過）: {type(e).__name__}: {str(e)[:80]}")
        return None


def _rebuild_guide_on_zoom(zoom_base: str, job_dir, idx: int,
                           zoning_result: dict, source_path: str,
                           crop_box) -> tuple[str | None, bool | None]:
    """在客廳區特寫上重畫引導圖。門若已裁出鏡就不再傳門框——
    畫面裡沒有門，規劃器就不必為它保留禁區，這正是特寫的價值。

    回傳 (guide_path, door_visible)：door_visible 只有在入口 bbox 格式與座標有效時
    才是 True/False；缺入口資料、bbox 損壞或讀圖失敗時回 None，呼叫端必須保守
    視為門仍可能在鏡內。
    """
    try:
        import cv2
        src = cv2.imread(str(source_path))
        zoom = cv2.imread(str(zoom_base))
        if src is None or zoom is None:
            return None, None
        oh, ow = src.shape[:2]
        zh, zw = zoom.shape[:2]
        zones = zoning_result.get("zones") or {}

        def _to_zoom(key):
            bb = ((zones.get(key) or {}).get("bbox_on_best_photo"))
            return _bbox1000_to_crop_px(bb, ow, oh, crop_box) if bb else None

        entrance_bbox = ((zones.get("entrance_zone") or {}).get("bbox_on_best_photo"))
        entrance_bbox_valid = False
        if isinstance(entrance_bbox, (list, tuple)) and len(entrance_bbox) == 4:
            try:
                ey0, ex0, ey1, ex1 = [float(v) for v in entrance_bbox]
                entrance_bbox_valid = ex1 > ex0 and ey1 > ey0
            except (TypeError, ValueError):
                entrance_bbox_valid = False
        door = (_bbox1000_to_crop_px(entrance_bbox, ow, oh, crop_box)
                if entrance_bbox_valid else None)
        door_visible = (bool(door) if entrance_bbox_valid else None)
        living = _to_zoom("living_zone")
        blocked = [b for b in (_to_zoom("walkway"), _to_zoom("no_go_zone")) if b]
        if door and blocked:
            blocked = [b for b in blocked if not _rects_intersect(b, door)]
        guide_path = _build_layout_guide_image(
            zoom_base, job_dir, idx, _guide_sofa_side(zoning_result),
            entrance_side=_entrance_side_from_zoning(zoning_result) if door else "",
            entrance_bbox=door,
            focal_side=_preferred_focal_side(zoning_result),
            auto_float=False, blocked_rects=blocked, living_bbox=living,
        )
        return guide_path, door_visible
    except Exception as e:
        print(f"[pipeline] 特寫引導圖重建失敗（略過）: {type(e).__name__}: {str(e)[:80]}")
        return None, None


def _incomplete_message(validation_summary: dict | None) -> str:
    """交不出圖時給客戶的文案要對得上真實死因。

    2026-07-19 fal 餘額耗盡，一張圖都沒生出來，客戶看到的卻是
    「主空間仍未通過配置驗收」——把系統問題說成設計問題，害人往格局方向查了
    一整天。沒有圖可驗的時候絕不能說「驗收沒過」。
    """
    dropped = [d for d in ((validation_summary or {}).get("dropped_renders") or [])
               if isinstance(d, dict)]
    classes = {d.get("failure_class") for d in dropped}
    if any(
        d.get("failure_class") == "s2_preflight_blocked"
        or d.get("layout_mode") == "s2_blocked_legacy"
        for d in dropped
    ):
        return ("這個客廳視角的安全配置前檢未通過；系統已在生成前停止，"
                "未產生錯誤設計圖。建議改用能完整看見左右牆與大門的正面照片再試")
    # 斜角／碎牆房：幾何模型描述不了這個視角，同一張照片再重跑幾次都一樣，
    # 唯一有效的動作是改用正面拍攝。不講清楚的話客服和客戶只會一直重跑。
    if any(d.get("layout_mode") == "legacy_fallback" for d in dropped):
        return ("這個拍攝角度我們的空間建模無法完整判讀（斜角或牆面被多個門切斷），"
                "建議站在客廳一端、鏡頭順著長邊正面重拍一張再試，成功率最高")
    system_only = bool(classes) and classes <= {"infrastructure", "validator_exception"}
    if system_only:
        return "系統暫時無法完成生成（非設計問題），我們已收到通知，請聯絡客服協助重跑"
    return "主空間仍未通過配置驗收，請聯絡客服重新處理"


def _slim_validation_summary(summary: dict | None) -> dict | None:
    """精簡／極簡 payload 專用的 validation_summary：只留死因摘要，丟掉 raw_verdict。

    精簡 payload 存在的唯一理由是「小到一定寫得進去」（ED3B66EF：完整版卡在
    result_upsert，渲染到 92% 交不出去）——它本來就刻意捨棄逐圖 validation。
    完整 validation_history 每個 event 都帶一份完整判官輸出（實測 1,567 bytes），
    3 視角 × 6 次重試 ≈ 28KB，正好是「重試最多、最需要診斷」的單最肥。
    那會讓救命的退路自己寫不進去，客戶連圖都拿不到。
    所以這裡只留 failure_class / stage / attempt / exception —— 三層 payload
    都查得到死在哪層、為什麼，完整證據鏈留在完整版與 Railway log。
    """
    if not isinstance(summary, dict):
        return summary
    slim = {k: v for k, v in summary.items() if k not in ("dropped_renders",)}
    dropped = []
    for d in (summary.get("dropped_renders") or []):
        if not isinstance(d, dict):
            continue
        final = d.get("validation_final") or {}
        dropped.append({
            **{k: d.get(k) for k in
               # layout_mode 必須留：incomplete 文案靠它判斷要不要叫客戶正面重拍。
               # 掉了就會退回通用的「配置驗收」，客戶又去重跑同一張斜角照片。
               # blocked_render_url 也要留：那是付費生成的落選圖，掉了就白花錢又沒得看。
               ("style", "style_label", "angle_label", "room_type", "timeout", "reason",
                "failure_class", "validation_stage", "validation_attempt_count",
                "layout_mode", "blocked_render_url")},
            "validation_final": {
                **{k: final.get(k) for k in
                   ("ok", "hard_fail", "validation_unavailable", "validation_outage",
                    "exception_type")},
                "exception_message": str(final.get("exception_message") or "")[:200] or None,
            },
            # 逐次嘗試只留「哪一階段、第幾次、什麼死因」，不帶 raw_verdict
            "validation_trail": [
                {k: h.get(k) for k in
                 ("validation_stage", "attempt", "failure_class", "exception_type")}
                for h in (d.get("validation_history") or [])
            ],
        })
    if dropped:
        slim["dropped_renders"] = dropped
    # shadow 契約明細同樣是純觀測用的大物件，精簡版只留件數
    _shadow = slim.get("layout_contract_shadow")
    if isinstance(_shadow, dict) and _shadow.get("items"):
        slim["layout_contract_shadow"] = {
            "count": _shadow.get("count"), "affects_delivery": False, "items_trimmed": True}
    return slim


def _retry_metrics(validation: dict | None) -> dict:
    """抽出「可以比較進退」的量測值——重試有沒有在收斂,只能看數字。

    真實資料（199 次生成 → 54 張交付,47% 是重試）顯示重試不是全然浪費:
    單視角客廳 6 張交付裡有 5 張是靠重試救回來的,所以不能一刀砍重試上限。
    真正的浪費是「卡在同一個閘門、數字完全沒動」的那種——10AAED25 的門距
    三次都是 0（毫無進展）,但同一張的成對錯位 92→87→85→74→72（在收斂）。
    所以判準是「這個閘門的數字有沒有變好」,不是「重試過幾次」。
    """
    if not isinstance(validation, dict):
        return {}
    metrics: dict[str, float] = {}
    boxes = validation.get("render_bboxes") or {}
    try:
        from gemini_analyze import _door_adjacency_violation
        viol = _door_adjacency_violation(boxes)
        if viol:
            name, gap, door_w, _thr = viol
            # 以門寬正規化,不同房型/裁切之間才可比
            metrics[f"door_gap_{name}"] = float(gap) / max(1.0, float(door_w))
    except Exception:
        pass
    pair = _pair_center_delta(validation, tolerance=0)
    if pair:
        # 錯位越小越好 → 取負值統一成「越大越好」
        metrics["pair_align"] = -float(pair.get("abs_delta_y") or 0)
    return metrics


# 進步門檻：小於這個幅度視為原地踏步（門距以門寬為單位、錯位以 1000 分之一為單位）
RETRY_PROGRESS_EPS = {"door_gap": 0.02, "pair_align": 3.0}


def _console_repair_candidate_is_monotonic(
    previous_validation: dict | None,
    candidate_validation: dict | None,
) -> tuple[bool, str]:
    """電視櫃離門修復只能改善門距，不得破壞已通過的對向／結構條件。

    7B39FD17：門距修到通過，但 TV 被搬到沙發同側（中心差 203）；後續再修
    對向又把 TV 拉回門邊。這裡在候選成為下一輪底圖前做確定性拒收，避免修復互相覆寫。
    """
    prev = previous_validation or {}
    cand = candidate_validation or {}
    boxes = cand.get("render_bboxes") or {}
    if not all(isinstance(boxes.get(name), (list, tuple)) and len(boxes[name]) == 4
               for name in ("entrance_door", "focal_anchor", "sofa")):
        return False, "candidate bbox incomplete"

    prev_focal = (prev.get("render_bboxes") or {}).get("focal_anchor")
    cand_focal = boxes.get("focal_anchor")
    if not (isinstance(prev_focal, (list, tuple)) and len(prev_focal) == 4):
        return False, "previous focal bbox incomplete"
    prev_wall_side = (
        "left" if (float(prev_focal[1]) + float(prev_focal[3])) / 2.0 < 500.0
        else "right"
    )
    cand_wall_side = (
        "left" if (float(cand_focal[1]) + float(cand_focal[3])) / 2.0 < 500.0
        else "right"
    )
    if cand_wall_side != prev_wall_side:
        return False, f"console wall side changed ({prev_wall_side}→{cand_wall_side})"

    regression_flags = (
        "furniture_blocks_walkway", "sofa_intrudes_walkway", "sofa_faces_walkway",
        "sofa_on_wrong_side", "sofa_outside_living_zone", "sofa_back_against_window",
        "sofa_facing_window", "sofa_facing_entrance_door", "spatial_fidelity_fail",
        "windows_changed", "walls_changed", "ceiling_changed", "floor_changed",
        "offframe_room_invaded", "recessed_space_added",
    )
    newly_failed = [flag for flag in regression_flags
                    if cand.get(flag) is True and prev.get(flag) is not True]
    if newly_failed:
        return False, "new hard failure=" + ",".join(newly_failed)
    if ((cand.get("camera_axis_preserved") is False
         and prev.get("camera_axis_preserved") is not False)
            or (cand.get("passage_openings_preserved") is False
                and prev.get("passage_openings_preserved") is not False)):
        return False, "camera/passage regressed"

    # 門距單調必須先算：EAF26AF6 實測修復候選是門距 2→0 更差、對向 90→31；
    # 舊版先用 PAIR_CENTER_TOLERANCE=25 回報，log 完全看不到門距惡化。
    # 先查門距＝可診斷；25 不得當複合修復的絕對交付門檻（交付硬閘是 EXTREME）。
    try:
        from gemini_analyze import _door_adjacency_violation
        prev_violation = _door_adjacency_violation(prev.get("render_bboxes") or {})
        cand_violation = _door_adjacency_violation(boxes)
    except Exception as exc:
        return False, f"door metric unavailable ({type(exc).__name__})"
    if cand_violation:
        if cand_violation[0] != "focal_anchor":
            return False, f"door offender changed to {cand_violation[0]}"
        if prev_violation and prev_violation[0] == "focal_anchor":
            prev_gap = float(prev_violation[1]) / max(1.0, float(prev_violation[2]))
            cand_gap = float(cand_violation[1]) / max(1.0, float(cand_violation[2]))
            if cand_gap - prev_gap <= RETRY_PROGRESS_EPS["door_gap"]:
                return False, f"console door gap stalled ({prev_gap:.2f}→{cand_gap:.2f})"

    prev_pair = _pair_center_delta(prev, tolerance=0)
    cand_pair = _pair_center_delta(cand, tolerance=0)
    prev_abs = int((prev_pair or {}).get("abs_delta_y") or 0)
    cand_abs = int((cand_pair or {}).get("abs_delta_y") or 0)

    if isinstance(prev.get("focal_door_axis_conflict"), dict):
        # 複合修復（門距+對向）：藍框目標是 EXTREME 內 + 不比上一輪更歪。
        # 不得要求 ≤25——那是美感微調，不是硬擋；31 已優於接受組裡的 32/50/60/89。
        if cand_abs > PAIR_CENTER_EXTREME:
            return False, (
                f"console/sofa axis still extreme "
                f"({cand_abs}/1000 > {PAIR_CENTER_EXTREME})"
            )
        if cand_abs - prev_abs > RETRY_PROGRESS_EPS["pair_align"]:
            return False, (
                f"console/sofa axis regressed ({prev_abs}→{cand_abs}/1000)"
            )
    else:
        pair = _pair_center_delta(cand, tolerance=PAIR_CENTER_EXTREME)
        if cand.get("focal_anchor_misaligned_with_sofa") is True or pair:
            delta = (pair or {}).get("abs_delta_y")
            return False, (
                f"pair alignment regressed"
                f"{f' ({delta}/1000)' if delta is not None else ''}"
            )
        # 純避門：對向原本已過，不得變差超過 eps（仍可比 25 寬，但不能退步）
        if prev_abs <= PAIR_CENTER_TOLERANCE and cand_abs - prev_abs > RETRY_PROGRESS_EPS["pair_align"]:
            return False, (
                f"pair alignment regressed ({prev_abs}→{cand_abs}/1000)"
            )
    return True, ""


_Z3_REGRESSION_FLAGS = (
    "furniture_blocks_door", "furniture_blocks_walkway", "sofa_intrudes_walkway",
    "sofa_faces_walkway", "sofa_on_wrong_side", "sofa_outside_living_zone",
    "sofa_back_against_window", "sofa_facing_window", "sofa_facing_entrance_door",
    "coffee_table_in_walkway", "focal_anchor_misaligned_with_sofa",
    "spatial_fidelity_fail", "windows_changed", "walls_changed", "ceiling_changed",
    "floor_changed", "offframe_room_invaded", "recessed_space_added", "kitchen_added",
    "product_visibility_fail", "product_sofa_seating_mismatch", "guide_overlay_present",
)


def _z3_candidate_regression_reason(
    previous_validation: dict | None,
    candidate_validation: dict | None,
) -> str | None:
    """拒絕會新增硬傷的 Z3 候選；舊版不得被更差的新圖覆蓋。"""
    prev = previous_validation or {}
    cand = candidate_validation or {}
    if not cand:
        return "candidate validation missing"
    if cand.get("validation_unavailable") or cand.get("validation_outage"):
        return "candidate validation unavailable"
    newly_failed = [flag for flag in _Z3_REGRESSION_FLAGS
                    if cand.get(flag) is True and prev.get(flag) is not True]
    if newly_failed:
        return "new hard failure=" + ",".join(newly_failed)
    for field in ("camera_axis_preserved", "passage_openings_preserved",
                  "main_window_region_match", "sofa_focal_face_each_other",
                  "product_sofa_seating_match"):
        if cand.get(field) is False and prev.get(field) is not False:
            return f"{field} regressed"
    if cand.get("hard_fail") is True and prev.get("hard_fail") is not True:
        return "candidate introduced hard_fail"
    return None


def _retry_is_stuck(prev: dict | None, cur: dict | None) -> tuple[bool, str]:
    """同一個閘門連兩次擋、數字沒有變好 → 再生一次也是同樣結果,別燒。

    只有「兩次都量得到的同一個指標」才拿來比；任何一項有進步就放行重試。
    量不到（判官沒給 bbox、換了失敗原因）一律放行——省錢不得優先於交付。
    """
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        return False, ""
    shared = [k for k in cur if k in prev]
    if not shared:
        return False, ""
    stalled = []
    for k in shared:
        eps = RETRY_PROGRESS_EPS["door_gap"] if k.startswith("door_gap") else \
            RETRY_PROGRESS_EPS.get(k, 0.0)
        if float(cur[k]) - float(prev[k]) > eps:
            return False, ""          # 任一指標真的變好 → 值得再試
        stalled.append(f"{k} {prev[k]:.2f}→{cur[k]:.2f}")
    return True, "；".join(stalled)


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
        # 純診斷：本機底圖 → 原始 storage key。guide trace 只存 key，不存本機暫存路徑。
        photo_key_by_local: dict[str, str] = {}
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
                    photo_key_by_local[str(local)] = key
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
                    photo_key_by_local[str(local)] = key
                else:
                    print(f"[pipeline] Supabase 影片 {key} 下載失敗，跳過")
            else:
                resolved_paths.append(p)
        photo_paths = resolved_paths

        gemini_uris = [p[len("gemini://"):] for p in photo_paths if p.startswith("gemini://")]
        video_paths = [p for p in photo_paths if not p.startswith("gemini://") and Path(p).suffix.lower() in VIDEO_EXTS]
        image_paths = [p for p in photo_paths if not p.startswith("gemini://") and Path(p).suffix.lower() not in VIDEO_EXTS]

        # 單空間不吃影片：影片的價值在「全室理解」（房間怎麼連、動線、房別歸屬、
        # 尺寸校正、口述需求），單一房間全都拿不到，幾何又是單張——送影片只會多燒
        # token、幾乎零增量。單空間一律純照片分析；只有全室（space_type=whole）才
        # 把影片送進 Gemini 理解。（付款前分區已一律純照片，見 /api/zoning。）
        _is_whole = str(space_type or "").strip().lower() == "whole"
        # 只在「有照片可分析」時才丟影片——單空間若只有影片沒照片（邊角情況），
        # 仍保留影片當唯一素材，不能清空害它沒東西可分析。
        if video_paths and not _is_whole and image_paths:
            print(f"[pipeline] 單空間（space_type={space_type}）→ 影片不進分析，"
                  "純照片理解（影片價值僅在全室，省 token）")
            video_paths = []

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
        # 純診斷：guide 沒畫成時是哪一條路徑擋的。只寫不讀，不參與任何交付/驗證/重試判斷。
        layout_guide_skip_reasons: dict[int, str | None] = {}
        for _vi, (_bp, _rt) in enumerate(zip(flux_bases, angle_room_types)):
            if _rt == "living" and os.environ.get("LAYOUT_GUIDE", "1").strip() != "0":
                if _conservative_layout_reason:
                    layout_guide_paths[_vi] = None
                    layout_guide_modes[_vi] = "conservative_no_binding"
                    layout_guide_skip_reasons[_vi] = "conservative_no_binding"
                    continue
                layout_guide_modes[_vi] = (
                    "auto_float" if _sofa_side_for_guide == "free" and _auto_float_for_guide
                    else "auto_constraints" if _sofa_side_for_guide == "free"
                    else "bound_constraints"
                )
                if zone_crop_flags[_vi]:
                    print(f"[pipeline] guide[{_vi}] 略過：zone crop 尚無可驗證座標轉換")
                    layout_guide_paths[_vi] = None
                    layout_guide_skip_reasons[_vi] = "zone_crop_no_verified_transform"
                    continue
                _source_matches_zoning = _zoning_bbox_matches_source(
                    crop_source_paths[_vi], image_paths, user_zoning_v2 or {})
                if user_zoning_v2 and not _source_matches_zoning:
                    print(f"[pipeline] guide[{_vi}] 略過：底圖不是 zoning 主視角，禁止跨照片套 bbox")
                    layout_guide_paths[_vi] = None
                    layout_guide_skip_reasons[_vi] = "base_not_zoning_primary_photo"
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
                    layout_guide_skip_reasons[_vi] = "zoning_bbox_map_exception"
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
                # 走完全部前置檢查卻仍畫不出來，是另一種失敗，要跟「被前面擋掉」分開
                layout_guide_skip_reasons[_vi] = (
                    None if layout_guide_paths[_vi] else "guide_render_returned_empty")

        # ── S2 authoritative geometry Contract；flag off 時保留 S1 shadow ──
        layout_contract_shadows: list[dict] = []
        layout_contract_artifacts: dict[int, dict] = {}
        # S2 模型化不了的視角：豁免付費前 S2 強制，改走 legacy 門感知引導
        layout_contract_s2_waived: set[int] = set()
        # 硬擋時同圖重抽 zoning 的觀測紀錄（付 Fal 前；不進布局規則）
        s2_zoning_resample_log: list[dict] = []
        _s2_enabled = _layout_contract_s2_enabled()
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
                # S2 只在未裁切、包含大門與完整牆腳的原圖座標工作。
                _contract_photo = uncropped_bases.get(_vi) or crop_source_paths[_vi] or _bp
                if _s2_enabled:
                    def _run_one_s2(_zv2, *, _photo=_contract_photo, _view=_vi,
                                    _sofa=_sofa_mode_shadow, _float=_can_float_shadow,
                                    _legacy=zoning_result):
                        return _run_layout_contract_s2(
                            job_id=job_id,
                            job_dir=job_dir,
                            photo_path=_photo,
                            view_index=_view,
                            user_zoning_v2=_zv2,
                            legacy_zoning=_legacy,
                            sofa_mode=_sofa,
                            image_paths=image_paths,
                            can_float=_float,
                        )

                    _sum, _artifacts, _zv2_commit, _zlog = _s2_contract_with_zoning_resample(
                        initial_zoning_v2=user_zoning_v2,
                        photo_path=_contract_photo,
                        run_contract=_run_one_s2,
                    )
                    if _zlog:
                        # 不存 Railway 容器的完整路徑：容器一換就失效，還把執行環境
                        # 寫進客戶訂單。photo_key ＋ sha 才是跨部署對得起來的識別。
                        s2_zoning_resample_log.append({
                            "view_index": _vi,
                            "photo_key": _portable_photo_key(
                                _contract_photo, zoning=user_zoning_v2,
                                key_by_local=photo_key_by_local),
                            "photo_sha256": _source_file_sha256(_contract_photo),
                            "attempts": _zlog,
                        })
                    # 重抽後合格：只把幾何換掉。
                    # 刻意**不**重跑 flatten／不重算 _sofa_mode_shadow／_can_float_shadow——
                    # _s2_zoning_with_resampled_geometry 只換結構觀測，proposed_zones 與
                    # existing_zones 原樣保留，所以 flatten 出來會是同一份，重跑只是多一次
                    # 有副作用的機會（_auto_can_float 重算），而且一旦換側就等於用 AI 建議
                    # 冒充客戶指定（e318392 修過的那類錯）。
                    if isinstance(_zv2_commit, dict):
                        user_zoning_v2 = _zv2_commit
                        print(f"[pipeline] S2 重抽 zoning 成功 view={_vi} "
                              f"attempts={len(_zlog)} → 已替換幾何"
                              f"（側別 {_sofa_mode_shadow} / can_float {_can_float_shadow} 不變）")
                    layout_contract_shadows.append(_sum)
                    layout_contract_artifacts[_vi] = _artifacts
                    if _artifacts.get("eligible"):
                        _sync_s2_candidate_sides(
                            zoning_result, _artifacts.get("contract") or {})
                        # 座標與生成底圖保持同一張原圖；禁止 crop 把門或走道藏掉。
                        flux_bases[_vi] = _contract_photo
                        crop_flags[_vi] = False
                        zone_crop_flags[_vi] = False
                        crop_notes[_vi] = "s2_bound_uncropped_source"
                        door_excluded_flags[_vi] = False
                        layout_guide_paths[_vi] = _artifacts["guide_path"]
                        layout_guide_modes[_vi] = "auto_s2_contract"
                        layout_guide_skip_reasons[_vi] = None
                    elif (_s2_model_not_applicable(_sum) or _s2_verifier_unstable(_sum)
                          or _s2_blocked_fallback_enabled()):
                        # S2 的幾何模型建立在「兩面相對長牆＋共同深度軸」上，只吃
                        # 正面拍攝的長條房。兩種「S2 模型化不了這房型」都回退 legacy：
                        # ①連候選都生不出來（NO_USABLE_WALL，3135DE37 斜角方正房）；
                        # ②候選生得出來但判官 fail 且 fail 欄位跨多次不穩定
                        #   （173C14C5：sofa_back/left_wall/right_wall/walkway/cross_axis
                        #   每次亂跳＝判官對此房型不確定，不是穩定的真不安全）。
                        # 「這個房型我模型化不了」不等於「這個配置不安全」：前者交回
                        # legacy 門感知引導＋生成後校準閘門把關，後者才該擋。
                        # 第三種情形（2026-08-01 新增）：判官驗過且判不安全。
                        # 以前這條走 else → s2_blocked_legacy → 付費前檢擋死 → 客戶零圖。
                        # 用戶裁決「零圖傷害最大」＋離線量到 legacy 對這類單 7/7 出得了
                        # 配置，改成一律退 legacy。見 _s2_blocked_fallback_enabled。
                        _waive_why = (
                            "verifier_unstable" if _s2_verifier_unstable(_sum)
                            else ",".join(_sum.get("unsafe_codes") or [])
                            or "verifier_blocked")
                        if not (_s2_model_not_applicable(_sum)
                                or _s2_verifier_unstable(_sum)):
                            _waive_why = f"verifier_blocked({_waive_why})"
                        print(f"[pipeline] S2 不適用此房型（{_waive_why}）"
                              "→ 回退 legacy 門感知引導，不擋生成")
                        layout_contract_s2_waived.add(_vi)
                        layout_guide_modes[_vi] = layout_guide_modes.get(
                            _vi, "bound") or "bound"
                        # 整個房型描述不了，就退一步只做「客廳區特寫」：分區層認得
                        # 出哪一塊是客廳，裁進去之後門窗雜訊出鏡、相對兩牆回來，
                        # 規劃器就有解了（928AD8B4 實測）。
                        _lz_bbox = ((zoning_result.get("zones") or {}).get(
                            "living_zone") or {}).get("bbox_on_best_photo")
                        # 2D212624：living_zone 幾乎蓋滿整張（含大門），只裁客廳區
                        # 門還在畫面裡 → 電視櫃緊貼大門落選。把入口 bbox 一起傳進去，
                        # 讓裁切把門推出鏡頭（門在鏡外＝對門問題物理上不存在）。
                        _ent_bbox = ((zoning_result.get("zones") or {}).get(
                            "entrance_zone") or {}).get("bbox_on_best_photo")
                        _zoom = _crop_to_living_zone(
                            _contract_photo, job_dir, _vi, _lz_bbox,
                            entrance_bbox1000=_ent_bbox)
                        if _zoom:
                            _zoom_base, _zoom_box = _zoom
                            flux_bases[_vi] = _zoom_base
                            uncropped_bases.setdefault(_vi, _contract_photo)
                            crop_flags[_vi] = True
                            crop_boxes[_vi] = _zoom_box
                            crop_notes[_vi] = "s2_waived_living_zone_zoom"
                            zone_crop_flags[_vi] = True
                            _zoom_guide, _zoom_door_visible = _rebuild_guide_on_zoom(
                                _zoom_base, job_dir, _vi, zoning_result,
                                _contract_photo, _zoom_box)
                            # 只有入口 bbox 明確完全落在 crop 外才可關閉避門 prompt。
                            # None（缺 bbox／讀圖失敗）一律保守當門仍可能在鏡內。
                            door_excluded_flags[_vi] = (_zoom_door_visible is False)
                            # zoom 後座標系已變；重建失敗不得沿用裁切前的舊 guide。
                            layout_guide_paths.pop(_vi, None)
                            layout_guide_modes[_vi] = "living_zone_zoom"
                            if _zoom_guide:
                                layout_guide_paths[_vi] = _zoom_guide
                                layout_guide_skip_reasons[_vi] = None
                            else:
                                layout_guide_skip_reasons[_vi] = "zoom_guide_rebuild_failed"
                    else:
                        # 只有 S2_BLOCKED_FALLBACK=0（急救關閉退回機制）才會走到這裡：
                        # 判官驗過且判不安全 → 不回退 S2，付費前檢會擋死（客戶零圖）。
                        # 保留 legacy 引導圖（如果前面 _build_layout_guide_image 已建好）。
                        # 30FBA4A5 教訓：舊版直接 pop 拔掉引導圖 → 模型在零引導
                        # 下自己擺 → 沙發貼死門框（gap=0）。legacy 引導圖帶 door_clear
                        # 禁區，至少讓模型看得到門邊淨空要求；生成後閘門照樣把關。
                        if not layout_guide_paths.get(_vi):
                            layout_guide_paths.pop(_vi, None)
                            layout_guide_skip_reasons.setdefault(
                                _vi, "s2_blocked_no_legacy_guide")
                        layout_guide_modes[_vi] = "s2_blocked_legacy"
                    continue

                # S1 fallback｜只觀測，不接正式交付。
                if user_zoning_v2 and not _zoning_bbox_matches_source(
                        _contract_photo, image_paths, user_zoning_v2 or {}):
                    layout_contract_shadows.append({
                        "view_index": _vi, "status": "skipped",
                        "reason": "photo_not_zoning_best_photo",
                        "affects_delivery": False,
                    })
                    continue
                _sum = _run_layout_contract_shadow(
                    job_id=job_id,
                    job_dir=job_dir,
                    photo_path=_contract_photo,
                    view_index=_vi,
                    zoning_result=zoning_result,
                    user_zoning_v2=user_zoning_v2,
                    analysis=analysis,
                    sofa_mode=_sofa_mode_shadow,
                    can_float=_can_float_shadow,
                    image_paths=image_paths,
                )
                if _sum:
                    layout_contract_shadows.append(_sum)
        except Exception as _shadow_err:
            # S2 開啟時後續 paid preflight 仍會因缺 artifact fail closed。
            print(f"[pipeline] layout_contract 批次例外: {_shadow_err}")

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
                _s2_artifact = layout_contract_artifacts.get(vi) or {}
                copy["_layout_contract_s2_required"] = bool(
                    _s2_enabled and rt == "living" and vi not in layout_contract_s2_waived)
                # 交付紀錄要分得出這張圖是走 S2 幾何合約還是 legacy 回退——
                # 兩者品質保證不同，混在一起看不出模型覆蓋率夠不夠。
                copy["_layout_mode"] = (
                    "legacy_fallback" if vi in layout_contract_s2_waived
                    else "s2_contract" if layout_guide_modes.get(vi) == "auto_s2_contract"
                    else layout_guide_modes.get(vi) or "legacy")
                if rt == "living":
                    # 純診斷：規劃階段先記一筆，讓「還沒生成就被擋掉」的張數
                    # （s2_preflight_blocked）也留得下 guide 紀錄。
                    _origin = uncropped_bases.get(vi) or base
                    append_guide_trace(copy, guide_trace_record(
                        stage="plan",
                        attempt=0,
                        layout_mode=copy["_layout_mode"],
                        guide_path=layout_guide_paths.get(vi),
                        # 原圖身分與 guide 畫布身分分開存：裁切/zoom 後兩者不同張
                        original_source_path=_origin,
                        original_source_key=photo_key_by_local.get(str(_origin)),
                        guide_canvas_path=base,
                        coordinate_space=(
                            "living_zone_zoom_crop" if zone_cropped
                            else "cropped_source" if cropped
                            else "uncropped_source"),
                        skip_reason=layout_guide_skip_reasons.get(vi),
                    ))
                # 兩套規劃器都描述不了、又沒有 guide 時，只在大門已確認完全出鏡
                # 才允許單次生成。門仍可見或狀態未知時，裸生只會把家具貼回門邊。
                # 豁免旗標要顯式帶到生成端：付費前閘門的全域開關會蓋掉
                # _layout_contract_s2_required=False，只有這個旗標壓得過去。
                copy["_layout_contract_s2_waived"] = bool(vi in layout_contract_s2_waived)
                copy["_allow_single_shot_without_guide"] = (
                    _allow_waived_single_shot_without_guide(
                        vi in layout_contract_s2_waived,
                        rt,
                        layout_guide_paths.get(vi),
                        bool(door_excluded_flags[vi]),
                    )
                )
                copy["_s2_compact_entry_mode"] = False
                _s2_contract_path = _s2_artifact.get("contract_path")
                if copy["_layout_contract_s2_required"] and _s2_contract_path:
                    try:
                        _s2_contract_data = json.loads(
                            Path(_s2_contract_path).read_text(encoding="utf-8")
                        )
                        copy["_s2_compact_entry_mode"] = _s2_compact_entry_mode(
                            zoning_result, _s2_contract_data,
                        )
                    except Exception:
                        copy["_s2_compact_entry_mode"] = False
                copy["_room_type"] = rt
                copy["_layout_contract_s2"] = _s2_contract_path
                copy["_layout_contract_s2_sha256"] = _s2_artifact.get("contract_sha256")
                copy["_layout_reconciliation_s2"] = _s2_artifact.get("reconciliation_path")
                copy["_layout_reconciliation_s2_sha256"] = _s2_artifact.get("reconciliation_sha256")
                copy["_layout_geometry_verification_s2"] = _s2_artifact.get("verification_path")
                copy["_layout_geometry_verification_s2_sha256"] = _s2_artifact.get("verification_sha256")
                copy["_layout_guide_s2_sha256"] = _s2_artifact.get("guide_sha256")
                # 生成 prompt 也要講同一塊禁區：引導圖已經把它漆紅，但文字若只說
                # 「紅色是門／玄關」，模型就不知道那條橫貫的紅帶是進門通道。
                # 只帶「有沒有／門在哪面牆」這個訊號，座標仍只有幾何端持有。
                copy["_s2_entrance_no_go_side"] = (
                    ((_s2_artifact.get("contract") or {}).get("extensions") or {})
                    .get("s2_entrance_no_go_door_wall_side"))
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
        _p1_detect_cache: dict = {}   # P1 首渲偵測：同 base 跨風格只偵測一次
        # 一次渲染一張：對應 base 不同（analysis + design_mode 傳進去）
        final = []
        for idx, entry in enumerate(expanded):
            if (_cross_room_on and entry.get("_room_type") == "dining"
                    and _living_render_by_style.get(entry.get("style"))):
                entry["_consistency_ref_path"] = _living_render_by_style[entry.get("style")]
            # S2 判官已經驗過且判定候選不安全：這是終態，不是生成失敗。
            # 直接建立可診斷的 no-render 結果，禁止進 generate/validate/retry 空轉。
            preflight_blocked = _s2_preflight_blocked_result(entry)
            if preflight_blocked is not None:
                preflight_blocked["angle_label"] = entry.get("_angle_label", "主視角")
                preflight_blocked["room_type"] = entry.get("_room_type", "living")
                preflight_blocked["cropped"] = bool(entry.get("_cropped"))
                preflight_blocked["door_excluded"] = bool(entry.get("_door_excluded"))
                _record_validation_attempt(
                    preflight_blocked,
                    job_id=job_id,
                    stage="pre_generation",
                    attempt=1,
                    validation=preflight_blocked["validation"],
                )
                # ── 影子模式（S2_BLOCK_LEGACY_SHADOW，預設關）：只量數據、不交付、
                # 不改客戶看到的封鎖行為。收集「S2 擋掉的房，legacy 能不能救」。
                #   =1    免費層：只記 legacy 有沒有引導圖（零 fal / 零 Gemini）
                #   =full fal 層：額外跑一次 legacy 生成+驗證，記有沒有過生成後閘門
                _shadow_mode = os.environ.get("S2_BLOCK_LEGACY_SHADOW", "0").strip().lower()
                if _shadow_mode not in ("", "0", "off") and entry.get("_room_type") == "living":
                    try:
                        _shadow = _s2_shadow_free_signal(
                            entry, (preflight_blocked["validation"] or {}).get("reason") or "")
                        _shadow["job"] = job_id
                        _shadow["idx"] = idx
                        if _shadow_mode == "full" and _shadow["legacy_guide"]:
                            from gemini_analyze import validate_render as _vr
                            _sc = dict(entry)
                            _sc["_layout_contract_s2_required"] = False
                            _sc["_layout_contract_s2_waived"] = True
                            _sc["_allow_single_shot_without_guide"] = False
                            _sc["_s2_retry_artifacts_active"] = False
                            _sg = generate_renders(
                                entry["_base_path"], [_sc], output_dir=str(job_dir),
                                analysis=analysis, design_mode=design_mode, zoning=zoning_result,
                                customer_notes=customer_notes, budget_tier=budget_tier,
                                force_anchored=force_anchored, job_id=job_id,
                                upload_id_masked=uid_masked, attempt=1,
                                stage="s2_shadow_legacy", room_type="living")
                            _sr = (_sg or [{}])[0] if _sg else {}
                            _srp = _sr.get("render_path")
                            if _srp and Path(_srp).exists():
                                _sv = _fail_closed_validation(
                                    _vr(entry["_base_path"], _srp,
                                        entry.get("_angle_label", ""),
                                        layout_context=None, room_type="living",
                                        design_mode=design_mode),
                                    "living")
                                _shadow["shadow_generated"] = True
                                _shadow["shadow_passed"] = _sv.get("hard_fail") is not True
                                _shadow["shadow_fail"] = (
                                    "" if _sv.get("hard_fail") is not True
                                    else (_sv.get("reason") or "")[:120])
                            else:
                                _shadow["shadow_generated"] = False
                                _shadow["shadow_fail"] = "no_render(bare-gen擋/fal失敗)"
                        print("[s2-shadow] " + json.dumps(_shadow, ensure_ascii=False, default=str))
                    except Exception as _se:
                        print(f"[s2-shadow] 影子模式例外（不影響封鎖）: "
                              f"{type(_se).__name__}: {str(_se)[:100]}")
                print(f"[pipeline] render[{idx}] S2 前檢封鎖 → 終止，不進生成／補生鏈")
                final.append(preflight_blocked)
                continue
            # ── P1 首渲硬綁（P1_FIRST_RENDER_MASK，預設關）：S2 客廳首渲時偵測源照
            # 舊家具 → mask 清舊物 + 綁 S2 footprint，同時解「黏原位」與「黏外觀(cream 沙發)」。
            #   =0/off  關（預設）    =1/on  真掛 mask 進 fal（配 first_render_layout 保留完整 prompt）
            #   =shadow 偵測+建 mask 檔+log，不送 mask_url（先量、零 fal 影響）
            # 只掛首渲(attempt=1，此迴圈即是)；重試以 _s2_retry_artifacts_active 清掉不外漏。
            _p1_mode = os.environ.get("P1_FIRST_RENDER_MASK", "0").strip().lower()
            if (_p1_mode not in ("", "0", "off")
                    and entry.get("_room_type") == "living"
                    and entry.get("_layout_contract_s2_required") is True
                    and entry.get("_layout_contract_s2")):
                try:
                    _base = entry["_base_path"]
                    if _base not in _p1_detect_cache:
                        from gemini_analyze import detect_source_furniture as _dsf
                        _p1_detect_cache[_base] = _dsf(_base)
                    _det = _p1_detect_cache[_base] or {}
                    _p1_mask = _build_s2_first_render_mask(
                        _base, entry.get("_layout_contract_s2") or "", _det,
                        str(Path(job_dir) / f"mask_p1_first_{idx:02d}.png"))
                    if _p1_mask and _p1_mode in ("1", "on", "true"):
                        entry["_edit_mask_path"] = _p1_mask
                        entry["_edit_mask_mode"] = "first_render_layout"
                        entry["_force_mask_local_edit"] = True
                        entry["_s2_retry_artifacts_active"] = True  # 重試前會被清，不外漏
                        print(f"[p1] render[{idx}] 首渲硬綁 mask 掛上 "
                              f"sofa={bool(_det.get('sofa'))} "
                              f"ct={bool(_det.get('coffee_table'))} "
                              f"door={bool(_det.get('entrance_door'))}")
                    else:
                        print("[p1-shadow] " + json.dumps({
                            "job": job_id, "idx": idx, "mode": _p1_mode,
                            "mask_built": bool(_p1_mask),
                            "detect": {k: bool(_det.get(k)) for k in
                                       ("sofa", "coffee_table", "focal_anchor", "entrance_door")},
                        }, ensure_ascii=False))
                except Exception as _p1e:
                    print(f"[p1] 首渲硬綁例外（不影響生成）: "
                          f"{type(_p1e).__name__}: {str(_p1e)[:100]}")
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
                r["_s2_unmodelable"] = bool(entry.get("_layout_contract_s2_waived"))  # #2: S2建不了模型的房
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
                if r.get("_s2_preflight_blocked"):
                    continue
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
                            _record_validation_attempt(
                                r, job_id=job_id, stage="post_render", attempt=_v_try + 1,
                                validation=v)
                            break
                        except Exception as ve:
                            v = {"ok": None, "error": str(ve)[:500],
                                 "exception_type": type(ve).__name__}
                            _record_validation_attempt(
                                r, job_id=job_id, stage="post_render", attempt=_v_try + 1,
                                validation=v, error=ve)
                            print(f"[pipeline] 驗證崩潰（第 {_v_try+1} 次）"
                                  f"{r.get('style')}/{r.get('room_type')}: {str(ve)[:100]}")
                else:
                    v = {"ok": None, "error": "missing base or render path",
                         "exception_type": "MissingRenderPath"}
                    _record_validation_attempt(
                        r, job_id=job_id, stage="post_render", attempt=1, validation=v)
                r["validation"] = _fail_closed_validation(v, r.get("room_type", "living"))
        except Exception as outer:
            print(f"[pipeline] 驗證階段例外: {outer}")
            for r in final:
                if "validation" not in r:
                    _outer_v = {"ok": None, "error": str(outer)[:500],
                                "exception_type": type(outer).__name__}
                    _record_validation_attempt(
                        r, job_id=job_id, stage="post_render", attempt=1,
                        validation=_outer_v, error=outer)
                    r["validation"] = _fail_closed_validation(
                        _outer_v, r.get("room_type", "living"))

        # ── Z3: multi-image renderer 結構失敗自動重試；S2 固定 GPT Image 2 ──
        use_image_edit_retry = _image_edit_retry_enabled(final)
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
            "guide_overlay_present",     # S2 guide 色線／色塊滲入正式成品
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

        if use_image_edit_retry:
            failed_stage = "z3_retry"
            last_progress = 92
            # C2.6: anchored 白名單測試 retry 上限 = 1, legacy 維持 2
            MAX_RETRY = 1 if force_anchored else 2
            for idx in range(len(final)):
                # 每張 render 自己跑 retry loop（最多 MAX_RETRY 次）
                while True:
                    r = final[idx]
                    if r.get("_s2_preflight_blocked"):
                        print(f"[pipeline] render[{idx}] S2 前檢已封鎖 → 跳過 Z3")
                        break
                    current_rc = int(r.get("retry_count") or 0)
                    if current_rc >= MAX_RETRY:
                        break  # 硬上限
                    if r.get("_allow_single_shot_without_guide"):
                        # 無引導的單張放行單：重試只會拿同樣的文字條件再抽一次，
                        # 那正是這道閘門當初要防的燒錢模式。
                        print(f"[pipeline] render[{idx}] 無引導單張模式 → 不重試")
                        break
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
                    # 省錢閘門：同一個閘門連兩次擋且量測值沒變好 → 再生也是同一張。
                    # 只在已經重試過一次之後才判（第一次沒有比較基準）。
                    _cur_metrics = _retry_metrics(v)
                    if current_rc >= 1:
                        _stuck, _detail = _retry_is_stuck(r.get("_retry_metrics_prev"), _cur_metrics)
                        if _stuck:
                            print(f"[pipeline] Z3 停止重試 render[{idx}] — 量測無進展（{_detail}），"
                                  "不再燒 fal/判官")
                            r["retry_reason"] = (
                                f"{r.get('retry_reason') or ''} | 重試無進展停止：{_detail}").strip(" |")
                            break
                    r["_retry_metrics_prev"] = _cur_metrics
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
                    _clear_s2_retry_edit_artifacts(entry)
                    base_for_gen = entry["_base_path"]
                    pair_alignment_base = _activate_pair_alignment_edit(
                        v, r, entry, str(job_dir), idx)
                    console_base = None
                    alignment_base = None
                    repair_mode = None
                    if pair_alignment_base:
                        repair_mode = "pair_alignment"
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["tv_alignment_edit"] = True
                        base_for_gen = pair_alignment_base
                        retry_reason = (
                            f"TV/sofa pair centre correction "
                            f"(delta={v.get('pair_center_delta_y')}/1000)"
                        )
                    else:
                        # 593408CC：先分流貼門對象——電視櫃貼門走 console 遮罩硬修
                        console_base = _activate_console_door_edit(
                            v, r, entry, str(job_dir), idx, str(current_rc + 1))
                        if console_base:
                            repair_mode = "console_door"
                            retry_ctx = dict(retry_ctx or {})
                            retry_ctx["console_door_clearance_edit"] = True
                            if isinstance(v.get("focal_door_axis_conflict"), dict):
                                retry_ctx["console_axis_alignment_edit"] = True
                            base_for_gen = console_base
                            retry_reason = "console past door (mask hard repair)"
                        elif _door_block_offender(v) == "focal_anchor":
                            # 櫃貼門但算不出同時保住對正的安全目標，不退回通用重生。
                            r["_console_repair_exhausted"] = True
                            r["retry_reason"] = "console repair skipped: no pair-safe target"
                            print(f"[pipeline] Z3 render[{idx}] 電視櫃避門無安全目標 → 生成前停止")
                            break
                        else:
                            alignment_base = _sofa_alignment_edit_base(
                                v, r, entry.get("_room_type", "living"))
                    if alignment_base:
                        repair_mode = "sofa_alignment"
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["sofa_alignment_edit"] = True
                        if v.get("sofa_on_wrong_side") is True:
                            retry_ctx["sofa_cross_room_relocate"] = True
                        base_for_gen = alignment_base
                        if entry.get("_layout_contract_s2_required") is True:
                            repair_guide = _build_s2_sofa_repair_guide(
                                alignment_base,
                                entry.get("_layout_contract_s2") or "",
                                str(Path(job_dir) / f"guide_s2_sofa_repair_{idx:02d}_{current_rc + 1}.jpg"),
                                validation=v,
                                compact_entry_mode=entry.get("_s2_compact_entry_mode") is True,
                            )
                            if repair_guide:
                                entry["_consistency_ref_path"] = repair_guide
                            edit_mask = _build_s2_sofa_edit_mask(
                                alignment_base,
                                entry.get("_layout_contract_s2") or "",
                                v,
                                str(Path(job_dir) / f"mask_s2_sofa_repair_{idx:02d}_{current_rc + 1}.png"),
                                compact_entry_mode=entry.get("_s2_compact_entry_mode") is True,
                            )
                            if edit_mask:
                                entry["_edit_mask_path"] = edit_mask
                                entry["_edit_mask_mode"] = "sofa"
                            if repair_guide or edit_mask:
                                entry["_s2_retry_artifacts_active"] = True
                    elif (not pair_alignment_base and not console_base
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
                    new_r["validation_history"] = list(r.get("validation_history") or [])
                    # 量測基準必須跟著 render 走——final[idx] 換成 new_r 之後,
                    # 下一圈才比得出「這次有沒有比上次好」。
                    new_r["_retry_metrics_prev"] = r.get("_retry_metrics_prev")
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
                            new_v = {"ok": None, "error": "missing base or render path after retry",
                                     "exception_type": "MissingRenderPath"}
                        _record_validation_attempt(
                            new_r, job_id=job_id, stage="z3", attempt=current_rc + 1,
                            validation=new_v)
                    except Exception as ve:
                        new_v = {"ok": None, "error": f"revalidate failed: {str(ve)[:500]}",
                                 "exception_type": type(ve).__name__}
                        _record_validation_attempt(
                            new_r, job_id=job_id, stage="z3", attempt=current_rc + 1,
                            validation=new_v, error=ve)
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
                    # EDD4856E：Z3 修 pair 時曾把已通過的門距改壞；新版若新增
                    # 任何硬傷，不得覆蓋較佳舊版，也不得成為下一輪底圖。
                    regression_reason = _z3_candidate_regression_reason(
                        v, new_r.get("validation"))
                    if regression_reason:
                        r["validation_history"] = list(new_r.get("validation_history") or [])
                        r["retry_count"] = current_rc + 1
                        r["retry_reason"] = (
                            f"{retry_reason} | candidate rejected: {regression_reason}")
                        print(f"[pipeline] Z3 候選新增硬傷 → 保留較佳版本 render[{idx}] "
                              f"— {regression_reason}")
                        break
                    if repair_mode == "console_door":
                        monotonic, monotonic_reason = _console_repair_candidate_is_monotonic(
                            v, new_r.get("validation"))
                        if not monotonic:
                            r["validation_history"] = list(new_r.get("validation_history") or [])
                            r["retry_count"] = current_rc + 1
                            r["_console_repair_exhausted"] = True
                            r["retry_reason"] = (
                                f"{retry_reason} | candidate rejected: {monotonic_reason}")
                            print(f"[pipeline] Z3 console candidate 拒收並停止後續付費補生 "
                                  f"render[{idx}] — {monotonic_reason}")
                            break
                        if (_door_block_offender(new_r.get("validation")) == "focal_anchor"
                                and current_rc + 1 >= MAX_RETRY):
                            new_r["_console_repair_exhausted"] = True
                            new_r["retry_reason"] = (
                                f"{retry_reason} | console repair budget exhausted")
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
        if use_image_edit_retry:
            for idx in range(len(final)):
                r = final[idx]
                if r.get("_s2_preflight_blocked"):
                    print(f"[pipeline] render[{idx}] S2 前檢已封鎖 → 跳過 Phase2")
                    continue
                if r.get("_console_repair_exhausted"):
                    print(f"[pipeline] render[{idx}] 電視櫃離門修復已停止 → 跳過 Phase2（不再燒 fal/Gemini）")
                    continue
                v = r.get("validation") or {}
                if _skip_unmodelable_extra_repair(r):
                    print(f"[pipeline] render[{idx}] S2不合格視角+門硬傷 → 跳過 Phase2 補生（省 fal，走重拍）")
                    continue
                if not v.get("hard_fail"):
                    continue
                if v.get("validation_outage"):
                    print("[pipeline] Gemini 額度斷線（429）——跳過 Phase2 補生，不燒 fal")
                    continue
                if r.get("_allow_single_shot_without_guide"):
                    print(f"[pipeline] render[{idx}] 無引導單張模式 → 跳過 Phase2 補生")
                    continue
                if idx >= len(expanded):
                    continue
                entry = expanded[idx]
                retry_ctx = _build_retry_ctx_from_validation(v)
                print(f"[pipeline] Phase2 硬傷補生 render[{idx}] style={r.get('style')} "
                      f"— {(v.get('reason') or '')[:120]}")
                write_status(job_id, job_dir, "rendering", 93, "為未通過的風格再生成一次…")
                failed_stage = "phase2_hardfix_generate_renders"
                phase2_retry_seq = int(r.get("retry_count") or 0) + 1
                _clear_s2_retry_edit_artifacts(entry)
                base_for_gen = entry["_base_path"]
                pair_alignment_base = _activate_pair_alignment_edit(
                    v, r, entry, str(job_dir), idx)
                console_base = None
                alignment_base = None
                if pair_alignment_base:
                    retry_ctx = dict(retry_ctx or {})
                    retry_ctx["tv_alignment_edit"] = True
                    base_for_gen = pair_alignment_base
                else:
                    console_base = _activate_console_door_edit(
                        v, r, entry, str(job_dir), idx, str(phase2_retry_seq))
                    if console_base:
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["console_door_clearance_edit"] = True
                        if isinstance(v.get("focal_door_axis_conflict"), dict):
                            retry_ctx["console_axis_alignment_edit"] = True
                        base_for_gen = console_base
                    elif _door_block_offender(v) == "focal_anchor":
                        r["_console_repair_exhausted"] = True
                        r["retry_reason"] = "console repair skipped: no pair-safe target"
                        print(f"[pipeline] Phase2 render[{idx}] 電視櫃避門無安全目標 → 生成前停止")
                        continue
                    else:
                        alignment_base = _sofa_alignment_edit_base(
                            v, r, entry.get("_room_type", "living"))
                if alignment_base:
                    retry_ctx = dict(retry_ctx or {})
                    retry_ctx["sofa_alignment_edit"] = True
                    if v.get("sofa_on_wrong_side") is True:
                        retry_ctx["sofa_cross_room_relocate"] = True
                    base_for_gen = alignment_base
                    if entry.get("_layout_contract_s2_required") is True:
                        repair_guide = _build_s2_sofa_repair_guide(
                            alignment_base,
                            entry.get("_layout_contract_s2") or "",
                            str(Path(job_dir) / f"guide_s2_sofa_repair_{idx:02d}_{phase2_retry_seq}.jpg"),
                            validation=v,
                            compact_entry_mode=entry.get("_s2_compact_entry_mode") is True,
                        )
                        if repair_guide:
                            entry["_consistency_ref_path"] = repair_guide
                        edit_mask = _build_s2_sofa_edit_mask(
                            alignment_base,
                            entry.get("_layout_contract_s2") or "",
                            v,
                            str(Path(job_dir) / f"mask_s2_sofa_repair_{idx:02d}_{phase2_retry_seq}.png"),
                            compact_entry_mode=entry.get("_s2_compact_entry_mode") is True,
                        )
                        if edit_mask:
                            entry["_edit_mask_path"] = edit_mask
                            entry["_edit_mask_mode"] = "sofa"
                        if repair_guide or edit_mask:
                            entry["_s2_retry_artifacts_active"] = True
                elif not pair_alignment_base and not console_base:
                    # 40063497：只有商品保真失敗、幾何已過 → 用當前這張成品做局部商品修，
                    # 別退回原始底圖全新重生（那會把 Z3 已修好的門距丟掉、沙發又貼門）。
                    product_edit_base = _product_only_edit_base(
                        v, r, entry.get("_room_type", "living"))
                    if product_edit_base:
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["product_fidelity_edit"] = True
                        base_for_gen = product_edit_base
                    elif ((entry.get("_room_type") or "living") == "living"
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
                new_r["validation_history"] = list(r.get("validation_history") or [])
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
                        new_v = {"ok": None, "error": "missing path after hardfix",
                                 "exception_type": "MissingRenderPath"}
                    _record_validation_attempt(
                        new_r, job_id=job_id, stage="phase2", attempt=1,
                        validation=new_v)
                except Exception as ve:
                    new_v = {"ok": None, "error": f"revalidate hardfix failed: {str(ve)[:500]}",
                             "exception_type": type(ve).__name__}
                    _record_validation_attempt(
                        new_r, job_id=job_id, stage="phase2", attempt=1,
                        validation=new_v, error=ve)
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
                # 必須看 deterministic fail-closed 後的終判；raw Gemini 可能漏掉
                # 7B39FD17 的極端 pair delta，不能把其誤當成功候選。
                if not (new_r.get("validation") or {}).get("hard_fail"):
                    final[idx] = new_r
                    print(f"[pipeline] Phase2 補生成功 render[{idx}] style={new_r.get('style')}")
                else:
                    r["validation_history"] = list(new_r.get("validation_history") or [])
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
            # 生成後被擋的圖：fal 已收費、圖也在，過去直接丟掉連看都看不到。
            # 上傳並記 URL，讓營運方能眼球判「這張到底該不該擋」——用戶裁決是
            # 校準庫的唯一標準，看不到圖就無從裁決。付費前擋的單沒有 render_path，
            # 自然 blocked_render_url=None（本來就沒生圖）。
            _blocked_path = r.get("render_path") or ""
            _blocked_url = None
            if _blocked_path and Path(_blocked_path).exists():
                try:
                    _blocked_url = sb_upload_render(job_id, Path(_blocked_path))
                except Exception as _be:
                    print(f"[pipeline] 落選圖上傳失敗（略過）: {str(_be)[:80]}")
            dropped_validation_reasons.append({
                "style":       r.get("style"),
                "style_label": r.get("style_label"),
                "angle_label": r.get("angle_label"),     # 哪個房間/視角失敗
                "room_type":   r.get("room_type"),
                "timeout":     bool(_is_timeout),         # 前端可顯示友善「生成逾時」文案
                "reason":      str(reason)[:240],
                "layout_mode": r.get("_layout_mode") or "legacy",
                "blocked_render_url": _blocked_url,       # 未通過品檢的版本（可眼球校準）
                # 2D212624 盲區：落選單沒存裁切資訊，事後查不出「門到底有沒有
                # 被推出鏡頭」——只能靠肉眼看落選圖猜。退回 legacy 這條路的成敗
                # 完全繫於門有沒有出鏡，這三個欄位讓它變成可查的事實。
                "cropped":       bool(r.get("cropped")),
                "crop_note":     r.get("crop_note"),
                "door_excluded": bool(r.get("door_excluded")),
                **_validation_diagnostics(r),
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
        # S2 硬擋時同圖重抽 zoning 的觀測紀錄（付 Fal 前；純診斷）
        if s2_zoning_resample_log:
            result_json_payload["s2_zoning_resample"] = {
                "max_attempts": _s2_zoning_resample_max(),
                "views": s2_zoning_resample_log,
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
                "focal_anchor_misaligned_with_sofa", "focal_door_axis_conflict",
                "sofa_facing_entrance_door", "furniture_blocks_door",
                "furniture_blocks_walkway") if kk in v}

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
            "validation_summary": _slim_validation_summary(validation_summary),
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
            "validation_summary": _slim_validation_summary(validation_summary),
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
                    if r.get("_s2_preflight_blocked"):
                        print(f"[pipeline] render[{idx}] S2 前檢已封鎖 → 跳過 Phase3")
                        continue
                    if r.get("_console_repair_exhausted"):
                        print(f"[pipeline] render[{idx}] 電視櫃離門修復已停止 → 跳過 Phase3（不再燒 fal/Gemini）")
                        continue
                    if not _is_hard_fail(r) or idx >= len(expanded):
                        continue
                    entry = expanded[idx]
                    _clear_s2_retry_edit_artifacts(entry)
                    v0 = r.get("validation") or {}
                    if _skip_unmodelable_extra_repair(r):
                        print(f"[pipeline] render[{idx}] S2不合格視角+門硬傷 → 跳過 Phase3 補生（省 fal，走重拍）")
                        continue
                    if v0.get("validation_outage"):
                        print("[pipeline] Gemini 額度斷線（429）——跳過 Phase3 補生，不燒 fal")
                        continue
                    if r.get("_allow_single_shot_without_guide"):
                        print(f"[pipeline] render[{idx}] 無引導單張模式 → 跳過 Phase3 補生")
                        continue
                    retry_ctx = _build_retry_ctx_from_validation(v0)
                    # AI-auto guide 綁在同一底圖，Phase3 不得換圖或改用未裁切原圖。
                    pair_alignment_base = _activate_pair_alignment_edit(
                        v0, r, entry, str(job_dir), idx)
                    console_base = None
                    alignment_base = None
                    if pair_alignment_base:
                        retry_ctx = dict(retry_ctx or {})
                        retry_ctx["tv_alignment_edit"] = True
                        strategies = [("TV中心軸局部校正", pair_alignment_base, None)]
                    else:
                        console_base = _activate_console_door_edit(
                            v0, r, entry, str(job_dir), idx, "p3")
                        if console_base:
                            retry_ctx = dict(retry_ctx or {})
                            retry_ctx["console_door_clearance_edit"] = True
                            if isinstance(v0.get("focal_door_axis_conflict"), dict):
                                retry_ctx["console_axis_alignment_edit"] = True
                            strategies = [("電視櫃離門遮罩硬修", console_base, None)]
                        elif _door_block_offender(v0) == "focal_anchor":
                            r["_console_repair_exhausted"] = True
                            r["retry_reason"] = "console repair skipped: no pair-safe target"
                            print(f"[pipeline] Phase3 render[{idx}] 電視櫃避門無安全目標 → 生成前停止")
                            continue
                        else:
                            alignment_base = _sofa_alignment_edit_base(
                                v0, r, entry.get("_room_type", "living"))
                            product_edit_base = (
                                None if alignment_base else
                                _product_only_edit_base(v0, r, entry.get("_room_type", "living")))
                            if alignment_base:
                                retry_ctx = dict(retry_ctx or {})
                                retry_ctx["sofa_alignment_edit"] = True
                                if v0.get("sofa_on_wrong_side") is True:
                                    retry_ctx["sofa_cross_room_relocate"] = True
                                strategies = [("沙發跨房搬移" if v0.get("sofa_on_wrong_side")
                                               else "沙發局部位移", alignment_base, None)]
                            elif product_edit_base:
                                # 40063497：只有商品失敗、幾何已過 → 局部商品修保住門距，
                                # 不走 _phase3_base_strategies 換底圖重生。
                                retry_ctx = dict(retry_ctx or {})
                                retry_ctx["product_fidelity_edit"] = True
                                strategies = [("商品局部修（保幾何）", product_edit_base, None)]
                            else:
                                strategies = _phase3_base_strategies(entry)
                    fixed = None
                    for _p3_attempt, (tag, base_p, model_ov) in enumerate(strategies, start=1):
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
                            _missing_v = {"ok": None, "error": "missing render path after phase3",
                                          "exception_type": "MissingRenderPath"}
                            _record_validation_attempt(
                                r, job_id=job_id, stage="phase3", attempt=_p3_attempt,
                                validation=_missing_v)
                            continue
                        cand["validation_history"] = list(r.get("validation_history") or [])
                        try:
                            from gemini_analyze import validate_render
                            _lc = layout_ctx if (entry.get("_room_type") or "living") == "living" else None
                            _lc = _product_fidelity_into_layout_ctx(_lc, entry)
                            validation_base = (
                                entry["_base_path"]
                                if (pair_alignment_base or console_base or alignment_base)
                                else base_p
                            )
                            v3 = validate_render(validation_base, rpath, entry["_angle_label"],
                                                 layout_context=_lc,
                                                 room_type=entry.get("_room_type", "living"),
                                                 design_mode=design_mode)
                            _record_validation_attempt(
                                cand, job_id=job_id, stage="phase3", attempt=_p3_attempt,
                                validation=v3)
                            v3 = _fail_closed_validation(
                                v3, entry.get("_room_type", "living"))
                        except Exception as v_e:
                            _error_v = {"ok": None, "error": str(v_e)[:500],
                                        "exception_type": type(v_e).__name__}
                            _record_validation_attempt(
                                cand, job_id=job_id, stage="phase3", attempt=_p3_attempt,
                                validation=_error_v, error=v_e)
                            r["validation_history"] = list(cand.get("validation_history") or [])
                            print(f"[pipeline] Phase3 驗證例外（{tag}）: {str(v_e)[:120]}")
                            continue
                        r["validation_history"] = list(cand.get("validation_history") or [])
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
                            "validation_history": fixed.get("validation_history", []),
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
                    _post_vs = _post_rj.get("validation_summary") or {}
                    for _dropped in (_post_vs.get("dropped_renders") or []):
                        _match = find_dropped_render_match(_dropped, final)
                        if _match is not None:
                            merge_dropped_render_diagnostics(_dropped, _match)
                    _post_rj["validation_summary"] = _post_vs
                    _post_rj.pop("repairing", None)
                    _post_rj["repair_incomplete"] = True
                    sb_upsert({
                        "job_id": job_id,
                        "status": "incomplete",
                        "progress": 100,
                        "message": _incomplete_message(_post_vs),
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
    local_source_keys: list[str] = []
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
            local_source_keys.append(canonical_photo_key(url))

    if not local_photos:
        return JSONResponse(status_code=500, content={"error": "failed to download any photo from supabase"})

    # 3. Gemini zoning v2（2026-07-08 起：有影片就抽關鍵幀一起送——影片是拿來
    #    理解結構/方向/動線的，分區判讀是客戶第一眼看到的「AI 理解你家」，
    #    不能只看照片。全程 best-effort：任何失敗都退回純照片，不擋分區頁。）
    try:
        from zoning_v2 import compute_zoning_v2, draw_overlay
    except ImportError as e:
        return JSONResponse(status_code=500, content={"error": f"zoning module missing: {e}"})

    # 付款前分區一律純照片——不碰影片。影片的價值在付款後的完整全室分析
    # （動線/房間連接/口述需求），分區 overlay 用不到它（幾何綁單張、分區框畫在
    # 最佳照片上）。過去這裡抽 4 幀送 Gemini，等於對「還沒付款、可能不會付」的
    # 人先燒影片 token。影片理解全部留到付款後 run_pipeline。
    zoning_kf: list = []

    zoning = compute_zoning_v2(local_photos, video_keyframes=zoning_kf or None)
    if zoning.get("error"):
        return JSONResponse(status_code=500, content={"error": f"gemini zoning failed: {zoning['error']}"})

    # 4. 畫 overlay。S2 座標只屬於 Gemini 選定的 best_photo_index；
    # 使用者 living metadata 只在模型 index 無效時 fallback，不能覆蓋座標來源。
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
    _model_best_idx = zoning.get("best_photo_index")
    if isinstance(_model_best_idx, int) and not isinstance(_model_best_idx, bool) \
            and 0 <= _model_best_idx < len(local_photos):
        best_idx = _model_best_idx
        print(f"[/api/zoning] 分區圖採用 Gemini S2 best photo idx={best_idx}")
    else:
        best_idx = prefer_idx if prefer_idx is not None else 0
        print(f"[/api/zoning] 模型 best index 無效，fallback photo idx={best_idx}")
    best_photo = local_photos[best_idx]
    best_source_key = local_source_keys[best_idx] if best_idx < len(local_source_keys) else ""
    if not best_source_key:
        return JSONResponse(status_code=500, content={"error": "zoning source key missing"})
    zoning["_source_binding"] = {
        "photo_key": best_source_key,
        "sha256": _source_file_sha256(best_photo),
    }
    existing_path = tmp_dir / "z_overlay_existing.jpg"
    proposed_path = tmp_dir / "z_overlay_proposed.jpg"
    # 門→對面牆的禁區帶：讀規劃器那**同一份**幾何（entrance_hard_no_go_polygon），
    # 不在這裡另外拉一個灰色 bbox。客戶在確認頁看到的，就是生成端真正遵守的那塊。
    # 算不出來（門開在進深端／標記退化）就回 None，照舊只畫 zones，不畫假的給客戶看。
    try:
        from zoning_v2 import entrance_no_go_polygon_for_overlay
        _no_go_norm = entrance_no_go_polygon_for_overlay(
            best_photo, zoning.get("struct_geometry_v1"))
    except Exception as _e:
        print(f"[/api/zoning] 門前禁區略過: {type(_e).__name__}: {str(_e)[:90]}")
        _no_go_norm = None
    try:
        draw_overlay(best_photo, zoning.get("existing_zones", {}),
                     "EXISTING ZONES (AI inferred original use)", existing_path)
        draw_overlay(best_photo, zoning.get("proposed_zones", {}),
                     "PROPOSED ZONES (AI suggested layout)", proposed_path,
                     entrance_no_go_norm=_no_go_norm)
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
                    # 分區圖已改 JPEG（PNG 每張 1.9MB、JPEG q88 約 0.2MB）。
                    # 副檔名決定 Content-Type，不要寫死 image/png——寫死會讓
                    # JPEG bytes 被當成 PNG 送，瀏覽器不一定吃。
                    "Content-Type":  ("image/png" if local.suffix.lower() == ".png"
                                      else "image/jpeg"),
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

    overlay_existing_url = _upload_overlay(existing_path, "zoning_overlay_existing.jpg")
    overlay_proposed_url = _upload_overlay(proposed_path, "zoning_overlay_proposed.jpg")

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

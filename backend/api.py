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

SUPABASE_URL = "https://cjezgczjjsxfoeifduaj.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNqZXpnY3pqanN4Zm9laWZkdWFqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzk0NjE3NDYsImV4cCI6MjA5NTAzNzc0Nn0.K8zAdT5U3ApWCe4T-noBY5mrseCUSi2-A6Sn8JLU5X4"
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
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_ENDPOINT          = os.environ.get("R2_ENDPOINT", "").strip()
R2_BUCKET            = os.environ.get("R2_BUCKET", "deco168-uploads").strip()

def _r2_client():
    """惰性建立 R2 boto3 client（避免 import 時就要求金鑰）"""
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )

def r2_presign_put(key: str, content_type: str = "video/mp4", expires_in: int = 3600) -> str | None:
    """產生 PUT presigned URL，讓前端直接上傳影片到 R2"""
    if not (R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT):
        return None
    try:
        return _r2_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
    except Exception as e:
        print(f"[r2_presign_put] 失敗: {e}")
        return None

def r2_download_object(key: str, dest: Path) -> str | None:
    """從 R2 下載物件到本機"""
    if not (R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT):
        return None
    try:
        _r2_client().download_file(R2_BUCKET, key, str(dest))
        return str(dest)
    except Exception as e:
        print(f"[r2_download] {key} 失敗: {e}")
        return None

def r2_delete_object(key: str) -> bool:
    """從 R2 刪除物件（pipeline 跑完用）"""
    if not (R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT):
        return False
    try:
        _r2_client().delete_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception as e:
        print(f"[r2_delete] {key} 失敗: {e}")
        return False

app = FastAPI(title="DECO168 API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/jobs", StaticFiles(directory=str(JOBS_DIR), html=False), name="jobs")


# ─── Supabase helpers ─────────────────────────────────────────────────────────

def sb_upsert(data: dict):
    try:
        _req.post(f"{SUPABASE_URL}/rest/v1/orders", json=data,
                  headers=_SB_HEADERS, timeout=8)
    except Exception:
        pass

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
            "Content-Type":  "image/jpeg",
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


def run_pipeline(job_id: str, photo_paths: list, styles: list, plan: str,
                 space_type: str = "living", render_angle: str = "single"):
    job_dir = JOBS_DIR / job_id
    os.chdir(str(BASE_DIR))

    try:
        sys.path.insert(0, str(BASE_DIR))
        from test_full_pipeline import analyze_image, generate_renders
        from furniture_match import enrich_renders

        # 先把 r2:// 或 supabase:// 影片從雲端下載到本機 job_dir
        # r2_keys_to_delete: pipeline 跑完後要清掉的 R2 物件
        r2_keys_to_delete: list[str] = []
        resolved_paths: list[str] = []
        for p in photo_paths:
            if p.startswith("r2://"):
                key = p[len("r2://"):]
                fname = key.split("/")[-1] or f"video_{uuid.uuid4().hex[:6]}.mp4"
                dest = job_dir / fname
                write_status(job_id, job_dir, "downloading", 8, "從雲端下載影片…")
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
                write_status(job_id, job_dir, "downloading", 8, "從雲端下載影片…")
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

        if gemini_uris or video_paths:
            from gemini_analyze import analyze_space
            if gemini_uris:
                write_status(job_id, job_dir, "analyzing", 15, "Gemini Pro 分析影片+照片（理解整體格局）…")
                analysis = analyze_space(gemini_uris[0], user_styles=styles or None,
                                         is_uri=True, extra_photos=image_paths or None,
                                         space_type=space_type)
            else:
                write_status(job_id, job_dir, "analyzing", 10, "上傳影片到 Gemini（大檔案需要幾分鐘）…")
                analysis = analyze_space(video_paths[0], user_styles=styles or None,
                                         extra_photos=image_paths or None,
                                         space_type=space_type)
        else:
            write_status(job_id, job_dir, "analyzing", 15, "Gemini AI 分析空間照片…")
            extra = image_paths[1:] if len(image_paths) > 1 else None
            analysis = analyze_image(image_paths[0], styles or None, extra_photos=extra)

        # ── 決定 Flux 輸入角度 ──
        # 單一全景 → 1 張最美角度；多角度 → 多張（照片 + 影片抽幀）
        base_video = video_paths[0] if video_paths else None
        flux_bases: list[str] = []  # 每張對應「該渲染哪個角度」
        angle_labels: list[str] = []  # 角度的中文標籤

        if render_angle == "multi":
            # 全室 → 3 個區域；單一房間 → 3 個角度
            target_count = 3
            # 用戶照片優先
            for i, p in enumerate(image_paths[:target_count]):
                flux_bases.append(p)
                angle_labels.append(f"角度{i+1}")
            # 不夠就從影片均勻抽幀補
            if len(flux_bases) < target_count and base_video:
                positions = [0.2, 0.5, 0.8]
                for j in range(target_count - len(flux_bases)):
                    frame_path = str(job_dir / f"frame_{len(flux_bases):02d}.jpg")
                    extract_frame(base_video, frame_path, position=positions[j])
                    if Path(frame_path).exists():
                        flux_bases.append(frame_path)
                        angle_labels.append(f"角度{len(flux_bases)}")
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

        print(f"[pipeline] 渲染基底 {len(flux_bases)} 張：{[Path(p).name for p in flux_bases]}")

        write_status(job_id, job_dir, "matching", 45, "配對風格家具中…")
        enriched = enrich_renders(analysis.get("renders", []), analysis=analysis)

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
                     f"AI 生成 {total} 張渲染圖（{len(enriched)} 風格 × {len(flux_bases)} 角度）…")

        # 一次渲染一張：對應 base 不同
        final = []
        for idx, entry in enumerate(expanded):
            single_result = generate_renders(entry["_base_path"], [entry], output_dir=str(job_dir))
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
            })

        sb_upsert({"job_id": job_id, "status": "completed", "progress": 100,
                   "message": "設計方案生成完畢！",
                   "result_json": {"analysis": analysis, "renders": slim_renders}})

        # 跑完自動清掉 R2 上的影片（隱私 + 省空間）
        for key in r2_keys_to_delete:
            ok = r2_delete_object(key)
            print(f"[pipeline] R2 清除 {key}: {'OK' if ok else 'FAIL'}")

    except Exception as e:
        err_txt = traceback.format_exc()
        write_status(job_id, job_dir, "failed", 0, f"處理失敗，請聯絡客服")
        sb_upsert({"job_id": job_id, "status": "failed", "message": f"處理失敗，請聯絡客服",
                   "result_json": {"error": str(e), "traceback": err_txt[-2000:]}})
        with open(job_dir / "error.log", "w", encoding="utf-8") as f:
            f.write(err_txt)


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
async def upload_photos(
    upload_id:  str               = Form(...),
    photos:     List[UploadFile]  = File(default=[]),
    video_keys: str               = Form(default="[]"),
):
    """
    上傳註冊端點：
    - 影片：前端用 presigned PUT 直接上傳到 R2，這裡只收 R2 物件 key
    - 照片：本機保留 + Supabase Storage 備份
    """
    upload_dir = UPLOADS_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    local_paths: list[str] = []
    photo_urls:  list[str] = []

    # 影片：前端已直接傳 R2，把 key 轉成 r2://<obj> 的虛擬路徑
    try:
        keys = json.loads(video_keys or "[]")
        if not isinstance(keys, list):
            keys = []
    except Exception:
        keys = []
    for k in keys:
        if isinstance(k, str) and k.strip():
            local_paths.append(f"r2://{k.strip()}")

    # 照片：本機 + Supabase Storage
    for i, photo in enumerate(photos):
        ext  = Path(photo.filename or "photo.jpg").suffix.lower() or ".jpg"
        dest = upload_dir / f"photo_{i:02d}{ext}"
        data = await photo.read()
        with open(dest, "wb") as f:
            f.write(data)
        local_paths.append(str(dest))
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        url = sb_upload_file(upload_id, dest.name, data, mime)
        if url:
            photo_urls.append(url)

    with open(upload_dir / "paths.json", "w", encoding="utf-8") as f:
        json.dump(local_paths, f)
    sb_save_upload(upload_id, photo_urls, "", keys)

    return {"upload_id": upload_id, "count": len(local_paths)}


@app.post("/api/job")
async def create_job(
    background_tasks: BackgroundTasks,
    upload_id: str    = Form(...),
    styles: str       = Form(default=""),
    plan: str         = Form(default="A"),
    space_type: str   = Form(default="living"),    # living/dining/bedroom/study/whole
    render_angle: str = Form(default="single"),    # single/multi
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
    with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"job_id": job_id, "plan": plan, "styles": styles_list,
                   "space_type": space_type, "render_angle": render_angle,
                   "photo_count": len(new_paths)}, f, ensure_ascii=False)

    sb_upsert({"job_id": job_id, "plan": plan, "styles": styles_list,
               "photo_count": len(new_paths), "status": "queued",
               "progress": 5, "message": "已排入隊列，即將開始分析…"})

    write_status(job_id, job_dir, "queued", 5, "已排入隊列，即將開始分析…")
    background_tasks.add_task(run_pipeline, job_id, new_paths, styles_list, plan,
                              space_type, render_angle)

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


@app.get("/health")
def health():
    keys = [k for k in os.environ if "GEMINI" in k or "GOOGLE" in k or "FAL" in k or "AI" in k or "R2" in k]
    return {
        "status": "ok",
        "base_dir": str(BASE_DIR),
        "gemini_key": "set" if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")) else "MISSING",
        "fal_key":    "set" if os.environ.get("FAL_KEY") else "MISSING",
        "r2_access_key": "set" if R2_ACCESS_KEY_ID else "MISSING",
        "r2_secret":     "set" if R2_SECRET_ACCESS_KEY else "MISSING",
        "r2_endpoint":   R2_ENDPOINT or "MISSING",
        "r2_bucket":     R2_BUCKET or "MISSING",
        "matching_keys": keys,
        "all_keys": sorted(os.environ.keys()),
    }

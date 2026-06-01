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

def sb_save_upload(upload_id: str, photo_urls: list, video_uri: str):
    """把上傳紀錄存到 Supabase uploads table"""
    try:
        _req.post(
            f"{SUPABASE_URL}/rest/v1/uploads",
            json={"upload_id": upload_id, "photo_urls": photo_urls, "video_uri": video_uri},
            headers=_SB_HEADERS, timeout=8
        )
    except Exception:
        pass

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

def extract_frame(video_path: str, out_path: str) -> str:
    """從影片中段抽一幀作為 Flux 輸入，回傳儲存路徑"""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 3))
        ok, frame = cap.read()
        cap.release()
        if ok:
            cv2.imwrite(out_path, frame)
            return out_path
    except Exception:
        pass
    # fallback: 用 ffmpeg
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "3", "-i", video_path,
         "-vframes", "1", "-q:v", "2", out_path],
        capture_output=True
    )
    return out_path


def run_pipeline(job_id: str, photo_paths: list, styles: list, plan: str):
    job_dir = JOBS_DIR / job_id
    os.chdir(str(BASE_DIR))

    try:
        sys.path.insert(0, str(BASE_DIR))
        from test_full_pipeline import analyze_image, generate_renders
        from furniture_match import enrich_renders

        # 分類影片與照片（含 gemini:// URI）
        gemini_uris = [p[len("gemini://"):] for p in photo_paths if p.startswith("gemini://")]
        video_paths = [p for p in photo_paths if not p.startswith("gemini://") and Path(p).suffix.lower() in VIDEO_EXTS]
        image_paths = [p for p in photo_paths if not p.startswith("gemini://") and Path(p).suffix.lower() not in VIDEO_EXTS]

        if gemini_uris or video_paths:
            # ── 影片模式：Gemini 分析影片，理解完整空間 ──
            from gemini_analyze import analyze_space
            write_status(job_id, job_dir, "analyzing", 15, "Gemini AI 正在分析空間影片（理解整體格局）…")
            # 優先用已上傳的 Gemini URI，否則用本機路徑
            if gemini_uris:
                analysis = analyze_space(gemini_uris[0], user_styles=styles or None, is_uri=True)
            else:
                analysis = analyze_space(video_paths[0], user_styles=styles or None)

            # Flux 渲染基底：優先用用戶上傳的照片，否則從影片抽幀
            if image_paths:
                main_photo = image_paths[0]
            else:
                frame_path = str(job_dir / "frame_for_flux.jpg")
                main_photo = extract_frame(video_paths[0], frame_path)
        else:
            # ── 照片模式（原有邏輯）──
            write_status(job_id, job_dir, "analyzing", 15, "Gemini AI 正在分析空間照片…")
            main_photo = image_paths[0]
            extra = image_paths[1:] if len(image_paths) > 1 else None
            analysis = analyze_image(main_photo, styles or None, extra_photos=extra)

        write_status(job_id, job_dir, "matching", 45, "配對風格家具中…")
        enriched = enrich_renders(analysis.get("renders", []), analysis=analysis)

        write_status(job_id, job_dir, "rendering", 60, "AI 渲染圖生成中（約 5-10 分鐘）…")
        final = generate_renders(main_photo, enriched, output_dir=str(job_dir))

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
                "render_filename":   render_path.name if render_path else None,
                "render_url":        render_url,
                "render_error":      r.get("error"),
                "matched_furniture": r.get("matched_furniture", [])[:3],
            })

        sb_upsert({"job_id": job_id, "status": "completed", "progress": 100,
                   "message": "設計方案生成完畢！",
                   "result_json": {"analysis": analysis, "renders": slim_renders}})

    except Exception as e:
        err_txt = traceback.format_exc()
        write_status(job_id, job_dir, "failed", 0, f"處理失敗，請聯絡客服")
        sb_upsert({"job_id": job_id, "status": "failed", "message": f"處理失敗，請聯絡客服",
                   "result_json": {"error": str(e), "traceback": err_txt[-2000:]}})
        with open(job_dir / "error.log", "w", encoding="utf-8") as f:
            f.write(err_txt)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_photos(
    photos: List[UploadFile] = File(default=[]),
    videos: List[UploadFile] = File(default=[]),
):
    """照片存 Supabase Storage；影片送 Gemini Files API；元資料存 DB"""
    upload_id  = uuid.uuid4().hex[:8].upper()
    upload_dir = UPLOADS_DIR / upload_id
    upload_dir.mkdir(parents=True)

    local_paths: list[str] = []
    photo_urls:  list[str] = []
    video_uri:   str       = ""

    # 影片 — 先存本機，再非同步上傳 Gemini Files（由 pipeline 使用 URI）
    for i, video in enumerate(videos):
        ext  = Path(video.filename or "video.mp4").suffix.lower() or ".mp4"
        dest = upload_dir / f"video_{i:02d}{ext}"
        data = await video.read()
        with open(dest, "wb") as f:
            f.write(data)
        local_paths.append(str(dest))
        # 上傳到 Gemini Files API，存 URI（48小時有效）
        try:
            gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY") or "").strip()
            if gemini_key:
                from google import genai as _genai
                _gc = _genai.Client(api_key=gemini_key)
                gfile = _gc.files.upload(file=str(dest))
                import time as _time
                for _ in range(30):
                    if gfile.state.name != "PROCESSING":
                        break
                    _time.sleep(3)
                    gfile = _gc.files.get(name=gfile.name)
                if gfile.state.name == "ACTIVE":
                    video_uri = gfile.uri
        except Exception:
            pass  # fallback: pipeline 用本機路徑

    # 照片 — 存本機 + 上傳 Supabase Storage
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

    # 儲存到本機 paths.json + Supabase uploads table
    with open(upload_dir / "paths.json", "w", encoding="utf-8") as f:
        json.dump(local_paths, f)
    sb_save_upload(upload_id, photo_urls, video_uri)

    return {"upload_id": upload_id, "count": len(local_paths)}


@app.post("/api/job")
async def create_job(
    background_tasks: BackgroundTasks,
    upload_id: str = Form(...),
    styles: str   = Form(default=""),
    plan: str     = Form(default="A"),
):
    """建立 AI Job，在背景執行完整 pipeline"""
    paths_file = UPLOADS_DIR / upload_id / "paths.json"
    upload_dir = UPLOADS_DIR / upload_id

    # 本機找不到時，從 Supabase 恢復
    if not paths_file.exists():
        record = sb_get_upload(upload_id)
        if not record:
            return JSONResponse(status_code=404, content={"error": "upload_id not found，請重新上傳"})
        # 重建本機目錄
        upload_dir.mkdir(parents=True, exist_ok=True)
        recovered: list[str] = []
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
        # 影片：若有 Gemini URI 則加入 paths（pipeline 會識別）
        if record.get("video_uri"):
            recovered.insert(0, f"gemini://{record['video_uri']}")
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
        if path.startswith("gemini://"):
            new_paths.append(path)  # Gemini URI 直接保留
            continue
        src = Path(path)
        if src.exists():
            dst = job_dir / src.name
            shutil.copy2(src, dst)
            new_paths.append(str(dst))

    styles_list = [s.strip() for s in styles.split(",") if s.strip()]
    with open(job_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"job_id": job_id, "plan": plan, "styles": styles_list,
                   "photo_count": len(new_paths)}, f, ensure_ascii=False)

    # 寫入 Supabase（初始記錄）
    sb_upsert({"job_id": job_id, "plan": plan, "styles": styles_list,
               "photo_count": len(new_paths), "status": "queued",
               "progress": 5, "message": "已排入隊列，即將開始分析…"})

    write_status(job_id, job_dir, "queued", 5, "已排入隊列，即將開始分析…")
    background_tasks.add_task(run_pipeline, job_id, new_paths, styles_list, plan)

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
    keys = [k for k in os.environ if "GEMINI" in k or "GOOGLE" in k or "FAL" in k or "AI" in k]
    return {
        "status": "ok",
        "base_dir": str(BASE_DIR),
        "gemini_key": "set" if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")) else "MISSING",
        "fal_key":    "set" if os.environ.get("FAL_KEY") else "MISSING",
        "matching_keys": keys,
        "all_keys": sorted(os.environ.keys()),
    }

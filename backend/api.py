# DECO168 FastAPI Backend
# 啟動: cd backend && python3.11 -m uvicorn api:app --reload --port 8000
import os, sys, json, uuid, shutil, traceback
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


def run_pipeline(job_id: str, photo_paths: list, styles: list, plan: str):
    job_dir = JOBS_DIR / job_id
    os.chdir(str(BASE_DIR))

    try:
        sys.path.insert(0, str(BASE_DIR))
        from test_full_pipeline import analyze_image, generate_renders
        from furniture_match import enrich_renders

        write_status(job_id, job_dir, "analyzing", 15, "Gemini AI 正在分析空間照片…")
        main_photo = photo_paths[0]
        extra = photo_paths[1:] if len(photo_paths) > 1 else None
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
            render_path = Path(r.get("render_path", ""))
            render_url = None
            if render_path.exists():
                render_url = sb_upload_render(job_id, render_path)
            slim_renders.append({
                "style":             r.get("style"),
                "style_label":       r.get("style_label"),
                "render_filename":   render_path.name if render_path.name else None,
                "render_url":        render_url,  # Supabase 公開 URL
                "matched_furniture": r.get("matched_furniture", [])[:3],
            })

        sb_upsert({"job_id": job_id, "status": "completed", "progress": 100,
                   "message": "設計方案生成完畢！",
                   "result_json": {"analysis": analysis, "renders": slim_renders}})

    except Exception as e:
        err_txt = traceback.format_exc()
        write_status(job_id, job_dir, "failed", 0, f"處理失敗：{e}")
        with open(job_dir / "error.log", "w", encoding="utf-8") as f:
            f.write(err_txt)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_photos(photos: List[UploadFile] = File(...)):
    """暫存照片；返回 upload_id 供 /api/job 使用"""
    upload_id  = uuid.uuid4().hex[:8].upper()
    upload_dir = UPLOADS_DIR / upload_id
    upload_dir.mkdir(parents=True)

    paths: list[str] = []
    for i, photo in enumerate(photos):
        ext  = Path(photo.filename or "photo.jpg").suffix.lower() or ".jpg"
        dest = upload_dir / f"photo_{i:02d}{ext}"
        with open(dest, "wb") as f:
            f.write(await photo.read())
        paths.append(str(dest))

    with open(upload_dir / "paths.json", "w", encoding="utf-8") as f:
        json.dump(paths, f)

    return {"upload_id": upload_id, "count": len(paths)}


@app.post("/api/job")
async def create_job(
    background_tasks: BackgroundTasks,
    upload_id: str = Form(...),
    styles: str   = Form(default=""),
    plan: str     = Form(default="A"),
):
    """建立 AI Job，在背景執行完整 pipeline"""
    paths_file = UPLOADS_DIR / upload_id / "paths.json"
    if not paths_file.exists():
        return JSONResponse(status_code=404, content={"error": "upload_id not found"})

    with open(paths_file, encoding="utf-8") as f:
        photo_paths: list[str] = json.load(f)

    if not photo_paths:
        return JSONResponse(status_code=400, content={"error": "no photos found"})

    job_id  = uuid.uuid4().hex[:8].upper()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)

    new_paths: list[str] = []
    for path in photo_paths:
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

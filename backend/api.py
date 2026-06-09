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
# C2.7 C2: run_pipeline + pipeline 緊密綁定 helper 已搬到 pipeline_runner.py
# api.py 透過 import 取用; route handler 行為 0 變化.
from db_helpers import (
    SUPABASE_URL, SUPABASE_KEY, _SB_HEADERS,
    sb_upsert, sb_get,
    sb_upload_file, sb_save_upload,
    sb_download_object, sb_get_upload, sb_upload_render,
    r2_presign_put, r2_download_object, r2_delete_object,
    write_status,
)
from pipeline_runner import (
    run_pipeline,
    AnchoredValidationFailed,
    BASE_DIR, UPLOADS_DIR, JOBS_DIR,
)
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

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


def z3_needs_retry(validation: dict | None) -> tuple[bool, str]:
    """
    Z3: 判斷一張 render 是否需要重試。
    觸發條件（任一）：
      - validation.ok is False AND 有結構類 flag (walls/recessed/windows changed)
      - reason 含「開口被封 / 走廊消失 / 牆面改變 / 填平 / 封閉 / 通道」等關鍵字
    回傳 (should_retry, reason_text)
    """
    if not isinstance(validation, dict):
        return False, ""
    if validation.get("ok") is not False:
        return False, ""

    bad_flags = []
    for k in ("walls_changed", "recessed_space_added", "windows_changed"):
        if validation.get(k):
            bad_flags.append(k)

    reason = (validation.get("reason") or "").strip()
    bad_kw = ["開口被封", "走廊消失", "牆面改變", "填平", "封閉", "通道", "封住", "被封", "封死"]
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
                 space_type: str = "living", render_angle: str = "single",
                 design_mode: str = "furnish",
                 user_zoning_v2: dict | None = None,
                 user_layout_choice: str = ""):
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
                write_status(job_id, job_dir, "analyzing", 15, "分析影片+照片（理解整體格局）…")
                analysis = analyze_space(gemini_uris[0], user_styles=styles or None,
                                         is_uri=True, extra_photos=image_paths or None,
                                         space_type=space_type)
            else:
                write_status(job_id, job_dir, "analyzing", 10, "影片上傳分析中（大檔案需要幾分鐘）…")
                analysis = analyze_space(video_paths[0], user_styles=styles or None,
                                         extra_photos=image_paths or None,
                                         space_type=space_type)
        else:
            write_status(job_id, job_dir, "analyzing", 15, "分析空間照片中…")
            extra = image_paths[1:] if len(image_paths) > 1 else None
            analysis = analyze_image(image_paths[0], styles or None, extra_photos=extra)

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
            write_status(job_id, job_dir, "zoning", 40, "理解空間動線中…")
            if zoning_photos:
                try:
                    from zoning import compute_zoning
                    zoning_result = compute_zoning(zoning_photos)
                except Exception as ze:
                    print(f"[pipeline] zoning 例外（不阻斷）: {ze}")
                    zoning_result = {"error": str(ze)[:300], "confidence": "none"}
        print(f"[pipeline] zoning confidence={zoning_result.get('confidence')} "
              f"error={zoning_result.get('error', '(none)')[:80]}")

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
                     f"生成 {total} 張風格渲染圖（{len(enriched)} 風格 × {len(flux_bases)} 角度）…")

        # 一次渲染一張：對應 base 不同（analysis + design_mode 傳進去）
        final = []
        for idx, entry in enumerate(expanded):
            single_result = generate_renders(entry["_base_path"], [entry],
                                             output_dir=str(job_dir),
                                             analysis=analysis, design_mode=design_mode,
                                             zoning=zoning_result)
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
        write_status(job_id, job_dir, "validating", 85, "驗證渲染結構保留度…")
        try:
            from gemini_analyze import validate_render
            for r in final:
                bpath = r.get("_base_path") or ""
                rpath = r.get("render_path") or ""
                if bpath and rpath and Path(bpath).exists() and Path(rpath).exists():
                    try:
                        v = validate_render(bpath, rpath, r.get("_angle_label", ""))
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
        if use_nano:
            for idx in range(len(final)):
                r = final[idx]
                if r.get("retry_count"):  # 已重試過不再重試
                    continue
                v = r.get("validation") or {}
                should_retry, retry_reason = z3_needs_retry(v)
                if not should_retry:
                    continue
                if idx >= len(expanded):
                    continue
                entry = expanded[idx]
                print(f"[pipeline] Z3 retry render[{idx}] style={r.get('style')} — {retry_reason}")
                write_status(job_id, job_dir, "rendering", 92, "修正結構問題的設計圖中…")
                try:
                    retry_results = generate_renders(
                        entry["_base_path"], [entry],
                        output_dir=str(job_dir),
                        analysis=analysis, design_mode=design_mode,
                        zoning=zoning_result,
                    )
                except Exception as re_e:
                    print(f"[pipeline] Z3 retry 例外: {re_e}")
                    r["retry_count"] = 1
                    r["retry_reason"] = f"retry exception: {str(re_e)[:200]}"
                    continue
                if not retry_results:
                    r["retry_count"] = 1
                    r["retry_reason"] = f"{retry_reason} | retry returned empty"
                    continue
                new_r = retry_results[0]
                # 改名加 _retry
                if new_r.get("render_path"):
                    src_p = Path(new_r["render_path"])
                    new_name = f"render_{entry.get('style','x')}_{idx:02d}_retry{src_p.suffix}"
                    new_p = src_p.parent / new_name
                    try:
                        src_p.rename(new_p)
                        new_r["render_path"] = str(new_p)
                    except Exception:
                        pass
                # 重新 validate
                try:
                    from gemini_analyze import validate_render
                    bpath = entry["_base_path"]
                    rpath = new_r.get("render_path") or ""
                    if rpath and Path(bpath).exists() and Path(rpath).exists():
                        new_v = validate_render(bpath, rpath, entry["_angle_label"])
                    else:
                        new_v = {"ok": None, "error": "missing base or render path after retry"}
                except Exception as ve:
                    new_v = {"ok": None, "error": f"revalidate failed: {str(ve)[:200]}"}
                new_r["validation"]   = new_v
                new_r["angle_label"]  = entry["_angle_label"]
                new_r["retry_count"]  = 1
                new_r["retry_reason"] = retry_reason
                final[idx] = new_r
                retry_n += 1
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

        sb_upsert({"job_id": job_id, "status": "completed", "progress": 100,
                   "message": "設計方案生成完畢！",
                   "result_json": {
                       "analysis":           analysis,
                       "zoning":             zoning_result,
                       "zoning_v2":          user_zoning_v2,         # Z2: 保留原始 v2（未轉換）
                       "layout_choice":      user_layout_choice or None,
                       "renders":            slim_renders,
                       "validation_summary": validation_summary,
                   }})

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
                   "design_mode": design_mode,
                   "photo_count": len(new_paths)}, f, ensure_ascii=False)

    sb_upsert({"job_id": job_id, "plan": plan, "styles": styles_list,
               "photo_count": len(new_paths), "status": "queued",
               "progress": 5, "message": "已排入隊列，即將開始分析…"})

    # Z2: parse 使用者已確認的 v2 zoning（可選）
    user_zoning_v2 = None
    if zoning_json:
        try:
            parsed = json.loads(zoning_json)
            if isinstance(parsed, dict):
                user_zoning_v2 = parsed
        except Exception as je:
            print(f"[/api/job] zoning_json parse 失敗, 忽略: {je}")

    write_status(job_id, job_dir, "queued", 5, "已排入隊列，即將開始分析…")
    background_tasks.add_task(run_pipeline, job_id, new_paths, styles_list, plan,
                              space_type, render_angle, design_mode,
                              user_zoning_v2, layout_choice)

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

    # 5. 上傳 overlay 到 Supabase Storage（uploads bucket，回 public URL）
    def _upload_overlay(local: Path, name: str) -> str | None:
        try:
            data = local.read_bytes()
            storage_path = f"{upload_id}/{name}"
            r = _req.post(
                f"{SUPABASE_URL}/storage/v1/object/uploads/{storage_path}",
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
                return f"{SUPABASE_URL}/storage/v1/object/public/uploads/{storage_path}"
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
    return {
        "status": "ok",
        "base_dir": str(BASE_DIR),
        "gemini_key": "set" if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")) else "MISSING",
        "fal_key":    "set" if os.environ.get("FAL_KEY") else "MISSING",
        "r2_access_key": "set" if ak else "MISSING",
        "r2_secret":     "set" if sk else "MISSING",
        "r2_endpoint":   "set" if ep else "MISSING",
        "r2_bucket":     bucket or "MISSING",  # bucket 名稱本來就在 R2 可見，不算 secret
    }

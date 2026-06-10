"""
worker.py — DECO168 anchored job worker (C2.7 commit 6)

Phase 1 範圍:
  - 只 claim execution_mode=anchored 訂單 (legacy 仍由 web BackgroundTasks 處理)
  - 共用既有 pipeline_runner.run_pipeline (匯入, 不搬家)
  - 透過 db_queue fenced RPC 寫 DB; 收 ClaimRevoked 即乾淨退出

主迴圈:
  1. db_queue.claim_job 取一筆
  2. validate queue_payload (缺欄 → fenced failed, error_type=InvalidQueuePayload)
  3. 下載 photo_paths (object key 安全檢查, 拒絕 path traversal / http)
  4. 啟動 heartbeat thread (daemon, 30s/cycle); heartbeat 失敗 set claim_lost event
  5. PipelineWriter(worker_id=WID, claim_token=token) → run_pipeline
  6. 結束: stop heartbeat thread; 不主動釋放 lease (避免 grace period 雙重執行)
  7. SIGTERM/SIGINT: drain mode, 停 claim 新工作; 已 in-flight 自然完成或 lease 過期被 reclaim

不 import api.py.
"""
import os
import sys
import re
import time
import uuid
import socket
import signal
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_helpers import sb_download_object, r2_download_object
from pipeline_runner import (
    run_pipeline, PipelineWriter, ClaimRevoked, JOBS_DIR,
)
import db_queue


WORKER_ID = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
EXECUTION_MODE      = os.environ.get("WORKER_EXECUTION_MODE",    "anchored")
LEASE_SECONDS       = int(os.environ.get("WORKER_LEASE_SECONDS",     "900"))
HEARTBEAT_INTERVAL  = int(os.environ.get("WORKER_HEARTBEAT_SECONDS",  "30"))
CLAIM_POLL_INTERVAL = int(os.environ.get("WORKER_CLAIM_POLL_SECONDS", "5"))

_shutdown = threading.Event()


# ─── Queue payload validation ──────────────────────────────────────
class InvalidQueuePayload(Exception):
    pass


class QueuePayloadDownloadError(Exception):
    pass


def validate_queue_payload(payload) -> list[str]:
    """回 missing 欄位 list (空 = OK)."""
    if not isinstance(payload, dict):
        return ["queue_payload_not_dict"]
    missing = []
    if not payload.get("upload_id"):
        missing.append("upload_id")
    pp = payload.get("photo_paths")
    if not isinstance(pp, list) or len(pp) == 0:
        missing.append("photo_paths")
    sty = payload.get("styles")
    if not isinstance(sty, list) or len(sty) == 0:
        missing.append("styles")
    return missing


# 只接受這兩種 scheme 的 object key
_ALLOWED_PATH_PREFIXES = ("supabase://", "r2://")


def _is_safe_object_key(path: str) -> bool:
    """拒絕 path traversal / http URL / 絕對路徑 / 未知 scheme."""
    if not isinstance(path, str) or not path.strip():
        return False
    s = path.strip()
    # 必須是 supabase:// 或 r2://
    if not any(s.startswith(p) for p in _ALLOWED_PATH_PREFIXES):
        return False
    # 不允許 path traversal
    if ".." in s:
        return False
    # 不允許 backslash 怪招
    if "\\" in s:
        return False
    return True


def _safe_filename(name: str) -> str:
    """sanitize: 只留 alnum / dot / dash / underscore; 不允許前綴 dot; 限長."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if safe.startswith("."):
        safe = "_" + safe[1:]
    if not safe:
        safe = "file"
    return safe[:200]


def download_photo_paths_to_input(job_id: str, photo_paths: list[str]) -> list[str]:
    """
    下載 queue_payload.photo_paths 到 JOBS_DIR/<job_id>/input/<safe_name>.
    回本機路徑 list. 任一失敗 → raise QueuePayloadDownloadError.
    """
    input_dir = JOBS_DIR / job_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[str] = []
    for raw in photo_paths:
        if not _is_safe_object_key(raw):
            raise QueuePayloadDownloadError(
                f"unsafe object key rejected: {str(raw)[:60]}"
            )
        if raw.startswith("supabase://"):
            key = raw[len("supabase://"):]
            fname = _safe_filename(key.split("/")[-1] or "photo.jpg")
            dest = input_dir / fname
            local = sb_download_object(key, dest)
            if not local:
                raise QueuePayloadDownloadError(f"supabase download failed: {key[:40]}")
            local_paths.append(local)
        elif raw.startswith("r2://"):
            key = raw[len("r2://"):]
            fname = _safe_filename(key.split("/")[-1] or "video.mp4")
            dest = input_dir / fname
            local = r2_download_object(key, dest)
            if not local:
                raise QueuePayloadDownloadError(f"r2 download failed: {key[:40]}")
            local_paths.append(local)
    if not local_paths:
        raise QueuePayloadDownloadError("no photo_paths downloaded")
    return local_paths


# ─── Heartbeat thread ──────────────────────────────────────────────
def _heartbeat_loop(job_id: str, claim_token: str,
                    stop_evt: threading.Event,
                    claim_lost_evt: threading.Event) -> None:
    """daemon thread: 每 HEARTBEAT_INTERVAL 秒 RPC heartbeat. False → set claim_lost."""
    while not stop_evt.wait(HEARTBEAT_INTERVAL):
        if claim_lost_evt.is_set():
            return
        try:
            ok = db_queue.heartbeat(WORKER_ID, job_id, claim_token, LEASE_SECONDS)
            if not ok:
                # claim 已被回收 (lease 過期 / token 不符 / job 已 released)
                print(f"[worker] heartbeat lost job_id={job_id} worker_id={WORKER_ID}")
                claim_lost_evt.set()
                return
        except Exception as e:
            # 瞬時錯誤; 下個 cycle 再試. 不直接 set claim_lost.
            print(f"[worker] heartbeat exception job_id={job_id} "
                  f"error_type={type(e).__name__}")


# ─── 單一 job 生命週期 ─────────────────────────────────────────────
def run_one_job(job: dict) -> None:
    job_id = job["job_id"]
    claim_token = job["claim_token"]
    payload = job.get("queue_payload") or {}
    print(f"[worker] claimed job_id={job_id} attempt={job.get('attempt_count')} "
          f"is_reclaim={job.get('is_reclaim')}")

    # 1. Payload validation
    missing = validate_queue_payload(payload)
    if missing:
        print(f"[worker] invalid payload job_id={job_id} missing={missing}")
        try:
            db_queue.finalize(
                WORKER_ID, job_id, claim_token,
                "failed",
                "訂單資料不完整，請重新下單",
                {
                    "error":          "queue_payload validation failed",
                    "error_type":     "InvalidQueuePayload",
                    "missing_fields": missing,
                    "failed_stage":   "payload_validation",
                },
            )
        except Exception as e:
            print(f"[worker] invalid payload finalize 失敗: {type(e).__name__}")
        return

    # 2. Download photo_paths
    try:
        local_paths = download_photo_paths_to_input(job_id, payload["photo_paths"])
    except QueuePayloadDownloadError as e:
        print(f"[worker] download error job_id={job_id}: {e}")
        try:
            db_queue.finalize(
                WORKER_ID, job_id, claim_token,
                "failed",
                "讀取空間資料失敗，請重新嘗試",
                {
                    "error":        str(e)[:300],
                    "error_type":   "QueuePayloadDownloadError",
                    "failed_stage": "queue_payload_download",
                },
            )
        except Exception as fe:
            print(f"[worker] download-fail finalize 失敗: {type(fe).__name__}")
        return

    # 3. Heartbeat thread
    stop_hb     = threading.Event()
    claim_lost  = threading.Event()
    hb_thread   = threading.Thread(
        target=_heartbeat_loop,
        args=(job_id, claim_token, stop_hb, claim_lost),
        daemon=True,
    )
    hb_thread.start()

    # 4. run_pipeline with worker-mode writer
    writer = PipelineWriter(worker_id=WORKER_ID, claim_token=claim_token)
    try:
        run_pipeline(
            job_id=job_id,
            photo_paths=local_paths,
            styles=payload["styles"],
            plan=payload.get("plan", "A"),
            space_type=payload.get("space_type", "living"),
            render_angle=payload.get("render_angle", "single"),
            design_mode=payload.get("design_mode", "furnish"),
            user_zoning_v2=payload.get("user_zoning_v2"),
            user_layout_choice=payload.get("layout_choice", ""),
            budget_tier=payload.get("budget_tier", "tier3"),
            customer_notes=payload.get("customer_notes", ""),
            preferred_store=payload.get("preferred_store", "none"),
            upload_id=payload.get("upload_id", ""),
            writer=writer,
        )
    except ClaimRevoked as e:
        # run_pipeline 已經 log 過; 此處不重複處理
        print(f"[worker] claim revoked during run job_id={job_id}: {e}")
    except Exception as e:
        # 理論上 run_pipeline 內 outer except 應該已處理. 走到這裡是邊界 case.
        print(f"[worker] unexpected: job_id={job_id} type={type(e).__name__}: "
              f"{str(e)[:200]}")
    finally:
        # 停 heartbeat; 不主動釋放 lease
        stop_hb.set()
        hb_thread.join(timeout=5)


# ─── SIGTERM / SIGINT ──────────────────────────────────────────────
def _on_sigterm(signum, frame):
    print(f"[worker] SIGTERM received (signum={signum}); drain mode "
          f"(stop claim new jobs; in-flight 自然結束或 lease 過期)")
    _shutdown.set()


signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT,  _on_sigterm)


# ─── Main loop ─────────────────────────────────────────────────────
def main() -> None:
    print(f"[worker] starting worker_id={WORKER_ID} mode={EXECUTION_MODE} "
          f"lease={LEASE_SECONDS}s heartbeat={HEARTBEAT_INTERVAL}s "
          f"poll={CLAIM_POLL_INTERVAL}s")

    # Boot: 撈一次 max_attempts 已達的 zombie
    try:
        n = db_queue.fail_exhausted()
        if n > 0:
            print(f"[worker] boot: marked {n} exhausted jobs as failed")
    except Exception as e:
        print(f"[worker] boot fail_exhausted exception: {type(e).__name__}")

    while not _shutdown.is_set():
        try:
            job = db_queue.claim_job(WORKER_ID, EXECUTION_MODE, LEASE_SECONDS)
        except Exception as e:
            print(f"[worker] claim exception: {type(e).__name__}: {str(e)[:200]}")
            _shutdown.wait(CLAIM_POLL_INTERVAL)
            continue

        if job is None:
            # Idle: 順手清 zombie
            try:
                n = db_queue.fail_exhausted()
                if n > 0:
                    print(f"[worker] idle: marked {n} exhausted jobs as failed")
            except Exception:
                pass
            _shutdown.wait(CLAIM_POLL_INTERVAL)
            continue

        run_one_job(job)

    print(f"[worker] shutdown clean worker_id={WORKER_ID}")
    # 不主動釋放 lease — 讓 SIGTERM grace period 內可能完成;
    # 若 process 被殺 → daemon heartbeat thread 跟著死, lease 自然到期 →
    # 另一個 worker reclaim (attempt_count + 1)


if __name__ == "__main__":
    main()

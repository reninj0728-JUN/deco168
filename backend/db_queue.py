"""
db_queue.py — Supabase RPC wrappers for worker queue (C2.7 commit 6)

封裝 C2.7 commit 5 建立的 8 個 fenced RPC functions.
使用 SUPABASE_SERVICE_ROLE_KEY 呼叫 (anon key 一律 REVOKE).

設計原則:
  - 不直接 UPDATE orders, 全走 RPC
  - 缺 service role key → fail-fast (但延後到第一次使用而非 import time)
  - HTTP / network error 與 RPC false 明確分開
  - 所有呼叫有 timeout 上限
  - log 不洩漏 key
"""
import os
import requests as _req
from db_helpers import SUPABASE_URL


class RpcError(Exception):
    """Network / HTTP error or unparseable response from Supabase RPC."""
    pass


# RPC HTTP timeout (秒). claim 可能 SKIP LOCKED 略長, 其他較短.
_RPC_TIMEOUT_DEFAULT = 15
_RPC_TIMEOUT_CLAIM   = 20


def _get_service_role_key() -> str:
    """讀取 SUPABASE_SERVICE_ROLE_KEY; 缺則 raise (fail-fast on first use)."""
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY not set; cannot use worker queue. "
            "Worker mode requires service_role; anon key 不支援 worker_* RPCs."
        )
    return key


def _headers() -> dict:
    key = _get_service_role_key()
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


def _call_rpc(name: str, args: dict, timeout: int = _RPC_TIMEOUT_DEFAULT):
    """
    呼叫 public.<name>(args). 回傳解析後 JSON.
    Network / non-2xx → RpcError (不混淆 RPC 回 false / 空結果).
    log 不印 service key.
    """
    url = f"{SUPABASE_URL}/rest/v1/rpc/{name}"
    try:
        r = _req.post(url, json=args, headers=_headers(), timeout=timeout)
    except RuntimeError:
        # 來自 _get_service_role_key, 不包裝 raise
        raise
    except Exception as e:
        raise RpcError(f"network error calling RPC {name}: {type(e).__name__}") from e

    if r.status_code < 200 or r.status_code >= 300:
        # truncate body 避免任何意外資料外洩
        body = (r.text or "")[:300]
        raise RpcError(f"RPC {name} HTTP {r.status_code}: {body}")

    try:
        return r.json()
    except Exception as e:
        raise RpcError(f"RPC {name} bad JSON response: {type(e).__name__}") from e


# ═══════════════════════════════════════════════════════════════════
# Public wrappers (一一對應 commit 5 RPC)
# ═══════════════════════════════════════════════════════════════════

def claim_job(worker_id: str, execution_mode: str, lease_seconds: int = 900) -> dict | None:
    """
    Atomic claim: 拿一筆可 claim 的 job, 失敗回 None.

    成功時回 dict (worker_claim_job RETURN TABLE 第 1 列):
      {
        "job_id": "<text>",
        "claim_token": "<uuid>",
        "attempt_count": <int>,
        "queue_payload": <jsonb>,
        "prev_claimed_by": <text|null>,
        "prev_worker_state": <text|null>,
        "is_reclaim": <bool>,
      }
    """
    rows = _call_rpc(
        "worker_claim_job",
        {
            "p_worker_id":      worker_id,
            "p_execution_mode": execution_mode,
            "p_lease_seconds":  lease_seconds,
        },
        timeout=_RPC_TIMEOUT_CLAIM,
    )
    if isinstance(rows, list) and rows:
        return rows[0]
    return None


def heartbeat(worker_id: str, job_id: str, claim_token: str,
              lease_seconds: int = 900) -> bool:
    """Fenced heartbeat; 回 True 成功, False 表 claim 已被回收."""
    res = _call_rpc("worker_heartbeat", {
        "p_worker_id":     worker_id,
        "p_job_id":        job_id,
        "p_claim_token":   claim_token,
        "p_lease_seconds": lease_seconds,
    })
    return bool(res)


def update_progress(worker_id: str, job_id: str, claim_token: str,
                    status: str, progress: int, message: str) -> bool:
    """Fenced progress update; False 表 claim 已被回收."""
    res = _call_rpc("worker_update_progress", {
        "p_worker_id":   worker_id,
        "p_job_id":      job_id,
        "p_claim_token": claim_token,
        "p_status":      status,
        "p_progress":    progress,
        "p_message":     message,
    })
    return bool(res)


def merge_result(worker_id: str, job_id: str, claim_token: str,
                 result_json_merge: dict) -> bool:
    """Fenced result_json merge; False 表 claim 已被回收."""
    res = _call_rpc("worker_merge_result", {
        "p_worker_id":         worker_id,
        "p_job_id":            job_id,
        "p_claim_token":       claim_token,
        "p_result_json_merge": result_json_merge or {},
    })
    return bool(res)


def set_state(worker_id: str, job_id: str, claim_token: str,
              new_state: str) -> bool:
    """Fenced state transition; running/finalizing only."""
    res = _call_rpc("worker_set_state", {
        "p_worker_id":   worker_id,
        "p_job_id":      job_id,
        "p_claim_token": claim_token,
        "p_new_state":   new_state,
    })
    return bool(res)


def finalize(worker_id: str, job_id: str, claim_token: str,
             final_status: str, final_message: str,
             result_json_merge: dict) -> bool:
    """Fenced terminal write (completed/failed); False 表 claim 已被回收."""
    res = _call_rpc("worker_finalize", {
        "p_worker_id":         worker_id,
        "p_job_id":            job_id,
        "p_claim_token":       claim_token,
        "p_final_status":      final_status,
        "p_final_message":     final_message,
        "p_result_json_merge": result_json_merge or {},
    })
    return bool(res)


def fail_exhausted() -> int:
    """Sweep max_attempts 達上限的 stale jobs, 回 N 筆被標 failed."""
    res = _call_rpc("worker_fail_exhausted_attempts", {})
    if isinstance(res, int):
        return res
    if isinstance(res, list) and res:
        first = res[0]
        if isinstance(first, int):
            return first
        if isinstance(first, dict):
            # PostgREST 有時把純 integer 結果包成 [{"worker_fail_exhausted_attempts": 0}]
            for v in first.values():
                if isinstance(v, int):
                    return v
    return 0


# 本 commit 封裝, 但不主動呼叫 (pg_cron schedule 留到 commit 8).
def watchdog_mark_stale_jobs() -> list[dict]:
    res = _call_rpc("watchdog_mark_stale_jobs", {})
    return res if isinstance(res, list) else []

"""
Supabase Storage 保留期清理：刪掉 renders / uploads 兩個 bucket 裡
超過 RETENTION_DAYS（預設 14 天）的檔案。

用 storage list API 遞迴列出檔案（含 created_at），過期的分批刪除。
api.py 的自動清理走同一套邏輯（複製這裡的 walk + delete 實作）。

用法：railway run python3 _purge_old_storage.py [--dry-run]
"""
import os, sys, io, json
from datetime import datetime, timedelta, timezone

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SUPABASE_URL = "https://cjezgczjjsxfoeifduaj.supabase.co"
KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
HEADERS = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
RETENTION_DAYS = 14
BUCKETS = ["renders", "uploads"]
DRY_RUN = "--dry-run" in sys.argv


def list_prefix(bucket: str, prefix: str) -> list[dict]:
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
        json={"prefix": prefix, "limit": 1000,
              "sortBy": {"column": "name", "order": "asc"}},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def walk_old_files(bucket: str, prefix: str, cutoff: datetime, depth: int = 0) -> list[str]:
    """遞迴收集 created_at < cutoff 的檔案完整路徑（bucket 內相對路徑）。"""
    if depth > 4:
        return []
    old: list[str] = []
    for entry in list_prefix(bucket, prefix):
        name = entry.get("name")
        if not name:
            continue
        full = f"{prefix}{name}" if not prefix else f"{prefix.rstrip('/')}/{name}"
        if entry.get("id") is None:  # 資料夾
            old += walk_old_files(bucket, full + "/", cutoff, depth + 1)
            continue
        created = entry.get("created_at")
        if not created:
            continue
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            old.append(full)
    return old


def delete_batch(bucket: str, paths: list[str]) -> int:
    deleted = 0
    for i in range(0, len(paths), 100):
        chunk = paths[i:i + 100]
        r = requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{bucket}",
            json={"prefixes": chunk}, headers=HEADERS, timeout=60,
        )
        if r.ok:
            deleted += len(chunk)
        else:
            print(f"  刪除批次失敗 {r.status_code}: {r.text[:150]}")
    return deleted


def main():
    if not KEY:
        print("找不到 SUPABASE_SERVICE_KEY，中止")
        sys.exit(1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    print(f"保留期 {RETENTION_DAYS} 天，刪除 {cutoff.isoformat()} 之前的檔案"
          + ("（DRY RUN，不實際刪）" if DRY_RUN else ""))
    total = 0
    for bucket in BUCKETS:
        old = walk_old_files(bucket, "", cutoff)
        print(f"[{bucket}] 過期檔案 {len(old)} 個")
        if old and not DRY_RUN:
            n = delete_batch(bucket, old)
            print(f"[{bucket}] 已刪 {n} 個")
            total += n
    print(f"\n完成，共刪 {total} 個檔案")


if __name__ == "__main__":
    main()

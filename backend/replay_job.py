"""
正式重放工具（A2，B0CDF6A0 教訓）：重放舊單必須帶原單的 zoning 確認，
否則配置合約整層休眠，測出來的是「沒有合約的裸能力」，會誤判 production 退步。

用法（需 SUPABASE_SERVICE_KEY，建議 railway run）：
    python replay_job.py <job_id>              # 完整重放（帶原單 zoning，預設）
    python replay_job.py <job_id> --no-zoning  # 刻意測「無確認」路徑，
                                               # 報告必須標 NO_USER_ZONING，不得與 production 混比

腳本從 orders 讀回原單的 rooms / photo_meta / styles / palettes / zoning_v2，
原樣組回 POST /api/job。照片必須仍在保留期內（14 天）。
"""
import io
import json
import os
import sys

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

API = "https://deco168-production.up.railway.app"
SUPABASE_URL = "https://cjezgczjjsxfoeifduaj.supabase.co"
KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()


def fetch_order(job_id: str) -> dict:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/orders",
        params={"job_id": f"eq.{job_id}", "select": "job_id,plan,styles,result_json"},
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise SystemExit(f"找不到 job {job_id}")
    return rows[0]


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    job_id = sys.argv[1].strip().upper()
    with_zoning = "--no-zoning" not in sys.argv
    if not KEY:
        raise SystemExit("需要 SUPABASE_SERVICE_KEY（用 railway run 跑）")

    row = fetch_order(job_id)
    rj = row.get("result_json") or {}
    ci = rj.get("customer_inputs") or {}
    rooms = rj.get("rooms") or []
    pm_by_key = ci.get("photo_meta_by_key") or {}
    if not rooms:
        raise SystemExit(f"{job_id} 沒有 rooms 資料（非多空間單？）——請用網站重下")

    # upload_id 從 photo_key 取（uploads/<id>/<file>）
    first_key = (rooms[0].get("photo_keys") or [None])[0]
    if not first_key:
        raise SystemExit("原單沒有照片 key")
    upload_id = first_key.split("/")[1]

    # rooms_json：photo_meta 由 photo_meta_by_key 還原（後端要 per-room array）
    rooms_out = []
    for room in rooms:
        keys = room.get("photo_keys") or []
        metas = [pm_by_key[k] for k in keys if k in pm_by_key]
        rooms_out.append({
            "room_id":    room.get("room_id") or room.get("room_type"),
            "room_type":  room.get("room_type"),
            "room_label": room.get("room_label") or "",
            "is_primary": bool(room.get("is_primary")),
            "room_notes": room.get("room_notes") or "",
            "photo_keys": keys,
            "video_keys": room.get("video_keys") or [],
            "photo_meta": metas,
        })

    zoning_json = ""
    if with_zoning:
        zv2 = rj.get("zoning_v2")
        if isinstance(zv2, dict) and zv2:
            zoning_json = json.dumps(zv2, ensure_ascii=False)
        else:
            print("⚠ 原單沒有 zoning_v2 可帶——這次重放等同 NO_USER_ZONING，報告要標註")

    fd = {
        "upload_id":      upload_id,
        "styles":         ",".join(row.get("styles") or []),
        "plan":           row.get("plan") or "B",
        "space_type":     "whole" if len(rooms_out) >= 1 else "living",
        "render_angle":   "multi",
        "design_mode":    ci.get("design_mode") or "furnish",
        "layout_choice":  rj.get("layout_choice") or "A",
        "zoning_json":    zoning_json,
        "budget_tier":    ci.get("budget_tier") or "tier2",
        "customer_notes": ci.get("customer_notes") or "",
        "preferred_store": ci.get("preferred_store") or "none",
        "rooms_json":     json.dumps(rooms_out, ensure_ascii=False),
        "palettes_json":  json.dumps(ci.get("palettes") or {}, ensure_ascii=False),
    }
    tag = "WITH_ZONING" if (with_zoning and zoning_json) else "NO_USER_ZONING"
    print(f"重放 {job_id} → {tag}  styles={fd['styles']}  rooms={len(rooms_out)}")
    r = requests.post(f"{API}/api/job", data=fd, timeout=60)
    print("HTTP", r.status_code, r.text[:200])
    if r.ok:
        print(f"\n新 job：{r.json().get('job_id')}   （報告標註：{tag}）")


if __name__ == "__main__":
    main()

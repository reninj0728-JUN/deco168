# -*- coding: utf-8 -*-
"""交付趨勢一覽：79386c6（挖門）上線後，交付率有沒有從 11% 回來。

用法：railway run python backend/_trend.py          # 預設看最近 60 張
      railway run python backend/_trend.py 120      # 看更多

為什麼要這支：2026-08-01 查到 7 月客廳單以 7/19 為界斷成兩段——
07-02~07-16 走 legacy 交付 61%，07-19~07-31 走 S2 交付 11%，而主因是
「可用牆段蓋過大門」（79386c6 已修）。要判斷有沒有救回來，得看新單。

⚠️ 判讀前先看「不重複照片數」。同一張照片跑十次不是十個樣本——
那只是同一個房間多擲十次骰子，看不出系統有沒有變好。
"""
import io
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 60
DOOR_FIX_BUILD = "79386c6"

KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
if not KEY:
    raise SystemExit("需要 SUPABASE_SERVICE_KEY（用 railway run 跑）")

URL = ("https://cjezgczjjsxfoeifduaj.supabase.co/rest/v1/orders"
       f"?select=job_id,status,created_at,result_json&order=created_at.desc&limit={LIMIT}")
rows = json.loads(urllib.request.urlopen(urllib.request.Request(
    URL, headers={"apikey": KEY, "Authorization": "Bearer " + KEY}), timeout=240).read())


def stage_of(rj):
    """這張單走到哪一關才停。"""
    if rj.get("renders"):
        return "① 交付"
    if rj.get("error") or rj.get("failed_stage"):
        return "⑤ 分析/程式炸掉"
    dropped = (rj.get("validation_summary") or {}).get("dropped_renders") or []
    # ⚠️ 前檢擋掉也會留一筆落選紀錄，只是沒有圖。要看有沒有真的圖網址才分得出
    # 「付了 Fal 的錢才被擋」和「根本沒進 Fal」——這正是這支腳本要回答的問題。
    generated = any(
        (d or {}).get("blocked_render_url") or (d or {}).get("render_url")
        for d in dropped if isinstance(d, dict))
    if generated:
        return "③ 畫出來被閘門擋（已付 Fal）"
    if dropped or rj.get("living_incomplete"):
        return "② S2 前檢擋（沒進 Fal）"
    return "④ 其他沒交付"


rows_ok = []
for r in rows:
    rj = r.get("result_json")
    if isinstance(rj, str):
        try:
            rj = json.loads(rj)
        except Exception:
            rj = {}
    if not isinstance(rj, dict):
        rj = {}
    prov = ((rj.get("zoning_v2") or {}).get("_provenance") or {})
    media = prov.get("sent_media") or [{}]
    rows_ok.append({
        "job": r["job_id"], "status": r["status"], "at": r["created_at"][:10],
        "build": prov.get("code_revision") or "?",
        "photo": (media[0].get("sha256") or "?")[:8],
        "stage": stage_of(rj), "n": len(rj.get("renders") or []),
        "space": str((rj.get("analysis") or {}).get("space_type") or "?"),
    })

after = [x for x in rows_ok if x["build"].startswith(DOOR_FIX_BUILD)]
before = [x for x in rows_ok if not x["build"].startswith(DOOR_FIX_BUILD)]

print(f"最近 {len(rows_ok)} 張訂單\n")
for label, group in (("挖門修正之後（79386c6+）", after), ("之前", before)):
    if not group:
        print(f"=== {label} ===  （沒有資料）\n")
        continue
    delivered = sum(1 for x in group if x["stage"] == "① 交付")
    photos = {x["photo"] for x in group if x["photo"] != "?"}
    print(f"=== {label} ===  {len(group)} 張，交付 {delivered} "
          f"({delivered / len(group) * 100:.0f}%)")
    print(f"    不重複照片：{len(photos) or '未知（舊單沒有 provenance）'}"
          f"{'  ⚠️ 樣本是同一張照片重跑，看不出系統趨勢' if len(photos) == 1 and len(group) > 2 else ''}")
    for stage, count in sorted(Counter(x["stage"] for x in group).items()):
        print(f"    {stage:22} {count:3}  {count / len(group) * 100:4.0f}%")
    print()

by_photo = defaultdict(Counter)
for x in rows_ok:
    by_photo[x["photo"]][x["stage"]] += 1
if len(by_photo) > 1:
    print("=== 依照片 ===")
    for photo, counter in sorted(by_photo.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counter.values())
        print(f"  {photo:10} {total:3} 張  交付 {counter.get('① 交付', 0)}  "
              f"{dict(counter)}")

print("\n=== 最近 12 張 ===")
print(f"{'訂單':10}{'日期':12}{'build':10}{'照片':10}{'空間':8}{'走到哪'}")
for x in rows_ok[:12]:
    print(f"{x['job']:10}{x['at']:12}{x['build'][:8]:10}{x['photo']:10}"
          f"{x['space'][:6]:8}{x['stage']}")

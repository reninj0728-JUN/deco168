# -*- coding: utf-8 -*-
"""失敗單一鍵診斷｜用法：railway run python3 backend/_diag.py <訂單編號>

一次撈出：擋在哪一層、為什麼、有沒有生圖、落選圖 URL、S2 幾何原因。
以後訂單失敗，跑這個就看得到根因，不必逐單翻程式。
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
if not KEY:
    raise SystemExit("需要 SUPABASE_SERVICE_KEY（用 railway run 跑）")
JOB = (sys.argv[1] if len(sys.argv) > 1 else "").strip().upper()
if not JOB:
    raise SystemExit("用法：railway run python3 backend/_diag.py <訂單編號>")

url = ("https://cjezgczjjsxfoeifduaj.supabase.co/rest/v1/orders"
       f"?job_id=ilike.{JOB}*&select=job_id,status,message,created_at,result_json")
req = urllib.request.Request(url, headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
rows = json.loads(urllib.request.urlopen(req, timeout=30).read())
if not rows:
    raise SystemExit(f"找不到訂單 {JOB}")

r = rows[0]
rj = r.get("result_json")
if isinstance(rj, str):
    rj = json.loads(rj)
rj = rj if isinstance(rj, dict) else {}
vs = rj.get("validation_summary") or {}

print("=" * 64)
print(f"訂單 {r.get('job_id')}   {r.get('status')}   {r.get('created_at', '')[:16]}")
print(f"對客戶顯示：{r.get('message')}")
print("=" * 64)
print(f"生成 {vs.get('total')} 張 → 交付 {len(rj.get('renders') or [])} 張、"
      f"落選 {vs.get('dropped')} 張、重試 {vs.get('retry_count')} 次")
print()

for d in (vs.get("dropped_renders") or []):
    print(f"■ {d.get('angle_label')} / {d.get('style_label')}")
    print(f"   死因分類 : {d.get('failure_class')}   走的路 : {d.get('layout_mode')}")
    print(f"   擋在哪層 : {d.get('validation_stage')}   嘗試 : {d.get('validation_attempt_count')} 次")
    fin = d.get("validation_final") or {}
    print(f"   例外     : {fin.get('exception_type')} / {fin.get('exception_message')}")
    print(f"   原因全文 : {d.get('reason')}")
    bu = d.get("blocked_render_url")
    print(f"   落選圖   : {bu or '（付費前就擋掉，沒有生圖）'}")
    for h in (d.get("validation_history") or []):
        rv = h.get("raw_verdict") or {}
        print(f"     - {h.get('validation_stage')} #{h.get('attempt')} "
              f"class={h.get('failure_class')} exc={h.get('exception_type')} "
              f"reason={str(rv.get('reason') or '')[:60]}")
    print()

# S2 幾何層（付費前就擋時，原因在這裡）
sh = rj.get("layout_contract_shadow") or {}
for it in (sh.get("items") or []):
    print("── S2 幾何層 ──")
    print(f"   狀態 : {it.get('status')}   合格 : {it.get('pre_generation_eligible')}")
    print(f"   不合格碼 : {it.get('unsafe_codes')}")
    print(f"   判官驗了 : {it.get('verification_attempt_count')} 次  "
          f"結果 : {it.get('verification_status')}  "
          f"失敗欄位 : {it.get('verification_failed_fields')}")
    print(f"   說明 : {it.get('reason')}")

# 原始照片與分區（判斷是不是取景角度問題）
z = rj.get("zoning") or {}
print("\n── 空間判讀 ──")
print(f"   sofa={z.get('_sofa_layout')} 門側={z.get('_entrance_side')} 窗側={z.get('_window_side')}")
raw = (rj.get("zoning_v2") or {}).get("struct_geometry_v1") or {}
segs = raw.get("usable_wall_segments") or []
print(f"   可用牆段 : 左 {sum(1 for s in segs if s.get('side') == 'left')} 段、"
      f"右 {sum(1 for s in segs if s.get('side') == 'right')} 段"
      f"（S2 要左右各至少一段）")
if raw.get("uncertainty_notes"):
    print(f"   模型自述 : {raw.get('uncertainty_notes')}")
photo = (rj.get("rooms") or [{}])[0].get("photo_keys") or []
print(f"   原始照片 : {[p.split('/')[-1] for p in photo]}")

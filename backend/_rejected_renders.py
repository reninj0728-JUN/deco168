# -*- coding: utf-8 -*-
"""內部用：把「未通過品檢」的落選圖整理出來，給人眼與 AI 檢查。

客戶端已經不顯示落選圖了（result.html 的 blockedPreviewHTML 回空字串，
/api/job/{id}/result 的白名單也不回 blocked_render_url）。但圖本身沒有刪——
那是最有價值的優化素材：它記錄了「模型做錯了什麼、判官為什麼擋」。

這支就是那個出口。預設只列清單；--download 會把圖抓下來，依
  <out>/<job_id>/<房型>_<第幾次>_<失敗原因>.jpg
歸檔，方便一次看一批，也方便丟給模型批次判讀。

用法：
  python backend/_rejected_renders.py                 # 最近 30 張單，只列清單
  python backend/_rejected_renders.py --days 7
  python backend/_rejected_renders.py --job C1168B61
  python backend/_rejected_renders.py --download      # 連圖一起抓下來歸檔
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SB = "https://cjezgczjjsxfoeifduaj.supabase.co/rest/v1"
KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
OUT = Path(os.environ.get("REJECTED_OUT") or (Path(__file__).parent / "_rejected"))


def _arg(name: str, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _safe(s: str, n: int = 40) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(s or ""))[:n].strip("_") or "x"


def main() -> None:
    if not KEY:
        print("需要 SUPABASE_SERVICE_KEY（Railway Variables 裡那把）"); return
    job = _arg("--job")
    limit = int(_arg("--limit", "30"))
    q = (f"{SB}/orders?job_id=eq.{job}&select=job_id,created_at,result_json" if job else
         f"{SB}/orders?select=job_id,created_at,result_json&order=created_at.desc&limit={limit}")
    rows = json.loads(_get(q))

    total = 0
    for row in rows:
        rj = row.get("result_json")
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except Exception:
                continue
        if not isinstance(rj, dict):
            continue
        vs = rj.get("validation_summary") or {}
        dropped = [d for d in (vs.get("dropped_renders") or []) if isinstance(d, dict)]
        if not dropped:
            continue
        print(f"\n■ {row['job_id']}  {str(row.get('created_at'))[:10]}  "
              f"交付 {vs.get('delivered')}／落選 {len(dropped)}")
        for i, d in enumerate(dropped, 1):
            url = d.get("blocked_render_url") or ""
            why = d.get("reason") or d.get("failure_class") or ""
            ngs = d.get("ng_reasons") or vs.get("ng_reasons") or []
            print(f"   {i}. [{d.get('room_type')}/{d.get('angle_label')}] {str(why)[:50]}")
            if ngs:
                print(f"      判官：{'；'.join(str(x)[:60] for x in ngs[:3])}")
            if url:
                print(f"      {url[:100]}")
            total += 1
            if "--download" in sys.argv and url.startswith("http"):
                dest = OUT / row["job_id"]
                dest.mkdir(parents=True, exist_ok=True)
                fn = dest / f"{_safe(d.get('room_type'))}_{i}_{_safe(why, 28)}.jpg"
                try:
                    fn.write_bytes(urllib.request.urlopen(url, timeout=60).read())
                    print(f"      → 存到 {fn}")
                except Exception as e:
                    print(f"      ✗ 下載失敗 {type(e).__name__}")
    print(f"\n共 {total} 張落選圖"
          + (f"，已歸檔到 {OUT}" if "--download" in sys.argv else "（加 --download 抓圖）"))


if __name__ == "__main__":
    main()

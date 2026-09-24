# -*- coding: utf-8 -*-
"""新訂單不收影片（2026-09-24 產品決定）。

上傳頁已拿掉影片入口；後端 /api/upload 再擋一次，部署前就開著的舊上傳頁
送來的影片也不會進 pipeline。舊訂單不受影響——重建走 Supabase 裡的 video_keys。

真的打端點，不掃原始碼（`if False:` 這種破壞只有真請求抓得到）。

取代 test_upload_video_claims.py（鎖上傳頁影片文案，入口拿掉後一併移除）。
那支留下的證據，哪天要重開影片（ACCEPT_VIDEO_UPLOADS=1）文案只能照這個寫：
  A559DD2B（2026-08-08）是唯一有完整證據鏈的影片單——影片確實進了 Gemini，
  貢獻是格局完整度＋坪數估算（照片沒拍到的廚房、以流理台/門框當比例尺）；
  渲染沒有貢獻（`[zoning_v2] video_frames=0`，底圖/配對/判官全走照片）。
  所以只能講「格局」「坪數」，**不可以講「提升渲染品質」**，也不會多出圖。
"""
from __future__ import annotations

import json
from pathlib import Path

import api


def _post_upload(monkeypatch, tmp_path, video_keys):
    saved = {}
    monkeypatch.setattr(api, "UPLOADS_DIR", Path(tmp_path) / "uploads")
    monkeypatch.setattr(api, "sb_save_upload",
                        lambda uid, urls, uri="", vkeys=None: saved.update(vkeys=vkeys))
    from fastapi.testclient import TestClient
    r = TestClient(api.app).post("/api/upload", data={
        "upload_id": "PYTEST_VIDEO",
        "photo_keys": json.dumps(["uploads/PYTEST_VIDEO/p0.jpg"]),
        "video_keys": json.dumps(video_keys)})
    assert r.status_code == 200, r.text
    paths = json.loads((api.UPLOADS_DIR / "PYTEST_VIDEO" / "paths.json").read_text("utf-8"))
    return r.json(), paths, saved


def test_video_keys_from_a_stale_upload_page_are_dropped(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCEPT_VIDEO_UPLOADS", raising=False)
    body, paths, saved = _post_upload(monkeypatch, tmp_path, ["uploads/x/video_1.mp4"])
    assert body["videos"] == 0 and body["photos"] == 1
    assert not any(p.startswith("r2://") for p in paths), paths
    assert saved["vkeys"] == [], "影片 key 還是被寫進 uploads 紀錄"


def test_switch_can_reopen_video_intake(monkeypatch, tmp_path):
    """開關存在且真的有效——哪天要重新開放，不必改程式。"""
    monkeypatch.setenv("ACCEPT_VIDEO_UPLOADS", "1")
    body, paths, _ = _post_upload(monkeypatch, tmp_path, ["uploads/x/video_1.mp4"])
    assert body["videos"] == 1
    assert "r2://uploads/x/video_1.mp4" in paths


def test_upload_page_has_no_video_entry():
    html = (Path(__file__).resolve().parent.parent / "upload.html").read_text(encoding="utf-8")
    for s in ('accept="video/*"', "video_keys", "/api/r2/presign"):
        assert s not in html, f"上傳頁還有影片入口：{s}"

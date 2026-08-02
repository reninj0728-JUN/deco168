# -*- coding: utf-8 -*-
"""保留期：storage 與訂單內容都只留 30 天。

2026-08-03 查到的狀況：storage 有 14 天自動清理（`_purge_expired_storage`），
但 **orders 這張表從來沒被清過**——232 筆佔 450MB，是免費版上限 500MB 的 90%。
其中 7/16 前的 175 筆就佔 400MB（6 月 reference_map 存 base64 整張照片的
歷史包袱，6 月底已修；7 月起每筆只剩 14KB）。

分區確認頁的圖也一併從 PNG 改 JPEG：那只是給客戶看的確認圖、不提供下載，
PNG 每張 1.9MB（78 張佔 Storage 146MB、53%），JPEG q88 約 0.2MB。
成品渲染圖早就是 JPEG，這是漏網的最後一處。
"""
from __future__ import annotations

import inspect

import api
import cv2
import numpy as np
import pytest
import zoning_v2


# ── 分區圖格式 ─────────────────────────────────────────────────────────

def test_overlay_is_jpeg_not_png(tmp_path):
    """分區圖必須輸出 JPEG，而且要明顯比 PNG 小。"""
    photo = tmp_path / "room.jpg"
    # 用有雜訊的圖，避免純色讓 PNG 壓得異常小而測不出差異
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (900, 1200, 3), dtype=np.uint8)
    cv2.imwrite(str(photo), noise)

    zones = {"living_zone": {"bbox_on_best_photo": [300, 100, 900, 900]}}
    out = tmp_path / "overlay.jpg"
    zoning_v2.draw_overlay(photo, zones, "t", out)

    head = out.read_bytes()[:3]
    assert head == b"\xff\xd8\xff", f"不是 JPEG，開頭是 {head!r}"
    assert cv2.imread(str(out)) is not None, "JPEG 讀不回來"

    png = tmp_path / "overlay.png"
    ok, buf = cv2.imencode(".png", cv2.imread(str(photo)))
    assert ok
    png.write_bytes(buf.tobytes())
    assert out.stat().st_size < png.stat().st_size * 0.5, (
        f"JPEG {out.stat().st_size} 沒有明顯小於 PNG {png.stat().st_size}")


def test_overlay_upload_content_type_follows_suffix():
    """上傳的 Content-Type 不得寫死 image/png，否則 JPEG 會被當 PNG 送。"""
    src = inspect.getsource(api)
    assert '"Content-Type":  "image/png",\n' not in src, "還有寫死的 image/png"
    assert 'local.suffix.lower() == ".png"' in src, "Content-Type 沒有跟著副檔名走"


def test_overlay_filenames_are_jpg():
    """暫存檔與上傳檔名都要是 .jpg，不能留 .png 名字裝 JPEG 內容。"""
    src = inspect.getsource(api)
    for stale in ("z_overlay_existing.png", "z_overlay_proposed.png",
                  "zoning_overlay_existing.png", "zoning_overlay_proposed.png"):
        assert stale not in src, f"還在用 PNG 檔名: {stale}"
    for wanted in ("z_overlay_existing.jpg", "zoning_overlay_proposed.jpg"):
        assert wanted in src, f"缺 {wanted}"


# ── 訂單保留期 ─────────────────────────────────────────────────────────

def test_orders_purge_is_wired_to_startup():
    """訂單清理必須真的掛在 startup 上——storage 有清、DB 沒清就是現在這個問題。"""
    src = inspect.getsource(api._startup_watchdog)
    assert "_purge_expired_orders" in src, "訂單清理沒有掛上 startup"
    assert "_purge_expired_storage" in src, "storage 清理不見了"


def test_orders_purge_uses_same_retention_constant():
    """兩邊必須用同一個保留天數，不可各寫各的（兩套口徑是這專案的老毛病）。"""
    for fn in (api._purge_expired_orders, api._purge_expired_storage):
        assert "RETENTION_DAYS" in inspect.getsource(fn), f"{fn.__name__} 自己寫了另一個天數"
    assert api.RETENTION_DAYS == 30, "保留期應為 30 天"
    assert api.STORAGE_RETENTION_DAYS == api.RETENTION_DAYS, "兩個常數必須同值"


def test_orders_purge_clears_content_but_keeps_the_row():
    """只清空 result_json，保留該列——結果頁才能回「已過期」而不是壞頁。"""
    src = inspect.getsource(api._purge_expired_orders)
    assert '"result_json": None' in src, "沒有清空 result_json"
    assert "_req.patch" in src, "用了 delete 而不是 patch——整列刪掉結果頁會壞"
    assert "_req.delete" not in src, "不應該刪整列"


def test_orders_purge_only_touches_expired_rows(monkeypatch):
    """守門：清理只能打到 created_at 早於保留期、且還有內容的列。"""
    seen = {}

    class FakeResp:
        ok = True
        status_code = 200
        headers = {"content-range": "0-9/10"}
        text = ""

    def fake_patch(url, params=None, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["json"] = json
        return FakeResp()

    monkeypatch.setattr(api._req, "patch", fake_patch)
    api._purge_expired_orders()

    assert seen["url"].endswith("/rest/v1/orders")
    assert seen["params"]["created_at"].startswith("lt."), "沒有限定只清過期的"
    assert seen["params"]["result_json"] == "not.is.null", "會重複清已經清過的列"
    assert seen["json"]["result_json"] is None


def test_orders_purge_never_raises(monkeypatch):
    """清理失敗不得影響服務啟動。"""
    def boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(api._req, "patch", boom)
    api._purge_expired_orders()          # 不得拋出

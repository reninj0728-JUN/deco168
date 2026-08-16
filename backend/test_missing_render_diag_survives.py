# -*- coding: utf-8 -*-
"""MissingRenderPath 的判別欄位必須活著進到存檔紀錄。

線上實查（2026-08-12，D8FCE0EF／60F540A4／18D682A1）：7 筆落選全部只有
`exception_type = MissingRenderPath`，**`diag` 一個都沒有**。原因是
`_validation_diagnostics` 組 `validation_final` 時用固定 key 白名單，漏了 diag；
`_slim_validation_summary` 同樣漏。四個出口都老老實實寫了 `v["diag"]`，
但寫了沒人收，等於整套診斷在 production 是死的。

代價很具體：線上只看得到一個 `infrastructure` 標籤，分不出是
  · fal 根本沒回圖（render_path_set=False）
  · 圖沒落地（render_file_exists=False）
  · 底圖不見了（base_file_exists=False）
三種原因的修法完全不同，而 fal 沒錢那次就是因為分不出來，害人往格局方向
查了一整天（見 `_incomplete_message` 的註解）。

⚠️ 既有的 `test_dropped_reason_and_reshoot_copy.py` 只數「原始碼裡有 4 個出口」，
   數不到「欄位有沒有活著送到終點」——測到隔壁那個變數了。這裡補的是終點。
"""
from __future__ import annotations

import api

DIAG = {
    "render_path_set": False, "render_file_exists": False,
    "base_path_set": True, "base_file_exists": True,
    "render_error_present": True, "error_type": "FalGenerationTimeout",
}


def _render_with_diag():
    v = {"ok": None, "error": "missing base or render path",
         "exception_type": "MissingRenderPath", "diag": dict(DIAG)}
    return {"room_type": "living", "style": "muji", "validation": v,
            "validation_history": [
                {"validation_stage": "post_render", "attempt": 1,
                 "failure_class": "infrastructure", "validation": v}],
            "error_type": "MissingRenderPath"}


def test_diag_reaches_the_stored_record():
    """🔴 出口寫了 diag，終點就必須收得到——否則等於沒寫。"""
    out = api._validation_diagnostics(_render_with_diag())
    got = (out.get("validation_final") or {}).get("diag")
    assert got, "diag 在 _validation_diagnostics 就被白名單濾掉了"
    assert got == DIAG, f"diag 內容被改動：{got}"


def test_diag_survives_the_slim_payload_too():
    """精簡版正是最需要診斷的單在用的（重試多 ⇒ 完整版寫不進 Supabase）。"""
    full = {"total": 1, "ok": 0, "ng": 1,
            "dropped_renders": [{
                "room_type": "living", "failure_class": "infrastructure",
                **api._validation_diagnostics(_render_with_diag())}]}
    slim = api._slim_validation_summary(full)
    got = ((slim.get("dropped_renders") or [{}])[0]
           .get("validation_final") or {}).get("diag")
    assert got == DIAG, f"精簡版把 diag 濾掉了：{got}"


def test_three_failure_modes_stay_distinguishable():
    """三種故障必須靠這組欄位分得開——分不開就只剩一個沒用的 infrastructure。"""
    cases = {
        "fal 沒回圖": {"render_path": "", "base_path": __file__},
        "圖沒落地": {"render_path": "/nonexistent/render.jpg", "base_path": __file__},
        "底圖不見": {"render_path": __file__, "base_path": "/nonexistent/base.jpg"},
    }
    seen = {}
    for label, kw in cases.items():
        d = api._missing_render_diag(**kw)
        key = (d["render_path_set"], d["render_file_exists"], d["base_file_exists"])
        assert key not in seen, f"「{label}」與「{seen[key]}」的欄位組合一模一樣，分不出來"
        seen[key] = label


def test_diag_carries_no_free_text():
    """🔴 這是客戶可見 payload：只准布林＋類別名，不得夾路徑／token／原文。

    `_missing_render_diag` 的 docstring 說得很清楚——曾寫過遮蔽函式想濾路徑，
    6 種格式全漏。所以規則是結構化欄位，不是「濾乾淨的字串」。
    """
    d = api._missing_render_diag(
        render_path="C:/app/jobs/X/render.jpg",
        base_path="/workspace/base.jpg",
        error="Bearer TOPSECRET https://signed.url/x?token=abc",
        error_type="FalGenerationTimeout")
    for k, v in d.items():
        if k == "error_type":
            assert v == "FalGenerationTimeout"
            continue
        assert isinstance(v, bool), f"{k} 不是布林（{type(v).__name__}），可能夾帶原文"
    blob = repr(d)
    for leak in ("TOPSECRET", "https://", "C:/app", "/workspace"):
        assert leak not in blob, f"診斷欄位夾帶了 {leak}"

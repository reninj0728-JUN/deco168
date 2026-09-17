# -*- coding: utf-8 -*-
"""結果 API 只能回客戶頁用得到的欄位。

🔴 /api/job/{id}/result 沒有認證：知道 8 碼 job id 就拿得到整包。實測 C1168B61
   回 47,287 字元，裡面有伺服器絕對路徑 /app/jobs/…、14 個 sha256／fingerprint、
   flux_prompt（餵給模型的完整指令）、zoning 的 _provenance、廚房 bbox。
   結果頁一個都沒用到——分享連結＝整包工作底稿外流，而且 8 碼 hex 猜得到。

⚠️ 先前那次 payload_trimmed 是為了「DB 塞不下 8.6MB」的容量裁切，不是權限控制。
   同一個詞、不同問題，所以這裡一直是全裸的。

這支測試兩個方向都守：
  · 敏感欄位不准出去
  · 前端真的會讀的欄位不准被濾掉（白名單漏一個＝畫面少東西）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api

ROOT = Path(__file__).resolve().parent.parent
FULL = {
    "renders": [{
        "style": "nordic", "style_label": "北歐", "room_type": "living", "room_key": "living",
        "angle_label": "客廳", "render_url": "https://x/a.jpg", "render_filename": "a.jpg",
        "cropped": True, "door_excluded": False, "matched_furniture": [{"name_zh": "沙發"}],
        "soft_furnishing": [], "notes": "說明",
        # 以下都不該出去
        "flux_prompt": "warm oak, minimalist sofa…",
        "reference_map": [{"url": "data:image/jpeg;base64,AAAA"}],
        "validation": {"sofa_depth_percent_estimate": 71, "reason": "內部判官理由"},
        "_base_path": "/app/jobs/X/base.jpg", "render_model": "openai/gpt-image-2.5/sunburst/edit",
        "validation_history": [{"stage": "post_render"}], "pipeline_version": "v9",
    }],
    "analysis": {
        "design_analysis": "摘要", "layout_notes": "格局", "lighting": "採光", "space_type": "全室",
        "architectural_features": {"kitchen": "右牆一字型廚具"}, "room_dimensions": {"width_m": 3.8},
        "photo_classifications": [{"photo_index": 0}], "best_photo_index": 0,
    },
    "customer_inputs": {
        "design_mode": "furnish", "budget_tier": "tier2", "budget_label_zh": "10–20 萬",
        "preferred_store_label_zh": "不限", "customer_notes": "備註",
        "photo_meta_by_key": {"uploads/ABC/photo_01.jpg": {"target_note": "左邊靠窗是客廳"}},
    },
    "validation_summary": {"delivered": 4, "dropped": 0, "total": 4, "ng_reasons": ["x"],
                           "dropped_renders": []},
    "rooms": [{"room_id": "living_room", "room_type": "living_room", "is_primary": True,
               "photo_keys": ["uploads/ABC/photo_01.jpg"]}],
    "created_at": "2026-09-17T13:16:33+00:00",
    # 整包內部狀態
    "zoning": {"_origin": "user_confirmed_v2", "spatial_synthesis": {}},
    "zoning_v2": {"_provenance": {"request_fingerprint": "a" * 64}},
    "layout_contract_shadow": {"items": [{"contract_json": "/app/jobs/X/layout_contract_s2/c.json",
                                          "contract_hash": "b" * 64}]},
    "s2_zoning_resample": {}, "build_tag": "fullmode-rewrite-v2", "render_mode": "legacy",
    "layout_choice": "A",
}


@pytest.fixture(scope="module")
def out():
    return api._client_result_payload(FULL)


@pytest.mark.parametrize("key", [
    "zoning", "zoning_v2", "layout_contract_shadow", "s2_zoning_resample",
    "build_tag", "render_mode", "layout_choice",
])
def test_internal_top_level_blocks_are_gone(out, key):
    assert key not in out, f"內部欄位 {key} 還在回給客戶"


@pytest.mark.parametrize("needle,label", [
    ("/app/jobs/", "伺服器絕對路徑"),
    ("flux_prompt", "餵給模型的指令"),
    ("_provenance", "zoning provenance"),
    ("architectural_features", "廚具／門窗描述"),
    ("photo_meta_by_key", "上傳路徑"),
    ("validation_history", "判官歷程"),
    ("reference_map", "參考圖（含 base64）"),
    ("_base_path", "底圖路徑"),
    ("ng_reasons", "判官失敗原因"),
])
def test_no_internal_content_leaks(out, needle, label):
    blob = json.dumps(out, ensure_ascii=False)
    assert needle not in blob, f"{label} 外流：{needle}"


def test_no_64_hex_fingerprints(out):
    blob = json.dumps(out, ensure_ascii=False)
    assert not re.search(r"[a-f0-9]{64}", blob), "sha256／fingerprint 外流"


# ── 反方向：前端要用的不准被濾掉 ──────────────────────────────────
def test_every_field_the_page_reads_survives(out):
    """🔴 白名單漏一個＝畫面少東西。直接跟 result.html 實際的存取對帳。"""
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    top = set(re.findall(r"\bdata\.([A-Za-z_][A-Za-z0-9_]*)", html))
    # 這些是「有才顯示」的狀態旗標，本例沒有不代表被濾掉
    optional = {"message", "repairing", "repair_incomplete", "needs_regen", "living_incomplete"}
    missing = [k for k in top - optional if k not in out]
    assert not missing, f"result.html 會讀但白名單沒放行：{missing}"


def test_render_and_analysis_fields_survive(out):
    r = out["renders"][0]
    for k in ("style", "style_label", "room_type", "room_key", "angle_label",
              "render_url", "render_filename", "matched_furniture", "soft_furnishing"):
        assert k in r, f"render 少了 {k}"
    for k in ("design_analysis", "layout_notes", "lighting", "space_type"):
        assert k in out["analysis"], f"analysis 少了 {k}"
    for k in ("design_mode", "budget_label_zh", "customer_notes"):
        assert k in out["customer_inputs"], f"customer_inputs 少了 {k}"


def test_dropped_rooms_survive_but_not_the_reject_image():
    """未交付的房間名要留（客戶要知道少了哪間），落選圖網址不留。"""
    src = dict(FULL)
    src["validation_summary"] = {
        "delivered": 3, "dropped": 1, "total": 4,
        "dropped_renders": [{"room_type": "living", "angle_label": "客廳",
                             "blocked_render_url": "https://x/blocked.jpg",
                             "contract_hash": "c" * 64}]}
    o = api._client_result_payload(src)
    dr = o["validation_summary"]["dropped_renders"][0]
    assert dr == {"room_type": "living", "angle_label": "客廳"}


def test_reshoot_card_still_gets_its_failure_signals():
    """🔴 白名單第一版把 failure_class / layout_mode 濾掉了——那是迴歸。

    結果頁的 isModellingFailure() 就是看這兩個欄位是不是
    s2_preflight_blocked / s2_blocked_legacy，才決定要說
    「這個角度建模不了，請換角度重拍」還是「系統已自動修正／聯絡客服免費重出」。
    欄位一掉，判斷永遠 false，付費前被擋下的客廳會拿到完全相反的文案——
    正是 71DC312E / 293BDE11 花力氣拆開的那兩句。
    """
    src = dict(FULL)
    src["validation_summary"] = {
        "delivered": 0, "dropped": 1, "total": 1,
        "dropped_renders": [{
            "room_type": "living", "angle_label": "客廳",
            "failure_class": "s2_preflight_blocked", "layout_mode": "s2_blocked_legacy",
            "style": "nordic", "style_label": "北歐",
            "blocked_render_url": "https://x/blocked.jpg", "contract_hash": "c" * 64}]}
    d = api._client_result_payload(src)["validation_summary"]["dropped_renders"][0]
    assert d.get("failure_class") == "s2_preflight_blocked", "重拍卡判斷失去依據"
    assert d.get("layout_mode") == "s2_blocked_legacy"
    assert "blocked_render_url" not in d, "落選圖網址不該回"
    assert "contract_hash" not in d


def test_whitelist_covers_every_dropped_field_the_page_reads():
    """跟 result.html 對帳：它從 dropped 元素讀的欄位，白名單要全部放行。

    ⚠️ 例外只有 blocked_render_url——那是刻意不回的（落選圖不給客戶看）。
    第一版我只掃了 showDroppedNotice 附近 21 行，漏掉 isModellingFailure()，
    所以才把失敗分類濾掉。這條改成掃全檔。
    """
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    read = set()
    for m in re.finditer(r"(dropped|dropped_renders)[^;{]{0,80}?function\s*\((\w+)\)\s*\{", html):
        var = m.group(2)
        seg = html[m.end():m.end() + 500]
        read |= set(re.findall(r"\b" + var + r"\.([A-Za-z_][A-Za-z0-9_]*)", seg))
    assert read, "掃不到 dropped 的欄位存取，測試目標錯了"
    src = dict(FULL)
    src["validation_summary"] = {"delivered": 0, "dropped": 1, "total": 1,
                                 "dropped_renders": [{k: "x" for k in read}]}
    got = set(api._client_result_payload(src)["validation_summary"]["dropped_renders"][0])
    missing = read - got - {"blocked_render_url"}
    assert not missing, f"result.html 會讀但白名單濾掉了：{sorted(missing)}"


# ── 方案由後端決定 ──────────────────────────────────────────────
def test_plan_comes_from_the_backend(out):
    """前端原本讀 localStorage，別人開分享連結會看到「單一空間」。"""
    assert out["plan"] == "B" and out["room_count"] >= 1


def test_single_room_order_is_plan_a():
    src = {"renders": [{"room_type": "living", "room_key": "living"}],
           "analysis": {"space_type": "客廳"}}
    o = api._client_result_payload(src)
    assert o["plan"] == "A" and o["room_count"] == 1


def test_frontend_prefers_backend_plan():
    """⚠️ 必須內嵌、而且用 typeof 守 window：
    · node 測試樁沒有 window，直接寫 window._hb 會 ReferenceError（實測炸掉 35 條）
    · 測試樁是按函式名逐個抽取的，另開一個 helper 函式抽不到，一樣 ReferenceError
    """
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    hits = [l for l in html.splitlines()
            if "deco_plan" in l and not l.strip().startswith("//")]
    assert len(hits) >= 2, f"找不到兩處方案讀取：{hits}"
    for l in hits:
        assert "window._hb" in l, f"這行仍只讀 localStorage：{l.strip()[:80]}"
        assert "typeof window" in l, f"沒有守 window，node 環境會炸：{l.strip()[:80]}"


# ── 載入失敗不得秀示範資料 ──────────────────────────────────────
def test_load_failure_does_not_show_the_demo_design():
    """🔴 舊行為：載入失敗 → buildUI(_demoRenders)，把寫死的示範設計當成客戶的單。

    付費客戶重整一次（後端重啟／網路不穩）就可能看到別人的示範沙發，畫面上還不說
    那是假的。⚠️ 這跟「生成失敗要重拍」是兩回事，那條走 needs_regen。
    """
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    i = html.index(".catch(function(e)")
    # ⚠️ 要濾掉註解再比：那段的註解裡本來就寫著「原本是 buildUI(_demoRenders)」，
    #    直接掃原始字串會被自己的說明絆倒（第一版就這樣誤紅）。
    seg = chr(10).join(l for l in html[i:i + 1600].splitlines()
                       if not l.strip().startswith("//"))
    assert "_demoRenders" not in seg, "載入失敗仍會顯示示範設計"
    assert "loadResult" in seg, "沒有重試"
    # 沒有 job id 的純展示模式仍可用 demo
    assert "if (!JOB_ID) { buildUI(_demoRenders); return; }" in html


def test_rejected_renders_are_not_shown_to_the_customer():
    """落選圖整塊拿掉：看到品檢沒過的圖只會讓客戶懷疑成品。"""
    html = (ROOT / "result.html").read_text(encoding="utf-8")
    i = html.index("function blockedPreviewHTML")
    seg = html[i:i + 1600]
    assert "<details" not in seg, "落選圖摺疊區還在"
    body = "\n".join(l for l in seg.splitlines() if not l.strip().startswith("//"))
    assert "僅供內部參考" not in body, "「僅供內部參考」這句還會出現在客戶畫面上"


def test_the_endpoint_actually_calls_the_filter():
    """🔴 接線守門：端點必須真的呼叫 _client_result_payload。

    上面每一條都是直接呼叫函式驗的——端點若改回 `return result`，函式再正確也
    等於沒用，而且測試會全綠。（這正是 44c56e6 犯過的錯：修對了函式、接錯了分支。）
    """
    src = (Path(__file__).resolve().parent / "api.py").read_text(encoding="utf-8")
    i = src.index('@app.get("/api/job/{job_id}/result")')
    seg = src[i:i + 1800]
    body = chr(10).join(l for l in seg.splitlines() if not l.strip().startswith("#"))
    assert body.count("_client_result_payload(result)") >= 2, \
        "結果端點沒有把兩條回傳路徑（Supabase 與本機 result.json）都過濾"
    assert not re.search(r"^\s*return result\s*$", body, re.M), \
        "還有一條路徑直接回傳完整 result_json"

# -*- coding: utf-8 -*-
"""付款前說明＋同意（2026-09-25 定稿）。

- 付款按鈕上方四行＋必勾框（含「不適用七日無理由解除權」）＋選填的作品案例同意。
- 後端驗證：沒勾、或客人看到的是舊版說明，都不建單。
- 同意紀錄（條款版本＋伺服器時間、案例同意＋時間）建單當下就寫進 orders.result_json，
  失敗路徑會 merge 既有 result_json，所以不會因為單失敗就不見。
⚠️ 正式效力上線前請懂消保法的人看過這四行與 terms.html。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import api

ROOT = Path(__file__).resolve().parent.parent
PAY = (ROOT / "zoning-confirm.html").read_text(encoding="utf-8")
TERMS = (ROOT / "terms.html").read_text(encoding="utf-8")
HOME = (ROOT / "index.html").read_text(encoding="utf-8")
RESULT = (ROOT / "result.html").read_text(encoding="utf-8")


def test_terms_version_is_the_same_everywhere():
    m = re.search(r"var TERMS_VERSION = '([^']+)'", PAY)
    assert m and m.group(1) == api.TERMS_VERSION
    assert f"版本 {api.TERMS_VERSION}" in TERMS


def test_the_lines_the_customer_agrees_to_are_on_the_payment_page():
    for must in ("不含現場丈量與施工設計", "購買前請核對商品規格與現場尺寸",
                 "留在畫面裡的門、窗與固定廚具不會被移動或改掉",
                 "不因個人喜好改變而退費", "3 個工作天內免費補正",
                 "不適用七日無理由解除權", "不具名方式作為作品案例展示", 'href="terms.html"'):
        assert must in PAY, f"付款頁少了：{must}"
    # 不寫「一律不退」、不提 AI（官網決定不提 AI）
    blk = PAY[PAY.index('id="termsBlock"'):PAY.index('id="payBtn"')]
    assert "一律" not in blk and "AI" not in blk


def test_terms_page_states_the_agreed_rules():
    for must in ("不因個人喜好改變而退費", "3 個工作天內免費補正", "實付金額 ÷ 應交付的空間數",
                 "NT$500", "自下單日起保存 30 天", "合作的雲端運算服務",
                 "移除訂單編號、地址、人物", "畫面外的區域不代表被移除"):
        assert must in TERMS, f"服務說明少了：{must}"
    assert "AI" not in TERMS


def test_retention_wording_matches_the_code():
    """保存期限從【下單日】算：orders 以 created_at 清空 result_json，不是交付日。"""
    assert api.RETENTION_DAYS == 30
    assert "交付後" not in TERMS


def test_homepage_no_longer_promises_you_will_not_buy_wrong():
    assert "不買錯" not in HOME
    assert 'href="terms.html"' in HOME


def test_result_page_carries_the_two_reminders():
    assert "家具尺寸以商品頁規格為準" in RESULT
    assert "購買前請確認尺寸與現貨" in RESULT


# ── 後端真的擋、真的記 ────────────────────────────────────────────────

def _post(monkeypatch, tmp_path, **extra):
    seen, upserts = {}, []
    monkeypatch.setattr(api, "run_pipeline", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(api, "sb_upsert", lambda d, *a, **k: upserts.append(d) or True)
    monkeypatch.setattr(api, "JOBS_DIR", Path(tmp_path) / "jobs")
    monkeypatch.setattr(api, "UPLOADS_DIR", Path(tmp_path) / "uploads")
    (Path(tmp_path) / "jobs").mkdir(parents=True, exist_ok=True)
    up = api.UPLOADS_DIR / "PYTEST_TERMS"
    up.mkdir(parents=True, exist_ok=True)
    (up / "paths.json").write_text(json.dumps([str(up / "p0.jpg")]), encoding="utf-8")
    from fastapi.testclient import TestClient
    r = TestClient(api.app).post("/api/job", data={
        "upload_id": "PYTEST_TERMS", "styles": "modern", "plan": "A", "space_type": "living", **extra})
    return r, seen, upserts


def test_no_consent_no_order(monkeypatch, tmp_path):
    r, seen, upserts = _post(monkeypatch, tmp_path)
    assert r.status_code == 400 and "勾選" in r.json()["error"]
    assert not seen and not upserts, "沒勾卻已經開始建單"


def test_stale_version_is_rejected(monkeypatch, tmp_path):
    r, seen, _ = _post(monkeypatch, tmp_path, terms_accepted="1", terms_version="2020-01-01")
    assert r.status_code == 400 and "更新" in r.json()["error"]
    assert not seen


def test_consent_is_recorded_at_order_creation(monkeypatch, tmp_path):
    r, seen, upserts = _post(monkeypatch, tmp_path, terms_accepted="1",
                             terms_version=api.TERMS_VERSION, showcase_consent="1")
    assert r.status_code == 200, r.text
    c = seen["consent"]
    assert c["terms_version"] == api.TERMS_VERSION and c["terms_accepted_at"]
    assert c["showcase_consent"] is True and c["showcase_consent_at"]
    created = [u for u in upserts if u.get("status") == "queued"][0]
    assert created["result_json"]["customer_inputs"]["consent"] == c


def test_showcase_is_optional_and_recorded_as_no(monkeypatch, tmp_path):
    r, seen, _ = _post(monkeypatch, tmp_path, terms_accepted="1", terms_version=api.TERMS_VERSION)
    assert r.status_code == 200
    assert seen["consent"]["showcase_consent"] is False
    assert seen["consent"]["showcase_consent_at"] is None


def test_consent_is_never_sent_back_to_the_customer_page():
    assert "consent" not in api._CLIENT_INPUTS

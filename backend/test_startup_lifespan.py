# -*- coding: utf-8 -*-
"""啟動生命週期｜清理跑得到，但清理爆掉不准拖垮服務。

`@app.on_event("startup")` 已被 FastAPI 棄用，改用 `lifespan`。搬家最容易出的
兩種事故，這裡各釘一條：

  1. **搬完沒接上** —— lifespan 忘了呼叫 watchdog，過期 job／storage／orders
     再也不會被清，而且沒有任何錯誤訊息，可以壞很久沒人發現。
  2. **搬完擋住啟動** —— lifespan 裡的例外會讓 uvicorn 啟動失敗，
     Railway 的 `/health` 永遠不回應 ⇒ 全站中斷。
     清理失敗只是垃圾沒清到；服務起不來是所有客戶都進不來。

⚠️ 「三個子函式各自都有 try/except」不等於外層不用包：
   `threading.Thread(...).start()` 本身、以及日後任何新增的啟動步驟，
   都在那三層防護之外。
"""
from __future__ import annotations

import inspect

import api
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def watchdog_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(api, "_sweep_stale_jobs", lambda: calls.append("sweep"))
    monkeypatch.setattr(api, "_purge_expired_storage",
                        lambda: calls.append("storage"))
    monkeypatch.setattr(api, "_purge_expired_orders",
                        lambda: calls.append("orders"))
    return calls


def test_startup_cleanup_actually_runs(watchdog_calls):
    """lifespan 必須真的把三件清理跑起來——搬家最常見的失敗是「忘了接上」。"""
    with TestClient(api.app) as c:
        assert c.get("/health").status_code == 200
    # storage / orders 走背景執行緒，給它們一點時間收斂
    for _ in range(100):
        if {"sweep", "storage", "orders"} <= set(watchdog_calls):
            break
        import time
        time.sleep(0.02)
    assert "sweep" in watchdog_calls, "過期 job 掃描沒被呼叫"
    assert {"storage", "orders"} <= set(watchdog_calls), (
        f"背景清理沒被呼叫，只跑了 {watchdog_calls}")


def test_service_still_starts_when_cleanup_explodes(monkeypatch):
    """🔴 清理爆掉時服務仍要起得來、/health 仍要回 ok。

    這條就是外層 try/except 的存在理由。拿掉它，這個測試會直接炸在
    TestClient 啟動，等同 Railway 起不來。
    """
    def _boom():
        raise RuntimeError("模擬啟動清理爆炸")

    monkeypatch.setattr(api, "_startup_watchdog", _boom)
    with TestClient(api.app) as c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json().get("status") == "ok", (
        "啟動清理爆炸就把服務拖垮了——Railway 會整站無回應")


def test_deprecated_on_event_is_gone():
    """棄用的 on_event 不得殘留——兩套啟動機制並存會讓人以為改了其實沒生效。"""
    src = inspect.getsource(api)
    assert 'on_event("startup")' not in src
    assert "lifespan=lifespan" in src, "app 沒有真的掛上 lifespan"


def test_lifespan_is_defined_before_the_app_uses_it():
    """`app = FastAPI(..., lifespan=lifespan)` 必須排在 lifespan 定義之後。

    這次重構把 app 建立整段往後搬，順序錯就是 import 時 NameError，
    Railway 連啟動都不會開始。
    """
    src = inspect.getsource(api)
    assert src.index("async def lifespan") < src.index("lifespan=lifespan")
    # 所有 @app.* 裝飾器也都必須在 app 建立之後
    app_at = src.index("\napp = FastAPI(")
    first_route = src.index("\n@app.")
    assert app_at < first_route, "有路由裝飾器排在 app 建立之前"

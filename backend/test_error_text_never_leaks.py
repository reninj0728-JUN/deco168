# -*- coding: utf-8 -*-
"""錯誤原文的**全出口**封鎖｜端到端，不是逐欄位掃。

🔴 這一檔的存在理由：上一輪我只清掉 `exception_message` 就宣告修好了，
但同一串原文仍從三條路出去（GPT 2026-08-07 用實際呼叫重現，不是理論風險）：

  ① `event["raw_verdict"]` 是 `raw` 的完整深拷貝 → 進 validation_history
     → 進 result_json → 客戶可見
  ② 同一個 event 整包被 `print()` → Railway log
  ③ `dropped_renders[].reason` 直接讀 `r["error"]`
  ④ `slim_renders[].render_error` 直接讀 `r["error"]`

而當時的測試甚至**主動保護**了這個行為
（`assert event["raw_verdict"]["error"] == "429 RESOURCE_EXHAUSTED"`）。

所以這裡不驗「某個欄位有沒有被清掉」——那種寫法每多一條路就漏一次。
改成：把哨兵字串餵進去，然後**把整份輸出序列化成一個字串**，斷言哨兵不在裡面。
新增任何欄位都自動被這條測試覆蓋。
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import api
import pytest

# 每個哨兵代表一種真實會出現在 fal / storage / Gemini 錯誤裡的機密形態
SENTINELS = {
    "bearer":   "Bearer TOPSECRETtokenvalue123",
    "url":      "https://signed.example.com/x?sig=deadbeefcafe",
    "abspath":  "/app/jobs/ABC123/render_modern_00.jpg",
    "winpath":  "C:/Users/deploy/AppData/Local/Temp/job/render.jpg",
    "unc":      r"\\server\share\job\photo.jpg",
    "r2":       "r2://uploads/JOB/photo_01.jpg",
    "apikey":   "apikey=sk-live-0123456789abcdef",
}
POISON = " ".join(SENTINELS.values())


def _assert_clean(blob: str, where: str):
    hits = [name for name, s in SENTINELS.items() if s in blob]
    assert not hits, f"{where} 洩漏了 {hits}"


# ── ① validation_history 的 raw_verdict + ② stdout ─────────────────

def _record(raw: dict, stage="post_render"):
    """跑一次 _record_validation_attempt，同時攔截 stdout。"""
    render: dict = {"style": "modern", "room_type": "living"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        event = api._record_validation_attempt(
            render, job_id="TESTJOB", stage=stage, attempt=1, validation=raw)
    return event, render, buf.getvalue()


def test_raw_verdict_and_stdout_are_both_clean():
    """一次驗兩條路：event 本身，以及它被 print 出去的那份。"""
    raw = {
        "ok": False, "hard_fail": True,
        "reason": "沙發緊貼大門門框擺放",           # 判官敘述，要留
        "ceiling_changed": True,                    # 幾何旗標，要留
        "error": POISON,                            # 系統注入，不可留
        "exception_message": POISON,
        "traceback": POISON,
        "raw_response": POISON,
    }
    event, render, out = _record(raw)
    _assert_clean(json.dumps(event, ensure_ascii=False, default=str), "event")
    _assert_clean(json.dumps(render, ensure_ascii=False, default=str),
                  "render.validation_history")
    _assert_clean(out, "stdout（Railway log）")
    assert out.strip(), "什麼都沒印——那不是安全，是把診斷一起弄丟了"


def test_judge_fields_survive_the_scrub():
    """反貧血：清洗不得把判官的判定一起洗掉，否則排查就瞎了。"""
    raw = {
        "ok": False, "hard_fail": True, "room_type": "living",
        "reason": "電視櫃貼鄰大門擺放阻擋進門落塵區",
        "sofa_zone_assessment": "沙發靠畫面左側牆面放置",
        "focal_anchor_assessment": "電視長櫃與沙發正面相對",
        "confirmed_living_zone_reference": "靠窗端 / 房間深處 (back)",
        "ceiling_changed": True, "furniture_blocks_door": True,
        "sofa_depth_percent_estimate": 42.5,
        "sofa_side_detected": "unclear",
        "render_bboxes": {"sofa": [10, 20, 30, 40], "entrance_door": None},
        "product_visibility": {"sofa": "visible", "rug": "missing"},
        "visibility_nice_bad": ["accent_chair"],
        "error": POISON,
    }
    event, _r, _o = _record(raw)
    rv = event["raw_verdict"]
    assert rv["reason"] == raw["reason"]
    assert rv["sofa_zone_assessment"] == raw["sofa_zone_assessment"]
    assert rv["focal_anchor_assessment"] == raw["focal_anchor_assessment"]
    assert rv["confirmed_living_zone_reference"] == raw["confirmed_living_zone_reference"]
    assert rv["ceiling_changed"] is True and rv["furniture_blocks_door"] is True
    assert rv["sofa_depth_percent_estimate"] == 42.5
    assert rv["sofa_side_detected"] == "unclear"
    assert rv["render_bboxes"] == {"sofa": [10, 20, 30, 40], "entrance_door": None}
    assert rv["product_visibility"] == {"sofa": "visible", "rug": "missing"}
    assert rv["visibility_nice_bad"] == ["accent_chair"]
    assert "error" not in rv


def test_redaction_is_visible_not_silent():
    """被擋下的欄位要留【名字】（不是值）——靜默的防線等於沒有防線。

    door_gap 守門那次就是靜默消失，合格數從 7 變 11 卻沒人知道為什麼。
    """
    event, _r, _o = _record({"ok": False, "reason": "x", "error": POISON,
                             "raw_response": POISON})
    red = event["raw_verdict"].get("_redacted_text_fields")
    assert red == ["error", "raw_response"], f"沒記下被擋的欄位名：{red}"
    _assert_clean(json.dumps(event, ensure_ascii=False), "redaction 紀錄本身")


def test_ok_none_is_not_treated_as_drop():
    """`ok: None` 是合法值（驗證沒跑），不得被當成「丟棄」哨兵刪掉。"""
    event, _r, _o = _record({"ok": None, "reason": "驗證未執行"})
    assert "ok" in event["raw_verdict"] and event["raw_verdict"]["ok"] is None


def test_new_unknown_text_field_is_dropped_by_default():
    """白名單語意：判官哪天多一個字串欄位，預設是**擋下**不是放行。

    黑名單會漏掉下一個新欄位——這個專案已經在渲染模型、重拍文案兩輪各栽過一次。
    """
    event, _r, _o = _record({"ok": False, "some_future_field": POISON})
    assert "some_future_field" not in event["raw_verdict"]
    assert "some_future_field" in event["raw_verdict"]["_redacted_text_fields"]


# ── ③ dropped_renders[].reason ────────────────────────────────────

@pytest.mark.parametrize("failure_class,expect_label", [
    ("infrastructure", "額度／餘額不足"),
    ("validator_exception", None),
])
def test_dropped_reason_uses_classified_label_not_raw(failure_class, expect_label):
    """落選原因走系統錯誤分支時，必須是分類標籤，不得是原文。"""
    out = api._customer_safe_message(
        failure_class, f"insufficient balance {POISON}", "SomeError")
    _assert_clean(str(out), f"dropped reason（{failure_class}）")
    if expect_label:
        assert out == expect_label, "分類標籤丟了，「是沒錢還是逾時」又看不到了"


def test_dropped_reason_branch_does_not_read_raw_error():
    """程式碼層：else 分支不得直接把 r.error 指給 reason。"""
    import ast
    import inspect
    import textwrap
    src = api.__dict__["__file__"]
    body = io.open(src, encoding="utf-8").read()
    i = body.index('reason = _safe_sys or _v_reason or "render 未產出"')
    window = body[i - 600:i]
    code = "\n".join(ln for ln in window.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert 'reason = r.get("error")' not in code, "落選原因又直接讀 r.error 了"
    assert "_customer_safe_message(" in code, "系統錯誤沒有先過安全分類"


# ── ④ slim_renders[].render_error ─────────────────────────────────

def test_render_error_is_a_label_or_none():
    assert api._classify_infra_message(POISON, None) == "上游服務錯誤"
    _assert_clean(str(api._classify_infra_message(POISON, None)), "render_error")
    assert api._classify_infra_message("", None) is None


def test_render_error_serialisation_is_guarded_in_source():
    """成功重試後 r.error 可能還留著上一輪的字串——不能無條件序列化。"""
    body = io.open(api.__dict__["__file__"], encoding="utf-8").read()
    i = body.index('"render_error":      (_classify_infra_message(')
    seg = body[i:i + 260]
    assert 'if r.get("error") else None' in seg, "沒出錯的 render 也會被貼標籤"


# ── 全出口彙總：一份假的完整 render 走過所有序列化 ──────────────────

def test_every_customer_visible_surface_at_once():
    """把哨兵塞進 render 的每一個錯誤欄位，掃過所有客戶可見輸出。"""
    render = {
        "style": "modern", "room_type": "living",
        "error": POISON, "render_error": POISON, "retry_reason": POISON,
        "error_type": "FalGenerationTimeout",
        "validation": {"ok": False, "hard_fail": True, "reason": "沙發擋門",
                       "error": POISON, "furniture_blocks_door": True},
        "validation_history": [],
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        api._record_validation_attempt(render, job_id="TESTJOB",
                                       stage="post_render", attempt=1,
                                       validation=render["validation"])
        diag = api._validation_diagnostics(render)
    _assert_clean(json.dumps(diag, ensure_ascii=False, default=str),
                  "_validation_diagnostics（進 dropped payload）")
    _assert_clean(buf.getvalue(), "stdout")
    # 反貧血：診斷價值還在
    assert diag["failure_class"] == "infrastructure"
    assert diag["validation_final"]["exception_message"] == "生成或驗證逾時"
    assert diag["validation_history"], "history 被清空了——那不是安全是失憶"


# ── ⑤ slim_renders[].validation（我自己掃出來的第五條路）─────────────

def test_slim_render_validation_is_scrubbed_too():
    """`slim_renders[].validation` 是整包序列化進客戶 payload 的。

    線上 233 筆 render 裡 15 筆的 `validation.error` 有值（2026-08-07 實查）。
    這條不在回饋清單裡，是掃「還有哪些地方整包序列化 validation」時找到的——
    修洩漏要掃出口，不要只修被指出的那幾個。
    """
    v = {"ok": False, "hard_fail": True, "reason": "沙發擋門",
         "furniture_blocks_door": True, "error": POISON}
    out = api._safe_verdict(v)
    _assert_clean(json.dumps(out, ensure_ascii=False), "slim_renders[].validation")
    assert out["reason"] == "沙發擋門" and out["furniture_blocks_door"] is True
    assert out["_redacted_text_fields"] == ["error"]


def test_slim_render_serialisation_uses_the_scrubber():
    body = io.open(api.__dict__["__file__"], encoding="utf-8").read()
    i = body.index('"validation":        ')
    seg = body[i:i + 120]
    assert "_safe_verdict(" in seg, "slim_renders 的 validation 又整包塞進去了"


def test_tiny_val_stays_a_whitelist():
    """極簡版 `_tiny_val` 本來就是正向認列——不得有人加上 error。"""
    body = io.open(api.__dict__["__file__"], encoding="utf-8").read()
    i = body.index("def _tiny_val(")
    seg = body[i:i + 700]
    assert '"error"' not in seg, "_tiny_val 被加進 error 欄位了"


# ── ⑥ product_visibility 的正式列舉值不得被清洗誤刪 ──────────────────

def test_all_official_product_visibility_values_survive():
    """🔴 `different` 是正式判定值，第一版白名單把它刪了（GPT 2026-08-07 實測）。

    `different` = 圖上有同類物件但明顯是另一件商品，會觸發
    `product_visibility_fail`（gemini_analyze.py 用 PRODUCT_VISIBILITY_BAD 判）。
    交付判定不受影響（那條路讀的是 render["validation"] 的旗標），
    但事後查「到底哪一件畫錯」就查不到了——診斷回歸也是回歸。
    """
    from gemini_analyze import PRODUCT_VISIBILITY_VALUES
    pv = {f"slot_{i}": v for i, v in enumerate(PRODUCT_VISIBILITY_VALUES)}
    out = api._safe_verdict({"ok": False, "product_visibility": pv})
    assert out["product_visibility"] == pv, (
        f"正式列舉值被清洗刪掉：少了 "
        f"{set(pv) - set(out.get('product_visibility') or {})}")
    assert "different" in PRODUCT_VISIBILITY_VALUES, "契約變了，這條測試要重看"


def test_whitelist_comes_from_the_contract_not_a_hand_written_list():
    """白名單必須從宣告端 import，不得在 api.py 另抄一份。

    兩套口徑是這專案的老毛病：HARD_FAIL_FLAGS 已經共用了，這個當時沒有——
    我照「線上抽樣看到的值」手寫，漏了 different、還多寫了契約裡沒有的 partial。
    """
    import inspect
    src = inspect.getsource(api._product_visibility_values)
    assert "from gemini_analyze import PRODUCT_VISIBILITY_VALUES" in src, (
        "沒有共用宣告端的契約")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    for invented in ('"partial"', '"visible"', '"missing"', '"different"'):
        assert invented not in code, f"又在 api.py 抄了一份列舉值（{invented}）"


def test_prompt_and_verdict_scrub_use_the_same_values():
    """prompt 給 Gemini 的選項字串，跟清洗放行的值，必須是同一份。"""
    import gemini_analyze as G
    for v in G.PRODUCT_VISIBILITY_VALUES:
        assert v in api._VERDICT_ENUM_VALUES, f"prompt 會產出 {v} 但清洗不放行"
    for v in G.PRODUCT_VISIBILITY_BAD:
        assert v in G.PRODUCT_VISIBILITY_VALUES, f"{v} 不在正式列舉裡"


def test_scrub_still_blocks_unknown_values_in_that_slot():
    """反向：`product_visibility` 底下的自由文字仍要擋——放行的是契約值，不是整欄。"""
    out = api._safe_verdict(
        {"ok": False, "product_visibility": {"sofa": "visible", "rug": POISON}})
    _assert_clean(json.dumps(out, ensure_ascii=False), "product_visibility")
    assert out["product_visibility"] == {"sofa": "visible"}


def test_missing_contract_import_fails_closed_not_open(monkeypatch):
    """🔴 import 不到契約時必須**收緊**（不放行任何列舉值），不是放行一份備份表。

    2026-07 的教訓：缺 `google-genai` 時規劃器的門距守門靜默消失，合格數
    7→11 沒人知道為什麼——那次是「缺套件 → 防線失效 → 放寬」。
    這裡失效方向必須相反。

    ⚠️ 這條是補一個**蓄意破壞測不到**的洞：我把 fallback 改成回傳一份硬寫的
    備份表，18 條測試全綠——因為測試環境 import 得到，那段程式碼根本沒被跑到。
    「沒有測試覆蓋的 except 分支」就是下一個靜默失效點。
    """
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "gemini_analyze":
            raise ImportError("simulated missing module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    monkeypatch.delitem(__import__("sys").modules, "gemini_analyze", raising=False)

    assert api._product_visibility_values() == frozenset(), (
        "契約 import 失敗時仍放行列舉值——那是 fail-open，"
        "等於在最不該信任的時候最寬鬆")

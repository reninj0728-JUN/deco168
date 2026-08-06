# -*- coding: utf-8 -*-
"""落選理由要寫真死因；不是所有 legacy_fallback 都叫客戶重拍。

293BDE11（2026-08-06）：客廳零圖，客戶看到的是
「這個拍攝角度我們的空間建模無法完整判讀…建議重拍」。

實際上：
  · 照片拍得很正，S2 規劃前檢 `判官前規劃合格: True`
  · 落選理由寫「購買清單中的藤編搖椅未出現在圖上」——但搖椅是
    `visibility_nice_bad`（非必備、本來就不殺圖），`product_visibility_fail: false`
  · **真正觸發 hard_fail 的是 `ceiling_changed`**：模型憑空畫出整片天花管線
    （四張原始照片天花板全部乾淨無管線）
  · 兩次自救都 `MissingRenderPath`，沒有新候選

兩個文案 bug：
① 判官的 `reason` 是自由文字，會挑它覺得值得講的講，不保證等於死因
   → 客服與排查被帶去查商品方向
② `legacy_fallback` 的判斷排在系統錯誤前面，而 `legacy_fallback` **不是失敗**
   ——A62AC21A 走同一條路徑（`crop_note=s2_waived_living_zone_zoom`）成功交付
   → 客戶被叫去重拍，但重拍不會改變 bbox 語意，同一個坑會再踩
"""
from __future__ import annotations

import re
from pathlib import Path

import api
import pytest

HTML = (Path(__file__).resolve().parent.parent / "result.html").read_text(encoding="utf-8")

# 293BDE11 客廳第一次判定，逐字取自 validation_history
J293_LIVING = {
    "ok": False, "hard_fail": True, "room_type": "living",
    "reason": "購買清單中的藤編搖椅未出現在圖上",
    "product_visibility_fail": False,
    "visibility_nice_bad": ["accent_chair"],
    "ceiling_changed": True,      # ← 真正的死因
    "walls_changed": False, "floor_changed": False, "windows_changed": False,
    "furniture_blocks_door": False, "sofa_focal_face_each_other": True,
}


# ── ① 真死因 ────────────────────────────────────────────────────────

def test_triggered_hard_flags_finds_the_real_cause():
    flags = api._triggered_hard_flags(J293_LIVING)
    assert "ceiling_changed" in flags, f"沒抓到真正的硬傷，只有 {flags}"
    assert "product_visibility_fail" not in flags, "把非必備缺漏當成硬傷了"


def test_triggered_hard_flags_uses_the_judge_list():
    """不得在 api.py 另抄一份 flag 清單——兩套口徑是這專案的老毛病。"""
    import ast
    import inspect
    import textwrap
    src = inspect.getsource(api._triggered_hard_flags)
    assert "from gemini_analyze import HARD_FAIL_FLAGS" in src, "沒有共用判官的清單"
    # 只看程式碼本體：docstring 裡舉例寫 ceiling_changed 是說明，不是抄清單。
    # （這個坑我在比例鎖與天花守門兩輪都踩過——掃字串會抓到自己的說明文字。）
    fn = ast.parse(textwrap.dedent(src)).body[0]
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           and isinstance(fn.body[0].value.value, str)) else fn.body
    code = "\n".join(ast.unparse(n) for n in body)
    for stolen in ("ceiling_changed", "walls_changed", "furniture_blocks_door"):
        assert stolen not in code, f"把 {stolen} 抄了一份到 api.py"


def test_clean_verdict_has_no_flags():
    """反貧血：乾淨的判定不得憑空生出硬傷。"""
    assert api._triggered_hard_flags(
        {"ok": True, "hard_fail": False, "ceiling_changed": False}) == []
    assert api._triggered_hard_flags(None) == []


def test_dropped_reason_prepends_real_flags():
    """落選理由必須帶上真正的 hard flag，不能只留判官的自由文字。"""
    import inspect
    src = inspect.getsource(api.run_pipeline) if hasattr(api, "run_pipeline") else ""
    if not src:
        src = Path(api.__file__).read_text(encoding="utf-8")
    assert "_triggered_hard_flags(v)" in src, "落選理由沒有計算真正的 hard flag"
    assert '"triggered_hard_flags"' in src, "落選紀錄沒有存下 hard flag，事後查不到"


# ── ② 重拍文案 ──────────────────────────────────────────────────────

def _summary(**kw):
    d = {"failure_class": "render_quality", "layout_mode": "legacy_fallback",
         "triggered_hard_flags": []}
    d.update(kw)
    return {"dropped_renders": [d]}


def test_any_post_generation_hardfail_never_asks_for_reshoot():
    """🔴 生成後的【任何】hard flag 都不是照片的錯，一律免費重出。

    第一版我寫成「排除 8 個品質旗標之後才講重拍」——那是**黑名單**，而
    `HARD_FAIL_FLAGS` 有 20 個。沙發擋門、擋走道、沙發錯邊…全在漏網名單裡，
    走 legacy 的圖照樣會被叫去重拍（GPT 2026-08-06 抓到）。
    同一個黑名單陷阱我在渲染模型白名單那輪已經被抓過一次。
    """
    from gemini_analyze import HARD_FAIL_FLAGS
    assert len(HARD_FAIL_FLAGS) >= 20, "旗標數變了，這條測試的前提要重看"
    for flag in HARD_FAIL_FLAGS:
        msg = api._incomplete_message(_summary(triggered_hard_flags=[flag]))
        assert "重拍" not in msg, f"{flag} 仍叫客戶重拍：{msg}"


def test_system_error_does_not_ask_for_reshoot():
    """系統／驗證器錯誤也不是照片的問題。"""
    msg = api._incomplete_message(
        _summary(failure_class="validator_exception", triggered_hard_flags=[]))
    assert "重拍" not in msg, f"仍叫客戶重拍：{msg}"
    assert "非設計問題" in msg


def test_plain_legacy_fallback_is_not_reshoot_evidence():
    """🔴 `legacy_fallback` 只是救援模式，不是重拍證據。

    A62AC21A 走同一條路徑成功交付。沒有明確的建模失敗訊號時，
    不得叫客戶重拍——連「沒有任何 hard flag」也不行。
    ⚠️ 我第一版的測試反過來鎖住「legacy_fallback 沒有那 8 個旗標就必須重拍」，
    等於把錯誤推論保護起來。
    """
    msg = api._incomplete_message(_summary(triggered_hard_flags=[]))
    assert "重拍" not in msg, f"單憑 legacy_fallback 就叫重拍：{msg}"
    assert "免費重出" in msg


@pytest.mark.parametrize("signal_field,signal", [
    ("failure_class", "s2_preflight_blocked"),
    ("layout_mode", "s2_blocked_legacy"),
])
def test_explicit_modelling_failure_still_asks_for_reshoot(signal_field, signal):
    """反貧血：真的是付費前建模失敗時，重拍建議必須還在——那條路本來就有用。"""
    msg = api._incomplete_message(_summary(**{signal_field: signal}))
    assert ("重拍" in msg) or ("正面照片" in msg), f"{signal} 不再建議重拍：{msg}"


def test_reshoot_uses_a_whitelist_not_a_blacklist():
    """實作必須是正向認列。黑名單（排除 N 個旗標）一定會漏。"""
    import inspect
    src = inspect.getsource(api._incomplete_message)
    assert "_MODELLING_FAILURE_SIGNALS" in src, "沒有建模失敗訊號的白名單"
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "triggered_hard_flags" not in code, (
        "重拍判斷又回去看 hard flag 黑名單了——那會漏掉 20 個裡沒列到的")


# ── ③ 前端同一套判斷 ────────────────────────────────────────────────

def test_frontend_mirrors_the_same_whitelist():
    """前端必須跟後端同一套正向認列，不得自己維護一份黑名單。"""
    m = re.search(r"var _MODELLING_FAILURE_SIGNALS =\s*\[(.*?)\];", HTML, re.S)
    assert m, "前端沒有建模失敗訊號的白名單"
    fe = set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))
    import inspect
    be_src = inspect.getsource(api._incomplete_message)
    m2 = re.search(r"_MODELLING_FAILURE_SIGNALS = \{(.*?)\}", be_src, re.S)
    assert m2, "後端沒有白名單"
    be = set(re.findall(r'"([a-z0-9_]+)"', m2.group(1)))
    assert fe == be, f"前後端白名單不一致：前端多 {fe-be}、後端多 {be-fe}"
    assert "_QUALITY_FLAGS" not in HTML, "前端還留著黑名單"


def test_frontend_reshoot_needs_a_positive_signal():
    m = re.search(r"var _isReshoot = (.*?);", HTML, re.S)
    assert m, "找不到 _isReshoot"
    cond = m.group(1)
    # 2026-08-07：訊號判斷抽成共用的 isModellingFailure()，客廳佔位卡吃同一份。
    assert "isModellingFailure" in cond, "前端沒有用正向訊號決定重拍卡"
    assert "legacy_fallback" not in cond, "legacy_fallback 又單獨決定重拍卡了"


# ── ④ MissingRenderPath 診斷 ────────────────────────────────────────

def test_all_four_exits_have_diag():
    """四個出口都要補——我第一版只補了 z3 與 phase2，另外兩個漏了，
    而測試只驗「至少兩處」所以全綠（GPT 2026-08-06 抓到）。"""
    src = Path(api.__file__).read_text(encoding="utf-8")
    n_exits = src.count('"exception_type": "MissingRenderPath"')
    n_diag = src.count('"diag": _missing_render_diag(')
    assert n_exits >= 4, f"MissingRenderPath 出口只剩 {n_exits} 個，前提變了"
    assert n_diag == n_exits, f"{n_exits} 個出口只有 {n_diag} 個補了診斷"


LEAKY_ERRORS = [
    "C:/Users/deploy/AppData/Local/Temp/job/render.jpg",   # 正斜線 Windows 路徑
    r"\server\share\job\render.jpg",                      # UNC
    "/workspace/jobs/X/render.jpg",                        # 不在常見 root 清單裡
    "r2://uploads/job/photo.jpg",
    "supabase://uploads/ABC/photo_01.jpg",
    "./jobs/ABC123/render_modern_00.jpg",                  # 相對路徑
    "failed https://fal.media/x?sig=abc&token=deadbeef",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
]


@pytest.mark.parametrize("raw", LEAKY_ERRORS)
def test_diag_stores_no_free_text_at_all(raw):
    """🔴 診斷【完全不存】錯誤原文——那是唯一擋得住路徑洩漏的做法。

    `dropped_renders` 會進客戶可見 payload。我第一版寫了遮蔽函式想把網址／
    路徑／token 濾掉，GPT 2026-08-06 實測**這 6 種路徑格式全部漏**：
    正斜線 Windows、UNC、/workspace、r2://、supabase://、相對路徑。
    跟路徑格式打地鼠打不完，所以改成根本不放文字。完整錯誤只留 Railway log。
    """
    diag = api._missing_render_diag(
        render_path="C:/app/x.jpg", base_path="/app/b.jpg",
        error=raw, error_type="FalError")
    blob = str(diag)
    for token in ("render.jpg", "photo.jpg", "fal.media", "Bearer",
                  "workspace", "server", "uploads", "C:/", "/app/"):
        assert token not in blob, f"診斷洩漏了「{token}」：{blob}"
    assert diag["render_error_present"] is True, "連「有沒有錯誤」都丟了"
    assert diag["error_type"] == "FalError", "錯誤類型要留著，那是分類依據"


def test_masking_helper_is_gone():
    """遮蔽函式必須整個移除，不留給人再拿去用（它擋不住 6 種路徑格式）。"""
    assert not hasattr(api, "_safe_error_digest")
    assert not hasattr(api, "_SECRET_PATTERNS")


def test_diag_still_distinguishes_the_three_failure_modes():
    """反貧血：剩下的欄位必須還能分辨「fal 沒回圖／檔案沒落地／底圖不見了」。"""
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        real = _P(td) / "r.jpg"
        real.write_bytes(b"x")
        d1 = api._missing_render_diag(render_path=None, base_path=str(real))
        assert d1["render_path_set"] is False and d1["base_file_exists"] is True
        d2 = api._missing_render_diag(render_path=str(_P(td) / "nope.jpg"),
                                      base_path=str(real))
        assert d2["render_path_set"] is True and d2["render_file_exists"] is False
        d3 = api._missing_render_diag(render_path=str(real),
                                      base_path=str(_P(td) / "gone.jpg"))
        assert d3["render_file_exists"] is True and d3["base_file_exists"] is False


def test_whitelist_has_no_dead_signals():
    """🔴 白名單只能放 production 真的會寫出來的值。

    第一版放了 `geometry_not_modelable`，但全庫沒有任何地方產出那個字串，
    而且它讀的 `blocked_reason_class`（實際值 INSUFFICIENT_EVIDENCE /
    INVALID_GEOMETRY）根本沒被帶進 dropped payload——那條分支永遠命中不了，
    測試只是人工塞不存在的資料才綠。死訊號比沒有更糟：讓人以為涵蓋了。
    """
    import inspect
    src = inspect.getsource(api._incomplete_message)
    m = re.search(r"_MODELLING_FAILURE_SIGNALS = \{(.*?)\}", src, re.S)
    assert m, "找不到白名單"
    assert set(re.findall(r'"([a-z0-9_]+)"', m.group(1))) == \
        {"s2_preflight_blocked", "s2_blocked_legacy"}, "白名單有死訊號或漏了"
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "blocked_reason_class" not in code, (
        "還在讀 blocked_reason_class——那個欄位沒被帶進 dropped payload，永遠是 None")


def test_frontend_whitelist_matches_and_has_no_dead_signals():
    m = re.search(r"var _MODELLING_FAILURE_SIGNALS = \[(.*?)\];", HTML, re.S)
    assert m, "前端沒有白名單"
    assert set(re.findall(r"'([a-z0-9_]+)'", m.group(1))) == \
        {"s2_preflight_blocked", "s2_blocked_legacy"}
    # 只掃程式碼：註解裡解釋「為什麼不讀 blocked_reason_class」是說明不是讀取。
    # （這個坑我這一輪已經踩第三次了——掃字串會抓到自己的說明文字。）
    code = "\n".join(ln for ln in HTML.splitlines()
                     if not ln.strip().startswith("//"))
    assert "blocked_reason_class" not in code, "前端還在讀沒有被帶出來的欄位"


# ── ⑤ log 也不能洩漏 ────────────────────────────────────────────────

def test_missing_render_prints_never_include_raw_error():
    """🔴 我把洩漏從 payload 搬到 Railway log 就當作修好了——那不算修。

    log 一樣會外流（GPT 2026-08-07 指出）。四個出口的 print 只能印結構化的
    `diag`，不得印 `raw_error`／`error` 原文。
    """
    import inspect
    src = Path(api.__file__).read_text(encoding="utf-8")
    assert "raw_error=" not in src, "print 又把錯誤原文印出來了"
    for marker in ("初驗無圖可驗", "z3 重驗無圖可驗", "phase2 硬修後無圖可驗",
                   "phase3 補生無圖可驗"):
        i = src.index(marker)
        line_end = src.index("\n", i)
        line = src[src.rindex("\n", 0, i):line_end]
        assert "get('error')" not in line and 'get("error")' not in line, (
            f"「{marker}」那行 print 帶了錯誤原文")


@pytest.mark.parametrize("raw", LEAKY_ERRORS)
def test_exception_message_never_carries_free_text_for_system_errors(raw):
    """系統例外的原文不得進客戶可見 payload——只留 exception_type 或分類標籤。"""
    for cls in ("validator_exception", "infrastructure", "s2_preflight_blocked"):
        out = api._customer_safe_message(cls, raw, "SomeError")
        blob = str(out)
        for token in ("render.jpg", "photo.jpg", "fal.media", "Bearer",
                      "workspace", "server", "uploads", "C:/", "/app/"):
            assert token not in blob, f"{cls} 洩漏了「{token}」：{blob}"


def test_infrastructure_still_says_what_kind_of_failure():
    """反貧血：分類標籤必須保住「是沒錢還是逾時」——2026-07-19 fal 餘額耗盡
    被報成「配置驗收失敗」，害人往格局方向查了一整天。"""
    assert api._customer_safe_message(
        "infrastructure", "insufficient balance for request") == "額度／餘額不足"
    assert api._customer_safe_message(
        "infrastructure", "fal request exceeded 180 seconds") == "生成或驗證逾時"
    assert api._customer_safe_message(
        "infrastructure", "", "FalGenerationTimeout") == "生成或驗證逾時"
    assert api._customer_safe_message(
        "infrastructure", "http 503 service unavailable") == "上游服務暫時不可用"


def test_customer_safe_message_never_passes_free_text_through():
    """🔴 這條原本是反過來寫的：斷言 render_quality 會原文放行判官敘述。

    但查過三個呼叫端後確認，**沒有任何一個會傳判官敘述進來**——傳的一律是
    `raw["error"]` / `str(exception)`。judge 回 ok=False 而 raw 又帶 error 時，
    failure_class 被算成 render_quality，整串系統原文就原封放行了
    （GPT 2026-08-07 用 `Bearer …` 實測打穿）。我用測試把那個洞鎖住了一輪。

    判官敘述有自己的路（`dropped_renders[].reason`、`raw_verdict["reason"]`），
    不需要也不應該經過這裡。
    """
    for cls in ("render_quality", "validator_exception", "s2_preflight_blocked", None):
        assert api._customer_safe_message(cls, "沙發緊貼大門門框擺放") is None, (
            f"{cls} 仍會原文放行——那是洩漏面不是功能")


def test_judge_description_still_reaches_the_customer_elsewhere():
    """反貧血：客服要看的判官敘述沒有消失，只是不走 exception_message。"""
    v = {"ok": False, "hard_fail": True, "reason": "沙發緊貼大門門框擺放"}
    event = api._record_validation_attempt(
        {"style": "modern", "room_type": "living"},
        job_id="J", stage="post_render", attempt=1, validation=v)
    assert event["raw_verdict"]["reason"] == "沙發緊貼大門門框擺放"


def test_whitelist_logic_is_not_duplicated():
    """🔴 白名單曾經寫成兩份（上面一個 if、下面再一個同條件判斷），
    後半永遠不可達（GPT 2026-08-07 抓到）。只能有一條。"""
    import inspect
    src = inspect.getsource(api._incomplete_message)
    assert src.count("_MODELLING_FAILURE_SIGNALS") == 3, (
        "白名單不是單一路徑（應為 1 次定義 + 2 次比對）")
    assert src.count("s2_preflight_blocked") == 1, "前檢訊號被寫了不只一處"
    assert src.count("s2_blocked_legacy") == 1, "legacy 封鎖訊號被寫了不只一處"


def test_frontend_reshoot_is_a_single_path():
    """前端同樣不得有 _preflight 與 _modellingFailed 兩套等價判斷。"""
    m = re.search(r"var _isReshoot = (.*?);", HTML, re.S)
    assert m, "找不到 _isReshoot"
    assert m.group(1).count("||") == 0, (
        f"_isReshoot 仍是多路 or：{m.group(1)}——兩套等價判斷只留一套")

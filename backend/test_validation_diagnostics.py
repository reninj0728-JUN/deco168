import json
from pathlib import Path

import api


def test_validation_attempt_records_pass_and_structured_log(capsys):
    render = {"style": "modern", "room_type": "living"}

    event = api._record_validation_attempt(
        render,
        job_id="TEST1234",
        stage="post_render",
        attempt=1,
        validation={"ok": True, "hard_fail": False},
    )

    assert event["ok"] is True
    assert event["hard_fail"] is False
    assert event["failure_class"] is None
    assert event["raw_verdict"] == {"ok": True, "hard_fail": False}
    assert render["validation_history"] == [event]
    line = capsys.readouterr().out.strip()
    assert line.startswith("[validation] ")
    payload = json.loads(line.removeprefix("[validation] "))
    assert payload["event"] == "render_validation_attempt"
    assert payload["job_id"] == "TEST1234"
    assert payload["validation_stage"] == "post_render"


def test_validation_attempt_preserves_exception_type_and_message(capsys):
    render = {"style": "nordic", "room_type": "living"}
    error = TimeoutError("Gemini judge timed out")

    event = api._record_validation_attempt(
        render,
        job_id="TEST5678",
        stage="z3",
        attempt=2,
        validation={"ok": None},
        error=error,
    )

    assert event["ok"] is None
    assert event["failure_class"] == "infrastructure"
    assert event["exception_type"] == "TimeoutError"
    assert event["exception_message"] == "Gemini judge timed out"
    assert '"exception_type": "TimeoutError"' in capsys.readouterr().out


def test_validation_attempt_history_appends_without_changing_verdict(capsys):
    render = {"validation_history": [{"validation_stage": "post_render", "attempt": 1}]}

    api._record_validation_attempt(
        render,
        job_id="TEST9012",
        stage="phase2",
        attempt=1,
        validation={"ok": False, "hard_fail": True},
    )

    assert len(render["validation_history"]) == 2
    assert render["validation_history"][-1]["validation_stage"] == "phase2"
    assert render["validation_history"][-1]["hard_fail"] is True
    assert render["validation_history"][-1]["failure_class"] == "render_quality"
    capsys.readouterr()


def test_validation_diagnostics_exposes_history_and_final_exception():
    render = {
        "validation_history": [
            {"validation_stage": "post_render", "attempt": 1, "ok": None},
            {"validation_stage": "phase3", "attempt": 1, "ok": None},
        ],
        "validation": {
            "ok": False,
            "hard_fail": True,
            "validation_unavailable": True,
            "exception_type": "ValueError",
            "error": "invalid Gemini response",
        },
    }

    diagnostic = api._validation_diagnostics(render)

    assert diagnostic["validation_stage"] == "phase3"
    assert diagnostic["validation_attempt_count"] == 2
    assert diagnostic["failure_class"] is None
    assert diagnostic["validation_final"] == {
        "ok": False,
        "hard_fail": True,
        "validation_unavailable": True,
        "validation_outage": None,
        "exception_type": "ValueError",
        "exception_message": "invalid Gemini response",
    }


def test_pipeline_hooks_all_post_render_validation_stages_and_trimmed_payloads():
    source = Path(api.__file__).read_text(encoding="utf-8")

    for stage in ("post_render", "z3", "phase2", "phase3"):
        assert f'stage="{stage}"' in source
    assert "**_validation_diagnostics(r)" in source
    # 完整版帶全份證據；精簡版與極簡版帶摘要（見 _slim_validation_summary 的體積理由）
    assert source.count('"validation_summary": validation_summary') >= 1
    assert source.count('"validation_summary": _slim_validation_summary(validation_summary)') == 2


def test_fail_closed_policy_is_unchanged_while_exception_is_retained():
    validation = api._fail_closed_validation(
        {"ok": None, "error": "judge unavailable", "exception_type": "RuntimeError"},
        "living",
    )

    assert validation["ok"] is False
    assert validation["hard_fail"] is True
    assert validation["validation_unavailable"] is True
    assert validation["exception_type"] == "RuntimeError"
    assert validation["error"] == "judge unavailable"


def test_render_quality_keeps_reason_flags_and_measurements(capsys):
    render = {"style": "modern", "room_type": "living"}
    verdict = {
        "ok": False,
        "hard_fail": True,
        "reason": "電視櫃阻擋門口",
        "furniture_blocks_walkway": True,
        "door_clearance_ratio": 0.18,
    }

    event = api._record_validation_attempt(
        render, job_id="QUALITY1", stage="post_render", attempt=1,
        validation=verdict,
    )

    assert event["failure_class"] == "render_quality"
    assert event["raw_verdict"] == verdict
    capsys.readouterr()


def test_quota_is_classified_as_infrastructure_on_any_validation_stage(capsys):
    render = {"style": "modern", "room_type": "living"}

    event = api._record_validation_attempt(
        render, job_id="QUOTA1", stage="phase3", attempt=1,
        validation={"ok": None, "error": "429 RESOURCE_EXHAUSTED"},
    )

    assert event["failure_class"] == "infrastructure"
    assert event["raw_verdict"]["error"] == "429 RESOURCE_EXHAUSTED"
    capsys.readouterr()


def test_generation_timeout_is_infrastructure_even_without_validation_history():
    diagnostic = api._validation_diagnostics({
        "error_type": "FalGenerationTimeout",
        "error": "fal request exceeded 180 seconds",
        "validation": {"ok": False, "hard_fail": True},
    })

    assert diagnostic["failure_class"] == "infrastructure"
    assert diagnostic["validation_final"]["exception_type"] == "FalGenerationTimeout"
    assert diagnostic["validation_final"]["exception_message"] == "fal request exceeded 180 seconds"


def _worst_case_summary():
    """3 視角 × 6 次全掛——重試最多的單，診斷 payload 也最肥。"""
    verdict = {
        "ok": False, "hard_fail": True, "reason": "沙" * 120,
        "render_bboxes": {"sofa": [513, 592, 866, 917],
                          "focal_anchor": [559, 83, 874, 329], "entrance_door": None},
        **{f"flag_{i}": False for i in range(30)},
    }
    dropped = []
    for i in range(3):
        render = {"style": "modern", "room_type": "living"}
        for attempt, stage in enumerate(
                ("post_render", "post_render", "z3", "z3", "phase2", "phase3"), start=1):
            api._record_validation_attempt(render, job_id="SIZE", stage=stage,
                                           attempt=attempt, validation=dict(verdict))
        render["validation"] = dict(verdict)
        dropped.append({"style": "modern", "angle_label": f"視角{i}", "room_type": "living",
                        "timeout": False, "reason": "沙" * 80,
                        **api._validation_diagnostics(render)})
    return {"total": 3, "ok": 0, "ng": 3, "retry_count": 6, "dropped_renders": dropped}


def test_trimmed_payload_summary_stays_small_but_keeps_the_cause():
    """精簡 payload 存在的唯一理由是「小到一定寫得進去」（ED3B66EF 卡 result_upsert）。
    完整 validation_history 每筆都帶整份判官輸出，會讓救命退路自己寫不進去。"""
    full = _worst_case_summary()
    slim = api._slim_validation_summary(full)

    full_bytes = len(json.dumps(full, ensure_ascii=False).encode())
    slim_bytes = len(json.dumps(slim, ensure_ascii=False).encode())
    assert full_bytes > 20_000, "測試素材需代表最壞情況"
    assert slim_bytes < 8_000, f"精簡版仍過大: {slim_bytes} bytes"
    assert slim_bytes < full_bytes / 3

    # 縮小不能犧牲「查得出死在哪層、為什麼」
    d = slim["dropped_renders"][0]
    assert d["failure_class"] == "render_quality"
    assert d["validation_stage"] == "phase3"
    assert d["validation_attempt_count"] == 6
    assert [t["validation_stage"] for t in d["validation_trail"]] == [
        "post_render", "post_render", "z3", "z3", "phase2", "phase3"]
    assert d["validation_final"]["hard_fail"] is True
    # raw_verdict（含 render_bboxes）不得進精簡 payload
    assert "raw_verdict" not in json.dumps(slim, ensure_ascii=False)
    assert "render_bboxes" not in json.dumps(slim, ensure_ascii=False)


def test_full_payload_keeps_complete_evidence():
    """完整版必須保留 raw_verdict——證據鏈不能兩層都砍掉。"""
    full = _worst_case_summary()
    assert "raw_verdict" in json.dumps(full, ensure_ascii=False)
    assert len(full["dropped_renders"][0]["validation_history"]) == 6


def test_diagnostics_failure_never_changes_the_verdict():
    """診斷 bug 不得偽裝成驗證失敗：呼叫點多在 try 內，診斷噴錯會被誤記。"""
    class NotAMapping:
        """真實情境：上游塞了非 dict 的驗證結果，診斷 dict() 會炸。"""
        def __bool__(self):
            return True

    render = {"style": "modern"}
    event = api._record_validation_attempt(
        render, job_id="X", stage="post_render", attempt=1, validation=NotAMapping())
    assert event == {}                      # 靜默失敗
    assert "validation_history" not in render or not render["validation_history"]


def test_trimmed_payload_drops_shadow_contract_items():
    """shadow 契約明細是純觀測大物件，精簡版只留件數。"""
    slim = api._slim_validation_summary({
        "total": 1, "ok": 1,
        "layout_contract_shadow": {"count": 2, "affects_delivery": False,
                                   "items": [{"blob": "x" * 5000}] * 2},
    })
    assert slim["layout_contract_shadow"]["items_trimmed"] is True
    assert "items" not in slim["layout_contract_shadow"]
    assert len(json.dumps(slim, ensure_ascii=False)) < 500


def test_fal_balance_exhaustion_is_not_treated_as_transient():
    """2026-07-19：餘額耗盡的錯誤字串跟 1164DFC6 的瞬時假鎖同樣以 locked 開頭，
    走了退避重試那條路，四張真單全滅還被記成「配置驗收失敗」。"""
    import test_full_pipeline as tfp

    hard = [
        "User is locked. Reason: Exhausted balance. Top up your balance.",
        "402 Payment Required",
        "Insufficient balance for this request",
        "Account suspended",
        "billing issue: card declined",
    ]
    for msg in hard:
        assert tfp._is_fal_account_blocked(Exception(msg)), f"應判硬失敗: {msg}"

    # 真的瞬時抖動仍必須重試（否則會退回 1164DFC6 那次白吃失敗）
    transient = [
        "User is locked",
        "Internal Server Error",
        "Service Unavailable",
        "Too Many Requests",
    ]
    for msg in transient:
        assert not tfp._is_fal_account_blocked(Exception(msg)), f"不該判硬失敗: {msg}"


def test_incomplete_message_matches_the_real_cause():
    """沒有圖可驗時絕不能對客戶說「驗收沒過」——那是把系統問題說成設計問題。"""
    system = {"dropped_renders": [
        {"failure_class": "infrastructure"}, {"failure_class": "validator_exception"}]}
    assert "系統" in api._incomplete_message(system)
    assert "配置驗收" not in api._incomplete_message(system)

    quality = {"dropped_renders": [{"failure_class": "render_quality"}]}
    assert "配置驗收" in api._incomplete_message(quality)

    # 混合（有真的爛圖）→ 仍講配置，不可拿系統問題當藉口掩蓋品質問題
    mixed = {"dropped_renders": [
        {"failure_class": "infrastructure"}, {"failure_class": "render_quality"}]}
    assert "配置驗收" in api._incomplete_message(mixed)

    # 沒有資料時維持原文案，不亂改
    assert "配置驗收" in api._incomplete_message({})
    assert "配置驗收" in api._incomplete_message(None)


def test_incomplete_message_tells_the_customer_to_reshoot_when_angle_is_unmodellable():
    """3135DE37｜斜角方正房 S2 建模不了 → 回退 legacy。這種單再重跑幾次都一樣，
    唯一有效的動作是正面重拍。文案不講清楚，客服和客戶只會一直重跑。"""
    waived = {"dropped_renders": [
        {"failure_class": "render_quality", "layout_mode": "legacy_fallback"}]}
    msg = api._incomplete_message(waived)
    assert "正面" in msg and "重拍" in msg
    assert "配置驗收" not in msg

    # S2 正常路徑的品質失敗 → 仍講配置驗收，不可叫客戶重拍
    s2 = {"dropped_renders": [
        {"failure_class": "render_quality", "layout_mode": "s2_contract"}]}
    assert "配置驗收" in api._incomplete_message(s2)
    assert "重拍" not in api._incomplete_message(s2)

    # 系統問題仍優先講系統（但 legacy_fallback 更具體，排在前面）
    infra = {"dropped_renders": [
        {"failure_class": "infrastructure", "layout_mode": "s2_contract"}]}
    assert "系統" in api._incomplete_message(infra)

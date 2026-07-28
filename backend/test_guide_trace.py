# -*- coding: utf-8 -*-
"""guide 觀測欄位的契約測試。

背景：失敗的客廳走 dropped_renders，那裡不存 reference_map，導致
「guide 有沒有真的送進生成請求」在失敗樣本上完全量不到。這組欄位補上這塊。

這些測試要釘死四件事：
  1. 逐次 attempt 記錄，不是訂單總結；
  2. attached 是在呼叫生成 API 的當下量的（參考圖被濾掉時要變 False）；
  3. 不存本機暫存路徑、不存 data URL／簽名網址／金鑰；
  4. 純診斷——交付／驗證／重試邏輯一律不得讀它。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import api
import test_full_pipeline as tfp


# ── 1. 逐次 attempt ────────────────────────────────────────────────
def test_attach_recorded_per_attempt_not_summarised():
    render = {"_layout_mode": "s2_contract"}
    for i in range(1, 4):
        tfp.record_guide_attach(render, stage="z3", attempt=i,
                                image_urls=["room", "guide"], guide_url="guide")
    trace = render["_guide_trace"]
    assert len(trace) == 3, "每次 attempt 都要留一列，不能只存最後一次"
    assert [row["attempt"] for row in trace] == [1, 2, 3]


def test_trace_is_capped_so_it_cannot_grow_unbounded():
    render = {}
    for i in range(40):
        tfp.record_guide_attach(render, stage="phase3", attempt=i,
                                image_urls=[], guide_url=None)
    assert len(render["_guide_trace"]) == 12
    # 保留最新的，不是最舊的
    assert render["_guide_trace"][-1]["attempt"] == 39


# ── 2. attached 必須在呼叫當下量 ───────────────────────────────────
def test_attached_true_only_when_guide_actually_in_image_urls():
    render = {}
    tfp.record_guide_attach(render, stage="initial", attempt=0,
                            image_urls=["room", "sofa", "guide"], guide_url="guide")
    assert render["_guide_trace"][0]["attached_to_generation_request"] is True


def test_attached_false_when_bad_reference_retry_strips_the_guide():
    """fal 抓不到參考圖時會重組 image_urls；guide 被濾掉就必須記成 False。
    這正是「不能事後推測」的情境——render 上仍掛著 guide，但請求裡沒有。"""
    render = {"_layout_mode": "legacy_fallback"}
    tfp.record_guide_attach(render, stage="initial", attempt=0,
                            image_urls=["room", "guide"], guide_url="guide")
    tfp.record_guide_attach(render, stage="initial", attempt=0,
                            image_urls=["room"], guide_url="guide")   # 重試後被濾掉
    rows = render["_guide_trace"]
    assert rows[0]["attached_to_generation_request"] is True
    assert rows[1]["attached_to_generation_request"] is False
    assert rows[1]["guide_created"] is True, "guide 有生成，只是沒送進去——兩件事要分得開"


def test_attached_false_when_no_guide_exists():
    render = {}
    tfp.record_guide_attach(render, stage="initial", attempt=0,
                            image_urls=["room"], guide_url=None)
    row = render["_guide_trace"][0]
    assert row["guide_created"] is False
    assert row["attached_to_generation_request"] is False


def test_recorder_never_breaks_generation():
    """觀測掛掉不可以害到生成。"""
    tfp.record_guide_attach(None, stage="x", attempt=0, image_urls=[], guide_url=None)
    bad = {"_guide_trace": "not-a-list"}
    tfp.record_guide_attach(bad, stage="x", attempt=0, image_urls=None, guide_url=None)
    assert isinstance(bad["_guide_trace"], list)


# ── 3. 不存本機路徑／不存機敏內容 ──────────────────────────────────
def test_record_stores_hash_not_local_path(tmp_path):
    guide = tmp_path / "layout_guide_0.png"
    guide.write_bytes(b"fake-guide-bytes")
    photo = tmp_path / "photo_01.jpg"
    photo.write_bytes(b"fake-photo-bytes")

    rec = api.guide_trace_record(
        stage="plan", attempt=0, layout_mode="s2_contract",
        guide_path=str(guide), original_source_path=str(photo),
        original_source_key="uploads/ABC/photo_01.jpg",
        guide_canvas_path=str(photo),
        coordinate_space="uncropped_source", skip_reason=None,
    )
    blob = json.dumps(rec, ensure_ascii=False)
    assert str(tmp_path) not in blob, "不可存本機暫存路徑（Railway 重啟後無意義）"
    assert rec["guide_sha256"] and len(rec["guide_sha256"]) == 64
    assert rec["original_source_sha256"] and len(rec["original_source_sha256"]) == 64
    assert rec["guide_basename"] == "layout_guide_0.png"
    assert rec["original_source_key"] == "uploads/ABC/photo_01.jpg"


def test_original_and_canvas_identity_stay_separate(tmp_path):
    """裁切/zoom 後 guide 畫在別張影像上；key 與 hash 混成一組就答不出
    「guide 是不是畫在對的原圖上」。"""
    original = tmp_path / "photo_01.jpg"
    original.write_bytes(b"ORIGINAL-uncropped")
    canvas = tmp_path / "living_zoom_0.jpg"
    canvas.write_bytes(b"CROPPED-zoom-canvas")

    rec = api.guide_trace_record(
        stage="plan", attempt=0, layout_mode="legacy_fallback",
        guide_path=None,
        original_source_path=str(original),
        original_source_key="uploads/ABC/photo_01.jpg",
        guide_canvas_path=str(canvas),
        coordinate_space="living_zone_zoom_crop",
    )
    assert rec["original_source_sha256"] != rec["guide_canvas_sha256"], (
        "裁切後兩者必須不同；相同代表又把身分混在一起了")
    assert rec["original_source_key"] == "uploads/ABC/photo_01.jpg"
    assert rec["coordinate_space"] == "living_zone_zoom_crop"


def test_guide_created_but_unreadable_is_distinguishable(tmp_path):
    """路徑有值不代表檔案在；讀不到的 guide 等於沒有，不可看起來像有。"""
    rec = api.guide_trace_record(
        stage="plan", attempt=0, layout_mode="legacy",
        guide_path=str(tmp_path / "missing_guide.png"),
    )
    assert rec["guide_created"] is True
    assert rec["guide_artifact_readable"] is False
    assert rec["guide_sha256"] is None


def test_record_contains_no_urls_or_secrets(tmp_path):
    guide = tmp_path / "g.png"
    guide.write_bytes(b"x")
    rec = api.guide_trace_record(
        stage="plan", attempt=0, layout_mode="legacy",
        guide_path=str(guide), original_source_path=None,
        original_source_key=None, coordinate_space="cropped_source",
        skip_reason=None,
    )
    blob = json.dumps(rec).lower()
    for banned in ("data:", "http://", "https://", "bearer", "apikey", "token"):
        assert banned not in blob, f"診斷欄位不可帶 {banned}"


def test_record_survives_unreadable_files():
    rec = api.guide_trace_record(
        stage="plan", attempt=0, layout_mode=None,
        guide_path="/does/not/exist.png", original_source_path="/nope.jpg",
        guide_canvas_path="/also-nope.jpg",
        original_source_key=None, coordinate_space=None, skip_reason=None,
    )
    assert rec["guide_sha256"] is None
    assert rec["original_source_sha256"] is None
    assert rec["guide_canvas_sha256"] is None


# ── 4. 必填欄位 ────────────────────────────────────────────────────
REQUIRED = {"stage", "attempt", "layout_mode", "guide_created", "skip_reason",
            "attached_to_generation_request"}


def test_plan_record_has_required_fields():
    rec = api.guide_trace_record(
        stage="plan", attempt=0, layout_mode="s2_blocked_legacy",
        guide_path=None, skip_reason="conservative_no_binding",
    )
    assert REQUIRED <= set(rec)
    assert rec["skip_reason"] == "conservative_no_binding"


def test_generation_record_has_required_fields():
    render = {"_layout_mode": "legacy_fallback"}
    tfp.record_guide_attach(render, stage="phase2", attempt=2,
                            image_urls=["a"], guide_url=None)
    assert REQUIRED <= set(render["_guide_trace"][0])


# ── 5. dropped render 真的帶得走完整 trace（GPT 指定的驗收）─────────
def _dropped_entry(render: dict) -> dict:
    """複製 api.py 組 dropped_renders 那一段的形狀。"""
    return {
        "style": render.get("style"),
        "room_type": render.get("room_type"),
        "reason": "沙發視線正對大門",
        "layout_mode": render.get("_layout_mode") or "legacy",
        **api._validation_diagnostics(render),
    }


def test_dropped_render_preserves_full_guide_trace():
    render = {
        "style": "modern",
        "room_type": "living",
        "_layout_mode": "s2_contract",
        "validation": {"ok": False, "hard_fail": True},
        "validation_history": [{"validation_stage": "z3", "failure_class": "render_quality"}],
    }
    api.append_guide_trace(render, api.guide_trace_record(
        stage="plan", attempt=0, layout_mode="s2_contract",
        guide_path=None, skip_reason="zone_crop_no_verified_transform",
    ))
    tfp.record_guide_attach(render, stage="initial", attempt=0,
                            image_urls=["room", "guide"], guide_url="guide")
    tfp.record_guide_attach(render, stage="z3", attempt=1,
                            image_urls=["room"], guide_url="guide")

    entry = _dropped_entry(render)
    trace = entry["guide_trace"]
    assert len(trace) == 3, "規劃 + 兩次生成都要留下來"
    assert trace[0]["skip_reason"] == "zone_crop_no_verified_transform"
    assert [r["stage"] for r in trace] == ["plan", "initial", "z3"]
    assert trace[1]["attached_to_generation_request"] is True
    assert trace[2]["attached_to_generation_request"] is False
    # 必須能原樣寫進 result_json
    json.dumps(entry, ensure_ascii=False)


def test_dropped_render_without_trace_is_empty_list_not_missing():
    entry = _dropped_entry({"room_type": "living", "validation": {"ok": False}})
    assert entry["guide_trace"] == []


# ── 7. Phase3 收尾必須把 guide_trace 一起同步回去 ──────────────────
def test_phase3_merge_syncs_guide_trace_not_just_history():
    """dropped_renders 在 Phase3 之前就寫好；Phase3 之後才產生的 guide 使用
    紀錄若沒同步，資料庫就永遠看不到 Phase3——而 incomplete 收尾走的正是這條。"""
    dropped = _dropped_entry({
        "style": "modern", "room_type": "living", "_layout_mode": "s2_contract",
        "validation": {"ok": False},
        "validation_history": [{"validation_stage": "z3"}],
        "_guide_trace": [{"stage": "plan", "attempt": 0}],
    })
    assert len(dropped["guide_trace"]) == 1

    # Phase3 又跑了一次生成，render 上多出新的紀錄
    final_render = {
        "style": "modern", "room_type": "living",
        "validation_history": [{"validation_stage": "z3"},
                               {"validation_stage": "phase3"}],
        "_guide_trace": [{"stage": "plan", "attempt": 0},
                         {"stage": "phase3", "attempt": 1,
                          "attached_to_generation_request": False}],
    }
    api.merge_dropped_render_diagnostics(dropped, final_render)

    assert dropped["validation_stage"] == "phase3"
    assert len(dropped["guide_trace"]) == 2, "Phase3 的 guide 紀錄沒被同步回去"
    assert dropped["guide_trace"][-1]["stage"] == "phase3"
    assert dropped["guide_trace"][-1]["attached_to_generation_request"] is False


def test_phase3_merge_does_not_wipe_existing_trace():
    """Phase3 的 render 若是新 dict、身上沒有 trace，不可把既有紀錄洗掉。"""
    dropped = {"guide_trace": [{"stage": "plan"}, {"stage": "initial"}]}
    api.merge_dropped_render_diagnostics(dropped, {"validation_history": []})
    assert len(dropped["guide_trace"]) == 2


def test_phase3_merge_keeps_non_diagnostic_fields():
    dropped = {"style": "modern", "room_type": "living",
               "reason": "沙發視線正對大門", "blocked_render_url": "kept"}
    api.merge_dropped_render_diagnostics(dropped, {"validation_history": []})
    assert dropped["reason"] == "沙發視線正對大門"
    assert dropped["blocked_render_url"] == "kept"
    assert dropped["style"] == "modern"


def test_phase3_finalizer_actually_calls_the_merge_helper():
    """證明收尾點真的接上了共用 helper，而不是又各搬各的。"""
    tree = ast.parse(Path(api.__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "merge_dropped_render_diagnostics"]
    assert calls, "merge_dropped_render_diagnostics 沒有被任何地方呼叫"


def test_no_second_divergent_merge_path_remains():
    """不准再有第二條「手動把 validation_history 塞回 dropped 條目」的路徑——
    那正是這次漏掉 guide_trace 的原因。"""
    src = Path(api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "merge_dropped_render_diagnostics")
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if helper.lineno <= node.lineno <= helper.end_lineno:
            continue
        for tgt in node.targets:
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and "dropped" in tgt.value.id.lower()
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value in ("validation_history", "guide_trace",
                                            "validation_attempt_count")):
                offenders.append(f"api.py:{node.lineno}")
    assert offenders == [], f"又出現分岔的 merge 路徑：{offenders}"


# ── 6. 純診斷：交付／驗證／重試不得讀它 ────────────────────────────
_TRACE_HELPERS = {"guide_trace_record", "append_guide_trace",
                  "_validation_diagnostics", "record_guide_attach"}


def _reads_outside_helpers(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helper_spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _TRACE_HELPERS:
            helper_spans.append((node.lineno, node.end_lineno))

    def inside_helper(lineno: int) -> bool:
        return any(a <= lineno <= b for a, b in helper_spans)

    hits = []
    for node in ast.walk(tree):
        # 只看「判斷式本身」，不看 body——在 if 內寫入是允許的，拿它當條件才不行
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            checked = [node.test]
        elif isinstance(node, (ast.BoolOp, ast.Compare)):
            checked = [node]
        else:
            continue
        for expr in checked:
            if "guide_trace" in ast.dump(expr) and not inside_helper(node.lineno):
                hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_the_detector_actually_has_teeth(tmp_path):
    """先證明這個偵測器抓得到違規，否則上面那條測試可能只是空過。"""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def deliver(r):\n"
        "    if r.get('_guide_trace'):\n"
        "        return None\n"
        "    return r\n",
        encoding="utf-8")
    assert _reads_outside_helpers(bad), "偵測器抓不到明顯違規，這條契約等於沒接上"

    ok = tmp_path / "ok.py"
    ok.write_text(
        "def deliver(r):\n"
        "    if r.get('room_type') == 'living':\n"
        "        r.setdefault('_guide_trace', []).append({})\n"
        "    return r\n",
        encoding="utf-8")
    assert _reads_outside_helpers(ok) == [], "在 if 內寫入不算讀取，不該誤報"


def test_guide_trace_is_write_only_outside_its_helpers():
    """任何交付／驗證／重試判斷都不准讀 guide_trace——它只能是事後診斷。"""
    backend = Path(api.__file__).parent
    offenders = (_reads_outside_helpers(backend / "api.py")
                 + _reads_outside_helpers(backend / "test_full_pipeline.py"))
    assert offenders == [], f"guide_trace 被拿去做判斷了：{offenders}"

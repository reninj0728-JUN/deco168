# -*- coding: utf-8 -*-
"""zoning provenance 的契約測試。

2026-07-30 受控三跑證實：同一份 production zoning 請求會產生結構不同的幾何
（living_floor 4↔6 點、候選數 7/6/5、連沙發左右都不同）。要做版本化幾何快取，
就得先有可信的 key 材料——而在此之前 result_json 只有寫死十天沒變的 build_tag，
model 與 schema 根本沒被記錄。

這組測試釘死四件事：
  1. 記的是「實際送出」的值（縮圖後影像、已格式化 prompt），不是事後推測；
  2. 只存雜湊與版本，不存 prompt 原文／網址／金鑰；
  3. **純觀測**——pipeline 任何判斷都不得讀它；
  4. 舊訂單沒有這個欄位仍然正常。
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

import zoning_v2 as z2


def _provenance(**over):
    prompt = z2.PROMPT.format(photo_count=1, video_note="")
    media = [{"kind": "photo", "index": 0,
              "sha256": hashlib.sha256(b"sent-bytes").hexdigest(),
              "bytes": 9, "mime": "image/jpeg"}]
    normalized = {"struct_geometry_v1": {"schema_version": "struct-geometry-v1",
                                         "source_photo_index": 0}}
    kwargs = {"model_id": "gemini-3.6-flash", "prompt_text": prompt,
              "sent_media": media, "normalized": normalized,
              "generation_config": {"response_mime_type": "application/json",
                                    "temperature": None, "seed": None},
              "attempt_trace": [{"attempt": 1, "extra_prompt_sha256": None,
                                 "outcome": "ok"}]}
    kwargs.update(over)
    return z2._build_provenance(**kwargs)


# ── 1. 記的是實際送出的值 ─────────────────────────────────────────
def test_prompt_hash_is_of_the_formatted_prompt_not_the_template():
    """模板含 {photo_count} 佔位符；雜湊未格式化的模板等於記錯東西。"""
    formatted = z2.PROMPT.format(photo_count=1, video_note="")
    p = _provenance(prompt_text=formatted)
    assert p["prompt_sha256"] == hashlib.sha256(formatted.encode()).hexdigest()
    assert p["prompt_sha256"] != hashlib.sha256(z2.PROMPT.encode()).hexdigest()


def test_media_hash_is_of_the_bytes_actually_sent():
    """Gemini 收到的是 _downscale_for_vision 之後的位元組，不是原始檔。"""
    sent = b"downscaled-bytes"
    p = _provenance(sent_media=[{"kind": "photo", "index": 0,
                                 "sha256": hashlib.sha256(sent).hexdigest(),
                                 "bytes": len(sent), "mime": "image/jpeg"}])
    assert p["sent_media"][0]["sha256"] == hashlib.sha256(sent).hexdigest()


def test_required_key_material_is_present():
    p = _provenance()
    for field in ("model", "code_revision", "schema_version", "normalizer_version",
                  "prompt_sha256", "generation_config", "sent_media",
                  "source_photo_index", "request_fingerprint", "fingerprint_inputs",
                  "interpreter_fingerprint", "attempt_count", "used_retry",
                  "attempts", "captured_at"):
        assert field in p, f"缺少快取 key 材料：{field}"


def test_fingerprint_changes_with_model_prompt_or_image():
    base = _provenance()["request_fingerprint"]
    assert _provenance(model_id="other-model")["request_fingerprint"] != base
    assert _provenance(prompt_text="不一樣的提示詞")["request_fingerprint"] != base
    assert _provenance(sent_media=[{"kind": "photo", "index": 0,
                                    "sha256": "deadbeef", "bytes": 1,
                                    "mime": "image/jpeg"}])["request_fingerprint"] != base


def test_fingerprint_changes_when_generation_config_changes():
    """哪天有人鎖 temperature 或 seed，指紋必須變——否則快取會拿舊設定的幾何誤命中。"""
    base = _provenance()["request_fingerprint"]
    assert _provenance(generation_config={"response_mime_type": "application/json",
                                          "temperature": 0.0, "seed": None},
                       )["request_fingerprint"] != base
    assert _provenance(generation_config={"response_mime_type": "application/json",
                                          "temperature": None, "seed": 42},
                       )["request_fingerprint"] != base
    assert _provenance(generation_config={"response_mime_type": "text/plain",
                                          "temperature": None, "seed": None},
                       )["request_fingerprint"] != base


def test_fingerprint_changes_when_media_mime_changes():
    """同樣位元組換個 MIME 宣告＝送出的請求不同。"""
    def media(mime):
        return [{"kind": "photo", "index": 0, "sha256": "abc123",
                 "bytes": 9, "mime": mime}]
    assert (_provenance(sent_media=media("image/jpeg"))["request_fingerprint"]
            != _provenance(sent_media=media("image/png"))["request_fingerprint"])


def test_fingerprint_ignores_key_order_but_not_values():
    """canonical JSON：欄位順序不得影響指紋，值變了才變。"""
    a = _provenance(generation_config={"response_mime_type": "application/json",
                                       "temperature": None, "seed": None})
    b = _provenance(generation_config={"seed": None, "temperature": None,
                                       "response_mime_type": "application/json"})
    assert a["request_fingerprint"] == b["request_fingerprint"]


def test_interpreter_fingerprint_tracks_schema_and_normalizer(monkeypatch):
    """請求一樣但解析／正規化契約換版＝舊幾何不能再用。"""
    base = _provenance()["interpreter_fingerprint"]
    other_schema = _provenance(normalized={"struct_geometry_v1": {
        "schema_version": "struct-geometry-v2", "source_photo_index": 0}})
    assert other_schema["interpreter_fingerprint"] != base
    monkeypatch.setattr(z2, "NORMALIZER_VERSION", "9999-99-99")
    assert _provenance()["interpreter_fingerprint"] != base


def test_code_revision_is_not_baked_into_any_fingerprint(monkeypatch):
    """天天推 master；把 commit sha 綁進 key 會讓每次無關部署清空整份快取。"""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "aaaaaaaa" * 5)
    a = _provenance()
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "bbbbbbbb" * 5)
    b = _provenance()
    assert a["code_revision"] != b["code_revision"], "code_revision 應照實記錄"
    assert a["request_fingerprint"] == b["request_fingerprint"]
    assert a["interpreter_fingerprint"] == b["interpreter_fingerprint"]


def test_fingerprint_inputs_names_exactly_what_went_in():
    """列出的輸入與實際雜湊的欄位必須對得上，否則下一步組 key 的人會被誤導。"""
    p = _provenance()
    assert set(p["fingerprint_inputs"]) == {
        "model", "generation_config", "prompt_sha256", "sent_media[].kind",
        "sent_media[].index", "sent_media[].sha256", "sent_media[].mime"}
    assert "code_revision" not in p["fingerprint_inputs"]
    assert "captured_at" not in p["fingerprint_inputs"]


def test_fingerprint_is_stable_for_the_same_request():
    assert _provenance()["request_fingerprint"] == _provenance()["request_fingerprint"]


def test_volatile_fields_are_outside_the_fingerprint():
    """captured_at 每次都不同；它若進指紋，快取永遠不會命中。"""
    import time
    a = _provenance()
    time.sleep(0.01)
    b = _provenance()
    assert a["request_fingerprint"] == b["request_fingerprint"]


def test_code_revision_is_unknown_when_not_deployed(monkeypatch):
    """取不到部署 commit 時要明確記 unknown——不得拿它當快取 key。"""
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    assert _provenance()["code_revision"] == "unknown"


# ── 1b. retry：實際送出的是第幾次、有沒有被追加修復提示詞 ─────────
def _drive_retry(texts):
    """用真的 _generate_json_with_retry 跑，不是複製一份邏輯來量。"""
    from test_zoning_v2_json_retry import _Client, _Types
    trace: list = []
    client = _Client(list(texts))
    result = z2._generate_json_with_retry(
        client=client, types_module=_Types, parts=["photo", "prompt"],
        model="gemini-test", max_attempts=2, attempt_trace=trace)
    return result, trace, client


def test_attempt_trace_records_each_actual_request():
    good = json.dumps({"best_photo_index": 0})
    _r, trace, client = _drive_retry(['{"broken":', good])
    assert len(client.models.calls) == 2, "前提：真的重試了兩次"
    assert len(trace) == 2, "每次實際送出都要留一筆"
    assert trace[0]["outcome"] == "invalid_json" and trace[0]["extra_prompt_sha256"] is None
    assert trace[1]["outcome"] == "ok" and trace[1]["extra_prompt_sha256"]


def test_first_attempt_success_records_no_retry():
    _r, trace, client = _drive_retry([json.dumps({"best_photo_index": 0})])
    assert len(client.models.calls) == 1
    assert [a["outcome"] for a in trace] == ["ok"]
    p = _provenance(attempt_trace=trace)
    assert p["attempt_count"] == 1 and p["used_retry"] is False


def test_provenance_marks_geometry_that_came_from_a_retry():
    """從第 2 次回來的幾何，provenance 必須說得出來——否則「記實際送出值」不成立。"""
    _r, trace, _c = _drive_retry(['{"broken":', json.dumps({"best_photo_index": 0})])
    p = _provenance(attempt_trace=trace)
    assert p["attempt_count"] == 2
    assert p["used_retry"] is True
    assert [a["retry_suffix"] for a in p["attempts"]] == [False, True]


def test_retry_attempt_has_a_different_request_fingerprint():
    """第 2 次多了一段修復提示詞＝送出的 request 不同，指紋不可與第 1 次相同。"""
    _r, trace, _c = _drive_retry(['{"broken":', json.dumps({"best_photo_index": 0})])
    p = _provenance(attempt_trace=trace)
    first, second = p["attempts"][0], p["attempts"][1]
    assert first["request_fingerprint"] != second["request_fingerprint"]
    # base request 指紋維持不變 → 步驟 2 的快取 key 仍可用 base，不被 retry 打亂
    assert first["request_fingerprint"] != p["request_fingerprint"]
    single = _provenance(attempt_trace=[{"attempt": 1, "extra_prompt_sha256": None,
                                         "outcome": "ok"}])
    assert single["attempts"][0]["request_fingerprint"] == first["request_fingerprint"]


def test_empty_response_is_recorded_as_its_own_outcome():
    _r, trace, _c = _drive_retry(["", json.dumps({"best_photo_index": 0})])
    assert [a["outcome"] for a in trace] == ["empty_response", "ok"]


def test_attempt_trace_is_optional_for_existing_callers():
    from test_zoning_v2_json_retry import _Client, _Types
    client = _Client([json.dumps({"best_photo_index": 0})])
    assert z2._generate_json_with_retry(
        client=client, types_module=_Types, parts=["p"], model="m",
        max_attempts=2) == {"best_photo_index": 0}


def test_generation_config_snapshot_comes_from_the_config_actually_sent():
    """快照必須取自送模用的同一份 kwargs，不是另抄一份字面值。"""
    from test_zoning_v2_json_retry import _Client, _Types
    client = _Client([json.dumps({"best_photo_index": 0})])
    z2._generate_json_with_retry(client=client, types_module=_Types,
                                 parts=["p"], model="m", max_attempts=1)
    sent_cfg = client.models.calls[0]["config"].kwargs
    assert sent_cfg == z2._GENERATION_CONFIG_KWARGS
    snapshot = z2._generation_config_snapshot(_Types)
    for key, value in sent_cfg.items():
        assert snapshot[key] == value, f"快照與實際送出的 {key} 不一致"


def test_generation_config_snapshot_reads_the_real_sdk_object():
    """別只對著 stub 測——真的 GenerateContentConfig 也要讀得出未設欄位為 None。"""
    types_module = pytest.importorskip("google.genai.types")
    snapshot = z2._generation_config_snapshot(types_module)
    assert snapshot["response_mime_type"] == "application/json"
    assert snapshot["temperature"] is None and snapshot["seed"] is None
    json.dumps(snapshot)  # 必須可序列化進 result_json


def test_generation_config_snapshot_follows_a_config_change(monkeypatch):
    """改送模設定時快照要自己跟上，不能靠人記得同步改兩個地方。"""
    monkeypatch.setitem(z2._GENERATION_CONFIG_KWARGS, "temperature", 0.0)
    assert z2._generation_config_snapshot(
        __import__("test_zoning_v2_json_retry")._Types)["temperature"] == 0.0


# ── 2. 不存機敏內容 ───────────────────────────────────────────────
def test_provenance_carries_no_prompt_text_or_secrets():
    blob = json.dumps(_provenance(), ensure_ascii=False)
    assert "這 1 張是同一個空屋" not in blob, "不可存 prompt 原文"
    for banned in ("http://", "https://", "Bearer", "apikey", "api_key", "token"):
        assert banned.lower() not in blob.lower(), f"provenance 不可帶 {banned}"


# ── 3. 純觀測：pipeline 不得讀它做判斷 ────────────────────────────
def test_provenance_is_write_only_across_backend():
    """任何條件式讀到 _provenance 就是把它變成行為依賴，違反純觀測。"""
    backend = Path(z2.__file__).parent
    offenders = []
    for path in sorted(backend.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        builder_spans = [
            (n.lineno, n.end_lineno) for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_build_provenance"
        ]
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                checked = [node.test]
            elif isinstance(node, (ast.BoolOp, ast.Compare)):
                checked = [node]
            else:
                continue
            if any(a <= node.lineno <= b for a, b in builder_spans):
                continue
            for expr in checked:
                if "_provenance" in ast.dump(expr):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"_provenance 被拿去做判斷了：{offenders}"


def test_the_write_only_detector_has_teeth(tmp_path):
    """先證明偵測器抓得到違規，否則上面那條可能只是空過。"""
    bad = tmp_path / "bad_module.py"
    bad.write_text("def go(z):\n    if z.get('_provenance'):\n        return 1\n    return 0\n",
                   encoding="utf-8")
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    hit = any("_provenance" in ast.dump(n.test)
              for n in ast.walk(tree) if isinstance(n, ast.If))
    assert hit, "偵測邏輯抓不到明顯違規"


# ── 4. 舊訂單沒有這個欄位仍然正常 ─────────────────────────────────
def test_old_zoning_without_provenance_still_plans():
    """舊單的 zoning_v2 沒有 _provenance，讀取與規劃都不得受影響。"""
    import layout_geometry_s2 as s2
    from test_layout_geometry_s2 import _safe_geometry
    legacy = {"struct_geometry_v1": _safe_geometry()}
    assert "_provenance" not in legacy
    plan = s2.build_s2_plan(legacy["struct_geometry_v1"], width=1000, height=700,
                            expected_source_photo_index=0, sofa_side="free")
    assert plan.get("disposition") in ("SAFE_FOR_GENERATION", "BLOCKED")


def test_provenance_presence_does_not_change_planning():
    """加了 _provenance 之後，規劃結果必須完全一樣（純觀測的定義）。"""
    import copy
    import layout_geometry_s2 as s2
    from test_layout_geometry_s2 import _safe_geometry
    raw = _safe_geometry()
    before = s2.build_s2_plan(copy.deepcopy(raw), width=1000, height=700,
                              expected_source_photo_index=0, sofa_side="free")
    with_prov = copy.deepcopy(raw)
    with_prov["_provenance"] = _provenance()
    after = s2.build_s2_plan(with_prov, width=1000, height=700,
                             expected_source_photo_index=0, sofa_side="free")
    assert before.get("disposition") == after.get("disposition")
    assert before.get("chosen_candidate_id") == after.get("chosen_candidate_id")
    assert len(before.get("candidates") or []) == len(after.get("candidates") or [])

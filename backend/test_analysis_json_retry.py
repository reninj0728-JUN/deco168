# -*- coding: utf-8 -*-
"""空間分析 JSON 解析的防護測試。

9904366C（2026-07-31 19:45）：Gemini 回的 JSON 在第 43 行中段斷裂，
`analyze_image` 只有 `raw_decode`（只救尾端多垃圾），整張單在 pipeline
第 1 步就死成「處理失敗，請聯絡客服」——13 秒、進度 0、零交付。
最近 200 單有 9 張死在同一個訊息。

zoning_v2 早有重試、gemini_analyze 早有寬鬆解析＋修復器，就這個呼叫點沒接。
這組測試釘住：接上了，而且正常單不會多花錢。
"""
from __future__ import annotations

import json

import pytest

import test_full_pipeline as tfp


class _Resp:
    def __init__(self, text):
        self.text = text


class _Models:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self.texts.pop(0))


class _Client:
    def __init__(self, texts):
        self.models = _Models(texts)


class _Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Types:
    GenerateContentConfig = _Config


GOOD = json.dumps({"space_type": "living", "best_photo_index": 0})

# 9904366C 真實死法：物件中段斷掉，後面直接接下一個 key（缺逗號）
MID_BREAK = (
    '{\n  "space_type": "living",\n  "estimated_size": "20坪"\n'
    '  "best_photo_index": 0,\n  "renders": []\n}'
)


def _run(texts, **over):
    client = _Client(texts)
    kwargs = dict(client=client, types_module=_Types, model="gemini-3.5-flash",
                  contents=["photo", "prompt"], system_instruction="sys")
    kwargs.update(over)
    return tfp._generate_analysis_json_with_retry(**kwargs), client


# ── 正常單不受影響 ───────────────────────────────────────────────
def test_good_json_costs_exactly_one_call():
    result, client = _run([GOOD])
    assert result["space_type"] == "living"
    assert len(client.models.calls) == 1, "正常單不得多花一次 Gemini"
    assert "Your previous response" not in str(client.models.calls[0]["contents"])


def test_config_matches_what_analyze_image_used_before():
    """system_instruction 與 response_mime_type 不可在重構中掉了。"""
    _r, client = _run([GOOD])
    cfg = client.models.calls[0]["config"]
    assert cfg.system_instruction == "sys"
    assert cfg.response_mime_type == "application/json"
    assert client.models.calls[0]["model"] == "gemini-3.5-flash"


# ── 9904366C 的實際死法 ──────────────────────────────────────────
def test_mid_string_break_no_longer_kills_the_order():
    """9904366C 的死法：這張單現在活得下來。

    實測 `_repair_json_text` **修不了「缺逗號」**（它處理的是未跳脫引號／
    尾逗號／截斷），所以這一類會落到重問那一層。重點不是走哪一層，
    是「訂單不再在第 1 步猝死」。
    """
    result, client = _run([MID_BREAK, GOOD])
    assert isinstance(result, dict) and result["space_type"] == "living"
    assert len(client.models.calls) == 2, "修不了就該重問，而不是直接炸掉"


def test_old_raw_decode_alone_could_not_survive_this():
    """證明這個輸入真的會打死舊寫法，否則上面那條測試是空過的。"""
    with pytest.raises(json.JSONDecodeError):
        json.loads(MID_BREAK)
    with pytest.raises(json.JSONDecodeError):
        json.JSONDecoder().raw_decode(MID_BREAK)


# ── 修不了的才重問 ───────────────────────────────────────────────
def test_unrepairable_json_triggers_exactly_one_retry():
    result, client = _run(["這根本不是 JSON，是模型在講話。", GOOD])
    assert result["space_type"] == "living"
    assert len(client.models.calls) == 2
    assert _RETRY in str(client.models.calls[1]["contents"])
    assert _RETRY not in str(client.models.calls[0]["contents"])


def test_empty_response_also_retries():
    result, client = _run(["", GOOD])
    assert result["space_type"] == "living"
    assert len(client.models.calls) == 2


def test_non_object_json_retries():
    result, client = _run(["[1, 2, 3]", GOOD])
    assert result["space_type"] == "living"
    assert len(client.models.calls) == 2


def test_gives_up_after_the_cap_and_raises():
    """兩次都壞 → 丟例外給呼叫端 fail-closed，不得回半殘 dict 裸奔。"""
    client = _Client(["還是不是 JSON", "仍然不是 JSON"])
    with pytest.raises(ValueError):
        tfp._generate_analysis_json_with_retry(
            client=client, types_module=_Types, model="m",
            contents=["p"], system_instruction="sys")
    assert len(client.models.calls) == 2


def test_max_attempts_cannot_exceed_two_calls():
    """max_attempts 是給測試注入的，不是無限重試的後門。"""
    client = _Client(["壞"] * 9)
    with pytest.raises(ValueError):
        tfp._generate_analysis_json_with_retry(
            client=client, types_module=_Types, model="m", contents=["p"],
            system_instruction="sys", max_attempts=99)
    assert len(client.models.calls) == 2, \
        f"max_attempts=99 仍只准問 2 次，實際 {len(client.models.calls)}"


def test_repairable_flaws_still_cost_only_one_call():
    """既有修復器修得動的（markdown fence、尾端多垃圾）不該白花第二次。"""
    for text in ("```json\n" + GOOD + "\n```", GOOD + "\n\n以上就是分析結果。"):
        result, client = _run([text])
        assert result["space_type"] == "living"
        assert len(client.models.calls) == 1, f"{text[:20]} 不該觸發重問"


# ── 接線：analyze_image 真的走這條路 ────────────────────────────
def test_analyze_image_no_longer_parses_json_inline():
    """舊的裸 raw_decode 必須從 analyze_image 消失，否則防護等於沒接上。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(tfp.analyze_image))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(getattr(n.func, "id", None) == "_generate_analysis_json_with_retry"
               for n in calls), "analyze_image 沒有呼叫防護函式"
    assert not any(
        getattr(getattr(n.func, "value", None), "id", None) == "json"
        and getattr(n.func, "attr", None) == "loads" for n in calls
    ), "analyze_image 裡還有裸 json.loads"
    assert not any(getattr(n.func, "attr", None) == "raw_decode" for n in calls), \
        "analyze_image 裡還有裸 raw_decode"


def test_retry_happens_before_the_video_is_deleted():
    """影片用完即刪；重問若排在刪除之後，第二次就沒有影片可看。"""
    import inspect
    source = inspect.getsource(tfp.analyze_image)
    call_at = source.index("_generate_analysis_json_with_retry")
    delete_at = source.index("client.files.delete")
    assert call_at < delete_at, "重問必須排在刪影片之前"


_RETRY = tfp._ANALYSIS_JSON_RETRY_INSTRUCTION

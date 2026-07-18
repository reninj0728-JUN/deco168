# -*- coding: utf-8 -*-
import json

import pytest

import zoning_v2


class _Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Types:
    GenerateContentConfig = _Config


class _Response:
    def __init__(self, text):
        self.text = text


class _Models:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self.texts.pop(0))


class _Client:
    def __init__(self, texts):
        self.models = _Models(texts)


def test_zoning_json_retries_once_after_malformed_response():
    expected = {"best_photo_index": 0, "overall_confidence": "high"}
    client = _Client(['{"best_photo_index": 0', json.dumps(expected)])

    result = zoning_v2._generate_json_with_retry(
        client=client,
        types_module=_Types,
        parts=["photo", "prompt"],
        model="gemini-test",
        max_attempts=2,
    )

    assert result == expected
    assert len(client.models.calls) == 2
    assert "malformed" in client.models.calls[1]["contents"][-1].lower()


def test_zoning_json_stops_after_second_malformed_response():
    client = _Client(['{"bad":', '{"still_bad":'])

    with pytest.raises(json.JSONDecodeError):
        zoning_v2._generate_json_with_retry(
            client=client,
            types_module=_Types,
            parts=["photo", "prompt"],
            model="gemini-test",
            max_attempts=2,
        )

    assert len(client.models.calls) == 2

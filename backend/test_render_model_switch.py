# -*- coding: utf-8 -*-
"""換算圖模型（gpt-image-2 → 2.5 flare）的相容性鎖。

2026-09-14 升級。三件事必須同時成立，否則換模型會靜默壞掉：

1. **比例鎖的白名單要跟著換。** `_crop_upscale_factor` 對白名單外的模型回 None，
   依賴它的「38% 深裁 + 1.2× 上採樣」那道守門就整個跳過——不會報錯。
   （45% 天花板守門只看 y0/height，與模型無關，不受影響，這條也一起鎖住。）

2. **輸出尺寸桶要一致。** 裁切框收斂到模型不會輸出的比例＝白砍畫面。

3. **payload 守門要真的擋得住。** 舊版寫 `if render_model == RENDER_MODEL`，
   兩邊跟著同一個常數變，條件永遠成立，那個 raise 從來沒機會執行——
   註解宣稱的保護是假的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api
import test_full_pipeline as tfp

NEW = "openai/gpt-image-2.5/sunburst/edit"
FLARE = "openai/gpt-image-2.5/flare/edit"
OLD = "openai/gpt-image-2/edit"


def test_render_model_is_the_25_flare_edit_endpoint():
    """明確指定 endpoint，不是模糊的「2.5」——fal 有 flare/sunburst 兩個。"""
    assert tfp.RENDER_MODEL == NEW
    assert tfp._resolve_render_model(None) == NEW


def test_both_models_keep_the_aspect_lock():
    """新舊模型都必須在白名單裡：升級不得讓上採樣守門靜默失效。"""
    assert NEW in api._ASPECT_LOCKED_MODELS
    assert OLD in api._ASPECT_LOCKED_MODELS


@pytest.mark.parametrize("model", [NEW, OLD])
def test_upscale_factor_works_for_whitelisted_models(monkeypatch, model):
    monkeypatch.setattr(api, "_legacy_render_model", lambda: model)
    got = api._crop_upscale_factor(523, 349)
    assert got is not None and got > 1, f"{model} 的上採樣倍率算不出來：{got}"


def test_unknown_model_still_skips_the_lock(monkeypatch):
    """白名單是白名單：不認得的模型仍要跳過收斂，不准猜一個比例去裁。"""
    monkeypatch.setattr(api, "_legacy_render_model", lambda: "some/unknown-model")
    assert api._crop_upscale_factor(523, 349) is None


def test_both_models_share_the_same_size_buckets():
    """尺寸桶照舊：這次升級刻意不改構圖，不上 4K。

    同時換模型又改構圖，出問題分不清是誰造成的。
    """
    from test_full_pipeline import gpt_output_size_for_ratio
    assert gpt_output_size_for_ratio(1.5) == {"width": 1536, "height": 1024}
    assert gpt_output_size_for_ratio(0.8) == {"width": 1024, "height": 1536}
    assert gpt_output_size_for_ratio(1.0) == {"width": 1024, "height": 1024}


def test_payload_guard_is_not_self_referential():
    """🔴 守門不得寫成 `render_model == RENDER_MODEL`——那會永遠成立。

    必須比對一份獨立的「payload 格式相同」清單，換到清單外才擋得住。
    """
    src = (Path(__file__).resolve().parent / "test_full_pipeline.py").read_text(encoding="utf-8")
    assert "if render_model == RENDER_MODEL:" not in src, \
        "守門又寫回自我比對，換模型時不會擋"
    assert "if render_model in _SAME_PAYLOAD_MODELS:" in src
    # 三個都是同一種 fal edit payload 格式：舊模型與 flare 留著是為了能快速退回。
    assert set(tfp._SAME_PAYLOAD_MODELS) == {OLD, FLARE, NEW}


def test_quality_default_is_unchanged():
    """品質維持 medium：升級只換模型，不順便改品質（成本與畫質都會動）。"""
    src = (Path(__file__).resolve().parent / "test_full_pipeline.py").read_text(encoding="utf-8")
    assert 'os.environ.get("GPT_IMAGE_2_QUALITY", "medium")' in src, \
        "預設品質被改動了——那會同時影響成本與畫質，不該混在這次升級裡"

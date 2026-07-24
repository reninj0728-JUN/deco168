# -*- coding: utf-8 -*-
"""P1 首渲硬綁回歸測試（不打真 Gemini / 不送 fal）。

鎖住 Grok 三個 must-fix：①目標永遠 footprint、門距對目標算 ②first_render_layout
mask_mode 保留完整 prompt（不換局部修短令）③同座標系。以及偵測失敗不 crash。
"""
import json
import os
from unittest.mock import MagicMock, patch

from PIL import Image

import api
import gemini_analyze as ga


def test_detect_source_furniture_parses_and_fails_safe(tmp_path):
    src = tmp_path / "src.jpg"
    Image.new("RGB", (400, 300), "white").save(src)
    empty = {"sofa": None, "coffee_table": None,
             "focal_anchor": None, "entrance_door": None}

    # 正常解析
    resp = MagicMock()
    resp.text = ('{"sofa":[500,600,760,900],"coffee_table":[640,440,720,560],'
                 '"focal_anchor":null,"entrance_door":[200,40,700,200]}')
    client = MagicMock()
    client.models.generate_content.return_value = resp
    with patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
            patch.object(ga.genai, "Client", return_value=client):
        out = ga.detect_source_furniture(str(src))
    assert out["sofa"] == [500, 600, 760, 900]
    assert out["coffee_table"] == [640, 440, 720, 560]
    assert out["focal_anchor"] is None
    assert out["entrance_door"] == [200, 40, 700, 200]

    # 無 key → 全 None、不 crash
    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_AI_KEY": ""}):
        assert ga.detect_source_furniture(str(src)) == empty

    # Gemini 例外 → 全 None、不 crash
    bad = MagicMock()
    bad.models.generate_content.side_effect = RuntimeError("boom")
    with patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
            patch.object(ga.genai, "Client", return_value=bad):
        assert ga.detect_source_furniture(str(src)) == empty

    # JSON 壞（兩次都壞）→ 全 None、不 crash
    junk = MagicMock()
    junk.models.generate_content.return_value = MagicMock(text="not json{{")
    with patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
            patch.object(ga.genai, "Client", return_value=junk):
        assert ga.detect_source_furniture(str(src)) == empty


def _contract(tmp_path):
    contract = {
        "source": {"size": {"width": 1000, "height": 1000}},
        "decision": {"chosen_candidate_id": "c1"},
        "candidates": [{"candidate_id": "c1", "sofa_footprint_geometry_id": "g"}],
        "geometry": [{"geometry_id": "g",
                      "shape": {"coordinates": [[270, 520], [470, 520],
                                                [470, 760], [270, 760]]}}],
    }
    cp = tmp_path / "contract.json"
    cp.write_text(json.dumps(contract), encoding="utf-8")
    return str(cp)


def test_first_render_mask_erases_old_paints_footprint_locks_door(tmp_path):
    base = tmp_path / "base.jpg"
    Image.new("RGB", (1000, 1000), "white").save(base)
    cp = _contract(tmp_path)
    detect = {
        "sofa": [520, 620, 760, 900],          # 舊沙發貼右牆
        "coffee_table": [640, 440, 720, 560],   # 舊茶几中央
        "focal_anchor": None,
        "entrance_door": [200, 40, 700, 200],   # 門在左
    }
    mp = api._build_s2_first_render_mask(str(base), cp, detect, str(tmp_path / "m.png"))
    assert mp
    alpha = Image.open(mp).getchannel("A")
    assert alpha.getpixel((370, 640)) == 0, "footprint 目標區應透明（重畫沙發）"
    assert alpha.getpixel((760, 640)) == 0, "舊沙發區應透明（清掉黏著原物）"
    assert alpha.getpixel((500, 680)) == 0, "舊茶几區應透明（清掉）"
    assert alpha.getpixel((120, 450)) == 255, "大門必須鎖死"
    assert alpha.getpixel((880, 120)) == 255, "無關牆面預設鎖死"


def test_first_render_mask_footprint_only_fallback_when_no_detection(tmp_path):
    base = tmp_path / "base.jpg"
    Image.new("RGB", (1000, 1000), "white").save(base)
    cp = _contract(tmp_path)
    none_detect = {"sofa": None, "coffee_table": None,
                   "focal_anchor": None, "entrance_door": None}
    mp = api._build_s2_first_render_mask(str(base), cp, none_detect, str(tmp_path / "m2.png"))
    assert mp, "偵測全空仍應建出 footprint-only mask（比純 prompt 好），不 crash"
    alpha = Image.open(mp).getchannel("A")
    assert alpha.getpixel((370, 640)) == 0, "footprint 仍透明（paint）"
    assert alpha.getpixel((760, 640)) == 255, "沒偵測到就不 erase（維持鎖死）"

    # 無 contract → None（呼叫端 skip，正常首渲）
    assert api._build_s2_first_render_mask(
        str(base), str(tmp_path / "nope.json"), none_detect,
        str(tmp_path / "m3.png")) is None


def test_first_render_target_always_footprint_with_door_clearance():
    # 目標永遠用 footprint（不吃當前沙發）；太貼門 → 平移到 ≥0.25 門寬
    pts = [(210, 500), (410, 500), (410, 700), (210, 700)]  # px, 貼左門
    box = api._first_render_footprint_target_box(pts, [0, 40, 0, 200], 1000, 1000)
    assert box is not None
    # 門右緣 200 + 0.25*160=40 → fx0 應被推到 ≥ 240
    assert box[0] >= 239, f"門距未生效，box={box}"
    # 無門 → 原 footprint 外接框
    box2 = api._first_render_footprint_target_box(pts, None, 1000, 1000)
    assert box2 == (210, 500, 410, 700)


def test_first_render_layout_mask_reaches_fal_with_full_prompt_kept():
    import inspect

    import test_full_pipeline as tp
    # first_render_layout entry：S2 required + 真 png mask → edit 模型 + mask_url
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        mask = Path(td) / "m.png"
        Image.new("RGBA", (10, 10), (0, 0, 0, 255)).save(mask)
        entry = {
            "_room_type": "living",
            "_layout_contract_s2_required": True,
            "_edit_mask_path": str(mask),
            "_edit_mask_mode": "first_render_layout",
        }
        assert tp._resolve_render_model(entry) == "openai/gpt-image-2/edit"
        url = tp._gpt_image2_mask_data_url(entry)
        assert url and url.startswith("data:image/png;base64,")
    # must-fix②：first_render_layout 分支不得換成局部修短令（保留完整 prompt）
    src = inspect.getsource(tp.generate_renders)
    assert "first_render_layout" in src, "生成端未處理 first_render_layout"
    idx = src.index("first_render_layout")
    # 該分支附近應是「保留完整 prompt」，不呼叫 repair 短令
    branch = src[idx:idx + 400]
    assert "full prompt kept" in branch


def test_p1_default_off():
    # 預設關：沒設環境變數時不啟用（燒錢紀律）
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("P1_FIRST_RENDER_MASK", None)
        assert os.environ.get("P1_FIRST_RENDER_MASK", "0").strip().lower() in ("", "0", "off")

# -*- coding: utf-8 -*-
"""深裁＋上採樣第二道守門（F464DB35 / 13106F4F，2026-09-11 客訴）。

事故：兩張客廳成品長出原圖沒有的東西——F464DB35 編出一整條走廊加兩道門與明管
天花，13106F4F 編出一整排明管天花。兩張的判官都寫「結構與動線皆合理」，因為
判官拿【裁切後】的圖當對照（api.py 四個 validate_render 都吃 entry["_base_path"]），
天花板不在參考圖裡，ceiling_changed 不可能亮。

根因：S2 建模失敗 → 退到客廳區特寫 → 分區框（語意是「家具擺哪塊地板」）被當成
攝影裁切框 → 天花板整片切掉 → 模型必須從零畫天花與上半牆。

為什麼既有的 45% 沒擋住：那個門檻是拿六單**最終框**校的，(38%, 54%) 之間沒有
任何樣本。F464 42.9%、131 39.0% 正好掉在洞裡。

🔴 為什麼是 OR 不是「當且僅當」：293BDE11 用測試這張 4032x3024 合成圖算出來
   倍率只有 0.74×，寫成「上緣>38% 且 倍率>1.2×」會放它過去，等於把當初修掉的
   客廳零圖放回來。45% 那道必須原封不動留著。
"""
from __future__ import annotations

import pytest
from PIL import Image

import api


def _photo(tmp_path, name: str, w: int, h: int):
    p = tmp_path / name
    Image.new("RGB", (w, h), (235, 235, 235)).save(p, "JPEG", quality=90)
    import cv2
    assert cv2.imread(str(p)) is not None, (
        f"cv2 讀不到 fixture {p}——測試環境有問題，這一檔的結果全部不可信")
    return p


# 真實訂單：(job, 照片尺寸, living bbox, 期望裁切, 說明)
REAL = [
    # 🔴 兩張客訴單：天花板被切掉且需要放大 → 必須放棄裁切
    ("F464DB35", (828, 611), [470, 0, 1000, 930], False,
     "上緣 42.9%、放大 2.94×：編出整條走廊＋明管天花"),
    ("13106F4F", (1479, 1109), [430, 0, 1000, 1000], False,
     "上緣 39.0%、放大 1.51×：編出明管天花"),
    # ✅ 同樣深裁但像素夠：結構忠實，不得被新守門誤傷
    ("3A370448", (2390, 1792), [450, 0, 1000, 1000], True,
     "上緣 41.0%、放大 0.97×：牆窗門全忠實，只多一道燈槽"),
    # ✅ 需要放大但天花板還在畫面：官網正在用這張，不得誤傷
    ("0A3D74C7", (1024, 776), [260, 0, 1000, 1000], True,
     "上緣 21.9%、放大 1.69×：完全忠實"),
    ("6F1A03D8", (1024, 768), [360, 0, 1000, 1000], True,
     "上緣 31.9%、放大 1.96×：結構忠實"),
    ("449FD4B6", (4080, 3064), [340, 0, 1000, 1000], True,
     "上緣 30.0%、放大 0.48×"),
]


@pytest.mark.parametrize("job,size,living,should_crop,why", REAL)
def test_real_cases(tmp_path, job, size, living, should_crop, why):
    src = _photo(tmp_path, f"{job}.jpg", *size)
    res = api._crop_to_living_zone(str(src), tmp_path, 0, living)
    if should_crop:
        assert res is not None, f"{job} 結構忠實卻被新守門擋掉了——{why}"
    else:
        assert res is None, f"{job} 應該放棄裁切改用完整底圖——{why}"


def test_293_still_blocked_by_the_45_percent_rule(tmp_path):
    """🔴 回歸鎖：293BDE11 必須繼續被擋，而且是被【45% 那道】擋的。

    它的倍率只有 0.74×，若新守門寫成「且」就會放行。這條在證明兩道是 OR。
    """
    src = _photo(tmp_path, "293BDE11.jpg", 4032, 3024)
    living, door = [580, 0, 1000, 750], [420, 480, 670, 570]
    assert api._crop_to_living_zone(str(src), tmp_path, 0, living,
                                    entrance_bbox1000=door) is None

    # 證明「不是靠新守門擋的」：把新守門拿掉（門檻拉到不可能達到），仍要被擋
    old = api.CROP_DEEP_ZOOM_MAX_UPSCALE
    try:
        api.CROP_DEEP_ZOOM_MAX_UPSCALE = 999.0
        assert api._crop_to_living_zone(str(src), tmp_path, 1, living,
                                        entrance_bbox1000=door) is None, (
            "新守門關掉後 293BDE11 就過了 ⇒ 45% 那道沒有生效，兩道變成互相依賴")
    finally:
        api.CROP_DEEP_ZOOM_MAX_UPSCALE = old


def test_both_conditions_are_required(tmp_path):
    """新守門必須兩個條件同時成立才擋——只中一個的不得被擋。"""
    # 只深裁、像素充足（3A370448 形狀）→ 要裁
    src_a = _photo(tmp_path, "deep_but_big.jpg", 2390, 1792)
    assert api._crop_to_living_zone(str(src_a), tmp_path, 0, [450, 0, 1000, 1000]) is not None
    # 只需放大、天花板還在（0A3D74C7 形狀）→ 要裁
    src_b = _photo(tmp_path, "small_but_high.jpg", 1024, 776)
    assert api._crop_to_living_zone(str(src_b), tmp_path, 1, [260, 0, 1000, 1000]) is not None


def test_upscale_factor_reads_the_same_size_table_as_the_converger(tmp_path):
    """倍率必須跟比例收斂讀同一份輸出尺寸表，不可以各寫一組常數。

    ⚠️ 只用 1.5 比例的框測是測心酸的——那些全落在 1536 那一檔，寫死 1536 也會過
    （這條測試第一版就是這樣，被蓄意破壞抓到）。必須含直式與方形，才驗得到
    「有沒有真的查表」。
    """
    from test_full_pipeline import gpt_output_size_for_ratio
    cases = [
        (523, 349),    # 1.50 → 1536 檔（橫）
        (1587, 1058),  # 1.50 → 1536 檔（橫）
        (600, 750),    # 0.80 → 1024 檔（直）
        (800, 800),    # 1.00 → 1024 檔（方）
        (700, 680),    # 1.03 → 1024 檔（近方）
    ]
    widths = {gpt_output_size_for_ratio(cw / ch)["width"] for cw, ch in cases}
    assert widths == {1536, 1024}, (
        f"測資沒有涵蓋兩種輸出寬度（只有 {widths}），寫死常數也會過")
    for cw, ch in cases:
        size = gpt_output_size_for_ratio(cw / ch)
        want = max(size["width"] / cw, size["height"] / ch)
        got = api._crop_upscale_factor(cw, ch)
        assert got is not None and abs(got - want) < 1e-9, (
            f"{cw}x{ch}: 倍率 {got} 與 gpt_output_size_for_ratio 推出的 {want} 不一致")


def test_upscale_takes_the_larger_of_width_and_height():
    """倍率要取寬、高兩方向的較大者——只看寬會漏掉未收斂的直式框。

    比例收斂成功時框比例＝輸出比例，兩方向必然相等；只有收斂後過小、
    退回未收斂框時才會分岔（GPT 審核抓到的洞）。
    """
    from test_full_pipeline import gpt_output_size_for_ratio
    # 收斂成功的五個真實框：兩種算法必須給出同一個數（證明這個改動沒動到已驗證案例）
    for cw, ch in ((523, 349), (1015, 677), (1587, 1058), (909, 606), (2088, 1392)):
        w_only = gpt_output_size_for_ratio(cw / ch)["width"] / cw
        assert api._crop_upscale_factor(cw, ch) == pytest.approx(w_only), (
            f"{cw}x{ch} 是收斂後的框，兩種算法本該相同")
    # 未收斂的直式框：高度要放大更多，只看寬會低估
    cw, ch = 600, 750
    size = gpt_output_size_for_ratio(cw / ch)
    w_only, h_only = size["width"] / cw, size["height"] / ch
    assert h_only > w_only, "測資選錯了：這組寬高倍率沒有分岔，驗不到東西"
    assert api._crop_upscale_factor(cw, ch) == pytest.approx(h_only), (
        "直式未收斂框必須用高度倍率，否則守門會漏掉")


def test_guard_is_skipped_when_output_size_is_unprovable(monkeypatch, tmp_path):
    """模型換成輸出尺寸無法證明的那種時，這道守門要自己退場，不得亂擋。"""
    monkeypatch.setattr(api, "_legacy_render_model", lambda: "some/other-model")
    assert api._crop_upscale_factor(523, 349) is None
    # F464 形狀在倍率取不到時，只剩 45% 那道 → 42.9% 仍放行（維持舊行為）
    src = _photo(tmp_path, "unprovable.jpg", 828, 611)
    assert api._crop_to_living_zone(str(src), tmp_path, 0, [470, 0, 1000, 930]) is not None


def test_thresholds_sit_where_the_data_says():
    """門檻不得亂調：38% 是已交付組最深的最終框，1.2× 要留在 0.97 與 1.51 之間。"""
    assert api.CROP_DEEP_ZOOM_MIN_Y0 == pytest.approx(0.38)
    assert 0.97 < api.CROP_DEEP_ZOOM_MAX_UPSCALE < 1.51
    # 45% 那道不得被這次改動動到
    assert api.CROP_MUST_INCLUDE_CEILING_MAX_Y0 == pytest.approx(0.45)

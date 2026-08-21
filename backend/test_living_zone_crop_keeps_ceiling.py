# -*- coding: utf-8 -*-
"""客廳區特寫不得裁到「看不見天花板」的底圖。

293BDE11（2026-08-06）客廳零圖。四張照片天花板全部乾淨無管線，成品卻長出
一整片外露管線 → 判官 `ceiling_changed: true` → hard_fail → 客戶零圖。

根因不是判官誤判，也不是照片拍壞：
  `living_zone.bbox_on_best_photo = [580, 0, 1000, 750]`（y 58%~100%）
  ⚠️ 那個 bbox **沒有畫錯**——它的語意是「家具該擺在地板的哪一塊」，本來就
  畫在地板上。錯的是程式把這個**分區框**直接當成**攝影裁切框**去裁渲染底圖。
  裁出來 765x510、佔原圖 24%，幾乎整片地板，模型只能自己重建天花板。

門檻由真實資料定，不是拍腦袋（全站 6 單走過這條路徑，量的是門排除＋比例
收斂【之後】的最終框）：
    已交付 5 單  y0 = 21% / 32% / 33% / 33% / 38%
    唯一失敗      y0 = 54%
空窗 (38%, 54%)，門檻 45% 落在正中間，五個成功單一個都不會被動到。

⚠️ 不合格時回 `None`，呼叫端 (`if _zoom:`) 自動沿用未裁切的完整底圖。
   **不可以退去 `_crop_region_base`**：它會再呼叫一次 zoning、再拿一份同語意
   的 living_zone bbox 去裁，同一張照片實算 y0≈46%，仍舊看不到天花板——
   多燒一次 Gemini、多一次非決定性，而且修了等於沒修。
"""
from __future__ import annotations

import api
import pytest
from PIL import Image

W, H = 2000, 1500


def _photo(tmp_path, name="room.jpg", w=W, h=H):
    """建 fixture 並**確認 cv2 真的讀得到**。

    🔴 GPT 2026-08-06 抓到的假綠：在中文路徑下 OpenCV 讀不到檔，
    `_crop_to_living_zone` 對每一單都回 `None`——五個已交付案例全部失敗，
    但 293BDE11 因為本來就期待 `None`，**反而通過**。
    「293 回傳 None」單獨不足以證明天花板守門生效，任何前置讀圖失敗都能假綠。
    """
    p = tmp_path / name
    Image.new("RGB", (w, h), (235, 235, 235)).save(p, "JPEG", quality=90)
    import cv2
    assert cv2.imread(str(p)) is not None, (
        f"cv2 讀不到 fixture {p}——測試環境有問題，這一檔的結果全部不可信")
    return p


# 六單的真實 living_zone bbox / entrance bbox（取自 result_json）
REAL_CASES = [
    # (job, living_bbox, door_bbox, 是否已交付)
    ("293BDE11", [580, 0, 1000, 750],  [420, 480, 670, 570], False),
    ("A62AC21A", [250, 0, 980, 1000],  [320, 0, 880, 300],   True),
    ("255CA3D7", [360, 40, 950, 520],  [670, 800, 980, 980], True),
    ("29C70C03", [400, 150, 950, 920], [700, 50, 980, 300],  True),
    ("106993A7", [350, 50, 980, 950],  [320, 120, 860, 300], True),
    ("F8C5AD5F", [370, 30, 980, 520],  [295, 820, 720, 965], True),
]


# ── 守門本身 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("y0,height,ok", [
    (0, 1000, True),
    (380, 1000, True),      # 已交付組最深的 38%
    (450, 1000, True),      # 門檻本身仍放行
    (451, 1000, False),
    (540, 1000, False),     # 293BDE11 的 54%
])
def test_ceiling_guard_threshold(y0, height, ok):
    assert api._crop_keeps_room_structure(y0, height)[0] is ok


def test_guard_gives_a_reason():
    """不合格必須留原因，不得靜默——door_gap 那次守門悄悄消失的教訓。"""
    good, why = api._crop_keeps_room_structure(540, 1000)
    assert good is False and "天花板" in why and "54%" in why


def test_threshold_sits_in_the_measured_gap():
    """門檻必須落在「已交付最深」與「失敗」之間的空窗，不是拍腦袋定的。"""
    assert 0.38 < api.CROP_MUST_INCLUDE_CEILING_MAX_Y0 < 0.54


# ── 真實六單：只有 293BDE11 會被擋 ──────────────────────────────────

@pytest.mark.parametrize("job,living,door,delivered", REAL_CASES)
def test_real_cases(tmp_path, job, living, door, delivered):
    """已交付的五單裁切框不得改變；只有 293BDE11 放棄裁切。

    ⚠️ 用 4:3 的合成圖（與六單真實照片同比例）重現，驗的是 bbox → 最終框
    這條計算，不需要真照片內容。
    """
    src = _photo(tmp_path, f"{job}.jpg", 4032, 3024)
    res = api._crop_to_living_zone(str(src), tmp_path, 0, living, entrance_bbox1000=door)
    if delivered:
        assert res is not None, f"{job} 已交付卻被新守門擋掉了"
        _p, (x0, y0, x1, y1) = res
        assert y0 / 3024 <= api.CROP_MUST_INCLUDE_CEILING_MAX_Y0
    else:
        assert res is None, (
            f"{job} 是地板特寫（最終框 y0=54%），必須放棄裁切改用完整底圖")


def test_293_returns_none_because_of_the_guard_not_something_else(tmp_path, monkeypatch):
    """🔴 假綠回歸鎖：證明 293BDE11 回 `None` 是**天花板守門**造成的。

    把守門暫時關掉（永遠放行），293 必須產出裁切；恢復守門後才回 None。
    只斷言「回 None」的話，讀圖失敗、bbox 格式錯、任何前置例外都能讓它假綠
    ——GPT 2026-08-06 在中文路徑環境實測踩到過。
    """
    _job, living, door, _d = REAL_CASES[0]
    src = _photo(tmp_path, "guard_probe.jpg", 4032, 3024)

    monkeypatch.setattr(api, "_crop_keeps_room_structure", lambda y0, h: (True, ""))
    without_guard = api._crop_to_living_zone(str(src), tmp_path, 1, living,
                                             entrance_bbox1000=door)
    assert without_guard is not None, (
        "關掉守門之後 293 仍然裁不出東西——那代表它回 None 另有原因，"
        "這條回歸測試證明不了守門有生效")
    _p, (_x0, y0, _x1, _y1) = without_guard
    assert y0 / 3024 > api.CROP_MUST_INCLUDE_CEILING_MAX_Y0, (
        "293 的最終框其實沒超過門檻，fixture 前提不成立")

    monkeypatch.undo()
    assert api._crop_to_living_zone(str(src), tmp_path, 2, living,
                                    entrance_bbox1000=door) is None


def test_all_delivered_cases_still_crop(tmp_path):
    """整體回歸：五個成功單全部仍然會裁切，一個都不能少。"""
    kept = []
    for job, living, door, delivered in REAL_CASES:
        if not delivered:
            continue
        src = _photo(tmp_path, f"k_{job}.jpg", 4032, 3024)
        if api._crop_to_living_zone(str(src), tmp_path, 0, living, entrance_bbox1000=door):
            kept.append(job)
    assert len(kept) == 5, f"只剩 {kept} 還會裁切，其餘被新守門誤擋"


# ── fallback 的形狀：回 None，不得改叫另一個裁切函式 ────────────────

def test_rejects_by_returning_none_not_another_crop():
    """不合格時必須回 None（呼叫端沿用完整底圖），不得退去 `_crop_region_base`。

    `_crop_region_base` 會**再呼叫一次 `compute_zoning_v2`**（重跑 Gemini），
    拿另一份同語意的 living_zone bbox 去裁——同一張照片大概率裁出同樣的地板
    特寫（實算 y0≈46%，仍在門檻外），等於多花錢、多一次非決定性、修了沒用。
    """
    import inspect
    src = inspect.getsource(api._crop_to_living_zone)
    tail = src.split("_crop_keeps_room_structure")[-1]
    assert "return None" in tail, "不合格時沒有回 None"
    # ⚠️ 只掃程式碼本體：註解裡寫「不可以退去 _crop_region_base」是說明，不是呼叫。
    # （這個坑我在比例鎖那輪踩過一次——掃字串會抓到自己的註解。）
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "_crop_region_base(" not in code, (
        "客廳區特寫改去呼叫 _crop_region_base——那會再跑一次 zoning、"
        "而且大概率裁出同樣的地板特寫")


def test_guard_runs_after_door_exclusion_and_aspect_lock():
    """守門必須量【最終框】：門排除與比例收斂都會移動框，先量等於白量。"""
    import inspect
    src = inspect.getsource(api._crop_to_living_zone)
    i_door = src.index("_door_exclusion_limits")
    i_ratio = src.index("_converge_box_to_ar")
    i_guard = src.index("_crop_keeps_room_structure")
    assert i_guard > i_door, "守門排在門排除之前"
    assert i_guard > i_ratio, "守門排在比例收斂之前"


def test_region_base_is_guarded_after_the_ratio_lock():
    """🔴 `_crop_region_base` 現在也要守門，而且必須在 3:2 收斂**之後**。

    D21DC9E4（2026-08-21）：living_zone `[630,0,1000,1000]` 的語意是「家具該擺在
    地板的哪一塊」，本來就畫在地板上；程式把它當攝影裁切框 → 外擴 12% 後上緣 51%
    → 3:2 置中裁寬到 x 22%-78% → **天花板整片被切掉**，模型只好自己蓋一個。
    第一次生成的重試理由正是「天花板新增原本沒有的明管與軌道燈結構」。

    ⚠️ 位置必須在 3:2 之後：太高的框會 `y0 += trim*0.25` 再往下推，
       接在函式開頭會漏掉那種單。

    這條測試取代舊的 `test_region_base_deliberately_not_guarded`——當時不加是因為
    「34 次裁切全交付、沒量過不加」，程序正確；現在有反例，而且全站重量後
    走這條路的可量測訂單只有 1 筆（就是出事那張），另有 36 筆舊資料無 cropped 欄位。
    """
    import inspect
    src = inspect.getsource(api._crop_region_base)
    assert "_crop_keeps_room_structure" in src, "守門沒接上 _crop_region_base"
    # 3:2 收斂（_TARGET_AR）必須排在守門之前
    assert src.index("_TARGET_AR") < src.index("_crop_keeps_room_structure"), (
        "守門接在 3:2 收斂之前——太高的框會被 y0 再往下推，這樣會漏判")


def test_region_base_gives_up_cropping_when_ceiling_would_be_cut():
    """守門觸發時要**放棄裁切、回傳原圖**，不是硬裁一個沒天花板的框。"""
    import inspect
    src = inspect.getsource(api._crop_region_base)
    i = src.index("_crop_keeps_room_structure")
    tail = src[i:i + 400]
    # ⚠️ 不能只找 "return base_path, False"——函式後面「裁切檔寫入失敗」那條
    #    也長這樣，窗口一開大就會誤綠（我第一版就這樣沒紅）。釘那個具體的回傳。
    assert "return base_path, False, _why_struct, False" in tail, (
        "守門擋下後沒有退回原圖（必須回 base_path 且 cropped=False）")


def test_s2_waived_zoom_rejected_restores_uncropped_source():
    """🔴 S2 擋死退 legacy 時，zoom 被拒不得沿用前一刀裁壞的底圖。

    S2 合格那條路會把底圖還原成未裁原圖（`flux_bases[_vi] = _contract_photo`，
    註解寫「禁止 crop 把門或走道藏掉」）；擋死退 legacy 這條卻只有 `if _zoom:`
    沒有 else，zoom 被天花板守門拒絕後就繼續用 `_crop_region_base` 留下的裁切圖。
    最壞情況只是用整張原圖——本來就是裁切失敗的退路。
    """
    import inspect
    src = inspect.getsource(api)
    i = src.index("_zoom = _crop_to_living_zone")
    seg = src[i:i + 3000]
    assert "s2_waived_zoom_rejected_uncropped" in seg, "zoom 被拒沒有還原原圖"
    j = seg.index("s2_waived_zoom_rejected_uncropped")
    # ⚠️ 用**整行比對**，不要用子字串：`zone_crop_flags[_vi] = False` 這一行
    #    包含 `crop_flags[_vi] = False`，用 `in` 檢查會讓「刪掉真的那行」照樣綠
    #    （我第一版就是這樣沒紅）。
    lines = [l.strip() for l in seg[max(0, j - 800):j + 300].splitlines()]
    assert "crop_flags[_vi] = False" in lines, "還原時沒有清掉裁切旗標"
    assert "zone_crop_flags[_vi] = False" in lines, "還原時沒有清掉 zone 裁切旗標"
    assert "flux_bases[_vi] = _contract_photo" in lines, "沒有把底圖換回未裁原圖"

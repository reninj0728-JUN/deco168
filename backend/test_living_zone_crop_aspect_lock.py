# -*- coding: utf-8 -*-
"""客廳區特寫必須收斂到「模型真的會輸出」的比例，消除空間重構壓力。

29C70C03（2026-08-03）客戶回報：「這個門確定是裁掉嗎？怎麼感覺是移除掉？
因為裁掉後空間感也不對。」

追下去不是裁切失敗，是**底圖與輸出框比例不一致**：
  門排除後底圖 2561x1905 = 1.344
  gpt-image-2 只輸出三種固定尺寸，這張拿到 1536x1024 = 1.500
  → 差 11.6%，模型**必須**裁切／重構／補畫才能填滿輸出框，
     這部分畫面不會忠於原照片

⚠️ 比例差只證明「一定會發生重構」，**不能證明它會補在哪一側**。客訴內容與
這 11.6% 的落差時間吻合、方向合理，但那是推論不是量測。這組測試鎖的是
「消除比例差」這個可驗證的性質，不是「模型不會亂畫」這個做不到的保證。

`_crop_region_base` 早在 F87A75BB 就修過同一個 bug（比例鎖 v2「只裁不補」），
但 `_crop_to_living_zone` 這條 S2 退回 legacy 的路徑一直漏掉這道鎖。

三個設計決定，都在下面被測試鎖住：
1. 目標比例跟著**實際那個模型**走，不寫死 1.5。直式的客廳區若被硬拉成橫式
   要砍掉一半高度，比留著比例差更糟。
2. 更要命的是模型有兩個：`RENDER_MODEL` 預設仍是 nano-banana（十檔 enum，
   含 4:3=1.333），Railway 目前設 gpt-image-2（三檔）。同一個 1.344 的框，
   在 gpt-image-2 要裁到 1.5、在 banana 幾乎不用動——照錯模型的桶裁是白砍。
3. 收斂只裁不補，且不得抵銷門排除。
   ⚠️ 收斂方向會變：落 1.5 桶時裁【高】（與門排除的裁寬互不干擾），
   落 1.0 桶時裁【寬】、與門排除**同向疊加**。所以兩道守門都要驗。
"""
from __future__ import annotations

import inspect

import api
import pytest
from PIL import Image
from test_full_pipeline import gpt_output_size_for_ratio

W, H = 2000, 1500
DOOR_1000 = [320, 120, 860, 320]          # 大門在左
LIVING_FULL_1000 = [300, 100, 980, 950]   # 幾乎蓋滿整張、含大門
GPT2 = "openai/gpt-image-2/edit"
BANANA = "fal-ai/nano-banana-pro/edit"


@pytest.fixture(autouse=True)
def _pin_render_model(monkeypatch):
    """本機沒設 RENDER_MODEL 時預設是 banana——測試不能靠環境運氣。
    這裡釘成 Railway 現值；要驗別的模型的個別測試自己覆寫。"""
    monkeypatch.setenv("RENDER_MODEL", GPT2)


def _photo(tmp_path, w=W, h=H, name="room.jpg"):
    path = tmp_path / name
    Image.new("RGB", (w, h), (235, 235, 235)).save(path, "JPEG", quality=90)
    return path


def _crop(tmp_path, living, door=None, w=W, h=H, idx=0):
    out = api._crop_to_living_zone(
        str(_photo(tmp_path, w, h, f"room_{idx}.jpg")), tmp_path, idx,
        living, entrance_bbox1000=door)
    assert out, "裁不動"
    return out


def _ratio(box):
    x0, y0, x1, y1 = box
    return (x1 - x0) / (y1 - y0)


# ── 核心：底圖比例 == 模型輸出比例 ────────────────────────────────────

def test_crop_ratio_equals_model_output_ratio(tmp_path):
    """唯一真正重要的一條：交出去的底圖比例必須等於模型會輸出的比例。

    相等 → 模型不需要為了填滿輸出框而重構畫面；
    不等 → 差額必然由模型裁切／重構／補畫產生（29C70C03 的病灶）。
    """
    _path, box = _crop(tmp_path, LIVING_FULL_1000, DOOR_1000)
    size = gpt_output_size_for_ratio(_ratio(box))
    out_ar = size["width"] / size["height"]
    assert abs(_ratio(box) - out_ar) <= 0.02, (
        f"底圖 {_ratio(box):.3f} 與模型輸出 {out_ar:.3f} 不同 → "
        f"{abs(out_ar - _ratio(box)) / _ratio(box):.1%} 的畫面會由模型重構產生")


def test_unconverged_box_really_would_have_been_wrong(tmp_path):
    """反貧血：證明上一條測到的是新加的收斂，不是本來就會過。

    拿收斂前的框（門排除後、未收斂）算一次，落差必須顯著存在——
    否則上一條就算刪掉收斂程式碼也會通過，那就是廢測試。
    """
    _path, box = _crop(tmp_path, LIVING_FULL_1000, DOOR_1000)
    x0, y0, x1, y1 = box
    # 還原收斂前的框：收斂只裁不補，所以原框是 living+pad 再套門排除
    lx0, lx1 = LIVING_FULL_1000[1], LIVING_FULL_1000[3]
    ly0, ly1 = LIVING_FULL_1000[0], LIVING_FULL_1000[2]
    ux0 = int(max(0, (lx0 / 1000.0 - 0.04) * W))
    ux1 = int(min(W, (lx1 / 1000.0 + 0.04) * W))
    uy0 = int(max(0, (ly0 / 1000.0 - 0.04) * H))
    uy1 = int(min(H, (ly1 / 1000.0 + 0.04) * H))
    d_x0 = int(DOOR_1000[1] / 1000.0 * W)
    d_x1 = int(DOOR_1000[3] / 1000.0 * W)
    lo, hi = api._door_exclusion_limits(W, d_x0, d_x1)
    ux0, ux1 = max(ux0, lo), min(ux1, hi)

    before = (ux1 - ux0) / (uy1 - uy0)
    size = gpt_output_size_for_ratio(before)
    out_ar = size["width"] / size["height"]
    assert abs(before - out_ar) > 0.02, (
        f"收斂前比例 {before:.3f} 本來就等於輸出 {out_ar:.3f}，"
        f"這組 fixture 測不出收斂有沒有生效")
    assert (x0, y0, x1, y1) != (ux0, uy0, ux1, uy1), "收斂根本沒動到框"


# ── 只裁不補 ──────────────────────────────────────────────────────────

def test_convergence_never_grows_the_box(tmp_path):
    """只裁不補：收斂後的框必須含於收斂前的框。

    補寬會把已經推出鏡的大門重新拉回畫面——那就白修了。
    """
    for x0, y0, x1, y1, target in (
        (100, 100, 1900, 1000, 1.5),      # 太寬 → 裁寬
        (100, 100, 1000, 1400, 1.5),      # 太高 → 裁高
        (100, 100, 1000, 1000, 0.667),    # 直式目標
    ):
        nx0, ny0, nx1, ny1 = api._converge_box_to_ar(x0, y0, x1, y1, target)
        assert x0 <= nx0 <= nx1 <= x1, f"水平方向外擴了: {(nx0, nx1)} 不在 {(x0, x1)} 內"
        assert y0 <= ny0 <= ny1 <= y1, f"垂直方向外擴了: {(ny0, ny1)} 不在 {(y0, y1)} 內"
        assert abs((nx1 - nx0) / (ny1 - ny0) - target) <= 0.02, "沒收斂到目標比例"


def test_convergence_does_not_undo_door_exclusion(tmp_path):
    """收斂不得把大門重新帶回畫面——這是最容易回歸的一條。"""
    _path, (x0, _y0, x1, _y1) = _crop(tmp_path, LIVING_FULL_1000, DOOR_1000)
    door_x0 = int(DOOR_1000[1] / 1000.0 * W)
    door_x1 = int(DOOR_1000[3] / 1000.0 * W)
    assert not (x0 < door_x1 and door_x0 < x1), (
        f"收斂後裁切窗 x[{x0},{x1}] 又與大門 x[{door_x0},{door_x1}] 重疊")


def test_both_guards_hold_after_both_cuts(tmp_path):
    """門排除與比例收斂**都跑完之後**，寬高兩道守門都要過。

    ⚠️ 修正一個我先前講太滿的說法：「兩刀方向不同所以不疊加」只在落 1.5 桶時
    成立（門排除裁寬、收斂裁高）。落 1.0 桶時收斂也是**裁寬**，與門排除同向
    疊加——8 張實測有 3 張落 1.0 桶。所以不能只驗其中一邊，兩邊都要驗。
    """
    for living, door in ((LIVING_FULL_1000, DOOR_1000),
                         ([250, 0, 990, 1000], [320, 0, 980, 300]),   # 落 1.0 桶（同向疊加）
                         ([250, 150, 950, 950], [320, 120, 850, 300])):
        out = api._crop_to_living_zone(
            str(_photo(tmp_path, name=f"g_{living[1]}_{door[1]}.jpg")),
            tmp_path, 0, living, entrance_bbox1000=door)
        assert out, f"living={living} 裁不動"
        _p, (x0, y0, x1, y1) = out
        assert (x1 - x0) >= W * 0.28, f"living={living} 收斂後寬度 {x1-x0} 破守門"
        assert (y1 - y0) >= H * 0.28, f"living={living} 收斂後高度 {y1-y0} 破守門"


def test_width_cut_case_really_compounds_with_door_exclusion(tmp_path):
    """反貧血：證明上一條不是空跑——確實存在「收斂也裁寬」的案例。

    若哪天收斂改成永遠裁高，這條會失敗並提醒重新檢視守門論證。
    """
    living, door = [250, 0, 990, 1000], [320, 0, 980, 300]
    ux0 = int(max(0, (living[1] / 1000.0 - 0.04) * W))
    ux1 = int(min(W, (living[3] / 1000.0 + 0.04) * W))
    uy0 = int(max(0, (living[0] / 1000.0 - 0.04) * H))
    uy1 = int(min(H, (living[2] / 1000.0 + 0.04) * H))
    d_x0 = int(door[1] / 1000.0 * W)
    d_x1 = int(door[3] / 1000.0 * W)
    lo, hi = api._door_exclusion_limits(W, d_x0, d_x1)
    nx0, nx1 = max(ux0, lo), min(ux1, hi)
    if (nx0, nx1) != (ux0, ux1) and (nx1 - nx0) >= W * 0.30:
        ux0, ux1 = nx0, nx1
    target = api._model_output_ar_for(ux1 - ux0, uy1 - uy0)
    cx0, cy0, cx1, cy1 = api._converge_box_to_ar(ux0, uy0, ux1, uy1, target)
    assert (cx1 - cx0) < (ux1 - ux0), "這組 fixture 的收斂沒有裁寬，證不出同向疊加"
    assert (cy1 - cy0) == (uy1 - uy0), "同時裁了高，fixture 不純"


# ── 目標比例是推導的、不是寫死的 ──────────────────────────────────────

def test_target_ar_is_derived_not_hardcoded(tmp_path):
    """直式客廳區不得被硬拉成 3:2。

    寫死 1.5 的話，0.667 的框要砍掉一半高度；推導版會收斂到 0.667（幾乎不動）。
    """
    tall = api._model_output_ar_for(1000, 1500)
    assert tall == pytest.approx(1024 / 1536, abs=0.01), (
        f"直式框拿到目標比例 {tall}——目標比例被寫死成橫式了")
    square = api._model_output_ar_for(1000, 1000)
    assert square == pytest.approx(1.0, abs=0.01), f"方形框拿到 {square}"
    wide = api._model_output_ar_for(2561, 1905)
    assert wide == pytest.approx(1.5, abs=0.01), f"橫式框拿到 {wide}"


def test_target_ar_shares_one_source_of_truth():
    """門檻必須向來源取，不得在 api.py 另寫一組常數。

    兩套口徑是這系列問題的病根（`_door_exclusion_limits` 也是刻意共用的）。
    """
    import ast
    import textwrap
    src = inspect.getsource(api._model_output_ar_for)
    assert "gpt_output_size_for_ratio" in src, "沒有共用 gpt-image-2 的門檻"

    # 只看程式碼本體：docstring 裡舉例寫 "4:3=1.333" 是說明，不是抄門檻
    fn = ast.parse(textwrap.dedent(src)).body[0]
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           and isinstance(fn.body[0].value.value, str)) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    for stolen in ("1.15", "0.87", "21:9", "4:3", "5:4", "1536", "1024"):
        assert stolen not in code, f"把桶子門檻 {stolen} 抄了一份到 api.py 的程式碼裡"


# ── 模型感知：兩個模型的桶完全不同，不可混用 ──────────────────────────

def test_only_proven_model_gets_the_aspect_lock(monkeypatch):
    """白名單：只有 gpt-image-2 收斂，其餘一律回 None。

    ⚠️ 這條是修一個真 bug 的回歸鎖。舊版寫成
        if model == gpt2: ... else: <用另一個模型的 enum>
    於是**任何**不認得的模型都會照一份沒有證據的比例裁圖，而且靜默無訊息。
    白名單才不會有這種漏斗。

    2026-08-04 起渲染模型已硬綁 gpt-image-2，所以這條測的是守門本身還在，
    不是「env 會不會換模型」——直接替換解析函式，不繞 env。
    """
    assert api._model_output_ar_for(2561, 1905) == pytest.approx(1.5, abs=0.01)

    for other in ("fal-ai/nano-banana-pro/edit", "some/unknown/model-v9", "", "auto"):
        monkeypatch.setattr(api, "_legacy_render_model", lambda _m=other: _m)
        assert api._model_output_ar_for(2561, 1905) is None, (
            f"model={other!r} 竟然拿到了收斂目標——白名單破了")


def test_whitelist_matches_the_only_render_model():
    """白名單常數必須等於生成端唯一的模型，不得各寫各的。"""
    from test_full_pipeline import RENDER_MODEL
    assert api._ASPECT_LOCKED_MODEL == RENDER_MODEL, (
        f"裁切端鎖 {api._ASPECT_LOCKED_MODEL}、生成端跑 {RENDER_MODEL}")


def test_threshold_lookup_failure_also_skips(monkeypatch):
    """連 gpt-image-2 這條路，門檻查不到時也必須跳過，不得退回猜一個 1.5。

    退回 1.5 看似安全（橫圖多數都是 1.5），但方圖／直圖的框會被硬拉成橫式、
    白砍一大塊——而且沒有任何訊息說它猜過。
    """
    monkeypatch.setenv("RENDER_MODEL", GPT2)
    import test_full_pipeline as tfp

    def boom(_r):
        raise RuntimeError("threshold lookup down")

    monkeypatch.setattr(tfp, "gpt_output_size_for_ratio", boom)
    assert api._model_output_ar_for(2561, 1905) is None, "門檻查不到卻猜了一個比例"
    assert api._model_output_ar_for(2703, 2389) is None


def test_no_other_render_endpoint_remains():
    """絆線：生成端不得再有第二個渲染模型端點。

    2026-08-04 移除 nano-banana 殘黨——它一單也沒跑過，卻讓每個下游都得處理
    「萬一翻回去」的假想情況（比例桶就為此繞了一大圈）。這條擋它回來：
    只要 `generate_renders` 又出現別的 fal 模型字串，比例鎖的白名單前提就破了，
    必須重新確認那個模型的輸出比例可不可證。
    """
    import inspect
    import test_full_pipeline as tfp
    src = inspect.getsource(tfp.generate_renders)
    for gone in ("nano-banana", "aspect_ratio", "build_anchored_inputs", "use_anchored"):
        assert gone not in src, f"generate_renders 又出現 {gone!r}——白名單前提要重驗"
    assert "RENDER_MODEL" in src, "生成端沒有引用唯一的 RENDER_MODEL 常數"
    assert tfp.RENDER_MODEL == "openai/gpt-image-2/edit"


def test_render_model_resolution_shares_the_generator(monkeypatch):
    """裁切端解析出來的模型，必須就是生成端會用的那個，不得自己寫一份。

    以前這裡是「連預設值都要一致」；模型硬綁之後改成更強的斷言：
    **env 怎麼設都不影響**，兩邊永遠同值。
    """
    from test_full_pipeline import _resolve_render_model
    for env in (GPT2, "fal-ai/nano-banana-pro/edit", "亂寫", None):
        if env is None:
            monkeypatch.delenv("RENDER_MODEL", raising=False)
        else:
            monkeypatch.setenv("RENDER_MODEL", env)
        assert api._legacy_render_model() == _resolve_render_model(None) == GPT2, (
            f"RENDER_MODEL={env!r} 竟然改變了模型解析")
    src = inspect.getsource(api._legacy_render_model)
    assert "_resolve_render_model" in src, "沒有共用生成端的解析函式"


@pytest.mark.parametrize("model", ["fal-ai/nano-banana-pro/edit", "some/unknown/model-v9"])
def test_unproven_model_keeps_the_unconverged_box(tmp_path, monkeypatch, model):
    """非 gpt-image-2 時整條收斂跳過：裁切框必須完全等於未收斂的框。

    ⚠️ 有一版這條測試是假的——它 monkeypatch 讓另一個模型的比例函式拋例外才
    拿到 None，但真實的未知模型不會拋例外，會安靜地走進那個分支。現在模型已
    硬綁，這裡替換的是解析函式本身，測的是白名單守門的行為。
    """
    monkeypatch.setattr(api, "_legacy_render_model", lambda: model)
    out = api._crop_to_living_zone(
        str(_photo(tmp_path, name=f"u_{model.replace('/', '_')}.jpg")), tmp_path, 9,
        LIVING_FULL_1000, entrance_bbox1000=DOOR_1000)
    assert out, "跳過收斂就整個裁不動了"
    _p, box = out

    ux0 = int(max(0, (LIVING_FULL_1000[1] / 1000.0 - 0.04) * W))
    ux1 = int(min(W, (LIVING_FULL_1000[3] / 1000.0 + 0.04) * W))
    uy0 = int(max(0, (LIVING_FULL_1000[0] / 1000.0 - 0.04) * H))
    uy1 = int(min(H, (LIVING_FULL_1000[2] / 1000.0 + 0.04) * H))
    d_x0 = int(DOOR_1000[1] / 1000.0 * W)
    d_x1 = int(DOOR_1000[3] / 1000.0 * W)
    lo, hi = api._door_exclusion_limits(W, d_x0, d_x1)
    nx0, nx1 = max(ux0, lo), min(ux1, hi)
    if (nx0, nx1) != (ux0, ux1) and (nx1 - nx0) >= W * 0.30:
        ux0, ux1 = nx0, nx1
    assert box == (ux0, uy0, ux1, uy1), (
        f"model={model} 應該完全跳過收斂，卻裁成 {box}（未收斂框是 "
        f"{(ux0, uy0, ux1, uy1)}）")
    assert box[0] >= d_x1, "跳過收斂時連門排除都掉了"


def test_convergence_is_a_fixed_point():
    """收斂不會把框推到另一個尺寸桶去——所以不需要迭代。

    若 1.344→1.5 之後 1.5 又落到別的桶，就會出現「收斂完仍然不等於輸出」。
    """
    for r in (0.50, 0.70, 0.87, 0.95, 1.15, 1.344, 2.30):
        target = api._model_output_ar_for(int(r * 1000), 1000)
        again = api._model_output_ar_for(int(target * 1000), 1000)
        assert target == pytest.approx(again, abs=0.01), (
            f"比例 {r} → 目標 {target} → 再取一次變成 {again}，不是不動點")


# ── 守門與健壯性 ──────────────────────────────────────────────────────

def test_guard_keeps_unconverged_box_when_result_too_small(tmp_path):
    """收斂後破 0.28 守門時，保留**未收斂**的框——寧可讓模型補一點，
    也不要交一張只剩一條的廢底圖（零圖對客戶傷害最大）。

    fixture 設計：裁出 600x799（ar=0.751 → 直式桶 → 目標 0.667），
    收斂要把寬砍到 532 < 0.28W=560。原框剛好過得了更早那道 0.30 守門，
    所以踩到的確實是收斂這一道，不是別的。
    """
    narrow = [250, 400, 703, 620]
    ux0 = int(max(0, (narrow[1] / 1000.0 - 0.04) * W))
    ux1 = int(min(W, (narrow[3] / 1000.0 + 0.04) * W))
    uy0 = int(max(0, (narrow[0] / 1000.0 - 0.04) * H))
    uy1 = int(min(H, (narrow[2] / 1000.0 + 0.04) * H))
    assert (ux1 - ux0) >= W * 0.30 and (uy1 - uy0) >= H * 0.30, "fixture 過不了前一道守門"
    target = api._model_output_ar_for(ux1 - ux0, uy1 - uy0)
    cx0, _cy0, cx1, _cy1 = api._converge_box_to_ar(ux0, uy0, ux1, uy1, target)
    assert (cx1 - cx0) < W * 0.28, "fixture 收斂後沒有破守門，測不到這條路"

    out = api._crop_to_living_zone(
        str(_photo(tmp_path)), tmp_path, 0, narrow, entrance_bbox1000=None)
    assert out, "收斂破守門時整個放棄裁切了——應該退回未收斂的框"
    path, box = out
    assert box == (ux0, uy0, ux1, uy1), f"沒有退回未收斂的框，拿到 {box}"
    with Image.open(path) as im:
        assert im.size == (ux1 - ux0, uy1 - uy0), "回傳原框卻寫出收斂後的圖"


def test_written_image_matches_returned_box(tmp_path):
    """實際寫出來的圖必須等於回傳的 crop_box。

    收斂若只改了座標卻寫出舊的 crop，引導圖會把分區座標對映到錯的畫面上
    （`_rebuild_guide_on_zoom` 是靠這個 box 換算的），比不收斂還糟。
    """
    path, (x0, y0, x1, y1) = _crop(tmp_path, LIVING_FULL_1000, DOOR_1000)
    with Image.open(path) as im:
        assert im.size == (x1 - x0, y1 - y0), (
            f"寫出的圖 {im.size} 與回傳的框 {(x1-x0, y1-y0)} 不符")


def test_aspect_lock_is_wired_into_living_zone_crop():
    """鎖住接線：客廳區特寫必須真的呼叫收斂，不能只留註解。"""
    src = inspect.getsource(api._crop_to_living_zone)
    assert "_converge_box_to_ar(" in src, "客廳區特寫沒有接上比例鎖"
    assert "_model_output_ar_for(" in src, "目標比例沒有向共用來源取"


def test_gpt_image_size_for_behaviour_unchanged(tmp_path):
    """抽出 `gpt_output_size_for_ratio` 之後，原本的檔案版行為不得改變。"""
    from test_full_pipeline import _gpt_image_size_for
    cases = {
        (2000, 1000): {"width": 1536, "height": 1024},
        (1000, 2000): {"width": 1024, "height": 1536},
        (1000, 1000): {"width": 1024, "height": 1024},
        (2561, 1905): {"width": 1536, "height": 1024},
    }
    for (w, h), want in cases.items():
        p = _photo(tmp_path, w, h, f"s_{w}x{h}.jpg")
        assert _gpt_image_size_for(str(p)) == want, f"{w}x{h} 尺寸判斷變了"
    assert _gpt_image_size_for("/nonexistent/x.jpg") == {"width": 1536, "height": 1024}, (
        "讀不到檔的退路變了")

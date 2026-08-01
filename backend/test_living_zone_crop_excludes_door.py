# -*- coding: utf-8 -*-
"""S2 退回 legacy 時的「客廳區特寫」必須把大門推出鏡頭外。

2D212624（2026-08-01）：退回機制有生效（layout_mode=legacy_fallback，不再零圖），
但成品裡大門還在畫面左邊 → 電視櫃緊貼大門（門距 0.050 門寬，需 0.28）→ 落選。

根因：`_crop_to_living_zone` 只裁 living_zone，**完全沒有門排除**。
它的 docstring 承諾「大門與兩扇臥室門全部出鏡」，但那是 928AD8B4 那張照片
剛好 living_zone 不含門的運氣，實作從來沒有保證它。
2D212624 的 living_zone 幾乎蓋滿整張（含大門）→ 裁完門還在。

⚠️ 也是我離線量測的錯：那份「legacy 7/7 產得出配置」是套 `_door_exclusion_limits`
算的，但實際這條路根本沒叫它——我量了一個不存在的版本。這裡把兩者接起來。
"""
from __future__ import annotations

import api
import pytest
from PIL import Image

W, H = 2000, 1500
# 大門在左側：x 120~320 /1000（照 2D212624 的形狀）
DOOR_1000 = [320, 120, 860, 320]
# living_zone 幾乎蓋滿整張，含大門——這正是舊版失效的條件
LIVING_FULL_1000 = [300, 100, 980, 950]


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "room.jpg"
    Image.new("RGB", (W, H), (235, 235, 235)).save(path, "JPEG", quality=90)
    return path


def _crop_x_range(result):
    assert result, "裁不動"
    _, (x0, _y0, x1, _y1) = result
    return x0, x1


def test_door_is_pushed_out_of_frame(photo, tmp_path):
    """核心：living_zone 含門時，裁切左緣必須推到門右緣之後。"""
    out = api._crop_to_living_zone(
        str(photo), tmp_path, 0, LIVING_FULL_1000, entrance_bbox1000=DOOR_1000)
    x0, x1 = _crop_x_range(out)
    door_x1 = int(DOOR_1000[3] / 1000.0 * W)
    assert x0 >= door_x1, (
        f"裁切左緣 {x0} 沒有推過門右緣 {door_x1}——大門仍在畫面裡")
    assert x1 - x0 >= W * 0.30, "推完之後畫面太窄"


def test_old_behaviour_still_keeps_the_door(photo, tmp_path):
    """反貧血：不傳門 bbox（＝舊行為）時門確實還在畫面裡。

    證明上一條測到的是新加的門排除，不是本來就會過。
    """
    out = api._crop_to_living_zone(str(photo), tmp_path, 0, LIVING_FULL_1000)
    x0, _x1 = _crop_x_range(out)
    door_x1 = int(DOOR_1000[3] / 1000.0 * W)
    assert x0 < door_x1, "沒傳門 bbox 卻把門裁掉了，測試前提不成立"


def test_rendered_pixels_no_longer_contain_the_door(photo, tmp_path):
    """不只驗座標——實際裁出來的圖不能再包含門的像素範圍。"""
    out = api._crop_to_living_zone(
        str(photo), tmp_path, 0, LIVING_FULL_1000, entrance_bbox1000=DOOR_1000)
    path, (x0, y0, x1, y1) = out
    with Image.open(path) as im:
        assert im.size == (x1 - x0, y1 - y0)
    door_x0 = int(DOOR_1000[1] / 1000.0 * W)
    door_x1 = int(DOOR_1000[3] / 1000.0 * W)
    # 裁切視窗與門的 x 區間不得有交集
    assert not (x0 < door_x1 and door_x0 < x1), "裁切視窗仍與大門重疊"


def test_narrow_result_keeps_original_crop(photo, tmp_path):
    """門太寬（推完會只剩一條）時必須放棄推，不得產生廢底圖。"""
    fat_door = [320, 0, 860, 900]      # 門佔 90% 畫寬
    out = api._crop_to_living_zone(
        str(photo), tmp_path, 0, LIVING_FULL_1000, entrance_bbox1000=fat_door)
    x0, x1 = _crop_x_range(out)
    assert x1 - x0 >= W * 0.30, "推完過窄卻還是推了"


def test_bad_entrance_bbox_is_ignored(photo, tmp_path):
    """壞的門 bbox 不得讓裁切爆掉——這條路是 S2 擋死後的最後一招。"""
    for bad in (None, [], [1, 2], ["a", "b", "c", "d"], [0, 0, 0, 0]):
        out = api._crop_to_living_zone(
            str(photo), tmp_path, 0, LIVING_FULL_1000, entrance_bbox1000=bad)
        assert out, f"門 bbox={bad!r} 讓客廳區裁切整個失敗"


def test_door_exclusion_shares_one_implementation():
    """門排除必須共用 `_door_exclusion_limits`，不得另寫一套緩衝算法。

    兩套門排除口徑就是這整個系列問題的病根（legacy 有、S2 沒繼承、
    分區頁又畫第三種）。這條鎖住共用。
    """
    import inspect
    src = inspect.getsource(api._crop_to_living_zone)
    assert "_door_exclusion_limits(" in src, "客廳區特寫沒有共用同一個門排除函式"

# -*- coding: utf-8 -*-
"""書房／餐廳的桌子槽位不得配到化妝桌。

A62AC21A（2026-08-05）：`room_type=study` 配到「簡約白色化妝桌椅組合」，
渲染圖上就是一張梳妝台加鏡子。書房的核心是工作面，客戶付錢拿到梳妝台。

根因：`table` 是一個大類，書桌／化妝桌／邊桌全在裡面，而
`SLOT_NAME_GUARDS["table"]` 原本只擋電視櫃與玄關櫃。

⚠️ 化妝桌在【臥室】完全合理，所以守衛**只掛 study**，不掛 "any"、也不掛 dining
   ——餐廳走的是 `dining_table` 槽位，`table` 在餐廳根本用不到，掛上去是死規則。
⚠️ dry-run 實測：擋掉之後 cream 的同風格書桌池歸零（那 3 件全是化妝桌），
   但 Stage B 相近風格有 112 件真書桌接手——沒有任何風格會拿不到桌子。
"""
from __future__ import annotations

import furniture_match as fm
import pytest

VANITY_NAMES = [
    "簡約白色化妝桌椅組合",          # A62AC21A 實際配到的那件
    "白色化妝台附鏡子及化妝椅",
    "【新川】化妝桌(輕奢梳妝台 小型出租屋梳妝台)",
    "三面鏡化妝台附椅",
    "木質化妝台附鏡子與凳子",
    "70*40cm化妝桌椅組 智能LED",
]
DESK_NAMES = [
    "文創集 科法輕奢白3.5尺四抽書桌",
    "簡約北歐風實木電腦桌",
    "居家辦公桌 工作桌",
]


@pytest.mark.parametrize("name", VANITY_NAMES)
def test_vanity_blocked_from_study_desk_slot(name):
    assert fm.violates_slot_guard("table", "study", name) is True, (
        f"「{name}」仍能進書房的桌子槽位")


def test_no_dead_guard_on_dining():
    """餐廳走的是 `dining_table` 槽位，`table` 在餐廳根本用不到。

    第一版我把守衛也掛在 dining，那是死規則——掛了不會有任何效果，
    卻會讓人以為餐桌被保護了。
    """
    assert fm.ROOM_RULES["dining"]["must"] == ["dining_table", "dining_chair"], (
        "餐廳 must 槽位變了，這條測試的前提要重看")
    assert "table" not in fm.ROOM_RULES["dining"]["must"], "餐廳現在會用 table 槽位了"
    assert "dining" not in (fm.SLOT_NAME_GUARDS.get("table") or {}), (
        "table 槽位掛了 dining 守衛——那是死規則")


@pytest.mark.parametrize("name", VANITY_NAMES)
def test_vanity_still_allowed_in_bedroom(name):
    """反貧血：梳妝台在臥室是正常需求，不得被一起擋掉。"""
    assert fm.violates_slot_guard("table", "bedroom", name) is False, (
        f"「{name}」在臥室被誤擋——守衛掛錯成 any 了")


@pytest.mark.parametrize("name", DESK_NAMES)
def test_real_desks_still_pass_everywhere(name):
    """真書桌在三個房型都不得被新守衛掃到。"""
    for room in ("study", "dining", "bedroom"):
        assert fm.violates_slot_guard("table", room, name) is False, (
            f"「{name}」在 {room} 被誤擋")


def test_existing_table_guards_unchanged():
    """原有的電視櫃／玄關櫃守衛不得因為這次改動失效。"""
    for name in ("9.7尺L型電視中空櫃", "北歐風電視櫃", "三層鞋櫃", "玄關櫃"):
        assert fm.violates_slot_guard("table", "study", name) is True, f"{name} 不再被擋"
        assert fm.violates_slot_guard("table", "living", name) is True, f"{name} 不再被擋"


def test_keyword_stuffed_desk_is_not_blocked():
    """🔴 誤擋回歸鎖：台灣電商標題會塞詞，真書桌的品名也可能出現「化妝桌」。

    目錄裡這件是真的書桌：
      學生學習書桌 居家辦公桌 臥室化妝桌 飄窗旁休閒桌 小護型電腦桌 休閒桌 學習桌
    第一版守衛把它一起擋掉了。品名同時宣告書桌時，書桌宣告優先。
    """
    stuffed = "學生學習書桌 居家辦公桌 臥室化妝桌 飄窗旁休閒桌 小護型電腦桌 休閒桌 學習桌"
    assert fm.violates_slot_guard("table", "study", stuffed) is False, (
        "塞詞標題的真書桌被誤擋")
    # 但「電視櫃」這種 any 級守衛不得被書桌宣告解除
    assert fm.violates_slot_guard("table", "study", "書桌型電視櫃") is True, (
        "any 級守衛（電視櫃）被書桌宣告蓋掉了")


def test_stage_c_lifeline_still_applies_the_guard():
    """🔴 Stage C 回歸鎖：`table` 是 study 的 must-have，而 Stage C 原本
    完全不套槽位守衛——化妝桌被 Stage A/B 擋掉之後仍會從保命池被撈回書房。

    這條驗真實配對器的最終結果，不是只驗池子裡有貨。
    """
    import inspect
    src = inspect.getsource(fm._pick_best_in_category)
    tail = src.split("Stage C")[-1]
    assert "violates_slot_guard" in tail, "Stage C 仍完全不套槽位守衛"
    assert "放寬守衛" in tail, "守衛內池空掉時沒有退路，must-have 可能變空"


@pytest.mark.parametrize("style", [
    "modern", "nordic", "cream", "muji", "japanese", "wood", "luxury",
    "french", "chinese-modern", "industrial", "art-deco", "boho", "mediterranean",
])
def test_end_to_end_study_desk_is_never_a_vanity(style):
    """端到端：真的呼叫配對器，斷言書房選出來的桌子不是化妝桌。

    涵蓋 CLAUDE.md 列的全部正式風格（含目錄裡件數為 0 的），
    件數 0 的風格靠 Stage C 保命，正好驗到守衛有沒有跟著生效。

    ⚠️ 不用 pytest.skip 當保護傘：Stage C 撈的是**全目錄不分風格**的 table，
    只要目錄有桌子就一定選得到，所以 None 是真失敗不是「缺貨」。
    """
    catalog = fm.load_catalog()
    chosen = fm._pick_best_in_category(
        "table", style, ["desk", "study", "work"], catalog,
        must_categories=["table", "chair"], room_mode="study")
    assert chosen is not None, (
        f"{style} 書房連 Stage C 保命都選不到桌子——must-have 會變空")
    name = chosen.get("name_zh") or ""
    is_vanity = any(k in name for k in ("化妝桌", "化妝台", "梳妝台", "梳妝桌"))
    declares_desk = any(k in name for k in fm._DESK_NAME_KW)
    assert (not is_vanity) or declares_desk, (
        f"{style} 書房選到化妝桌：{name}")


def test_no_style_loses_its_study_desk_pool():
    """守衛不得讓任何風格在 Stage B 之後仍拿不到書桌。

    這條是上線前的實質保證：同風格池可以被擋空（cream 就是），
    但相近風格必須接得住，否則書房會沒有桌子。
    """
    catalog = fm.load_catalog()
    tables = [it for it in catalog if fm.resolve_category(it) == "table"]
    assert tables, "目錄裡沒有 table 大類，測試前提不成立"

    def usable(items, room):
        out = []
        for it in items:
            nm = (it.get("name_zh") or "").strip()
            if fm.is_multi_piece_bundle(nm):
                continue
            if any(k in nm for k in fm.TABLE_JUNK_KW):
                continue
            if fm.violates_slot_guard("table", room, nm):
                continue
            out.append(it)
        return out

    thin = []
    for style in ("modern", "nordic", "cream", "muji", "japanese", "wood", "luxury"):
        rel = fm._get_related_styles(style)
        pool = [it for it in tables
                if style in (it.get("style_tags") or [])
                or any(s in (it.get("style_tags") or []) for s in rel)]
        n = len(usable(pool, "study"))
        if n == 0:
            thin.append(f"{style}=0")
    assert not thin, f"這些風格連 Stage B 都拿不到書桌：{thin}"

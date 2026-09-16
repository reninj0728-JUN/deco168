# -*- coding: utf-8 -*-
"""客戶寫「客廳不靠窗」時，系統不准反過來逼客廳靠窗。

7F0874C7：客戶在照片備註（photo_meta_by_key[...].target_note）寫了「客廳不靠窗」。
全域 customer_notes 是空的，所以第一眼會誤以為客戶什麼都沒填。

🔴 這句話不是「被忽略」，是【被反過來執行】。因為所有判斷都寫成 `k in s`，
   而「客廳不靠窗」裡面含有「靠窗」子字串：
     · 判官 ws_kws 命中「靠窗」→ is_window_side=True → 反過來要求沙發往窗端擺
       （沙發深度硬門檻 58%／65%）
     · _score_photo_for_room 命中「靠窗」→ +100，把它當成最強的「靠窗客廳主圖」信號
     · _note_implies_rear_near_window 認得出否定，但只用來「不升級」，沒有反向效果
   於是付費客戶講的話被系統倒過來強制執行。

🔴 所以修法不能只在 _apply_target_note_layout_constraints 加一個否定分支：
   契約說「不准靠窗」、判官仍逼「必須靠窗」＝無盡重試，或模型為了過關把客廳擺回窗邊。
   四個消費端必須共用同一個否定判準。

⚠️ 只禁窗端，不指定去哪。「不靠窗」沒說要去近端——硬推近端會撞 2879173D
   （沙發吃掉進門落腳區）。近端／中段留給既有規則。
⚠️ 這一刀拆的是「客戶指示被反著用」，拆不掉「系統硬要在廚具牆上放電視櫃」
   （tv_side 仍可能是廚具牆、media_console 仍必配）。那是後續步驟。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api
from gemini_analyze import note_forbids_window_side

NEG = "客廳不靠窗"
POS = "客廳靠窗"


# ── 共用判準 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("note", [
    "客廳不靠窗", "不要靠窗", "客廳不要窗邊", "沙發不在窗邊",
    "客廳遠離窗", "not near window", "away from the window",
])
def test_negative_notes_are_recognised(note):
    assert note_forbids_window_side(note) is True, f"沒認出否定句：{note}"


@pytest.mark.parametrize("note", [POS, "靠窗做客廳", "喜歡淺木色", "餐廳中段", "", None, 123])
def test_positive_or_irrelevant_notes_are_not_flagged(note):
    assert note_forbids_window_side(note) is False, f"誤判成否定句：{note!r}"


def test_only_one_negative_wordlist_exists():
    """🔴 判準只准有一份。兩邊各自維護，會對同一句話得到相反結論。"""
    src = (Path(__file__).resolve().parent / "api.py").read_text(encoding="utf-8")
    assert "negative_markers = (" not in src, "api.py 又自己養了一份否定清單"
    assert src.count("note_forbids_window_side") >= 3, \
        "api.py 的三個消費端沒有全部接上共用判準"


# ── 判官：不得反過來逼靠窗 ──────────────────────────────────────────
def _is_window_side(note, hint="unspecified", living_where=""):
    """複製判官那一行的條件，驗的是同一組輸入會得到什麼結論。"""
    ws_kws = ["靠窗", "窗邊", "窗戶", "底端", "深處", "後半段", "後段",
              "深端", "底部", "底側", "尾端", "末端"]
    signal = f"{living_where} {hint} {note}"
    return (not note_forbids_window_side(note)) and (
        hint == "rear_near_window" or any(k in signal for k in ws_kws))


def test_judge_does_not_demand_window_side_for_a_negative_note():
    """🔴 舊行為：「客廳不靠窗」→ is_window_side=True → 沙發硬門檻 58% 逼往窗端。"""
    assert _is_window_side(NEG) is False, "否定句仍被判成靠窗，沙發會被逼回窗邊"
    assert _is_window_side(POS) is True, "肯定句的原有行為被改壞了"


def test_negative_note_overrides_the_structured_hint():
    """備註與 hint 衝突時以「不要」為準——備註更具體、也是後寫的。"""
    assert _is_window_side(NEG, hint="rear_near_window") is False


def test_judge_source_uses_the_shared_helper():
    src = (Path(__file__).resolve().parent / "gemini_analyze.py").read_text(encoding="utf-8")
    i = src.index("is_window_side = ")
    assert "note_forbids_window_side" in src[i - 400:i + 300], \
        "判官的 is_window_side 沒有接上否定判準"


# ── 契約：要覆寫，不是往後接一句自相矛盾的話 ────────────────────────
def _apply(note, where="靠陽台採光落地窗前方的中深處地面與左側長牆區域"):
    z = {"zones": {"living_zone": {"where": where}}, "furniture_placement_rules": {}}
    return api._apply_target_note_layout_constraints(z, note, "living", "unspecified")


def test_contract_replaces_the_window_side_description():
    out = _apply(NEG)
    w = out["zones"]["living_zone"]["where"]
    assert "不靠窗" in w
    assert "落地窗前方" not in w, f"舊的靠窗描述沒被覆寫，同一欄自相矛盾：{w}"
    assert out["zones"]["living_zone"].get("_user_forbids_window_side") is True


def test_contract_adds_a_no_go_with_teeth():
    """光改描述咬不住——「餐廳中段」能生效就是因為同時加了 no_go。"""
    out = _apply(NEG)
    no_go = out["furniture_placement_rules"]["no_large_furniture_zones"]
    assert any("不靠窗" in x for x in no_go), f"沒有窗端 no-go：{no_go}"
    joined = " ".join(no_go)
    for item in ("沙發", "地毯", "茶几", "電視櫃"):
        assert item in joined, f"no-go 沒點名 {item}"


def test_contract_does_not_invent_a_destination():
    """🔴 只禁窗端。「不靠窗」沒說要去近端——硬推近端會撞 2879173D。"""
    w = _apply(NEG)["zones"]["living_zone"]["where"]
    for bad in ("近端", "靠門", "玄關", "入口處", "中段"):
        assert bad not in w, f"發明了去處「{bad}」：{w}"


def test_positive_note_behaviour_unchanged():
    out = _apply(POS)
    assert "客廳靠窗端" in out["zones"]["living_zone"]["where"]
    assert not out["furniture_placement_rules"].get("no_large_furniture_zones")


def test_no_note_orders_are_untouched():
    """沒寫備註的單一個字都不准動。"""
    before = "靠陽台採光落地窗前方的中深處地面與左側長牆區域"   # _apply 的預設值，要一字不差
    for note in ("", None, "喜歡淺木色"):
        out = _apply(note, where=before)
        assert out["zones"]["living_zone"]["where"] == before, f"note={note!r} 動到了契約"
        assert not out["furniture_placement_rules"].get("no_large_furniture_zones"),             f"note={note!r} 憑空加了 no-go"


# ── 生成端：prompt 的深度鐵則不得跟契約打架 ────────────────────────
#
# 🔴 這一段是 c7b5bf6 自己挖出來的洞。那一刀把契約覆寫成
#    「客廳【不靠窗】…不得設在落地窗／採光窗前…」，而 prompt_builder 用的是
#    `k in living_where` 子字串比對——「不靠窗」裡面含有「靠窗」，於是契約說
#    不准靠窗、prompt 反而注入「sofa MUST be placed close to the window」。
#    判官那時已經改成否定優先、不會再擋 → 圖會過、位置照樣錯。
WINDOW_WHERE = "靠陽台採光落地窗前方的中深處地面與左側長牆區域"
DEPTH_RULE = "sofa MUST be placed close to the window"


def _layout_section(note):
    from prompt_builder import _build_layout_section
    z = {"zones": {"living_zone": {"where": WINDOW_WHERE}},
         "furniture_placement_rules": {}, "_origin": "user_confirmed_v2",
         "spatial_synthesis": {}}
    return _build_layout_section(api._apply_target_note_layout_constraints(
        z, note, "living", "unspecified"))


def test_prompt_does_not_force_window_side_for_a_negative_note():
    assert DEPTH_RULE not in _layout_section(NEG),         "契約說不准靠窗，prompt 仍注入「沙發必須靠窗」——兩邊打架"


@pytest.mark.parametrize("note", [POS, "", "喜歡淺木色"])
def test_prompt_keeps_window_rule_for_everyone_else(note):
    """肯定句與沒備註的單，原有的靠窗深度鐵則一個字都不能少。"""
    assert DEPTH_RULE in _layout_section(note), f"note={note!r} 的靠窗鐵則被誤刪"


def test_prompt_side_uses_the_shared_helper():
    src = (Path(__file__).resolve().parent / "prompt_builder.py").read_text(encoding="utf-8")
    i = src.index("is_window_side = ")
    assert "note_forbids_window_side" in src[i - 700:i + 200],         "prompt_builder 的 is_window_side 沒接上否定判準"


# ── 選圖：否定句不得被當成「靠窗主圖」最強信號 ──────────────────────
def test_photo_scoring_does_not_boost_a_negative_note():
    neg = api._score_photo_for_room({"target_note": NEG, "photo_contains": ["living"]}, "living")
    pos = api._score_photo_for_room({"target_note": POS, "photo_contains": ["living"]}, "living")
    assert pos - neg >= 100, f"否定句仍拿到靠窗加分（neg={neg} pos={pos}）"


# ── 全案備註（上傳頁最顯眼那個框）也要有配置效力 ──────────────────
#
# 🔴 全案備註 customer_notes 長期【沒有配置效力】：它進得了生成 prompt，但被
#    _NOTES_WRAPPER_PREFIX 明文關掉——「只當風格偏好、不准搬客廳」（防 prompt
#    injection）。真正有效力的是逐張 target_note，卻收在「＋ 補充說明」按鈕後面。
#    客戶把在意的事寫在看得見的框裡，系統不但不聽，還告訴模型不准聽。
#
# ⚠️ 開放的只有【已驗證的句型】。白名單之外的自由文字完全不變，仍走 wrapper
#    只當風格偏好——防注入沒有被放寬。

def test_global_note_is_used_when_the_photo_note_has_no_pattern():
    note, src = api._effective_layout_note("喜歡淺木色", "客廳不要靠窗")
    assert (note, src) == ("客廳不要靠窗", "global")


def test_photo_note_wins_on_conflict():
    """逐張寫「不靠窗」、大框寫「靠窗」→ 用逐張。"""
    note, src = api._effective_layout_note(NEG, POS)
    assert (note, src) == (NEG, "photo")


def test_the_two_notes_are_never_concatenated():
    """🔴 接成一個字串會互相污染：「不靠窗」+「靠窗」的子字串判斷會打架。"""
    note, _ = api._effective_layout_note(NEG, POS)
    assert POS not in note or note == NEG


def test_global_note_about_another_room_does_not_touch_the_living_contract():
    """「主臥不靠窗」不得改到客廳。"""
    note, src = api._effective_layout_note("", "主臥不靠窗")
    assert src == "photo" and note == ""


def test_free_text_still_has_no_layout_power():
    """沒命中句型的自由文字維持原行為，繼續只當風格偏好。"""
    for g in ("喜歡淺木色", "不要紅色", "預算抓緊一點", ""):
        note, src = api._effective_layout_note("", g)
        assert src == "photo" and note == "", f"自由文字「{g}」不該取得配置效力"


def test_judge_sees_the_flag_when_the_ban_came_from_the_global_note():
    """🔴 禁令來自全案備註時 target_note 是空的——判官只看 target_note 會被
    契約覆寫文字裡的「靠窗」子字串反咬，契約禁窗端、判官逼靠窗。"""
    z = {"zones": {"living_zone": {"where": WINDOW_WHERE}},
         "furniture_placement_rules": {}, "_origin": "user_confirmed_v2"}
    z = api._apply_target_note_layout_constraints(z, "客廳不要靠窗", "living", None)
    lz = z["zones"]["living_zone"]
    assert lz.get("_user_forbids_window_side") is True
    src = (Path(__file__).resolve().parent / "api.py").read_text(encoding="utf-8")
    assert '"user_forbids_window_side"' in src, "layout_ctx 沒把旗標帶給判官"
    g = (Path(__file__).resolve().parent / "gemini_analyze.py").read_text(encoding="utf-8")
    i = g.index("is_window_side = (not _forbids_window)")
    assert "user_forbids_window_side" in g[i - 500:i], "判官沒有讀旗標"


def test_global_note_does_not_touch_photo_scoring():
    """⚠️ 選圖的 +100 是「這張照片在講靠窗」。全案備註不是某一張的屬性，
    拿去加分會選錯底圖——所以 _score_photo_for_room 只准看逐張 target_note。"""
    src = (Path(__file__).resolve().parent / "api.py").read_text(encoding="utf-8")
    i = src.index("def _score_photo_for_room")
    seg = src[i:i + 2000]
    assert "customer_notes" not in seg and "_effective_layout_note" not in seg, \
        "選圖吃到了全案備註"


def test_upload_copy_only_promises_what_the_backend_honours():
    """上傳頁文案不得承諾後端做不到的事（舊文案寫「哪面牆不要擋」都會生效）。"""
    html = (Path(__file__).resolve().parent.parent / "upload.html").read_text(encoding="utf-8")
    i = html.index('class="notes-hint"')
    seg = html[i:i + 400]
    for phrase in ("客廳不靠窗", "餐廳中段"):
        assert phrase in seg, f"文案沒有示範會生效的講法：{phrase}"
    assert "哪面牆不要擋" not in seg, "文案仍在承諾沒有實作的能力"

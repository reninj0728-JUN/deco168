# -*- coding: utf-8 -*-
"""判官重新規劃這條路必須留下紀錄（E64D1C31 盲區）。

症狀：同樣的幾何、同樣的程式、同樣的參數，離線 build_s2_plan 得到 2 個合格，
線上卻是 0 個合格 BLOCKED——而 verify_and_replan_s2 的六個提前 return
一個都不留紀錄，事後完全查不出是哪條路擋的（連猜三次假設全被推翻）。

這裡鎖住：①每個 return 都標了分支 ②三個欄位真的寫進訂單 ③_diag 看得到。
純可觀測性，不改任何判定行為。
"""
import ast
import inspect
from pathlib import Path

import api
import layout_geometry_verifier_s2 as lgvs2

EXPECTED_BRANCHES = {
    "no_transverse_reference",              # 地板橫軸基準拿不到
    "replan_not_eligible",                  # 帶基準重新規劃後不合格 ← E64D1C31 疑似走這條
    "verifier_exception",                   # 判官例外
    "verified_pass",                        # 通過
    "opposite_candidate_recheck_blocked",   # 相反側候選複驗仍不安全
    "verdict_blocked",                      # 判官判不安全
}


def _returned_dicts(fn):
    """抓出函式裡所有 `return {...}` 的字典字面值。"""
    src = inspect.getsource(fn)
    tree = ast.parse("".join(src.splitlines(keepends=True)).lstrip())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            vals = {}
            for k, v in zip(node.value.keys, node.value.values):
                if (isinstance(k, ast.Constant) and k.value == "exit_branch"
                        and isinstance(v, ast.Constant)):
                    vals["exit_branch"] = v.value
            out.append((keys, vals))
    return out


def test_every_verifier_return_is_labelled():
    """六個提前 return 每一個都要標分支——漏一個就等於留一個看不見的洞。"""
    rets = [r for r in _returned_dicts(lgvs2.verify_and_replan_s2) if "plan" in r[0]]
    assert len(rets) >= 6, f"預期至少 6 個 return，實得 {len(rets)}"
    for keys, vals in rets:
        assert "exit_branch" in keys, f"有 return 沒標 exit_branch：keys={keys}"
    labels = {v["exit_branch"] for _, v in rets if "exit_branch" in v}
    assert labels == EXPECTED_BRANCHES, f"分支名不符：{labels ^ EXPECTED_BRANCHES}"


def test_observability_fields_reach_the_persisted_summary():
    """三個欄位必須寫進訂單，否則跟今天一樣：記了也看不到。"""
    src = inspect.getsource(api._run_layout_contract_s2)
    for field in ("plan_eligible_before_verifier",
                  "verifier_exit_branch",
                  "plan_overwritten_by_verifier"):
        assert src.count(f'"{field}"') >= 2, \
            f"{field} 必須同時被寫入 artifacts 與 summary（才會進 result_json）"


def test_diag_surfaces_the_branch():
    """下一單失敗時，一行指令就看得到是哪條分支擋的。"""
    diag = (Path(api.__file__).parent / "_diag.py").read_text(encoding="utf-8")
    assert "verifier_exit_branch" in diag
    assert "plan_overwritten_by_verifier" in diag


def test_observability_does_not_change_any_verdict():
    """純可觀測性：不得動到任何判定門檻或閘門。"""
    import gemini_analyze as ga
    import layout_geometry_s2 as s2
    assert ga.DOOR_GAP_MIN_SOFA == 0.25
    assert ga.DOOR_GAP_MIN_FOCAL == 0.28
    assert api.PAIR_CENTER_EXTREME == 100
    assert s2.MIN_WALL_SPAN_T == 0.18

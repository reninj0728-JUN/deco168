"""
Step 1 unit tests — PhotoMeta v1 ingestion + validation + degradation.

Run:  python backend/test_photo_meta_v1.py
"""
import sys as _sys_for_encoding
try:
    _sys_for_encoding.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys_for_encoding.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 補:
"""

範圍:
  測試 _normalize_photo_meta_for_room (純函式, 不打 fal, 不改 DB).
  從 api.py 用 regex 抽函式 + exec, 避免 import 整檔拉 FastAPI dependency.

覆蓋 spec 內 5 個 test case:
  T1  老格式 (沒 photo_meta)                → 自動退化, 0 error
  T2  新格式 (完整 photo_meta)               → 通過, 寫入正確
  T3  target_zone 不在 photo_contains       → 400-equivalent
  T4  avoid_zones 包含 target_zone          → 400-equivalent
  T5  photo_meta.photo_key 不屬於 room      → 400-equivalent
額外:
  T6  非法 Zone enum                        → 400
  T7  非法 LocationHint enum                → 400
  T8  photo_contains 空 array               → 400
  T9  photo_meta 不是 list                  → 400
  T10 photo_keys 部分沒被 photo_meta 涵蓋   → 應補退化值
"""
import os
import re
import sys


BACKEND = os.path.dirname(os.path.abspath(__file__))
api_path = os.path.join(BACKEND, "api.py")
with open(api_path, "r", encoding="utf-8") as f:
    src = f.read()


# ─── 從 api.py 抽 vocabulary + helper (不 import 整檔) ───────────────
ns: dict = {}

# ZONE_ENUM
m = re.search(r"^ZONE_ENUM:.*?^\)", src, re.MULTILINE | re.DOTALL)
assert m, "ZONE_ENUM 抽不到"
exec(m.group(0), ns)

# LOCATION_HINT_ENUM
m = re.search(r"^LOCATION_HINT_ENUM:.*?^\)", src, re.MULTILINE | re.DOTALL)
assert m, "LOCATION_HINT_ENUM 抽不到"
exec(m.group(0), ns)

# ROOM_TYPE_TO_ZONE
m = re.search(r"^ROOM_TYPE_TO_ZONE:.*?^\}", src, re.MULTILINE | re.DOTALL)
assert m, "ROOM_TYPE_TO_ZONE 抽不到"
exec(m.group(0), ns)

# _normalize_photo_meta_for_room (停在下一個 top-level def / class)
m = re.search(
    r"^def _normalize_photo_meta_for_room.*?(?=^def |^class |^@app)",
    src, re.MULTILINE | re.DOTALL,
)
assert m, "_normalize_photo_meta_for_room 抽不到"
exec(m.group(0), ns)

ZONE_ENUM           = ns["ZONE_ENUM"]
LOCATION_HINT_ENUM  = ns["LOCATION_HINT_ENUM"]
ROOM_TYPE_TO_ZONE   = ns["ROOM_TYPE_TO_ZONE"]
_normalize          = ns["_normalize_photo_meta_for_room"]


# ─── 校驗常數值 (鎖死 spec) ─────────────────────────────────────────
EXPECTED_ZONES = ("living", "dining", "walkway", "entrance",
                  "kitchen", "bedroom", "study", "balcony", "other")
EXPECTED_HINTS = ("rear_near_window", "front_near_entrance",
                  "left_side", "right_side", "center", "unspecified")

vocab_ok = True
if tuple(ZONE_ENUM) != EXPECTED_ZONES:
    print(f"  FAIL vocab: ZONE_ENUM = {ZONE_ENUM}")
    vocab_ok = False
if tuple(LOCATION_HINT_ENUM) != EXPECTED_HINTS:
    print(f"  FAIL vocab: LOCATION_HINT_ENUM = {LOCATION_HINT_ENUM}")
    vocab_ok = False
if not vocab_ok:
    print("  vocab FAIL — abort tests")
    sys.exit(1)
print(f"  PASS vocab: 9 zones + 6 hints")


# ─── Test helpers ──────────────────────────────────────────────────
results: list[bool] = []


def assert_pass(name, room, expected_count=None, check_each=None):
    out, err = _normalize(room)
    if err:
        print(f"  FAIL {name}: 預期 PASS 但收到 err={err!r}")
        return False
    if expected_count is not None and len(out) != expected_count:
        print(f"  FAIL {name}: 期望 {expected_count} 筆, 收到 {len(out)} 筆")
        return False
    if check_each:
        for i, m in enumerate(out):
            ok, reason = check_each(m)
            if not ok:
                print(f"  FAIL {name}: out[{i}] {reason}, m={m}")
                return False
    print(f"  PASS {name}: out_len={len(out)}")
    return True


def assert_fail(name, room, err_must_contain):
    out, err = _normalize(room)
    if not err:
        print(f"  FAIL {name}: 預期 fail 但通過, out={out}")
        return False
    missing = [s for s in err_must_contain if s not in err]
    if missing:
        print(f"  FAIL {name}: err 缺關鍵字 {missing}, 實際 err={err!r}")
        return False
    print(f"  PASS {name}: err='{err[:80]}'")
    return True


# ─── T1: 老格式 (沒 photo_meta) → 退化 ────────────────────────────
print("\n[T1] 老格式無 photo_meta 自動退化")
results.append(assert_pass(
    "T1_no_photo_meta",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg", "uploads/X/photo_02.jpg"],
    },
    expected_count=2,
    check_each=lambda m: (
        (m["target_zone"] == "living"
         and m["target_location_hint"] == "unspecified"
         and m["photo_contains"] == ["living"]
         and m["avoid_zones"] == []),
        "退化值不符合預期",
    ),
))

# T1b: 不同 room_type → 退化值對應
print("\n[T1b] 各 room_type 退化")
for rt, expected_zone in [
    ("living_room", "living"),
    ("dining_room", "dining"),
    ("bedroom", "bedroom"),
    ("study_workspace", "study"),
    ("other_room", "other"),
    ("不在表內", "other"),  # 未知 → other
]:
    out, err = _normalize({"room_type": rt,
                            "photo_keys": ["uploads/X/photo_01.jpg"]})
    ok = (not err and len(out) == 1
          and out[0]["target_zone"] == expected_zone
          and out[0]["photo_contains"] == [expected_zone])
    print(f"  {'PASS' if ok else 'FAIL'} T1b_{rt}: target={out[0]['target_zone'] if out else '?'}")
    results.append(ok)


# ─── T2: 新格式 (完整 photo_meta) → 通過 ───────────────────────────
print("\n[T2] 完整 photo_meta 通過")
results.append(assert_pass(
    "T2_full",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/X/photo_01.jpg",
            "photo_contains":       ["living", "dining"],
            "target_zone":          "living",
            "target_location_hint": "rear_near_window",
            "avoid_zones":          ["dining"],
        }],
    },
    expected_count=1,
    check_each=lambda m: (
        (m["target_zone"] == "living"
         and m["target_location_hint"] == "rear_near_window"
         and m["photo_contains"] == ["living", "dining"]
         and m["avoid_zones"] == ["dining"]),
        "欄位值不符合送入值",
    ),
))


# ─── T3: target_zone 不在 photo_contains → 400 ───────────────────
print("\n[T3] target_zone ∉ photo_contains")
results.append(assert_fail(
    "T3_target_not_in_contains",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/X/photo_01.jpg",
            "photo_contains":       ["dining"],
            "target_zone":          "living",
            "target_location_hint": "unspecified",
            "avoid_zones":          [],
        }],
    },
    err_must_contain=["target_zone", "photo_contains"],
))


# ─── T4: avoid_zones 含 target_zone → 400 ─────────────────────────
print("\n[T4] avoid_zones ∋ target_zone")
results.append(assert_fail(
    "T4_avoid_contains_target",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/X/photo_01.jpg",
            "photo_contains":       ["living"],
            "target_zone":          "living",
            "target_location_hint": "unspecified",
            "avoid_zones":          ["living"],
        }],
    },
    err_must_contain=["avoid_zones", "target_zone"],
))


# ─── T5: photo_meta.photo_key 不屬於 room.photo_keys → 400 ────────
print("\n[T5] photo_meta.photo_key ∉ room.photo_keys")
results.append(assert_fail(
    "T5_photo_key_not_in_room",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/Y/photo_99.jpg",
            "photo_contains":       ["living"],
            "target_zone":          "living",
            "target_location_hint": "unspecified",
            "avoid_zones":          [],
        }],
    },
    err_must_contain=["photo_key", "不屬於"],
))


# ─── T6: 非法 Zone enum ────────────────────────────────────────────
print("\n[T6] 非法 Zone enum")
results.append(assert_fail(
    "T6_invalid_zone",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/X/photo_01.jpg",
            "photo_contains":       ["living", "garden"],   # garden 不在 enum
            "target_zone":          "living",
            "target_location_hint": "unspecified",
            "avoid_zones":          [],
        }],
    },
    err_must_contain=["photo_contains", "Zone", "garden"],
))


# ─── T7: 非法 LocationHint enum ───────────────────────────────────
print("\n[T7] 非法 LocationHint enum")
results.append(assert_fail(
    "T7_invalid_hint",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/X/photo_01.jpg",
            "photo_contains":       ["living"],
            "target_zone":          "living",
            "target_location_hint": "diagonal_back_left",  # 不在 enum
            "avoid_zones":          [],
        }],
    },
    err_must_contain=["target_location_hint", "diagonal_back_left"],
))


# ─── T8: photo_contains 空 array → 400 ───────────────────────────
print("\n[T8] photo_contains 空 array")
results.append(assert_fail(
    "T8_empty_contains",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": [{
            "photo_key":            "uploads/X/photo_01.jpg",
            "photo_contains":       [],
            "target_zone":          "living",
            "target_location_hint": "unspecified",
            "avoid_zones":          [],
        }],
    },
    err_must_contain=["photo_contains", "非空"],
))


# ─── T9: photo_meta 不是 list → 400 ──────────────────────────────
print("\n[T9] photo_meta 不是 array")
results.append(assert_fail(
    "T9_meta_not_list",
    {
        "room_type": "living_room",
        "photo_keys": ["uploads/X/photo_01.jpg"],
        "photo_meta": {"photo_key": "uploads/X/photo_01.jpg"},  # 是 dict 不是 list
    },
    err_must_contain=["photo_meta", "array"],
))


# ─── T10: 部分 photo_keys 沒被 photo_meta 涵蓋 → 補退化值 ───────
print("\n[T10] 部分 keys 未被 photo_meta 涵蓋, 應補退化")
out, err = _normalize({
    "room_type": "dining_room",
    "photo_keys": ["uploads/X/photo_01.jpg",
                    "uploads/X/photo_02.jpg",
                    "uploads/X/photo_03.jpg"],
    "photo_meta": [{
        "photo_key":            "uploads/X/photo_01.jpg",
        "photo_contains":       ["dining"],
        "target_zone":          "dining",
        "target_location_hint": "center",
        "avoid_zones":          [],
    }],
})
ok = (not err and len(out) == 3)
if ok:
    by_key = {m["photo_key"]: m for m in out}
    ok = ok and by_key["uploads/X/photo_01.jpg"]["target_location_hint"] == "center"
    ok = ok and by_key["uploads/X/photo_02.jpg"]["target_location_hint"] == "unspecified"
    ok = ok and by_key["uploads/X/photo_03.jpg"]["target_zone"] == "dining"  # 退化
print(f"  {'PASS' if ok else 'FAIL'} T10_partial_coverage: out_len={len(out)} err={err!r}")
results.append(ok)


# ─── Summary ───────────────────────────────────────────────────────
print(f"\n{'═' * 60}")
ok_cnt = sum(1 for x in results if x)
print(f"  {ok_cnt}/{len(results)} PASS")
print(f"  {'全部通過' if ok_cnt == len(results) else '有 FAIL 案例'}")
print(f"{'═' * 60}")
sys.exit(0 if ok_cnt == len(results) else 1)

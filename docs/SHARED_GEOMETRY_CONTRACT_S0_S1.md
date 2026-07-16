# Shared Geometry Contract｜S0 Mapping＋S1 Shadow Dual-Write

狀態｜S0、S1 implemented
規格權威｜`SHARED_GEOMETRY_CONTRACT_V1.md`＋`shared_geometry_contract_v1.schema.json`
規格基準｜`d89fd25`
Production交付｜仍為legacy fail-closed

---

## 1｜本階段邊界

S0、S1只做

1. 現行legacy欄位對照v1欄位與reason code
2. 建立唯一v1 writer `layout_contract_v1.build_layout_contract`
3. 在既有layout shadow內dual-write v1 Contract JSON
4. v1結果只進`validation_summary.layout_contract_shadow.items[].contract_v1`

本階段禁止

- v1 Contract控制guide／prompt／FAL／retry／validation／delivery
- legacy `safe_layout=true`升成v1 `SAFE_FOR_GENERATION`
- legacy bbox、side hint、`struct_keypoints`升成grant evidence
- 啟用paid gate三元AND
- 用S1輸出宣稱幾何問題已根治

---

## 2｜S0 legacy映射

| 現行來源 | v1位置 | S0／S1處理 | Hard gate資格 |
|---|---|---|---|
| 實際shadow照片路徑 | `source.photo_key` | 暫存canonical local key | 否，S2需接正式storage key |
| 實際shadow照片bytes | `source.sha256` | 串流SHA-256 | 可供binding，但不足以放行 |
| 實際照片尺寸 | `source.size` | PIL讀取width／height | 可供binding，但不足以放行 |
| pipeline `_vi` | `source.view_index` | 寫入當前living view index | 可供audit |
| `best_photo_index` | `legacy_inputs.best_photo_index` | 只留legacy audit | 不可單獨當photo binding |
| `image_paths[best_photo_index]` | binding 判定輸入 | 必須與`photo_path`為同一檔（`_zoning_bbox_matches_source`） | 通過才允許map bbox；缺`image_paths`一律未驗證 |
| `bbox_on_best_photo` | `geometry[].kind=legacy_*_bbox` | **僅**在`legacy_bbox_binding_verified=true`時映射；原始yxyx保留於extensions，v1 shape轉xyxy | 永遠INELIGIBLE，只供veto／audit |
| `sofa_side／tv_side` | `legacy_inputs.side_hints` | `authoritative=false` | 不可 |
| `struct_keypoints` | legacy prototype輸入 | S1不寫成v1 grant geometry | Production不可；fixture回歸only |
| prototype `candidates[]` | `legacy_inputs.legacy_shadow.candidates[]` | 精簡保存 | 不寫入v1 `candidates[]` |
| prototype `chosen` | `legacy_inputs.legacy_shadow.chosen` | audit only | 不可 |
| prototype `safe_layout` | `legacy_inputs.legacy_shadow.safe_layout` | audit only | 不可改v1 decision |
| prototype `disposition` | `legacy_inputs.legacy_shadow.disposition` | audit only | 不可改v1 disposition |
| `_layout_guide` | 未接v1 | S1保持legacy | 不產生ReconciliationReport |
| `_layout_guide_mode` | 未接v1 | S1保持legacy | 不可當Contract decision |
| `_auto_focal_side／_auto_can_float` | 未接v1 | S1保持legacy | 不可當Contract geometry |
| render `validation.hard_fail` | 未接v1 | 繼續legacy delivery truth | 未建立ValidationReport |
| render `validation` | 未接v1 | S1不轉寫 | 預留S5 |

---

## 3｜S1 writer輸出規則

`backend/layout_contract_v1.py`

固定輸出

```text
object_type = layout_contract
schema_version = 1.0.0-draft
version = 1
model_input = null
candidates = []
disposition = BLOCKED
blocked_reason_class = INSUFFICIENT_EVIDENCE
pre_generation_eligible = false
affects_delivery = false
```

固定缺口reason codes

```text
MISSING_DOOR_FLOOR_CONTACT
MISSING_ENTRANCE_LANDING
MISSING_WALKWAY_POLYGON
MISSING_LIVING_FLOOR
NO_USABLE_WALL
MISSING_WALL_PLANE_EVIDENCE
CANDIDATE_GEOMETRY_INCOMPLETE
```

沒有驗證legacy bbox與目前source photo綁定時先加

```text
MISSING_PHOTO_BINDING
```

驗證規則（硬）

```text
legacy_bbox_binding_verified =
  image_paths 非空
  AND best_photo_index 為合法整數
  AND abspath(photo_path) == abspath(image_paths[best_photo_index])
```

禁止

- 只有`user_zoning_v2`存在就當 verified
- 只有`best_photo_index`是 int 就 map bbox
- 缺`image_paths`時預設 true
- 在套用`best_photo_index`前filter空值或重排`image_paths`

沒有合法legacy門bbox時再加

```text
MISSING_DOOR
```

Legacy prototype即使回傳`safe_layout=true`，v1仍必須BLOCKED。

---

## 4｜Hash與immutability

- 每次build建立新的`contract_id`
- `contract_hash`為canonical JSON SHA-256
- hash計算排除`version_chain.contract_hash`本身
- JSON使用UTF-8、key排序、無多餘空白、禁止NaN
- Contract檔寫出後不回寫
- S1沒有replan；`parent_contract_id=null`、`replan_reason_codes=[]`

---

## 5｜正式接線點

既有入口

```text
api.py::_run_layout_contract_shadow
```

pipeline 呼叫時必須傳入

```text
image_paths = 本單解析後的照片列表
```

供 v1 判定 `photo_path` 是否為 zoning best photo。單元測試若要 map bbox，也必須顯式傳同一張檔。

輸出目錄

```text
{job_dir}/layout_contract_shadow/
```

同一view寫兩份檔

```text
contract_{job_id}_v{view}_{mode}.json       # legacy prototype
contract_v1_{job_id}_v{view}_{mode}.json    # Shared Geometry Contract v1
```

Result summary

```text
validation_summary.layout_contract_shadow.items[].contract_v1
```

Summary只含id、hash、path、disposition、pre-generation eligibility與`affects_delivery=false`，完整Contract留在獨立JSON。

---

## 6｜Rollback與故障隔離

| 開關／狀況 | 行為 |
|---|---|
| `LAYOUT_CONTRACT_SHADOW=0` | 關閉legacy＋v1整段shadow |
| `LAYOUT_CONTRACT_V1_SHADOW=0` | 只關v1 dual-write，legacy shadow照跑 |
| v1 builder例外 | `contract_v1.status=error`，legacy shadow照回`status=ok` |
| v1 JSON寫檔失敗 | 同上，不改delivery |
| v1 decision BLOCKED | 只記錄，不阻擋目前legacy生成 |

`LAYOUT_CONTRACT_V1_SHADOW`預設開啟，但永遠不影響交付。

---

## 7｜驗收

必過

- v1 Contract通過frozen JSON Schema
- source hash與實際照片一致
- contract hash可重算
- legacy safe不得promote
- v1 candidates保持空陣列
- v1 writer失敗不影響legacy shadow
- v1 feature flag可獨立rollback
- backend全測無回歸

S1完成只代表shadow writer已接線，不代表

- Contract可授權付費生成
- Contract已產生同源formal artifact
- Reconciliation已接線
- ValidationReport已接線
- 多房型false-safe已完成驗證

---

## 8｜下一階段入口

S2才開始建立Contract-native Must geometry producer

1. `door_floor_contact_edge`
2. `entrance_landing`
3. `living_floor`
4. `usable_wall_segment`
5. wall-plane三選一證據

在上述geometry具備合格source binding、transform、evidence與eligibility之前，v1 decision必須持續BLOCKED。

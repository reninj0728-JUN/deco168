# Shared Geometry Contract v1｜架構契約正文

狀態｜**FROZEN DRAFT**
適用範圍｜DECO168 客廳自動配置
基準版本｜`8bf4faa`
文件版本｜`1.0.0-draft`
本文件只定義規格，不代表 production 已實作 Contract v1。

---

## 0｜目前產品狀態

目前正式系統仍是 `pre-contract / legacy fail-closed`。

| 現行機制 | v1 前定位 |
|---|---|
| AI auto 沒有有效 `_layout_guide` → `LayoutPreflightBlocked` | Legacy 付費前止血，保留 |
| 紅／綠／藍 `_layout_guide` | 非 authoritative 提示層，不是 LayoutContract |
| `_proto_layout_contract.py` A／B／F | Shadow prototype，`affects_delivery=false` |
| `validation.hard_fail` | ContractValidationReport 的 legacy 雛形 |
| `safe_layout` | Deprecated 相容欄位；目前不可視為交付真相 |

已完成｜沒有可驗證 guide 時，不呼叫 FAL、不燒付費重試。
未完成｜自動產生可溯源牆／地幾何，並穩定決定沙發、TV與觀看軸。

---

## 1｜凍結憲法

### 1.1 單一配置真相源

只有 `build_layout_contract(...)` 可以決定

- 候選 A／B／F
- sofa footprint
- TV footprint與wall contact
- view axis
- orientation normals
- chosen candidate
- disposition
- `pre_generation_eligible`

Guide renderer、prompt builder、FAL caller、retry、validator只能讀Contract，不得重新sample或重算位置。

### 1.2 幾何必須可溯源

任何bbox、polygon、segment、axis或normal都必須帶

- source photo key與SHA-256
- source size
- coordinate space
- transform chain
- producer與版本
- evidence mode
- confidence
- visibility
- geometry validation
- eligibility policy結果

沒有provenance的幾何，不得進hard gate。

### 1.3 Fail-closed

以下任一成立，`pre_generation_eligible=false`

- 缺門或門來源不明
- 跨照片套座標
- source size不一致
- transform不完整／失效
- 沒有入口landing
- 沒有連續walkway
- 沒有living floor
- 沒有可用TV牆段
- 幾何invalid／ineligible
- 沒有可行候選
- Contract與正式guide／mask不一致

### 1.4 Contract不可原地改寫

`LayoutContract.geometry`與`LayoutContract.decision`建立後不可修改。

- 同Contract retry｜只能重做執行，不能換位置
- 需要replan｜建立新Contract版本，帶`parent_contract_id`與reason chain
- 禁止同一`contract_id`原地把A改成B、移動沙發或改TV牆

### 1.5 三層證據常備

每個客廳案件應可回放

1. Source evidence｜底圖＋門／牆／地板／走道證據
2. Model input evidence｜模型實際收到的房間底圖＋guide／mask＋有序輸入hash
3. Render evidence｜成品＋沙發／TV／門／走道偵測與violations

三層證據是內部稽核儀器，不進客戶UI。

---

## 2｜操作禁令

1. 禁止新增第二套位置決策者。新helper若產生沙發／TV位置，只能位於Contract Builder內；其他用途必須標`non_authoritative`或`debug_only`。
2. 禁止把`inferred`或模型自報高confidence直接promote成`hard_gate_eligible`。
3. 不調紅藍框比例來提高通過率。
4. 不縮門禁區製造假safe。
5. 不把TV邊緣帶或y-band升成硬真理。
6. Contract v1完成前，不把shadow升成交付gate。
7. Contract v1完成前，不接Seedream商業路徑。

> confidence是輸入特徵，不是放行權；放行權只看版本化Eligibility Policy。

---

## 3｜三個獨立物件

### 3.1 LayoutContract｜不可變

唯一writer｜`build_layout_contract`

包含

- source binding
- transform chain
- geometry evidence
- candidates
- decision
- version chain

只能主張生圖前資格，不得主張成品可交付。

### 3.2 ContractValidationReport｜每個render一份

唯一writer｜`validate_*`

包含

- contract／render binding
- observations
- violations
- `delivery_eligible`

Report可以指出違反Contract的哪一條，不得改Contract的位置。

### 3.3 ContractReconciliationReport｜Contract對正式artifact

唯一writer｜`reconcile_contract_and_guide`

包含

- Contract binding
- formal plan／guide／mask binding
- transform與hash一致性
- mismatches
- `consistent`
- `binding_eligible`

Reconciler只能比較，不得反推修改Contract decision。

---

## 4｜兩階段eligible

| 階段 | 唯一真相 | 意義 |
|---|---|---|
| 生圖前 | `LayoutContract.decision.pre_generation_eligible` ＋ Reconciliation `binding_eligible` | 是否允許呼叫付費模型 |
| 生圖後 | `ContractValidationReport.delivery_eligible` | 實際成品是否可交付 |

付費授權必須是

```text
paid_generation_authorized =
    contract.decision.pre_generation_eligible
    AND reconciliation.consistent
    AND reconciliation.binding_eligible
```

交付授權必須是

```text
delivery_authorized =
    paid_generation_authorized
    AND validation_report.delivery_eligible
```

`SAFE_FOR_GENERATION`只表示可以花錢，不代表成品一定可交。

---

## 5｜Evidence與Eligibility

### 5.1 evidence_mode

| 值 | 定義 | 預設hard-gate資格 |
|---|---|---|
| `observed` | 可見邊界可直接定位 | 仍需通過Policy |
| `inferred` | 遮擋、畫面外延伸或模型推估 | 預設不可promote |
| `manual_fixture` | 離線人工標註，只供回歸 | Production不可直接使用 |
| `missing` | 無證據 | 不可 |
| `invalid` | 來源、座標或幾何失效 | 不可 |

### 5.2 eligibility

每個幾何物件與最終decision都要有

```json
{
  "status": "ELIGIBLE | INELIGIBLE",
  "policy_version": "geometry-eligibility-v1",
  "passed_checks": [],
  "failed_checks": []
}
```

`hard_gate_eligible`若需供舊碼使用，只能是上述結構的衍生相容布林，不能成為另一個writer。

### 5.3 幾何合法性最低檢查

- source photo與hash存在
- coordinate space為已知枚舉
- 全部點在宣告空間範圍內
- polygon無自交
- polygon面積達最低門檻
- segment／normal非零長度
- transform chain連續
- crop／resize輸出尺寸一致
- geometry與model input綁定一致

---

## 6｜座標與Transform規範

### 6.1 v1 canonical點順序

Contract v1所有point一律使用

```text
[x, y]
```

Legacy `bbox_on_best_photo`使用

```text
[ymin, xmin, ymax, xmax]，0–1000 normalized
```

遷移時必須顯式轉換，禁止把legacy陣列直接冒充v1 polygon。

### 6.2 最小transform chain

| 步驟 | v1要求 |
|---|---|
| source → crop | 有裁切時Must |
| crop → guide／mask座標 | 有binding artifact時Must |
| guide／crop → model input resize／pad | Must；未知就不能claim pixel-perfect |
| model input artifact hash | Must |

任何未知步驟必須記

```json
{"type":"unknown","declared":false}
```

存在unknown transform時，受影響幾何不得`ELIGIBLE`。

### 6.3 `model_input`的精確定義

`LayoutContract.model_input`只代表承載空間幾何的房間底圖，也就是實際送進生成模型的裁切／resize後room image。

它不代表

- 最終render
- 家具商品參考圖
- guide／mask本身
- 整個FAL `image_urls`陣列

Guide／mask由`ContractReconciliationReport.formal_artifact`另外綁定。完整付費呼叫的有序輸入清單屬三層證據artifact，必須另外保存，但不得回寫Contract位置。

---

## 7｜六欄最小欄位表 A｜現有zoning可遷移

> 可遷移不代表可放行。Legacy Gemini bbox與side主要作veto、audit或候選hint。

| 欄位 | M／S | 唯一Producer | 證據門檻 | Consumers | 缺失／失效原因碼 |
|---|---|---|---|---|---|
| `source.photo_key` | Must | upload／room binding | 必須解析到實際送模底圖 | 全模組 | `MISSING_PHOTO_BINDING` |
| `source.sha256` | Must | image IO | 與底圖byte一致 | 全模組／audit | `PHOTO_HASH_MISMATCH` |
| `source.size` | Must | image IO | 與實檔一致 | transform／guide／validator | `SOURCE_SIZE_MISMATCH` |
| `source.view_index` | Should | selection layer | 只能輔助，不能取代photo key | audit | `MISSING_VIEW_INDEX` |
| `transforms[]` | Must | crop／input builder | chain連續、尺寸一致 | guide／model input／validator | `TRANSFORM_CHAIN_INCOMPLETE` |
| `coordinate_space` | Must | Contract Builder規範化 | 必須為固定枚舉 | 全模組 | `UNKNOWN_COORD_SPACE` |
| `legacy.entrance_side` | Must* | zoning v2 | side標籤；只能veto／敘事 | candidate粗篩／audit | `MISSING_DOOR_SIDE` |
| `legacy.door_bbox` | Must* | zoning v2 | source綁定正確；只可veto | geometry adapter／audit | `MISSING_DOOR` |
| `legacy.living_bbox` | Should | zoning v2 | AABB only | 候選粗篩 | `MISSING_LIVING_ZONE` |
| `legacy.walkway_bbox` | Should | zoning v2 | AABB only；只可veto | 候選粗篩 | `MISSING_WALKWAY_EVIDENCE` |
| `legacy.no_go_bbox` | Should | zoning v2 | AABB only；只可veto | 候選粗篩 | `MISSING_NO_GO_EVIDENCE` |
| `legacy.wall_inventory` | Should | zoning v2 | 文字語意，不是wall geometry | audit／structure measurer hint | `MISSING_WALL_INVENTORY` |
| `legacy.sofa_side` | Should | zoning v2 | AI建議，不是decision | candidate hint／reconciliation | `MISSING_LEGACY_SIDE_HINT` |
| `legacy.tv_side` | Should | zoning v2 | AI建議，不是decision | candidate hint／reconciliation | `MISSING_LEGACY_SIDE_HINT` |
| `legacy.confidence` | Should | zoning v2 | 只作feature | audit／eligibility input | `MISSING_LEGACY_CONFIDENCE` |

`Must*`｜要談auto配置時必須存在；缺失即BLOCKED，不得補預設。

---

## 8｜六欄最小欄位表 B｜v1必須新增量測

| 欄位 | M／S | 唯一Producer | 證據門檻 | Consumers | 缺失／失效原因碼 |
|---|---|---|---|---|---|
| `door.floor_contact_edge` | Must for grant | structure measurer | observed優先；inferred預設ineligible | builder／validator | `MISSING_DOOR_FLOOR_CONTACT` |
| `entrance_landing` polygon | Must for grant | structure measurer | floor-plane近似＋ELIGIBLE | builder／validator | `MISSING_ENTRANCE_LANDING` |
| `walkway` continuous polygon | Must for grant | structure measurer | 連續區，不是單一AABB | builder／validator | `MISSING_WALKWAY_POLYGON` |
| `living_floor` polygon | Must for all auto-safe | structure measurer | 可粗但必須可見、合法、ELIGIBLE | builder／validator | `MISSING_LIVING_FLOOR` |
| `usable_wall_segment[]` | Must for TV grant | structure measurer | 可驗證牆段 | builder／artifact renderer | `NO_USABLE_WALL` |
| `wall_floor_boundary` | Conditional Must | structure measurer | TV grant替代組之一 | builder | `MISSING_WALL_PLANE_EVIDENCE` |
| `verified_wall_plane` | Conditional Must | structure measurer | TV grant替代組之一 | builder | `MISSING_WALL_PLANE_EVIDENCE` |
| `wall_contact_edge_observed` | Conditional Must | structure measurer | TV grant替代組之一 | builder | `MISSING_WALL_PLANE_EVIDENCE` |
| `candidates[]` | Must | Contract Builder only | 每個含geometry refs、constraints與淘汰原因 | shadow／audit／decision | `NO_VIABLE_CANDIDATE` |
| `sofa_footprint` | Must for chosen | Contract Builder | 落在living floor且不撞禁區 | artifact／validator | `SOFA_OUTSIDE_FLOOR` |
| `tv_footprint` | Must for chosen | Contract Builder | 與wall contact一致 | artifact／validator | `TV_NOT_ON_WALL` |
| `tv_wall_contact` | Must for chosen | Contract Builder | 落在usable wall segment | artifact／validator | `TV_NOT_ON_WALL` |
| `view_axis` floor-plane | Must for chosen | Contract Builder | centers＋orientation normals一致 | prompt／validator | `VIEW_AXIS_INVALID` |
| `sofa_orientation` | Must for chosen | Contract Builder | 與view axis一致 | prompt／validator | `ORIENTATION_MISMATCH` |
| `tv_orientation` | Must for chosen | Contract Builder | 與view axis反向一致 | prompt／validator | `ORIENTATION_MISMATCH` |
| `decision.chosen_candidate_id` | Must when safe | Contract Builder only | 指向通過候選 | 全下游 | `NO_VIABLE_CANDIDATE` |
| `decision.candidate_type` | Must when safe | Contract Builder only | `A／B／F` | 全下游 | `NO_VIABLE_CANDIDATE` |
| `decision.disposition` | Must | Contract Builder only | `SAFE_FOR_GENERATION／BLOCKED` | 全下游 | reason codes |
| `decision.pre_generation_eligible` | Must | Contract Builder only | 所有Must＋候選gate合取 | paid gate | reason codes |
| `version_chain` | Must | Contract Builder only | hash／parent／policy完整 | retry／audit | `CONTRACT_VERSION_INVALID` |
| `eligibility` per geometry | Must | Eligibility Policy | 版本化合取規則 | decision | `GEOM_NOT_ELIGIBLE` |

### 8.1 TV_GRANT固定條件

```text
usable_wall_segment
AND (
    wall_floor_boundary
    OR verified_wall_plane
    OR wall_contact_edge_observed
)
```

禁止使用未枚舉的「其他等效證據」。新增替代項必須提升`policy_version`。

### 8.2 F候選額外條件

F是privileged candidate，不是`free`模式預設。

除A／B共同Must外，F還要求

- 空間尺度可信
- living floor完整度更高
- 四周淨空可驗證
- sofa完整footprint可見
- orientation可驗證

任一不足｜`FLOAT_NOT_PROVEN`。

### 8.3 Candidate check與failure code分離

Candidate constraint固定使用正向`check_code`

```text
PHOTO_BINDING_VALID
TRANSFORM_CHAIN_VALID
DOOR_FLOOR_CONTACT_VALID
ENTRANCE_LANDING_CLEAR
WALKWAY_CLEAR
LIVING_FLOOR_VALID
USABLE_WALL_VALID
WALL_PLANE_EVIDENCE_VALID
SOFA_INSIDE_LIVING_FLOOR
TV_ON_USABLE_WALL
VIEW_AXIS_VALID
VIEW_AXIS_CLEAR_OF_DOOR
ORIENTATIONS_ALIGNED
FLOAT_PROVEN
```

每筆constraint包含

```text
check_code
status = PASS／FAIL／UNKNOWN
failure_code = null when PASS；reason code when FAIL／UNKNOWN
geometry_ids[]
```

禁止用`WALKWAY_COLLISION + PASS`這類正反語意不清的資料形狀。

---

## 9｜六欄最小欄位表 C｜Validation Report

| 欄位 | M／S | 唯一Producer | 證據門檻 | Consumers | 缺失／失效原因碼 |
|---|---|---|---|---|---|
| `report_id` | Must | validator | 唯一 | audit／delivery | `VALIDATION_REPORT_INVALID` |
| `contract_id／hash` | Must | validator binding | 必須對到不可變Contract | audit／retry | `CONTRACT_BINDING_MISMATCH` |
| `render_id／hash` | Must | render IO | 與實際成品一致 | audit／delivery | `RENDER_HASH_MISMATCH` |
| `validation_policy_version` | Must | validator | 固定版本化規則 | audit／replay | `VALIDATION_REPORT_INVALID` |
| `observations[]` | Must | validator only | 每項PASS／FAIL／UNKNOWN | audit／retry | `VALIDATION_UNAVAILABLE` |
| `violations[]` | Must | validator only | clause與reason code可機讀 | retry／delivery | violation codes |
| `delivery_eligible` | Must | validator only | 所有delivery hard gates合取 | delivery gate | violation codes |
| `legacy_projection.hard_fail` | Should | compatibility adapter | 由delivery結果衍生 | 舊碼 | — |

`delivery_eligible=true`時，以下12項validation check必須全部PASS

```text
CONTRACT_BINDING_VALID
RENDER_GEOMETRY_AVAILABLE
DOOR_PRESERVED
PASSAGE_PRESERVED
CAMERA_AXIS_PRESERVED
WALLS_PRESERVED
WINDOWS_PRESERVED
WALKWAY_CLEAR
DOOR_CLEAR
SOFA_ENTRANCE_RELATION_VALID
SOFA_TV_AXIS_VALID
TV_WALL_CONTACT_VALID
```

只有沒有HARD violation不夠；缺任何Must observation都不能交付。

Validator不得在Report寫「沙發應改左邊」。它只能說哪條Contract被違反；重新配置由新Contract版本處理。

---

## 10｜六欄最小欄位表 D｜Reconciliation Report

| 欄位 | M／S | 唯一Producer | 證據門檻 | Consumers | 缺失／失效原因碼 |
|---|---|---|---|---|---|
| `report_id` | Must | reconciler | 唯一 | paid gate／audit | `RECONCILIATION_REPORT_INVALID` |
| `contract_id／hash` | Must | reconciler binding | 對到不可變Contract | paid gate | `CONTRACT_BINDING_MISMATCH` |
| `formal_artifact id／hash` | Must | artifact IO | guide／mask／plan實體存在 | paid gate／audit | `GUIDE_MISSING` |
| `formal_plan_hash` | Must | formal planner | 位置由Contract render，不重算 | reconciler | `FORMAL_PLAN_MISMATCH` |
| `reconciliation_policy_version` | Must | reconciler | 固定版本化比對規則 | audit／replay | `RECONCILIATION_REPORT_INVALID` |
| `consistent` | Must | reconciler only | Contract與artifact同源 | paid gate | `GUIDE_CONTRACT_MISMATCH` |
| `mismatches[]` | Must | reconciler only | 機器碼＋細節 | audit | reconciliation codes |
| `binding_eligible` | Must | reconciler only | transform／hash／位置全過 | paid gate | reconciliation codes |

過渡期若正式仍是legacy紅藍guide，Reconciler比的是「Contract decision與guide rectangles是否同源」，不是把legacy guide升成Contract。

---

## 11｜現有zoning遷移盤點

| Legacy來源 | 真實語意 | v1去向 | 遷移政策 |
|---|---|---|---|
| `zoning_v2.best_photo_index` | 多圖陣列索引 | `source.view_index` | 不能單獨當binding；必須解析photo key＋hash |
| `_zoning_best_photo_index` | flatten後索引 | audit metadata | 同上 |
| `rooms[].photo_keys`／PhotoMeta `photo_key` | 真實上傳物件key | `source.photo_key` | 可作binding起點，仍需hash |
| `bbox_on_best_photo` | 0–1000 `[ymin,xmin,ymax,xmax]` | legacy geometry evidence | 轉成v1 `[x,y]`；預設inferred／veto-only |
| `existing_zones.entrance_zone` | 門／玄關粗區 | legacy door evidence | 不可單獨grant safe |
| `existing_zones.walkway` | 走道粗AABB | legacy walkway veto | 不可冒充continuous polygon |
| `proposed_zones.living_zone` | AI建議客廳區 | legacy candidate hint | 不可冒充living floor |
| `no_large_furniture_zone`／`no_go_zone` | 粗禁放AABB | legacy veto | 不可冒充door swing／landing |
| `spatial_synthesis.wall_inventory` | 牆面文字描述 | structure hint／audit | `has_opening`不是wall geometry |
| `sofa_side／tv_side` | Gemini位置建議 | candidate hint | 不可寫decision |
| `sofa_side_confidence` | 模型自報信心 | eligibility feature | 不能promote |
| `struct_keypoints` | 目前多為fixture／選填 | manual fixture evidence | production hard gate不可直接使用 |
| `_auto_focal_side` | legacy planner決策 | reconciliation input | 非authoritative |
| `_auto_can_float` | legacy啟發式 | reconciliation input | 不可直接建立F |
| `_layout_guide` | 圖檔路徑 | formal artifact | 不是Contract geometry |
| `_layout_guide_mode` | legacy模式 | artifact metadata | 不是decision |
| `_layout_conservative` | legacy警告 | audit／blocked hint | 不可取代reason codes |
| prototype `safe_layout` | shadow合取結果 | deprecated alias | 最多映射pregen，不代表delivery |
| prototype `chosen／template` | shadow候選id | legacy reconciliation | 不可直接升正式choice |
| prototype `sofa_quad／tv_quad` | 2D推估梯形 | shadow geometry | 缺provenance／eligibility時不可grant |
| `crop_boxes[]` | 實際crop tuple | transform step | 可遷移，須綁source與output size |
| GPT `_gpt_image_size_for()` | 1536×1024等輸出選擇 | model-input transform metadata | 需補resize／pad是否由API執行 |
| `validation.hard_fail` | legacy交付硬傷 | compatibility projection | 由Report `delivery_eligible`反推 |
| `validation.render_bboxes` | 成品Gemini bbox | Report observations | 不得改Contract geometry |
| `needs_regen`／retry context | legacy retry訊號 | violation-driven retry | 只能引用failed clauses |

---

## 12｜唯一Writer與讀寫權限

| 物件／欄位 | 唯一Writer | 可讀者 | 禁止行為 |
|---|---|---|---|
| LayoutContract | `build_layout_contract` | 全下游 | 下游回寫position／decision |
| Contract geometry | Contract Builder | renderer／prompt／validator／audit | Validator修polygon |
| Contract decision | Contract Builder | paid gate／renderer／prompt／retry／audit | Retry原地A→B |
| Validation Report | `validate_*` | delivery／retry／audit | 寫建議位置並回寫Contract |
| Reconciliation Report | `reconcile_contract_and_guide` | paid gate／audit | 反推修改Contract |
| Guide／mask artifact | Contract artifact renderer | FAL／reconciler／audit | 自己sample沙發／TV |
| Prompt | Contract-aware prompt builder | FAL／audit | 自行發明left／right／center |

### 12.1 Version chain與hash規則

第一版Contract必須

```text
version = 1
parent_contract_id = null
replan_reason_codes = []
```

任何replan必須建立新的`contract_id`，並符合

```text
version >= 2
parent_contract_id = 前一版contract_id
replan_reason_codes = 觸發replan的機器原因碼，至少一個
```

`contract_hash`固定使用SHA-256，輸入為RFC 8785 JSON Canonicalization Scheme（JCS）序列化後的完整不可變Contract，但計算時排除`version_chain.contract_hash`欄位本身。

禁止

- 同一`contract_id`原地增加version
- parent缺失卻宣稱replan
- reason chain為空卻改配置
- 各服務用不同JSON排序方式重算hash

---

## 13｜候選與Decision規則

### 13.1 candidate_type

```text
A｜一種結構配置族
B｜另一種結構配置族
F｜有額外證據門檻的浮置配置族
```

A／B的實際左右不得硬編成永久語意；必須由來源幾何導出。

### 13.2 disposition

Contract v1只使用

```text
SAFE_FOR_GENERATION
BLOCKED
```

`BLOCKED`細節由

```text
blocked_reason_class
unsafe_codes[]
```

表達，不持續膨脹disposition枚舉。

### 13.3 BLOCKED分類

```text
INSUFFICIENT_EVIDENCE
NEEDS_ALTERNATE_ANGLE
INVALID_GEOMETRY
NO_VIABLE_LAYOUT
RECONCILIATION_REQUIRED
```

### 13.4 safe_layout相容欄位

若舊碼尚需`safe_layout`

```text
safe_layout = decision.pre_generation_eligible
```

它是deprecated projection，不可被前端或delivery當成品安全。

---

## 14｜Reason codes枚舉

### 14.1 Source／Provenance

- `MISSING_PHOTO_BINDING`
- `PHOTO_HASH_MISMATCH`
- `CROSS_PHOTO_COORDS`
- `SOURCE_SIZE_MISMATCH`
- `MISSING_VIEW_INDEX`
- `UNKNOWN_COORD_SPACE`
- `TRANSFORM_CHAIN_INCOMPLETE`
- `CROP_TRANSFORM_INVALID`
- `MODEL_INPUT_TRANSFORM_UNKNOWN`
- `MODEL_INPUT_HASH_MISMATCH`
- `MISSING_LEGACY_SIDE_HINT`
- `MISSING_LEGACY_CONFIDENCE`

### 14.2 Structure／Geometry

- `MISSING_DOOR`
- `MISSING_DOOR_SIDE`
- `MISSING_DOOR_FLOOR_CONTACT`
- `MISSING_ENTRANCE_LANDING`
- `MISSING_WALKWAY_EVIDENCE`
- `MISSING_WALKWAY_POLYGON`
- `MISSING_LIVING_ZONE`
- `MISSING_LIVING_FLOOR`
- `MISSING_NO_GO_EVIDENCE`
- `MISSING_WALL_INVENTORY`
- `NO_USABLE_WALL`
- `MISSING_WALL_PLANE_EVIDENCE`
- `LOW_CONFIDENCE_STRUCTURE`
- `GEOMETRY_INVALID`
- `GEOM_NOT_ELIGIBLE`

### 14.3 Candidate／Decision

- `NO_VIABLE_CANDIDATE`
- `CANDIDATE_GEOMETRY_INCOMPLETE`
- `WALKWAY_COLLISION`
- `ENTRANCE_LANDING_COLLISION`
- `DOOR_SWING_COLLISION`
- `SOFA_OUTSIDE_FLOOR`
- `TV_NOT_ON_WALL`
- `VIEW_AXIS_INVALID`
- `VIEW_AXIS_THROUGH_DOOR`
- `ORIENTATION_MISMATCH`
- `FLOAT_NOT_PROVEN`
- `CONTRACT_VERSION_INVALID`

### 14.4 Reconciliation

- `GUIDE_MISSING`
- `GUIDE_HASH_MISMATCH`
- `GUIDE_CONTRACT_MISMATCH`
- `GUIDE_TRANSFORM_INVALID`
- `FORMAL_PLAN_MISMATCH`
- `SHADOW_FORMAL_MISMATCH`
- `RECONCILIATION_REPORT_INVALID`

### 14.5 Render／Delivery

- `CONTRACT_BINDING_MISMATCH`
- `RENDER_HASH_MISMATCH`
- `VALIDATION_REPORT_INVALID`
- `VALIDATION_UNAVAILABLE`
- `RENDER_GEOMETRY_MISSING`
- `DOOR_REMOVED_OR_MOVED`
- `PASSAGE_NOT_PRESERVED`
- `CAMERA_AXIS_CHANGED`
- `WALLS_CHANGED`
- `WINDOWS_CHANGED`
- `FURNITURE_BLOCKS_WALKWAY`
- `FURNITURE_BLOCKS_DOOR`
- `SOFA_FACES_ENTRANCE`
- `SOFA_TV_AXIS_VIOLATION`
- `TV_WALL_CONTACT_VIOLATION`

Reason code只能增加於新policy／schema版本，不得把既有code改成不同語意。

---

## 15｜Retry與版本鏈

### 15.1 同Contract retry

只允許修執行違約

- 同一Contract重新呼叫模型
- 改善Contract文字表達
- 重新render同一份mask／guide
- 針對Validation violations要求模型遵守

不得改

- candidate
- sofa／TV位置
- view axis
- disposition

### 15.2 Replan

如果要換candidate或位置

1. 建立`contract_vN+1`
2. `parent_contract_id = contract_vN.contract_id`
3. 寫入replan reason codes
4. 重新產artifact
5. 重新Reconciliation
6. 通過後才能再次付費生成

---

## 16｜三層證據artifact

每個Contract版本至少保存

```text
source_overlay
contract_json
formal_plan_json
binding_guide_or_mask
model_input_image
reconciliation_report
render_image
validation_report
```

每個影像artifact保存

- SHA-256
- width／height
- coordinate space
- contract id／hash
- source photo key／hash
- producer version

---

## 17｜KPI與宣稱邊界

v1前期KPI順序

1. Contract自洽率
2. Verified golden false-safe率
3. Audited production sample false-safe率
4. Shadow vs formal一致率
5. 三層證據可回放率
6. Delivery正確率／封門率
7. 最後才看生成成功率

允許宣稱

```text
verified golden false-safe = 0
audited production sample false-safe = 0
unaudited fleet = unknown
```

禁止把有限fixture結果宣稱為全站false-safe等於0。

---

## 18｜Contract v1規格完成定義

每個欄位都必須回答

1. 誰產生？
2. 證據來自哪張圖、哪個transform？
3. 誰可讀、誰禁止寫？
4. 缺失／invalid時使用哪個reason code與disposition？
5. 影響pre-generation、delivery，還是audit only？

整份規格還必須證明

- A／B／F與disposition分離
- Contract與Validation Report分離
- Reconciliation不回寫Contract
- geometry／decision immutable
- retry replan產生新版本
- legacy bbox不會被promote成grant evidence
- 任一unknown transform都不能claim hard-gate eligible

### 18.1 JSON Schema之外的Semantic Validator

JSON Schema負責物件形狀與基本條件，但無法完整證明跨陣列引用與hash真實性。進Sprint前必須另定義semantic validator，至少檢查

- `contract_hash`由不可變Contract內容重算一致
- `chosen_candidate_id`確實存在於`candidates[]`
- `decision.candidate_type`與chosen candidate一致
- candidate引用的全部geometry id存在且kind正確
- geometry id與candidate id在同一Contract內唯一
- 每個geometry的`source_photo_key`等於Contract source，或有明確合法的多視角binding
- transform chain的`to_space`與下一步`from_space`連續
- model input size／hash與實際artifact一致
- ELIGIBLE candidate沒有FAIL或UNKNOWN constraint
- SAFE decision引用的Must geometry都是`available＋ELIGIBLE`
- Validation／Reconciliation的`contract_id＋contract_hash`同時吻合

任一semantic check失敗，整體視為`BLOCKED`；不得因JSON Schema通過就直接付費生成。

---

## 19｜實作前凍結範圍

在本文件與JSON Schema通過review前

- production不改Contract資料流
- 不重構zoning
- 不改現行guide比例
- 不改validator hard gate
- 不開始Seedream商業測試
- 保留`8bf4faa`的legacy fail-closed

本文件是規格基準，不是「已完成Shared Geometry Contract v1」的宣告。

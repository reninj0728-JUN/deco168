# Known Bugs

## [CLOSED 2026-06-04] furniture_match.py 客廳推薦含吧台椅

**Job:** 32E5CC13 modern
**原始現象:** 客廳家具清單推薦了「黑色金屬高腳吧台椅 (IKEA STIG, NT$499)」；nordic 風格 matched_furniture 甚至沒有沙發/地毯。

**Root cause:**
- 全分類混評分後「每類取 1」會被高分配件擠掉主家具
- `CATEGORY_ZH_TO_EN` 茶几 → 'table' 跟餐桌混 bucket
- 椅子沒細分，吧台椅/餐椅/單人椅都歸同類

**修復內容（2026-06-04）:**
- `furniture_match.py` 改兩階段配對（mode='living' 預設）：
  - Stage 1: MUST_HAVE [sofa, coffee_table, rug] 每類保證撈 1 件（同風格 → fallback 相近風格，category 鎖死）
  - Stage 2: NICE_TO_HAVE 補滿 top_n
  - 全程排除 EXCLUDED [bar_stool, dining_chair, dining_table, bed, bedding]
- `CATEGORY_ZH_TO_EN`：茶几獨立為 coffee_table；桌子保守維持 table
- 新增 `refine_subcategory()` 按 name_zh 細分椅子（bar_stool/dining_chair/accent_chair）與桌子（dining_table/side_table）
- 輸出新增 `category_en` 欄位反映細分結果
- 不動 catalog、api.py、test_full_pipeline.py、前端、所有 POC

**驗收:** `backend/test_furniture_match_fix.py` PASS
- Case A（空 prompt）& Case B（有 prompt）modern + nordic 4 組全 PASS
- 兩個風格都湊齊 sofa + coffee_table + rug，零吧台椅/餐椅/餐桌

---

## [OPEN 2026-06-05] Rug reference 顏色/花紋服從度低

**Job:** T5DEV01 nordic（正式 pipeline 跑出來）
**現象:** reference_map 給的 IKEA 地毯（黑色簡約圖案）→ render 出來是米白長毛地毯，顏色/花紋對不上。

**Root cause（推測）:**
- catalog 給的 nordic rug 高分選項本身偏深色/幾何，但 Nano Banana 收到 nordic 風格 prompt（cream / oak / soft natural light）後，自己把地毯渲成淺色
- Sofa 和茶几的顏色服從度都比地毯好，可能是地毯佔畫面比例大、模型優先讓地毯配合整體風格

**修法（待 phase 2）:**
- 方案 A: prompt_builder 加強：`The rug must EXACTLY match reference image N's color and pattern. Do not substitute the rug color to fit the style.`
- 方案 B: furniture_match nordic 模式優先挑「淺色/素色長毛」地毯，避免一開始就跟風格衝突
- 方案 C: 兩者並用

**優先順序:** 低（不擋上線，已列入 T5 PASS 報告 backlog）
**影響:** 購買清單地毯跟畫面看起來不像同一件；客戶可能困惑

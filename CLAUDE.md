# DECO168 — Claude Code 專案守則

AI 室內設計平台。用戶上傳空間照片 → Gemini 分析 → Flux 生成渲染圖 → 推薦真實家具。

---

## 部署架構

| 層 | 服務 | 觸發方式 |
|----|------|---------|
| 前端 | Vercel → `https://deco168.vercel.app` | push to master 自動部署 |
| 後端 | Railway → `https://deco168-production.up.railway.app` | push to master 自動部署 |
| 資料庫 | Supabase（orders 表 + renders storage） | API 呼叫 |
| Git | `reninj0728-JUN/deco168` master | 唯一主線，無分支 |

---

## API Keys — 絕對禁止進 git

- `GEMINI_API_KEY` 和 `FAL_KEY` 只能設在 Railway 環境變數
- `.env` 已在 `.gitignore`，永遠不 commit
- 任何 `.py` 檔禁止出現 hardcode 的 key 字串
- 如果發現 `os.environ['KEY'] = 'hardcoded_value'` 這種寫法，立刻移除

---

## Pipeline 流程（不要亂動順序）

```
照片上傳（/api/upload）
  → Gemini 分析空間（gemini_analyze.py / analyze_image）
  → 家具配對（furniture_match.py / enrich_renders）
  → Flux 生成渲染圖（test_full_pipeline.py / generate_renders）
  → 結果存 Supabase + 回傳前端
```

關鍵檔案：
- `backend/api.py` — FastAPI 主服務，Railway 跑這個
- `backend/test_full_pipeline.py` — pipeline 核心，api.py 會 import 它
- `backend/gemini_analyze.py` — Gemini system prompt + analyze_space()
- `backend/furniture_match.py` — 家具評分配對邏輯
- `backend/furniture_catalog.json` — 5018 件家具，9種風格

---

## 家具目錄規則

- 9 種風格：modern / nordic / japanese / muji / luxury / art-deco / boho / industrial / mediterranean
- 新增批次用 `catalog_batchN.py`，跑完確認 total 數字才算完成
- 用 `name_zh` 去重，不要手動改 JSON
- catalog_batch*.py 不需要 commit，只要 `furniture_catalog.json` 進 git

---

## 前端頁面

| 頁面 | 用途 |
|------|------|
| `index.html` | 首頁 |
| `upload.html` | 照片上傳 |
| `style-form.html` | 風格選擇 |
| `checkout.html` | 付款（MVP 假按鈕） |
| `generate.html` | 等待 AI 生成 |
| `result.html` | 顯示渲染圖 + 家具推薦 |

---

## 修改程式碼的規則

1. 改後端 → push 到 master → Railway 自動重啟（約 2 分鐘）
2. 改前端 → push 到 master → Vercel 自動部署（約 1 分鐘）
3. 確認 `/health` 回傳 `{"status":"ok"}` 才算 Railway 重啟完成
4. `furniture_catalog.json` 是大檔，push 前確認 item 數正確

## 不要做的事

- 不要新增 `.env` 以外的方式儲存 key
- 不要分支，直接 master
- 不要改 `furniture_catalog.json` 的 schema（id / name_zh / brand / category / style_tags / keywords / colors / price_twd / image_url / purchase_url / dimensions / flux_descriptor）
- 不要在 catalog_batch*.py 以外的地方批次新增家具

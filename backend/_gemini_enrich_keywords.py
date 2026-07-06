"""
全目錄 keywords 豐富化（純文字模式，不下載圖片）。

背景：score_item 的關鍵字比對是英文 flux_prompt 對商品 keywords/descriptor，
但目錄商品的 keywords 幾乎都是 1 個中文行銷詞（例如「輕奢沙發」），英文子句
永遠對不上 → 大量商品同分、配對只能靠風格分粗排。

做法：用 Gemini 文字模式（不用看圖，flux_descriptor 已經是看圖產生的視覺描述），
根據 name_zh + flux_descriptor + colors + category 幫每件商品產 8~12 個
「英文單字/短詞」關鍵字（材質/顏色/形狀/尺寸感/適用情境），append 進 keywords
（保留原有中文詞）。配合 furniture_match._prompt_word_overlap 的字詞級比對，
讓 Gemini 分析房間後產的 flux_prompt 能真正挑到「視覺上對的」商品。

一次 25 件批次送，3316 件約 133 次呼叫、10 分鐘內跑完。
用法：railway run python3 _gemini_enrich_keywords.py
"""
import json, os, sys, io, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

CATALOG_PATH = Path(__file__).parent / "furniture_catalog_real.json"
BATCH_SIZE = 25

PROMPT_HEADER = """你是室內設計 AI 配對系統的資料工程師。以下是家具商品清單（JSON），
每件有 id / 中文名 / 英文視覺描述 / 顏色 / 類目。

幫每件商品產生 8~12 個「英文」搜尋關鍵字（小寫），涵蓋：
- 材質（fabric/leather/oak/walnut/marble/rattan/metal/velvet...）
- 顏色（grey/beige/cream/white/black/brown...）
- 形狀與特徵（curved/tufted/low-profile/slatted/round/tapered legs...）
- 尺寸感（compact/oversized/two-seater/loveseat...）
- 風格語彙（minimalist/scandinavian/wabi-sabi/brass-accent...）

規則：
- 只能用單字或 2 個字的短詞（例如 "grey", "fabric sofa", "tapered legs"）
- 必須跟該商品的視覺描述一致，不可以編造商品沒有的特徵
- 回傳 JSON 陣列，每個元素 {"id": "...", "en_keywords": ["...", ...]}，不要多餘文字

商品清單：
"""


def enrich_batch(client, types, items: list) -> dict:
    payload = [{
        "id": it["id"],
        "name_zh": it.get("name_zh", "")[:60],
        "flux_descriptor": it.get("flux_descriptor", "")[:200],
        "colors": it.get("colors", []),
        "category": it.get("category", ""),
    } for it in items]
    resp = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=[PROMPT_HEADER + json.dumps(payload, ensure_ascii=False)],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    result = json.loads(resp.text)
    out = {}
    for row in result:
        if isinstance(row, dict) and row.get("id") and isinstance(row.get("en_keywords"), list):
            kws = [str(k).strip().lower() for k in row["en_keywords"]
                   if isinstance(k, str) and 0 < len(k.strip()) <= 30]
            if kws:
                out[row["id"]] = kws[:12]
    return out


def main():
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")
    if not api_key:
        print("找不到 GEMINI_API_KEY，中止")
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    # 只處理還沒有英文關鍵字的（可重跑續傳）
    def has_en(it):
        return any(k.isascii() for k in (it.get("keywords") or []))
    todo = [it for it in cat if not has_en(it)]
    print(f"目錄總數: {len(cat)}，待補英文關鍵字: {len(todo)}")

    by_id = {it["id"]: it for it in cat}
    done = 0
    failed_batches = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        try:
            result = enrich_batch(client, types, batch)
            for iid, en_kws in result.items():
                item = by_id.get(iid)
                if item is None:
                    continue
                old = [k for k in (item.get("keywords") or []) if k and not k.isascii()]
                item["keywords"] = old + en_kws
            done += len(result)
        except Exception as e:
            failed_batches += 1
            print(f"  批次 {i//BATCH_SIZE + 1} 失敗：{type(e).__name__}: {str(e)[:100]}")
            if "RESOURCE_EXHAUSTED" in str(e):
                print("  Gemini 額度用完，提前收工（已完成部分照常寫回）")
                break
        if (i // BATCH_SIZE + 1) % 10 == 0:
            print(f"  進度: {min(i + BATCH_SIZE, len(todo))}/{len(todo)}（成功 {done}）")
            # 每 10 批寫一次，中途死掉不用全部重來
            CATALOG_PATH.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.3)

    CATALOG_PATH.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成。成功補 {done} 件英文關鍵字，失敗批次 {failed_batches}")


if __name__ == "__main__":
    main()

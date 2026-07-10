"""
幫這次新加的 585 件商品（style_tags 是關鍵字猜的、flux_descriptor 是空的）
補跑 Gemini 視覺分類，看真實商品圖判斷風格/顏色/類目/英文描述，跟舊版
2,731 件的標準看齊。

只更新 style_tags / colors / category / dimensions / flux_descriptor 這幾個
欄位；id / image_url / purchase_url / price_twd / source 保持不變（那些
已經是真實爬蟲資料，不需要重新判斷）。

跟 scraper_momo.py 的 CLASSIFY_PROMPT 幾乎一樣，但風格清單改成「現在網站
實際在賣的 9 種」（modern/cream/nordic/japanese/wood/luxury/french/muji/
chinese-modern），不是舊版那個已經沒在用的 art-deco/boho/industrial/
mediterranean 清單——用舊清單分類，法式/新中式的商品會被硬塞進不相關的
風格桶。

用法：python3 _gemini_classify_new.py
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

VALID_STYLES = ["modern", "cream", "nordic", "japanese", "wood", "luxury",
                "french", "muji", "chinese-modern"]

CLASSIFY_PROMPT = """你是專業室內設計師，請仔細分析這張傢俱產品圖。

回傳 JSON（嚴格照格式，不要多餘文字）：
{
  "style_tags": ["最符合的風格"],
  "colors": ["主色1","主色2"],
  "category": "沙發/床架/桌子/椅子/茶几/收納/燈具/地毯/窗簾/裝飾/抱枕/寢具/傢俱 之一",
  "dimensions": "若圖中可見尺寸標示則填入，否則留空",
  "flux_descriptor": "英文，描述此傢俱給AI圖像生成用，30字以內，包含材質顏色形狀"
}

style_tags 只能填一個，從以下選擇：
modern / cream / nordic / japanese / wood / luxury / french / muji / chinese-modern

判斷標準：
- modern：線條簡潔、灰白黑色系、金屬腳
- cream：奶油白、米色系、柔和溫暖、圓潤造型
- nordic：原木、白色、簡約溫暖
- japanese：低矮、原木、禪意、侘寂
- wood：大量原木質感、自然療癒、木紋明顯
- luxury：大理石、金屬邊框（金/黃銅）、絲絨、輕奢華麗
- french：弧線曲線、雕花、蕾絲/荷葉邊、浪漫柔美、香檳/奶油粉色系
- muji：棉麻、原木、無印良品極簡感
- chinese-modern：東方元素（格柵/圈椅/如意紋）配現代簡約線條

若商品明顯不是家具本體（是布套/坐墊/桌布/被套等配件耗材），style_tags
仍照常填一個最相關的風格，category 填「寢具」或「裝飾」，不要填家具本體
類目（這是為了避免配件被誤當家具主體用於配對）。"""


def _parse_classify_json(text: str) -> dict:
    """Gemini 偶爾吐 markdown 圍欄、未跳脫引號或尾註——多層 fallback。"""
    import re
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty response")

    def _try_load(s: str) -> dict | None:
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    obj = _try_load(raw)
    if obj:
        return obj

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        obj = _try_load(m.group(1))
        if obj:
            return obj

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        blob = raw[start : end + 1]
        obj = _try_load(blob)
        if obj:
            return obj
        blob2 = re.sub(r",\s*([}\]])", r"\1", blob)
        obj = _try_load(blob2)
        if obj:
            return obj

    # 欄位級 regex fallback（flux_descriptor 常有未跳脫引號弄壞 JSON）
    def _field(name: str) -> str | None:
        mm = re.search(
            rf'"{name}"\s*:\s*"((?:\\.|[^"\\])*)"',
            raw,
            re.DOTALL,
        )
        if mm:
            return mm.group(1).replace('\\"', '"').replace("\\n", " ").strip()
        # 寬鬆：取到下一個 ", 或行尾
        mm = re.search(rf'"{name}"\s*:\s*"([^"]*)"', raw)
        return mm.group(1).strip() if mm else None

    def _list_field(name: str) -> list[str]:
        mm = re.search(rf'"{name}"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
        if not mm:
            return []
        return [x.strip().strip('"').strip("'") for x in re.findall(r'"([^"]+)"', mm.group(1))]

    style_tags = _list_field("style_tags")
    colors = _list_field("colors")
    category = _field("category") or ""
    dimensions = _field("dimensions") or ""
    flux = _field("flux_descriptor") or ""
    if not (style_tags or category or flux):
        raise ValueError(f"no JSON object in: {raw[:120]!r}")
    return {
        "style_tags": style_tags,
        "colors": colors,
        "category": category,
        "dimensions": dimensions,
        "flux_descriptor": flux,
    }


def _fetch_image_bytes(url: str) -> bytes:
    """下載商品圖。momoshop 部分圖床（i1/i2/i3.momoshop.com.tw）憑證缺
    Subject Key Identifier，新版 OpenSSL 直接拒絕——僅在這種情況下對
    公開商品圖片改用 verify=False 重試（一次性分類用，不碰正式 api.py 流程）。"""
    import requests
    import urllib3
    try:
        resp = requests.get(url, timeout=20)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, timeout=20, verify=False)
    if not resp.ok:
        raise RuntimeError(f"圖片下載失敗 HTTP {resp.status_code}")
    return resp.content


def _log(msg: str) -> None:
    print(msg, flush=True)


def _save_catalog(items: list) -> None:
    CATALOG_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def classify_batch(catalog: list, to_classify: list) -> tuple[int, int]:
    """In-place update catalog by id; checkpoint every 20 successes/attempts."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")
    if not api_key:
        _log("找不到 GEMINI_API_KEY，中止")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    by_id = {it["id"]: i for i, it in enumerate(catalog) if it.get("id")}
    failed = 0
    done = 0

    for i, item in enumerate(to_classify):
        label = (item.get("name_zh") or "")[:25]
        item_id = item.get("id")
        try:
            img_bytes = _fetch_image_bytes(item["image_url"])
            mime = "image/webp" if item["image_url"].lower().endswith(".webp") else "image/jpeg"

            resp = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=[
                    types.Part.from_bytes(data=img_bytes, mime_type=mime),
                    CLASSIFY_PROMPT,
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            result = _parse_classify_json(getattr(resp, "text", None) or "")

            style = (result.get("style_tags") or [None])[0]
            if style not in VALID_STYLES:
                # Gemini 給的風格不在現行 9 種清單內 → 保留原本關鍵字搜尋時的猜測，
                # 不要硬塞成錯的風格
                style = (item.get("style_tags") or ["modern"])[0]

            new_item = dict(item)
            new_item["style_tags"] = [style]
            if result.get("colors"):
                new_item["colors"] = result["colors"]
            if result.get("category"):
                new_item["category"] = result["category"]
            if result.get("dimensions"):
                new_item["dimensions"] = result["dimensions"]
            if result.get("flux_descriptor"):
                new_item["flux_descriptor"] = result["flux_descriptor"]
            # 保證至少有 descriptor，避免重跑時無限重試同一件
            if not new_item.get("flux_descriptor"):
                new_item["flux_descriptor"] = (new_item.get("name_zh") or "furniture")[:40]

            if item_id in by_id:
                catalog[by_id[item_id]] = new_item
            done += 1

        except Exception as e:
            _log(f"  [{i+1}/{len(to_classify)}] 失敗（{label}）：{type(e).__name__}: {str(e)[:80]}")
            failed += 1
            # 失敗不寫 descriptor → 可重跑

        if (i + 1) % 20 == 0 or (i + 1) == len(to_classify):
            _save_catalog(catalog)
            _log(f"  進度: {i+1}/{len(to_classify)} ok≈{done} fail={failed}（已 checkpoint）")
        time.sleep(0.35)

    return done, failed


def main():
    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    to_classify = [it for it in cat if not it.get("flux_descriptor")]
    _log(f"目錄總數: {len(cat)}，待分類: {len(to_classify)}")
    if not to_classify:
        _log("沒有待分類項目")
        return

    done, failed = classify_batch(cat, to_classify)
    _save_catalog(cat)

    from collections import Counter
    style_dist = Counter(it["style_tags"][0] for it in cat if it.get("style_tags"))
    still = sum(1 for it in cat if not it.get("flux_descriptor"))
    _log(f"\n完成。成功≈{done}，失敗 {failed}，仍無 descriptor {still}")
    _log("全目錄風格分佈: " + str(dict(style_dist)))


if __name__ == "__main__":
    main()


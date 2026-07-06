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


def classify_batch(items: list) -> tuple[list, int]:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")
    if not api_key:
        print("找不到 GEMINI_API_KEY，中止")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    updated = []
    failed = 0

    for i, item in enumerate(items):
        label = (item.get("name_zh") or "")[:25]
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
            result = json.loads(resp.text)

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
            updated.append(new_item)

        except Exception as e:
            print(f"  [{i+1}/{len(items)}] 失敗（{label}）：{type(e).__name__}: {str(e)[:80]}")
            updated.append(item)  # 保留原樣，不遺失資料
            failed += 1
            continue

        if (i + 1) % 20 == 0:
            print(f"  進度: {i+1}/{len(items)}（失敗 {failed}）")
        time.sleep(0.4)

    return updated, failed


def main():
    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    to_classify = [it for it in cat if not it.get("flux_descriptor")]
    print(f"目錄總數: {len(cat)}，待分類: {len(to_classify)}")

    updated, failed = classify_batch(to_classify)
    updated_by_id = {it["id"]: it for it in updated}

    final = [updated_by_id.get(it["id"], it) for it in cat]

    from collections import Counter
    style_dist = Counter(it["style_tags"][0] for it in final if it.get("style_tags"))

    CATALOG_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成。分類 {len(to_classify)} 件，失敗 {failed} 件（失敗的保留原分類，未遺失）")
    print("全目錄風格分佈:", dict(style_dist))


if __name__ == "__main__":
    main()

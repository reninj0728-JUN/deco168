"""
重試 _gemini_reclassify_legacy.py 這批裡因為 momoshop 圖床 SSL 連線問題失敗、
被迫退回 modern 的 22 件商品。每件圖片下載失敗時多重試 3 次（間隔漸增），
盡量避免再次因為暫時性連線問題被迫用假分類。

用法：python3 _retry_ssl_failed.py
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
RETRY_IDS = json.loads((Path(__file__).parent / "_retry_ids.json").read_text(encoding="utf-8"))

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

style_tags 只能填一個，從以下選擇（這是網站現行實際在賣的風格，只能選這些，
不可以填其他風格例如 industrial/boho/mediterranean/art-deco，這些已經不賣了）：
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


def fetch_image(url, retries=3):
    import requests
    import urllib3
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=20)
            if resp.ok:
                return resp.content
            last_err = RuntimeError(f"HTTP {resp.status_code}")
        except requests.exceptions.SSLError:
            # momoshop 部分圖床憑證缺 Subject Key Identifier 欄位，新版 OpenSSL 會直接拒絕。
            # 已跟使用者確認：僅在這支一次性分類腳本內、僅對公開商品圖片下載跳過憑證驗證，
            # 不影響 api.py 正式流程。
            try:
                with urllib3.warnings.catch_warnings():
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    resp = requests.get(url, timeout=20, verify=False)
                if resp.ok:
                    return resp.content
                last_err = RuntimeError(f"HTTP {resp.status_code}")
            except Exception as e2:
                last_err = e2
        except Exception as e:
            last_err = e
        time.sleep(2 * (attempt + 1))
    raise last_err


def main():
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")
    if not api_key:
        print("找不到 GEMINI_API_KEY，中止")
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    by_id = {it["id"]: it for it in cat}

    still_failed = 0
    fixed = 0
    for iid in RETRY_IDS:
        item = by_id[iid]
        label = item["name_zh"][:25]
        try:
            img_bytes = fetch_image(item["image_url"])
            mime = "image/webp" if item["image_url"].lower().endswith(".webp") else "image/jpeg"
            resp = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=[types.Part.from_bytes(data=img_bytes, mime_type=mime), CLASSIFY_PROMPT],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            result = json.loads(resp.text)
            style = (result.get("style_tags") or [None])[0]
            if style not in VALID_STYLES:
                style = "modern"
            item["style_tags"] = [style]
            if result.get("colors"):
                item["colors"] = result["colors"]
            if result.get("category"):
                item["category"] = result["category"]
            if result.get("dimensions"):
                item["dimensions"] = result["dimensions"]
            if result.get("flux_descriptor"):
                item["flux_descriptor"] = result["flux_descriptor"]
            fixed += 1
            print(f"OK  {iid} -> {style} | {label}")
        except Exception as e:
            still_failed += 1
            print(f"FAIL {iid}（{label}）：{type(e).__name__}: {str(e)[:80]}")
        time.sleep(0.5)

    CATALOG_PATH.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n重試完成：成功 {fixed} / 仍失敗 {still_failed}（共 {len(RETRY_IDS)} 件）")


if __name__ == "__main__":
    main()

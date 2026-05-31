"""
對 furniture_catalog_real.json 所有商品做 Gemini 視覺分類
- 模型: gemini-2.5-flash-lite（免費版）
- 速度: 每 4 秒一件
- 斷點續跑: 進度存 reclassify_progress.json，中斷後接著跑
- 日誌: 同時寫到 reclassify_log.txt
"""
import json, os, sys, time, re
from pathlib import Path

CATALOG_PATH  = Path(__file__).parent / "furniture_catalog_real.json"
PROGRESS_PATH = Path(__file__).parent / "reclassify_progress.json"
LOG_PATH      = Path(__file__).parent / "reclassify_log.txt"
MODEL         = "gemini-2.5-flash-lite"
SLEEP_SEC     = 2

VALID_STYLES = ["modern","japanese","luxury","nordic","muji","cream","wood","french","chinese-modern"]

CLASSIFY_PROMPT = """你是專業室內設計師，請仔細分析這張傢俱產品圖。

回傳 JSON（嚴格照格式，不要多餘文字）：
{
  "name_zh": "繁體中文品名，精簡15字以內，只描述產品本身",
  "style_tags": ["最符合的一個風格"],
  "colors": ["主色1","主色2"],
  "category": "沙發/床架/桌子/椅子/茶几/收納/燈具/地毯/窗簾/裝飾/抱枕/寢具/傢俱",
  "dimensions": "尺寸（若圖中可見則填，否則留空字串）",
  "flux_descriptor": "英文，30字以內，描述此傢俱外觀顏色材質給AI圖像生成用"
}

style_tags 只能選一個：modern/nordic/japanese/muji/luxury/cream/wood/french/chinese-modern

判斷標準：
- cream：米白色、奶油色、布克萊布料、圓弧造型、溫柔質感
- wood：原木紋理、實木腳、蜂蜜色或琥珀色、自然森系
- french：弧線設計、藤編、黃銅腳、絲絨、復古優雅
- chinese-modern：深色木材、格柵、東方元素、黃銅、現代中式
- modern：線條簡潔、灰白黑色系、金屬腳
- nordic：淺色原木、白色、簡約溫暖
- japanese：低矮、侘寂感、原木、留白
- muji：棉麻、原木、無印良品感、功能極簡
- luxury：大理石、金屬邊框、絲絨、輕奢"""


def log(msg: str):
    line = str(msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        safe = line.encode('ascii', errors='replace').decode('ascii')
        try:
            print(safe, flush=True)
        except Exception:
            pass
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_progress() -> set:
    if PROGRESS_PATH.exists():
        data = json.loads(PROGRESS_PATH.read_text(encoding='utf-8'))
        return set(data.get('done_ids', []))
    return set()


def save_progress(done_ids: set):
    PROGRESS_PATH.write_text(
        json.dumps({'done_ids': list(done_ids)}, ensure_ascii=False),
        encoding='utf-8'
    )


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    # 載入 .env
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if not gemini_key:
        log("ERROR: GEMINI_API_KEY 未設定")
        sys.exit(1)

    from google import genai
    from google.genai import types
    import requests as req_lib

    client = genai.Client(api_key=gemini_key)
    catalog = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    done_ids = load_progress()
    todo = [it for it in catalog if it.get('id') not in done_ids]

    log(f"總計: {len(catalog)} 件 | 已完成: {len(done_ids)} | 待處理: {len(todo)}")
    if not todo:
        log("全部已完成！")
        return

    mins = len(todo) * SLEEP_SEC / 60
    log(f"預估時間: {mins:.0f} 分鐘 | 模型: {MODEL}")
    log("")

    ok_count = 0
    fail_count = 0
    catalog_by_id = {it['id']: it for it in catalog}

    for i, item in enumerate(todo):
        item_id = item['id']
        name = item.get('name_zh', '')
        style_hint = item.get('style_tags', ['modern'])[0]

        prefix = f"  [{i+1}/{len(todo)}] {name[:25]:<25} [{style_hint}]"

        try:
            img_url = item.get('image_url', '')
            if not img_url:
                raise Exception("無圖片URL")

            img_resp = req_lib.get(img_url, timeout=10)
            if not img_resp.ok:
                raise Exception(f"圖片{img_resp.status_code}")

            ct = img_resp.headers.get('Content-Type', '')
            if 'webp' in ct or img_url.endswith('.webp'):
                mime = "image/webp"
            elif 'png' in ct or img_url.endswith('.png'):
                mime = "image/png"
            else:
                mime = "image/jpeg"

            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=MODEL,
                        contents=[
                            types.Part.from_bytes(data=img_resp.content, mime_type=mime),
                            CLASSIFY_PROMPT,
                        ],
                        config=types.GenerateContentConfig(response_mime_type="application/json"),
                    )
                    break
                except Exception as ex:
                    ex_str = str(ex)
                    if ('429' in ex_str or '503' in ex_str) and attempt < 2:
                        wait = 60 * (attempt + 1)  # 60s, 120s — exceeds RPM window
                        label = 'rate limit' if '429' in ex_str else 'server err'
                        log(f"{prefix}  {label}, wait {wait}s...")
                        time.sleep(wait)
                    else:
                        raise

            c = json.loads(resp.text)
            style = c.get('style_tags', [style_hint])[0]
            if style not in VALID_STYLES:
                style = style_hint

            catalog_by_id[item_id].update({
                'name_zh':         c.get('name_zh', name)[:25],
                'style_tags':      [style],
                'colors':          c.get('colors', []),
                'category':        c.get('category', item.get('category', '傢俱')),
                'dimensions':      c.get('dimensions', ''),
                'flux_descriptor': c.get('flux_descriptor', ''),
            })

            ok_count += 1
            done_ids.add(item_id)
            log(f"{prefix}  OK -> {style}")

        except Exception as e:
            fail_count += 1
            # 不加進 done_ids，下次重跑可以補分類
            log(f"{prefix}  FAIL ({str(e)[:40]})")

        if (i + 1) % 20 == 0:
            save_progress(done_ids)
            CATALOG_PATH.write_text(
                json.dumps(list(catalog_by_id.values()), ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
            log(f"\n  >>> 進度儲存 {i+1}/{len(todo)}  OK:{ok_count} Fail:{fail_count}\n")

        time.sleep(SLEEP_SEC)

    # 最終儲存
    save_progress(done_ids)
    CATALOG_PATH.write_text(
        json.dumps(list(catalog_by_id.values()), ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    from collections import Counter
    final = list(catalog_by_id.values())
    styles = Counter(it['style_tags'][0] for it in final if it.get('style_tags'))
    has_flux = sum(1 for it in final if it.get('flux_descriptor') and len(it['flux_descriptor']) > 10)

    log(f"\n{'='*60}")
    log(f"完成！ OK:{ok_count}  Fail:{fail_count}  有flux:{has_flux}/{len(final)}")
    log("風格分布:")
    for s in VALID_STYLES:
        log(f"  {s:<15}: {styles.get(s,0)}")


if __name__ == "__main__":
    main()

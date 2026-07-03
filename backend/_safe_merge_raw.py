"""
安全版合併腳本：把爬蟲的 raw 輸出「增量合併」進 furniture_catalog_real.json，
不覆寫既有資料（既有的 merge_catalogs.py / scraper_momo.py main() 都是直接
覆寫整個真實目錄，會把 pchome/hola/nitori 等既有來源全部清空——太危險，改寫這支）。

用法：python3 _safe_merge_raw.py <raw_json_path>
raw_json 格式需含：id, name_zh, price_twd, image_url, purchase_url, source,
                  category（中文）, style_hint（單一風格 id）
"""
import json, re, sys, io
from pathlib import Path
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CATALOG_PATH = Path(__file__).parent / "furniture_catalog_real.json"

# 沿用 merge_catalogs.py 的清洗規則（這部分是安全、可重用的）
JUNK_PATTERNS = [
    r'^滿\d', r'^買\d+送\d+', r'^\d+折', r'^\d+%',
    r'^限時', r'^特價', r'^最低', r'^現省',
    r'^\[', r'^【限', r'^MIT\s', r'^\d+CM',
    r'^部分地區', r'^台灣製造可', r'^台灣與歐美', r'^下單\d+折',
    r'^一般地區', r'^適用於\d+', r'^小戶型', r'^單人即可',
    r'^高穩定', r'^高彈力', r'^高回彈', r'^磨毛',
    r'^六角', r'^疊摞', r'^拿取方便',
]

def is_junk(name: str) -> bool:
    if len(name) < 5:
        return True
    for pat in JUNK_PATTERNS:
        if re.match(pat, name):
            return True
    if re.search(r'折\d+|折300|折600|折100', name):
        return True
    return False

# 中文品名關鍵字 → 正確類目（修正 scraper 搜尋當下貼的粗略 category 標籤，
# 例如搜尋關鍵字是「法式沙發」但搜出來的其實是沙發套/沙發巾這種配件）
#
# 配件/耗材規則必須放在最前面！第一批真爬蟲資料（286件 pchome）實測抓到
# 17 件「沙發墊/沙發套/茶几桌巾」被通用的「沙發」/「茶几」關鍵字誤吃——
# 因為舊版規則順序是「沙發」在前，"沙發墊"這種字串一樣命中"沙發"子字串，
# 配件規則排在後面永遠輪不到。改成配件規則優先判斷，是配件就直接分流，
# 不會被家具本體規則搶先吃掉。
CATEGORY_RULES = [
    (['沙發套','沙發巾','沙發墊','沙發罩','沙發蓋布','坐墊','保護墊'], '寢具'),
    (['床包','床單','保潔墊','床罩','床裙','防塵套'], '寢具'),
    (['桌布','桌墊','玻璃墊','茶几桌巾'], '裝飾'),
    (['椅套','椅墊'], '寢具'),
    (['L型沙發','三人座沙發','雙人座沙發','單人座沙發','三人沙發','雙人沙發','單人沙發',
      '沙發床','貓抓布沙發','布沙發','皮沙發','沙發椅','懶人沙發','沙發'], '沙發'),
    (['餐桌','書桌','工作桌','電腦桌','咖啡桌','飯桌','摺疊桌','辦公桌'], '桌子'),
    (['餐椅','辦公椅','電腦椅','休閒椅','搖椅','藤椅','單椅','吧椅','吧台椅','椅'], '椅子'),
    (['床架','床組','床頭板','掀床','收納床','雙人床','單人床架'], '床架'),
    (['茶几','邊几','角几','矮桌','邊桌'], '茶几'),
    (['電視櫃','書架','書櫃','收納櫃','置物架','衣櫃','斗櫃','抽屜','書案','層架','屏風'], '收納'),
    (['吊燈','台燈','落地燈','壁燈','燈具','燈飾'], '燈具'),
    (['地毯','毯子'], '地毯'),
    (['窗簾','遮光簾','捲簾','百葉'], '窗簾'),
    (['掛鏡','裝飾鏡','掛畫','壁畫','花器','擺件','時鐘'], '裝飾'),
    (['抱枕','靠枕','枕頭'], '抱枕'),
    # 沙發套/沙發巾/毯子這種配件不該歸進沙發類目，會污染沙發選品——歸寢具/紡織
    (['沙發套','沙發巾','沙發墊','保護墊','披肩毯','毛毯'], '寢具'),
    (['被','寢具','床單','床包','枕套'], '寢具'),
]

def detect_category(name: str) -> str:
    for keywords, cat in CATEGORY_RULES:
        if any(kw in name for kw in keywords):
            return cat
    return '傢俱'


def is_multi_piece_bundle(name: str) -> bool:
    """多件商品合照偵測（比 furniture_match.py 的正則寬鬆，這裡直接整筆排除
    不進目錄，不只是配對時降權）。實測抓到「茶几130cm+電視櫃180cm...組合」
    這種標題：兩個以上的 NNNcm 尺寸標注（暗示兩件不同家具各自報尺寸）
    + 含套組/組合/件套關鍵字，家具類目專用（寢具的枕頭被套組合是正常雙件寢具，
    不算多件家具合照，排除在外）。"""
    if len(re.findall(r'\d+\s*cm', name)) < 2:
        return False
    return any(kw in name for kw in ('套組', '組合', '件套', '全套'))


def main():
    if len(sys.argv) < 2:
        print("用法: python3 _safe_merge_raw.py <raw_json_path>")
        sys.exit(1)
    raw_path = Path(sys.argv[1])
    if not raw_path.exists():
        print(f"找不到檔案: {raw_path}")
        sys.exit(1)

    existing = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    existing_urls = {it.get("purchase_url") for it in existing if it.get("purchase_url")}
    existing_ids = {it.get("id") for it in existing if it.get("id")}
    print(f"現有目錄: {len(existing)} 件")

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    print(f"raw 檔案: {len(raw)} 件")

    added = []
    skipped_dup = 0
    skipped_junk = 0
    skipped_incomplete = 0
    for it in raw:
        name = (it.get("name_zh") or "").strip()
        url = it.get("purchase_url") or ""
        img = it.get("image_url") or ""
        style = it.get("style_hint") or it.get("style") or ""

        if not (name and url and img and style):
            skipped_incomplete += 1
            continue
        if is_junk(name):
            skipped_junk += 1
            continue
        if url in existing_urls or it.get("id") in existing_ids:
            skipped_dup += 1
            continue

        cat = detect_category(name)
        # 多件商品合照只在真的家具類目擋（沙發/桌子/椅子/床架/茶几/收納）——
        # 寢具的「枕頭被套組」是正常雙件寢具，不算誤導性的多件家具合照
        if cat in ('沙發', '桌子', '椅子', '床架', '茶几', '收納') and is_multi_piece_bundle(name):
            skipped_junk += 1
            continue
        added.append({
            "id":              it.get("id") or f"{it.get('source','x')}-{len(existing)+len(added)+1:04d}",
            "name_zh":         name[:40],
            "price_twd":       it.get("price_twd", 0),
            "image_url":       img,
            "purchase_url":    url,
            "source":          it.get("source", ""),
            "category":        cat,
            "style_tags":      [style],
            "colors":          it.get("colors", []),
            "dimensions":      it.get("dimensions", ""),
            "flux_descriptor": it.get("flux_descriptor", ""),
        })
        existing_urls.add(url)

    print(f"\n跳過：不完整(缺名/圖/連結/風格) {skipped_incomplete}，垃圾文字 {skipped_junk}，"
          f"已存在(跟現有目錄重複) {skipped_dup}")
    print(f"新增: {len(added)} 件")

    if added:
        style_dist = Counter(it["style_tags"][0] for it in added)
        cat_dist = Counter(it["category"] for it in added)
        print("\n新增的風格分佈:", dict(style_dist))
        print("新增的類目分佈:", dict(cat_dist))

    combined = existing + added
    CATALOG_PATH.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成。總筆數: {len(existing)} -> {len(combined)}")


if __name__ == "__main__":
    main()

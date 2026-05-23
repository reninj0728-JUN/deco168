"""
淘寶聯盟 (阿里媽媽) 官方 API — 家具搜尋
需要：
  1. 申請淘寶聯盟帳號 https://pub.alimama.com/
  2. 建立推廣位，取得 adzone_id
  3. 申請 App → 取得 app_key / app_secret
  4. 將 TAOBAO_APP_KEY / TAOBAO_APP_SECRET / TAOBAO_ADZONE_ID 加入 .env

官方文件：https://open.taobao.com/api.htm?docId=35941
"""
import os
import json
import time
import hashlib
import hmac
import requests
from datetime import datetime

TAOBAO_GATEWAY = "https://eco.taobao.com/router/rest"

# 9 種風格 → 淘寶搜尋關鍵字
STYLE_TAOBAO_KEYWORDS = {
    "modern":        ["現代簡約沙發", "北歐簡約電視櫃", "白色實木書桌", "極簡主義燈具"],
    "japanese":      ["日式原木茶几", "榻榻米矮桌", "日式禪意燈", "和風收納架"],
    "luxury":        ["輕奢天鵝絨沙發", "大理石紋咖啡桌", "黃銅落地燈", "輕奢絨布椅"],
    "nordic":        ["北歐實木餐椅", "北歐風地毯", "棉麻窗簾", "原木層板架"],
    "industrial":    ["工業風書架", "復古皮革椅", "工業風鐵藝燈", "黑鐵管層架"],
    "mediterranean": ["地中海藤編椅", "地中海陶瓷花器", "地中海拱形掛鏡", "麻繩燈具"],
    "muji":          ["無印風收納盒", "原木低矮床架", "簡約紙質燈罩", "竹製儲物籃"],
    "art-deco":      ["Art Deco幾何地毯", "金屬幾何燭臺", "鏡面妝台", "復古絨布椅"],
    "boho":          ["波西米亞流蘇地毯", "藤編吊椅", "麻繩掛飾", "馬卡龍落地燈"],
}


def _sign(params: dict, app_secret: str) -> str:
    """淘寶 API 簽名算法"""
    sorted_params = sorted(params.items())
    query = "".join(f"{k}{v}" for k, v in sorted_params)
    sign_str = app_secret + query + app_secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def search_taobao_material(keyword: str, app_key: str, app_secret: str, adzone_id: str, page_size: int = 5) -> list[dict]:
    """
    搜尋淘寶聯盟素材（商品列表）
    API: taobao.tbk.dg.material.optional
    """
    params = {
        "method": "taobao.tbk.dg.material.optional",
        "app_key": app_key,
        "session": "",  # 公開 API 不需要 session
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "q": keyword,
        "page_size": str(page_size),
        "page_no": "1",
        "adzone_id": adzone_id,
        "platform": "2",  # 2=PC
        "material_id": "4",  # 4=通用
    }

    params["sign"] = _sign(params, app_secret)

    try:
        resp = requests.post(TAOBAO_GATEWAY, params=params, timeout=10)
        data = resp.json()

        # 解析回應
        result_key = "taobao_tbk_dg_material_optional_response"
        if result_key not in data:
            error = data.get("error_response", {})
            print(f"  [Taobao API Error] {error.get('code')}: {error.get('zh_desc')}")
            return []

        items = data[result_key].get("result", {}).get("result_list", {}).get("map_data", [])
        return [_parse_taobao_item(item) for item in items if item]

    except Exception as e:
        print(f"  [ERROR] 淘寶 API 請求失敗: {e}")
        return []


def _parse_taobao_item(item: dict) -> dict:
    """解析淘寶聯盟商品格式"""
    item_info = item.get("item_basic_info", {})
    price_info = item.get("price_promotion_info", {})
    coupon_info = item.get("coupon_info", {})

    # 價格（元 → 台幣約 x4.5）
    price_yuan = float(price_info.get("zk_final_price", "0") or "0")
    price_twd = int(price_yuan * 4.5)

    # 折扣後價格
    sale_price_yuan = float(price_info.get("sale_price", "0") or "0")
    if sale_price_yuan > 0:
        price_twd = int(sale_price_yuan * 4.5)

    # 優惠券
    coupon_amount = coupon_info.get("coupon_amount", 0)

    # 圖片
    pic_url = item_info.get("pict_url", "")
    if pic_url and not pic_url.startswith("http"):
        pic_url = "https:" + pic_url

    # 購買連結（聯盟短連結）
    click_url = item_info.get("click_url", "")

    # 商品 ID
    item_id = str(item_info.get("item_id", ""))

    return {
        "id": f"taobao-{item_id}",
        "name_zh": item_info.get("title", "")[:50],
        "brand": "淘寶精選",
        "category": "unknown",  # 後面用 AI 分類
        "style_tags": [],  # 後面根據搜尋關鍵字補
        "keywords": [],
        "colors": [],
        "price_twd": price_twd,
        "price_yuan": price_yuan,
        "coupon_twd": int(coupon_amount * 4.5),
        "image_url": pic_url,
        "purchase_url": click_url or f"https://item.taobao.com/item.htm?id={item_id}",
        "source": "taobao_api",
        "dimensions": "",
        "flux_descriptor": "",
    }


def build_taobao_catalog(styles: list[str] | None = None, max_per_keyword: int = 3) -> list[dict]:
    """
    搜尋淘寶聯盟，建立風格化家具目錄
    需要環境變數：TAOBAO_APP_KEY, TAOBAO_APP_SECRET, TAOBAO_ADZONE_ID
    """
    app_key = os.environ.get("TAOBAO_APP_KEY", "")
    app_secret = os.environ.get("TAOBAO_APP_SECRET", "")
    adzone_id = os.environ.get("TAOBAO_ADZONE_ID", "")

    if not all([app_key, app_secret, adzone_id]):
        print("[Taobao] 缺少環境變數：TAOBAO_APP_KEY / TAOBAO_APP_SECRET / TAOBAO_ADZONE_ID")
        print("[Taobao] 請先申請 https://pub.alimama.com/ 取得 API 金鑰")
        return []

    target_styles = styles or list(STYLE_TAOBAO_KEYWORDS.keys())
    all_items = []
    seen_ids = set()

    for style in target_styles:
        keywords = STYLE_TAOBAO_KEYWORDS.get(style, [])
        print(f"\n[{style}] 搜尋 {len(keywords)} 個關鍵字...")

        for keyword in keywords:
            print(f"  搜尋: {keyword}")
            products = search_taobao_material(keyword, app_key, app_secret, adzone_id, max_per_keyword)
            print(f"  → 找到 {len(products)} 件")

            for p in products:
                pid = p["id"]
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                p["style_tags"] = [style]
                all_items.append(p)

            time.sleep(0.5)  # API rate limit

    return all_items


def mock_taobao_result() -> list[dict]:
    """
    模擬回傳（測試用，不需要 API 金鑰）
    實際申請 API 前可用此資料測試流程
    """
    return [
        {
            "id": "taobao-mock-001",
            "name_zh": "北歐風實木餐椅 白橡木椅腿亞麻布面",
            "brand": "淘寶精選",
            "category": "chair",
            "style_tags": ["nordic", "modern"],
            "keywords": ["dining chair", "white oak", "linen"],
            "colors": ["beige", "white", "natural wood"],
            "price_twd": 1800,
            "price_yuan": 400,
            "image_url": "https://img.alicdn.com/imgextra/i3/sample_chair.jpg",
            "purchase_url": "https://item.taobao.com/item.htm?id=sample001",
            "source": "taobao_mock",
            "dimensions": "W45 x D50 x H80 cm",
            "flux_descriptor": "solid oak dining chair, linen fabric seat, minimalist Scandinavian legs",
        },
        {
            "id": "taobao-mock-002",
            "name_zh": "日式原木茶几 圓形實木低矮小桌",
            "brand": "淘寶精選",
            "category": "table",
            "style_tags": ["japanese", "muji"],
            "keywords": ["low coffee table", "natural wood", "round table"],
            "colors": ["natural wood", "warm brown"],
            "price_twd": 2700,
            "price_yuan": 600,
            "image_url": "https://img.alicdn.com/imgextra/i3/sample_table.jpg",
            "purchase_url": "https://item.taobao.com/item.htm?id=sample002",
            "source": "taobao_mock",
            "dimensions": "D60 x H35 cm",
            "flux_descriptor": "round solid wood low coffee table, natural grain, Japanese minimal",
        },
        {
            "id": "taobao-mock-003",
            "name_zh": "波西米亞流蘇地毯 幾何圖案 客廳地墊",
            "brand": "淘寶精選",
            "category": "rug",
            "style_tags": ["boho", "mediterranean"],
            "keywords": ["kilim rug", "geometric pattern", "fringe rug"],
            "colors": ["terracotta", "mustard", "cream", "rust"],
            "price_twd": 3600,
            "price_yuan": 800,
            "image_url": "https://img.alicdn.com/imgextra/i3/sample_rug.jpg",
            "purchase_url": "https://item.taobao.com/item.htm?id=sample003",
            "source": "taobao_mock",
            "dimensions": "160 x 230 cm",
            "flux_descriptor": "vintage kilim rug, geometric tribal pattern, terracotta fringe edge",
        },
    ]


if __name__ == "__main__":
    import sys

    if "--mock" in sys.argv:
        items = mock_taobao_result()
        print(f"[模擬模式] 回傳 {len(items)} 件商品")
    else:
        styles = sys.argv[1].split(",") if len(sys.argv) > 1 else ["nordic", "japanese"]
        items = build_taobao_catalog(styles=styles, max_per_keyword=3)

    print(f"\n共 {len(items)} 件")
    for item in items:
        print(f"  {item['name_zh'][:30]:30} {item.get('style_tags')} NT${item['price_twd']}")

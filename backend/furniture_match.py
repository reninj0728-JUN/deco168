"""
傢俱配對模組
輸入：Gemini 分析的 renders[] (含 flux_prompt)
輸出：每個 render 附帶 3~5 件真實傢俱推薦 + 產品圖片 URL
"""
import json
import os
import random
import re
from pathlib import Path

# 真實商品目錄（momo 爬取，有真實圖片和購買連結）
CATALOG_REAL_PATH = Path(__file__).parent / "furniture_catalog_real.json"
# 舊目錄作為備用（AI 生成，無真實圖片）
CATALOG_FALLBACK_PATH = Path(__file__).parent / "furniture_catalog.json"

# 中文類別 → 英文類別對照（配合 CATEGORY_KEYWORDS）
# 茶几獨立成 coffee_table（不再跟餐桌混在 table）
# 桌子保守處理為 table（不細分餐桌/邊几，避免影響其他空間邏輯）
# 椅子細分由 refine_subcategory() 按 name_zh 處理，bucket 仍是 chair
CATEGORY_ZH_TO_EN = {
    '沙發': 'sofa',
    '茶几': 'coffee_table',
    '桌子': 'table',
    '椅子': 'chair',
    '床架': 'bed',
    '收納': 'storage',
    '燈具': 'lighting',
    '地毯': 'rug',
    '窗簾': 'curtain',
    '裝飾': 'mirror',
    '抱枕': 'pillow',
    '寢具': 'bedding',
    '傢俱': 'other',
}

# ── 客廳模式品類規則 ──
# 必撈（fallback 只能在同 category 內換相近風格，不准跨 category 替代）
LIVING_MUST_HAVE = ['sofa', 'coffee_table', 'rug', 'media_console']

# 加分（補滿 top_n 用，同類各取 1）
LIVING_NICE_TO_HAVE = [
    'accent_chair', 'side_table',
]

# 客廳模式排除（不准進主家具清單）
LIVING_EXCLUDED = ['bar_stool', 'dining_chair', 'dining_table', 'bed', 'bedding']

# ── Step-2：全室逐房型配置規則 ───────────────────────────────────────────────
# 每房型獨立的 必備(must) / 加分(nice) / 排除(excluded)。
# living 直接沿用上面的常數，行為與單空間客廳完全一致（不可改）。
# 目錄盤點後微調：side_table(床頭櫃) 只有 13 件、各風格餐桌/書桌深度不均，
#   故把這些薄品類放 nice、不放 must，避免硬性要求落空（薄格子用跨風格保命補）。
# dropdown 只有 living/dining/bedroom/study；其他房型(玄關/廚房…)一律退回 living。
ROOM_RULES = {
    'living': {
        'must':     LIVING_MUST_HAVE,
        'nice':     LIVING_NICE_TO_HAVE,
        'excluded': LIVING_EXCLUDED,
    },
    'bedroom': {
        'must':     ['bed', 'storage'],
        'nice':     ['side_table', 'rug', 'lighting'],
        'excluded': ['sofa', 'coffee_table', 'media_console',
                     'dining_table', 'dining_chair', 'bar_stool'],
    },
    'dining': {
        'must':     ['dining_table', 'dining_chair'],
        'nice':     ['rug', 'lighting'],   # 不放 side_table：配到的多是小邊桌、渲染常沒畫→清單≠圖
        'excluded': ['sofa', 'bed', 'bedding', 'coffee_table', 'media_console'],
    },
    'study': {
        'must':     ['table', 'chair'],
        'nice':     ['storage', 'rug', 'lighting'],
        'excluded': ['sofa', 'bed', 'bedding', 'dining_table', 'dining_chair'],
    },
}

def normalize_room_type(raw: str) -> str:
    """把 region name / room_type 正規化成標準房型（單一真實來源）。
    未知 → 'living'（其他空間退回客廳邏輯）。
    優先序刻意：臥室 → 書房 → 客廳(含客餐廳/公領域，有沙發) → 餐廳(純餐)。
    例：『客餐廳公領域』→ living（有沙發）；純『餐廳』→ dining。"""
    s = (raw or '').strip().lower()
    zh = raw or ''
    if any(k in zh for k in ('臥', '寢')) or 'bedroom' in s or 'bed room' in s:
        return 'bedroom'
    if any(k in zh for k in ('書房', '工作', '書')) or s in ('study', 'office', 'workspace') or 'study' in s:
        return 'study'
    # 客廳 / 客餐廳 / 公領域 / 起居：歸 living（主要公共空間、要有沙發），先於純餐廳判斷
    if any(k in zh for k in ('客廳', '客餐廳', '起居', '公領域', '客')) or s in ('living', 'living_room') or 'living' in s:
        return 'living'
    if any(k in zh for k in ('餐廳', '餐廚', '用餐', '餐區', '餐')) or 'dining' in s:
        return 'dining'
    return 'living'

# ── 軟裝接入 (2026-06-18, Step 3B 規則修正後) ────────────────────────────────
# 軟裝 = 「不算主家具總計、另開獨立區塊建議」的搭配商品.
# 預算策略採 B 方案: 主家具 (sofa/coffee_table/rug) 算主總計; 軟裝獨立顯示, 不併主總計.
#
# Step 3B 規則修正後 real catalog 預期可用品類 (audit 確認):
#   pillow    ~110 件  (category=抱枕 扣掉抱枕套後)
#   curtain     91 件  (category=窗簾)
#   wall_art   ~45 件  (category=裝飾 含掛畫/相框/壁掛框/裝飾畫)
#   vase      ~200 件  (category=裝飾 含花瓶/花盆/盆器/花盆套 — 容器類全收)
#   plant       ~7 件  (catalog 真植栽稀少)
#   decor      ~10 件  (category=裝飾 含擺件/裝飾盤/展示罩)
#   textile    ~10 件  (category=裝飾 含沙發墊/沙發毯/抱枕套)
#   lighting   103 件  (category=燈具 桌燈/立燈/吊燈)
#
# 每張 render 依風格挑 3-5 件; 不再固定 8 類全塞, 避免結果頁出現圖上沒有的硬湊軟裝.
SOFT_FURNISHING_CATS = [
    'pillow', 'curtain', 'wall_art', 'vase', 'plant',
    'decor', 'textile', 'lighting',
]
SOFT_FURNISHING_MAX_RESULTS = 5

SOFT_STYLE_CAT_PRIORITY = {
    'french': ['lighting', 'curtain', 'wall_art', 'vase', 'plant', 'textile', 'pillow', 'decor'],
    'luxury': ['lighting', 'wall_art', 'vase', 'curtain', 'plant', 'decor', 'pillow', 'textile'],
    'art-deco': ['lighting', 'wall_art', 'vase', 'curtain', 'decor', 'plant', 'pillow', 'textile'],
    'chinese-modern': ['lighting', 'wall_art', 'vase', 'plant', 'curtain', 'textile', 'decor', 'pillow'],
    'modern': ['lighting', 'wall_art', 'plant', 'curtain', 'vase', 'decor', 'pillow', 'textile'],
    'muji': ['curtain', 'plant', 'lighting', 'wall_art', 'vase', 'textile', 'pillow', 'decor'],
    'nordic': ['curtain', 'plant', 'lighting', 'wall_art', 'vase', 'textile', 'pillow', 'decor'],
    'cream': ['curtain', 'lighting', 'plant', 'wall_art', 'vase', 'textile', 'pillow', 'decor'],
    'japanese': ['curtain', 'plant', 'wall_art', 'lighting', 'vase', 'textile', 'pillow', 'decor'],
    'wood': ['plant', 'curtain', 'lighting', 'wall_art', 'vase', 'textile', 'pillow', 'decor'],
}

# 紗簾延伸購買項目：厚窗簾+紗簾是常見的雙層窗簾組合，渲染圖常見這個層次，
# 但軟裝配對每類目只選 1 件，紗簾原本完全沒被獨立列出來（用戶回饋：
# 圖上有紗窗，清單卻只有厚窗簾，資料庫其實有 91 件窗簾裡 11 件是紗簾）。
SHEER_CURTAIN_KW = ["紗簾", "紗窗", "薄紗", "紗質", "voile", "sheer"]

# 軟裝單件預算上限 (不算主總計, 但仍隨 tier 控制單件不要太貴)
SOFT_FURNISHING_CAP = {
    'tier1': 3000,
    'tier2': 6000,
    'tier3': None,
}

# 椅子細分關鍵字（name_zh 命中即覆蓋 chair 為子類）
CHAIR_SUBCAT_RULES = [
    ('bar_stool', ['吧台', '吧檯', '吧椅', '高腳椅', 'bar stool', 'STIG', 'barstool']),
    ('dining_chair', ['餐椅', 'dining chair']),
    ('accent_chair', ['單人沙發', '單椅', '搖椅', '躺椅', '休閒椅', 'lounge', 'accent chair', '皮革椅', '扶手椅', 'armchair']),
]

# 軟裝細分 (Step 3B, 2026-06-18): 「裝飾」cat 278 件原本只 13/59/7/3 = 79 件命中,
# 其餘 195 件全 fallback 到 mirror, 其中 156 件根本不是鏡子.
# 重做規則: 用「強關鍵字」而非寬詞, 嚴格不用「畫/框/造型/裝飾」這 4 個 — 會誤抓
# 「金屬框架椅 / 造型茶几 / 裝飾抱枕」等已正確分類的家具.
# textile 為新子類, 接住沙發墊 / 沙發毯 / 沙發巾 / 抱枕套 等紡織軟裝.
DECOR_SUBCAT_RULES = [
    # wall_art: 牆面藝術 (掛畫類). 不用「畫」「框」單字, 改用組合詞.
    ('wall_art', [
        '掛畫', '壁畫', '壁飾', '壁掛框', '掛牆',
        '相框', '畫框', '裝飾畫', '藝術畫',
        'wall art', 'poster', 'photo frame', 'picture frame',
    ]),
    # vase: 花瓶 + 花盆容器 (catalog 沒真植栽, 但有大量容器類軟裝, 全收進 vase).
    ('vase', [
        '花瓶', '花器', '花盆', '盆器', '花盆套',
        'vase', 'planter', 'pot',
    ]),
    # plant: 真植物 / 仿植物本體 (catalog 量少 ~7 件, 規則不放寬).
    ('plant', [
        '植栽', '盆栽', '綠植', 'potted plant', 'green plant',
    ]),
    # decor: 純擺飾. 強關鍵字: 擺件 / 擺飾 / 雕塑 / 公仔 / 裝飾盤 (組合詞) / 展示罩.
    # 嚴格不用「裝飾」「造型」單字, 否則會抓到 裝飾抱枕 / 造型桌燈 等.
    ('decor', [
        '擺件', '擺飾', '飾品', '雕塑', '公仔',
        '裝飾盤', '裝飾擺件', '展示罩',
        'ornament', 'figurine', 'sculpture',
    ]),
    # textile: 紡織軟裝 (沙發墊 / 沙發毯 / 抱枕套 等). 從「裝飾」cat 撈出來.
    ('textile', [
        '沙發墊', '沙發毯', '沙發巾', '沙發蓋毯', '抱枕套',
        'throw blanket', 'sofa cover', 'pillow cover',
    ]),
]

# 桌子細分（保守：只在 LIVING_EXCLUDED 過濾與 NICE_TO_HAVE 配對時才用）
TABLE_SUBCAT_RULES = [
    ('dining_table', ['餐桌', 'dining table']),
    ('side_table', ['邊几', '邊桌', '床頭櫃', 'side table', '小茶桌', '角几']),
]

MEDIA_CONSOLE_KEYWORDS = [
    '電視櫃', '電視柜', '電視架', '視聽櫃', '影音櫃',
    'tv stand', 'tv cabinet', 'media console', 'media cabinet',
    'low media console', '低櫃', '矮櫃',
]


def refine_subcategory(en_cat: str, name_zh: str) -> str:
    """按品名細分 chair / table / mirror(裝飾雜燴 → 軟裝) / pillow(抱枕套→textile)，其他類別維持原樣"""
    name_lower = (name_zh or '').lower()
    if any(kw.lower() in name_lower for kw in MEDIA_CONSOLE_KEYWORDS):
        return 'media_console'
    # 沙發墊 / 沙發套 / 坐墊 等是「軟裝紡織」，不是主沙發本體。
    # 修 bug：catalog 把這類錯標成 category=沙發 時，會被當成主沙發配進來
    # （例：NT$337「法式優雅冰絲透氣沙發墊」被選為沙發）。歸 textile。
    if en_cat == 'sofa':
        # 涵蓋各種「沙發布料/套/墊/巾」用語，避免又被當主沙發（蓋布≠蓋毯，先前漏掉）。
        for kw in ('沙發墊', '沙發套', '沙發罩', '沙發巾', '沙發蓋毯', '沙發毯',
                   '沙發蓋布', '蓋布', '布套', '保護套', '防塵套', '彈性套',
                   '坐墊', '椅墊', 'sofa cover', 'sofa cushion', 'seat cushion',
                   'slipcover', 'couch cover', 'sofa throw'):
            if kw.lower() in name_lower:
                return 'textile'
        return 'sofa'
    if en_cat == 'chair':
        for sub, kws in CHAIR_SUBCAT_RULES:
            if any(kw.lower() in name_lower for kw in kws):
                return sub
        return 'chair'
    if en_cat == 'table':
        for sub, kws in TABLE_SUBCAT_RULES:
            if any(kw.lower() in name_lower for kw in kws):
                return sub
        return 'table'
    # Step 3B (2026-06-18) pillow 分支: 「抱枕套」/「pillow cover」是紡織軟裝, 歸 textile.
    # 純抱枕本體 → 維持 pillow.
    if en_cat == 'pillow':
        for kw in ('抱枕套', 'pillow cover', 'pillow case', 'cushion cover'):
            if kw.lower() in name_lower:
                return 'textile'
        return 'pillow'
    # Step 3B (2026-06-18) mirror 分支: 修 fallback —
    #   1. 先試 DECOR_SUBCAT_RULES (wall_art / vase / plant / decor / textile).
    #   2. 都沒命中時, 不再 fallback 到 mirror;
    #      只有 name_zh 明確含「鏡」/「mirror」 才歸 mirror.
    #   3. 其餘歸 'decor_unknown' (新狀態, 不進 SOFT_FURNISHING_CATS,
    #      但保留資料完整性, 後續 audit / 規則 補強用).
    if en_cat == 'mirror':
        for sub, kws in DECOR_SUBCAT_RULES:
            if any(kw.lower() in name_lower for kw in kws):
                return sub
        if ('鏡' in (name_zh or '')) or ('mirror' in name_lower):
            return 'mirror'
        return 'decor_unknown'
    # 廚房瀝水盤/托盤/碗盤架等被目錄錯標成「收納(storage)」→ 改判 decor，
    # 不讓它被當成衣櫃/收納主家具配進臥室/書房（例：83A9B577 主臥出現「多功能淺色瀝水盤」）。
    if en_cat == 'storage':
        for kw in ('瀝水', '碗盤', '碗架', '杯架', '餐具', '置物盤', '托盤',
                   '水盤', '瀝水架', '瀝水籃', 'dish rack', 'drainer', 'tray'):
            if kw.lower() in name_lower:
                return 'decor'
    return en_cat


def resolve_category(item: dict) -> str:
    """取得家具最終解析後的英文類別（含細分）"""
    raw = item.get('category', '')
    en_cat = CATEGORY_ZH_TO_EN.get(raw, raw.lower() if isinstance(raw, str) else 'other')
    return refine_subcategory(en_cat, item.get('name_zh', ''))

# 家具類別關鍵字 → category
CATEGORY_KEYWORDS = {
    "sofa":     ["sofa", "couch", "沙發", "loveseat", "sectional"],
    "chair":    ["chair", "stool", "椅", "seat", "armchair", "recliner"],
    "table":    ["table", "desk", "桌", "coffee table", "dining table", "side table"],
    "bed":      ["bed", "bed frame", "床", "headboard", "platform bed"],
    "shelving": ["shelf", "shelving", "shelves", "bookcase", "架", "收納架", "書架"],
    "storage":  ["storage", "cabinet", "dresser", "收納", "抽屜", "衣櫃", "buffet"],
    "lighting": ["lamp", "pendant", "sconce", "燈", "lighting", "chandelier", "lantern"],
    "rug":      ["rug", "carpet", "mat", "地毯", "kilim"],
    "curtain":  ["curtain", "drape", "blind", "窗簾"],
    "mirror":   ["mirror", "鏡"],
    "plant":    ["plant", "pot", "planter", "植物", "花器", "盆栽"],
}


def load_catalog() -> list[dict]:
    """載入傢俱目錄：優先用真實商品，不足時補舊目錄"""
    catalog = []
    if CATALOG_REAL_PATH.exists():
        with open(CATALOG_REAL_PATH, "r", encoding="utf-8") as f:
            catalog = json.load(f)
        print(f"[furniture_match] 真實目錄: {len(catalog)} 件 (momo)")
    if not catalog and CATALOG_FALLBACK_PATH.exists():
        with open(CATALOG_FALLBACK_PATH, "r", encoding="utf-8") as f:
            catalog = json.load(f)
        print(f"[furniture_match] 備用目錄: {len(catalog)} 件 (AI生成)")
    return catalog


def extract_categories_from_prompt(flux_prompt: str) -> list[str]:
    """從 flux_prompt 關鍵字提取需要的家具類別"""
    prompt_lower = flux_prompt.lower()
    found = []
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in prompt_lower for kw in keywords):
            found.append(cat)
    return found or ["sofa", "table", "lighting"]  # 默認推薦三大類


# ── 長條型客廳：擺位敏感沙發降權規則（root-cause fix for L-sofa-in-narrow-room）──
# aspect_ratio = max(L,W) / min(L,W)
# 跨所有風格，不只 nordic
LONG_ROOM_ASPECT_THRESHOLD = 2.0      # >= 2.0 才觸發
LONG_ROOM_ABS_LENGTH_M     = 6.0      # 或長邊 >= 6m（66784D97: 7.5×3.8 比例 1.97 差點漏掉）
LONG_ROOM_SHAPE_PENALTY    = -5.0     # 大降權但不硬排除（catalog 無直線替代時仍可保命）
LONG_ROOM_BAD_SHAPE_KW = [
    # L 型
    "l型", "l形", "l 型", "l 形", "l-shape", "l shape", "l shaped",
    # U 型
    "u型", "u形", "u 型", "u 形", "u-shape", "u shape", "u shaped",
    # 沙發床
    "沙發床", "sofa bed", "sofabed", "sleeper sofa",
    # 貴妃沙發 / chaise（非對稱單側延伸，本質與 L 沙發同類問題）
    "貴妃", "貴妃椅", "貴妃座", "chaise", "chaise lounge", "chaise longue",
    # 附腳凳一體沙發：模型在長條房可能畫成 chaise 延伸式（1D5F4BA5 觀察補入）
    "附腳凳", "腳凳", "footstool", "ottoman attached",
    # 過深沙發（次要，加分排除）
    "加深", "超深",
]


# ── 小空間 / 尺寸不明：體積過大沙發降權（root-cause fix for C15719C5 —
#   270cm 法式電動沙發把客廳擠得比實際小）──────────────────────────────
# 真實目錄（momo/pchome）99%+ 商品沒填 dimensions，數值過濾 (filter_by_dimensions)
# 幾乎不會生效；這裡用商品名關鍵字當第二道防線，跟長條房的「形狀」降權互補
# （這條抓的是「體積/座位數」，不是「形狀」）。降權不硬排除，避免某風格選無可選。
SMALL_ROOM_MAX_WIDTH_DEFAULT = 240    # parse_max_width_cm 抓不到房間尺寸時的保守預設
SMALL_ROOM_OVERSIZED_PENALTY = -4.0
OVERSIZED_SOFA_KW = [
    "電動", "electric recliner", "power recliner",
    "三人座", "三人位", "三人沙發", "3人座",
    "加大", "特大", "超大", "加寬",
    "貴妃", "chaise",  # 與 LONG_ROOM_BAD_SHAPE_KW 重疊，兩種情境都該降權
]


# ── tier3 高預算方案的「質感底線」（4C3560A2 回饋：20 萬方案配 NT$549 茶几、
#   NT$1,440 地毯，客戶信任感直接掉）。低於底線的不進主家具——但軟性：
#   該類別過濾後空了就回退原池，保證 must-have 永不缺。──────────────────
TIER3_PRICE_FLOOR = {
    "coffee_table":  3000,
    "rug":           2500,
    "media_console": 4000,
}
# 奢華系風格 × 日式/無印/北歐 命名 → 降權（「日式簡約實木電視櫃」配 luxury 很出戲）
LUXURY_MISMATCH_STYLES  = ("luxury", "french", "art-deco")
LUXURY_MISMATCH_KW      = ["日式", "無印", "北歐", "和風", "muji"]
LUXURY_MISMATCH_PENALTY = -3.0

# 「地毯」類的功能性雜項（浴室墊/門墊/防滑墊/巧拼）不該當客廳主地毯
# （66784D97：都會簡約 20 萬方案主清單出現「防滑橡膠地墊 NT$899」）。降權不硬排除。
RUG_JUNK_KW      = ["地墊", "防滑", "門墊", "腳踏墊", "巧拼", "浴室", "止滑",
                    "餐墊", "隔熱墊", "西餐墊"]
RUG_JUNK_PENALTY = -4.0

# 「桌子」類的功能性雜項（桌布/餐桌墊/桌旗/玻璃墊）不該當書桌/餐桌本體——
# 這些是保護布/裝飾巾，商品照裡通常沒有桌子實體。同一種問題的第二個案例
# （luxury 爬蟲批次抓到 30+ 件「新中式桌布/餐桌墊」被 category 規則誤當桌子），
# 這次直接在配對層加降權，比每次資料更新後人工挑掉更持久。
# 2026-07-08（job 20A8220A）：pchome 有大量簡體字商品名（家具贴膜/防烫贴纸），
# 繁體關鍵字全 miss → 法式風格茶几配到「家具貼膜」。簡繁都要列。
TABLE_JUNK_KW      = ["桌布", "桌墊", "桌旗", "玻璃墊", "餐墊", "防燙墊",
                      "貼膜", "贴膜", "保護膜", "保护膜", "貼紙", "贴纸",
                      "防烫", "軟玻璃", "软玻璃", "桌巾", "臺布", "台布"]
TABLE_JUNK_PENALTY = -4.0

# 主臥/一般臥室不該配雙層床、兒童床（9871F294：新中式主臥配到
# 「木製雙層床架附樓梯收納」上下舖，主臥出現小孩房家具）。
# bedroom 房型的床預設服務成人臥室；未來有「兒童房」獨立房型再另開白名單。
BED_KIDS_KW      = ["雙層床", "上下舖", "上下鋪", "上下床", "子母床", "兒童床",
                    "高架床", "樓梯櫃", "雙層", "滑梯"]
BED_KIDS_PENALTY = -5.0

# 電競系商品（碳纖維電競桌/RGB/電競椅）只適合 modern/industrial 調性——
# 343FFAE7 奶油暖居書房被配到「碳纖維紋理電競桌」，跟奶油風完全不搭。降權不硬排除。
GAMING_KW              = ["電競", "carbon fiber", "碳纖維", "rgb"]
GAMING_ALLOWED_STYLES  = ("modern", "industrial")
GAMING_MISMATCH_PENALTY = -4.0

# 多件商品合照（B4174D56：「深色木製方桌、長條桌、座椅套組」被當單一書桌參考圖，
# 畫面裡同時有方桌+長凳+邊櫃三種家具，AI 參考混亂，渲染出走樣的桌子）。
# 精準比對「名詞、名詞…套組/組合/件套」——只抓真的用頓號/「與」明確列出
# 多樣家具的商品名，不誤傷「組合式書桌」這種單件家具的複合命名。
_MULTI_PIECE_BUNDLE_RE = re.compile(r'.+[、與+].+(套組|組合|件套|全套)$')
MULTI_PIECE_BUNDLE_PENALTY = -5.0

# 20A8220A 抓漏：「集集客 客廳桌櫃組 岩板茶几電視櫃70+180（客廳桌 電視櫃組 子母桌…」
# 逃過上面的 regex（結尾被截斷、用的是「桌櫃組」不是「套組」）→ 被當單一電視櫃
# 參考圖，渲染同時冒出茶几+電視櫃整組。補一層語意判斷：
# 商品名出現 2 種以上「不同家具本體名詞」＋任一組/套字樣 = 多件套組。
_FURN_TYPE_TOKENS = ("茶几", "電視櫃", "沙發", "餐桌", "書桌", "床架", "邊几",
                     "子母桌", "餐椅", "斗櫃", "邊櫃", "床頭櫃", "衣櫃", "書櫃")
_BUNDLE_HINT_RE = re.compile(r"(套組|組合|件套|全套|超值組|[桌櫃椅][組套])")


def is_multi_piece_bundle(name: str) -> bool:
    """單張商品照裡有多件不同家具的套組商品：當單一家具參考圖會誤導渲染。"""
    nm = (name or "").strip()
    if not nm:
        return False
    if _MULTI_PIECE_BUNDLE_RE.search(nm):
        return True
    types_hit = {t for t in _FURN_TYPE_TOKENS if t in nm}
    return len(types_hit) >= 2 and bool(_BUNDLE_HINT_RE.search(nm))


# ── Phase A：預算 tier + 賣場偏好 ──────────────────────────────────────────────
#
# budget_tier: 'tier1' / 'tier2' / 'tier3'  (前端三段下拉)
#   tier1 = 10 萬內｜家具＋軟裝輕改造
#   tier2 = 10–20 萬｜完整家具搭配＋燈飾窗簾
#   tier3 = 20 萬以上｜全室風格整合＋局部硬裝建議
#
# 設計：對 sofa / coffee_table / rug 給單件上限（其餘 nice-to-have 用 default）
# fallback：嚴格 → 1.5× → 完全放寬，保證 must_have 永不空

BUDGET_LABEL_ZH = {
    'tier1': '10 萬內｜家具＋軟裝輕改造',
    'tier2': '10–20 萬｜完整家具搭配＋燈飾窗簾',
    'tier3': '20 萬以上｜全室風格整合＋局部硬裝建議',
}

# 各 tier 的單件上限（單位 TWD）。tier3 不設上限 = 不過濾。
# default：未列出品類在 tier1/tier2 用較寬鬆值（避免燈具/窗簾被誤砍）
BUDGET_CAT_CAP = {
    'tier1': {
        'sofa':         25000,
        'coffee_table': 8000,
        'rug':          6000,
        'media_console': 12000,
        'lighting':     6000,
        'curtain':      8000,
        'accent_chair': 12000,
        'side_table':   5000,
        'default':      10000,
    },
    'tier2': {
        'sofa':         60000,
        'coffee_table': 20000,
        'rug':          15000,
        'media_console': 30000,
        'lighting':     15000,
        'curtain':      18000,
        'accent_chair': 30000,
        'side_table':   12000,
        'default':      20000,
    },
    'tier3': {},  # 空 dict = 不設上限
}

# fallback 寬鬆倍率（嚴格 → 1.5× → 不設限）
BUDGET_RELAX_MULTIPLIER = 1.5


def _budget_cap_for(budget_tier: str, cat_en: str) -> int | None:
    """回該 tier + 品類的單件上限（TWD）。tier3 或缺值回 None = 不過濾。"""
    if budget_tier not in BUDGET_CAT_CAP:
        return None
    caps = BUDGET_CAT_CAP[budget_tier]
    if not caps:
        return None
    return caps.get(cat_en) or caps.get('default')


def _under_budget(item: dict, cap: int | None) -> bool:
    """item 價格是否在 cap 內。cap=None 永遠 True。"""
    if cap is None:
        return True
    try:
        price = int(item.get('price_twd') or 0)
    except (TypeError, ValueError):
        return True  # 價格資料壞掉就不過濾，由評分決定
    if price <= 0:
        return True
    return price <= cap


# preferred_store: 'none' / 'momo' / 'ikea' / 'hola' / 'trplus'
# 加分制（不硬篩）：風格命中 +3 > 賣場偏好 +2 > 顏色加分 +0.5
PREFERRED_STORE_BONUS = 2.0
PREFERRED_STORE_ALIAS = {
    'momo':   ['momo', 'momoshop', 'momo購物'],
    'ikea':   ['ikea', '宜家'],
    'hola':   ['hola', '和樂家居'],
    'trplus': ['trplus', '特力屋', 'test rite', 'testrite'],
}
STORE_LABEL_ZH = {
    'none':   '不指定',
    'momo':   'momo 優先',
    'ikea':   'IKEA 優先',
    'hola':   'HOLA 優先',
    'trplus': '特力屋優先',
}


def _store_bonus(item: dict, preferred_store: str) -> float:
    """賣場符合偏好時 +2 分。'none' / 缺值不加。"""
    if not preferred_store or preferred_store == 'none':
        return 0.0
    aliases = PREFERRED_STORE_ALIAS.get(preferred_store, [])
    if not aliases:
        return 0.0
    brand = (item.get('brand') or '').lower()
    if any(a in brand for a in aliases):
        return PREFERRED_STORE_BONUS
    return 0.0


def compute_room_aspect_ratio(room_dims: dict | None) -> float:
    """從 analysis.room_dimensions 算長寬比 = max(L,W)/min(L,W)。沒資料回 0。"""
    if not isinstance(room_dims, dict):
        return 0.0
    try:
        L = float(room_dims.get("length_m") or 0)
        W = float(room_dims.get("width_m") or 0)
    except (TypeError, ValueError):
        return 0.0
    if L <= 0 or W <= 0:
        return 0.0
    return max(L, W) / min(L, W)


def _long_room_sofa_penalty(item: dict, is_long_room: bool) -> float:
    """
    長條型客廳對「擺位敏感沙發」降權：
      L 型 / U 型 / 沙發床 → -5 分
    不影響其他家具（茶几/地毯/燈等）。
    回 0 或 LONG_ROOM_SHAPE_PENALTY。
    """
    if not is_long_room:
        return 0.0
    if resolve_category(item) != "sofa":
        return 0.0
    name = (item.get("name_zh") or "").lower()
    descriptor = (item.get("flux_descriptor") or "").lower()
    blob = name + " " + descriptor
    for kw in LONG_ROOM_BAD_SHAPE_KW:
        if kw in blob:
            return LONG_ROOM_SHAPE_PENALTY
    return 0.0


def _oversized_sofa_penalty(item: dict, is_small_room: bool) -> float:
    """
    小空間 / 房間尺寸不明時，對「體積偏大」沙發（電動/三人座/加大…）降權。
    跟 _long_room_sofa_penalty 互補：那個抓形狀（L/U/貴妃），這個抓體積/座位數。
    回 0 或 SMALL_ROOM_OVERSIZED_PENALTY。
    """
    if not is_small_room:
        return 0.0
    if resolve_category(item) != "sofa":
        return 0.0
    name = (item.get("name_zh") or "").lower()
    descriptor = (item.get("flux_descriptor") or "").lower()
    blob = name + " " + descriptor
    for kw in OVERSIZED_SOFA_KW:
        if kw in blob:
            return SMALL_ROOM_OVERSIZED_PENALTY
    return 0.0


_PROMPT_WORD_RE = re.compile(r"[a-z]{4,}")
# 每個 flux_prompt 結尾固定的攝影/畫質 boilerplate — 所有商品都會同樣命中，只是稀釋上限
_PROMPT_STOP_WORDS = frozenset({
    "with", "room", "style", "styled", "interior", "design", "professional",
    "photography", "staged", "showroom", "editorial", "styling", "wide",
    "angle", "soft", "natural", "light", "lighting", "people", "text",
    "watermark", "distortion", "artifacts", "concept", "visual", "space",
    "living", "bedroom", "dining", "kitchen", "study", "apartment", "home",
})


def _prompt_word_overlap(prompt_keywords: list[str], item_keywords: list[str],
                         descriptor: str) -> int:
    """flux_prompt 子句拆成英文單字（≥4 字母、去 boilerplate），
    對 flux_descriptor + keywords 的單字集合算重疊數。"""
    prompt_words: set[str] = set()
    for pkw in prompt_keywords:
        prompt_words.update(w for w in _PROMPT_WORD_RE.findall(pkw)
                            if w not in _PROMPT_STOP_WORDS)
    if not prompt_words:
        return 0
    item_words = set(_PROMPT_WORD_RE.findall(descriptor))
    for ikw in item_keywords:
        item_words.update(_PROMPT_WORD_RE.findall(ikw))
    return len(prompt_words & item_words)


def score_item(item: dict, style: str, prompt_keywords: list[str],
               match_style: bool = True, is_long_room: bool = False,
               is_small_room: bool = False,
               preferred_store: str = "none") -> float:
    """
    評分一件家具（不含類別加分，類別由外層篩選控制）

    - 風格 tag 命中 +3，相近風格 +1（match_style=False 時跳過，用於 fallback）
    - flux_prompt 關鍵字命中（descriptor 或 keywords 或 name）+1/個
    - 顏色命中 +0.5/個
    - 有圖片 +1，有購買連結 +0.5
    - 長條型客廳 + L/U/沙發床 → -5（避免 Nano Banana 跟著 ref 圖做 L 形擋走道）
    - 小空間/尺寸不明 + 電動/三人座/加大沙發 → -4（避免大件把客廳擠得比實際小）
    - preferred_store 符合 → +2（風格 3 > 賣場 2 > 顏色 0.5；非硬篩）
    """
    score = 0.0

    if match_style:
        item_styles = item.get("style_tags", [])
        if style in item_styles:
            score += 3
        elif any(s in item_styles for s in _get_related_styles(style)):
            score += 1

    item_keywords = [kw.lower() for kw in item.get("keywords", [])]
    item_descriptor = (item.get("flux_descriptor") or "").lower()
    item_name = (item.get("name_zh") or "").lower()
    search_text = item_descriptor or item_name
    for pkw in prompt_keywords:
        if pkw and (any(pkw in ikw for ikw in item_keywords) or pkw in search_text):
            score += 1

    # 字詞級比對：上面整句包含比對幾乎不命中（flux_prompt 是英文子句、keywords 是
    # 中文行銷詞），大量商品因此同分。把子句拆單字對 descriptor/keywords 做重疊計分，
    # 每字 +0.4、上限 +2.0（< 風格分 3.0，風格仍主導排序）
    score += min(_prompt_word_overlap(prompt_keywords, item_keywords, item_descriptor) * 0.4, 2.0)

    item_colors = [c.lower() for c in item.get("colors", [])]
    for pkw in prompt_keywords:
        if pkw and any(pkw in color for color in item_colors):
            score += 0.5

    if item.get("image_url"):
        score += 1
    if item.get("purchase_url"):
        score += 0.5

    # 長條型客廳沙發形狀降權
    score += _long_room_sofa_penalty(item, is_long_room)

    # 小空間/尺寸不明沙發體積降權
    score += _oversized_sofa_penalty(item, is_small_room)

    # 奢華系風格 × 日式/無印/北歐命名 → 降權（不硬排除）
    if style in LUXURY_MISMATCH_STYLES:
        _nm = (item.get("name_zh") or "")
        if any(kw in _nm for kw in LUXURY_MISMATCH_KW):
            score += LUXURY_MISMATCH_PENALTY

    # 地毯類的功能性雜項（地墊/防滑墊/門墊）降權 — 不該當主地毯
    if resolve_category(item) == "rug":
        _nm2 = (item.get("name_zh") or "")
        if any(kw in _nm2 for kw in RUG_JUNK_KW):
            score += RUG_JUNK_PENALTY

    # 桌子類的功能性雜項（桌布/餐桌墊/桌旗）降權 — 不該當書桌/餐桌本體
    if resolve_category(item) in ("table", "coffee_table"):
        _nm4 = (item.get("name_zh") or "")
        if any(kw in _nm4 for kw in TABLE_JUNK_KW):
            score += TABLE_JUNK_PENALTY

    # 雙層床/兒童床降權 — 主臥不該出現小孩房家具（9871F294）
    if resolve_category(item) == "bed":
        _nm5 = (item.get("name_zh") or "")
        if any(kw in _nm5 for kw in BED_KIDS_KW):
            score += BED_KIDS_PENALTY

    # 電競系商品配到非 modern/industrial 風格 → 降權（奶油風書房不該出現電競桌）
    if style not in GAMING_ALLOWED_STYLES:
        _nm3 = ((item.get("name_zh") or "") + " " + (item.get("flux_descriptor") or "")).lower()
        if any(kw in _nm3 for kw in GAMING_KW):
            score += GAMING_MISMATCH_PENALTY

    # 多件商品合照（一張圖裡有 2+ 種不同家具）當單一家具參考會誤導渲染，降權
    if is_multi_piece_bundle(item.get("name_zh") or ""):
        score += MULTI_PIECE_BUNDLE_PENALTY

    # 賣場偏好加分
    score += _store_bonus(item, preferred_store)

    return score


def _pick_best_in_category(
    target_cat: str,
    style: str,
    prompt_keywords: list[str],
    catalog: list[dict],
    is_long_room: bool = False,
    is_small_room: bool = False,
    budget_tier: str = "tier3",
    preferred_store: str = "none",
    must_categories: list[str] | None = None,
) -> dict | None:
    """
    在指定 category 中，先撈同風格 → 再 fallback 相近風格 → 否則 None。
    Fallback 只放寬風格，category 鎖死（不准用 chair 替代 sofa）。
    is_long_room=True 時，sofa 撈取會對 L/U/沙發床降權（降權不硬排除，保命撈直線）。
    is_small_room=True 時，sofa 撈取會對電動/三人座/加大等體積偏大款降權。

    Phase A 預算 fallback 三段（在「同風格」與「相近風格」各自內部都跑）：
      strict   : 嚴格符合 BUDGET_CAT_CAP
      relax1.5 : 上限 × 1.5
      open     : 完全放寬（不過濾預算）— 保證 must_have 永不空
    """
    cap = _budget_cap_for(budget_tier, target_cat)

    # 多件商品合照 + 桌面保護膜/地墊類垃圾優先排除（B4174D97 / 20A8220A：
    # 某風格該品類只剩 1 件目錄商品、還是套組或貼膜時，純降權分數贏不了「沒有對手」
    # ——同風格/相近風格池要直接排掉，逼它掉到 Stage B/C 找真的單件家具。
    # Stage C（must-have 保命，跨所有風格）不套用，保證極端情況下 must-have 仍不會空。
    def _prefer_non_bundle(pool: list[dict]) -> list[dict]:
        # 刻意不做「篩完空了退回原池」——這裡要的就是「同風格只剩多件套/垃圾」時
        # 讓 Stage A/B 判定成沒有可用商品。Stage C 沒套此過濾，must-have 不會空。
        out = []
        for it in pool:
            nm = (it.get("name_zh") or "").strip()
            if is_multi_piece_bundle(nm):
                continue
            if target_cat in ("table", "coffee_table") and any(kw in nm for kw in TABLE_JUNK_KW):
                continue   # 桌布/貼膜/防燙墊不是桌子（20A8220A：法式茶几配到家具貼膜）
            if target_cat == "rug" and any(kw in nm for kw in RUG_JUNK_KW):
                continue   # 餐墊/門墊/防滑墊不是主地毯
            if target_cat == "bed" and any(kw in nm for kw in BED_KIDS_KW):
                continue   # 雙層床/兒童床不進主臥（9871F294）
            out.append(it)
        return out

    # tier3 質感底線：太便宜的茶几/地毯/電視櫃不進高預算主家具；
    # 底線篩完空了就回退原池（保證 must-have 永不缺）
    def _apply_tier3_floor(pool: list[dict]) -> list[dict]:
        if budget_tier != "tier3":
            return pool
        floor = TIER3_PRICE_FLOOR.get(target_cat)
        if not floor:
            return pool
        floored = []
        for it in pool:
            try:
                price = int(it.get("price_twd") or 0)
            except (TypeError, ValueError):
                price = 0
            if price <= 0 or price >= floor:   # 沒標價的不硬砍
                floored.append(it)
        return floored or pool

    def _scored_in_pool(pool: list[dict], match_style: bool) -> dict | None:
        if not pool:
            return None
        scored = [
            (
                score_item(it, style, prompt_keywords,
                           match_style=match_style,
                           is_long_room=is_long_room,
                           is_small_room=is_small_room,
                           preferred_store=preferred_store),
                it,
            )
            for it in pool
        ]
        # 同分候選（常見：風格關鍵字沒命中時大量商品打平）用隨機打散，
        # 避免永遠選到目錄陣列裡排最前面那件、每個客戶拿到一樣的家具
        scored.sort(key=lambda x: (-x[0], random.random()))
        return scored[0][1]

    def _filter(items: list[dict], cap_to_use: int | None) -> list[dict]:
        if cap_to_use is None:
            return items
        return [it for it in items if _under_budget(it, cap_to_use)]

    # Stage A: 嚴格同風格
    primary = _apply_tier3_floor(_prefer_non_bundle([
        it for it in catalog
        if resolve_category(it) == target_cat
        and style in it.get("style_tags", [])
    ]))
    if primary:
        # 1. 嚴格預算
        chosen = _scored_in_pool(_filter(primary, cap), match_style=True)
        if chosen is not None:
            return chosen
        # 2. 放寬 1.5×
        if cap is not None:
            relaxed_cap = int(cap * BUDGET_RELAX_MULTIPLIER)
            chosen = _scored_in_pool(_filter(primary, relaxed_cap), match_style=True)
            if chosen is not None:
                print(f"[furniture_match] {budget_tier} {target_cat} 嚴格無解，放寬至 NT${relaxed_cap}")
                return chosen
        # 3. 完全放寬（同風格內任何價位）
        chosen = _scored_in_pool(primary, match_style=True)
        if chosen is not None:
            if cap is not None:
                print(f"[furniture_match] {budget_tier} {target_cat} 同風格內預算內無料，回全價域")
            return chosen

    # Stage B: fallback 到相近風格（同 category）
    related = _get_related_styles(style)
    if related:
        fallback = _apply_tier3_floor(_prefer_non_bundle([
            it for it in catalog
            if resolve_category(it) == target_cat
            and any(s in it.get("style_tags", []) for s in related)
        ]))
        if fallback:
            # 1. 嚴格預算
            chosen = _scored_in_pool(_filter(fallback, cap), match_style=False)
            if chosen is not None:
                return chosen
            # 2. 放寬 1.5×
            if cap is not None:
                relaxed_cap = int(cap * BUDGET_RELAX_MULTIPLIER)
                chosen = _scored_in_pool(_filter(fallback, relaxed_cap), match_style=False)
                if chosen is not None:
                    print(f"[furniture_match] {budget_tier} {target_cat} 相近風格放寬 1.5×")
                    return chosen
            # 3. 完全放寬
            chosen = _scored_in_pool(fallback, match_style=False)
            if chosen is not None:
                if cap is not None:
                    print(f"[furniture_match] {budget_tier} {target_cat} 相近風格放寬全價域")
                return chosen

    # Stage C: must-have 保命 — category 鎖死、風格全放寬（任何風格都行）。
    # 只給 must-have（沙發/茶几/地毯/電視櫃）：這些圖一定會畫，清單不能缺，
    # 否則空格會被從不渲染的 nice-to-have（單椅/邊几）遞補成「圖上沒有的家具」。
    if target_cat in (must_categories if must_categories is not None else LIVING_MUST_HAVE):
        any_style = [it for it in catalog if resolve_category(it) == target_cat]
        if any_style:
            chosen = (_scored_in_pool(_filter(any_style, cap), match_style=False)
                      or _scored_in_pool(any_style, match_style=False))
            if chosen is not None:
                print(f"[furniture_match] {target_cat} 跨風格保命撈取（同/相近風格無料）")
                return chosen

    return None


# 使用者選的『色系』→ 配對用顏色關鍵字（跟 colors 欄位 + flux_descriptor 比對）。
# 20A8220A 後續：色系不准再改商品顏色（見 prompt_builder._palette_clause），
# 那色系要真的有感，就得在「挑商品」這一步就挑顏色相符的——選莫蘭迪粉但目錄
# 沒有粉色沙發時，寧可前端不開放該色系（style-form STYLE_PALETTE_MAP 已按庫存重配）。
PALETTE_COLOR_HINTS = {
    "暖白":   ["白", "米", "原木", "淺木", "white", "beige", "ivory", "oak"],
    "灰調":   ["灰", "石", "大理", "grey", "gray", "marble", "stone"],
    "深色":   ["黑", "深", "胡桃", "金", "black", "dark", "walnut", "gold"],
    "莫蘭迪綠": ["綠", "green", "sage"],
    "莫蘭迪粉": ["粉", "玫瑰", "pink", "rose", "blush"],
    "深藍":   ["藍", "橡木", "blue", "navy", "oak"],
    "奶油":   ["奶油", "米", "磚紅", "棕", "cream", "beige", "terracotta", "brown"],
    "黑白":   ["黑", "白", "black", "white"],
}


def palette_keywords(palette_name: str | None) -> list[str]:
    """色系中文名（例『莫蘭迪粉』『暖白 + 淺木』）→ 顏色關鍵字。對不到 → 空。"""
    pal = (palette_name or "").strip()
    if not pal:
        return []
    for key, kws in PALETTE_COLOR_HINTS.items():
        if key in pal:
            return kws
    return []


def match_furniture(
    style: str,
    flux_prompt: str,
    catalog: list[dict],
    top_n: int = 5,
    mode: str = 'living',
    is_long_room: bool = False,
    is_small_room: bool = False,
    budget_tier: str = "tier3",
    preferred_store: str = "none",
    extra_keywords: list[str] | None = None,
) -> list[dict]:
    """
    兩階段配對（mode='living' 預設）：
      Stage 1: LIVING_MUST_HAVE 每類保證撈 1 件（同風格 → fallback 風格）
      Stage 2: LIVING_NICE_TO_HAVE 補滿 top_n（依分數）
      全程排除 LIVING_EXCLUDED

    is_long_room=True 時，sofa 撈取會避開 L/U/沙發床（跨所有風格）
    is_small_room=True 時，sofa 撈取會避開電動/三人座/加大等體積偏大款
    budget_tier / preferred_store：Phase A 加入，影響評分與品類預算上限
    extra_keywords：額外評分關鍵字（使用者選的色系→顏色字），讓選色影響「挑哪件商品」

    其他 mode 暫沿用「全分類混評分 + 每類 1 件」舊邏輯（未來再擴）
    """
    prompt_keywords = [kw.strip().lower() for kw in flux_prompt.split(",")]
    if extra_keywords:
        prompt_keywords += [str(k).strip().lower() for k in extra_keywords if str(k).strip()]

    # mode 即標準 room_type（living/bedroom/dining/study）；未知房型退回客廳規則。
    rule = ROOM_RULES.get(mode, ROOM_RULES['living'])
    must = rule['must']
    nice = rule['nice']
    excluded = set(rule['excluded'])

    # 先剔除 EXCLUDED 品類
    pool = [it for it in catalog if resolve_category(it) not in excluded]

    selected_by_cat: dict[str, dict] = {}

    # Stage 1: MUST_HAVE
    for cat in must:
        best = _pick_best_in_category(cat, style, prompt_keywords, pool,
                                      is_long_room=is_long_room,
                                      is_small_room=is_small_room,
                                      budget_tier=budget_tier,
                                      preferred_store=preferred_store,
                                      must_categories=must)
        if best is not None:
            selected_by_cat[cat] = best

    # Stage 2: NICE_TO_HAVE
    # 對 nice 仍套上 budget cap（保留 fallback：嚴格→1.5×→放寬）
    remaining = top_n - len(selected_by_cat)
    if remaining > 0:
        nice_pool = [
            it for it in pool
            if resolve_category(it) in nice
            and resolve_category(it) not in selected_by_cat
        ]

        def _under_cat_cap(it):
            cap = _budget_cap_for(budget_tier, resolve_category(it))
            return _under_budget(it, cap)

        def _under_relaxed_cap(it):
            cap = _budget_cap_for(budget_tier, resolve_category(it))
            if cap is None:
                return True
            return _under_budget(it, int(cap * BUDGET_RELAX_MULTIPLIER))

        strict_pool  = [it for it in nice_pool if _under_cat_cap(it)]
        relaxed_pool = [it for it in nice_pool if _under_relaxed_cap(it)] if budget_tier != 'tier3' else nice_pool

        chosen_pool = strict_pool or relaxed_pool or nice_pool

        scored = [
            (score_item(it, style, prompt_keywords,
                        is_long_room=is_long_room,
                        is_small_room=is_small_room,
                        preferred_store=preferred_store), it)
            for it in chosen_pool
        ]
        # 同分候選隨機打散，理由同上（避免 nice-to-have 也永遠固定同一批）
        scored.sort(key=lambda x: (-x[0], random.random()))
        for _, it in scored:
            cat = resolve_category(it)
            if cat in selected_by_cat:
                continue
            selected_by_cat[cat] = it
            if len(selected_by_cat) >= top_n:
                break

    # 以 must 順序優先，nice 依後續加入順序
    ordered: list[dict] = []
    for cat in must:
        if cat in selected_by_cat:
            ordered.append(selected_by_cat[cat])
    for cat, it in selected_by_cat.items():
        if cat not in must:
            ordered.append(it)

    return ordered


def match_soft_furnishing(
    style: str,
    catalog: list[dict],
    budget_tier: str = "tier3",
    preferred_store: str = "none",
) -> list[dict]:
    """
    軟裝接入: 為當前風格挑 3-5 件真正適合放進圖面的軟裝.

    matching 原則 (per spec):
      1. 類別多樣性: 依風格 priority 各 cat 撈 1 件, 最多 5 件
      2. 每件必須有 image_url
      3. 每件必須有 purchase_url
      4. 不硬塞不適合的固定 8 類; 撈不到某 cat 就跳過
      5. 軟裝仍不併主家具總計 (由 result.html 控制)

    與 match_furniture 完全獨立 — 不影響主家具撈取邏輯, 不算進主總計.
    撈不到某 cat 就跳過 (不 fallback 跨類別).
    回傳 list (順序為風格 priority).
    """
    soft_cap = SOFT_FURNISHING_CAP.get(budget_tier)
    cat_order = SOFT_STYLE_CAT_PRIORITY.get(style, SOFT_FURNISHING_CATS)

    def _under_soft_budget(item: dict) -> bool:
        if soft_cap is None:
            return True
        try:
            price = int(item.get('price_twd') or 0)
        except (TypeError, ValueError):
            return True
        return price <= 0 or price <= soft_cap

    def _has_url(it: dict) -> bool:
        img = (it.get("image_url") or "").startswith("http")
        buy = (it.get("purchase_url") or "").startswith("http")
        return img and buy

    selected: list[dict] = []
    for cat in cat_order:
        if len(selected) >= SOFT_FURNISHING_MAX_RESULTS:
            break
        cat_pool = [it for it in catalog if resolve_category(it) == cat]
        # Stage 1: 有圖 + 有購買連結 + 預算內
        pool = [it for it in cat_pool if _has_url(it) and _under_soft_budget(it)]
        if not pool:
            # Stage 2: 放寬預算, 但圖/購買連結仍硬性
            pool = [it for it in cat_pool if _has_url(it)]
        if not pool:
            # 該 cat 缺資料完整的商品 → 跳過, 不放寬到無連結商品
            continue
        scored = [
            (score_item(it, style, [],
                        match_style=True,
                        is_long_room=False,
                        preferred_store=preferred_store), it)
            for it in pool
        ]
        scored.sort(key=lambda x: -x[0])
        selected.append(scored[0][1])

    # 紗簾延伸項目：主窗簾若不是紗質款，額外配一件紗簾當「延伸購買品項」——
    # 不佔前面 5 件主配額（append 在迴圈外，len(selected) 上限檢查管不到這裡），
    # 也不會被拿去當生成參考圖（prompt_builder 每類目只取第 1 件當視覺參考，
    # 這件排第二，自動只進購物清單、不影響渲染畫面）。
    curtain_pick = next((it for it in selected if resolve_category(it) == "curtain"), None)
    if curtain_pick and not any(kw in (curtain_pick.get("name_zh") or "") for kw in SHEER_CURTAIN_KW):
        sheer_pool = [
            it for it in catalog
            if resolve_category(it) == "curtain"
            and any(kw in (it.get("name_zh") or "") for kw in SHEER_CURTAIN_KW)
            and _has_url(it)
            and it.get("id") != curtain_pick.get("id")
        ]
        if sheer_pool:
            scored_sheer = [
                (score_item(it, style, [], match_style=True, preferred_store=preferred_store), it)
                for it in sheer_pool
            ]
            scored_sheer.sort(key=lambda x: -x[0])
            # 紗簾目錄只標了 nordic/modern/cream 三種風格（用戶查證抓到：11 件裡
            # 7 種風格完全沒標籤）。沒標籤的風格會讓所有候選同分，排序退化成
            # 純目錄順序——與其挑一件不對版的充數，寧可不加。base 分數只有
            # image(+1)+purchase_url(+0.5)=1.5，>1.5 代表真的吃到風格加分。
            if scored_sheer[0][0] > 1.5:
                selected.append(scored_sheer[0][1])

    return selected


def _legacy_match(style: str, prompt_keywords: list[str], catalog: list[dict], top_n: int) -> list[dict]:
    """舊邏輯（非 living 模式 fallback 用）：混評分 + 每類 1 件"""
    scored = []
    for item in catalog:
        sc = score_item(item, style, prompt_keywords)
        if sc > 0:
            scored.append((sc, item))
    scored.sort(key=lambda x: -x[0])
    selected = []
    cat_seen: set[str] = set()
    for _, item in scored:
        cat = item.get("category", "other")
        if cat in cat_seen:
            continue
        cat_seen.add(cat)
        selected.append(item)
        if len(selected) >= top_n:
            break
    return selected


def _get_related_styles(style: str) -> list[str]:
    """風格相近群組，用於寬鬆配對"""
    groups = [
        ["modern", "muji", "nordic", "cream"],
        ["japanese", "muji", "wood"],
        ["luxury", "art-deco", "french"],
        ["cream", "nordic", "muji", "wood"],
        ["wood", "japanese", "muji", "nordic"],
        ["french", "luxury", "art-deco"],
        ["chinese-modern", "luxury"],
        # 舊風格保留相性（catalog 舊品項仍有這些 tag）
        ["boho", "wood"],
        ["industrial", "modern"],
    ]
    for group in groups:
        if style in group:
            return [s for s in group if s != style]
    return []


def _width_cap_for_short_side(short_side_m: float) -> int:
    """短邊（米）→ 沙發寬度上限（cm），分段而非線性比例。
    舊版 short_side*0.55 對 3m 寬的正常長條客廳只給 165cm——沙發會迷你到像套房，
    質感反而下降（GPT 抓漏）。真實室內尺度：3m 寬客廳放 220cm 沙發沿長牆完全正常，
    擋不擋走道主要看「深度/形狀/位置」，那由 L/U/貴妃降權 + prompt 走道規則管。"""
    if short_side_m < 2.6:
        return 200
    if short_side_m < 3.2:
        return 220
    if short_side_m < 3.8:
        return 240
    return 300   # 大空間：≥280 時 filter_by_dimensions 不過濾（與舊行為一致）


def parse_max_width_cm(estimated_size: str, room_dims: dict | None = None) -> int:
    """
    根據坪數或實際尺寸，返回家具最大允許寬度（cm）。
    避免 8 坪小空間配到 W270cm 大沙發，也避免過度保守選到迷你沙發。
    """
    if room_dims:
        width_m = room_dims.get("width_m", 0)
        length_m = room_dims.get("length_m", 0)
        short_side = min(width_m, length_m) if width_m and length_m else 0
        if short_side > 0:
            return _width_cap_for_short_side(short_side)

    # 從坪數字串提取數字範圍的下限
    nums = re.findall(r'\d+', str(estimated_size))
    if nums:
        sqping = int(nums[0])  # 取下限
        sqm = sqping * 3.305
        short_side_est = (sqm ** 0.5) * 0.85  # 估算短邊
        return _width_cap_for_short_side(short_side_est)
    # 完全無法判斷空間大小：真實目錄（momo/pchome）99% 商品沒填 dimensions，
    # 數值過濾本身幾乎不會生效，這個預設主要靠關鍵字降權把關。
    # 寧可保守（可能誤降權中型沙發）也不要放任特大件（GPT+Grok 共識）。
    return SMALL_ROOM_MAX_WIDTH_DEFAULT


# 裸 cm 匹配：(?<!\d) 禁止從數字中間起匹配（否則 '高180cm' 會從 '80cm' 配到 80）
_BARE_CM_RE = re.compile(r'(?<!\d)(\d+)\s*cm', re.IGNORECASE)
# 數字前綴若是高/深/直徑類標記 → 不是寬度
_NON_WIDTH_PREFIX_RE = re.compile(r'(?:高|深|直徑|dia|ø|[hd])\s*$', re.IGNORECASE)


def _extract_width_cm(dims: str, allow_bare: bool = False) -> int | None:
    """從 dimensions 字串抓寬度（cm）。真實目錄（momo/pchome 爬蟲）格式很雜，
    只認 'W270' 會漏掉大多數真實商品（例：'-270cm-'）。依序嘗試：
      1. 'W270' / 'W 270' / '寬270' 標準格式（所有品類）
      2. allow_bare=True 時才收裸數字+cm（'-270cm-' / '長270cm'）——只給 sofa 用，
         且數字必須完整（不能從 '高180cm' 中段配出 80，GPT round-3 抓漏）、
         前綴是 高/深/H/D/Dia/直徑 一律不當寬度。
    抓不到回 None（不過濾，維持原行為）。"""
    if not dims:
        return None
    w_match = re.search(r'[W寬]\s*(\d+)', dims)
    if w_match:
        return int(w_match.group(1))
    if allow_bare:
        for m in _BARE_CM_RE.finditer(dims):
            if _NON_WIDTH_PREFIX_RE.search(dims[:m.start(1)]):
                continue   # 高180cm / D90cm / Dia40cm：非寬度標記，跳過
            return int(m.group(1))
    return None


def filter_by_dimensions(items: list[dict], max_width_cm: int) -> list[dict]:
    """過濾掉寬度超過 max_width_cm 的家具。
    裸 'NNNcm' 格式只對 sofa 當寬度（電商沙發常只標總長=寬度；
    其他品類的單一數字常是高度/直徑/深度，照抓會誤殺窗簾/燈具/櫃子）。"""
    if max_width_cm >= 280:  # 大空間不過濾
        return items
    result = []
    for item in items:
        allow_bare = resolve_category(item) == "sofa"
        w = _extract_width_cm(item.get("dimensions", ""), allow_bare=allow_bare)
        if w is not None and w > max_width_cm:
            continue  # 家具太大，跳過
        result.append(item)
    return result


def enrich_renders(renders: list[dict], analysis: dict | None = None,
                   budget_tier: str = "tier3",
                   preferred_store: str = "none",
                   room_type: str = "living",
                   palettes: dict | None = None) -> list[dict]:
    """
    主入口：為每個 render 加上配對家具

    輸入格式（來自 gemini_analyze.py）：
    [{"style": "modern", "style_label": "現代簡約", "flux_prompt": "..."}]

    analysis: Gemini 分析結果（含 estimated_size 和 room_dimensions）
    budget_tier: 'tier1' / 'tier2' / 'tier3'（影響品類預算上限與 fallback）
    preferred_store: 'none'/'momo'/'ikea'/'hola'/'trplus'（評分加分，不硬篩）
    palettes: {style_id: 使用者選的色系中文名}——轉成顏色關鍵字進評分，
              讓「選莫蘭迪粉」真的優先挑到粉色調商品（色系不再改商品顏色後，
              這是色系影響成品的正道）

    輸出：每個 render 加上 "matched_furniture" 欄位
    """
    catalog = load_catalog()
    if not catalog:
        print("[furniture_match] 警告：目錄為空，跳過配對")
        return renders

    # 根據空間大小決定家具最大寬度
    estimated_size = analysis.get("estimated_size", "") if analysis else ""
    room_dims = analysis.get("room_dimensions") if analysis else None
    max_w = parse_max_width_cm(estimated_size, room_dims)
    print(f"[furniture_match] 空間: {estimated_size} → 傢俱寬度上限: {max_w}cm")

    # 長條型客廳判定（root cause fix for L sofa-in-narrow-room）
    # 66784D97 抓漏：7.5×3.8m 房長寬比 1.97 差 0.03 沒過門檻 → L 沙發照配。
    # 縱深 >= 6m 的房不管比例都是長房（沙發沿長牆、走道縱貫），補絕對深度條件。
    aspect = compute_room_aspect_ratio(room_dims)
    _long_side = 0.0
    if isinstance(room_dims, dict):
        try:
            _long_side = max(float(room_dims.get("length_m") or 0),
                             float(room_dims.get("width_m") or 0))
        except (TypeError, ValueError):
            _long_side = 0.0
    is_long_room = (aspect >= LONG_ROOM_ASPECT_THRESHOLD) or (_long_side >= LONG_ROOM_ABS_LENGTH_M)
    if aspect > 0:
        print(f"[furniture_match] 客廳長寬比={aspect:.2f} 長邊={_long_side:.1f}m "
              f"is_long_room={is_long_room}"
              + (f"  → L/U/沙發床 降權 {LONG_ROOM_SHAPE_PENALTY}" if is_long_room else ""))

    # 小空間 / 尺寸不明判定（root cause fix for C15719C5 — 270cm 法式沙發把客廳擠小）
    is_small_room = max_w <= SMALL_ROOM_MAX_WIDTH_DEFAULT
    if is_small_room:
        print(f"[furniture_match] 空間偏小或尺寸不明 (上限={max_w}cm) "
              f"→ 電動/三人座/加大沙發降權 {SMALL_ROOM_OVERSIZED_PENALTY}")

    print(f"[furniture_match] budget_tier={budget_tier} preferred_store={preferred_store}")

    enriched = []
    for render in renders:
        style = render.get("style", "")
        flux_prompt = render.get("flux_prompt", "")
        # 使用者選的色系 → 顏色關鍵字（顏色命中 +0.5/字詞重疊加分，軟性偏好不硬篩）
        pal_kws = palette_keywords((palettes or {}).get(style))
        if pal_kws:
            print(f"[furniture_match] {style} 色系關鍵字加權: {pal_kws[:4]}…")
        # 先過濾尺寸再配對，避免唯一的電視櫃在選中後才被刪除。
        room_catalog = filter_by_dimensions(catalog, max_w)
        matched = match_furniture(style, flux_prompt, room_catalog, top_n=5, mode=room_type,
                                  is_long_room=is_long_room,
                                  is_small_room=is_small_room,
                                  budget_tier=budget_tier,
                                  preferred_store=preferred_store,
                                  extra_keywords=pal_kws)
        matched = matched[:5]

        render_copy = dict(render)
        # 數值版長條房信號給 prompt_builder 用：zoning 的 room_shape 用詞不可靠
        # （63B7B5C9 深長型房被寫成「長方形」→ 窄房保守軟裝模式沒觸發，
        # 落地花盆把沙發擠向走道）。長寬比 >= 2.0 是穩定的數值判斷。
        render_copy["_is_long_room"] = bool(is_long_room)
        render_copy["matched_furniture"] = [
            {
                "id": item.get("id", ""),
                "name_zh": item.get("name_zh", ""),
                "brand": item.get("brand", ""),
                # 原始中文類別保留，另加 category_en 反映細分結果
                "category": item.get("category", ""),
                "category_en": resolve_category(item),
                "price_twd": item.get("price_twd", 0),
                "image_url": item.get("image_url", ""),
                "purchase_url": item.get("purchase_url", ""),
                "flux_descriptor": item.get("flux_descriptor", "") or item.get("name_zh", ""),
                "dimensions": item.get("dimensions", ""),
                "colors": item.get("colors", []),
            }
            for item in matched
        ]

        # 軟裝接入 (2026-06-18): 額外撈 pillow/curtain/wall_art/vase/plant 進 soft_furnishing,
        # 跟 matched_furniture 完全分開. 結果頁獨立顯示「軟裝搭配建議」, 不併主總計.
        soft = match_soft_furnishing(style, catalog,
                                     budget_tier=budget_tier,
                                     preferred_store=preferred_store)
        render_copy["soft_furnishing"] = [
            {
                "id": item.get("id", ""),
                "name_zh": item.get("name_zh", ""),
                "brand": item.get("brand", ""),
                "category": item.get("category", ""),
                "category_en": resolve_category(item),
                "price_twd": item.get("price_twd", 0),
                "image_url": item.get("image_url", ""),
                "purchase_url": item.get("purchase_url", ""),
            }
            for item in soft
        ]
        if soft:
            cats_dbg = [resolve_category(it) for it in soft]
            print(f"[furniture_match] soft_furnishing ({style}): "
                  f"{len(soft)} 件 cats={cats_dbg}")

        enriched.append(render_copy)

    return enriched


def build_reference_image_list(matched_furniture: list[dict]) -> list[str]:
    """提取有圖片的家具 URL，用於 Flux reference image"""
    return [
        item["image_url"]
        for item in matched_furniture
        if item.get("image_url") and item["image_url"].startswith("http")
    ]


def print_furniture_table(renders: list[dict]):
    """印出家具配對結果表格（CLI 用）"""
    import sys
    out = sys.stdout
    enc = getattr(out, "encoding", "utf-8") or "utf-8"

    def safe_print(text: str):
        print(text.encode(enc, errors="replace").decode(enc))

    for render in renders:
        style = render.get("style_label", render.get("style", ""))
        furniture = render.get("matched_furniture", [])
        safe_print(f"\n{'='*80}")
        safe_print(f"Style: {style}")
        safe_print(f"{'='*80}")
        safe_print(f"{'Name':<30} {'Brand':<10} {'Cat':<10} {'Price(TWD)':<12} {'Dims':<20}")
        safe_print("-" * 84)
        for item in furniture:
            safe_print(
                f"{item['name_zh'][:28]:<30} "
                f"{item['brand'][:8]:<10} "
                f"{item['category']:<10} "
                f"{item['price_twd']:<12} "
                f"{item.get('dimensions','')[:18]:<20}"
            )


if __name__ == "__main__":
    # 測試：用假 renders 跑配對
    test_renders = [
        {
            "style": "modern",
            "style_label": "現代簡約",
            "flux_prompt": "white oak panels, minimalist linen sofa, recessed LED ceiling, floating TV console, light greige palette",
        },
        {
            "style": "nordic",
            "style_label": "北歐 Scandinavian",
            "flux_prompt": "white birch, wool armchair, pine dining table, oversized pendant, cotton linen curtain, sheepskin rug",
        },
        {
            "style": "boho",
            "style_label": "波希米亞 Boho",
            "flux_prompt": "kilim rug, macrame wall hanging, rattan hanging chair, string fairy lights, floor cushion, monstera plant",
        },
    ]

    enriched = enrich_renders(test_renders)
    print_furniture_table(enriched)

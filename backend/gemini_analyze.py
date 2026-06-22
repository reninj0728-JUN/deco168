"""
Step 1 — Gemini 3.1 Flash-Lite 分析影片
輸入：影片路徑 + 用戶選擇的風格（可選）
輸出：dict（空間描述 + 3 個 Flux prompt，風格由用戶指定或 AI 推薦）
"""
import os
import json
import time
from google import genai
from google.genai import types


SYSTEM_PROMPT = """
你是台灣頂尖室內設計 AI，專精空間分析與 Flux Kontext Pro 圖像生成 Prompt 撰寫。

━━ FLUX PROMPT 鐵則 ━━
1. 只用逗號分隔的 keyword，禁止長句敘述
2. 順序：家具材質 → 表面處理 → 燈光 → 風格氛圍 → 攝影後綴
3. 每個 prompt 結尾固定加：professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD
4. 根據空間實際採光、格局特性選詞，不要每次都一樣
5. 禁止模糊詞：beautiful / nice / cozy / elegant / stunning（無法引導生圖）
6. 禁止重複關鍵字，每個詞都要有實際意義
7. 加入具體材質表面處理：brushed / matte / polished / aged / whitewashed / oiled

✅ 正確格式（攝影感）：
warm white oak panels, matte finish, linen sofa in cream, recessed LED ceiling, warm 3000K accent, contemporary minimalist living room, professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD

❌ 錯誤格式：
The room has beautiful natural wood elements — 禁止長句
photorealistic interior render — 禁止，這樣會出 CGI 感而非攝影感

━━ 負面提示（每個 flux_prompt 結尾必加）━━
no people, no text, no watermark, no distortion, no cartoon, no oversaturated colors, no unrealistic proportions, no CGI artifacts, no floating objects

━━ 空間規模判斷 ━━
【精準尺規法】觀察以下基準物估算空間：
- 門框高度 ≈ 200cm，寬度 ≈ 90cm
- 標準天花板高度 ≈ 240-270cm（台灣老公寓240，新成屋270）
- 標準沙發高度 ≈ 80-90cm，深度 ≈ 90cm
- 插座距地板 ≈ 30cm
- 窗台距地 ≈ 90cm，窗高 ≈ 120-150cm
用這些基準估算房間長寬高，若可見多個參照物要交叉比對，給出 estimated_length_m / estimated_width_m / estimated_height_m

小空間（<15坪）→ 加：light reflective surface, open concept, visual expansion, mirror accent panel
中空間（15-35坪）→ 標準詞庫即可
大空間（>35坪）→ 加：double-height ceiling, statement furniture, architectural feature wall

━━ 10 種風格詞庫 ━━

【都會簡約 modern】台灣最主流，適合小坪數
材質：white oak / ash wood panels / matte concrete / brushed aluminum / frosted glass
色調：warm white / off-white / light greige / soft charcoal / nude beige
燈光：recessed LED ceiling / linear pendant / warm 3000K cove / indirect wall wash
家具：minimalist linen sofa / floating TV console / slim oak dining table / open shelving
台灣特性：善用間接照明掩蓋低矮天花板，淺色擴大小空間視覺感

【日式侘寂 japanese】療癒感強，台灣最受歡迎
材質：natural cedar / washi texture wall / stone tile / aged linen / unfinished oak / shoji screen
色調：warm sand / muted clay / soft taupe / earth tones / moss green / ash grey
燈光：diffused natural light / low warm pendant / indirect wall wash / paper lantern glow
家具：low platform seating / ceramic vessels / hand-thrown pottery / tatami-inspired mat
台灣特性：台灣濕熱氣候，強調通風感、自然材質透氣性

【現代輕奢 luxury】台中高端市場主流
材質：Carrara marble / travertine / brushed brass / black steel / velvet / lacquered wood
色調：dark charcoal / midnight navy / warm ivory / champagne gold / deep walnut
燈光：dramatic pendant / wall sconces / LED strip accent / chandelier / spotlighting
家具：curved velvet sofa / marble dining table / brass coffee table / sculptural side chair
台灣特性：梁柱多，用深色或造型天花板化解，強調局部奢華而非全面堆砌

【北歐清簡 nordic】溫馨留白，家庭友善
材質：white birch / pine wood / wool felt / cotton linen / rattan / light plywood
色調：pure white / soft cream / powder blue / blush pink / dusty sage / muted terracotta
燈光：oversized pendant / warm Edison bulb / floor lamp beside sofa / natural daylight maximize
家具：clean-leg dining chair / hygge armchair / storage bench / modular shelving / sheepskin rug
台灣特性：台灣潮濕，選材避免實木易變形，偏向貼皮板材或金屬腳家具

【無印極簡 muji】功能主義，極簡美學
材質：unfinished beech / natural cotton canvas / recycled paper texture / light ash / matte ceramic
色調：off-white / warm beige / light grey / natural linen / pale wood
燈光：diffused ceiling panel / simple pendant / task lamp / maximize window natural light
家具：modular storage system / folding stool / low bed frame / wall-mounted desk / woven storage basket
台灣特性：台灣小坪數剛需，MUJI 風模組化收納解決問題，視覺整潔不壓迫

【奶油暖居 cream】近年台灣最主流，溫柔質感
材質：light oak / natural linen / boucle fabric / matte ceramic / rattan / warm plaster
色調：warm ivory / cream white / linen beige / soft camel / warm sand / oatmeal
燈光：warm 2700K pendant / fabric shade floor lamp / indirect warm wash / sheer curtain diffused natural light
家具：rounded boucle sofa / curved wood side table / woven storage basket / soft linen cushions
台灣特性：搭配木地板和大理石紋磚效果最佳，近年台灣 IG 裝潢最多點讚風格

【森林原木 wood】自然療癒，森系氣息
材質：solid oak / walnut / cedar / bamboo / natural stone / cotton linen / hand-woven textile
色調：warm amber / honey wood / caramel / natural sand / forest green / earthy brown
燈光：warm Edison bulb / rattan pendant / maximize skylight / candle warmth
家具：solid wood dining table / live-edge coffee table / woven rattan chair / wooden open shelving
台灣特性：結合台灣在地木材文化，搭配植栽（黃金葛/虎尾蘭）效果自然，台灣氣候易於養植

【法式浪漫 french】弧線美學，女性市場首選
材質：carved wood moulding / aged linen / velvet / polished brass / marble / cane rattan
色調：ivory white / soft grey / dusty blue / rose blush / champagne / antique white
燈光：crystal chandelier / brass wall sconce / draped fabric shade / warm romantic diffused light
家具：curved Louis-style sofa / cane bistro chair / ornate mirror / antique console table / floor-length linen curtain
台灣特性：法式風台灣女性市場接受度高，適合採光好或挑高空間，梁柱可用石膏線條裝飾

【新中式美學 chinese-modern】東方美學，高端市場
材質：dark walnut / rosewood veneer / jade green tile / brushed bronze / rice paper screen / black stone
色調：deep walnut / terracotta / ink black / jade green / warm gold / cream white
燈光：low hanging lantern-inspired pendant / indirect cove / architectural spotlighting / paper screen ambient
家具：low profile tea table / lattice screen partition / ceramic accent piece / calligraphy wall art / woven cushion
台灣特性：30-50 歲高端客群，融合東方美學，強調留白精緻工藝，適合中大坪數開放格局

━━ 空間規模判斷 ━━
小空間（<15坪）→ 加：light reflective surface / open concept / visual expansion / mirror panel
中空間（15-35坪）→ 標準詞庫即可
大空間（>35坪）→ 加：double-height ceiling / statement furniture / architectural feature wall

━━ 梁柱因應（台灣老公寓常見，若影片可見明顯梁柱）━━
modern         → floating ceiling soffit, concealed beam, indirect cove lighting
japanese       → exposed beam aesthetic, natural wood beam wrap, zen architectural detail
luxury         → coffered ceiling, architectural beam feature, dramatic pendant to draw eye down
nordic         → painted beam white, integrated beam shelf, casual hygge aesthetic
muji           → concealed beam panel, flush ceiling, minimal distraction
art-deco       → geometric beam casing, gold trim accent, architectural feature
cream          → soft plaster beam wrap, warm ivory tone, indirect warm cove light above
wood           → celebrate natural beam, stain to match floor, wooden beam as design feature
french         → decorative plaster moulding wrap, champagne tone, ornate corbel accent
chinese-modern → dark walnut beam casing, lattice screen integration, architectural statement

━━ 風格與空間相性（AI 推薦邏輯）━━
採光充足 + 小坪數  → modern / muji / nordic / cream
採光不足 + 小坪數  → japanese（善用燈光製造溫度）/ muji / wood
大坪數 + 高天花板  → luxury / art-deco / french / chinese-modern
南向大窗 + 熱帶氣候 → wood / cream
屋主提到放鬆療癒   → japanese / wood / nordic / cream
屋主提到高端質感   → luxury / art-deco / french / chinese-modern
屋主提到個性獨特   → art-deco / chinese-modern / french
屋主提到實用收納   → muji / modern / nordic

━━ Few-shot 示範（照這個品質輸出）━━

空間：25坪客廳，南向採光充足，無現有裝潢，門框可見 → 估算長6m×寬5m×高2.6m

flux_prompt for modern：
warm white oak panels matte finish, minimalist linen sofa cream fabric, floating TV console lacquered white, recessed LED strip ceiling, warm 3000K cove light, floor-to-ceiling windows southern light, light greige palette, contemporary minimalist living room, professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts

flux_prompt for japanese：
oiled natural cedar wall panels, washi texture accent wall matte, honed stone tile floor, low platform seating natural linen, hand-thrown ceramic vessels, diffused south window light, indirect warm wall wash, moss green accent, Japanese wabi-sabi minimalist, professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts

flux_prompt for nordic：
white birch veneer panels, wool felt armchair oatmeal, solid pine dining table natural, oversized linen pendant warm 2700K, sheer cotton curtain floor length, soft cream and dusty sage palette, hygge living room styled, Scandinavian minimalist, professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts

flux_prompt for cream：
warm ivory boucle sofa rounded silhouette, light oak side table matte finish, linen curtain sheer floor length, rattan woven storage basket, soft cream terrazzo tile floor, warm 2700K pendant fabric shade, cream and camel warm palette, warm minimalist cream interior styled, professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts
"""

VALID_STYLES = ["modern", "japanese", "luxury", "nordic", "muji", "cream", "wood", "french", "chinese-modern"]


SPACE_TYPE_LABELS = {
    "living":  "客廳",
    "dining":  "餐廳",
    "bedroom": "臥室",
    "study":   "書房",
    "whole":   "全室",
}


def analyze_space(
    video_path: str,
    user_styles: list[str] | None = None,
    is_uri: bool = False,
    extra_photos: list[str] | None = None,
    space_type: str = "living",
) -> dict:
    """
    分析空間影片並生成 Flux prompts。
    video_path: 本機路徑 或 Gemini Files URI（is_uri=True 時直接使用）
    extra_photos: 用戶另外上傳的照片清單，會一起送 Gemini 幫助理解全室
    space_type: 用戶選的目標空間（living/dining/bedroom/study/whole）
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_AI_KEY 未設定，請在 Railway Variables 設定")
    client = genai.Client(api_key=api_key.strip())

    # 用戶必須選 2 種風格（前端強制），不再 AI 自動推薦補齊
    if user_styles and all(s in VALID_STYLES for s in user_styles):
        fixed_styles = user_styles[:2]
        mode = "user_selected"
    else:
        # 沒指定 fallback：給 2 種最通用
        fixed_styles = ["modern", "nordic"]
        mode = "fallback"
    style_instruction = (
        f"用戶選定 {len(fixed_styles)} 種風格：{', '.join(fixed_styles)}。"
        f"renders 陣列必須恰好 {len(fixed_styles)} 個，順序、style 欄位完全對應。"
    )

    if is_uri:
        # 直接用已上傳的 Gemini Files URI，不重複上傳
        from google.genai import types as _types
        video_file = type('F', (), {'uri': video_path, 'mime_type': 'video/mp4'})()
        print(f"[Gemini] 使用既有影片 URI: {video_path[:60]}")
    else:
        print(f"[Gemini] 上傳影片: {video_path}")
        video_file = client.files.upload(file=video_path)
        while video_file.state.name == "PROCESSING":
            print("[Gemini] 影片處理中...")
            time.sleep(3)
            video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        raise RuntimeError("Gemini 影片上傳失敗，請確認檔案格式")

    # 把用戶照片也讀進來給 Gemini（建立完整空間理解）
    import base64 as _b64
    photo_parts = []
    if extra_photos:
        for p in extra_photos:
            try:
                ext = os.path.splitext(p)[1].lower()
                mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
                with open(p, "rb") as f:
                    photo_parts.append(types.Part.from_bytes(
                        data=f.read(), mime_type=mime
                    ))
            except Exception as _e:
                print(f"[Gemini] 無法讀照片 {p}: {_e}")

    photos_note = (
        f"另外附 {len(photo_parts)} 張用戶上傳的照片（第 0~{len(photo_parts)-1} 張）。"
        f"影片用來理解整體格局，照片是用戶選的「想呈現的角度」。"
        if photo_parts else "本次只有影片，沒有額外照片。"
    )
    space_label = SPACE_TYPE_LABELS.get(space_type, "客廳")
    if space_type == "whole":
        scope_instruction = (
            "用戶選的是【全室】——識別空間裡幾個主要房間/區域"
            "（例如：客廳、餐廳、廚房、主臥、書房、玄關）。"
            "regions 陣列**必須恰好 3 個**：挑最值得渲染的 3 個房間（優先客廳/主臥/餐廳之類核心空間）。"
        )
    else:
        scope_instruction = (
            f"用戶選的是【{space_label}】——只聚焦這一個房間。"
            "regions 陣列**必須恰好 3 個**：同一個房間的 3 個不同角度（例如：全景、沙發角、電視牆角）。"
        )

    prompt = f"""
分析這個空間（影片 + 照片）。本次用戶目標空間：【{space_label}】

{scope_instruction}

{photos_note}

【空間量測步驟 — 必須先做】
1. 找出可見的基準物：門框（高200cm/寬90cm）、窗台（距地90cm）、插座（距地30cm）
2. 用基準物推算房間的長度、寬度、天花板高度（公尺）
3. 交叉比對影片不同段落和各張照片的基準物
4. 用長×寬計算坪數（1坪=3.305㎡），給保守估計

【完整空間理解 — 完美復刻所需資訊】
找出並回報：
- 門的位置（主入口、房門、浴室門各在哪面牆、什麼方位）
- 廚房位置（如果有；開放式/獨立、靠哪面牆）
- 窗戶位置（哪面牆、大小估計、是否落地窗）
- 天花板特徵（梁柱、明管、灑水頭、燈具位置）
- 地板材質與顏色
- 牆面現況（油漆顏色、是否有裝潢）

{style_instruction}

回傳以下 JSON（嚴格照格式）：

{{
  "space_type": "空間類型",
  "estimated_size": "估計坪數",
  "room_dimensions": {{
    "length_m": 數字,
    "width_m": 數字,
    "height_m": 數字,
    "confidence": "high/medium/low",
    "reference_used": "用了哪些基準物"
  }},
  "architectural_features": {{
    "doors": "門位置描述，例如：主入口在北牆中央，浴室門在東牆",
    "kitchen": "廚房位置或 '無'",
    "windows": "窗戶位置描述",
    "ceiling": "天花板特徵，例如：明管走線、有梁",
    "floor": "地板材質與顏色",
    "walls": "牆面現況"
  }},
  "layout_notes": "格局描述",
  "lighting": "採光條件",
  "current_style": "目前裝潢風格",
  "owner_requests": "屋主需求（沒說填 '未提及'）",
  "design_analysis": "空間分析摘要，繁體中文，80字以內",
  "recommended_styles": ["style_id_1", "style_id_2", "style_id_3"],
  "recommend_reason": "推薦原因，繁體中文，50字以內",
  "best_photo_index": {("0~" + str(len(photo_parts)-1) + " 整數，指最美/最完整的主角度") if photo_parts else "null"},
  "regions": [
    {{
      "name": "區域名稱（例如：客廳、餐廳、主臥；或 全景視角、沙發角度 之類）",
      "description": "區域格局描述，30字內",
      "best_photo_index": {("0~" + str(len(photo_parts)-1) + " 整數，這個區域最適合用第幾張用戶照片渲染") if photo_parts else "null"},
      "video_position": "0.0~1.0 浮點數，這個區域在影片裡大約出現的位置（用戶若沒上傳照片或照片不足時用）"
    }}
  ],
  "renders": [
    {{"style":"style_id（必須對應用戶選的 {fixed_styles}）","style_label":"中文名稱","flux_prompt":"逗號分隔 keyword，結尾必須是 professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts"}}
  ]
}}

【極重要規則】
- regions 陣列**必須恰好 3 個**，順序為「最重要/最值得呈現」優先。
- 每個 region 的 best_photo_index 與 video_position **都要填**：best_photo_index 是首選，video_position 是備案。
- 全室模式（space_type=whole）：每個 region 對應不同房間，可能要用不同照片或不同影片時間點。
- 單房模式（其他）：每個 region 對應同一個房間的不同角度。
- renders 陣列必須恰好 {len(fixed_styles)} 個，順序對應用戶選的風格：{fixed_styles}。
- flux_prompt 要根據空間實際採光、格局選詞。<15坪加 light reflective surface, open concept, visual expansion。
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=[video_file] + photo_parts + [prompt],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
        ),
    )

    try:
        client.files.delete(name=video_file.name)
    except Exception:
        pass

    result = json.loads(response.text)
    result["_mode"] = mode  # 記錄是 AI 推薦還是用戶指定
    return result


# 硬傷 flag（2026-06-21）：交付/重生的單一判準。任一為 true → render 不可交付，需重生。
# 軟傷（深度小偏差、茶几略偏、軟裝不齊等）不在此列 → 照常交付。
# 分類依據見產品決策：結構破壞 / 動線阻塞 / 沙發錯邊 / 跑錯分區 / 背窗 / 完全沒對向 = 硬傷。
HARD_FAIL_FLAGS = (
    # 結構破壞
    "kitchen_added", "recessed_space_added", "windows_changed",
    "walls_changed", "ceiling_changed", "floor_changed",
    # 動線阻塞（封門 / 擋走道）
    "furniture_blocks_walkway", "sofa_faces_walkway",
    # 核心配置錯誤
    "sofa_on_wrong_side",                 # 沙發放錯確認側
    "sofa_outside_living_zone",           # 客廳跑錯分區
    "sofa_back_against_window",           # 沙發背窗
    "focal_anchor_misaligned_with_sofa",  # 沙發與電視完全沒對向 / not_present
)


# 位置語意關鍵字：補充說明含這些才算「位置指令」，才啟動 strict_depth。
# 「喜歡淺木色」「不要紅色」這類非位置補充不該啟動嚴格深度驗收。
_POSITION_INTENT_KW = (
    "靠窗", "窗邊", "窗戶", "近窗", "後段", "後半", "深處", "底端", "底部",
    "尾端", "末端", "中段", "中間", "中央", "前段", "前半", "入口", "靠門",
    "近門", "玄關", "左側", "右側", "靠牆", "牆邊",
    "window", "rear", "back", "deep", "front", "middle", "center", "centre", "entrance",
)


def _note_has_position_intent(note: str | None) -> bool:
    """補充說明是否含位置語意（決定要不要啟動嚴格深度驗收）。"""
    n = (note or "").strip()
    return bool(n) and any(k in n for k in _POSITION_INTENT_KW)


def _compute_strict_depth(target_note: str | None,
                          target_hint: str | None,
                          has_dining_middle_constraint: bool) -> bool:
    """是否啟動嚴格深度驗收：使用者「明確指定位置」時才啟動。
    觸發來源：補充說明含位置語意 / hint=rear_near_window / 餐廳在中段約束。
    純文字偏好（淺木色、不要紅色…）不啟動。"""
    return (
        _note_has_position_intent(target_note)
        or has_dining_middle_constraint
        or target_hint == "rear_near_window"
    )


def _bbox_center(b):
    """[ymin,xmin,ymax,xmax] → (cy, cx)；格式不符回 None。"""
    if not (isinstance(b, (list, tuple)) and len(b) == 4):
        return None
    try:
        ymin, xmin, ymax, xmax = [float(v) for v in b]
    except (TypeError, ValueError):
        return None
    return ((ymin + ymax) / 2.0, (xmin + xmax) / 2.0)


def _grounded_depth_pct(target_bbox, window_bbox):
    """用渲染圖上偵測到的 bbox，客觀算「target 離窗多近」→ 深度%（窗=100，最遠端=0）。
    取「窗離畫面邊較遠」的那個軸當深度長軸；資料不足回 None。
    用途：取代 Gemini 自述的深度百分比（它常把中段沙發誤報成靠窗）。"""
    tc = _bbox_center(target_bbox)
    wc = _bbox_center(window_bbox)
    if not tc or not wc:
        return None
    wy, wx = wc
    span_y = max(wy, 1000.0 - wy)
    span_x = max(wx, 1000.0 - wx)
    if span_y >= span_x:           # 垂直為深度長軸（窗在上/下端）
        dist, span = abs(tc[0] - wy), span_y
    else:                          # 水平為深度長軸（窗在左/右端）
        dist, span = abs(tc[1] - wx), span_x
    if span <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - dist / span)))


def _depth_classification(pct, hard_floor: int, soft_floor: int,
                          strict: bool, qual_wrong: bool) -> str:
    """深度分級 → 'ok' / 'soft' / 'hard'。
    - pct 無資料 → 'ok'（不誤判）。
    - pct >= soft_floor → 'ok'（達標）。
    - 容差 grace：strict=5、寬鬆=20。低於 hard_floor-grace 或（低於 hard_floor 且質性描述在錯區）→ 'hard'。
    - 其餘介於門檻間的小偏差 → 'soft'（照交付）。"""
    if not isinstance(pct, (int, float)):
        return "ok"
    if pct >= soft_floor:
        return "ok"
    grace = 5 if strict else 20
    cutoff = hard_floor - grace
    if pct < cutoff or (pct < hard_floor and qual_wrong):
        return "hard"
    return "soft"


# ─── 渲染結構驗證 ────────────────────────────────────────────────────
# 比對「該 render 對應的主照片」vs「渲染圖」，純評估、不重跑、不過濾
def _enforce_sofa_focal_orientation(
    result: dict,
    has_layout_ctx: bool,
    is_long_room_layout: bool = False,
) -> dict:
    """Enforce sofa orientation and long-room circulation flags."""
    result.setdefault("sofa_back_against_window", False)
    result.setdefault("sofa_focal_face_each_other", None)
    result.setdefault("sofa_against_side_wall", None)
    result.setdefault("sofa_intrudes_walkway", None)
    result.setdefault("coffee_table_in_walkway", None)
    if not has_layout_ctx:
        return result

    forced_reasons = []
    if result.get("sofa_back_against_window") is True:
        result["sofa_outside_living_zone"] = True
        forced_reasons.append("沙發背靠窗牆，靠窗只代表深度位置，不代表可以堵在窗前")
    if result.get("sofa_focal_face_each_other") is False:
        result["focal_anchor_misaligned_with_sofa"] = True
        forced_reasons.append("沙發與電視櫃／焦點家具沒有正面相對")
    if is_long_room_layout:
        if result.get("sofa_against_side_wall") is not True:
            result["sofa_outside_living_zone"] = True
            forced_reasons.append("長條房沙發未貼左／右長側牆")
        if result.get("sofa_intrudes_walkway") is not False:
            result["furniture_blocks_walkway"] = True
            forced_reasons.append("沙發侵入入口至房門／窗側的連續走道")
        if result.get("coffee_table_in_walkway") is not False:
            result["furniture_blocks_walkway"] = True
            forced_reasons.append("茶几或地毯侵入主走道")

    if forced_reasons:
        result["ok"] = False
        prev_reason = (result.get("reason") or "").strip()
        tag = "[orientation code-gate] " + "；".join(forced_reasons)
        result["reason"] = (prev_reason + " | " + tag) if prev_reason else tag
    return result


def validate_render(
    original_path: str,
    render_path: str,
    region_name: str = "",
    layout_context: dict | None = None,
) -> dict:
    """
    送 2 張本機圖片給 Gemini，回傳結構保留 + 家具動線評估 JSON：
      {ok, kitchen_added, recessed_space_added, windows_changed,
       walls_changed, ceiling_changed, floor_changed,
       furniture_blocks_walkway, sofa_faces_walkway,
       sofa_outside_living_zone, reason}

    家具動線拆成兩個獨立判斷：
      furniture_blocks_walkway = 家具「物理擋住」主動線（人需繞行）
      sofa_faces_walkway = 沙發「正面朝向」走廊/門口/通道開口（人坐下對著動線）

    layout_context（選用，user_confirmed_v2 時建議傳入）：
      {
        "layout_choice":    "A" / "B",
        "living_where":     "深處靠窗區域...",
        "sofa_wall_rule":   "建議右側為電視主牆...",   # optional
        "walkway":          "前段、左側...",            # optional
        "no_large_furniture_zones": [...],              # optional
        "target_location_hint": "rear_near_window",     # optional
        "target_note":      "客廳靠窗"                  # optional
      }
      傳入時會額外做 Q3：sofa 是否明顯違反 confirmed living zone。
      不傳則 sofa_outside_living_zone 一律回 false（避免無依據誤判）。
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or
               os.environ.get("GOOGLE_AI_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_AI_KEY 未設定")
    client = genai.Client(api_key=api_key)

    def _read_part(path: str):
        ext = os.path.splitext(path)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        with open(path, "rb") as f:
            return types.Part.from_bytes(data=f.read(), mime_type=mime)

    original_part = _read_part(original_path)
    render_part   = _read_part(render_path)

    region_hint = f"目標房型/角度：【{region_name}】\n" if region_name else ""

    # ── 組裝 confirmed-layout 區段（只在 user_confirmed_v2 有資料時觸發 Q3）──
    has_layout_ctx = isinstance(layout_context, dict) and bool(layout_context.get("living_where"))
    room_shape = str(layout_context.get("room_shape") or "") if has_layout_ctx else ""
    room_shape_lower = room_shape.lower()
    is_long_room_layout = any(
        k in room_shape_lower
        for k in ("長條", "狹長", "長型", "long rectangular", "elongated", "long room")
    )
    if has_layout_ctx:
        choice  = layout_context.get("layout_choice", "A")
        living  = layout_context.get("living_where", "")
        sofa_rule = layout_context.get("sofa_wall_rule", "")
        walkway = layout_context.get("walkway", "")
        no_large_zones = layout_context.get("no_large_furniture_zones") or []
        if isinstance(no_large_zones, list):
            no_large_zones_text = "；".join(str(x) for x in no_large_zones if x)
        else:
            no_large_zones_text = str(no_large_zones or "")
        target_hint = layout_context.get("target_location_hint", "")
        target_note = layout_context.get("target_note", "")
        # 沙發左右邊 ground truth（zoning 階段決定）；驗收用它判「錯邊」
        expected_sofa_side = str(layout_context.get("sofa_side") or "").strip().lower()
        if expected_sofa_side not in ("left", "right"):
            expected_sofa_side = ""
        _side_zh = {"left": "畫面左側長牆", "right": "畫面右側長牆"}
        sofa_side_line = (
            f"- 沙發應靠的邊（binding，以主視角觀看者視角）：「{_side_zh[expected_sofa_side]}」"
            f"，電視/焦點牆在對側。\n"
            if expected_sofa_side else ""
        )
        layout_block = (
            f"【已確認 layout 資訊（客戶在分區確認頁勾選的方案 {choice}）】\n"
            + (f"- 房間形狀：「{room_shape}」\n" if room_shape else "")
            + f"- 客廳區位置（binding）：「{living}」\n"
            + (f"- 沙發應靠的牆：「{sofa_rule}」\n" if sofa_rule else "")
            + sofa_side_line
            + (f"- 主走道位置：「{walkway}」\n" if walkway else "")
            + (f"- 禁放大型家具區：「{no_large_zones_text}」\n" if no_large_zones_text else "")
            + (f"- 使用者照片補充說明：「{target_note}」\n" if target_note else "")
            + (f"- 結構化位置 hint：「{target_hint}」\n" if target_hint else "")
            + (
                "- 驗收規則：若補充說明或位置 hint 指向靠窗/後段/深處，沙發、茶几、地毯、"
                "focal anchor 必須整組靠近窗側 living zone；跑到中段或壓到走道就是 fail。\n"
            )
        )
        # C2.1：JSON 多 3 個 debug 欄位（即使 sofa_outside_living_zone=false 也要填）
        # C2.2：再加 3 個 focal_anchor 相關欄位
        # C2.4：再加 2 個 depth_percent 數字（讓我們 code 端做 threshold，不純信 Gemini）
        layout_q_field = (
            '  "sofa_outside_living_zone": bool,\n'
            '  "sofa_zone_assessment": "簡短中文描述沙發實際所在位置（必填）",\n'
            '  "sofa_depth_position": "front / middle / back（必填）",\n'
            '  "sofa_depth_percent_estimate": 0-100 整數,\n'
            '  "confirmed_living_zone_reference": "從確認文字推導出的 living zone 預期深度位置（必填）",\n'
            '  "focal_anchor_misaligned_with_sofa": bool,\n'
            '  "focal_anchor_depth_position": "front / middle / back / not_present（必填）",\n'
            '  "focal_anchor_depth_percent_estimate": 0-100 整數（not_present 時填 -1）,\n'
            '  "focal_anchor_assessment": "簡短中文描述 focal_anchor 位置（必填）",\n'
            '  "sofa_back_against_window": bool,\n'
            '  "sofa_focal_face_each_other": bool,\n'
            '  "sofa_against_side_wall": bool,\n'
            '  "sofa_side_detected": "left / right / unclear（沙發椅背實際貼畫面哪一側長牆，必填）",\n'
            '  "sofa_intrudes_walkway": bool,\n'
            '  "coffee_table_in_walkway": bool,\n'
        )
    else:
        layout_block = ""
        layout_q_field = ""

    sofa_side_validation_block = ""
    if has_layout_ctx and expected_sofa_side:
        _exp_zh = "畫面左側" if expected_sofa_side == "left" else "畫面右側"
        _opp_zh = "畫面右側" if expected_sofa_side == "left" else "畫面左側"
        sofa_side_validation_block = f"""
Q2c: 沙發左右邊（本案 layout 已指定，必須驗收）
- 以「渲染圖觀看者視角」判斷沙發椅背實際貼在畫面左側長牆還是右側長牆，填入 sofa_side_detected
  （left / right；若沙發橫放、背窗、浮在中央無法判斷則填 unclear）。
- 本案已指定沙發應靠【{_exp_zh}】長牆，電視/焦點牆在【{_opp_zh}】。
- 只在 sofa_side_detected 明確等於指定側時才算正確；若沙發貼到對側（{_opp_zh}）即為錯邊，ok 必須為 false。
"""

    long_room_validation_block = ""
    if is_long_room_layout:
        long_room_validation_block = """
Q2b: 長條客廳側牆與縱向走道（本案為長條／狹長格局，必須驗收）
- sofa_against_side_wall = true 只在沙發椅背完整貼齊並平行於左側或右側長牆時成立。
  長牆是從入口端延伸至窗端的牆，不是最深處的窗牆。沙發橫放、背窗、浮在中間都填 false。
- sofa_intrudes_walkway = true：沙發主體、扶手或 chaise 侵入入口通往房門／窗側的連續路徑，
  使人必須繞行，或目測淨寬小於約 80 cm。
- coffee_table_in_walkway = true：茶几或地毯位於房間縱向通道中央，而不是緊湊地位於沙發與對向
  電視櫃／焦點家具之間；只要行走需繞過茶几就填 true。
- 長條客廳必須同時滿足：沙發貼一側長牆、焦點家具在對側長牆、兩者正面相對、入口到各開口
  保留約 80-90 cm 連續通道。任一不符，ok 必須為 false。
"""

    prompt = f"""
你會看到 2 張圖：
  image_1 = 原始空間照片
  image_2 = AI 渲染圖（聲稱是同一空間 + 家具+軟裝風格化）

{region_hint}{layout_block}判斷渲染圖有沒有破壞原始結構，且家具擺位是否合理可行走。回傳嚴格 JSON：
{{
  "ok": true/false,
  "kitchen_added": bool,
  "recessed_space_added": bool,
  "windows_changed": bool,
  "walls_changed": bool,
  "ceiling_changed": bool,
  "floor_changed": bool,
  "furniture_blocks_walkway": bool,
  "sofa_faces_walkway": bool,
  "render_bboxes": {{
    "sofa":         [ymin, xmin, ymax, xmax] 或 null,
    "main_window":  [ymin, xmin, ymax, xmax] 或 null,
    "focal_anchor": [ymin, xmin, ymax, xmax] 或 null
  }},
{layout_q_field}  "reason": "簡短中文 60 字內描述主要問題（ok=true 時填 '結構與動線皆合理'）"
}}

【render_bboxes（客觀定位用，務必精準）】
- 在「渲染圖 image_2」上，用 normalized 0–1000 的 [ymin, xmin, ymax, xmax] 標出：
  sofa（主沙發整體）、main_window（畫面中主要的窗戶/落地窗）、focal_anchor（電視櫃/視聽櫃/主視覺焦點家具）。
- 這是給後端「直接量測沙發離窗多近」用的客觀座標，請依實際畫面標，不要憑空想像；看不到的物件填 null。

【結構保留判定】
- kitchen_added：原圖沒廚房元素，渲染圖出現廚房櫥櫃/水槽/料理台/餐桌/瓦斯爐
- recessed_space_added：渲染圖出現原圖沒的凹間/額外房間/隔斷/廊道
- windows_changed：窗戶數量/位置/形狀差異 > 20%
- walls_changed：牆面明顯新增材質（大理石板/木皮/線板/造型牆）—— 純油漆顏色不同**不算**
- ceiling_changed：天花板明顯新增結構（嵌燈陣列/木作/降板/間接照明溝槽）—— 吊燈或單一燈具**不算**結構
- floor_changed：**裸露地板**材質/方向/比例顯著被改。**新增地毯不算 floor_changed**

【家具動線判定 — 拆成兩個獨立問題，分開判斷】

Q1: furniture_blocks_walkway（家具「物理擋住」主動線）
- furniture_blocks_walkway = true 當任一成立：
  * 沙發、茶几、地毯或大型家具明顯擋住「左/右側走廊開口」或「主動線」，人需繞行/跨越才能通過
  * 沙發「浮」在房間中間（不靠牆），且本體位於走道路徑上
  * 茶几或地毯的一部分延伸到走道區，導致通道寬度看起來 < 60cm
  * 從入口走到窗邊或從入口走到房門開口，路徑被家具吃掉
  * L 型沙發的轉角或側邊伸入走廊開口前的淨空區
- furniture_blocks_walkway = false 當動線實際可行（沒有家具擋路）

Q2: sofa_faces_walkway（沙發「正面朝向」走道 — 即使沒擋）
- sofa_faces_walkway = true 當以下成立（**獨立於 Q1 判斷**）：
  * 沙發的「正面」（坐下時眼睛看的方向）直接對著走廊開口、側牆房門、或主走道
    （坐下時眼前就是有人走來走去的動線，心理不適）
  * 沙發「背」靠的方向也對 — 看「正面朝向」哪個 vector：
    - 沙發正面朝 TV 牆/窗/室內焦點 → false
    - 沙發正面朝 走廊開口/房門/主走道 → true
  * 注意：沙發**沒擋路**但「面對走道」依然 true（這是兩個獨立檢查）
- sofa_faces_walkway = false 當沙發正面朝向：
  * 對面 TV 牆（純實牆，沒開口）
  * 窗景方向
  * 室內主視覺焦點（壁畫/景觀/裝飾牆）
{long_room_validation_block}{sofa_side_validation_block}
""" + ("""
Q3: sofa_outside_living_zone（沙發是否違反客戶已確認的 living zone）

【最高原則】
**Do not judge only whether the render looks reasonable.**
**Judge whether the sofa respects the customer-confirmed living zone.**

畫面再漂亮、構圖再合理、沙發朝向再 ergonomic — 只要沙發不在客戶確認
的「深度位置」上，就必須回 sofa_outside_living_zone = true。

【判定步驟（依序進行）】

Step 1 — 從確認文字推導 living zone 的「深度位置」：
  * 出現「靠窗 / 窗邊 / 底端 / 深處 / 後半段 / 後段 / 深端 / 底部 / 尾端 /
        window / back of the room / back end / deep end / far end / rear」
    → confirmed_living_zone_reference = "靠窗端 / 房間深處 (back)"
  * 出現「前段 / 入口側 / 近門 / 前半段 / front / entrance / near the door」
    → confirmed_living_zone_reference = "前段 / 入口側 (front)"
  * 出現「中段 / 中間 / middle / center / center of the room」
    → confirmed_living_zone_reference = "中段 (middle)"

Step 2 — 觀察渲染圖內沙發的「實際深度位置」：
  * 沙發在房間長軸上的哪一段？前段 / 中段 / 後段？
  * 距離窗戶遠不遠？緊鄰窗 vs 隔一段距離 vs 在房間另一頭？
  * 沙發背靠的是 confirmed zone 那側的牆嗎？還是不同側的牆？
  * 填入 sofa_depth_position = "front" / "middle" / "back" （三選一）
  * 填入 sofa_zone_assessment 簡短描述，例：
      「沙發放在房間前中段，距離靠窗 living zone 約一個沙發長度」
      「沙發緊鄰底端窗台，落在確認的後段 living zone 內」
      「沙發背靠左前段牆面，方向朝右後方窗戶」

Step 2b — 估深度百分比 (C2.4)：
  * 把渲染圖的深度軸視為 0% 到 100%：
      0%   = 畫面最前（近相機 / 房間入口端）
      100% = 畫面最深（遠相機 / 通常是窗邊）
  * 估算沙發**主體中心**在這條軸上的百分比
  * 填入 sofa_depth_percent_estimate（0–100 整數）
  * 估算原則：
      sofa 緊鄰窗台 → 80-95
      sofa 在房間後 1/3 → 65-80
      sofa 在房間中段 → 40-60
      sofa 在房間前 1/3 → 15-35
      sofa 緊鄰入口 → 5-15

Step 3 — 判定 sofa_outside_living_zone：

  sofa_outside_living_zone = true 當：
  * confirmed = 靠窗端 (back) 但 sofa_depth_position 在 front 或 middle
    → 確認靠窗後段，沙發卻在前/中段
  * confirmed = 靠窗端 (back) 但沙發看起來明顯沒緊鄰窗或房間深處
  * confirmed = 前段 (front) 但 sofa_depth_position 在 back
  * confirmed = 中段 (middle) 但 sofa_depth_position 在 front 或 back
  * 沙發主要落入 dining zone / entrance zone / walkway 內
  * 沙發背靠的牆明顯不是 confirmed zone 那一側

  sofa_outside_living_zone = false 只在以下情況：
  * sofa_depth_position 跟 confirmed_living_zone_reference 在同一段
    （back vs back / front vs front / middle vs middle）
  * 沙發本體明顯落在 confirmed living zone 區域內

【嚴格度提醒】
之前的版本對「small drift」會放過 → 漏判率高。本次升級：
* 即使「沙發大致在 living zone 邊緣」、「靠近但沒在裡面」、「在 zone 跟
  相鄰區的交界」→ 都應視為 outside（true）
* 「sofa-length 等級的偏移」就算明顯違反，不再算 small drift
* 沙發背靠錯側牆 → 即使位置在 zone 內也是 outside（朝向不對等於 zone 不對）

【reason 寫法（若 sofa_outside_living_zone=true）】
reason 必須明確寫出哪裡不對，例：
* 「沙發未在客戶確認的靠窗後半段客廳區，而被擺放在前中段」
* 「沙發偏前，未靠近窗邊 living zone」
* 「沙發在前段，confirmed 應在後段靠窗」
* 「沙發位於入口側 / 餐廳區 / 主動線旁，違反確認分區」
* 「sofa is in the front half, not near the window-side confirmed zone」

────────────────────────────────────────────────────────────────────────

Q4: focal_anchor_misaligned_with_sofa（焦點家具 / 主牆家具是否跟沙發對齊形成同一組客廳）

【背景】
focal_anchor = 主牆焦點家具 = TV cabinet / media console / sideboard /
low cabinet / display cabinet / 窄牆櫃 + framed art / storage bench 等
**實體家具**。focal_anchor 應該放在沙發對面或視覺對位的牆，跟 sofa
形成「同一個緊湊的客廳組合」。Q3 只看沙發單點，Q4 看 focal_anchor
跟 sofa 的對位關係 — 兩個獨立判斷。

【判定步驟】

Step 1 — 在 render 圖內辨識 focal_anchor：
* 找尋電視櫃 / 矮櫃 / 邊櫃 / 媒體櫃 / 展示櫃 / 窄牆櫃 / 收納長凳等實體家具
* 注意：「壁掛畫」**單獨存在不算 focal_anchor**，必須是有量體的家具
* 浮層 / 漂浮架子也不算 focal_anchor（必須是著地或牆掛的真實家具）
* 如果完全沒辨識到 focal_anchor → focal_anchor_depth_position = "not_present"
  且 focal_anchor_misaligned_with_sofa = true（自動判 misaligned）

Step 2 — 判斷 focal_anchor 深度位置：
* 跟 sofa 一樣判 front / middle / back
* 填入 focal_anchor_depth_position
* 填入 focal_anchor_assessment 中文描述（必填），例：
    「TV 櫃在房間中段靠左牆，距離沙發約一個房間長度」
    「media console 緊鄰窗側左牆，跟沙發在同一段深度形成對位」
    「主牆家具缺席，僅有壁掛畫」

Step 2b — 估 focal_anchor 深度百分比 (C2.4)：
* 同沙發的深度軸 0%（近相機）→ 100%（遠相機 / 通常是窗邊）
* 估算 focal_anchor 主體中心在這條軸上的百分比
* 填入 focal_anchor_depth_percent_estimate（0–100 整數）
* 若 focal_anchor 不存在 (not_present)：filled with -1
* 估算原則同 sofa

Step 3 — 判定 focal_anchor_misaligned_with_sofa：

focal_anchor_misaligned_with_sofa = true 當以下任一成立：
* focal_anchor 不存在 / 只有壁畫沒有實體家具
* focal_anchor 跟 sofa 的深度位置跨越過大：
    - sofa 在 back，focal_anchor 在 front → true（明顯跨房間）
    - sofa 在 back，focal_anchor 在 middle 且明顯偏前 → true
    - sofa 在 middle，focal_anchor 在 back 或 front → true
* focal_anchor 主體落入 dining zone / entrance zone / 主要動線內
* focal_anchor 跟 sofa 距離過遠，整組客廳被拉長/拉散，無緊湊感
* focal_anchor 沒有位於 sofa 正前方的對向牆面
* focal_anchor 只在 sofa 的相鄰／垂直側牆，兩者並未正面相對
* sofa 在右側靠窗、focal_anchor 卻被擺到左前段入口附近 → true

focal_anchor_misaligned_with_sofa = false 只在以下情況：
* sofa 在 confirmed living zone
* focal_anchor 也在同一個 confirmed living zone，並且位於 sofa 正前方的對向牆面
* sofa、coffee table、rug、focal_anchor 看起來形成一組緊湊客廳配置
* sofa 的正面朝向 focal_anchor，中間是茶几／地毯

【方向必填欄位】
* sofa_back_against_window = true：沙發椅背直接擋在主窗前／背靠窗牆。
  「客廳靠窗」只是深度分區，不是要沙發背靠窗。
* sofa_focal_face_each_other = true 只能在沙發正面朝向電視櫃／focal anchor 時填 true。
* 「同樣在 back 深度」、「距離很近」、「位於垂直側牆」都不代表面對面，必須填 false。

【嚴格度提醒】
* 跟 Q3 同樣原則：**不要因為「畫面構圖合理」就 false**
* focal_anchor 距 sofa「半個房間長度」就視為 misaligned
* focal_anchor 在前段／入口側／動線旁就視為 misaligned
* 即使 sofa 自己正確 (Q3=false)，focal_anchor 跑掉 (Q4=true) 也是大問題

【reason 寫法（若 focal_anchor_misaligned_with_sofa=true）】
reason 必須明確寫出，例：
* 「主牆家具未對齊沙發，TV 櫃位於前段而沙發在後段」
* 「電視櫃位於入口側 / 餐廳區，距離沙發過遠」
* 「焦點家具不存在，只有壁畫沒有實體家具」
* 「客廳組合被拉散，sofa 在後段右側、TV 櫃在前段左側」
* 「main wall furniture is misaligned with sofa — TV cabinet in front zone」
""" if has_layout_ctx else "") + """
【reason 寫法】
- 若 furniture_blocks_walkway=true → reason 寫「家具擋走道」「沙發擋走廊開口」等
- 若 sofa_faces_walkway=true → reason 必須**明確寫**「沙發朝向走廊」「沙發正面對著房門」「沙發面對走道」
  （不要用「結構與動線皆合理」這種模糊語句）
- 若 sofa_outside_living_zone=true → reason 必須**明確寫**「沙發未在確認 living zone」
  「沙發違反確認分區」或對等說明
- 若 focal_anchor_misaligned_with_sofa=true → reason 必須**明確寫**「主牆家具未對齊沙發」
  「電視櫃位於前段／入口側」「焦點家具不存在」「客廳組合被拉散」或對等說明
- 若 sofa_back_against_window=true → reason 必須寫「沙發背靠窗牆」
- 若 sofa_focal_face_each_other=false → reason 必須寫「沙發與電視櫃未正面相對」
- 若 sofa_against_side_wall=false → reason 必須寫「長條房沙發未貼左右長牆」
- 若 sofa_intrudes_walkway=true → reason 必須寫「沙發侵入主走道」
- 若 coffee_table_in_walkway=true → reason 必須寫「茶几或地毯侵入主走道」
- 上述都 false 且結構保留 → reason = 「結構與動線皆合理」

ok = 所有違規 flag 全為 false，且有 layout_context 時 sofa_focal_face_each_other 必須為 true。
reason 必須具體（例「L 沙發擋住左側通往臥室的走廊開口」非「動線不合理」）。
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=[original_part, render_part, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    result = json.loads(response.text)
    # 確保新欄位永遠存在（沒 layout_context 時預設 False；不誤判）
    result.setdefault("sofa_outside_living_zone", False)
    # C2.1 debug 欄位（即使 false 也要有；沒 layout_context 時為空字串）
    result.setdefault("sofa_zone_assessment", "")
    result.setdefault("sofa_depth_position", "")
    result.setdefault("confirmed_living_zone_reference", "")
    # C2.2 focal_anchor 欄位（向下相容）
    result.setdefault("focal_anchor_misaligned_with_sofa", False)
    result.setdefault("focal_anchor_depth_position", "")
    result.setdefault("focal_anchor_assessment", "")
    result = _enforce_sofa_focal_orientation(
        result,
        has_layout_ctx,
        is_long_room_layout=is_long_room_layout,
    )
    # 沙發左右邊 ground truth 驗收（2026-06-21）：layout 指定了 sofa_side 時，
    # 沙發貼到對側即 sofa_on_wrong_side=true（high severity → 觸發帶原因的重試）。
    result.setdefault("sofa_side_detected", "unclear")
    result.setdefault("sofa_on_wrong_side", False)
    if has_layout_ctx and expected_sofa_side in ("left", "right"):
        detected = str(result.get("sofa_side_detected") or "").strip().lower()
        if detected in ("left", "right") and detected != expected_sofa_side:
            result["sofa_on_wrong_side"] = True
            result["ok"] = False
            _opp_zh = "左側" if expected_sofa_side == "right" else "右側"
            tag = f"[side code-gate] 沙發貼錯邊：應靠{'左' if expected_sofa_side=='left' else '右'}側長牆，實際貼{_opp_zh}"
            prev = (result.get("reason") or "").strip()
            result["reason"] = (prev + " | " + tag) if prev else tag
    # C2.4 depth_percent 欄位
    result.setdefault("sofa_depth_percent_estimate", None)
    result.setdefault("focal_anchor_depth_percent_estimate", None)

    # ── 客觀深度修正 (2026-06-23)：用渲染圖上偵測到的 sofa/window bbox 算「離窗多近」，
    # 取代 Gemini 自述的深度百分比（它常把中段沙發誤報成靠窗，hard_fail 因此漏判）。
    # 取較保守（較小）值：grounded 量到沙發偏前時，不讓 Gemini 自述偏高蓋掉真實位置。
    rb = result.get("render_bboxes") or {}
    if isinstance(rb, dict):
        g_sofa = _grounded_depth_pct(rb.get("sofa"), rb.get("main_window"))
        if g_sofa is not None:
            result["sofa_depth_grounded_pct"] = round(g_sofa, 1)
            sp = result.get("sofa_depth_percent_estimate")
            result["sofa_depth_percent_estimate"] = (
                min(sp, g_sofa) if isinstance(sp, (int, float)) else g_sofa
            )
        g_focal = _grounded_depth_pct(rb.get("focal_anchor"), rb.get("main_window"))
        if g_focal is not None:
            result["focal_anchor_depth_grounded_pct"] = round(g_focal, 1)
            ap = result.get("focal_anchor_depth_percent_estimate")
            if isinstance(ap, (int, float)) and ap >= 0:
                result["focal_anchor_depth_percent_estimate"] = min(ap, g_focal)
            elif not isinstance(ap, (int, float)) or ap < 0:
                # ap 為 None/not_present(-1) 時不覆寫（避免把 not_present 變成有值）
                pass

    # ── C2.4: code 端 threshold enforce ─────────────────────────────────
    # 只在 user_confirmed_v2 + 靠窗 layout 時 enforce，避免影響其他路徑
    if has_layout_ctx:
        living_w = layout_context.get("living_where", "") if isinstance(layout_context, dict) else ""
        target_hint = layout_context.get("target_location_hint", "") if isinstance(layout_context, dict) else ""
        target_note = layout_context.get("target_note", "") if isinstance(layout_context, dict) else ""
        no_large_zones_for_threshold = layout_context.get("no_large_furniture_zones", []) if isinstance(layout_context, dict) else []
        if isinstance(no_large_zones_for_threshold, list):
            no_large_text_for_threshold = " ".join(str(x) for x in no_large_zones_for_threshold)
        else:
            no_large_text_for_threshold = str(no_large_zones_for_threshold or "")
        window_signal = f"{living_w} {target_hint} {target_note}"
        dining_middle_signal = f"{target_note} {living_w} {no_large_text_for_threshold}"
        ws_kws = ["靠窗", "窗邊", "窗戶", "底端", "深處", "後半段", "後段",
                  "深端", "底部", "底側", "尾端", "末端",
                  "window", "back of the room", "back end", "deep end", "far end", "rear"]
        dining_middle_kws = ["中段", "中間", "中央", "middle", "center", "centre"]
        has_dining_middle_constraint = (
            isinstance(dining_middle_signal, str)
            and ("餐廳" in dining_middle_signal or "dining" in dining_middle_signal.lower())
            and any(k in dining_middle_signal for k in dining_middle_kws)
        )
        is_window_side = (
            target_hint == "rear_near_window"
            or (isinstance(window_signal, str) and any(k in window_signal for k in ws_kws))
        )

        if is_window_side:
            sofa_pct = result.get("sofa_depth_percent_estimate")
            anchor_pct = result.get("focal_anchor_depth_percent_estimate")
            assessment_text = (result.get("sofa_zone_assessment") or "") + " " + (result.get("reason") or "")
            anchor_text     = (result.get("focal_anchor_assessment") or "")
            sofa_hard_floor = 75 if has_dining_middle_constraint else 60
            sofa_soft_floor = 80 if has_dining_middle_constraint else 65
            anchor_hard_floor = 60 if has_dining_middle_constraint else 45
            anchor_soft_floor = 65 if has_dining_middle_constraint else 50

            soft_kw_sofa = ["中段", "偏前", "離窗遠", "前段", "前中段", "前半段",
                            "middle", "front", "away from the window"]
            soft_kw_anchor = ["入口", "餐廳", "中段", "離 sofa 過遠", "離沙發過遠",
                              "前段", "前中段", "前半段",
                              "entrance", "dining", "transition", "middle", "front"]

            # 硬傷 vs 軟傷分級 (2026-06-21, 收緊 2026-06-21b)：
            #   - 容差內的小偏差 → 軟傷，照交付，不擋圖、不重生。
            #   - 超出容差 / Gemini 質性描述在錯區 → 硬傷（跑錯分區）。
            #   - 焦點 not_present → 硬傷（完全沒對向）。
            # 容差大小看「使用者有沒有明確指定位置」：
            #   - 有明確指定（補充說明含位置語意 / 靠窗 hint / 餐廳在中）→ 容差只給 5 點，
            #     低於硬門檻就算錯區（避免「明明標了靠窗、沙發卻跑到中間」被當小事放過）。
            #   - 無明確指定（含非位置補充如「喜歡淺木色」）→ 容差 20 點，較寬鬆。
            strict_depth = _compute_strict_depth(
                target_note, target_hint, has_dining_middle_constraint
            )
            forced_solz = False        # 硬傷：沙發跑錯分區
            forced_focal = False       # 硬傷：焦點完全沒對向 / 嚴重錯位
            soft_notes = []            # 軟傷：可交付，只記錄

            sofa_cls = _depth_classification(
                sofa_pct, sofa_hard_floor, sofa_soft_floor, strict_depth,
                any(kw in assessment_text for kw in soft_kw_sofa),
            )
            if sofa_cls == "hard":
                forced_solz = True
            elif sofa_cls == "soft":
                soft_notes.append(
                    f"sofa 深度估 {int(sofa_pct)}% 略低於目標 {sofa_soft_floor}%（軟傷，照交付）"
                )

            anchor_not_present = (
                result.get("focal_anchor_depth_position") == "not_present"
                or (isinstance(anchor_pct, (int, float)) and anchor_pct < 0)
            )
            if anchor_not_present:
                forced_focal = True
            else:
                anchor_cls = _depth_classification(
                    anchor_pct, anchor_hard_floor, anchor_soft_floor, strict_depth,
                    any(kw in anchor_text for kw in soft_kw_anchor),
                )
                if anchor_cls == "hard":
                    forced_focal = True
                elif anchor_cls == "soft":
                    soft_notes.append(
                        f"focal_anchor 深度估 {int(anchor_pct)}% 略低於目標 {anchor_soft_floor}%（軟傷，照交付）"
                    )

            if soft_notes:
                existing_soft = result.get("soft_issues")
                result["soft_issues"] = (existing_soft if isinstance(existing_soft, list) else []) + soft_notes

            if forced_solz or forced_focal:
                # 硬傷才覆寫 flag + ok=false（觸發重生 / 擋交付）
                forced_reasons = []
                if forced_solz:
                    result["sofa_outside_living_zone"] = True
                    sp = int(sofa_pct) if isinstance(sofa_pct, (int, float)) else "?"
                    forced_reasons.append(
                        f"sofa 深度估 {sp}% 嚴重偏離或質性描述在錯區，視為未在靠窗 living zone"
                    )
                if forced_focal:
                    result["focal_anchor_misaligned_with_sofa"] = True
                    ap = int(anchor_pct) if isinstance(anchor_pct, (int, float)) else "?"
                    forced_reasons.append(
                        f"focal_anchor 深度估 {ap}% 嚴重偏離或 not_present，與 sofa 未對齊"
                    )
                result["ok"] = False
                prev_reason = (result.get("reason") or "").strip()
                tag = "[C2.4 code-threshold] " + "; ".join(forced_reasons)
                result["reason"] = (prev_reason + " | " + tag) if prev_reason else tag

    # ── 硬傷彙整 (2026-06-21)：delivery / retry 共用的單一判準 ──
    result.setdefault("soft_issues", [])
    result["hard_fail"] = any(result.get(f) for f in HARD_FAIL_FLAGS)
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("使用方式: python gemini_analyze.py <影片路徑> [風格1,風格2,...]")
        print("範例: python gemini_analyze.py video.mp4 japanese,nordic")
        sys.exit(1)

    styles_arg = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    result = analyze_space(sys.argv[1], user_styles=styles_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))

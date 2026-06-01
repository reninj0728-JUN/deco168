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


def analyze_space(
    video_path: str,
    user_styles: list[str] | None = None,
    is_uri: bool = False,
    extra_photos: list[str] | None = None,
) -> dict:
    """
    分析空間影片並生成 Flux prompts。
    video_path: 本機路徑 或 Gemini Files URI（is_uri=True 時直接使用）
    extra_photos: 用戶另外上傳的照片清單，會一起送 Gemini 幫助理解全室
    """
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_AI_KEY 未設定，請在 Railway Variables 設定")
    client = genai.Client(api_key=api_key.strip())

    if user_styles and all(s in VALID_STYLES for s in user_styles):
        fixed_styles = user_styles[:3]
        mode = "user_selected"
        style_instruction = f"""
用戶已選擇以下風格（必須包含）：{', '.join(fixed_styles)}
{"如果不足 3 個，AI 自行從剩餘風格中補齊至 3 個，選最適合此空間的。" if len(fixed_styles) < 3 else ""}
"""
    else:
        fixed_styles = []
        mode = "ai_recommended"
        style_instruction = """
用戶未指定風格，請根據空間的採光、坪數、格局、屋主需求，
從以下 9 種風格中推薦最適合的 3 種：
modern / japanese / luxury / nordic / muji / cream / wood / french / chinese-modern
"""

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

    prompt = f"""
分析這個空間（影片 + 照片）。

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
  "best_photo_index": {("這個欄位回傳 0~" + str(len(photo_parts)-1) + " 之間的整數，指出哪一張用戶照片最適合當「設計呈現的主角度」（最美、構圖最完整、最能看出空間感）。若全部都不好就填 0。") if photo_parts else "null"},
  "renders": [
    {{"style":"style_id","style_label":"中文名稱","flux_prompt":"逗號分隔 keyword，結尾必須是 professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts"}},
    {{"style":"style_id","style_label":"中文名稱","flux_prompt":"..."}},
    {{"style":"style_id","style_label":"中文名稱","flux_prompt":"..."}}
  ]
}}

flux_prompt 要根據空間實際採光、格局選詞。<15坪加 light reflective surface, open concept, visual expansion。
"""

    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("使用方式: python gemini_analyze.py <影片路徑> [風格1,風格2,...]")
        print("範例: python gemini_analyze.py video.mp4 japanese,nordic")
        sys.exit(1)

    styles_arg = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    result = analyze_space(sys.argv[1], user_styles=styles_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))

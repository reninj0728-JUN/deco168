"""
Step 1 — Gemini 3.1 Flash-Lite 分析影片
輸入：影片路徑 + 用戶選擇的風格（可選）
輸出：dict（空間描述 + 3 個 Flux prompt，風格由用戶指定或 AI 推薦）
"""
import os
import io
import re
import json
import time
from google import genai
from google.genai import types


def compute_spatial_fidelity(result: dict) -> tuple[bool, list]:
    """從 validate_render 結果算「空間保真」是否失守（純邏輯、可單測、不打 API）。
    三個「保留」欄位缺省 True、一個「入侵」欄位缺省 False；任一失守回 (True, [中文原因…])。
    2A520C25 根治：離譜重畫的圖 windows/walls_changed 全 false 仍能過，這關補上。"""
    problems = []
    if not result.get("camera_axis_preserved", True):
        problems.append("相機視角被換成另一個空間")
    if not result.get("main_window_region_match", True):
        problems.append("主窗方位被移動")
    if not result.get("passage_openings_preserved", True):
        problems.append("原有走道門洞消失")
    if result.get("offframe_room_invaded", False):
        problems.append("畫面外的廚房/房間被畫進畫面")
    return bool(problems), problems


def _repair_json_text(t: str) -> str:
    """修復 Gemini 常見的 JSON 斷裂（B0CDF6A0：Expecting ',' delimiter —
    reason 字串內未跳脫引號，重打一次同樣壞＝系統性格式問題，必須修復不能靠運氣）。
    處理三種：1) 字串內未跳脫的引號  2) 尾逗號  3) 截斷缺右括號。"""
    out: list = []
    in_str = False
    esc = False
    i, n = 0, len(t)
    while i < n:
        c = t[i]
        if esc:
            out.append(c); esc = False; i += 1; continue
        if c == "\\":
            out.append(c); esc = True; i += 1; continue
        if c == '"':
            if not in_str:
                in_str = True
                out.append(c)
            else:
                # 字串中遇到引號：下一個非空白字元是 , : } ] 或結尾才是真收尾，
                # 否則視為內文引號（「沙發"正面"朝向」）→ 轉義
                j = i + 1
                while j < n and t[j] in " \t\r\n":
                    j += 1
                if j >= n or t[j] in ',:}]':
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
            i += 1; continue
        out.append(c); i += 1
    s = "".join(out)
    if in_str:
        s += '"'          # 截斷在字串中間
    s = re.sub(r",\s*([}\]])", r"\1", s)   # 尾逗號
    miss_brk = s.count("[") - s.count("]")
    if miss_brk > 0:
        s += "]" * miss_brk
    miss_brace = s.count("{") - s.count("}")
    if miss_brace > 0:
        s += "}" * miss_brace
    return s


def _json_loads_lenient(text: str) -> dict:
    """解析 Gemini 回的 JSON，容忍常見格式瑕疵。
    20A8220A：尾端多文字/markdown fence → 去 fence + raw_decode。
    B0CDF6A0：字串中段斷裂（未跳脫引號）→ _repair_json_text 修復後再 parse。
    全部失敗才丟例外給呼叫端（呼叫端 fail-closed，不裸奔）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    i = t.find("{")
    if i >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(t[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # 修復器：字串內引號/尾逗號/截斷
        try:
            repaired = _repair_json_text(t[i:])
            obj, _end = json.JSONDecoder().raw_decode(repaired)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Gemini JSON 無法解析: {t[:120]!r}")


def _downscale_for_vision(data: bytes, orig_mime: str,
                          max_side: int = 1536, quality: int = 85) -> tuple[bytes, str]:
    """縮小「送進 Gemini」的圖片以省視覺 token（成本主要吃解析度，不是品質）。
    最長邊 > max_side 才縮；已夠小則原樣回傳。格局/結構/驗收判斷在 1536px 完全足夠，
    品質不受影響。任何失敗 → 回原圖。回傳 (bytes, mime)。"""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        # 手機直拍照片常帶 EXIF Orientation；resize/重新編碼若不先轉正，
        # 輸出會永久丟失方向資訊（PIL save() 預設不帶 exif）→ Gemini 看到橫躺的房間。
        # api.py 下載階段已轉正大部分照片，這裡是第二道防線（涵蓋其他呼叫路徑）。
        orientation = im.getexif().get(0x0112, 1)
        if orientation not in (1, None):
            im = ImageOps.exif_transpose(im)
        w, h = im.size
        if max(w, h) <= max_side and orientation in (1, None):
            return data, orig_mime          # 已經夠小且方向正常，不動（避免無謂重壓）
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"
    except Exception as _e:
        return data, orig_mime              # 失敗就用原圖，永不擋流程


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
    user_notes: str = "",
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

    # 用戶選 2~3 種風格（單一空間 2 種、全室 1+加購最多 3 種），不再 AI 自動推薦補齊
    if user_styles and all(s in VALID_STYLES for s in user_styles):
        fixed_styles = user_styles[:3]
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
        # 無上限 PROCESSING 等待會吊死整單（D50FC472）。此路徑只有純影片單會走
        # （影片是唯一素材、無照片可退），逾時就明確報錯，別讓單卡到容器重啟。
        _vid_t0 = time.time()
        while video_file.state.name == "PROCESSING":
            if time.time() - _vid_t0 > 180:
                raise RuntimeError("Gemini 影片處理逾時（卡在 PROCESSING 超過 180s），請重新上傳或改傳照片")
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
                    _d, _m = _downscale_for_vision(f.read(), mime)
                    photo_parts.append(types.Part.from_bytes(data=_d, mime_type=_m))
            except Exception as _e:
                print(f"[Gemini] 無法讀照片 {p}: {_e}")

    photos_note = (
        f"另外附 {len(photo_parts)} 張用戶上傳的照片（第 0~{len(photo_parts)-1} 張）。"
        f"影片用來理解整體格局，照片是用戶選的「想呈現的角度」。"
        if photo_parts else "本次只有影片，沒有額外照片。"
    )
    space_label = SPACE_TYPE_LABELS.get(space_type, "客廳")
    # 屋主備註（例如「中間是餐廳」）→ 強力影響 region 切分，讓 Gemini 照指定切出對應房間。
    _notes_clause = ""
    if (user_notes or "").strip():
        _notes_clause = (
            f"\n【屋主明確指定 — 最高優先，必須遵守】「{user_notes.strip()[:200]}」。"
            "請依此切分 regions：屋主說中間/某處是餐廳，就要切出獨立的『餐廳(dining)』region；"
            "說某處當書房，就切『書房(study)』。不要把它併進客廳。"
        )
    if space_type == "whole":
        scope_instruction = (
            "用戶選的是【全室】——識別空間裡幾個主要房間/區域"
            "（例如：客廳、餐廳、主臥、書房）。"
            "regions 陣列**必須恰好 3 個、且 3 個 room_type 全部不同**（優先 客廳/餐廳/主臥）；"
            "開放式客餐廳請拆成『客廳』+『餐廳』兩個不同 region，不要給兩個都標客廳。"
            + _notes_clause
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
  "owner_requests": "屋主需求：若影片有聲音，務必『聽』屋主口述（例如『這裡想做日式』『沙發不要靠這邊』『這面牆留白』『這間當書房』），逐條結構化寫下，並盡量標註對應房間或影片時間點；都沒提就填 '未提及'",
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
- 音訊：影片若有屋主說話，必須一併聆聽並把需求提取進 owner_requests；全室模式下，盡量把每個需求對應到 regions 裡的房間。這是理解屋主真實意圖的重要來源，不可忽略。
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

    result = _json_loads_lenient(response.text)
    result["_mode"] = mode  # 記錄是 AI 推薦還是用戶指定
    return result


# 硬傷 flag（2026-06-21）：交付/重生的單一判準。任一為 true → render 不可交付，需重生。
# 軟傷（深度小偏差、茶几略偏、軟裝不齊等）不在此列 → 照常交付。
# 分類依據見產品決策：結構破壞 / 動線阻塞 / 沙發錯邊 / 跑錯分區 / 背窗 / 完全沒對向 = 硬傷。
HARD_FAIL_FLAGS = (
    # 結構破壞
    "kitchen_added", "recessed_space_added", "windows_changed",
    "walls_changed", "ceiling_changed", "floor_changed",
    # 動線阻塞（封門 / 擋走道 / 家具擋門）
    "furniture_blocks_walkway", "furniture_blocks_door", "sofa_faces_walkway",
    # 空間保真（相機軸/窗位/走道門洞/畫面外房間入侵）— 2A520C25 根治
    "spatial_fidelity_fail",
    # 產品可見性（清單 must 商品圖上沒畫/畫成別件）— 50873CF0/B0CDF6A0 根治
    # env PRODUCT_VISIBILITY_GATE=0 時該 flag 恆 False，等於整層關閉
    "product_visibility_fail",
    # 沙發視線正對大門（1A3B0C68：門與電視同牆、體感對門）
    "sofa_facing_entrance_door",
    # 核心配置錯誤
    "sofa_on_wrong_side",                 # 沙發放錯確認側
    "sofa_outside_living_zone",           # 客廳跑錯分區
    "sofa_back_against_window",           # 沙發背窗
    "focal_anchor_misaligned_with_sofa",  # 沙發與電視完全沒對向 / not_present
    # 產品一致（護城河：清單座位數 ≠ 圖上座位數）
    "product_sofa_seating_mismatch",
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


def _door_adjacency_violation(rb: dict) -> tuple | None:
    """BB034AB8 根治：門邊淨空幾何判定（0-1000 normalized bbox）。

    客廳的 focal_anchor / sofa 框與 entrance_door 框「重疊、或水平間距
    < 0.25 門寬且垂直有交集」→ 回 (物件名, 間距, 門寬)；否則 None。
    門檻取保守 0.25：側牆透視壓縮會讓深處櫃子在影像上顯得離門近
    （21CCB9AF 合格圖間距約 0.3-0.5 門寬），不能誤殺；真貼門/重疊一定抓到。"""
    if not isinstance(rb, dict):
        return None

    def _valid(b):
        return (isinstance(b, (list, tuple)) and len(b) == 4
                and all(isinstance(x, (int, float)) for x in b)
                and b[2] > b[0] and b[3] > b[1])

    door = rb.get("entrance_door")
    if not _valid(door):
        return None
    door_w = door[3] - door[1]
    for nm in ("focal_anchor", "sofa"):
        b = rb.get(nm)
        if not _valid(b):
            continue
        x_gap = max(door[1] - b[3], b[1] - door[3], 0)
        y_overlap = min(door[2], b[2]) - max(door[0], b[0])
        if x_gap < 0.25 * door_w and y_overlap > 0:
            return (nm, x_gap, door_w)
    return None


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
                          strict: bool, qual_wrong: bool,
                          no_soft: bool = False) -> str:
    """深度分級 → 'ok' / 'soft' / 'hard'。
    - pct 無資料 → 'ok'（不誤判）。
    - pct >= soft_floor → 'ok'（達標）。
    - no_soft=True（明確分區：客廳靠窗+餐廳中段）：未達 soft_floor 一律 'hard'，沒有軟傷帶
      → 73–77% 這種「還是偏中段」的圖直接擋掉重生，不再軟交。
    - 否則 容差 grace：strict=5、寬鬆=20；低於 hard_floor-grace 或（低於 hard_floor 且質性在錯區）→ 'hard'。
    - 其餘介於門檻間的小偏差 → 'soft'（照交付）。"""
    if not isinstance(pct, (int, float)):
        return "ok"
    if pct >= soft_floor:
        return "ok"
    if no_soft:
        return "hard"
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

    # A1（B0CDF6A0）：正對=false 是客廳普世硬傷，不需要用戶確認分區也要擋
    # （北歐電視櫃跑到餐廳位、face 沒人問直接 ok=true 的根治）。
    # None 的 fail-closed 仍只在有 layout ctx 時做（無 ctx 時模型答不出來不硬扣）。
    _sfe_early = result.get("sofa_focal_face_each_other")
    if _sfe_early is False and not has_layout_ctx:
        result["focal_anchor_misaligned_with_sofa"] = True
        prev = (result.get("reason") or "").strip()
        tag = "[orientation] 沙發與電視櫃／焦點家具沒有正面相對"
        result["reason"] = (tag + " | " + prev) if prev and "皆合理" not in prev else tag
        result["ok"] = False

    if not has_layout_ctx:
        return result

    forced_reasons = []
    if result.get("sofa_back_against_window") is True:
        result["sofa_outside_living_zone"] = True
        forced_reasons.append("沙發背靠窗牆，靠窗只代表深度位置，不代表可以堵在窗前")
    # 正面相對：false 必擋；None（模型沒答）在有 layout 時 fail-closed（1FC382CA 日式 TV 跑餐廳）
    sfe = result.get("sofa_focal_face_each_other")
    if sfe is False:
        result["focal_anchor_misaligned_with_sofa"] = True
        forced_reasons.append("沙發與電視櫃／焦點家具沒有正面相對")
    elif sfe is None and has_layout_ctx:
        result["focal_anchor_misaligned_with_sofa"] = True
        forced_reasons.append("無法確認沙發與電視櫃正面相對（視為未對齊）")
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
    room_type: str = "living",
    design_mode: str = "furnish",
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
            _d, _m = _downscale_for_vision(f.read(), mime)
            return types.Part.from_bytes(data=_d, mime_type=_m)

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
        # A1（B0CDF6A0 根治）：沒有用戶確認分區時，客廳的「沙發-電視正對」以前
        # 連問都不問（face=null 直接放行）→ 電視櫃跑到餐廳位、不對沙發全放行。
        # 正對是客廳的普世結構事實，不依賴誰確認的分區——客廳一律要問。
        if (room_type or "living") == "living":
            layout_q_field = '  "sofa_focal_face_each_other": bool,\n'
            layout_block = """
【客廳通用正對檢查（本案無用戶確認分區，仍必須做）】
- sofa_focal_face_each_other：圖上主沙發「正面」是否直接朝向電視櫃／焦點櫃，
  且兩者大致相對（同一條視線軸）。
- 沙發與電視櫃在互相垂直的兩面牆、電視櫃在餐區／走道深處遠離沙發、
  或根本沒有焦點家具 → 填 false。
- 正常面對面擺放 → 填 true。
"""

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

    # 產品座位數鎖（護城河：清單單人座不可渲成雙人）— 不依賴 user_confirmed layout
    exp_seat = ""
    exp_sofa_name = ""
    if isinstance(layout_context, dict):
        exp_seat = str(layout_context.get("expected_sofa_seating") or "").strip()
        exp_sofa_name = str(layout_context.get("expected_sofa_name") or "").strip()
    product_seat_block = ""
    if (room_type or "living") == "living" and exp_seat in ("single", "multi"):
        if exp_seat == "single":
            product_seat_block = f"""
Q2d: 沙發座位數與購買清單一致（本案清單主沙發為單人座／單椅）
- 商品名參考：「{exp_sofa_name or '單人座沙發'}」
- 渲染圖主沙發必須是**單一座位**（armchair / 單人椅形式），不可是雙人座、三人座、長沙發。
- 若圖上主沙發明顯 ≥2 個座位 → product_sofa_seating_match=false，ok 必須為 false。
- 若是單人座 → product_sofa_seating_match=true。
"""
        else:
            product_seat_block = f"""
Q2d: 沙發座位數與購買清單一致（本案清單主沙發為雙人／三人／沙發組）
- 商品名參考：「{exp_sofa_name or '多人座沙發'}」
- 渲染圖主沙發必須是**多人座**（至少約 2 人寬），不可縮成單人扶手椅。
- 若圖上主沙發明顯只有 1 個座位 → product_sofa_seating_match=false，ok 必須為 false。
- 若是雙人以上 → product_sofa_seating_match=true。
"""
    else:
        # 無座位數資訊時仍要填欄位，預設 true（不因缺資料誤殺）
        product_seat_block = """
Q2d: product_sofa_seating_match — 本案無座位數規格，一律填 true。
"""

    # ── C 層（50873CF0/B0CDF6A0 根治）：產品可見性驗收 ────────────────────
    # 清單對了但「圖上完全沒畫 / 畫成完全不同的東西」以前不驗——書房收納櫃
    # 圖與商品完全不同也 ok=true。客廳 must 商品一律問「有沒有大致出現」。
    # env PRODUCT_VISIBILITY_GATE=0 可整層關閉（出事的急救開關，預設開）。
    _pv_gate_on = os.environ.get("PRODUCT_VISIBILITY_GATE", "1").strip() != "0"
    _must_products = []
    if _pv_gate_on and isinstance(layout_context, dict):
        _mp = layout_context.get("must_products")
        if isinstance(_mp, list):
            _must_products = [p for p in _mp if isinstance(p, dict) and p.get("cat")]
    product_visibility_block = ""
    product_visibility_field = ""
    if _must_products:
        _pv_lines = "\n".join(
            f"  - {p['cat']}: 「{(p.get('name') or '')[:50]}」 {(p.get('desc') or '')[:100]}"
            for p in _must_products
        )
        _pv_keys = ", ".join(f'"{p["cat"]}"' for p in _must_products)
        product_visibility_field = (
            '  "product_visibility": {' +
            ", ".join(f'"{p["cat"]}": "visible|different|missing"' for p in _must_products) +
            '},\n'
        )
        product_visibility_block = f"""
Q2e: 產品可見性（購買清單商品，圖上有沒有「大致出現」——所有房型都檢，
清單=渲染圖是本服務鐵則：客人買的每一件主家具都必須真的畫在圖上）
本案「這個房間」的購物清單商品：
{_pv_lines}
對每一件判斷（key: {_pv_keys}）：
- visible：圖上有同類物件且與商品大致相符（允許輕度風格化、角度/光線差異、局部被遮擋）。
- different：圖上有同類物件，但**明顯是另一件商品**——形狀、顏色、材質全都對不上
  （例：清單是懸浮式淺色電視櫃，圖上畫成灰色大理石檯面落地櫃）。
- missing：圖上**完全沒有**該類物件。
判斷從寬：只有「完全沒畫」或「一眼就知道不是這件」才標 missing/different；
風格化微調一律 visible，不要吹毛求疵。
"""

    # 把 design_mode 直接告訴 judge（比事後蓋 flag 乾淨；reason 也會寫得準）。
    # 注意：下面【結構保留判定】的 walls_changed/ceiling_changed 定義也跟著 design_mode 變，
    # 避免「clause 說標 false、定義卻說新增材質=違規」自相矛盾讓 Gemini 猶豫(Grok #1)。
    if (design_mode or "furnish") == "full":
        _reno_clause = (
            "【裝潢模式 — 重要】本案為「家具＋軟裝＋裝潢」付費整修：牆面油漆/壁紙/線板/造型牆、"
            "天花板間接照明/cove/嵌燈/淺溝縫等【表面飾材改造】都是允許且預期的。"
            "即使你看到線板、cove lighting、造型牆，也請把 walls_changed 與 ceiling_changed 標 false，"
            "並在 reason 寫「full 模式允許的表面裝潢」——除非牆被「移動位置」或房間被重新隔間。"
            "你仍要嚴格抓真結構違規：動到牆的位置/開口、加廚房、多隔間凹間、改窗、換裸露地板，"
            "以及沙發位置/方向/走道/視角比例都要正確。\n\n"
        )
        _walls_def = ("walls_changed：**僅當牆被移動位置/房間被重新隔間**才算 true；"
                      "新增油漆/壁紙/線板/造型牆等表面飾材【不算，標 false】（裝潢模式允許）")
        _ceiling_def = ("ceiling_changed：**僅當天花降板改變高度/結構**才算 true；"
                        "新增間接照明/cove/嵌燈/溝槽等表面處理【不算，標 false】（裝潢模式允許）")
    else:
        _reno_clause = (
            "【家具模式】牆面、天花、地板必須完全保留原始狀態；新增材質/結構即為違規。\n\n"
        )
        _walls_def = ("walls_changed：牆面明顯新增材質（大理石板/木皮/線板/造型牆），"
                      "或**整面牆被換成明顯不同的顏色**（例：白牆變深灰/深色/彩色，"
                      "非光線陰影或白平衡差異）—— 家具模式牆面必須保持原樣；"
                      "僅輕微亮度/色溫差異**不算**")
        _ceiling_def = ("ceiling_changed：與原照對比，天花板**新增**了原本沒有的結構"
                        "（嵌燈陣列/木作/降板/間接照明溝槽/整圈燈帶光暈）才算 true——"
                        "**原照本來就有的 cove/嵌燈/線板被正確保留下來，不算**；"
                        "吊燈或單一燈具**不算**結構")

    # 1A3B0C68：沙發視線對大門的專屬檢查（sofa_faces_walkway 判太嚴——要正中對準
    # 走道才算；「電視跟大門擠同一面牆、坐沙發視線掃到大門」這種體感不適漏掉了）
    _is_living_q = (room_type or "living") == "living"
    entrance_q_field = ('  "sofa_facing_entrance_door": bool,\n' if _is_living_q else "")
    entrance_face_block = ("""
Q2f: sofa_facing_entrance_door（沙發視線是否對著大門 — 客廳必答）
- 先在 image_2 找大門/玄關門（常見：深色金屬門、雙開門、有金邊飾條的門）。
- 符合**任一**條件 → true：
  a) 人坐在主沙發上正面直視，大門門扇落在視線正前方約 ±35 度範圍內。
  b) 沙發面向的電視/焦點櫃與大門在同一面牆上**緊鄰**——櫃體邊緣到門框的距離
     目測小於一個櫃身寬（電視和大門擠在一起，坐沙發看電視時大門就在畫面裡）。
- 大門在沙發的側面、側後方、背後、與電視櫃距離超過一個櫃身寬，
  或 image_2 根本看不到大門 → false。
- 這是體感判斷：客戶坐上沙發看電視時會不會「同時看著自家大門」？會 → true。
""" if _is_living_q else "")

    product_q_field = (
        '  "product_sofa_seating_match": bool,\n'
        if exp_seat in ("single", "multi") else ""
    ) + product_visibility_field + entrance_q_field
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
  "furniture_blocks_door": bool,
  "sofa_faces_walkway": bool,
  "camera_axis_preserved": bool,
  "main_window_region_match": bool,
  "passage_openings_preserved": bool,
  "offframe_room_invaded": bool,
  "render_bboxes": {{
    "sofa":          [ymin, xmin, ymax, xmax] 或 null,
    "main_window":   [ymin, xmin, ymax, xmax] 或 null,
    "focal_anchor":  [ymin, xmin, ymax, xmax] 或 null,
    "entrance_door": [ymin, xmin, ymax, xmax] 或 null
  }},
{layout_q_field}{product_q_field}  "reason": "簡短中文 60 字內描述主要問題（ok=true 時填 '結構與動線皆合理'）"
}}

【render_bboxes（客觀定位用，務必精準）】
- 在「渲染圖 image_2」上，用 normalized 0–1000 的 [ymin, xmin, ymax, xmax] 標出：
  sofa（主沙發整體）、main_window（畫面中主要的窗戶/落地窗）、focal_anchor（電視櫃/視聽櫃/主視覺焦點家具）、
  entrance_door（大門/玄關門整組門片，常見深色金屬雙開門；畫面中看不到就填 null）。
- 這是給後端「直接量測沙發離窗多近、家具離大門多近」用的客觀座標，請依實際畫面標，不要憑空想像；看不到的物件填 null。
- 標完後回頭核對 Q1b：若 focal_anchor 或 sofa 的框與 entrance_door 的框重疊、
  或間距目測小於一個門寬 → furniture_blocks_door 必須照實標 true。

{_reno_clause}【結構保留判定】
- kitchen_added：原圖沒廚房元素，渲染圖出現廚房櫥櫃/水槽/料理台/餐桌/瓦斯爐
- recessed_space_added：渲染圖出現原圖沒的凹間/額外房間/隔斷/廊道
- windows_changed：窗戶數量/位置/形狀差異 > 20%。
  也包含「憑空窗簾」（FE964758 抓漏）：原圖是實牆/通道的位置，渲染圖掛上
  整面落地窗簾、看起來像那裡有窗 → true（客戶會誤以為該處有窗，結構謊言）。
  窗簾掛在原圖真實窗戶上 → 不算。
- {_walls_def}
- {_ceiling_def}
- floor_changed：**裸露地板**材質/方向/比例顯著被改。**新增地毯不算 floor_changed**

【空間保真判定 — SPATIAL FIDELITY（最高優先，寧可誤判為不合格）】
核心原則：**image_1（原始照片）是這個房間長什麼樣的唯一真相**。不要憑
「一張看起來合理的客廳」就放行——要問「這**還是不是 image_1 那一間、那個視角**」。
把每一項當成兩張圖疊在一起比對，不確定就判成「未保留 / 被入侵」。

- camera_axis_preserved（相機軸線是否保留）：
  * image_2 是否從 **image_1 大致相同的相機位置與朝向** 拍攝（同一條觀看軸線、
    同樣的景深走向）。
  * = false 當：視角被換成「往房間深處長廊看」「鏡頭明顯轉向別面牆」「景深/透視
    被壓扁或拉長成另一種空間」——即使家具擺得漂亮也算 false。
- main_window_region_match（主窗方位是否一致）：
  * image_1 的主要窗戶/落地窗在畫面的哪一側（左/右/正前/後方）。
  * = false 當：渲染圖把主窗移到不同的牆或方位（例：原圖大窗在**左側**，渲染圖
    變成窗在**正後方/右側**）。
- passage_openings_preserved（走道/門洞是否保留）：
  * 先在 image_1 找出所有「開口」：房門、門洞、通往走廊/其他房間的開口、陽台門。
  * = false 當：image_1 明顯可見的走道開口或門洞，在 image_2 中**被牆面/家具蓋掉、
    消失、或被填平成連續實牆**。（這是「走道不見了」的客觀判準）
- offframe_room_invaded（畫面外空間是否被畫進畫面）：
  * = true 當：image_1 的這個視角**看不到**的整區空間（整套廚房櫥櫃+電器、另一個
    房間、額外的落地窗牆），在 image_2 中**憑空長進畫面**。
  * 注意：這跟 kitchen_added 互補——kitchen_added 看「有沒有冒出廚房元素」，
    這一項看「有沒有把畫面外的整個房間/廚房搬進這個視角」。
  * image_1 本來就framed到的東西不算入侵；只有原視角看不到、卻被生成出來的才算。

【家具動線判定 — 拆成兩個獨立問題，分開判斷】

Q1: furniture_blocks_walkway（家具「物理擋住」主動線）
- furniture_blocks_walkway = true 當任一成立：
  * 沙發、茶几、地毯或大型家具明顯擋住「左/右側走廊開口」或「主動線」，人需繞行/跨越才能通過
  * 沙發「浮」在房間中間（不靠牆），且本體位於走道路徑上
  * 茶几或地毯的一部分延伸到走道區，導致通道寬度看起來 < 60cm
  * 從入口走到窗邊或從入口走到房門開口，路徑被家具吃掉
  * L 型沙發的轉角或側邊伸入走廊開口前的淨空區
- furniture_blocks_walkway = false 當動線實際可行（沒有家具擋路）

Q1b: furniture_blocks_door（家具擋住「門」— 跟走道分開判斷，F87A75BB 抓漏）
- 先在**原始照片 image_1** 找出所有「門」：大門/玄關門（含深色金屬防盜門）、
  房間門、陽台門、以及沒有門片的門洞開口。
- furniture_blocks_door = true 當任一成立：
  * 電視櫃/邊櫃/沙發/任何家具放在任一扇門的正前方或與門重疊
    （開門會撞到、或人无法直接走到那扇門）
  * 家具遮住門的下半部（視覺上門被家具「切掉」）
  * 特別注意大門/玄關門——渲染圖把電視櫃貼著大門擺 = 一定是 true
  * 大門「開啟弧形範圍」內站著任何家具（門推開 90 度會撞到）= true
  * 大型家具（沙發/高櫃/床）貼著大門「同一面牆」擺、且家具邊緣到門框的
    距離目測小於一個門寬（雙開門以整組門片寬計）= true——
    玄關落塵區被家具吃掉、進門的人會直接撞到家具側面
    （6F1BFC19 抓漏：沙發貼在大門旁邊，舊條文只看「正前方」就放行了）
- furniture_blocks_door = false 當每扇門前方都有淨空、可直接走到，
  且大門旁一個門寬內沒有大型家具貼牆

Q2: sofa_faces_walkway（沙發「正面朝向」走道 — 即使沒擋）
- sofa_faces_walkway = true 當以下成立（**獨立於 Q1 判斷**）：
  * 沙發的「正面」（坐下時眼睛看的方向）直接對著走廊開口、側牆房門、或主走道
    （坐下時眼前就是有人走來走去的動線，心理不適）
  * 沙發的「正面」直接對著**大門/玄關門**（風水與心理雙重大忌，一定是 true）
  * 沙發「背」靠的方向也對 — 看「正面朝向」哪個 vector：
    - 沙發正面朝 TV 牆/窗/室內焦點 → false
    - 沙發正面朝 走廊開口/房門/主走道/大門 → true
  * 注意：沙發**沒擋路**但「面對走道」依然 true（這是兩個獨立檢查）
- sofa_faces_walkway = false 當沙發正面朝向：
  * 對面 TV 牆（純實牆，沒開口）
  * 窗景方向
  * 室內主視覺焦點（壁畫/景觀/裝飾牆）
{long_room_validation_block}{sofa_side_validation_block}{product_seat_block}{product_visibility_block}{entrance_face_block}
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
- 若 furniture_blocks_door=true → reason 必須**明確寫**「電視櫃擋住大門」「家具擋住房門」
  「櫃體與門重疊」等，指名是哪件家具擋哪扇門
- 若 product_visibility 有 missing/different → reason 指名哪件商品沒畫/畫錯
  （例「清單茶几未出現在圖上」「電視櫃畫成完全不同的款式」）
- 若 sofa_faces_walkway=true → reason 必須**明確寫**「沙發朝向走廊」「沙發正面對著房門」「沙發面對走道」
  「沙發正對大門」（不要用「結構與動線皆合理」這種模糊語句）
- 若 sofa_outside_living_zone=true → reason 必須**明確寫**「沙發未在確認 living zone」
  「沙發違反確認分區」或對等說明
- 若 focal_anchor_misaligned_with_sofa=true → reason 必須**明確寫**「主牆家具未對齊沙發」
  「電視櫃位於前段／入口側」「焦點家具不存在」「客廳組合被拉散」或對等說明
- 若 sofa_back_against_window=true → reason 必須寫「沙發背靠窗牆」
- 若 sofa_focal_face_each_other=false → reason 必須寫「沙發與電視櫃未正面相對」
- 若 focal anchor / 電視櫃落在餐廳區、玄關或遠離沙發 → sofa_focal_face_each_other=false 且
  focal_anchor_misaligned_with_sofa=true，reason 寫「電視櫃在餐廳區／未正對沙發」
- 若 sofa_against_side_wall=false → reason 必須寫「長條房沙發未貼左右長牆」
- 若 sofa_intrudes_walkway=true → reason 必須寫「沙發侵入主走道」
- 若 coffee_table_in_walkway=true → reason 必須寫「茶几或地毯侵入主走道」
- 若 product_sofa_seating_match=false → reason 必須寫「沙發座位數與商品不符（單人／雙人）」
- 上述都 false 且結構保留 → reason = 「結構與動線皆合理」

ok = 所有違規 flag 全為 false，且有 layout_context 時 sofa_focal_face_each_other 必須為 true。
reason 必須具體（例「L 沙發擋住左側通往臥室的走廊開口」非「動線不合理」）。
"""

    # 20A8220A：Gemini 偶發回傳格式瑕疵 JSON，裸 parse 一炸驗證就整張跳過（圖裸奔出貨）。
    # 寬鬆解析 + 解析失敗重新生成一次（重打一次幾乎都會回乾淨 JSON）。
    result = None
    for _attempt in range(2):
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[original_part, render_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
        try:
            result = _json_loads_lenient(response.text)
            break
        except (ValueError, json.JSONDecodeError) as pe:
            if _attempt == 1:
                raise
            print(f"[validate_render] JSON 解析失敗，重打一次: {str(pe)[:100]}")
    # 確保新欄位永遠存在（沒 layout_context 時預設 False；不誤判）
    result.setdefault("sofa_outside_living_zone", False)
    result.setdefault("furniture_blocks_door", False)   # F87A75BB：電視櫃擋大門盲區

    # 空間保真閘門（2A520C25 抓漏：法式客廳把 photo_03 重畫成廚房長廊、走道消失，
    # 但 windows/walls_changed 全 false → 假及格交付）。
    # R1（Grok 審核）：living 若 Gemini 完全沒回四個保真鍵 → 不能 silent pass，
    # 保守擋下（fail-closed，符合「寧可誤擋」；dry-run 顯示 Gemini 穩定會回，罕見）。
    _fid_keys = ("camera_axis_preserved", "main_window_region_match",
                 "passage_openings_preserved", "offframe_room_invaded")
    _fid_all_missing = all(k not in result for k in _fid_keys)
    result.setdefault("camera_axis_preserved", True)
    result.setdefault("main_window_region_match", True)
    result.setdefault("passage_openings_preserved", True)
    result.setdefault("offframe_room_invaded", False)
    _fid_fail, _fid_problems = compute_spatial_fidelity(result)
    if _fid_all_missing and (room_type or "living") == "living":
        _fid_fail = True
        _fid_problems = ["保真欄位未回傳，保守擋下重驗"]
    result["spatial_fidelity_fail"] = _fid_fail
    if _fid_problems:
        result["ok"] = False
        tag = "[空間保真] " + "、".join(_fid_problems)
        prev = (result.get("reason") or "").strip()
        # 保真失敗的 reason 放最前面（它比家具擺位更嚴重，Phase3/客服要先看到）
        result["reason"] = (tag + " | " + prev) if prev and "結構與動線皆合理" not in prev else tag
    # C2.1 debug 欄位（即使 false 也要有；沒 layout_context 時為空字串）
    result.setdefault("sofa_zone_assessment", "")
    result.setdefault("sofa_depth_position", "")
    result.setdefault("confirmed_living_zone_reference", "")
    # C2.2 focal_anchor 欄位（向下相容）
    result.setdefault("focal_anchor_misaligned_with_sofa", False)
    result.setdefault("focal_anchor_depth_position", "")
    result.setdefault("focal_anchor_assessment", "")
    # 沙發/客廳 code-gate 只對客廳有意義；餐廳/主臥/書房沒有沙發，跑了只會產生
    # 「沙發貼錯邊」這種假 ng → 觸發重試把該房畫歪(job 39A6843D 餐廳幻想根因)。
    _is_living_room = (room_type or "living") == "living"
    if _is_living_room:
        result = _enforce_sofa_focal_orientation(
            result,
            has_layout_ctx,
            is_long_room_layout=is_long_room_layout,
        )
    # 沙發左右邊 ground truth 驗收（2026-06-21）：layout 指定了 sofa_side 時，
    # 沙發貼到對側即 sofa_on_wrong_side=true（high severity → 觸發帶原因的重試）。
    result.setdefault("sofa_side_detected", "unclear")
    result.setdefault("sofa_on_wrong_side", False)
    if _is_living_room and has_layout_ctx and expected_sofa_side in ("left", "right"):
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

        # ── BB034AB8 根治：門邊淨空改幾何計算，不靠判官目測布林 ──
        # 判官 bbox 標得準（北歐高櫃 x=94 跟門框重疊）但 blocks_door 卻答 False
        # → 0 次重試就交付。程式直接量測 bbox，抓到就強制 blocks_door 進重試鏈。
        if (room_type or "living") == "living":
            _viol = _door_adjacency_violation(rb)
            if _viol and not result.get("furniture_blocks_door"):
                _nm, _x_gap, _door_w = _viol
                result["furniture_blocks_door"] = True
                result["ok"] = False
                _tag = (f"[幾何] {_nm} 與大門間距 {_x_gap:.0f} < 0.25 門寬"
                        f"（門寬 {_door_w:.0f}）——家具貼門/擋門")
                _prev = (result.get("reason") or "").strip()
                if _tag not in _prev:
                    result["reason"] = (_tag + " | " + _prev) if _prev and "皆合理" not in _prev else _tag

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
            # 深度分級 (2026-06-25 第一刀)：深度不再是「差幾趴就掉件」的主因。
            # 65–70% 這種已在窗側、結構/動線/朝向都合理的圖照交（避免少一張→客戶覺得被騙）；
            # 但沙發真的跑中前段（<60%）仍硬擋重生。規則：>=72% OK／60–72% 軟交／<60% 硬傷。
            # strict 容差=5：hard_floor=65 → 實際 <60 才硬；soft_floor=72 → >=72 即 OK。
            # 真硬傷（背窗/擋道/TV未對/錯邊/改結構）由各自質性 flag 處理，與這個深度數字無關。
            sofa_hard_floor = 65 if has_dining_middle_constraint else 58
            sofa_soft_floor = 72 if has_dining_middle_constraint else 64
            anchor_hard_floor = 60 if has_dining_middle_constraint else 50
            anchor_soft_floor = 72 if has_dining_middle_constraint else 60

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
            # 明確指定位置、但 bbox 量不到客觀深度（sofa/window 沒框）→ 不可只憑 Gemini 自述放行。
            # 標 sofa_depth_unverified=True 觸發重試（嘗試取得 bbox 再驗）；持續量不到才帶標記交付，
            # 不直接 drop（避免好圖被誤丟）。
            if strict_depth and result.get("sofa_depth_grounded_pct") is None:
                result["sofa_depth_unverified"] = True
            # 深度只當「太靠前=硬傷」的數值底線，不死卡 80%。72–80% 視為軟傷照交。
            # 真正的分區/朝向錯誤（背窗、沒貼側牆、TV未對齊、中段被佔）由各自硬 flag 擋，
            # 不靠這個百分比 → 避免「肉眼已靠窗只差幾趴」被整單擋掉而全滅。
            forced_solz = False        # 硬傷：沙發太靠前（跑錯分區）
            forced_focal = False       # 硬傷：焦點太靠前 / 完全沒對向
            soft_notes = []            # 軟傷：可交付，只記錄

            sofa_cls = _depth_classification(
                sofa_pct, sofa_hard_floor, sofa_soft_floor, strict_depth,
                any(kw in assessment_text for kw in soft_kw_sofa),
            )
            if sofa_cls == "hard":
                forced_solz = True
            elif sofa_cls == "soft":
                soft_notes.append(
                    f"sofa 深度估 {int(sofa_pct)}% 略低於理想 {sofa_soft_floor}%（軟傷，照交付）"
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
                        f"focal_anchor 深度估 {int(anchor_pct)}% 略低於理想 {anchor_soft_floor}%（軟傷，照交付）"
                    )

            if soft_notes:
                existing_soft = result.get("soft_issues")
                result["soft_issues"] = (existing_soft if isinstance(existing_soft, list) else []) + soft_notes

            if (forced_solz or forced_focal) and _is_living_room:
                # 硬傷才覆寫 flag + ok=false（觸發重生 / 擋交付）；非客廳不套沙發深度 gate
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

    # ── 交付安全網 (2026-06-23)：驗收理由若點名這些結構/分區問題，強制硬傷、不准交付 ──
    # 即使深度數字勉強過，只要 reason 抓到「電視櫃偏前/佔用餐廳/未對齊/擋入口」就擋。
    _reason_text = result.get("reason") or ""
    _BLOCK_FOCAL_KW = (
        "電視櫃位置偏前", "電視櫃偏前", "焦點偏前", "佔用餐廳", "應保留給餐廳",
        "電視櫃在餐廳", "電視櫃跑到餐廳", "落在餐廳", "位於餐廳",
        "落於中段", "落在中段", "未與後段沙發對齊", "未與沙發對齊", "與沙發未對齊", "未對齊",
        "未正面相對", "沒有正面相對",
    )
    _BLOCK_WALK_KW = (
        "擋入口", "擋住入口", "擋住門口", "封住入口", "擋住走道", "擋住通道",
        "佔用走道", "佔用通道", "阻擋入口", "阻擋走道",
    )
    if any(k in _reason_text for k in _BLOCK_FOCAL_KW):
        result["focal_anchor_misaligned_with_sofa"] = True
    if any(k in _reason_text for k in _BLOCK_WALK_KW):
        result["furniture_blocks_walkway"] = True

    # 產品座位數：false → hard（清單單人 vs 圖上雙人）
    result.setdefault("product_sofa_seating_match", True)
    result.setdefault("product_sofa_seating_mismatch", False)
    if (room_type or "living") == "living" and exp_seat in ("single", "multi"):
        if result.get("product_sofa_seating_match") is False:
            result["product_sofa_seating_mismatch"] = True
            result["ok"] = False
            tag = "[產品一致] 沙發座位數與購買清單不符"
            prev = (result.get("reason") or "").strip()
            if tag not in prev:
                result["reason"] = (tag + " | " + prev) if prev else tag
    else:
        result["product_sofa_seating_match"] = True
        result["product_sofa_seating_mismatch"] = False

    # 1A3B0C68：沙發視線對大門 → 硬傷（客廳限定；非客廳恆 False）
    result.setdefault("sofa_facing_entrance_door", False)
    if (room_type or "living") != "living":
        result["sofa_facing_entrance_door"] = False
    elif result.get("sofa_facing_entrance_door") is True:
        result["ok"] = False
        tag = "[對門] 沙發視線正對大門（客戶坐上沙發第一眼看著自家大門）"
        prev = (result.get("reason") or "").strip()
        if tag not in prev:
            result["reason"] = (tag + " | " + prev) if prev and "皆合理" not in prev else tag

    # C 層：產品可見性 → product_visibility_fail（env 關閉或沒帶清單時恆 False）
    # 46F1B2B5 分級（誤擋爆炸教訓：6 張只交付 1 張、12 次重試，臥室/書房全因
    # 「清單落地燈沒畫」被丟）——
    #   必備主家具（該房型 ROOM_RULES.must）missing/different → 硬傷重生（不變）
    #   加分品項（燈具/單椅/邊几…非 must）沒入圖 → 不殺圖，記到
    #   visibility_nice_bad，交付層把該品項從購物清單移除（清單=圖從清單端成立）
    result.setdefault("product_visibility", {})
    result["product_visibility_fail"] = False
    result.setdefault("visibility_nice_bad", [])
    if _must_products and isinstance(result.get("product_visibility"), dict):
        try:
            from furniture_match import ROOM_RULES
            _must_cats = set((ROOM_RULES.get(room_type or "living") or ROOM_RULES["living"])["must"])
        except Exception:
            _must_cats = {"sofa", "coffee_table", "rug", "media_console", "bed", "table", "chair",
                          "dining_table", "dining_chair", "storage"}
        _bad = {k: str(v).strip().lower() for k, v in result["product_visibility"].items()
                if str(v).strip().lower() in ("missing", "different")}
        _pv_bad_must = [f"{k}:{v}" for k, v in _bad.items() if k in _must_cats]
        result["visibility_nice_bad"] = [k for k in _bad if k not in _must_cats]
        if _pv_bad_must:
            result["product_visibility_fail"] = True
            result["ok"] = False
            tag = "[產品可見性] 清單商品未如實出現：" + "、".join(_pv_bad_must[:4])
            prev = (result.get("reason") or "").strip()
            if tag not in prev:
                result["reason"] = (tag + " | " + prev) if prev and "皆合理" not in prev else tag

    # ── 硬傷彙整 (2026-06-21)：delivery / retry 共用的單一判準 ──
    result.setdefault("soft_issues", [])
    result.setdefault("sofa_depth_unverified", False)  # 明確位置案但 bbox 量不到 → 重試（不 drop）
    # step-2：非客廳房型（臥室/餐廳/書房）關掉「沙發專屬」檢查——這些房間本來就不該有
    # 沙發/電視焦點，沿用客廳規則會把好圖誤判成硬傷 drop。只保留結構類 + 擋走道。
    result["room_type"] = room_type or "living"
    if room_type and room_type != "living":
        for _f in ("sofa_on_wrong_side", "sofa_outside_living_zone",
                   "sofa_back_against_window", "focal_anchor_misaligned_with_sofa",
                   "sofa_faces_walkway", "sofa_depth_unverified",
                   "product_sofa_seating_mismatch"):
            if result.get(_f):
                result[_f] = False
        result["product_sofa_seating_match"] = True

    # full（家具＋軟裝＋裝潢）模式：牆面/天花「表面飾材」改造是付費內容、預期內，
    # 不該被當硬傷 drop（否則裝潢成功的圖反被驗收殺掉，job EC9D03F6）。
    # 仍嚴守真結構違規：加廚房 / 多隔間凹間 / 動窗 / 換地板。
    if (design_mode or "furnish") == "full":
        for _f in ("walls_changed", "ceiling_changed"):
            if result.get(_f):
                result[_f] = False
                result["full_mode_surface_relaxed"] = True   # debug：此筆有放行表面裝潢
                result.setdefault("soft_issues", []).append(
                    f"{_f}（裝潢模式預期的牆/天花飾材改造，非結構違規）")

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

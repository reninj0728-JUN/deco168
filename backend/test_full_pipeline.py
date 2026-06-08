"""
完整 pipeline 測試腳本
用法：python3.11 test_full_pipeline.py <房間照片路徑> [風格1,風格2]
範例：python3.11 test_full_pipeline.py room.jpg modern,nordic

不需要影片，一張房間照片就能測試完整流程：
  Gemini 看圖分析 → 家具配對 → Flux 生成渲染圖
"""
import os, sys, json, base64, time, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

VALID_STYLES = ["modern","japanese","luxury","nordic","muji","cream","wood","french","chinese-modern"]

_client = None
_SYSTEM_PROMPT = None

def _get_client():
    global _client
    if _client is None:
        key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_AI_KEY')
        if not key:
            raise RuntimeError("GEMINI_API_KEY 未設定，請在 Railway Variables 設定")
        from google import genai
        _client = genai.Client(api_key=key)
    return _client

def _get_system_prompt():
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        base = Path(__file__).parent
        txt = (base / "gemini_analyze.py").read_text(encoding="utf-8")
        _SYSTEM_PROMPT = txt.split('SYSTEM_PROMPT = """')[1].split('"""')[0]
    return _SYSTEM_PROMPT

from google.genai import types
from furniture_match import enrich_renders
from prompt_builder import build_nano_banana_inputs
import fal_client
import requests

# ─── Step 1: Gemini 分析照片（支援多張）─────────────────────────────────────

# 空間類型中文標籤
_SPACE_LABEL = {
    "living":   "客廳",
    "dining":   "餐廳",
    "bedroom":  "臥室",
    "kitchen":  "廚房",
    "study":    "書房",
    "whole":    "全室",
}


def analyze_image(image_path: str, user_styles: list[str] | None = None,
                  extra_photos: list[str] | None = None,
                  space_type: str = "living",
                  render_angle: str = "single",
                  photo_sources: list[str] | None = None,
                  video_path: str | None = None) -> dict:
    """
    image_path   : 主要照片（給渲染基底用）
    extra_photos : 補充角度照片清單（一起送 Gemini 分析）
    space_type   : living / dining / bedroom / study / whole（前端帶來）
    render_angle : single / multi（前端帶來）
    photo_sources: 跟 all_paths 同長度的 source 標記列表，值是 "photo" 或 "video_keyframe"
                   None 則全部視為 "photo"（純照片模式預設）
    video_path   : 若提供，會上傳到 Gemini Files API 讓 Gemini 看整支影片理解全室
                   None 則純靜態圖模式

    Phase 1 + B + B' 新增：
      - 每張照片做 room_type 分類 → photo_classifications[]
      - render_angle=multi 時，從分類結果挑 3 張正確的 → regions[]
      - 不足時 → analysis.insufficient_photos = {...}
      - best_photo_index 必須從 space_type 對應的子集挑（whole 例外）
      - 同時有 photo 跟 keyframe 時，best_photo_index 優先 photo（render 品質）
      - video_path 給了 → Gemini 看影片做全室理解（動線/連接/相對位置）
    """
    all_paths = [image_path] + (extra_photos or [])
    photo_count = len(all_paths)
    sources = photo_sources or (["photo"] * photo_count)
    if len(sources) != photo_count:
        sources = ["photo"] * photo_count  # 防錯：長度不對就忽略

    print(f"\n{'='*56}")
    print(f"[Step 1] Gemini 分析 {photo_count} 張  (space_type={space_type}, render_angle={render_angle})")
    n_photo = sum(1 for s in sources if s == "photo")
    n_kf = sum(1 for s in sources if s == "video_keyframe")
    print(f"         來源：{n_photo} 張用戶照片 + {n_kf} 張影片 keyframe")
    for p, s in zip(all_paths, sources):
        print(f"         · [{s}] {p}")
    print(f"{'='*56}")

    def load_img(path):
        ext = Path(path).suffix.lower()
        mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode(), mime

    imgs = [load_img(p) for p in all_paths]

    if user_styles and all(s in VALID_STYLES for s in user_styles):
        fixed_styles = user_styles[:2]
    else:
        fixed_styles = ["modern", "nordic"]
    style_instruction = (
        f"用戶選定 {len(fixed_styles)} 種風格：{', '.join(fixed_styles)}。"
        f"renders 陣列必須恰好 {len(fixed_styles)} 個，順序與 style 完全對應。"
    )

    space_label = _SPACE_LABEL.get(space_type, space_type)
    render_angle_label = {"single": "單角度", "multi": "多角度"}.get(render_angle, render_angle)

    # 依 (space_type, render_angle) 動態組規則
    if space_type == "whole":
        room_focus_rule = (
            "用戶選的是【全室】：請對每張照片獨立分類房間用途，不要假設全部同一空間。"
        )
        best_photo_rule = (
            "best_photo_index：從所有照片裡挑「最具設計呈現價值的主視角」。"
            "優先選 room_type='living' 的；若沒有 living，依序退而求其次 dining > bedroom > 任意。"
        )
        if render_angle == "multi":
            regions_rule = (
                "regions[] 必須恰好 3 個，3 個 room_type 必須**全部不同**（優先 living/dining/bedroom）。"
                "每個 region 帶 name（中文，例如『客廳主視角』）、best_photo_index、room_type、angle_label（可空）。"
                "如果可分類的不同房間少於 3 種，regions 仍輸出可用的（< 3 個也行），不要硬補。"
            )
            insufficient_rule = (
                "如果不同 room_type 的照片少於 3 種，必須設 "
                "insufficient_photos = {required: 3, found: <實際 room_type 種數>, room_type: 'whole', message: '...'}。"
            )
        else:
            regions_rule = "render_angle=single：regions 設為空陣列 []。"
            insufficient_rule = "如果完全沒有照片可分類，設 insufficient_photos.found=0。"
    else:
        room_focus_rule = (
            f"用戶選的是【{space_label}】（room_type='{space_type}'）："
            f"只關注被分類為 '{space_type}' 的照片。其他空間（餐廳/玄關/走道等）即使存在也不該用來當主視角或 region。"
        )
        best_photo_rule = (
            f"best_photo_index：**必須**從 room_type='{space_type}' 的照片中挑「最完整呈現空間設計感的角度」。"
            f"如果完全沒有 '{space_type}' 的照片，best_photo_index 設為 -1。"
        )
        if render_angle == "multi":
            regions_rule = (
                f"regions[] 必須有 3 個，全部 room_type='{space_type}'，3 個不同 angle_label "
                f"（例如『入口往內』『沙發牆視角』『窗邊回看』『電視牆視角』『對角全景』）。"
                f"如果 '{space_type}' 照片少於 3 張，regions 仍輸出實際可用的（< 3 個也行），不要硬補非 {space_type} 的照片。"
            )
            insufficient_rule = (
                f"如果 room_type='{space_type}' 的照片少於 3 張，"
                f"必須設 insufficient_photos = {{required: 3, found: <實際張數>, room_type: '{space_type}', "
                f"message: '{space_label}多角度需 3 張不同{space_label}角度，目前只有 N 張'}}。"
            )
        else:
            regions_rule = "render_angle=single：regions 設為空陣列 []。"
            insufficient_rule = (
                f"如果 room_type='{space_type}' 的照片是 0 張，"
                f"必須設 insufficient_photos = {{required: 1, found: 0, room_type: '{space_type}', "
                f"message: '本方案需 ≥1 張{space_label}照片'}}。"
            )

    # 照片來源說明
    photo_source_lines = []
    for i, src in enumerate(sources):
        photo_source_lines.append(f"  - 第 {i} 張：{src}")

    video_role_note = (
        "**另外**你會看到「**1 段用戶上傳的影片**」。這段影片是**全室理解材料**：\n"
        "  - 用它理解整個空間的格局、動線、各房間相對位置、玄關往哪邊走到客廳/餐廳/臥室\n"
        "  - 用影片裡看到的走動順序與場景連結，幫助對下面 N 張靜態圖做正確分類\n"
        "  - 影片本身**不**作為 render 候選；render 永遠用靜態圖（photo 或 keyframe）\n"
        if video_path else ""
    )
    photo_source_note = (
        f"你看到的 {photo_count} 張**靜態影像**來源如下：\n"
        + "\n".join(photo_source_lines)
        + "\n"
        "其中：\n"
        "  - photo：用戶實際拍的照片，**畫質清晰、是用戶想呈現的角度**，是 render 首選\n"
        "  - video_keyframe：從用戶上傳的影片均勻抽出的時間切片，**畫質可能較差、角度可能歪**，"
        "主要功能是「作為 render 候選 base」，**不是 render 首選**\n"
        + video_role_note +
        "best_photo_index 規則加碼：如果同一個 room 同時存在 photo 跟 video_keyframe，"
        "best_photo_index **必須優先指向 photo 那張**（除非該 photo 對該 room 角度太爛）。"
        "只有當該 room 完全沒有 photo 時，best_photo_index 才允許指向 video_keyframe。"
    )
    photo_count_note = (
        f"你現在看到 {photo_count} 張靜態影像{('+ 1 段影片' if video_path else '')}。\n{photo_source_note}"
        if photo_count > 1 else f"你現在看到 1 張照片{('+ 1 段影片' if video_path else '')}。"
    )

    prompt = f"""
{photo_count_note}
分析這{'些' if photo_count > 1 else '張'}空間照片，理解完整格局，並依使用者選的方案做照片分類。

【使用者方案】
space_type = {space_type}（{space_label}）
render_angle = {render_angle}（{render_angle_label}）
{room_focus_rule}

【空間量測步驟 — 必須先做】
1. 找出畫面中可見的基準物：門框（高200cm/寬90cm）、窗台（距地90cm）、插座（距地30cm）、標準沙發（高85cm）
2. 交叉比對所有照片中的基準物，推算房間長度、寬度、天花板高度（公尺）
3. 找出各張照片拍攝方向，確認哪些牆面/角落已被覆蓋
4. 用長×寬計算坪數（1坪=3.305㎡），給保守估計
5. 特別記錄：天花板結構（明管/梁柱/灑水頭）、門的位置、窗戶位置——這些在渲染時必須保留

【照片分類 — 必須做（每張照片獨立判斷）】
本產品目標客戶是**空屋**裝潢設計，多數照片裡不會有家具。
所以判斷 room_type **不能仰賴家具線索**（沙發/餐桌/床/鞋櫃可能根本不在）。
你必須改用**空間結構與格局線索**：

room_type 可選值：living / dining / bedroom / kitchen / entrance / corridor / bathroom / study / balcony / unknown

各 room_type 的結構線索（沒有家具也能判斷）：
- living（客廳）:
  * 公空間中採光最好、最大的區域
  * 較大的窗戶或景觀窗
  * 開放/匯集動線（多個門口/開口都通過此處）
  * 牆面長度足以放沙發 + 電視牆對望
  * 通常與餐廳/玄關相連，沒有獨立隔間門
- dining（餐廳）:
  * 客廳與廚房之間的中段過渡區
  * 鄰近廚房管線/開口（牆面可能可見廚房入口）
  * 上方常有預留吊燈位（出線盒在中央，不是兩側）
  * 空間比客廳小、比走道寬
- bedroom（臥室）:
  * 有獨立進入的房門（門關起來會形成封閉空間）
  * 較小的方型/矩形格局，私密性高
  * 窗戶通常比客廳小
  * 牆面比例適合放床（一面長牆 + 兩面短牆）
  * 可能可見預留衣櫃凹槽或更衣室開口
- kitchen（廚房）:
  * 可見流理台預埋線（瓦斯/上下水/抽油煙機排管）
  * 牆面瓷磚或防水材質
  * 通常為獨立隔間或開放但有明顯區隔
- entrance（玄關）:
  * 看得到大門（含門框、門軸、貓眼/門鎖）
  * 入口緩衝區，可能可見對講機/開關面板/弱電箱集中
  * 鞋櫃預留凹槽或牆面格局留白
- corridor（走道）:
  * 窄長空間（寬通常 < 1.2m）
  * 兩側有房門或開口
  * 不是動線匯集點，是「通過」用的
  * 採光弱，無大窗
- bathroom（浴室）:
  * 防水材質牆面/地板（瓷磚/磁磚）
  * 排水管/通風口
  * 通常很小、有獨立門
- study（書房）:
  * 較小的獨立或半開放空間
  * 不像臥室那樣有明顯床牆，但格局比走道寬
- balcony（陽台）:
  * 對外開放，有護欄/落地門
  * 通常與室內以拉門分隔

判斷流程（**結構為主、家具為輔**）：
1. 先看格局：開放或封閉？空間大小？窗戶大小？動線位置？
2. 再看線索：管線預留位置？牆面材質？門的位置？
3. 最後才看家具（空屋通常沒有）
4. 證據不足 → confidence=medium 或 low，並在 uncertainty_notes 寫缺什麼證據

對每張照片輸出：
{{ "photo_index": 0/1/2..., "room_type": "...", "confidence": "high/medium/low",
   "angle_label": "客廳照片才需要（例如『入口往內』『沙發牆視角』『窗邊回看』）；其他空間填空字串",
   "reason": "看到的結構線索 + 為什麼這判斷（≤50 字，不要只說『有沙發』這種家具線索）",
   "uncertainty_notes": "如果 confidence < high，寫缺什麼證據 / 為什麼不能更確定（≤40 字，high 可空字串）" }}

【best_photo_index 規則】
{best_photo_rule}

【regions[] 規則】
{regions_rule}

【insufficient_photos 規則】
{insufficient_rule}
如果照片數量足夠：insufficient_photos 設為 null。

{style_instruction}

回傳以下 JSON（嚴格照格式）：
{{
  "space_type": "空間類型",
  "estimated_size": "估計坪數",
  "room_dimensions": {{
    "length_m": 數字, "width_m": 數字, "height_m": 數字,
    "confidence": "high/medium/low", "reference_used": "用了哪些基準物"
  }},
  "layout_notes": "格局描述",
  "lighting": "採光條件",
  "current_style": "目前裝潢風格",
  "owner_requests": "未提及",
  "design_analysis": "空間分析摘要，繁體中文，80字以內",
  "recommended_styles": ["style1","style2","style3"],
  "recommend_reason": "推薦原因，50字以內",
  "best_photo_index": "依上述規則，整數或 -1",
  "photo_classifications": [
    {{"photo_index": 0, "source": "photo|video_keyframe", "room_type": "...", "confidence": "...",
      "angle_label": "...", "reason": "結構線索（≤50 字）",
      "uncertainty_notes": "缺什麼證據（≤40 字，confidence=high 可空）"}}
  ],
  "regions": [
    {{"name": "中文名稱", "best_photo_index": 整數, "source": "photo|video_keyframe",
      "room_type": "...", "angle_label": "..."}}
  ],
  "insufficient_photos": null,
  "renders": [
    {{"style":"style_id（要對應 {fixed_styles}）","style_label":"中文名稱","flux_prompt":"逗號分隔keyword，結尾必須是 professional interior design photography, staged showroom, editorial styling, 35mm wide angle, soft natural light, UHD, no people, no text, no watermark, no distortion, no CGI artifacts"}}
  ]
}}

renders 陣列必須恰好 {len(fixed_styles)} 個，順序對應 {fixed_styles}。
photo_classifications 必須有 {photo_count} 個元素，每張照片各一個。
"""

    client = _get_client()
    # 影片上傳（若有）
    uploaded_video = None
    if video_path:
        try:
            print(f"  [影片] 上傳 {video_path} 到 Gemini Files API…")
            uploaded_video = client.files.upload(file=video_path)
            while uploaded_video.state.name == "PROCESSING":
                print(f"  [影片] 處理中… state={uploaded_video.state.name}")
                time.sleep(3)
                uploaded_video = client.files.get(name=uploaded_video.name)
            if uploaded_video.state.name == "FAILED":
                print(f"  [影片] 上傳失敗，改純靜態圖模式")
                uploaded_video = None
            else:
                print(f"  [影片] 就緒 uri={uploaded_video.name}")
        except Exception as e:
            print(f"  [影片] 上傳例外（改純靜態圖）: {e}")
            uploaded_video = None

    contents = []
    if uploaded_video:
        contents.append(uploaded_video)
    contents.extend([
        types.Part.from_bytes(data=base64.b64decode(b64), mime_type=m)
        for b64, m in imgs
    ])
    contents.append(prompt)

    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_get_system_prompt(),
                response_mime_type="application/json",
            ),
        )
    finally:
        # 用完即刪 Gemini Files 上的影片（隱私 + 清理）
        if uploaded_video:
            try:
                client.files.delete(name=uploaded_video.name)
                print(f"  [影片] 已從 Gemini Files 清除")
            except Exception as e:
                print(f"  [影片] 清除例外（可忽略）: {e}")
    elapsed = time.time() - t0

    # Gemini 偶爾在合法 JSON 之後追加 garbage（"Extra data"）→ 用 raw_decode 只取第一個 valid JSON
    raw_text = (resp.text or "").strip()
    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        result, _ = json.JSONDecoder().raw_decode(raw_text)
    dims = result.get('room_dimensions', {})
    print(f"  耗時：{elapsed:.1f}s")
    print(f"  空間：{result.get('space_type')} {result.get('estimated_size')}")
    if dims:
        print(f"  實測：{dims.get('length_m')}m × {dims.get('width_m')}m × H{dims.get('height_m')}m  [{dims.get('confidence')}]")
    print(f"  best_photo_index: {result.get('best_photo_index')}")
    pc = result.get('photo_classifications') or []
    if pc:
        print(f"  photo_classifications ({len(pc)}):")
        for c in pc:
            src = c.get('source', '?')
            print(f"    [{c.get('photo_index')}] src={src:<14} room_type={c.get('room_type','?'):<10} "
                  f"conf={c.get('confidence','?'):<7} angle={c.get('angle_label','') or '-':<14} "
                  f"reason={(c.get('reason') or '')[:60]}")
            unc = (c.get('uncertainty_notes') or '').strip()
            if unc:
                print(f"        uncertainty: {unc[:80]}")
    regs = result.get('regions') or []
    if regs:
        print(f"  regions ({len(regs)}):")
        for r in regs:
            print(f"    - {r.get('name','?')} | room={r.get('room_type','?')} "
                  f"| best_photo={r.get('best_photo_index')} (src={r.get('source','?')}) "
                  f"| angle={r.get('angle_label','')}")
    insuf = result.get('insufficient_photos')
    if insuf:
        print(f"  insufficient_photos: required={insuf.get('required')} found={insuf.get('found')} "
              f"room_type={insuf.get('room_type')} | {insuf.get('message','')}")
    return result


# ─── Step 2: 家具配對 ─────────────────────────────────────────────────────────

def show_furniture(enriched_renders: list[dict]):
    print(f"\n{'='*56}")
    print("[Step 2] 家具配對結果")
    print(f"{'='*56}")
    for render in enriched_renders:
        label = render.get("style_label", render.get("style", ""))
        furniture = render.get("matched_furniture", [])
        print(f"\n  【{label}】 配對 {len(furniture)} 件")
        print(f"  {'品名':<30} {'品牌':<8} {'類別':<8} {'價格':>10}  {'尺寸'}")
        print(f"  {'-'*75}")
        for item in furniture:
            name = item['name_zh'][:28]
            price = f"NT${item['price_twd']:,}" if item.get('price_twd') else '-'
            dims = item.get('dimensions','')[:18]
            print(f"  {name:<30} {item['brand']:<8} {item['category']:<8} {price:>10}  {dims}")


# ─── Step 3: Flux 生成渲染圖 ─────────────────────────────────────────────────

def _build_preserve_clause(analysis: dict | None, design_mode: str = "furnish") -> str:
    """
    把 Gemini 抓到的 architectural_features 變成具體 PRESERVE 指令。
    design_mode:
      - furnish: 只動家具/軟裝，禁止任何裝潢更動（天花板/牆/門/窗一律保留）
      - full:    可以改表面飾材+輕裝修（仍鎖格局結構）
    """
    feats = (analysis or {}).get("architectural_features") or {}
    dims  = (analysis or {}).get("room_dimensions") or {}

    # 反幻想語句（不論模式都加）
    anti_halluc = (
        "STRICT RULES: do not add extra windows; do not add extra doors; "
        "do not add new ceiling lights or fixtures that were not in the source photo; "
        "do not add wall paneling, marble walls, or wood walls unless already present; "
        "keep the exact same room dimensions and proportions as the source photo. "
    )

    parts = ["PRESERVE EXACTLY:"]
    if dims:
        L = dims.get("length_m"); W = dims.get("width_m"); H = dims.get("height_m")
        if L and W and H:
            parts.append(f"room measures {L}m long x {W}m wide x {H}m tall — keep this exact aspect;")
    if feats.get("doors"):   parts.append(f"doors: {feats['doors']} — same count, same positions;")
    if feats.get("windows"): parts.append(f"windows: {feats['windows']} — same count, same positions;")
    if feats.get("kitchen") and feats["kitchen"] != "無":
        parts.append(f"kitchen: {feats['kitchen']};")
    if feats.get("ceiling"):
        parts.append(f"ceiling: {feats['ceiling']} — keep pipes/sprinklers/beams visible if present;")
    if feats.get("floor"):
        parts.append(f"floor: {feats['floor']};")
    if feats.get("walls"):
        parts.append(f"walls: {feats['walls']};")

    if design_mode == "furnish":
        parts.append(
            "MODE: furniture-only restyle. DO NOT modify walls, ceiling, doors, windows, floor finish. "
            "ONLY change movable furniture, soft furnishings (rugs, curtains, cushions), decor objects, and lighting mood."
        )
    else:
        parts.append(
            "MODE: interior refinish. Wall/ceiling surface finish may be updated but DO NOT add structural elements. "
            "ONLY change surface finishes, furniture, decor, lighting mood."
        )

    return anti_halluc + " ".join(parts)


def generate_renders(image_paths, enriched_renders: list[dict], output_dir: str = "output",
                     analysis: dict | None = None, design_mode: str = "furnish",
                     zoning: dict | None = None,
                     customer_notes: str = "",
                     budget_tier: str = "tier3",
                     retry_context: dict | None = None):
    """
    image_paths: 單一路徑或 list；多張時每個 style 輪流用不同角度
    analysis:    Gemini 分析結果，用來建構具體 PRESERVE 指令
    zoning:      zoning.compute_zoning() 結果，僅 USE_NANO_BANANA=1 時使用
    customer_notes / budget_tier: Phase A 帶入 Nano Banana prompt（仍只 USE_NANO_BANANA=1 時生效）
    retry_context: C2.3 第二次 retry 用，含前次 sofa_pct / anchor_pct，附加進 prompt
    """
    use_nano = os.environ.get("USE_NANO_BANANA", "0").strip() == "1"

    print(f"\n{'='*56}")
    if use_nano:
        print("[Step 3] Nano Banana Pro 生成渲染圖（USE_NANO_BANANA=1）")
    else:
        print("[Step 3] Flux Kontext Pro 生成渲染圖")
    print(f"{'='*56}")

    os.makedirs(output_dir, exist_ok=True)

    if isinstance(image_paths, str):
        image_paths = [image_paths]

    def _to_data_url(path: str) -> str:
        ext = Path(path).suffix.lower()
        mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:{mime};base64,{b64}"

    img_urls = [_to_data_url(p) for p in image_paths]
    preserve_clause = _build_preserve_clause(analysis, design_mode=design_mode)
    # furnish 模式 guidance_scale 更低（更聽原圖），full 模式稍高
    guidance = 3.0 if design_mode == "furnish" else 4.0
    print(f"  渲染基底：{len(img_urls)} 張角度，design_mode={design_mode}, guidance={guidance}")
    print(f"  PRESERVE 指令: {preserve_clause[:160]}...")

    results = []
    for idx, render in enumerate(enriched_renders):
        style = render.get("style", "unknown")
        label = render.get("style_label", style)
        flux_prompt = render.get("flux_prompt", "")
        base_image_url = img_urls[idx % len(img_urls)]
        print(f"  風格 {idx+1} ({label}) 用角度: {Path(image_paths[idx % len(img_urls)]).name}")

        # 家具描述（家具產品圖暫不直接傳，因為 multi endpoint 會做合成而非參考）
        furniture_items = render.get("matched_furniture", [])[:3]
        furniture_desc = ", ".join(
            item.get("flux_descriptor", "") for item in furniture_items
            if item.get("flux_descriptor")
        )

        full_prompt = f"{flux_prompt}, {furniture_desc}" if furniture_desc else flux_prompt

        final_prompt = (
            preserve_clause + " "
            f"Apply this interior design style ONLY to surfaces/furniture/lighting (do not modify architecture): {full_prompt}"
        )

        print(f"\n  生成【{label}】...")
        print(f"  Prompt 結尾: ...{full_prompt[-80:]}")

        t0 = time.time()

        # ── USE_NANO_BANANA=1：multi-image edit 分支 ──
        if use_nano:
            inputs = build_nano_banana_inputs(render, zoning, base_image_url,
                                              customer_notes=customer_notes,
                                              budget_tier=budget_tier,
                                              retry_context=retry_context)
            print(f"  Nano Banana refs: {len(inputs['image_urls'])} 張 "
                  f"(prompt {len(inputs['prompt'])} chars)")
            try:
                result = fal_client.subscribe(
                    "fal-ai/nano-banana-pro/edit",
                    arguments={
                        "image_urls": inputs["image_urls"],
                        "prompt": inputs["prompt"],
                        "system_prompt": inputs["system_prompt"],
                        "resolution": "1K",
                        "output_format": "png",
                    },
                    with_logs=False,
                )
                elapsed = time.time() - t0
                img_url = (result.get("images") or [{}])[0].get("url")
                if not img_url:
                    raise ValueError(f"nano-banana-pro/edit 未回傳 URL，result keys: {list(result.keys())}")
                resp = requests.get(img_url, timeout=120)
                resp.raise_for_status()
                out_path = os.path.join(output_dir, f"render_{style}.png")
                with open(out_path, "wb") as f:
                    f.write(resp.content)

                print(f"  ✓ 完成 ({elapsed:.1f}s) → {out_path}")
                results.append({
                    **render,
                    "render_path": out_path,
                    "reference_map": inputs["reference_map"],
                    "notes": inputs["notes"],
                    "unmatched_visual_items": inputs["unmatched_visual_items"],
                    "pipeline_version": "nano-banana-v1",
                })
            except Exception as e:
                # 失敗：不自動 fallback Flux，直接標記 failed
                print(f"  ✗ Nano Banana 失敗: {e}")
                results.append({
                    **render,
                    "render_path": None,
                    "error": str(e),
                    "reference_map": inputs.get("reference_map", []),
                    "notes": inputs.get("notes", ""),
                    "unmatched_visual_items": inputs.get("unmatched_visual_items", []),
                    "pipeline_version": "nano-banana-v1",
                })
            continue   # 跳過底下的 Flux 分支

        try:
            # ── 正式 endpoint（鎖定 kontext，POC 結論 2026-06-03）──
            # kontext/max、ControlNet Canny/Depth/Depth+Canny 經 POC 比較後
            # 都未達可用門檻或無明顯收益，僅保留在 backend/poc_*.py 作未來研究。
            # 不要在沒有新 POC 證明前切換。
            result = fal_client.subscribe(
                "fal-ai/flux-pro/kontext",
                arguments={
                    "image_url": base_image_url,
                    "prompt": final_prompt,
                    "guidance_scale": guidance,
                    "num_inference_steps": 40,
                    "output_format": "jpeg",
                    "safety_tolerance": "5",
                },
                with_logs=False,
            )
            elapsed = time.time() - t0

            img_url = (result.get("images") or [{}])[0].get("url")
            if not img_url:
                raise ValueError(f"fal.ai 未回傳圖片 URL，result keys: {list(result.keys())}")
            resp = requests.get(img_url, timeout=60)
            resp.raise_for_status()
            out_path = os.path.join(output_dir, f"render_{style}.jpg")
            with open(out_path, "wb") as f:
                f.write(resp.content)

            print(f"  ✓ 完成 ({elapsed:.1f}s) → {out_path}")
            results.append({**render, "render_path": out_path, "pipeline_version": "flux-v1"})

        except Exception as e:
            print(f"  ✗ 失敗: {e}")
            results.append({**render, "render_path": None, "error": str(e), "pipeline_version": "flux-v1"})

    return results


# ─── 主程式 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
用法（單張）：python3.11 test_full_pipeline.py room.jpg [風格1,風格2]
用法（多張）：python3.11 test_full_pipeline.py room.jpg room2.jpg room3.jpg [風格1,風格2]
  第1張：主視角（Flux 渲染基底，選最廣角那張）
  第2-4張：補充角度（只給 Gemini 看，不渲染）
  最後一個參數若包含逗號則視為風格指定，否則視為額外照片
""")
    if len(sys.argv) < 2:
        sys.exit(1)

    # 解析參數：把含逗號的參數視為風格，其餘視為照片路徑
    image_paths = []
    user_styles = None
    for arg in sys.argv[1:]:
        if "," in arg or arg in VALID_STYLES:
            user_styles = arg.split(",")
        else:
            image_paths.append(arg)

    if not image_paths:
        print("請至少提供一張照片")
        sys.exit(1)

    image_path = image_paths[0]
    extra_photos = [p for p in image_paths[1:] if Path(p).exists()]

    if not Path(image_path).exists():
        print(f"找不到照片：{image_path}")
        sys.exit(1)

    if extra_photos:
        print(f"✓ 主照片：{image_path}")
        print(f"✓ 補充角度：{extra_photos}")
    else:
        print(f"✓ 單張模式：{image_path}")

    total_start = time.time()

    # Step 1
    analysis = analyze_image(image_path, user_styles, extra_photos=extra_photos or None)

    # Step 2
    enriched = enrich_renders(analysis.get("renders", []), analysis=analysis)
    show_furniture(enriched)

    # Step 3
    final = generate_renders(image_path, enriched)

    # 儲存完整結果
    os.makedirs("output", exist_ok=True)
    result_path = "output/test_result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({
            "analysis": analysis,
            "renders": final,
        }, f, ensure_ascii=False, indent=2)

    total = time.time() - total_start
    print(f"\n{'='*56}")
    print(f"完成！總耗時 {total:.1f}s")
    print(f"渲染圖 → output/render_*.jpg")
    print(f"完整結果 → {result_path}")
    print(f"{'='*56}")

"""
完整 pipeline 測試腳本
用法：python3.11 test_full_pipeline.py <房間照片路徑> [風格1,風格2]
範例：python3.11 test_full_pipeline.py room.jpg modern,nordic

不需要影片，一張房間照片就能測試完整流程：
  Gemini 看圖分析 → 家具配對 → Flux 生成渲染圖
"""
import os, sys, json, base64, time, io, re
from pathlib import Path

# pytest 收集會 import 本檔（檔名 test_ 開頭但其實是 pipeline 核心）；
# 在 pytest 底下換掉 stdout 會弄壞它的輸出捕捉（ValueError: I/O operation on closed file）
if "pytest" not in sys.modules and hasattr(sys.stdout, "buffer"):
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
from prompt_builder import build_nano_banana_inputs, build_anchored_inputs
import fal_client
import requests


# ═════════════════════════════════════════════════════════════════════════
# C2.6 生成可靠性安全鎖 — fal call bounded timeout + structured logging
# ═════════════════════════════════════════════════════════════════════════
class FalGenerationTimeout(Exception):
    """fal subscribe / queue / generation / download 總耗時超過 total_timeout"""
    pass


class FalResultDownloadError(Exception):
    """fal subscribe 已成功回傳, 但 image URL 缺失或下載失敗 (非超時類)"""
    pass


# 秒, 涵蓋 queue + 生成 + image download。Nano Banana Pro 在尖峰常 >180s，
# 全室一次打 8 張時逾時機率被放大→整批掉圖。拉高到 300 並可用環境變數 FAL_TIMEOUT 覆蓋。
try:
    DEFAULT_FAL_TIMEOUT = int(os.environ.get("FAL_TIMEOUT", "300"))
except (TypeError, ValueError):
    DEFAULT_FAL_TIMEOUT = 300


def _mask_uid_for_log(uid: str) -> str:
    """遮罩 upload_id (不是 job_id) 給 log 使用"""
    if not uid:
        return ""
    u = uid.strip()
    if len(u) < 5:
        return "*" * len(u)
    return f"{u[:2]}**{u[-3:]}"


def _emit_fal_log(outcome: str, **fields):
    """
    Structured single-line log for fal call lifecycle.

    outcome: started / queued / success / timeout / download_error / exception
    建議含: job_id, render_mode, style, render_index, attempt, stage
    選填:   upload_id_masked, fal_request_id, elapsed_seconds, error_type

    嚴禁傳完整 upload_id、API key、image URL query secret 或客戶資料。
    """
    parts = [f"outcome={outcome}"]
    for k in ("job_id", "upload_id_masked", "render_mode", "style",
              "render_index", "attempt", "stage", "fal_request_id",
              "elapsed_seconds", "error_type"):
        v = fields.get(k)
        if v is not None and v != "":
            parts.append(f"{k}={v}")
    print("[fal] " + " ".join(parts))


def _fal_subscribe_timed(model: str, arguments: dict, *,
                         total_timeout: int = DEFAULT_FAL_TIMEOUT,
                         log_ctx: dict) -> tuple[dict, bytes]:
    """
    包裝 fal_client.subscribe + image download, 單次硬性 deadline = total_timeout 秒.

    log_ctx 必含: job_id, render_mode, style, render_index, attempt, stage
                 (建議含 upload_id_masked)

    成功 → return (fal_result_dict, image_bytes)
    fal 超時 / 總 deadline 到 → raise FalGenerationTimeout
    image URL 缺失 / 下載 (非超時) 失敗 → raise FalResultDownloadError
    其他非預期例外 → 原樣 raise
    """
    deadline = time.time() + total_timeout
    request_id_box = {"id": None}

    _emit_fal_log("started", **log_ctx)

    def on_enq(rid: str):
        request_id_box["id"] = rid
        _emit_fal_log("queued", fal_request_id=rid, **log_ctx)

    t0 = time.time()
    # ── Phase 1: queue + generation ──
    try:
        gen_remaining = max(1.0, deadline - time.time())
        result = fal_client.subscribe(
            model,
            arguments=arguments,
            with_logs=False,
            on_enqueue=on_enq,
            client_timeout=gen_remaining,
        )
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        type_name = type(e).__name__
        # 判 timeout: 例外類名含 timeout / 已耗 >= 95% total_timeout
        is_timeout = ("timeout" in type_name.lower()) or (elapsed >= total_timeout * 0.95)
        if is_timeout:
            _emit_fal_log("timeout", fal_request_id=request_id_box["id"],
                          elapsed_seconds=elapsed, error_type=type_name, **log_ctx)
            raise FalGenerationTimeout(
                f"fal generation exceeded {total_timeout}s ({type_name})"
            ) from e
        _emit_fal_log("exception", fal_request_id=request_id_box["id"],
                      elapsed_seconds=elapsed, error_type=type_name, **log_ctx)
        raise

    # ── Phase 2: extract image URL ──
    img_url = (result.get("images") or [{}])[0].get("url")
    if not img_url:
        elapsed = round(time.time() - t0, 1)
        _emit_fal_log("download_error", fal_request_id=request_id_box["id"],
                      elapsed_seconds=elapsed, error_type="NoImageURL", **log_ctx)
        raise FalResultDownloadError(
            f"fal result no image URL; result keys={list(result.keys())}"
        )

    # ── Phase 3: image download (同一 deadline) ──
    dl_remaining = deadline - time.time()
    if dl_remaining <= 0:
        elapsed = round(time.time() - t0, 1)
        _emit_fal_log("timeout", fal_request_id=request_id_box["id"],
                      elapsed_seconds=elapsed, error_type="DeadlineReached", **log_ctx)
        raise FalGenerationTimeout(
            f"fal total deadline {total_timeout}s reached before image download"
        )

    try:
        resp = requests.get(img_url, timeout=min(60.0, dl_remaining))
        resp.raise_for_status()
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        type_name = type(e).__name__
        # 下載期間 timeout 也算總 deadline overflow
        if elapsed >= total_timeout * 0.95:
            _emit_fal_log("timeout", fal_request_id=request_id_box["id"],
                          elapsed_seconds=elapsed, error_type=type_name, **log_ctx)
            raise FalGenerationTimeout(
                f"fal image download caused total deadline overflow ({type_name})"
            ) from e
        _emit_fal_log("download_error", fal_request_id=request_id_box["id"],
                      elapsed_seconds=elapsed, error_type=type_name, **log_ctx)
        raise FalResultDownloadError(
            f"fal image download failed: {type_name}"
        ) from e

    elapsed = round(time.time() - t0, 1)
    _emit_fal_log("success", fal_request_id=request_id_box["id"],
                  elapsed_seconds=elapsed, **log_ctx)
    return result, resp.content


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
                  video_path: str | None = None,
                  photo_meta_list: list | None = None,
                  user_notes: str = "") -> dict:
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
        # 上限 3：全室方案基本 1 種 + 最多加購 2 種（單一空間前端只送 2 種）
        fixed_styles = user_styles[:3]
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
            _notes_clause = ""
            if (user_notes or "").strip():
                _notes_clause = (
                    f" 【屋主明確指定—最高優先，必須遵守】「{user_notes.strip()[:200]}」："
                    "屋主說中間/某處是餐廳，就切出獨立『dining』region；說當書房就切『study』，不要併進 living。"
                )
            regions_rule = (
                "regions[] 必須恰好 3 個，3 個 room_type 必須**全部不同**（優先 living/dining/bedroom）。"
                "開放式客餐廳要拆成 living + dining 兩個不同 region，不要兩個都標 living。"
                "每個 region 帶 name（中文，例如『客廳』『餐廳』『主臥室』）、best_photo_index、room_type、angle_label（可空）。"
                "如果可分類的不同房間少於 3 種，regions 仍輸出可用的（< 3 個也行），不要硬補。"
                + _notes_clause
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
        "**另外**你會看到「**1 段用戶上傳的影片**」。影片是**全屋空間結構的最高權威理解材料**"
        "——靜態照片只有零散視角（且可能拍得歪、廣角變形），影片才有連續的空間脈絡。"
        "你**必須主動從影片提取**以下資訊（不是可有可無的參考）：\n"
        "  - **房間連接與方向**：玄關→客廳→餐廳→各臥室怎麼走、每個門開在哪面牆、"
        "走道在哪一側——這些必須體現在你的空間描述與 regions 判斷\n"
        "  - **實際尺寸感**：用鏡頭走動的距離與轉角當比例線索，校正 room_dimensions / "
        "estimated_size（影片證據優先於單張照片的目測）\n"
        "  - **照片歸屬**：每張靜態照片對應影片中的哪個房間、哪個朝向——照片分類"
        "（photo_classifications / room_type）一律以影片證據為準\n"
        "  - **照片拍不到的死角**：窗的位置、樑柱、開口寬度，補進空間理解\n"
        "  - **修正照片誤導**：單張廣角照造成的比例/方向誤判，以影片的連續視角為準\n"
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

    # PhotoMeta v1 Step 2: 用戶在前端逐張指定「想設計哪一區 + 目標區域位置」.
    # 把它整理成 brief block 給 Gemini, 當分類 / best_photo / regions 的 ground truth.
    # photo_meta_list 對齊 all_paths (index 一致). 沒填 → 整段不出現, 等於現況行為.
    photo_meta_note = ""
    if photo_meta_list and any(isinstance(x, dict) for x in photo_meta_list):
        _PM_ZONE_ZH = {
            "living": "客廳", "dining": "餐廳", "bedroom": "臥室",
            "study": "書房", "kitchen": "廚房", "balcony": "陽台",
            "entrance": "玄關", "walkway": "走道", "other": "指定區",
        }
        _PM_HINT_ZH = {
            "rear_near_window":    "靠窗深處後段",
            "front_near_entrance": "入口前段",
            "left_side":           "左側",
            "right_side":          "右側",
            "center":              "中段",
            "unspecified":         "未指定",
        }
        _lines = []
        _note_lines = []
        for _i, _pm in enumerate(photo_meta_list):
            if not isinstance(_pm, dict):
                continue
            _tz = _pm.get("target_zone")
            _lh = _pm.get("target_location_hint")
            # photo_contains (Step 2 補完): 同一張照片包含的所有空間 (多選)
            _contains = _pm.get("photo_contains")
            if isinstance(_contains, list) and _contains:
                _contains_zh = "、".join(_PM_ZONE_ZH.get(z, z) for z in _contains)
            else:
                _contains_zh = ""
            # target_note (Step 2 補完): 用戶自由文字, ≤100 字, 結構化欄位之輔助
            _note = (_pm.get("target_note") or "").strip()
            if not _tz and not _lh and not _contains_zh and not _note:
                continue
            _zh_z = _PM_ZONE_ZH.get(_tz, _tz or "?")
            _zh_h = _PM_HINT_ZH.get(_lh, _lh or "?")
            _line = f"  - 第 {_i} 張："
            if _contains_zh:
                _line += f"照片含【{_contains_zh}】；"
            _line += f"想設計【{_zh_z}】，目標區域大約在【{_zh_h}】"
            _lines.append(_line)
            if _note:
                # 防 prompt injection: escape triple-backtick + 截到 100 字
                _safe_note = _note.replace("```", "'''")[:100]
                _note_lines.append(f"  - 第 {_i} 張補充說明：{_safe_note}")
        if _lines:
            photo_meta_note = (
                "\n【用戶逐張指定的設計區域 (最高優先 ground truth)】\n"
                + "\n".join(_lines) + "\n"
                "這些 user-explicit 指定**比你看到的家具/格局還重要**。"
                "如果用戶說『這張照片設計客廳』，就算這張照片裡同時看到餐廳/走道，"
                "best_photo_index 與 regions[] 仍應把這張照片歸到用戶指定的 zone。"
                "photo_contains 是用戶明確標註該張照片包含的所有空間 (多選), "
                "請以此為照片內含空間的 ground truth, 不要排除其中任何一個."
                "target_location_hint 不必寫進回傳 JSON，但會影響後續 render layout。\n"
            )
            if _note_lines:
                photo_meta_note += (
                    "\n【用戶補充說明 (輔助理解, 不可覆蓋上方結構化指定)】\n"
                    + "\n".join(_note_lines) + "\n"
                    "優先順序: photo_contains / target_zone / target_location_hint > 補充說明 > "
                    "你自己看照片推論. 若補充說明與結構化欄位衝突, 以結構化欄位為準.\n"
                )

    prompt = f"""
{photo_count_note}
{photo_meta_note}
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

def _source_dims(path: str) -> tuple[int, int] | None:
    """讀取 source 照片的 (width, height); PIL 缺席或檔案壞掉時回 None
    (caller 會 fallback 到 aspect_ratio="auto")"""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            return im.size
    except Exception:
        return None


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
        # 「keep pipes」曾被模型過度發揮成「多畫一堆管子」（3ACB0DF4 北歐客廳天花憑空
        # 多出成排管線）——明確講清楚：同樣數量、同樣位置，不准新增。
        parts.append(f"ceiling: {feats['ceiling']} — keep the EXACT SAME pipes/sprinklers/beams "
                     "as the source photo (same count, same positions); do NOT add, duplicate or "
                     "invent any extra pipes, conduits or tracks;")
    if feats.get("floor"):
        parts.append(f"floor: {feats['floor']};")
    if feats.get("walls"):
        parts.append(f"walls: {feats['walls']};")

    if design_mode == "furnish":
        # 天花板「照原樣」雙向講清楚：原照有 cove/嵌燈 → 原封保留（不是拆掉）；
        # 原照沒有 → 不准新增。新加的燈光只能來自「擺得上去的燈具」。
        # 另：家具必須腳著地（曾出現懸浮床＋床底燈帶的物理不可能家具）。
        parts.append(
            "MODE: furniture-only restyle. DO NOT modify walls, ceiling, doors, windows, floor finish — "
            "keep them EXACTLY as the source photo: if the photo already has cove lighting, recessed "
            "lights or moldings, KEEP them unchanged; if it does not, do NOT add any. "
            "ONLY change movable furniture, soft furnishings (rugs, curtains, cushions), decor objects, "
            "and lighting mood — any NEW light source must be a PORTABLE fixture (table lamp, floor lamp, "
            "plug-in pendant); never build new recessed lights, LED strips or lighting troughs into the "
            "room itself. Do NOT invent floating furniture or under-furniture glow — UNLESS a referenced "
            "catalog product is designed that way (e.g. a floating bed frame with built-in light strip): "
            "then render that product faithfully as its product image shows."
        )
    else:
        parts.append(
            "MODE: interior refinish. Wall/ceiling surface finish may be updated but DO NOT add structural elements. "
            "ONLY change surface finishes, furniture, decor, lighting mood."
        )

    return anti_halluc + " ".join(parts)


def _extract_failed_image_urls(err_text: str) -> list[str]:
    """從 fal file_download_error 訊息抽出抓不到的 image URL。
    例：[{... 'type': 'file_download_error', 'input': 'https://img.pchome.com.tw/...'}]"""
    if not err_text or "file_download_error" not in err_text:
        return []
    urls = re.findall(r"['\"]input['\"]\s*:\s*['\"](https?://[^'\"]+)['\"]", err_text)
    return list(dict.fromkeys(urls))  # 去重保序


def _save_render_jpg(img_bytes: bytes, out_path: str) -> None:
    """渲染圖統一存 JPEG（q88）：fal 回傳的 PNG 一張 5-10MB，是 Supabase 儲存爆量
    主因（2026-07 超額被停權事故）；JPEG 體積縮 ~90%、視覺無差。
    解不開就原樣寫檔——寧可檔案大，也不能讓渲染流程掛掉。"""
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.save(out_path, "JPEG", quality=88)
    except Exception:
        with open(out_path, "wb") as f:
            f.write(img_bytes)


def generate_renders(image_paths, enriched_renders: list[dict], output_dir: str = "output",
                     analysis: dict | None = None, design_mode: str = "furnish",
                     zoning: dict | None = None,
                     customer_notes: str = "",
                     budget_tier: str = "tier3",
                     retry_context: dict | None = None,
                     force_anchored: bool = False,
                     job_id: str = "",
                     upload_id_masked: str = "",
                     attempt: int = 1,
                     stage: str = "initial",
                     # PhotoMeta v1 Step 2: 使用者明確指定的設計目標 + 位置
                     target_zone: str | None = None,
                     target_location_hint: str | None = None,
                     # PhotoMeta v1 Step 2 補完: 使用者自由文字補充說明 (≤100 字, optional)
                     target_note: str | None = None,
                     # step-2: 此張渲染對應的標準房型（living/bedroom/dining/study）；
                     # 給 prompt_builder 依房型佈置（臥室擺床不擺沙發）。預設 living = 原行為。
                     room_type: str = "living"):
    """
    image_paths: 單一路徑或 list；多張時每個 style 輪流用不同角度
    analysis:    Gemini 分析結果，用來建構具體 PRESERVE 指令
    zoning:      zoning.compute_zoning() 結果，僅 USE_NANO_BANANA=1 時使用
    customer_notes / budget_tier: Phase A 帶入 Nano Banana prompt（仍只 USE_NANO_BANANA=1 時生效）
    retry_context: C2.3 第二次 retry 用，含前次 sofa_pct / anchor_pct，附加進 prompt
    """
    use_nano = os.environ.get("USE_NANO_BANANA", "0").strip() == "1"
    use_anchored = use_nano and (
        force_anchored
        or os.environ.get("USE_ANCHORED_MODE", "0").strip() == "1"
    )

    print(f"\n{'='*56}")
    if use_anchored:
        print("[Step 3] Nano Banana Pro ANCHORED MODE（USE_NANO_BANANA=1, USE_ANCHORED_MODE=1）")
    elif use_nano:
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
    # 影片口述需求（analysis.owner_requests）併進 customer_notes，一起進 render prompt。
    # 讓「全案備註 + 每張 target_note + 影片口述」三者都被模型看到，不讓影片優勢斷掉。
    _owner_req = ((analysis or {}).get("owner_requests") or "").strip()
    if _owner_req and _owner_req not in ("未提及", "無"):
        customer_notes = (customer_notes + " 屋主口述需求：" + _owner_req).strip()
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
            # ── USE_ANCHORED_MODE=1：D' 驗證過的 anchored 配方 ──
            if use_anchored:
                base_path = image_paths[idx % len(img_urls)]
                src_dims = _source_dims(base_path)
                a_inputs = build_anchored_inputs(
                    render, base_image_url, source_dims=src_dims,
                )
                print(f"  Nano Banana ANCHORED refs: {len(a_inputs['image_urls'])} 張 "
                      f"(prompt {len(a_inputs['prompt'])} chars, "
                      f"aspect_ratio={a_inputs['aspect_ratio']}, seed={a_inputs['seed']})")
                log_ctx = {
                    "job_id":           job_id,
                    "upload_id_masked": upload_id_masked,
                    "render_mode":      "anchored",
                    "style":            style,
                    "render_index":     idx,
                    "attempt":          attempt,
                    "stage":            stage,
                }
                try:
                    result, img_bytes = _fal_subscribe_timed(
                        "fal-ai/nano-banana-pro/edit",
                        {
                            "image_urls":     a_inputs["image_urls"],
                            "prompt":         a_inputs["prompt"],
                            "system_prompt":  a_inputs["system_prompt"],
                            "resolution":     a_inputs["resolution"],
                            "aspect_ratio":   a_inputs["aspect_ratio"],
                            "seed":           a_inputs["seed"],
                            "output_format":  a_inputs["output_format"],
                        },
                        log_ctx=log_ctx,
                    )
                    out_path = os.path.join(output_dir, f"render_{style}.jpg")
                    _save_render_jpg(img_bytes, out_path)
                    results.append({
                        **render,
                        "render_path": out_path,
                        "reference_map": a_inputs["reference_map"],
                        "notes": a_inputs["notes"],
                        "unmatched_visual_items": a_inputs["unmatched_visual_items"],
                        "pipeline_version": "nano-banana-anchored-v1",
                        "render_mode": "anchored",
                    })
                except (FalGenerationTimeout, FalResultDownloadError):
                    # C2.6 Patch A: 不吞 fal 明確例外; 讓 run_pipeline outer except 標 failed.
                    # _fal_subscribe_timed 已印 structured log, 不必重複.
                    raise
                except Exception as e:
                    results.append({
                        **render,
                        "render_path": None,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "reference_map": a_inputs.get("reference_map", []),
                        "notes": a_inputs.get("notes", ""),
                        "unmatched_visual_items": a_inputs.get("unmatched_visual_items", []),
                        "pipeline_version": "nano-banana-anchored-v1",
                        "render_mode": "anchored",
                    })
                continue   # 跳過底下的既有 nano-banana 與 Flux 分支

            # 跨房一致性：api.py 在 entry 上掛「同風格已完成的客廳成品圖」路徑
            _cons_url = None
            _cons_path = render.get("_consistency_ref_path")
            if _cons_path and os.path.exists(str(_cons_path)):
                try:
                    _cons_url = _to_data_url(str(_cons_path))
                except Exception as _ce:
                    print(f"  [consistency] 參考圖轉換失敗，略過: {_ce}")
            inputs = build_nano_banana_inputs(render, zoning, base_image_url,
                                              customer_notes=customer_notes,
                                              budget_tier=budget_tier,
                                              retry_context=retry_context,
                                              target_zone=target_zone,
                                              target_location_hint=target_location_hint,
                                              target_note=target_note,
                                              room_type=room_type,
                                              design_mode=design_mode,
                                              consistency_ref_url=_cons_url)
            print(f"  Nano Banana refs: {len(inputs['image_urls'])} 張 "
                  f"(prompt {len(inputs['prompt'])} chars)")
            log_ctx = {
                "job_id":           job_id,
                "upload_id_masked": upload_id_masked,
                "render_mode":      "legacy",
                "style":            style,
                "render_index":     idx,
                "attempt":          attempt,
                "stage":            stage,
            }
            # ── Model-specific payload (env-gated, 預設保守 Nano Banana) ──
            # RENDER_MODEL 預設 fal-ai/nano-banana-pro/edit (現況). 改為
            # openai/gpt-image-2/edit 啟用 GPT Image 2 medium 降成本.
            # 兩組 args 不同, 不能用同一份 payload 餵.
            render_model = os.environ.get("RENDER_MODEL", "fal-ai/nano-banana-pro/edit").strip()
            if render_model == "openai/gpt-image-2/edit":
                # gpt-image-2 傾向 auto zoom-in / 重構成 staged 室內攝影棚.
                # 補硬性 camera constraints 鎖原圖視角 + 廣角縱深 + 前景地板.
                # image_size=auto 明確設定 (即便目前已是預設, 防未來預設變動).
                _is_full = (design_mode or "furnish") == "full"
                camera_constraints = (
                    "Preserve the exact original camera position, wide-angle field of view, "
                    "framing, perspective, and foreground floor depth from the source photo. "
                    "Do not zoom in, crop, reframe, straighten into a staged interior photo, "
                    "or move the camera forward. "
                    "Keep the same amount of empty foreground floor visible as in the source image. "
                ) + (
                    # full：允許牆面/天花「表面飾材」改造，且可把外露消防/管線包進天花（裝潢常見），
                    # 但結構/開口/比例不變（不要與裝潢指令打架）
                    "You MAY refinish the wall surfaces (paint / simple wallpaper) and the ceiling "
                    "finish as instructed below, and you MAY conceal or box-in exposed ceiling "
                    "pipes / sprinkler conduits / surface wiring within the new ceiling treatment "
                    "(keep sprinkler heads visible if present). Keep the SAME room structure: wall "
                    "positions, wall openings, doorways, windows, floor direction and room "
                    "proportions unchanged. Do not rebuild it into a different room."
                    if _is_full else
                    # furnish：維持嚴格保留（只擺家具、不動任何表面）
                    "Add furniture into the existing room only; do not rebuild the room as a new "
                    "interior photoshoot. "
                    "Preserve existing ceiling pipes, lights, wall openings, doorways, windows, "
                    "wall seams, floor direction, and room proportions. Keep the SAME number and "
                    "layout of ceiling pipes/conduits as the source photo — never add or duplicate pipes."
                )
                # gpt-image-2/edit 沒有獨立 system_prompt 欄位 → 把 system_prompt 併進 prompt，
                # 否則所有硬規則（含 full 模式的「強制裝潢牆/天花」指令）都到不了模型。
                _sys = (inputs.get("system_prompt") or "").strip()
                fal_args = {
                    "image_urls":    inputs["image_urls"],
                    "prompt":        camera_constraints + " " + (_sys + " " if _sys else "") + inputs["prompt"],
                    "quality":       os.environ.get("GPT_IMAGE_2_QUALITY", "medium").strip(),
                    "output_format": "png",
                    "image_size":    "auto",
                }
            else:
                fal_args = {
                    "image_urls":    inputs["image_urls"],
                    "prompt":        inputs["prompt"],
                    "system_prompt": inputs["system_prompt"],
                    "resolution":    "1K",
                    "output_format": "png",
                }
            # fal 偶爾抓不到某張參考商品圖（例：PChome 圖）→ file_download_error，整張 render 失敗。
            # 對策：移除 fal 抓不到的參考圖（保留房間底圖）後重試一次，避免一張外部圖掛掉整個風格。
            attempt_args = fal_args
            _last_err = None
            for _try in range(2):
                try:
                    result, img_bytes = _fal_subscribe_timed(
                        render_model, attempt_args, log_ctx=log_ctx,
                    )
                    out_path = os.path.join(output_dir, f"render_{style}.jpg")
                    _save_render_jpg(img_bytes, out_path)
                    results.append({
                        **render,
                        "render_path": out_path,
                        "reference_map": inputs["reference_map"],
                        "notes": inputs["notes"],
                        "unmatched_visual_items": inputs["unmatched_visual_items"],
                        "pipeline_version": "nano-banana-v1",
                        "render_model": render_model,   # 實際用的模型（banana / gpt-image-2），方便 debug
                        "render_mode": "legacy",
                    })
                    _last_err = None
                    break
                except (FalGenerationTimeout, FalResultDownloadError):
                    # C2.6 Patch A: fal 明確例外不吞, 讓 outer except 標訂單 failed.
                    raise
                except Exception as e:
                    _last_err = e
                    bad = _extract_failed_image_urls(str(e))
                    keep = [u for u in attempt_args["image_urls"]
                            if u == base_image_url or u not in bad]
                    # 只有「確實移除了抓不到的參考圖、且仍保留房間底圖」才重試
                    if bad and len(keep) < len(attempt_args["image_urls"]) and base_image_url in keep:
                        print(f"  [render] fal 無法下載 {len(bad)} 張參考圖，移除後重試")
                        attempt_args = {**attempt_args, "image_urls": keep}
                        continue
                    break
            if _last_err is not None:
                # 重試後仍失敗：標記 failed（render_path=None），交付閘門會判為不可交付
                results.append({
                    **render,
                    "render_path": None,
                    "error": str(_last_err),
                    "error_type": type(_last_err).__name__,
                    "reference_map": inputs.get("reference_map", []),
                    "notes": inputs.get("notes", ""),
                    "unmatched_visual_items": inputs.get("unmatched_visual_items", []),
                    "pipeline_version": "nano-banana-v1",
                    "render_model": render_model,
                    "render_mode": "legacy",
                })
            continue   # 跳過底下的 Flux 分支

        # ── 正式 endpoint（鎖定 kontext，POC 結論 2026-06-03）──
        # kontext/max、ControlNet Canny/Depth/Depth+Canny 經 POC 比較後
        # 都未達可用門檻或無明顯收益，僅保留在 backend/poc_*.py 作未來研究。
        # 不要在沒有新 POC 證明前切換。
        log_ctx = {
            "job_id":           job_id,
            "upload_id_masked": upload_id_masked,
            "render_mode":      "legacy",
            "style":            style,
            "render_index":     idx,
            "attempt":          attempt,
            "stage":            stage,
        }
        try:
            result, img_bytes = _fal_subscribe_timed(
                "fal-ai/flux-pro/kontext",
                {
                    "image_url":           base_image_url,
                    "prompt":              final_prompt,
                    "guidance_scale":      guidance,
                    "num_inference_steps": 40,
                    "output_format":       "jpeg",
                    "safety_tolerance":    "5",
                },
                log_ctx=log_ctx,
            )
            out_path = os.path.join(output_dir, f"render_{style}.jpg")
            with open(out_path, "wb") as f:
                f.write(img_bytes)
            results.append({**render, "render_path": out_path, "pipeline_version": "flux-v1",
                            "render_mode": "legacy"})
        except (FalGenerationTimeout, FalResultDownloadError):
            # C2.6 Patch A: fal 明確例外不吞, 讓 outer except 標訂單 failed.
            raise
        except Exception as e:
            results.append({**render, "render_path": None, "error": str(e),
                            "error_type": type(e).__name__,
                            "pipeline_version": "flux-v1",
                            "render_mode": "legacy"})

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

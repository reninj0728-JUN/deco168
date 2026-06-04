"""
Nano Banana Pro multi-image prompt 組裝模組

提供：
    build_nano_banana_inputs(entry, zoning, room_image_url) -> dict

設計原則：
- 純函式、無 side effect
- 不打 fal、不寫 DB、不改 pipeline
- image_urls[0] 永遠是 room image
- 後面只放 matched_furniture 裡 category_en in (sofa, coffee_table, rug) 且有 image_url 的商品
- reference 編號動態建立（不硬寫 image 2/3/4）
- zoning 描述原文塞進 prompt，不 parse Wall A/B
- zoning confidence='none' 或缺失時退回無 zoning 版本
"""

# 主家具品類（必撈，跟 furniture_match.py 一致）
MUST_HAVE_CATS = ("sofa", "coffee_table", "rug")

# 品類顯示名稱（給 prompt 用）
CAT_DISPLAY = {
    "sofa":         ("SOFA",         "sofa"),
    "coffee_table": ("COFFEE TABLE", "coffee table"),
    "rug":          ("RUG",          "area rug"),
}

# 預設標示文字（result_json.notes 給 UI 用）
DEFAULT_NOTES = "主家具（沙發、茶几、地毯）為可購買商品；其餘物件為情境示意。"


SYSTEM_PROMPT = (
    "You are a strict interior staging assistant. Hard rules: "
    "1) PRESERVE the camera angle, framing, and perspective EXACTLY as in the ROOM reference image. "
    "2) PRESERVE all walls, the window, ceiling pipes/conduits/fixtures, and floor material EXACTLY. "
    "3) DO NOT add kitchen, sink, dining bar, extra doors, extra windows, wall paneling, "
    "marble walls, dropped ceiling, cove lights, LED strips, or arched openings. "
    "4) DO NOT fill, block, cover, plaster over, convert to a solid wall, or in any way remove "
    "any existing corridor opening, passage opening, doorway, or wall opening visible in the ROOM reference. "
    "Every existing wall opening MUST remain visible and unobstructed in the final image. "
    "5) USE PRODUCT REFERENCE IMAGES for furniture appearance — match each referenced product's "
    "color, material, form, and silhouette as closely as possible. "
    "6) ONLY add movable furniture, soft furnishings, decor, plants, and artwork — no structural changes."
)


def _is_zoning_usable(zoning: dict | None) -> bool:
    if not zoning or not isinstance(zoning, dict):
        return False
    if zoning.get("error"):
        return False
    conf = (zoning.get("confidence") or "").lower()
    return conf in ("high", "medium")


def _build_inputs_section(reference_map: list[dict]) -> str:
    """動態 enumerate 'Reference image N is X' 段落"""
    lines = ["INPUTS:"]
    for ref in reference_map:
        role = ref["role"]
        idx = ref["index"]
        if role == "ROOM":
            lines.append(f"Reference image {idx} is the ROOM (base scene to stage).")
        else:
            display = role  # already display form, e.g. SOFA / COFFEE TABLE / RUG
            lines.append(f"Reference image {idx} is the {display} PRODUCT (a real product to use).")
    return " ".join(lines)


def _build_layout_section(zoning: dict) -> str:
    """
    從 zoning 原文組裝 layout 描述（不 parse 牆名）。
    sofa_wall / tv_wall / living_zone.where / walkway / no_large_furniture_zones 全文塞進去。
    """
    syn = zoning.get("spatial_synthesis", {}) or {}
    zones = zoning.get("zones", {}) or {}
    rules = zoning.get("furniture_placement_rules", {}) or {}

    parts = ["ROOM LAYOUT (from spatial analysis of the room):"]

    if syn.get("room_shape"):
        parts.append(f"Room shape: {syn['room_shape']}.")
    if syn.get("main_window_wall"):
        win = syn.get("main_window_size", "") or ""
        parts.append(f"Window location: {syn['main_window_wall']}" + (f" ({win})." if win else "."))
    if syn.get("entrance_position"):
        parts.append(f"Entrance: {syn['entrance_position']}.")
    if syn.get("exposed_ceiling"):
        parts.append(f"Ceiling features (must preserve): {syn['exposed_ceiling']}.")

    walls = syn.get("wall_inventory") or []
    if walls:
        parts.append("Walls inventory:")
        for w in walls:
            name = w.get("name", "?")
            desc = w.get("description", "")
            opening = w.get("has_opening")
            opening_txt = "has opening" if opening else "fully solid"
            parts.append(f"- {name} [{opening_txt}]: {desc}")

    living = zones.get("living_zone", {}) or {}
    if living.get("where"):
        parts.append(f"Living zone: {living['where']}.")

    if rules.get("sofa_wall"):
        parts.append(f"Sofa wall rule: {rules['sofa_wall']}")
    if rules.get("tv_wall"):
        parts.append(f"TV/focal wall rule: {rules['tv_wall']}")
    if rules.get("coffee_table_position"):
        parts.append(f"Coffee table rule: {rules['coffee_table_position']}")
    if rules.get("rug_anchor"):
        parts.append(f"Rug anchor rule: {rules['rug_anchor']}")
    if rules.get("accent_chair_position"):
        parts.append(f"Accent chair rule: {rules['accent_chair_position']}")

    walkway = zones.get("walkway", {}) or {}
    if walkway.get("where"):
        parts.append(f"Walkway (must stay clear): {walkway['where']}.")

    no_go = rules.get("no_large_furniture_zones") or []
    if no_go:
        parts.append("NO-LARGE-FURNITURE zones (must remain unblocked):")
        for z in no_go:
            parts.append(f"- {z}")

    return " ".join(parts)


def _build_fallback_layout_section() -> str:
    """zoning 不可用時的退化版（純通則）"""
    return (
        "ROOM LAYOUT: "
        "Use the ROOM reference image to determine wall positions, window location, "
        "and entrance/corridor openings. "
        "Place the sofa against the longest solid wall (no openings). "
        "Keep the entrance area within 1.5m of the main door clear. "
        "Keep any corridor/passage opening unobstructed. "
        "Anchor the living conversation zone near the window for natural light."
    )


def _build_product_placement_section(reference_map: list[dict]) -> str:
    """根據 reference_map 裡的 sofa/coffee_table/rug 動態組裝家具擺位指令"""
    product_refs = [r for r in reference_map if r["role"] != "ROOM"]
    if not product_refs:
        return (
            "FURNITURE: Place a sofa, coffee table, and area rug appropriate to the style "
            "in the living zone. No product references provided — invent style-consistent items."
        )

    lines = ["FURNITURE PLACEMENT (must match product references for appearance):"]
    for r in product_refs:
        idx = r["index"]
        role_disp = r["role"]  # e.g. SOFA
        name = r.get("name_zh", "")
        cat_human = CAT_DISPLAY.get(r["cat_en"], (role_disp, role_disp.lower()))[1]
        lines.append(
            f"- {role_disp}: Place a {cat_human} matching the visual appearance of reference image {idx} "
            f"({name}). Match its color, material, form, and silhouette closely."
        )

    # 客廳通則家具擺位
    lines.append(
        "General placement: sofa against the designated sofa wall facing into the room; "
        "coffee table in front of the sofa; rug anchored under the coffee table within the living zone."
    )
    return " ".join(lines)


def _build_style_section(entry: dict) -> str:
    style_label = entry.get("style_label") or entry.get("style") or "interior style"
    flux_prompt = (entry.get("flux_prompt") or "").strip()
    parts = [f"STYLE: {style_label}."]
    if flux_prompt:
        parts.append(f"Style cues: {flux_prompt}.")
    parts.append(
        "May add complementary soft items (floor lamp, sheer curtains, accent chair, "
        "plants, artwork) consistent with the style — these are illustrative only "
        "and need not match any reference image."
    )
    return " ".join(parts)


QUALITY_TAIL = (
    "Camera angle and room proportions must match the ROOM reference EXACTLY. "
    "Photorealistic, 35mm wide-angle interior photography, editorial quality, UHD, "
    "no people, no text, no logos, no watermarks."
)

CRITICAL_RULES = (
    "CRITICAL: "
    "(a) Do not invent walls, doors, or windows that are not in the ROOM reference. "
    "(b) Do not transform the space into a kitchen or dining hall. "
    "(c) For each product reference, the corresponding item in the output must look like it "
    "in color, material, and silhouette — do not substitute a different-looking product. "
    "(d) Every visible corridor opening, passage opening, doorway, or wall opening in the ROOM "
    "reference MUST appear in the final image as an open, unobstructed opening. Filling these "
    "openings with a solid wall, plastering them over, or blocking them with furniture is forbidden."
)


def build_nano_banana_inputs(
    entry: dict,
    zoning: dict | None,
    room_image_url: str,
) -> dict:
    """
    組 Nano Banana Pro multi-image edit 所需的 prompt + image_urls。

    參數:
        entry: 單一 render entry，需含 style / style_label / matched_furniture[] / flux_prompt
        zoning: zoning.compute_zoning() 的回傳 dict（可為 None / 含 error）
        room_image_url: 已上傳到 fal 的 room image URL，或 data URL，或 Supabase public URL

    回傳:
        {
            "image_urls": [room_url, ...product_urls],
            "prompt": str,
            "system_prompt": str,
            "reference_map": [{"index", "role", "url", "cat_en", "name_zh", "id"}],
            "notes": str,
            "unmatched_visual_items": [],
        }
    """
    matched = entry.get("matched_furniture") or []

    # 過濾出 must_have 且有 image_url 的商品（順序固定 sofa → coffee_table → rug）
    selected: dict[str, dict] = {}
    for item in matched:
        cat = (item.get("category_en") or "").strip()
        url = (item.get("image_url") or "").strip()
        if cat in MUST_HAVE_CATS and url.startswith("http") and cat not in selected:
            selected[cat] = item

    # 構造 reference_map（index 1 = ROOM，固定）
    reference_map: list[dict] = [{
        "index": 1,
        "role": "ROOM",
        "url": room_image_url,
        "cat_en": None,
        "name_zh": None,
        "id": None,
    }]
    image_urls: list[str] = [room_image_url]

    next_idx = 2
    for cat in MUST_HAVE_CATS:
        if cat in selected:
            it = selected[cat]
            display_role, _ = CAT_DISPLAY[cat]
            reference_map.append({
                "index": next_idx,
                "role": display_role,
                "url": it.get("image_url"),
                "cat_en": cat,
                "name_zh": it.get("name_zh", ""),
                "id": it.get("id", ""),
            })
            image_urls.append(it.get("image_url"))
            next_idx += 1

    # 組 prompt 段落
    inputs_sec = _build_inputs_section(reference_map)

    if _is_zoning_usable(zoning):
        layout_sec = _build_layout_section(zoning)
    else:
        layout_sec = _build_fallback_layout_section()

    product_sec = _build_product_placement_section(reference_map)
    style_sec = _build_style_section(entry)

    prompt = "\n\n".join([
        inputs_sec,
        layout_sec,
        product_sec,
        style_sec,
        CRITICAL_RULES,
        QUALITY_TAIL,
    ])

    return {
        "image_urls": image_urls,
        "prompt": prompt,
        "system_prompt": SYSTEM_PROMPT,
        "reference_map": reference_map,
        "notes": DEFAULT_NOTES,
        "unmatched_visual_items": [],
    }

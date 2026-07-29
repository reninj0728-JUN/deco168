from pathlib import Path

import api


def _zoning(side_source: str | None) -> dict:
    living = {
        "where": "客廳深處",
        "rationale": "AI 建議沙發靠右牆",
        "sofa_side": "right",
        "tv_side": "left",
        "sofa_side_confidence": "high",
    }
    if side_source is not None:
        living["sofa_side_source"] = side_source
    return {
        "overall_confidence": "high",
        "spatial_synthesis": {"room_shape": "狹長型客廳"},
        "existing_zones": {},
        "proposed_zones": {"living_zone": living},
    }


def test_ai_default_side_is_a_recommendation_not_a_hard_binding():
    flattened = api.flatten_zoning_v2_to_v1(_zoning("ai_default"), "A")
    rules = flattened["furniture_placement_rules"]

    assert rules["sofa_side"] == ""
    assert rules["tv_side"] == ""
    assert rules["recommended_sofa_side"] == "right"
    assert rules["recommended_tv_side"] == "left"
    assert flattened["_sofa_layout"] == "free"
    assert api._guide_sofa_side(flattened) == "free"


def test_explicit_user_side_and_legacy_orders_remain_bound():
    explicit = api.flatten_zoning_v2_to_v1(_zoning("user_explicit"), "A")
    legacy = api.flatten_zoning_v2_to_v1(_zoning(None), "A")

    for flattened in (explicit, legacy):
        rules = flattened["furniture_placement_rules"]
        assert rules["sofa_side"] == "right"
        assert rules["tv_side"] == "left"
        assert "_sofa_layout" not in flattened
        assert api._guide_sofa_side(flattened) == "right"


def test_zoning_page_only_marks_an_actual_click_as_explicit():
    html = (Path(__file__).resolve().parent.parent / "zoning-confirm.html").read_text(
        encoding="utf-8"
    )

    assert "setSofaSide(aiSide, 'ai_default')" in html
    assert "setSofaSide('left', 'user_explicit')" in html
    assert "setSofaSide('right', 'user_explicit')" in html
    assert "lz.sofa_side_source = currentSofaSideSource" in html
    assert "currentSofaSideSource === 'user_explicit'" in html

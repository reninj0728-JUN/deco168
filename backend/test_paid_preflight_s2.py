# -*- coding: utf-8 -*-
"""S2 must fail before any paid renderer call."""
from __future__ import annotations

import hashlib
import inspect

import pytest
import test_full_pipeline as pipeline
from test_layout_preflight_s2 import _artifacts


def test_s2_preflight_guard_is_positioned_before_paid_subscribe():
    source = inspect.getsource(pipeline.generate_renders)
    assert source.index("_enforce_s2_paid_preflight") < source.index("_fal_subscribe_timed")


def test_s2_global_flag_blocks_living_render_when_metadata_is_missing(tmp_path, monkeypatch):
    source = tmp_path / "room.jpg"
    source.write_bytes(b"paid call must never see this")
    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "1")

    with pytest.raises(pipeline.LayoutPreflightBlocked):
        pipeline._enforce_s2_paid_preflight(str(source), {}, "living")

    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "0")
    result = pipeline._enforce_s2_paid_preflight(str(source), {}, "living")
    assert result["skipped"] is True


def test_s2_required_forces_multi_image_gpt_image2_mode(monkeypatch):
    monkeypatch.setenv("USE_NANO_BANANA", "0")
    monkeypatch.setenv("USE_ANCHORED_MODE", "0")
    monkeypatch.setenv("RENDER_MODEL", "fal-ai/nano-banana-pro/edit")

    assert pipeline._resolve_generation_mode(
        [{"_layout_contract_s2_required": True}], force_anchored=False,
    ) == (True, False)
    assert pipeline._resolve_render_model(
        {"_layout_contract_s2_required": True},
        override="fal-ai/nano-banana-pro/edit",
    ) == "openai/gpt-image-2/edit"
    assert pipeline._resolve_generation_mode(
        [{"_layout_contract_s2_required": False}], force_anchored=False,
    ) == (False, False)


def test_s2_required_render_cannot_bypass_preflight_via_room_type(tmp_path, monkeypatch):
    source = tmp_path / "room.jpg"
    source.write_bytes(b"paid call must never see this")
    monkeypatch.setenv("LAYOUT_CONTRACT_S2", "1")
    render = {"_layout_contract_s2_required": True}

    with pytest.raises(pipeline.LayoutPreflightBlocked):
        pipeline._enforce_s2_paid_preflight(str(source), render, "bedroom")


def test_missing_s2_artifacts_block_living_render_before_paid_call(tmp_path):
    source = tmp_path / "room.jpg"
    source.write_bytes(b"not relevant because artifacts are missing")
    render = {
        "_layout_contract_s2_required": True,
        "_layout_contract_s2": None,
        "_layout_guide": None,
        "_layout_reconciliation_s2": None,
    }

    with pytest.raises(pipeline.LayoutPreflightBlocked):
        pipeline._enforce_s2_paid_preflight(str(source), render, "living")


def test_valid_s2_artifacts_pass_paid_preflight(tmp_path):
    photo, _, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)
    render = {
        "_layout_contract_s2_required": True,
        "_layout_contract_s2": str(contract_path),
        "_layout_contract_s2_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "_layout_guide": guide["path"],
        "_layout_geometry_verification_s2": str(verification_path),
        "_layout_geometry_verification_s2_sha256": hashlib.sha256(
            verification_path.read_bytes()
        ).hexdigest(),
        "_layout_reconciliation_s2": str(report_path),
        "_layout_reconciliation_s2_sha256": hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest(),
        "_layout_guide_s2_sha256": guide["sha256"],
        "_room_type": "living",
    }

    result = pipeline._enforce_s2_paid_preflight(str(photo), render, "living")

    assert result["ok"] is True


def test_s2_local_edit_preflight_stays_bound_to_original_source(tmp_path):
    photo, _, contract_path, guide, verification_path, _, report_path = _artifacts(tmp_path)
    previous_render = tmp_path / "previous-render.jpg"
    previous_render.write_bytes(b"furnished edit base")
    render = {
        "_layout_contract_s2_required": True,
        "_base_path": str(photo),
        "_layout_contract_s2": str(contract_path),
        "_layout_contract_s2_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "_layout_guide": guide["path"],
        "_layout_guide_s2_sha256": guide["sha256"],
        "_layout_geometry_verification_s2": str(verification_path),
        "_layout_geometry_verification_s2_sha256": hashlib.sha256(
            verification_path.read_bytes()
        ).hexdigest(),
        "_layout_reconciliation_s2": str(report_path),
        "_layout_reconciliation_s2_sha256": hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest(),
        "_room_type": "living",
    }

    result = pipeline._enforce_s2_paid_preflight(
        str(previous_render), render, "living",
    )

    assert result["ok"] is True

import pytest

from core.fire_fusion import (
    FireFusion,
    FIRE_CONFIRMED,
    FIRE_IR_ONLY,
    NO_FIRE,
)


def test_fire_fusion_requires_ir_hotspot():
    fusion = FireFusion(ir_size=(160, 120), rgb_size=(960, 540))
    eo_only = [(0, 0, 100, 100, 0.9)]
    res = fusion.fuse([], eo_only)

    assert res["fire_detected"] is False
    assert res["status"] == NO_FIRE
    assert res["confirmed_count"] == 0
    assert res["ir_only_count"] == 0


def test_fire_fusion_ir_only_sets_ir_status():
    fusion = FireFusion(ir_size=(160, 120), rgb_size=(960, 540))
    ir_hotspots = [(10, 10, 100.0, 98.0)]
    res = fusion.fuse(ir_hotspots, [])

    assert res["fire_detected"] is True
    assert res["status"] == FIRE_IR_ONLY
    assert res["confirmed_count"] == 0
    assert res["ir_only_count"] == 1


def test_fire_fusion_matches_ir_with_eo_bbox():
    fusion = FireFusion(ir_size=(160, 120), rgb_size=(960, 540))
    ir_x, ir_y, temp_c, temp_raw = (80, 60, 120.0, 118.0)
    ir_hotspots = [(ir_x, ir_y, temp_c, temp_raw)]

    rgb_x, rgb_y = fusion.coord_mapper.ir_to_rgb(ir_x, ir_y)
    bbox_size = 20
    eo_bbox = (
        rgb_x - bbox_size / 2,
        rgb_y - bbox_size / 2,
        bbox_size,
        bbox_size,
        0.95,
    )

    res = fusion.fuse(ir_hotspots, [eo_bbox])

    assert res["fire_detected"] is True
    assert res["status"] == FIRE_CONFIRMED
    assert res["confirmed_count"] == 1
    assert res["ir_only_count"] == 0

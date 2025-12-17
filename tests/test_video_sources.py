from pathlib import Path

import pytest

from camera.rgb_video import VideoRGBCamera
from camera.purethermal.video_thermal import VideoThermalCamera
from core.buffer import DoubleBuffer


def _sample_path():
    root = Path(__file__).resolve().parents[1]
    return root / "sample" / "fire_sample.mp4"


@pytest.mark.skipif(not _sample_path().exists(), reason="sample video not found")
def test_video_rgb_camera_reads_frame():
    path = str(_sample_path())
    cfg = {"SLEEP": 0.0, "RES": [640, 480], "FPS": 30}
    buf = DoubleBuffer()
    cam = VideoRGBCamera(cfg, buf, [path], loop=False, frame_interval=0)

    frame, ts = cam.capture()
    try:
        assert frame is not None
        assert frame.ndim == 3
        assert frame.shape[2] == 3
        assert ts
    finally:
        if cam.cap:
            cam.cap.release()


@pytest.mark.skipif(not _sample_path().exists(), reason="sample video not found")
def test_video_thermal_camera_converts_to_raw16():
    path = str(_sample_path())
    cam = VideoThermalCamera(path, loop=False, target_size=(160, 120))

    frame = cam.capture()
    try:
        assert frame is not None
        assert frame.shape == (120, 160)
        assert frame.dtype.kind == "u"
    finally:
        cam.cleanup()

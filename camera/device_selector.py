import glob
import os
import re
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)


def _list_video_devices():
    """읽기 가능한 /dev/video* 경로 나열"""
    devices = []
    for path in glob.glob("/dev/video*"):
        if os.access(path, os.R_OK):
            devices.append(path)
    devices.sort(key=lambda p: int(re.sub(r"\D", "", p) or 0))
    return devices


def _probe_device_max_resolution(device):
    """v4l2-ctl로 지원 최대 해상도 추정 (실패 시 None)"""
    v4l2_cmd = shutil.which("v4l2-ctl") or "/usr/bin/v4l2-ctl"
    if os.path.exists(v4l2_cmd):
        try:
            res = subprocess.run(
                [v4l2_cmd, "--device", device, "--list-formats-ext"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            stdout = res.stdout or ""
            if "Type: Video Capture" not in stdout:
                return None
            sizes = re.findall(r"Size:\s+Discrete\s+(\d+)x(\d+)", stdout)
            if sizes:
                sizes_int = [(int(w), int(h)) for w, h in sizes]
                sizes_int.sort(key=lambda wh: wh[0] * wh[1], reverse=True)
                return sizes_int[0]
        except Exception:
            pass

    # v4l2-ctl을 사용 못할 때는 프레임을 직접 읽어 추정
    try:
        import cv2  # 지연 로드
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            try:
                dev_num = int(re.sub(r"\D", "", str(device)) or -1)
                if dev_num >= 0:
                    cap = cv2.VideoCapture(dev_num, cv2.CAP_V4L2)
            except Exception:
                pass
        if not cap.isOpened():
            cap.release()
            return None
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None and frame.ndim >= 2:
            h, w = frame.shape[:2]
            return w, h
    except Exception:
        return None


def auto_select_device(target_size=(640, 480), min_width=320):
    """목표 해상도에 가장 근접한 비디오 장치 자동 선택"""
    target_w = target_size[0] if target_size else 640
    devices = _list_video_devices()
    best = None
    best_score = -1
    for dev in devices:
        res = _probe_device_max_resolution(dev)
        if not res:
            continue
        w, h = res
        if w < min_width:
            continue
        score = w * h
        if score > best_score:
            best_score = score
            best = dev
    if best:
        return best
    # 해상도 정보를 못 얻어도 장치가 있으면 첫 번째로 fallback
    if devices:
        return devices[0]
    return None


class CameraDeviceSelector:
    """장치 자동 선택과 해상도 추정을 담당."""

    def __init__(self, target_size=(640, 480), min_width=320):
        self.target_size = target_size
        self.min_width = min_width

    def choose(self):
        dev = auto_select_device(self.target_size, self.min_width)
        if dev:
            logger.info("Auto-selected video device: %s", dev)
        else:
            logger.warning("No readable /dev/video* found for auto-select")
        return dev

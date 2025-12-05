import logging
import os
import cv2
import time
import subprocess
import threading

from datetime import datetime
from core.util import dyn_sleep
from core.state import camera_state
from camera.frame_source import FrameSource
from camera.device_selector import CameraDeviceSelector

logger = logging.getLogger(__name__)


def _log(msg, level=logging.INFO):
    logger.log(level, "[RGBCamera] %s", msg)


def _open_capture(dev_path, dev_num, size, fps):
    """한 번의 시도로 캡처를 연다. 성공/실패 여부만 반환."""
    _log(f"Opening video device: {dev_path} (index: {dev_num})")
    w, h = size

    gst_pipeline = (
        f"v4l2src device={dev_path} ! "
        f"video/x-raw,format=NV12,width={w},height={h},framerate={fps}/1 ! "
        "videoconvert ! appsink"
    )
    cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)

    if cap.isOpened():
        return cap

    _log("GStreamer backend failed, trying V4L2 with device index...", level=logging.WARNING)
    cap = cv2.VideoCapture(dev_num if dev_num is not None else dev_path, cv2.CAP_V4L2)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'NV12'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
    return cap


class RGBCamera(FrameSource):
    def __init__(self, cfg, d_buffer):
        super().__init__("RGBCamera")
        self.cap = None
        self.thread = None
        self.stop_event = threading.Event()
        self.last_ts = None
        self._cap_fail_count = 0
        self._reconnect_fail_threshold = 30

        self.fps = cfg['FPS']
        self.size = cfg['RES']
        self.sleep = cfg['SLEEP']
        self.device_override = cfg.get('DEVICE_OVERRIDE')
        self._auto_selector = CameraDeviceSelector(
            target_size=cfg.get('RES', (640, 480)),
            min_width=max(320, cfg.get('RES', [0])[0]),
        )

        self.d_buffer = d_buffer

        self.init_cam()

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass

    def _print_cap_info(self, device, frame):
        req_w, req_h = self.size
        rep_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        rep_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        rep_fps = self.cap.get(cv2.CAP_PROP_FPS)
        act_h, act_w = frame.shape[:2]

        _log(f"Connected to {device}")
        _log(
            f"Resolution - requested: {req_w}x{req_h}, "
            f"reported: {rep_w}x{rep_h}, actual: {act_w}x{act_h}"
        )
        _log(f"FPS - requested: {self.fps}, reported: {rep_fps:.2f}")

    def _release_cap(self):
        if self.cap:
            try:
                self.cap.release()
            except Exception as exc:
                _log(f"Failed to release capture: {exc}", level=logging.DEBUG)
            self.cap = None

    def init_cam(self):
        self._release_cap()
        device_override = getattr(self, 'device_override', None)
        device = device_override if device_override is not None else self._get_device()
        dev_path = device
        dev_num = None
        if isinstance(device, int):
            dev_num = device
            dev_path = f"/dev/video{device}"
        elif isinstance(device, str) and device.startswith("/dev/video"):
            try:
                dev_num = int(device.split("video")[-1])
            except Exception:
                dev_num = None
        
        self.cap = _open_capture(dev_path, dev_num, self.size, self.fps)

        if not self.cap.isOpened():
            try:
                subprocess.run(
                    ["sudo", "udevadm", "trigger"],
                    check=False,
                    capture_output=True
                )
                _log("Ran 'udevadm trigger', retrying capture...", level=logging.WARNING)
                time.sleep(2.0)
                self.cap = _open_capture(dev_path, dev_num, self.size, self.fps)
            except Exception as e:
                _log(f"udevadm trigger failed: {e}", level=logging.WARNING)

        if self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                self._print_cap_info(dev_path or self._get_device(), frame)
            else:
                _log(f"Device opened but failed to read frame from {dev_path}", level=logging.WARNING)
        else:
            _log(f"Failed to open device {dev_path}", level=logging.ERROR)
            
    def _get_device(self):
        raise NotImplementedError("_get_device() must be implemented in subclass")
    
    def capture(self):
        ret, frame = self.cap.read()
        if not ret or frame is None:
            self._cap_fail_count += 1
            if self._cap_fail_count % 30 == 1:
                _log(f"Capture failed (count: {self._cap_fail_count})", level=logging.WARNING)
            if self._cap_fail_count >= self._reconnect_fail_threshold and not self.stop_event.is_set():
                _log("Capture failing repeatedly, reinitializing camera...", level=logging.WARNING)
                self.init_cam()
                self._cap_fail_count = 0
            return None, None
        
        # 프레임 유효성 검사 (너무 작은 해상도/채널 오류 방지)
        if frame.size == 0 or frame.ndim < 2 or frame.shape[0] < 2 or frame.shape[1] < 2:
            return None, None
        # 1채널 또는 4채널을 BGR 3채널로 변환
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        elif frame.shape[2] != 3:
            return None, None
        
        # === 방향 조정 (키보드로 제어) ===
        # 회전 적용
        rotate = camera_state.rotate_rgb
        if rotate == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif rotate == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif rotate == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        
        # 좌우반전
        if camera_state.flip_h_rgb:
            frame = cv2.flip(frame, 1)
        
        # 상하반전
        if camera_state.flip_v_rgb:
            frame = cv2.flip(frame, 0)
        
        ts = datetime.now().strftime("%y%m%d%H%M%S%f")[:-4]
        self._cap_fail_count = 0
        return frame, ts

    def start(self):    
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self.thread

    def _loop(self):
        frame_count = 0
        while not self.stop_event.is_set():
            s_time = time.time()

            frame, ts = self.capture()
            if frame is None:
                dyn_sleep(s_time, self.sleep); continue

            if self.last_ts == ts:
                dyn_sleep(s_time, self.sleep); continue

            self.d_buffer.write((frame, ts))
            frame_count += 1
            if frame_count % 100 == 0:
                _log(f"Captured {frame_count} frames")
        
            self.last_ts = ts
                
            dyn_sleep(s_time, self.sleep)

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None
        self._release_cap()

class FrontRGBCamera(RGBCamera):
    def _get_device(self):
        for dev in ("/dev/video3", "/dev/video5"):
            if os.path.exists(dev):
                return dev

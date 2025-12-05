import logging
import threading
from typing import Any, Dict

from sender import send_images
from camera.source_factory import create_rgb_source, create_ir_source
from detector.tflite import TFLiteWorker
from core.buffer import DoubleBuffer
from core.state import (
    LabelScaleState,
    DEFAULT_LABEL_SCALE,
)
from display import display_loop

logger = logging.getLogger(__name__)


def _normalize_coord_cfg(params):
    """CONFIG의 COORD 키(대문자/소문자)를 런타임에서 쓰는 소문자 키로 정규화"""
    cfg = dict(params or {})

    def _get(key, default=None):
        val = cfg.get(key)
        if val is None:
            val = cfg.get(key.upper(), default)
        return val if val is not None else default

    out = {
        'offset_x': float(_get('offset_x', 0.0)),
        'offset_y': float(_get('offset_y', 0.0)),
        'scale': _get('scale', None),
    }
    if out['scale'] is not None:
        try:
            out['scale'] = float(out['scale'])
        except Exception:
            out['scale'] = None
    return out


class CoordState:
    def __init__(self, params=None):
        self._lock = threading.Lock()
        self._params = dict(params or {'offset_x': 0.0, 'offset_y': 0.0, 'scale': None})
        self._version = 0

    def get(self):
        with self._lock:
            return dict(self._params), self._version

    def update(self, **kwargs):
        with self._lock:
            self._params.update({k: v for k, v in kwargs.items() if v is not None})
            self._version += 1


class RuntimeController:
    """
    런타임 파이프라인을 묶어 관리하는 컨트롤러.
    - 카메라 소스/탐지 워커 시작·정지
    - 송신(TCP)/디스플레이 제어
    - 좌표/캡처 설정 공유
    """

    def __init__(self, buffers, server, sync_cfg, display_cfg, target_res, coord_cfg, capture_cfg, cfg=None):
        self.buffers = buffers
        self.server = server
        self.sync_cfg = sync_cfg
        self.display_cfg = display_cfg
        self.target_res = target_res
        self.coord_state = CoordState(_normalize_coord_cfg(coord_cfg))
        self.label_state = LabelScaleState(DEFAULT_LABEL_SCALE)
        self.capture_cfg = capture_cfg or {}
        self.cfg = cfg or {}
        self.sender_thread = None
        self.sender_stop = threading.Event()
        self.display_thread = None
        self.display_enabled = False
        self.rgb_source = None
        self.ir_source = None
        self.rgb_cfg = None
        self.ir_cfg = None
        self.rgb_input_cfg = None
        self.ir_input_cfg = None
        self.detector_worker = None
        self.detector_cfg = {}
        self._threads: Dict[str, threading.Thread] = {}

    def _start_thread(self, name, target, args=(), kwargs=None):
        if name in self._threads and self._threads[name].is_alive():
            return False
        t = threading.Thread(target=target, args=args, kwargs=kwargs or {}, daemon=True)
        self._threads[name] = t
        t.start()
        return True

    def _stop_thread(self, name, stop_event=None, join_timeout=2.0):
        t = self._threads.get(name)
        if not t:
            return False
        if stop_event:
            stop_event.set()
        t.join(timeout=join_timeout)
        self._threads.pop(name, None)
        return True

    def start_sender(self):
        self.sender_stop.clear()
        kwargs = {
            "host": self.server['IP'],
            "port": self.server['PORT'],
            "jpeg_quality": self.server.get('COMP_RATIO', 70),
            "sync_cfg": self.sync_cfg,
            "stop_event": self.sender_stop,
            "coord_state": self.coord_state,
            "label_state": self.label_state,
            "ir_size": tuple(self.ir_cfg.get('RES', (160, 120))) if self.ir_cfg else (160, 120),
            "rgb_size": tuple(self.target_res) if self.target_res else (960, 540),
        }
        return self._start_thread(
            "sender",
            target=send_images,
            args=(
                self.buffers['rgb'],
                self.buffers['ir'],
                self.buffers['ir16'],
                self.buffers['rgb_det'],
            ),
            kwargs=kwargs,
        )

    def stop_sender(self):
        return self._stop_thread("sender", stop_event=self.sender_stop)

    def sender_running(self):
        t = self._threads.get("sender")
        return t is not None and t.is_alive()

    def start_display(self):
        if self.display_enabled:
            return False
        self.display_enabled = True
        window_name = self.display_cfg.get('WINDOW_NAME', "Vision AI Display")
        return self._start_thread(
            "display",
            target=display_loop,
            args=(self.buffers['rgb'], self.buffers['ir'], self.buffers['rgb_det']),
            kwargs={"window_name": window_name, "target_res": self.target_res},
        )

    def stop_display(self):
        self.display_enabled = False
        return self._stop_thread("display")

    def display_running(self):
        t = self._threads.get("display")
        return self.display_enabled and t is not None and t.is_alive()

    def set_sources(self, rgb_source, ir_source, rgb_cfg, ir_cfg, rgb_input_cfg, ir_input_cfg):
        self.rgb_source = rgb_source
        self.ir_source = ir_source
        self.rgb_cfg = rgb_cfg
        self.ir_cfg = ir_cfg
        self.rgb_input_cfg = dict(rgb_input_cfg)
        self.ir_input_cfg = dict(ir_input_cfg)

    def set_detector(self, worker, det_cfg):
        self.detector_worker = worker
        self.detector_cfg = dict(det_cfg or {})

    def stop_sources(self):
        if self.rgb_source:
            try:
                self.rgb_source.stop()
            except Exception as exc:
                logger.warning("RGB source stop failed: %s", exc)
        if self.ir_source:
            try:
                self.ir_source.stop()
            except Exception as exc:
                logger.warning("IR source stop failed: %s", exc)

    def restart_sources(self, rgb_input_cfg=None, ir_input_cfg=None):
        if rgb_input_cfg:
            self.rgb_input_cfg = dict(rgb_input_cfg)
        if ir_input_cfg:
            self.ir_input_cfg = dict(ir_input_cfg)
        self.stop_sources()
        self.rgb_source = create_rgb_source(self.rgb_cfg, self.rgb_input_cfg, self.buffers['rgb'])
        self.ir_source = create_ir_source(self.ir_cfg, self.ir_input_cfg, self.buffers['ir'], self.buffers['ir16'])
        self.rgb_source.start()
        self.ir_source.start()
        return True

    def restart_ir_source(self):
        """IR 소스만 재시작 (RGB는 유지)"""
        if self.ir_source:
            try:
                self.ir_source.stop()
            except Exception:
                pass
        self.ir_source = create_ir_source(self.ir_cfg, self.ir_input_cfg, self.buffers['ir'], self.buffers['ir16'])
        self.ir_source.start()
        return True

    def get_input_cfg(self):
        return dict(self.rgb_input_cfg or {}), dict(self.ir_input_cfg or {})

    def get_coord_cfg(self):
        params, _ = self.coord_state.get()
        return params

    def set_coord_cfg(self, params):
        self.coord_state.update(**params)

    def get_label_scale(self):
        return self.label_state.get() if self.label_state else DEFAULT_LABEL_SCALE

    def adjust_label_scale(self, delta):
        if not self.label_state:
            return None
        return self.label_state.adjust(delta)

    def reset_label_scale(self):
        if not self.label_state:
            return None
        return self.label_state.reset()

    def get_sync_cfg(self):
        return dict(self.sync_cfg or {})

    def get_capture_cfg(self):
        return dict(self.capture_cfg or {})

    def update_ir_fire_cfg(self, fire_enabled=None, min_temp=None, thr=None, raw_thr=None, tau=None, restart=False):
        """IR 화점 탐지 관련 설정 업데이트. 기본은 런타임 적용, 필요 시 restart=True로 재시작"""
        ir = dict(self.ir_cfg or {})
        if 'FIRE_DETECTION' in ir and fire_enabled is not None:
            ir['FIRE_DETECTION'] = bool(fire_enabled)
        if min_temp is not None:
            ir['FIRE_MIN_TEMP'] = float(min_temp)
        if thr is not None:
            ir['FIRE_THR'] = float(thr)
        if raw_thr is not None:
            ir['FIRE_RAW_THR'] = float(raw_thr)
        if tau is not None:
            ir['TAU'] = float(tau)
        self.ir_cfg.update(ir)

        # 런타임 적용 지원 시 바로 반영
        if self.ir_source and hasattr(self.ir_source, "update_fire_params"):
            try:
                self.ir_source.update_fire_params(
                    fire_detection=fire_enabled,
                    min_temp=min_temp,
                    thr=thr,
                    raw_thr=raw_thr,
                    tau=tau,
                )
                if not restart:
                    return True
            except Exception:
                pass

        if restart:
            return self.restart_ir_source()
        return True

    def get_detector_cfg(self):
        return dict(self.detector_cfg or {})

    def restart_detector(self):
        """현재 설정으로 탐지 워커 재시작"""
        if self.detector_worker:
            try:
                self.detector_worker.stop()
                self.detector_worker.join(timeout=2.0)
            except Exception:
                pass
        cfg = self.detector_cfg or {}
        model_path = cfg.get('MODEL')
        labels_path = cfg.get('LABEL')
        delegate = cfg.get('DELEGATE')
        allowed = cfg.get('ALLOWED_CLASSES')
        use_npu = bool(cfg.get('USE_NPU', False))
        cpu_threads = cfg.get('CPU_THREADS', 1)
        conf_thr = float(cfg.get('CONF_THR', cfg.get('CONF_THRESHOLD', 0.15)))
        name = cfg.get('NAME', "DetRGB")
        new_worker = TFLiteWorker(
            model_path=model_path,
            labels_path=labels_path,
            input_buf=self.buffers['rgb'],
            output_buf=self.buffers['rgb_det'],
            allowed_class_ids=allowed,
            use_npu=use_npu,
            delegate_lib=delegate,
            cpu_threads=cpu_threads,
            target_fps=self.rgb_cfg.get('FPS', 30),
            target_res=self.target_res,
            conf_thr=conf_thr,
            name=name
        )
        new_worker.start()
        self.detector_worker = new_worker
        return True

    def update_detector_cfg(self, model_path=None, label_path=None, delegate=None, allowed_classes=None, use_npu=None, cpu_threads=None, conf_thr=None, restart=True):
        cfg = dict(self.detector_cfg or {})
        if model_path:
            cfg['MODEL'] = model_path
        if label_path:
            cfg['LABEL'] = label_path
        if delegate is not None:
            cfg['DELEGATE'] = delegate
        if allowed_classes is not None:
            cfg['ALLOWED_CLASSES'] = allowed_classes if allowed_classes else None
        if use_npu is not None:
            cfg['USE_NPU'] = bool(use_npu)
        if cpu_threads is not None:
            cfg['CPU_THREADS'] = int(cpu_threads)
        if conf_thr is not None:
            cfg['CONF_THR'] = float(conf_thr)
        self.detector_cfg = cfg
        if restart:
            return self.restart_detector()
        return True

    def status(self):
        """송신/디스플레이/소스/탐지기 상태를 요약"""
        return {
            "sender": self.sender_running(),
            "display": self.display_running(),
            "rgb_source": getattr(self.rgb_source, "thread", None) is not None and getattr(self.rgb_source.thread, "is_alive", lambda: False)(),
            "ir_source": getattr(self.ir_source, "thread", None) is not None and getattr(self.ir_source.thread, "is_alive", lambda: False)(),
            "detector": self.detector_worker is not None and self.detector_worker.is_alive(),
        }

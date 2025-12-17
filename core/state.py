# core/state.py
import threading

DEFAULT_LABEL_SCALE = 0.8
MIN_LABEL_SCALE = 0.4
MAX_LABEL_SCALE = 2.0
LABEL_SCALE_STEP = 0.1


class CameraState:
    """
    카메라 방향 조정 상태를 관리하는 싱글톤 클래스
    스레드 안전하게 flip/rotate 상태를 공유함
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_state()
        return cls._instance
    
    def _init_state(self):
        self._state_lock = threading.Lock()
        # IR 카메라 상태
        self._flip_h_ir = False      # 좌우반전
        self._flip_v_ir = False      # 상하반전
        self._rotate_ir = 0          # 회전 (0, 90, 180, 270)
        # RGB 카메라 상태
        self._flip_h_rgb = False     # 좌우반전
        self._flip_v_rgb = False     # 상하반전
        self._rotate_rgb = 0         # 회전 (0, 90, 180, 270)
    
    # === IR 카메라 ===
    @property
    def flip_h_ir(self):
        with self._state_lock:
            return self._flip_h_ir
    
    @property
    def flip_v_ir(self):
        with self._state_lock:
            return self._flip_v_ir
    
    @property
    def rotate_ir(self):
        with self._state_lock:
            return self._rotate_ir
    
    def toggle_flip_h_ir(self):
        with self._state_lock:
            self._flip_h_ir = not self._flip_h_ir
            return self._flip_h_ir
    
    def toggle_flip_v_ir(self):
        with self._state_lock:
            self._flip_v_ir = not self._flip_v_ir
            return self._flip_v_ir
    
    def rotate_ir_cw(self):
        """IR 카메라 시계방향 90도 회전"""
        with self._state_lock:
            self._rotate_ir = (self._rotate_ir + 90) % 360
            return self._rotate_ir
    
    # === RGB 카메라 ===
    @property
    def flip_h_rgb(self):
        with self._state_lock:
            return self._flip_h_rgb
    
    @property
    def flip_v_rgb(self):
        with self._state_lock:
            return self._flip_v_rgb
    
    @property
    def rotate_rgb(self):
        with self._state_lock:
            return self._rotate_rgb
    
    def toggle_flip_h_rgb(self):
        with self._state_lock:
            self._flip_h_rgb = not self._flip_h_rgb
            return self._flip_h_rgb
    
    def toggle_flip_v_rgb(self):
        with self._state_lock:
            self._flip_v_rgb = not self._flip_v_rgb
            return self._flip_v_rgb
    
    def rotate_rgb_cw(self):
        """RGB 카메라 시계방향 90도 회전"""
        with self._state_lock:
            self._rotate_rgb = (self._rotate_rgb + 90) % 360
            return self._rotate_rgb
    
    # === 공통 ===
    def toggle_flip_h_both(self):
        with self._state_lock:
            new_state = not self._flip_h_ir
            self._flip_h_ir = new_state
            self._flip_h_rgb = new_state
            return new_state
    
    def toggle_flip_v_both(self):
        with self._state_lock:
            new_state = not self._flip_v_ir
            self._flip_v_ir = new_state
            self._flip_v_rgb = new_state
            return new_state
    
    def get_status(self):
        with self._state_lock:
            return {
                'ir': {
                    'flip_h': self._flip_h_ir,
                    'flip_v': self._flip_v_ir,
                    'rotate': self._rotate_ir
                },
                'rgb': {
                    'flip_h': self._flip_h_rgb,
                    'flip_v': self._flip_v_rgb,
                    'rotate': self._rotate_rgb
                }
            }


# 전역 인스턴스
camera_state = CameraState()


class LabelScaleState:
    """RGB 검출 오버레이 라벨 크기 공유 상태"""

    def __init__(self, value=DEFAULT_LABEL_SCALE):
        self._lock = threading.Lock()
        self._value = float(value)

    def _clamp(self, value):
        return max(MIN_LABEL_SCALE, min(MAX_LABEL_SCALE, float(value)))

    def get(self):
        with self._lock:
            return self._value

    def set(self, value):
        with self._lock:
            self._value = self._clamp(value)
            return self._value

    def adjust(self, delta):
        with self._lock:
            self._value = self._clamp(self._value + float(delta))
            return self._value

    def reset(self):
        return self.set(DEFAULT_LABEL_SCALE)

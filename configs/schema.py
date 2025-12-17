from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional


@dataclass
class CameraConfig:
    FPS: int
    RES: Tuple[int, int]
    SLEEP: float
    DEVICE: Optional[str] = None
    FIRE_DETECTION: Optional[bool] = None
    FIRE_MIN_TEMP: Optional[float] = None
    FIRE_THR: Optional[float] = None
    FIRE_RAW_THR: Optional[float] = None
    TAU: Optional[float] = None
    DEVICE_OVERRIDE: Optional[str] = None
    ROTATE: Optional[int] = 0
    FLIP_H: Optional[bool] = False
    FLIP_V: Optional[bool] = False


@dataclass
class Config:
    MODEL: str
    LABEL: str
    DELEGATE: str
    CAMERA_IR: CameraConfig
    CAMERA_RGB_FRONT: CameraConfig
    TARGET_RES: Tuple[int, int]
    SERVER: Dict[str, Any]
    DISPLAY: Dict[str, Any]
    SYNC: Dict[str, Any]
    INPUT: Dict[str, Any]
    STATE: Dict[str, Any]
    CAPTURE: Dict[str, Any]
    COORD: Dict[str, Any]

import os
import yaml
import logging
from pathlib import Path

from configs.config import YAML_PATH
from configs.schema import Config, CameraConfig


class ConfigError(Exception):
    """설정 로드/검증 실패 시 사용."""


def _check_exists(path, name):
    if not path:
        raise ConfigError(f"{name} 경로가 비어 있습니다.")
    if not os.path.exists(path):
        raise ConfigError(f"{name} 경로가 존재하지 않습니다: {path}")
    return path


def _resolve_by_id(dev_path: str) -> str:
    """해당 /dev/videoX를 가리키는 by-id 링크가 있으면 반환."""
    by_id_dir = Path("/dev/v4l/by-id")
    try:
        for link in sorted(by_id_dir.glob("*")):
            try:
                if os.path.realpath(str(link)) == dev_path:
                    return str(link)
            except FileNotFoundError:
                continue
    except FileNotFoundError:
        pass
    return dev_path


def _scan_v4l_devices():
    base = Path("/sys/class/video4linux")
    devices = []
    try:
        entries = sorted(base.glob("video*"))
    except FileNotFoundError:
        return devices

    for entry in entries:
        name_file = entry / "name"
        try:
            name = name_file.read_text().strip()
        except Exception:
            continue
        dev_path = f"/dev/{entry.name}"
        devices.append({
            "name": name,
            "dev": dev_path,
            "by_id": _resolve_by_id(dev_path),
        })
    return devices


def _choose_device(devices, keywords):
    for dev in devices:
        lower = dev["name"].lower()
        if any(k in lower for k in keywords):
            return dev
    return None


def _needs_auto(path):
    if path is None:
        return True
    if isinstance(path, str) and path.strip().lower() == "auto":
        return True
    if isinstance(path, str) and not os.path.exists(path):
        return True
    return False


def _auto_map_devices(config: dict):
    """보드에 연결된 장치 이름으로 IR/RGB 디바이스를 자동 매핑."""
    logger = logging.getLogger(__name__)
    cam = config.get("CAMERA", {})
    ir_cfg = cam.get("IR", {})
    rgb_cfg = cam.get("RGB_FRONT", {})

    devices = _scan_v4l_devices()
    if not devices:
        return

    ir_dev = _choose_device(devices, ["purethermal"])
    rgb_dev = _choose_device(devices, ["viv_v4l2", "viv"]) or next(
        (d for d in devices if "purethermal" not in d["name"].lower()),
        None,
    )

    if ir_dev and _needs_auto(ir_cfg.get("DEVICE")):
        ir_cfg["DEVICE"] = ir_dev["by_id"]
        logger.info("IR 디바이스 자동 설정: %s (%s)", ir_cfg["DEVICE"], ir_dev["name"])
    if rgb_dev and _needs_auto(rgb_cfg.get("DEVICE")):
        rgb_cfg["DEVICE"] = rgb_dev["by_id"]
        logger.info("RGB 디바이스 자동 설정: %s (%s)", rgb_cfg["DEVICE"], rgb_dev["name"])


def get_cfg():
    if not os.path.exists(YAML_PATH):
        raise ConfigError(f"CONFIG_PATH가 가리키는 파일을 찾을 수 없습니다: {YAML_PATH}")

    with open(YAML_PATH, "r") as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    if not config or not isinstance(config, dict):
        raise ConfigError("설정 파일이 비어 있거나 형식이 올바르지 않습니다.")

    # 필수 키 검증
    try:
        model = config["MODEL"]
        label = config["LABEL"]
        delegate = config.get("DELEGATE")
        _check_exists(model, "MODEL")
        _check_exists(label, "LABEL")
        if delegate:
            _check_exists(delegate, "DELEGATE")

        cam = config["CAMERA"]
        _ = cam["IR"]
        _ = cam["RGB_FRONT"]
    except KeyError as e:
        raise ConfigError(f"필수 설정 키가 없습니다: {e}") from e

    _auto_map_devices(config)
    return _to_dataclass(config)


def _to_dataclass(raw: dict) -> Config:
    """dict 설정을 dataclass Config로 변환"""
    cam = raw["CAMERA"]
    return Config(
        MODEL=raw["MODEL"],
        LABEL=raw["LABEL"],
        DELEGATE=raw.get("DELEGATE", ""),
        CAMERA_IR=CameraConfig(**cam["IR"]),
        CAMERA_RGB_FRONT=CameraConfig(**cam["RGB_FRONT"]),
        TARGET_RES=tuple(raw.get("TARGET_RES", (0, 0))),
        SERVER=raw.get("SERVER", {}),
        DISPLAY=raw.get("DISPLAY", {}),
        SYNC=raw.get("SYNC", {}),
        INPUT=raw.get("INPUT", {}),
        STATE=raw.get("STATE", {}),
        CAPTURE=raw.get("CAPTURE", {}),
        COORD=raw.get("COORD", {}),
    )

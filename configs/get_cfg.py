import os
import yaml
from copy import deepcopy

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


def get_cfg():
    # 1순위: CONFIG_PATH 직접 지정
    if os.path.exists(YAML_PATH):
        with open(YAML_PATH, 'r') as f:
            config = yaml.load(f, Loader=yaml.FullLoader)
    else:
        # 2순위: base+env 병합 (선택적)
        base_path = os.path.join(os.path.dirname(__file__), "config_base.yaml")
        env_name = os.getenv("DEPLOY_ENV", "pc")
        env_path = os.path.join(os.path.dirname(__file__), "environments", f"{env_name}.yaml")
        if os.path.exists(base_path) and os.path.exists(env_path):
            with open(base_path, "r") as f:
                base_cfg = yaml.load(f, Loader=yaml.FullLoader) or {}
            with open(env_path, "r") as f:
                env_cfg = yaml.load(f, Loader=yaml.FullLoader) or {}
            config = merge_configs(base_cfg, env_cfg)
        else:
            raise ConfigError(f"CONFIG_PATH가 가리키는 파일을 찾을 수 없습니다: {YAML_PATH}")

    if not config or not isinstance(config, dict):
        raise ConfigError("설정 파일이 비어 있거나 형식이 올바르지 않습니다.")

    # 필수 키 검증
    try:
        model = config['MODEL']
        label = config['LABEL']
        delegate = config.get('DELEGATE')
        _check_exists(model, "MODEL")
        _check_exists(label, "LABEL")
        if delegate:
            _check_exists(delegate, "DELEGATE")

        cam = config['CAMERA']
        _ = cam['IR']
        _ = cam['RGB_FRONT']
    except KeyError as e:
        raise ConfigError(f"필수 설정 키가 없습니다: {e}") from e

    # 해상도/타입 검증
    _validate_resolution(config, cam)

    return _to_dataclass(config)


def _validate_resolution(config: dict, cam: dict):
    def _assert_res(res, name):
        if not isinstance(res, (list, tuple)) or len(res) != 2:
            raise ConfigError(f"{name} 해상도는 [width, height] 형식이어야 합니다: {res}")
        w, h = res
        if not isinstance(w, int) or not isinstance(h, int):
            raise ConfigError(f"{name} 해상도는 정수여야 합니다: {res}")
        if w % 16 != 0 or h % 8 != 0:
            raise ConfigError(f"{name} 해상도는 width 16배수, height 8배수여야 합니다: {res}")

    target_res = config.get('TARGET_RES')
    if target_res:
        _assert_res(target_res, "TARGET_RES")

    ir_res = cam.get('IR', {}).get('RES') if isinstance(cam, dict) else None
    rgb_res = cam.get('RGB_FRONT', {}).get('RES') if isinstance(cam, dict) else None
    if ir_res:
        _assert_res(ir_res, "CAMERA.IR.RES")
    if rgb_res:
        _assert_res(rgb_res, "CAMERA.RGB_FRONT.RES")


def merge_configs(base: dict, override: dict) -> dict:
    """
    얕은/깊은 dict 병합. override 우선.
    리스트/튜플은 override 값 사용, dict는 재귀 병합.
    """
    if base is None:
        return deepcopy(override)
    if override is None:
        return deepcopy(base)

    merged = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = merge_configs(merged.get(k, {}), v)
        else:
            merged[k] = deepcopy(v)
    return merged


def _to_dataclass(raw: dict) -> Config:
    """dict 설정을 dataclass Config로 변환"""
    cam = raw['CAMERA']
    return Config(
        MODEL=raw['MODEL'],
        LABEL=raw['LABEL'],
        DELEGATE=raw.get('DELEGATE', ""),
        CAMERA_IR=CameraConfig(**cam['IR']),
        CAMERA_RGB_FRONT=CameraConfig(**cam['RGB_FRONT']),
        TARGET_RES=tuple(raw.get('TARGET_RES', (0, 0))),
        SERVER=raw.get('SERVER', {}),
        DISPLAY=raw.get('DISPLAY', {}),
        SYNC=raw.get('SYNC', {}),
        INPUT=raw.get('INPUT', {}),
        STATE=raw.get('STATE', {}),
        CAPTURE=raw.get('CAPTURE', {}),
        COORD=raw.get('COORD', {}),
    )

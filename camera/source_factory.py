from camera.rgbcam import FrontRGBCamera
from camera.rgb_video import VideoRGBCamera
from camera.ircam import IRCamera
from camera.purethermal.video_thermal import VideoThermalCamera
from camera.purethermal.thermalcamera import ThermalCamera
from camera.mock_source import MockRGBCamera, MockThermalCamera
from camera.device_selector import CameraDeviceSelector


def _parse_paths(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value:
        if ';' in value:
            return [p.strip() for p in value.split(';') if p.strip()]
        return value
    return None


def _parse_interval(mode_cfg, key='FRAME_INTERVAL_MS', default=None):
    interval_ms = mode_cfg.get(key)
    if interval_ms is None:
        return default
    try:
        return max(0.0, float(interval_ms) / 1000.0)
    except Exception:
        return default


def create_rgb_source(rgb_cfg, mode_cfg, buffer):
    mode = str(mode_cfg.get('MODE', 'live') or 'live').lower()
    frame_interval = _parse_interval(mode_cfg, default=rgb_cfg.get('SLEEP'))
    if mode == 'video':
        paths = _parse_paths(mode_cfg.get('VIDEO_PATH'))
        if not paths:
            raise ValueError("RGB video mode requires VIDEO_PATH")
        loop = mode_cfg.get('LOOP', True)
        return VideoRGBCamera(rgb_cfg, buffer, paths, loop=loop, frame_interval=frame_interval)
    if mode == 'mock':
        color = tuple(mode_cfg.get('COLOR', (0, 255, 0)))
        return MockRGBCamera(rgb_cfg, buffer, color=color, frame_interval=frame_interval)
    if mode != 'live':
        raise ValueError(f"Unsupported RGB input mode: {mode}")

    device = mode_cfg.get('DEVICE', rgb_cfg.get('DEVICE'))
    if device == "":
        device = None
    if device is None or str(device).lower() == "auto":
        selector = CameraDeviceSelector(target_size=rgb_cfg.get('RES'), min_width=max(320, rgb_cfg.get('RES', [0])[0]))
        device = selector.choose()
    if device is not None:
        cfg = dict(rgb_cfg)
        cfg['DEVICE_OVERRIDE'] = device
        return FrontRGBCamera(cfg, buffer)
    return FrontRGBCamera(rgb_cfg, buffer)


def create_ir_source(ir_cfg, mode_cfg, ir_buffer, d16_buffer):
    mode = str(mode_cfg.get('MODE', 'live') or 'live').lower()
    cam_impl = None
    frame_interval = _parse_interval(mode_cfg)
    if mode == 'video':
        paths = _parse_paths(mode_cfg.get('VIDEO_PATH'))
        if not paths:
            raise ValueError("IR video mode requires VIDEO_PATH")
        loop = mode_cfg.get('LOOP', True)
        target_size = tuple(ir_cfg['RES'])
        cam_impl = VideoThermalCamera(paths, loop=loop, target_size=target_size, frame_interval=frame_interval)
    elif mode == 'mock':
        target_size = tuple(ir_cfg['RES'])
        cam_impl = MockThermalCamera(size=target_size, frame_interval=frame_interval)
    elif mode == 'live':
        device = mode_cfg.get('DEVICE', ir_cfg.get('DEVICE'))
        if isinstance(device, str):
            device = device.strip()
            if not device:
                device = None
            elif device.lower() == "auto":
                device = None
        if device:
            cam_impl = ThermalCamera(device_path=device)
    else:
        raise ValueError(f"Unsupported IR input mode: {mode}")
    return IRCamera(ir_cfg, ir_buffer, d16_buffer, cam_impl=cam_impl)

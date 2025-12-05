import cv2
import sys
import time
import select
import termios
import tty
import os
import logging
import argparse

from sender import send_images
from configs.get_cfg import get_cfg, ConfigError

from camera.source_factory import create_rgb_source, create_ir_source
from detector.tflite import TFLiteWorker
from core.buffer import DoubleBuffer
from core.state import (
    camera_state,
    LabelScaleState,
    DEFAULT_LABEL_SCALE,
    LABEL_SCALE_STEP,
)
from display import display_loop
from core.controller import RuntimeController, CoordState

logger = logging.getLogger(__name__)


def setup_logging():
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("tensorflow").setLevel(logging.WARNING)
    logging.getLogger("tflite_runtime").setLevel(logging.WARNING)


def _apply_input_overrides(name, cfg):
    prefix = name.upper()
    mode = os.getenv(f"{prefix}_INPUT_MODE")
    if mode:
        cfg['MODE'] = mode
    video_path = os.getenv(f"{prefix}_VIDEO_PATH")
    if video_path:
        if ';' in video_path:
            cfg['VIDEO_PATH'] = [p.strip() for p in video_path.split(';') if p.strip()]
        else:
            cfg['VIDEO_PATH'] = video_path
    loop = os.getenv(f"{prefix}_LOOP")
    if loop:
        cfg['LOOP'] = loop.lower() in ('1', 'true', 'yes', 'on')
    interval = os.getenv(f"{prefix}_FRAME_INTERVAL_MS")
    if interval:
        cfg['FRAME_INTERVAL_MS'] = int(interval)


def setup_keyboard():
    """터미널을 raw 모드로 설정 (키 입력 즉시 감지)"""
    try:
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        return old_settings
    except (termios.error, AttributeError, OSError) as e:
        logger.debug("Terminal setup failed (non-interactive): %s", e)
        return None


def restore_keyboard(old_settings):
    """터미널 설정 복원"""
    if old_settings:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except (termios.error, OSError) as e:
            logger.debug("Terminal restore failed: %s", e)


def check_keyboard():
    """
    키보드 입력 확인 (논블로킹)
    Returns: 입력된 키 또는 None
    """
    try:
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
    except (termios.error, OSError, ValueError) as e:
        logger.debug("Keyboard check failed: %s", e)
    return None


def print_help():
    """키보드 단축키 도움말 출력"""
    print("\n" + "=" * 55)
    print("Keyboard Controls:")
    print("-" * 55)
    print("  IR Camera:")
    print("    [1] Rotate IR 90 degrees (clockwise)")
    print("    [2] Toggle IR horizontal flip (left-right)")
    print("    [3] Toggle IR vertical flip (up-down)")
    print("    [g] Decrease IR tau (atmospheric transmittance)")
    print("    [t] Increase IR tau (atmospheric transmittance)")
    print("-" * 55)
    print("  RGB Camera:")
    print("    [4] Rotate RGB 90 degrees (clockwise)")
    print("    [5] Toggle RGB horizontal flip (left-right)")
    print("    [6] Toggle RGB vertical flip (up-down)")
    print("-" * 55)
    print("  Both Cameras:")
    print("    [7] Toggle BOTH horizontal flip")
    print("    [8] Toggle BOTH vertical flip")
    print("-" * 55)
    print("  Detection Overlay:")
    print("    [,] Decrease overlay label scale")
    print("    [.] Increase overlay label scale")
    print("    [0] Reset overlay label scale")
    print("-" * 55)
    print("  [s] Show current status")
    print("  [h] Show this help message")
    print("  [q] Quit application")
    print("=" * 55 + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Vision AI Application")
    parser.add_argument("--mode", choices=["cli", "gui"], default=None,
                        help="실행 모드 선택 (cli | gui), 기본값은 APP_MODE 또는 cli")
    return parser.parse_args()



def _load_config():
    cfg = get_cfg()
    if cfg is None:
        raise RuntimeError("Config is empty or invalid")
    return cfg


def _build_buffers():
    d16_ir, d_ir = DoubleBuffer(), DoubleBuffer()
    d_rgb, d_rgb_det = DoubleBuffer(), DoubleBuffer()
    return {
        'rgb': d_rgb,
        'rgb_det': d_rgb_det,
        'ir': d_ir,
        'ir16': d16_ir,
    }


def _start_sources(ir_cfg, ir_input_cfg, rgb_cfg, rgb_input_cfg, buffers):
    logger.info("IR source - Starting (%s)", ir_input_cfg.get('MODE', 'live'))
    ir_source = create_ir_source(ir_cfg, ir_input_cfg, buffers['ir'], buffers['ir16'])
    ir_source.start()

    logger.info("RGB source - Starting (%s)", rgb_input_cfg.get('MODE', 'live'))
    rgb_source = create_rgb_source(rgb_cfg, rgb_input_cfg, buffers['rgb'])
    rgb_source.start()

    return rgb_source, ir_source


def _start_detector(cfg, rgb_cfg, buffers, delegate, model, label):
    rgb_det_cfg = {
        'MODEL': model,
        'LABEL': label,
        'DELEGATE': delegate,
        'ALLOWED_CLASSES': [1],
        'USE_NPU': True,
        'CPU_THREADS': 1,
        'CONF_THR': float(getattr(cfg, 'CONF_THR', getattr(cfg, 'CONF_THRESHOLD', 0.15))),
        'NAME': "DetRGB",
    }
    worker = TFLiteWorker(
        model_path=model,
        labels_path=label,
        input_buf=buffers['rgb'],
        output_buf=buffers['rgb_det'],
        allowed_class_ids=rgb_det_cfg['ALLOWED_CLASSES'],
        use_npu=rgb_det_cfg['USE_NPU'],
        delegate_lib=delegate,
        cpu_threads=rgb_det_cfg['CPU_THREADS'],
        target_fps=rgb_cfg['FPS'],
        target_res=tuple(getattr(cfg, 'TARGET_RES', (rgb_cfg.get('RES', [0, 0])[0], rgb_cfg.get('RES', [0, 0])[1]))),
        conf_thr=rgb_det_cfg['CONF_THR'],
        name=rgb_det_cfg['NAME'],
    )
    worker.start()
    return worker, rgb_det_cfg


def _init_pipeline(gui_mode=False):
    cfg = _load_config()
    model = cfg.MODEL
    label = cfg.LABEL
    server = cfg.SERVER
    delegate = cfg.DELEGATE
    ir_cfg = cfg.CAMERA_IR.__dict__
    rgb_cfg = cfg.CAMERA_RGB_FRONT.__dict__

    # 카메라 회전/반전 설정 적용
    with camera_state._state_lock:
        camera_state._rotate_ir = ir_cfg.get('ROTATE', 0)
        camera_state._flip_h_ir = ir_cfg.get('FLIP_H', False)
        camera_state._flip_v_ir = ir_cfg.get('FLIP_V', False)
        camera_state._rotate_rgb = rgb_cfg.get('ROTATE', 0)
        camera_state._flip_h_rgb = rgb_cfg.get('FLIP_H', False)
        camera_state._flip_v_rgb = rgb_cfg.get('FLIP_V', False)

    state = cfg.STATE
    target_res = tuple(cfg.TARGET_RES)
    display_cfg = cfg.DISPLAY
    sync_cfg = cfg.SYNC
    input_cfg = cfg.INPUT

    rgb_input_cfg = dict(input_cfg.get('RGB', {})) if isinstance(input_cfg, dict) else {}
    ir_input_cfg = dict(input_cfg.get('IR', {})) if isinstance(input_cfg, dict) else {}
    _apply_input_overrides("RGB", rgb_input_cfg)
    _apply_input_overrides("IR", ir_input_cfg)

    display_enabled = False
    display_window = "Vision AI Display"
    if isinstance(display_cfg, dict):
        display_enabled = display_cfg.get('ENABLED', False)
        display_window = display_cfg.get('WINDOW_NAME', display_window)
    else:
        display_enabled = bool(display_cfg)
    if gui_mode:
        display_enabled = False

    buffers = _build_buffers()

    try:
        rgb_source, ir_source = _start_sources(ir_cfg, ir_input_cfg, rgb_cfg, rgb_input_cfg, buffers)
    except Exception as e:
        logger.exception("Camera source start failed: %s", e)
        raise

    try:
        rgb_det, rgb_det_cfg = _start_detector(cfg, rgb_cfg, buffers, delegate, model, label)
    except Exception as e:
        logger.exception("RGB-TFLite - Start failed: %s", e)
        raise

    coord_cfg = cfg.COORD
    capture_cfg = cfg.CAPTURE
    controller = RuntimeController(
        buffers,
        server,
        sync_cfg,
        display_cfg if isinstance(display_cfg, dict) else {},
        target_res,
        coord_cfg,
        capture_cfg,
        cfg=cfg
    )
    controller.set_sources(rgb_source, ir_source, rgb_cfg, ir_cfg, rgb_input_cfg, ir_input_cfg)
    if rgb_det:
        controller.set_detector(rgb_det, rgb_det_cfg)

    return {
        'cfg': cfg,
        'controller': controller,
        'display_enabled': display_enabled,
        'display_window': display_window,
        'gui_mode': gui_mode,
    }


def _run_cli(ctx):
    controller = ctx['controller']
    display_enabled = ctx['display_enabled']
    gui_mode = ctx['gui_mode']

    if not gui_mode:
        try:
            logger.info("TCP Sender - Starting")
            controller.start_sender()
        except Exception as e:
            logger.exception("TCP Sender - Start failed: %s", e)

    if display_enabled:
        try:
            logger.info("Display - Starting")
            controller.start_display()
        except Exception as e:
            logger.exception("Display - Start failed: %s", e)

    old_settings = setup_keyboard()
    print_help()
    try:
        while True:
            key = check_keyboard()

            if key == '1':
                angle = camera_state.rotate_ir_cw()
                logger.info("[IR] Rotation: %s degrees", angle)
            elif key == '2':
                state = camera_state.toggle_flip_h_ir()
                logger.info("[IR] Horizontal flip: %s", "ON" if state else "OFF")
            elif key == '3':
                state = camera_state.toggle_flip_v_ir()
                logger.info("[IR] Vertical flip: %s", "ON" if state else "OFF")
            elif key == '4':
                angle = camera_state.rotate_rgb_cw()
                logger.info("[RGB] Rotation: %s degrees", angle)
            elif key == '5':
                state = camera_state.toggle_flip_h_rgb()
                logger.info("[RGB] Horizontal flip: %s", "ON" if state else "OFF")
            elif key == '6':
                state = camera_state.toggle_flip_v_rgb()
                logger.info("[RGB] Vertical flip: %s", "ON" if state else "OFF")
            elif key == '7':
                state = camera_state.toggle_flip_h_both()
                logger.info("[BOTH] Horizontal flip: %s", "ON" if state else "OFF")
            elif key == '8':
                state = camera_state.toggle_flip_v_both()
                logger.info("[BOTH] Vertical flip: %s", "ON" if state else "OFF")
            elif key in ('g', '-'):
                cur_tau = float(controller.ir_cfg.get('TAU', 0.95) or 0.95)
                new_tau = max(0.1, cur_tau - 0.05)
                controller.update_ir_fire_cfg(tau=new_tau)
                logger.info("[IR] Tau: %.3f → %.3f", cur_tau, new_tau)
            elif key in ('t', '+'):
                cur_tau = float(controller.ir_cfg.get('TAU', 0.95) or 0.95)
                new_tau = min(1.0, cur_tau + 0.05)
                controller.update_ir_fire_cfg(tau=new_tau)
                logger.info("[IR] Tau: %.3f → %.3f", cur_tau, new_tau)
            elif key in (',', '<'):
                new_scale = controller.adjust_label_scale(-LABEL_SCALE_STEP)
                if new_scale is not None:
                    logger.info("[Overlay] Label scale: %.2fx (↓)", new_scale)
            elif key in ('.', '>'):
                new_scale = controller.adjust_label_scale(LABEL_SCALE_STEP)
                if new_scale is not None:
                    logger.info("[Overlay] Label scale: %.2fx (↑)", new_scale)
            elif key == '0':
                new_scale = controller.reset_label_scale()
                if new_scale is not None:
                    logger.info("[Overlay] Label scale reset → %.2fx", new_scale)
            elif key == 's':
                status = camera_state.get_status()
                ir = status['ir']
                rgb = status['rgb']
                logger.info(
                    "[Status] IR rotate=%3d flip_h=%s flip_v=%s",
                    ir['rotate'], "ON" if ir['flip_h'] else "OFF", "ON" if ir['flip_v'] else "OFF"
                )
                logger.info("[Status] IR tau=%.3f", float(controller.ir_cfg.get('TAU', 0.95) or 0.95))
                logger.info(
                    "[Status] RGB rotate=%3d flip_h=%s flip_v=%s",
                    rgb['rotate'], "ON" if rgb['flip_h'] else "OFF", "ON" if rgb['flip_v'] else "OFF"
                )
                logger.info("[Status] Overlay label scale=%.2fx", controller.get_label_scale())
            elif key == 'h':
                print_help()
            elif key == 'q':
                logger.info("Shutting down...")
                break

            time.sleep(0.1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        if controller:
            controller.stop_sources()
        restore_keyboard(old_settings)


def _run_gui(ctx):
    try:
        from gui.app_gui import run_gui
    except ImportError:
        # 분리된 모듈 시그니처가 다를 수 있으니 fallback
        try:
            from gui.main_window import MainWindow
            from PyQt6.QtWidgets import QApplication
        except ImportError as e:
            logger.error("GUI mode requested but PyQt6 not available: %s", e)
            sys.exit(1)

        app = QApplication([])
        window = MainWindow(ctx['controller'].buffers, camera_state, ctx['controller'], None)
        window.resize(1280, 720)
        window.show()
        app.exec()
        return

    run_gui(
        ctx['controller'].buffers,
        camera_state,
        ctx['controller'],
    )


def main():
    args = parse_args()
    env_mode = os.getenv("APP_MODE", "cli").lower()
    selected_mode = (args.mode or env_mode).lower()
    gui_mode = selected_mode == "gui"

    setup_logging()
    cv2.ocl.setUseOpenCL(True)

    try:
        ctx = _init_pipeline(gui_mode=gui_mode)
    except ConfigError as e:
        logger.error("Config error: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.exception("Pipeline init failed: %s", e)
        sys.exit(1)

    if gui_mode:
        _run_gui(ctx)
    else:
        _run_cli(ctx)


if __name__ == "__main__":
    main()

import logging
import subprocess
import sys
from pathlib import Path
from collections import deque
import time
import glob
import os

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QSizePolicy,
    QComboBox,
    QLineEdit,
    QCheckBox,
    QDoubleSpinBox,
    QSpinBox,
    QGroupBox,
    QGridLayout,
    QSplitter,
    QTabWidget,
    QFileDialog,
)
from PyQt6.QtCore import QTimer, Qt, QObject, pyqtSignal, QSize
from PyQt6.QtGui import QImage, QPixmap

import numpy as np
import cv2

from core.coord_mapper import CoordMapper
from core.fire_fusion import FireFusion, FireFusionFactory, prepare_fusion_for_output
from core.util import ts_to_epoch_ms
from core.state import DEFAULT_LABEL_SCALE

logger = logging.getLogger(__name__)

try:
    from gui.plot_widget import RollingPlot
except ImportError:
    RollingPlot = None

from gui.utils import cv_to_qpixmap, calc_fps, build_overlay
from gui.frame_updater import FrameUpdater


def _paths_to_text(value):
    if isinstance(value, (list, tuple)):
        return ";".join(str(v) for v in value)
    return str(value or "")


def _list_video_devices():
    """접근 가능한 /dev/video* 목록을 정렬해 반환"""
    devices = []
    for path in glob.glob("/dev/video*"):
        if os.access(path, os.R_OK):
            devices.append(path)
    def _key(p):
        try:
            return int("".join(ch for ch in p if ch.isdigit()))
        except Exception:
            return p
    return sorted(devices, key=_key)

def _refresh_device_combo(combo):
    current = combo.currentText().strip()
    combo.clear()
    combo.addItems([""] + _list_video_devices())
    combo.setCurrentText(current)


def _compact_layout(layout, margins=(6, 4, 6, 4), h_spacing=6, v_spacing=4):
    """레이아웃 기본 여백/간격을 줄여 컴팩트하게 배치"""
    if hasattr(layout, "setContentsMargins"):
        layout.setContentsMargins(*margins)
    if isinstance(layout, QGridLayout):
        layout.setHorizontalSpacing(h_spacing)
        layout.setVerticalSpacing(v_spacing)
    elif hasattr(layout, "setSpacing"):
        layout.setSpacing(min(h_spacing, v_spacing))


def _fix_label_height(label, lines=2, padding=4):
    """라벨 높이를 고정해 텍스트 길이 변화로 레이아웃이 흔들리지 않도록 설정"""
    fm = label.fontMetrics()
    h = fm.lineSpacing() * max(1, lines) + padding
    label.setFixedHeight(h)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class StablePreviewLabel(QLabel):
    """카메라 프리뷰가 바뀌어도 sizeHint가 흔들리지 않도록 고정된 힌트를 제공"""
    def __init__(self, text="", min_size=QSize(320, 180), parent=None):
        super().__init__(text, parent)
        self._base_hint = min_size
        self.setMinimumSize(min_size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self):
        return self._base_hint

    def minimumSizeHint(self):
        return self._base_hint


class LogSignaller(QObject):
    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.signaller = LogSignaller()

    def emit(self, record):
        msg = self.format(record)
        self.signaller.message.emit(msg)


class MainWindow(QMainWindow):
    def __init__(self, buffers, camera_state, controller, log_handler, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vision AI GUI")
        self.buffers = buffers
        self.camera_state = camera_state
        self.controller = controller
        self.capture_process = None
        self.log_handler = log_handler
        self.sync_cfg = controller.get_sync_cfg() if controller else {}
        self.rgb_ts_history = deque(maxlen=60)
        self.det_ts_history = deque(maxlen=60)
        self.ir_ts_history = deque(maxlen=60)
        self.config = controller.cfg if hasattr(controller, "cfg") else {}
        self.fusion_vis_mode = os.getenv("FUSION_VIS_MODE", "test").lower()
        self._last_det_ts = None
        self._coord_auto_set = False
        coord_params = self.controller.get_coord_cfg() if self.controller else {'offset_x': 0.0, 'offset_y': 0.0, 'scale': 1.0}
        self.ir_size = tuple(self.controller.ir_cfg.get('RES', (160, 120))) if self.controller else (160, 120)
        self.rgb_size = tuple(getattr(self.config, "TARGET_RES", (960, 540)))
        self.fire_fusion = FireFusion(
            ir_size=self.ir_size,
            rgb_size=self.rgb_size,
            offset_x=coord_params.get('offset_x', 0.0),
            offset_y=coord_params.get('offset_y', 0.0),
            scale=coord_params.get('scale')
        )

        rgb_input_cfg, ir_input_cfg = (self.controller.get_input_cfg() if self.controller else ({}, {}))

        self.rgb_label = StablePreviewLabel("RGB Preview")
        self.rgb_info = QLabel("-")
        self.rgb_info.setWordWrap(True)
        _fix_label_height(self.rgb_info, lines=2)
        self.rgb_info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rgb_info.setMinimumWidth(200)

        self.det_label = StablePreviewLabel("Det Preview")
        self.det_info = QLabel("-")
        self.det_info.setWordWrap(True)
        _fix_label_height(self.det_info, lines=2)
        self.det_info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.det_info.setMinimumWidth(200)
        self.ir_label = StablePreviewLabel("IR Preview")
        self.ir_info = QLabel("-")
        self.ir_info.setWordWrap(True)
        _fix_label_height(self.ir_info, lines=2)
        self.ir_info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.ir_info.setMinimumWidth(200)

        self.status_label = QLabel("Status: -")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _fix_label_height(self.status_label, lines=3, padding=6)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)

        self.det_plot = RollingPlot(title="Det FPS") if RollingPlot else None
        self.rgb_plot = RollingPlot(title="RGB FPS") if RollingPlot else None
        self.ir_plot = RollingPlot(title="IR FPS") if RollingPlot else None

        rotate_ir_btn = QPushButton("IR Rotate 90°")
        rotate_ir_btn.clicked.connect(self.on_rotate_ir)
        rotate_rgb_btn = QPushButton("RGB Rotate 90°")
        rotate_rgb_btn.clicked.connect(self.on_rotate_rgb)

        self.sender_btn = QPushButton("Start Sender")
        self.sender_btn.setCheckable(True)
        self.sender_btn.toggled.connect(self.on_sender_toggle)
        if self.controller and self.controller.sender_running():
            self.sender_btn.setChecked(True)
            self.sender_btn.setText("Stop Sender")

        self.capture_btn = QPushButton("Start Capture")
        self.capture_btn.setCheckable(True)
        self.capture_btn.toggled.connect(self.on_capture_toggle)

        self.rgb_mode_combo = QComboBox()
        self.rgb_mode_combo.addItems(["live", "video", "mock"])
        self.rgb_mode_combo.setCurrentText(str(rgb_input_cfg.get('MODE') or 'live').lower())
        self.rgb_path_edit = QLineEdit(_paths_to_text(rgb_input_cfg.get('VIDEO_PATH', '')))
        self.rgb_path_browse = QPushButton("Browse")
        self.rgb_path_browse.clicked.connect(self.browse_rgb_paths)
        self.rgb_loop_chk = QCheckBox("Loop")
        self.rgb_loop_chk.setChecked(bool(rgb_input_cfg.get('LOOP', True)))
        self.rgb_device_combo = QComboBox()
        self.rgb_device_combo.setEditable(True)
        self.rgb_device_combo.addItems([""] + _list_video_devices())
        self.rgb_device_combo.setCurrentText(str(rgb_input_cfg.get('DEVICE', "")))

        self.ir_mode_combo = QComboBox()
        self.ir_mode_combo.addItems(["live", "video", "mock"])
        self.ir_mode_combo.setCurrentText(str(ir_input_cfg.get('MODE') or 'live').lower())
        self.ir_path_edit = QLineEdit(_paths_to_text(ir_input_cfg.get('VIDEO_PATH', '')))
        self.ir_path_browse = QPushButton("Browse")
        self.ir_path_browse.clicked.connect(self.browse_ir_paths)
        self.ir_loop_chk = QCheckBox("Loop")
        self.ir_loop_chk.setChecked(bool(ir_input_cfg.get('LOOP', True)))
        self.ir_device_combo = QComboBox()
        self.ir_device_combo.setEditable(True)
        self.ir_device_combo.addItems([""] + _list_video_devices())
        self.ir_device_combo.setCurrentText(str(ir_input_cfg.get('DEVICE', "")))
        self.dev_refresh_btn = QPushButton("Refresh Devices")
        self.dev_refresh_btn.clicked.connect(self.refresh_device_lists)
        self.rgb_mode_combo.currentTextChanged.connect(self.update_device_fields)
        self.ir_mode_combo.currentTextChanged.connect(self.update_device_fields)

        det_cfg = self.controller.get_detector_cfg() if self.controller else {}
        self.model_edit = QLineEdit(det_cfg.get('MODEL', ""))
        self.delegate_edit = QLineEdit(det_cfg.get('DELEGATE', ""))
        self.label_edit = QLineEdit(det_cfg.get('LABEL', ""))
        self.conf_spin = QDoubleSpinBox()
        self.conf_spin.setRange(0.0, 1.0)
        self.conf_spin.setDecimals(3)
        self.conf_spin.setSingleStep(0.01)
        self.conf_spin.setValue(float(det_cfg.get('CONF_THR', det_cfg.get('CONF_THRESHOLD', 0.15))))
        self.model_browse = QPushButton("Browse")
        self.model_browse.clicked.connect(self.browse_model)
        self.label_browse = QPushButton("Browse")
        self.label_browse.clicked.connect(self.browse_label)
        self.delegate_browse = QPushButton("Browse")
        self.delegate_browse.clicked.connect(self.browse_delegate)
        self.cls_smoke_chk = QCheckBox("smoke")
        self.cls_fire_chk = QCheckBox("fire")
        allowed = det_cfg.get('ALLOWED_CLASSES', [1])
        if allowed is None or 0 in allowed:
            self.cls_smoke_chk.setChecked(True)
        if allowed is None or 1 in allowed:
            self.cls_fire_chk.setChecked(True)
        self.vis_mode_combo = QComboBox()
        self.vis_mode_combo.addItems(["test", "temp"])
        self.vis_mode_combo.setCurrentText(os.getenv("FUSION_VIS_MODE", "test").lower())
        self.vis_mode_combo.currentTextChanged.connect(self.on_vis_mode_change)
        self.apply_infer_btn = QPushButton("Apply RGB Inference")
        self.apply_infer_btn.clicked.connect(self.apply_infer_settings)
        # --- Input tab (RGB / IR) ---
        input_tab = QWidget()
        input_layout = QVBoxLayout(input_tab)
        rgb_box = QGroupBox("RGB Input")
        rgb_form = QGridLayout()
        rgb_form.setAlignment(Qt.AlignmentFlag.AlignTop)
        _compact_layout(rgb_form)
        rgb_form.setColumnStretch(1, 1)
        rgb_form.setColumnStretch(2, 1)
        rgb_form.addWidget(QLabel("Mode"), 0, 0)
        rgb_form.addWidget(self.rgb_mode_combo, 0, 1)
        rgb_form.addWidget(QLabel("Path(s)"), 1, 0)
        rgb_form.addWidget(self.rgb_path_edit, 1, 1, 1, 2)
        rgb_form.addWidget(self.rgb_path_browse, 1, 3)
        rgb_form.addWidget(self.rgb_loop_chk, 0, 2)
        rgb_form.addWidget(QLabel("Device"), 2, 0)
        rgb_form.addWidget(self.rgb_device_combo, 2, 1, 1, 2)
        rgb_box.setLayout(rgb_form)

        ir_box = QGroupBox("IR Input")
        ir_form = QGridLayout()
        ir_form.setAlignment(Qt.AlignmentFlag.AlignTop)
        _compact_layout(ir_form)
        ir_form.setColumnStretch(1, 1)
        ir_form.setColumnStretch(2, 1)
        ir_form.addWidget(QLabel("Mode"), 0, 0)
        ir_form.addWidget(self.ir_mode_combo, 0, 1)
        ir_form.addWidget(QLabel("Path(s)"), 1, 0)
        ir_form.addWidget(self.ir_path_edit, 1, 1, 1, 2)
        ir_form.addWidget(self.ir_path_browse, 1, 3)
        ir_form.addWidget(self.ir_loop_chk, 0, 2)
        ir_form.addWidget(QLabel("Device"), 2, 0)
        ir_form.addWidget(self.ir_device_combo, 2, 1, 1, 2)
        ir_box.setLayout(ir_form)

        self.apply_input_btn = QPushButton("Apply Input Settings")
        self.apply_input_btn.clicked.connect(self.apply_input_settings)
        input_layout.addWidget(rgb_box)
        input_layout.addWidget(ir_box)
        input_layout.addWidget(self.apply_input_btn)
        input_layout.addWidget(self.dev_refresh_btn)
        input_layout.addStretch()
        input_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        _compact_layout(input_layout, margins=(8, 6, 8, 6), h_spacing=6, v_spacing=6)

        coord_layout = QVBoxLayout()
        _compact_layout(coord_layout, margins=(8, 6, 8, 6), h_spacing=6, v_spacing=8)

        # Offset 그룹
        offset_group = QGroupBox("Offset")
        offset_form = QGridLayout()
        _compact_layout(offset_form, margins=(8, 6, 8, 6), h_spacing=6, v_spacing=6)
        self.offset_x_spin = QDoubleSpinBox()
        self.offset_x_spin.setRange(-1000.0, 1000.0)
        self.offset_x_spin.setDecimals(2)
        self.offset_x_spin.setValue(coord_params.get('offset_x', 0.0))
        self.offset_y_spin = QDoubleSpinBox()
        self.offset_y_spin.setRange(-1000.0, 1000.0)
        self.offset_y_spin.setDecimals(2)
        self.offset_y_spin.setValue(coord_params.get('offset_y', 0.0))
        offset_form.addWidget(QLabel("Offset X"), 0, 0)
        offset_form.addWidget(self.offset_x_spin, 0, 1, 1, 3)
        offset_form.addWidget(QLabel("Offset Y"), 1, 0)
        offset_form.addWidget(self.offset_y_spin, 1, 1, 1, 3)
        offset_form.addWidget(QLabel("Offset Step"), 2, 0)
        self.offset_step_spin = QDoubleSpinBox()
        self.offset_step_spin.setRange(0.1, 100.0)
        self.offset_step_spin.setValue(5.0)
        offset_form.addWidget(self.offset_step_spin, 2, 1, 1, 3)
        for col, (text, dx, dy) in enumerate([("←", -1, 0), ("→", 1, 0), ("↑", 0, -1), ("↓", 0, 1)], start=1):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _, dx=dx, dy=dy: self.nudge_offset(dx * self.offset_step_spin.value(), dy * self.offset_step_spin.value()))
            offset_form.addWidget(btn, 3, col)
        offset_group.setLayout(offset_form)
        coord_layout.addWidget(offset_group)

        # Scale 그룹
        scale_group = QGroupBox("Scale")
        scale_form = QGridLayout()
        _compact_layout(scale_form, margins=(8, 6, 8, 6), h_spacing=6, v_spacing=6)
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.1, 20.0)
        self.scale_spin.setDecimals(3)
        self.scale_spin.setSingleStep(0.05)
        self.scale_spin.setValue(coord_params.get('scale', 1.0) if coord_params.get('scale') is not None else 1.0)
        scale_form.addWidget(QLabel("Scale"), 0, 0)
        scale_form.addWidget(self.scale_spin, 0, 1, 1, 3)
        scale_form.addWidget(QLabel("Scale Step"), 1, 0)
        self.scale_step_spin = QDoubleSpinBox()
        self.scale_step_spin.setRange(0.001, 1.0)
        self.scale_step_spin.setValue(0.05)
        scale_form.addWidget(self.scale_step_spin, 1, 1, 1, 3)
        btn_minus = QPushButton("Scale -")
        btn_minus.clicked.connect(lambda: self.nudge_scale(-self.scale_step_spin.value()))
        btn_plus = QPushButton("Scale +")
        btn_plus.clicked.connect(lambda: self.nudge_scale(self.scale_step_spin.value()))
        scale_form.addWidget(btn_minus, 2, 1)
        scale_form.addWidget(btn_plus, 2, 2)

        scale_group.setLayout(scale_form)
        coord_layout.addWidget(scale_group)

        # Apply 버튼
        self.apply_coord_btn = QPushButton("Apply Coord")
        self.apply_coord_btn.clicked.connect(self.apply_coord_settings)
        coord_layout.addWidget(self.apply_coord_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        coord_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        coord_tab = QWidget()
        coord_tab.setLayout(coord_layout)

        capture_cfg = self.controller.get_capture_cfg() if self.controller else {}
        capture_layout = QGridLayout()
        capture_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        _compact_layout(capture_layout)
        capture_layout.setColumnStretch(1, 1)
        self.capture_output_edit = QLineEdit(capture_cfg.get('OUTPUT_DIR', "./capture_session"))
        capture_layout.addWidget(QLabel("Capture Output"), 0, 0)
        capture_layout.addWidget(self.capture_output_edit, 0, 1)

        self.capture_duration_spin = QDoubleSpinBox()
        self.capture_duration_spin.setSuffix(" s")
        self.capture_duration_spin.setRange(0, 10000)
        self.capture_duration_spin.setValue(capture_cfg.get('DURATION_SEC') or 0)
        capture_layout.addWidget(QLabel("Duration"), 1, 0)
        capture_layout.addWidget(self.capture_duration_spin, 1, 1)

        self.capture_max_spin = QSpinBox()
        self.capture_max_spin.setRange(0, 100000)
        self.capture_max_spin.setValue(capture_cfg.get('MAX_FRAMES') or 0)
        capture_layout.addWidget(QLabel("Max Frames"), 2, 0)
        capture_layout.addWidget(self.capture_max_spin, 2, 1)
        capture_tab = QWidget()
        capture_tab.setLayout(capture_layout)

        # IR 화점 탐지 설정
        ir_fire_cfg = (self.controller.ir_cfg if self.controller else {}).copy()
        ir_fire_box = QGroupBox("IR Hotspot Settings")
        ir_fire_layout = QGridLayout()
        ir_fire_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        _compact_layout(ir_fire_layout, margins=(8, 6, 8, 6), h_spacing=6, v_spacing=6)
        self.ir_fire_chk = QCheckBox("Fire Detection")
        self.ir_fire_chk.setChecked(bool(ir_fire_cfg.get('FIRE_DETECTION', True)))
        self.ir_fire_min = QDoubleSpinBox()
        self.ir_fire_min.setRange(-50.0, 400.0)
        self.ir_fire_min.setDecimals(1)
        self.ir_fire_min.setValue(ir_fire_cfg.get('FIRE_MIN_TEMP', 80))
        self.ir_fire_thr = QDoubleSpinBox()
        self.ir_fire_thr.setRange(0.0, 200.0)
        self.ir_fire_thr.setDecimals(1)
        self.ir_fire_thr.setValue(ir_fire_cfg.get('FIRE_THR', 20))
        self.ir_fire_raw = QDoubleSpinBox()
        self.ir_fire_raw.setRange(0.0, 200.0)
        self.ir_fire_raw.setDecimals(1)
        self.ir_fire_raw.setValue(ir_fire_cfg.get('FIRE_RAW_THR', 5))
        self.ir_tau = QDoubleSpinBox()
        self.ir_tau.setRange(0.1, 1.0)
        self.ir_tau.setDecimals(3)
        self.ir_tau.setSingleStep(0.01)
        self.ir_tau.setValue(ir_fire_cfg.get('TAU', 0.95))
        self.apply_ir_fire_btn = QPushButton("Apply IR Hotspot")
        self.apply_ir_fire_btn.clicked.connect(self.apply_ir_fire_settings)

        ir_fire_layout.addWidget(self.ir_fire_chk, 0, 0, 1, 2)
        ir_fire_layout.addWidget(QLabel("MinTemp(C)"), 1, 0)
        ir_fire_layout.addWidget(self.ir_fire_min, 1, 1)
        ir_fire_layout.addWidget(QLabel("Thr(C)"), 1, 2)
        ir_fire_layout.addWidget(self.ir_fire_thr, 1, 3)
        ir_fire_layout.addWidget(QLabel("RawThr(C)"), 2, 0)
        ir_fire_layout.addWidget(self.ir_fire_raw, 2, 1)
        ir_fire_layout.addWidget(QLabel("Tau"), 2, 2)
        ir_fire_layout.addWidget(self.ir_tau, 2, 3)
        ir_fire_layout.setColumnStretch(1, 1)
        ir_fire_layout.setColumnStretch(3, 1)
        ir_fire_layout.addWidget(self.apply_ir_fire_btn, 3, 0, 1, 4, alignment=Qt.AlignmentFlag.AlignLeft)
        ir_fire_box.setLayout(ir_fire_layout)
        ir_tab = QWidget()
        ir_tab_layout = QVBoxLayout(ir_tab)
        ir_tab_layout.addWidget(ir_fire_box)
        ir_tab_layout.addStretch()

        infer_box = QGroupBox("RGB Inference Settings")
        infer_layout = QGridLayout()
        infer_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        _compact_layout(infer_layout)
        infer_layout.setColumnStretch(1, 1)
        infer_layout.addWidget(QLabel("Model"), 0, 0)
        infer_layout.addWidget(self.model_edit, 0, 1)
        infer_layout.addWidget(self.model_browse, 0, 2)
        infer_layout.addWidget(QLabel("Label"), 1, 0)
        infer_layout.addWidget(self.label_edit, 1, 1)
        infer_layout.addWidget(self.label_browse, 1, 2)
        infer_layout.addWidget(QLabel("Delegate"), 2, 0)
        infer_layout.addWidget(self.delegate_edit, 2, 1)
        infer_layout.addWidget(self.delegate_browse, 2, 2)
        infer_layout.addWidget(QLabel("Conf Thr"), 3, 0)
        infer_layout.addWidget(self.conf_spin, 3, 1)
        infer_layout.addWidget(QLabel("Fusion Vis Mode"), 4, 0)
        infer_layout.addWidget(self.vis_mode_combo, 4, 1)
        class_row = QHBoxLayout()
        _compact_layout(class_row, margins=(0, 0, 0, 0), h_spacing=6, v_spacing=6)
        class_row.addWidget(QLabel("Classes"))
        class_row.addWidget(self.cls_smoke_chk)
        class_row.addWidget(self.cls_fire_chk)
        class_row.addStretch()
        infer_layout.addLayout(class_row, 5, 0, 1, 3)
        infer_layout.addWidget(self.apply_infer_btn, 6, 0, 1, 3)
        infer_box.setLayout(infer_layout)

        infer_tab = QWidget()
        infer_tab_layout = QVBoxLayout(infer_tab)
        infer_tab_layout.addWidget(infer_box)
        infer_tab_layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(rotate_ir_btn)
        btn_layout.addWidget(rotate_rgb_btn)
        btn_layout.addWidget(self.sender_btn)
        btn_layout.addWidget(self.capture_btn)
        btn_layout.addStretch()

        def preview_block(label_widget, info_widget):
            box = QVBoxLayout()
            box.addWidget(label_widget)
            box.addWidget(info_widget)
            return box

        preview_grid = QGridLayout()
        preview_grid.addLayout(preview_block(self.rgb_label, self.rgb_info), 0, 0)
        preview_grid.addLayout(preview_block(self.det_label, self.det_info), 0, 1)
        preview_grid.addLayout(preview_block(self.ir_label, self.ir_info), 1, 0)
        self.overlay_label = StablePreviewLabel("Overlay Preview")
        self.overlay_info = QLabel("-")
        self.overlay_info.setWordWrap(True)
        _fix_label_height(self.overlay_info, lines=2)
        self.overlay_info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.overlay_info.setMinimumWidth(200)
        overlay_block = QVBoxLayout()
        overlay_block.addWidget(self.overlay_label)
        overlay_block.addWidget(self.overlay_info)
        preview_grid.addLayout(overlay_block, 1, 1)
        preview_grid.setColumnStretch(0, 1)
        preview_grid.setColumnStretch(1, 1)
        preview_grid.setRowStretch(0, 1)
        preview_grid.setRowStretch(1, 1)

        # --- Tabs for settings ---
        tabs = QTabWidget()
        tabs.addTab(input_tab, "Input")
        tabs.addTab(infer_tab, "Inference")
        tabs.addTab(ir_tab, "IR Hotspot")
        tabs.addTab(coord_tab, "Overlay")
        tabs.addTab(capture_tab, "Capture")

        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)

        left_widget = QWidget()
        left_widget.setLayout(preview_grid)
        left_widget.setMinimumWidth(640)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_top_layout = QVBoxLayout()
        right_top_layout.addWidget(self.status_label)
        right_top_layout.addLayout(btn_layout)
        right_top_layout.addWidget(tabs)

        right_layout.addLayout(right_top_layout)
        right_layout.addStretch()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        # 하단 글로벌 영역: 플롯 + 로그를 전체 폭으로 배치 (세로 스플리터로 높이 조절 가능)
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        if self.rgb_plot and self.ir_plot:
            plot_layout = QHBoxLayout()
            if self.det_plot:
                plot_layout.addWidget(self.det_plot)
            plot_layout.addWidget(self.rgb_plot)
            plot_layout.addWidget(self.ir_plot)
            bottom_layout.addLayout(plot_layout)
        bottom_layout.addWidget(self.log_view)

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        v_splitter.addWidget(splitter)
        v_splitter.addWidget(bottom_widget)
        v_splitter.setStretchFactor(0, 5)
        v_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(v_splitter)

        # (log plot 추가 시 main_layout 쪽은 splitter로 대체했으므로 제거)

        self.setCentralWidget(main_widget)

        self.timer = QTimer(self)
        self.frame_updater = FrameUpdater(
            buffers,
            controller,
            self.config,
            {
                'rgb_label': self.rgb_label,
                'det_label': self.det_label,
                'ir_label': self.ir_label,
                'overlay_label': self.overlay_label,
                'rgb_info': self.rgb_info,
                'det_info': self.det_info,
                'ir_info': self.ir_info,
                'status_label': self.status_label,
                'fusion_info': self.fusion_info if hasattr(self, 'fusion_info') else None,
            },
            {
                'det_plot': self.det_plot,
                'rgb_plot': self.rgb_plot,
                'ir_plot': self.ir_plot,
            },
            self.sync_cfg,
        )
        self.frame_updater.set_fire_fusion(self.fire_fusion)

        self.timer.timeout.connect(self.frame_updater.update_frames)
        self.timer.start(50)
        self.update_device_fields()

    def on_rotate_ir(self):
        angle = self.camera_state.rotate_ir_cw()
        self.append_log(f"IR rotation: {angle}°")

    def on_rotate_rgb(self):
        angle = self.camera_state.rotate_rgb_cw()
        self.append_log(f"RGB rotation: {angle}°")

    def append_log(self, text):
        self.log_view.append(text)

    def refresh_device_lists(self):
        _refresh_device_combo(self.rgb_device_combo)
        _refresh_device_combo(self.ir_device_combo)
        self.append_log("Device list refreshed")

    def update_device_fields(self):
        """Enable device selection only for live mode."""
        rgb_live = self.rgb_mode_combo.currentText().lower() == "live"
        ir_live = self.ir_mode_combo.currentText().lower() == "live"
        self.rgb_device_combo.setEnabled(rgb_live)
        self.ir_device_combo.setEnabled(ir_live)
        self.dev_refresh_btn.setEnabled(rgb_live or ir_live)
        if not rgb_live:
            self.rgb_device_combo.setCurrentText("")
        if not ir_live:
            self.ir_device_combo.setCurrentText("")

    def on_sender_toggle(self, checked):
        if not self.controller:
            self.append_log("Controller unavailable")
            self.sender_btn.setChecked(False)
            return
        if checked:
            started = self.controller.start_sender()
            if started:
                self.append_log("Sender started")
                self.sender_btn.setText("Stop Sender")
            else:
                self.append_log("Sender already running")
                self.sender_btn.setChecked(True)
        else:
            self.controller.stop_sender()
            self.append_log("Sender stopped")
            self.sender_btn.setText("Start Sender")

    def on_capture_toggle(self, checked):
        if checked:
            self.start_capture()
        else:
            self.stop_capture()

    def start_capture(self):
        capture_script = Path(__file__).resolve().parents[1] / "capture.py"
        if not capture_script.exists():
            self.append_log("capture.py not found")
            self.capture_btn.setChecked(False)
            return
        try:
            cmd = [sys.executable, str(capture_script)]
            output_dir = self.capture_output_edit.text().strip()
            if output_dir:
                cmd += ["--output", output_dir]
            duration = self.capture_duration_spin.value()
            if duration > 0:
                cmd += ["--duration", f"{duration}"]
            max_frames = self.capture_max_spin.value()
            if max_frames > 0:
                cmd += ["--max-frames", str(max_frames)]

            self.capture_process = subprocess.Popen(cmd)
            self.append_log("Capture started")
            self.capture_btn.setText("Stop Capture")
        except Exception as e:
            self.append_log(f"Capture start failed: {e}")
            self.capture_btn.setChecked(False)
            self.capture_process = None

    def stop_capture(self):
        if self.capture_process:
            self.capture_process.terminate()
            self.capture_process = None
        self.append_log("Capture stopped")
        self.capture_btn.setText("Start Capture")

    def apply_input_settings(self):
        if not self.controller:
            self.append_log("Controller unavailable")
            return
        rgb_cfg = {
            'MODE': self.rgb_mode_combo.currentText().lower(),
            'LOOP': self.rgb_loop_chk.isChecked(),
            'DEVICE': self.rgb_device_combo.currentText().strip(),
        }
        ir_cfg = {
            'MODE': self.ir_mode_combo.currentText().lower(),
            'LOOP': self.ir_loop_chk.isChecked(),
            'DEVICE': self.ir_device_combo.currentText().strip(),
        }

        def parse_paths(text):
            parts = [p.strip() for p in text.split(';') if p.strip()]
            if not parts:
                return ""
            if len(parts) == 1:
                return parts[0]
            return parts

        rgb_paths = parse_paths(self.rgb_path_edit.text())
        ir_paths = parse_paths(self.ir_path_edit.text())

        if rgb_cfg['MODE'] == 'video' and not rgb_paths:
            self.append_log("RGB video mode requires path(s)")
            return
        if ir_cfg['MODE'] == 'video' and not ir_paths:
            self.append_log("IR video mode requires path(s)")
            return

        if rgb_paths:
            rgb_cfg['VIDEO_PATH'] = rgb_paths
        if ir_paths:
            ir_cfg['VIDEO_PATH'] = ir_paths
        if rgb_cfg['MODE'] != 'live':
            rgb_cfg['DEVICE'] = ""
        if ir_cfg['MODE'] != 'live':
            ir_cfg['DEVICE'] = ""

        try:
            self.controller.restart_sources(rgb_cfg, ir_cfg)
            self.append_log("Input settings applied")
        except Exception as e:
            self.append_log(f"Input apply failed: {e}")

    def apply_coord_settings(self):
        if not self.controller:
            self.append_log("Controller unavailable")
            return
        params = {
            'offset_x': self.offset_x_spin.value(),
            'offset_y': self.offset_y_spin.value(),
            'scale': self.scale_spin.value(),
        }
        self.controller.set_coord_cfg(params)
        # UI 동기화
        self.offset_x_spin.setValue(params.get('offset_x', self.offset_x_spin.value()))
        self.offset_y_spin.setValue(params.get('offset_y', self.offset_y_spin.value()))
        self.scale_spin.setValue(params.get('scale', self.scale_spin.value()))
        self.append_log(f"Coord updated: {params}")

    def apply_ir_fire_settings(self):
        if not self.controller:
            self.append_log("Controller unavailable")
            return
        try:
            self.controller.update_ir_fire_cfg(
                fire_enabled=self.ir_fire_chk.isChecked(),
                min_temp=self.ir_fire_min.value(),
                thr=self.ir_fire_thr.value(),
                raw_thr=self.ir_fire_raw.value(),
                tau=self.ir_tau.value(),
            )
            self.append_log("IR hotspot settings applied (runtime)")
        except Exception as e:
            self.append_log(f"IR hotspot apply failed: {e}")

    def apply_infer_settings(self):
        if not self.controller:
            self.append_log("Controller unavailable")
            return
        self.on_vis_mode_change(self.vis_mode_combo.currentText())
        allowed = []
        if self.cls_smoke_chk.isChecked():
            allowed.append(0)
        if self.cls_fire_chk.isChecked():
            allowed.append(1)
        if not allowed:
            allowed = None
        try:
            self.controller.update_detector_cfg(
                model_path=self.model_edit.text().strip(),
                label_path=self.label_edit.text().strip(),
                delegate=self.delegate_edit.text().strip(),
                allowed_classes=allowed,
                conf_thr=self.conf_spin.value(),
                use_npu=bool(self.delegate_edit.text().strip()),
                restart=True
            )
            self.append_log(f"RGB inference settings applied (detector restarted, vis_mode={self.fusion_vis_mode})")
        except Exception as e:
            self.append_log(f"RGB inference apply failed: {e}")

    def on_vis_mode_change(self, text):
        """시각화 모드 변경 시 내부 상태와 환경변수를 동기화"""
        self.fusion_vis_mode = (text or "test").lower()
        os.environ["FUSION_VIS_MODE"] = self.fusion_vis_mode

    def browse_model(self):
        start_dir = str(Path(self.model_edit.text()).parent) if self.model_edit.text() else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Select Model", start_dir, "TFLite Files (*.tflite);;All Files (*)")
        if path:
            self.model_edit.setText(path)

    def browse_label(self):
        start_dir = str(Path(self.label_edit.text()).parent) if self.label_edit.text() else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Select Label", start_dir, "Text Files (*.txt);;All Files (*)")
        if path:
            self.label_edit.setText(path)

    def browse_delegate(self):
        start_dir = str(Path(self.delegate_edit.text()).parent) if self.delegate_edit.text() else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Select Delegate", start_dir, "Shared Library (*.so);;All Files (*)")
        if path:
            self.delegate_edit.setText(path)

    def browse_rgb_paths(self):
        start_dir = str(Path(self.rgb_path_edit.text().split(";")[0]).parent) if self.rgb_path_edit.text() else str(Path.cwd())
        paths, _ = QFileDialog.getOpenFileNames(self, "Select RGB Video(s)", start_dir, "Video Files (*.mp4 *.avi *.mkv *.mov *.mpg *.mpeg);;All Files (*)")
        if paths:
            self.rgb_path_edit.setText(";".join(paths))

    def browse_ir_paths(self):
        start_dir = str(Path(self.ir_path_edit.text().split(";")[0]).parent) if self.ir_path_edit.text() else str(Path.cwd())
        paths, _ = QFileDialog.getOpenFileNames(self, "Select IR Video(s)", start_dir, "Video Files (*.mp4 *.avi *.mkv *.mov *.mpg *.mpeg);;All Files (*)")
        if paths:
            self.ir_path_edit.setText(";".join(paths))

    def nudge_offset(self, dx, dy):
        self.offset_x_spin.setValue(self.offset_x_spin.value() + dx)
        self.offset_y_spin.setValue(self.offset_y_spin.value() + dy)
        self.apply_coord_settings()

    def nudge_scale(self, ds):
        new_scale = max(0.1, self.scale_spin.value() + ds)
        self.scale_spin.setValue(new_scale)
        self.append_log(f"Scale nudged to {new_scale:.3f}")
        self.apply_coord_settings()

    def _sync_coord_ui(self):
        """컨트롤러에 저장된 좌표/스케일을 UI에 반영 (가시성 확보용)"""
        if not self.controller:
            return
        params = self.controller.get_coord_cfg()
        self.offset_x_spin.blockSignals(True)
        self.offset_y_spin.blockSignals(True)
        self.scale_spin.blockSignals(True)
        try:
            self.offset_x_spin.setValue(params.get('offset_x', self.offset_x_spin.value()))
            self.offset_y_spin.setValue(params.get('offset_y', self.offset_y_spin.value()))
            if params.get('scale') is not None:
                self.scale_spin.setValue(params.get('scale'))
            else:
                # 컨트롤러에 scale이 None이면 UI는 기본 1.0 유지
                self.scale_spin.setValue(max(0.1, self.scale_spin.value()))
        finally:
            self.offset_x_spin.blockSignals(False)
            self.offset_y_spin.blockSignals(False)
            self.scale_spin.blockSignals(False)

    def closeEvent(self, event):
        if self.capture_process:
            self.stop_capture()
        if self.controller and self.controller.sender_running():
            self.controller.stop_sender()
        if self.controller:
            self.controller.stop_sources()
        if self.log_handler:
            logging.getLogger().removeHandler(self.log_handler)
        super().closeEvent(event)

    def update_frames(self):
        det_item = self.buffers['rgb_det'].read()
        rgb_item = self.buffers['rgb'].read()
        ir_item = self.buffers['ir'].read()

        det_frame = det_item[0] if det_item else None
        det_meta = det_item[2] if det_item and len(det_item) > 2 else None
        det_count = len(det_meta) if det_meta else 0
        det_ts_str = det_item[1] if det_item else None
        ir_meta = ir_item[2] if ir_item and len(ir_item) > 2 else None
        ir_hotspots = ir_item[3] if ir_item and len(ir_item) > 3 else []
        ir_max = None
        ir_min = None
        if ir_meta and isinstance(ir_meta, dict):
            ir_max = ir_meta.get('temp_corrected', ir_meta.get('temp_raw'))
            ir_min = ir_meta.get('min_temp', None)
        rgb_frame = rgb_item[0] if rgb_item else None
        ir_frame = ir_item[0] if ir_item else None
        t_det = ts_to_epoch_ms(det_ts_str) if det_ts_str else None
        t_rgb = ts_to_epoch_ms(rgb_item[1]) if rgb_item else None
        t_ir = ts_to_epoch_ms(ir_item[1]) if ir_item else None

        if det_item and det_ts_str != self._last_det_ts:
            self._last_det_ts = det_ts_str
            self.det_ts_history.append(time.time() * 1000.0)
        if t_rgb:
            self.rgb_ts_history.append(t_rgb)
        if t_ir:
            self.ir_ts_history.append(t_ir)

        # 초기 scale이 None인 경우 프레임 크기로 자동 계산해 한 번만 UI/컨트롤러에 반영
        if self.controller and not self._coord_auto_set and rgb_frame is not None and ir_frame is not None:
            coord = self.controller.get_coord_cfg()
            if coord.get('scale') is None:
                try:
                    auto_scale = min(rgb_frame.shape[1] / ir_frame.shape[1], rgb_frame.shape[0] / ir_frame.shape[0])
                    if auto_scale > 0:
                        updated = dict(coord, scale=auto_scale)
                        self.controller.set_coord_cfg(updated)
                        self.scale_spin.blockSignals(True)
                        self.scale_spin.setValue(auto_scale)
                        self.scale_spin.blockSignals(False)
                        self._coord_auto_set = True
                        self.append_log(f"Coord scale auto-set: {auto_scale:.3f}")
                except Exception:
                    pass

        self._sync_coord_ui()

        vis_mode = getattr(self, "fusion_vis_mode", "test")
        # Fusion annotations will be drawn later (after mode filtering) on this base frame.
        annotated_det = det_frame.copy() if det_frame is not None else None

        if rgb_frame is not None:
            pix = _cv_to_qpixmap(rgb_frame)
            if pix:
                self.rgb_label.setPixmap(pix.scaled(
                    self.rgb_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            rgb_dev = "-"
            if self.controller:
                rgb_cfg, _ = self.controller.get_input_cfg()
                rgb_dev = rgb_cfg.get('DEVICE', "-")
            model_name = getattr(self.config, "MODEL", "-") if self.config else "-"
            self.rgb_info.setText(
                f"RGB {rgb_frame.shape[1]}x{rgb_frame.shape[0]} | fps~{_calc_fps(self.rgb_ts_history):.1f} | dev={rgb_dev} | model={model_name}"
            )
        if annotated_det is not None:
            pix = _cv_to_qpixmap(annotated_det)
            if pix:
                self.det_label.setPixmap(pix.scaled(
                    self.det_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            model_name = getattr(self.config, "MODEL", "-") if self.config else "-"
            self.det_info.setText(
                f"Det {annotated_det.shape[1]}x{annotated_det.shape[0]} | det={det_count} | model={model_name}"
            )
        if ir_frame is not None:
            pix = _cv_to_qpixmap(ir_frame)
            if pix:
                self.ir_label.setPixmap(pix.scaled(
                    self.ir_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            ir_dev = "-"
            if self.controller:
                _, ir_cfg = self.controller.get_input_cfg()
                ir_dev = ir_cfg.get('DEVICE', "-")
            self.ir_info.setText(
                f"IR {ir_frame.shape[1]}x{ir_frame.shape[0]} | fps~{_calc_fps(self.ir_ts_history):.1f} | dev={ir_dev}"
                + (f" | min={ir_min:.1f}C" if ir_min is not None else "")
                + (f" | max={ir_max:.1f}C" if ir_max is not None else "")
            )

        status = []
        if det_item:
            status.append(f"Det ts: {det_item[1]} (det={det_count})")
        if rgb_item:
            status.append(f"RGB ts: {rgb_item[1]}")
        if ir_item:
            status.append(f"IR ts: {ir_item[1]}")
        sender_state = "Sender: ON" if self.controller and self.controller.sender_running() else "Sender: OFF"
        display_state = ""
        det_fps = _calc_fps(self.det_ts_history)
        rgb_fps = _calc_fps(self.rgb_ts_history)
        ir_fps = _calc_fps(self.ir_ts_history)
        max_diff = self.sync_cfg.get('MAX_DIFF_MS', 120)
        if t_det and t_ir:
            diff = abs(t_det - t_ir)
            sync_state = f"SYNC: {'OK' if diff <= max_diff else f'WARN ({diff:.0f}ms)'}"
        else:
            sync_state = "SYNC: N/A"
        # 상태 라벨: 3줄 고정 포맷으로 높이 변동 방지
        line1 = f"{sender_state} | {sync_state} | MaxDiff={max_diff}ms"
        line2 = f"Det {det_fps:.1f} FPS | IR {ir_fps:.1f} FPS | RGB {rgb_fps:.1f} FPS"
        ts_det = det_item[1] if det_item else "-"
        ts_rgb = rgb_item[1] if rgb_item else "-"
        ts_ir = ir_item[1] if ir_item else "-"
        line3 = f"TS det={ts_det} | rgb={ts_rgb} | ir={ts_ir}"
        self.status_label.setText("\n".join([line1, line2, line3]))
        if self.det_plot:
            self.det_plot.update_value(det_fps)
        if self.rgb_plot:
            self.rgb_plot.update_value(rgb_fps)
        if self.ir_plot:
            self.ir_plot.update_value(ir_fps)

        base_rgb_for_overlay = rgb_frame
        overlay_frame = build_overlay(base_rgb_for_overlay, ir_frame, self.controller.get_coord_cfg() if self.controller else {})
        if overlay_frame is not None:
            pix = _cv_to_qpixmap(overlay_frame)
            if pix:
                self.overlay_label.setPixmap(pix.scaled(
                    self.overlay_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            coord = self.controller.get_coord_cfg() if self.controller else {}
            self.overlay_info.setText(
                f"Overlay offset=({coord.get('offset_x',0):.1f},{coord.get('offset_y',0):.1f}) scale={coord.get('scale','auto')}"
            )

        # Fusion (IR + EO) overlay with color-coded boxes on det frame
        fusion_info = "-"
        if det_meta and annotated_det is not None:
            if self.controller:
                params = self.controller.get_coord_cfg()
                self.fire_fusion.coord_mapper = CoordMapper(
                    ir_size=self.ir_size,
                    rgb_size=self.rgb_size,
                    offset_x=params.get('offset_x', 0.0),
                    offset_y=params.get('offset_y', 0.0),
                    scale=params.get('scale'),
                )
            if not isinstance(ir_hotspots, list):
                ir_hotspots = []
            eo_bboxes = [d for d in det_meta if len(d) >= 6]
            fusion = self.fire_fusion.fuse(ir_hotspots, eo_bboxes)
            label_scale = self.controller.get_label_scale() if self.controller else DEFAULT_LABEL_SCALE
            fusion_payload, annotated_det = prepare_fusion_for_output(
                fusion,
                det_frame=annotated_det,
                vis_mode=vis_mode,
                label_scale=label_scale,
                default_label_scale=DEFAULT_LABEL_SCALE,
                json_safe=False,
            )
            if fusion_payload:
                fusion_info = (
                    f"{fusion_payload.get('status','NO_FIRE')} | "
                    f"conf={fusion_payload.get('confidence',0.0):.2f} | "
                    f"ir_hotspot={len(ir_hotspots)} | eo={len(eo_bboxes)}"
                )

        # Det view 업데이트는 vis_mode 적용 후 그린 annotated_det을 사용
        if annotated_det is not None:
            pix = _cv_to_qpixmap(annotated_det)
            if pix:
                self.det_label.setPixmap(pix.scaled(
                    self.det_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            model_name = getattr(self.config, "MODEL", "-") if self.config else "-"
            self.det_info.setText(
                f"Det {annotated_det.shape[1]}x{annotated_det.shape[0]} | det={det_count} | model={model_name}"
            )


def run_gui(buffers, camera_state, controller):
    app = QApplication([])
    log_handler = QtLogHandler()
    log_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s", "%H:%M:%S")
    log_handler.setFormatter(formatter)
    logging.getLogger().addHandler(log_handler)
    window = MainWindow(buffers, camera_state, controller, log_handler)
    log_handler.signaller.message.connect(window.append_log)
    window.resize(1280, 720)
    window.show()
    app.exec()

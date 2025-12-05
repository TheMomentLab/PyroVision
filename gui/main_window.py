import logging
import os
import time
from pathlib import Path
from typing import Dict

from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSplitter,
)
from PyQt6.QtCore import Qt, QTimer

from gui.utils import cv_to_qpixmap
from gui.frame_updater import FrameUpdater


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, buffers, camera_state, controller, log_handler=None):
        super().__init__()
        self.buffers = buffers
        self.camera_state = camera_state
        self.controller = controller
        self.log_handler = log_handler

        self.central = QWidget()
        self.setCentralWidget(self.central)
        self.root_layout = QVBoxLayout()
        self.central.setLayout(self.root_layout)

        # === Video Labels ===
        self.rgb_label = QLabel("RGB Preview")
        self.det_label = QLabel("Det Preview")
        self.ir_label = QLabel("IR Preview")
        self.overlay_label = QLabel("Overlay Preview")
        for lbl in (self.rgb_label, self.det_label, self.ir_label, self.overlay_label):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background: #111; color: #eee;")

        # Info labels
        self.rgb_info = QLabel("-")
        self.det_info = QLabel("-")
        self.ir_info = QLabel("-")
        self.overlay_info = QLabel("-")
        self.status_label = QLabel("Status: -")
        self.fusion_info = QLabel("-")

        # Layout: simple placeholders (상세 레이아웃은 기존 app_gui.py 기반으로 추후 조립)
        video_row = QHBoxLayout()
        left = QVBoxLayout(); left.addWidget(self.rgb_label); left.addWidget(self.rgb_info)
        right = QVBoxLayout(); right.addWidget(self.ir_label); right.addWidget(self.ir_info)
        video_row.addLayout(left)
        video_row.addLayout(right)

        det_row = QHBoxLayout()
        det_left = QVBoxLayout(); det_left.addWidget(self.det_label); det_left.addWidget(self.det_info)
        det_right = QVBoxLayout(); det_right.addWidget(self.overlay_label); det_right.addWidget(self.overlay_info)
        det_row.addLayout(det_left)
        det_row.addLayout(det_right)

        self.root_layout.addLayout(video_row)
        self.root_layout.addLayout(det_row)
        self.root_layout.addWidget(self.status_label)
        self.root_layout.addWidget(self.fusion_info)

        # FrameUpdater wiring
        labels: Dict[str, QLabel] = {
            'rgb_label': self.rgb_label,
            'det_label': self.det_label,
            'ir_label': self.ir_label,
            'overlay_label': self.overlay_label,
            'rgb_info': self.rgb_info,
            'det_info': self.det_info,
            'ir_info': self.ir_info,
            'status_label': self.status_label,
            'fusion_info': self.fusion_info,
        }
        plots = {'det_plot': None, 'rgb_plot': None, 'ir_plot': None}
        self.frame_updater = FrameUpdater(buffers, controller, controller.cfg if controller else {}, labels, plots, controller.get_sync_cfg() if controller else {})
        self.frame_updater.set_fire_fusion(getattr(controller, 'fire_fusion', None))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.frame_updater.update_frames)
        self.timer.start(33)  # 약 30fps

    def closeEvent(self, event):
        if self.controller and self.controller.sender_running():
            self.controller.stop_sender()
        if self.controller:
            self.controller.stop_sources()
        if self.log_handler:
            logging.getLogger().removeHandler(self.log_handler)
        super().closeEvent(event)

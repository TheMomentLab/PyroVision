# sender.py
import cv2
import time
import struct
import socket
import numpy as np
import threading
import logging
import json
import zlib
import base64
import os

from core.fire_fusion import FireFusion, prepare_fusion_for_output
from core.state import (
    LabelScaleState,
    DEFAULT_LABEL_SCALE,
    MIN_LABEL_SCALE,
    MAX_LABEL_SCALE,
    LABEL_SCALE_STEP,
)
from core.util import ts_to_epoch_ms

logger = logging.getLogger(__name__)

REQUIRED_IMAGES = {
    "rgb_det": ("data_b64", "shape", "dtype"),
    "ir": ("data_b64", "shape", "dtype"),
}


class ImageSender:
    def __init__(self, host='localhost', port=9999, max_packet_mb=2.0, label_state=None):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.connected = False
        self.saving_mode = False  # Receiver의 저장 상태
        self.control_lock = threading.Lock()
        self.max_packet_bytes = int(max_packet_mb * 1024 * 1024)
        self.label_state = label_state
        self._label_scale = DEFAULT_LABEL_SCALE
        
    def connect(self):
        """서버에 연결"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024*1024*10)  # 10MB 송신 버퍼
            # 논블로킹 모드로 설정 (제어 명령 수신용)
            self.sock.setblocking(False)
            self.sock.connect((self.host, self.port))
            self.connected = True
            logger.info("Connected to %s:%d", self.host, self.port)
            return True
        except BlockingIOError:
            # 논블로킹 connect는 즉시 반환되므로 연결 대기
            import select
            _, writable, _ = select.select([], [self.sock], [], 5.0)
            if writable:
                self.connected = True
                logger.info("Connected to %s:%d", self.host, self.port)
                return True
            else:
                logger.warning("Connection timeout to %s:%d", self.host, self.port)
                self.connected = False
                return False
        except Exception as e:
            logger.error("Connection failed: %s", e)
            self.connected = False
            return False
    
    def check_control_command(self):
        """Receiver로부터 제어 명령 수신 (논블로킹)"""
        try:
            # 논블로킹 읽기 시도
            size_header = self.sock.recv(4)
            
            if not size_header or len(size_header) < 4:
                return
            
            payload_size = struct.unpack('>L', size_header)[0]
            
            # 데이터 읽기
            payload = b''
            while len(payload) < payload_size:
                chunk = self.sock.recv(payload_size - len(payload))
                if not chunk:
                    return
                payload += chunk
            
            # 명령 파싱 (JSON)
            try:
                command_dict = json.loads(payload.decode('utf-8'))
            except json.JSONDecodeError as e:
                logger.warning("Control command decode failed: %s", e)
                return
            command = command_dict.get('command', '')
            
            with self.control_lock:
                if command == 'start_saving':
                    self.saving_mode = True
                    logger.info("Saving mode ENABLED - Sending RGB + IR16")
                elif command == 'stop_saving':
                    self.saving_mode = False
                    logger.info("Saving mode DISABLED - Sending only RGB_DET + IR")
                elif command == 'label_scale_up':
                    new_scale = self._adjust_label_scale(delta=LABEL_SCALE_STEP)
                    logger.info("Label scale increased to %.2f", new_scale)
                elif command == 'label_scale_down':
                    new_scale = self._adjust_label_scale(delta=-LABEL_SCALE_STEP)
                    logger.info("Label scale decreased to %.2f", new_scale)
                elif command == 'label_scale_reset':
                    new_scale = self._adjust_label_scale(reset=True)
                    logger.info("Label scale reset to %.2f", new_scale)
        except BlockingIOError:
            # 데이터가 없음 (정상)
            pass
        except (socket.error, OSError) as e:
            logger.error("Control command socket error: %s", e)
            self.connected = False
        except Exception as e:
            logger.exception("Unexpected control command error: %s", e)
    
    def send_frame_data(self, data_dict):
        """프레임 데이터를 직렬화하여 전송"""
        if not self.connected:
            return False
            
        try:
            # JSON 직렬화 후 zlib 압축
            payload_json = json.dumps(data_dict, separators=(',', ':'), ensure_ascii=False)
            raw_bytes = payload_json.encode('utf-8')
            compressed = zlib.compress(raw_bytes, level=6)
            payload = compressed if len(compressed) < len(raw_bytes) else raw_bytes
            payload_size = len(payload)
            if payload_size > self.max_packet_bytes:
                logger.warning("Packet too large (%d > %d bytes); dropping", payload_size, self.max_packet_bytes)
                return False
            
            # 1. 데이터 크기를 먼저 전송 (4바이트, big-endian)
            size_header = struct.pack('>L', payload_size)
            
            # 논블로킹 소켓이므로 select로 전송 가능 대기
            import select
            _, writable, _ = select.select([], [self.sock], [], 1.0)
            if not writable:
                logger.warning("Socket not writable")
                return False
            
            self.sock.sendall(size_header)
            
            # 2. 실제 데이터 전송
            self.sock.sendall(payload)
            
            return True
        except Exception as e:
            logger.warning("Send failed: %s", e)
            self.connected = False
            return False
    
    def close(self):
        """연결 종료"""
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError as e:
                logger.debug("Socket close error: %s", e)
            self.sock = None
            logger.info("Connection closed")

    def _adjust_label_scale(self, delta=None, reset=False):
        if self.label_state:
            if reset:
                return self.label_state.reset()
            if delta is not None:
                return self.label_state.adjust(delta)
            return self.label_state.get()
        if reset:
            self._label_scale = DEFAULT_LABEL_SCALE
        elif delta is not None:
            self._label_scale = max(
                MIN_LABEL_SCALE,
                min(MAX_LABEL_SCALE, self._label_scale + delta),
            )
        return self._label_scale

    def get_label_scale(self):
        if self.label_state:
            return self.label_state.get()
        with self.control_lock:
            return self._label_scale
def send_images(d_rgb, d_ir, d16_ir, d_rgb_det, host='localhost', port=5000,
                jpeg_quality=70, resize_factor=1, sync_cfg=None, stop_event=None,
                coord_state=None, label_state=None, ir_size=None, rgb_size=None):
    """
    이미지 버퍼를 읽어서 TCP 소켓으로 전송 (JSON+zlib+base64)
    - 최신 프레임만 전송하여 적체를 방지
    - 연결이 끊기면 지수 백오프로 재연결 시도
    
    Args:
        d_rgb: RGB 카메라 버퍼
        d_ir: IR 8bit 버퍼
        d16_ir: IR 16bit 버퍼
        d_rgb_det: RGB 검출 결과 버퍼
        host: 서버 호스트
        port: 서버 포트
        jpeg_quality: JPEG 압축 품질 (0-100, 낮을수록 빠름)
        resize_factor: 전송 전 리사이즈 비율 (2=1/2, 3=1/3, 1=원본)
    """
    label_state = label_state or LabelScaleState(DEFAULT_LABEL_SCALE)
    ir_size = tuple(ir_size or (160, 120))
    rgb_size = tuple(rgb_size or (960, 540))
    sender = ImageSender(host, port, label_state=label_state)
    
    # 연결 재시도 (초기)
    max_retries = 5
    retry_count = 0
    while retry_count < max_retries:
        if sender.connect():
            break
        retry_count += 1
        logger.info("Retrying connection (%d/%d)...", retry_count, max_retries)
        time.sleep(2)
    
    if not sender.connected:
        logger.error("Failed to connect after retries. Sender exiting.")
        return
    
    # Fire Fusion 초기화
    def build_fusion(params):
        return FireFusion(
            ir_size=ir_size,
            rgb_size=rgb_size,
            offset_x=params.get('offset_x', 0.0),
            offset_y=params.get('offset_y', 0.0),
            scale=params.get('scale'),
        )

    coord_params = {'offset_x': 0.0, 'offset_y': 0.0, 'scale': None}
    coord_version = -1
    if coord_state:
        coord_params, coord_version = coord_state.get()

    fire_fusion = build_fusion(coord_params)
    
    frame_count = 0
    ir_frame_count = 0
    rgb_frame_count = 0
    start_time = time.time()
    last_print_time = start_time

    sync_enabled = bool(sync_cfg and sync_cfg.get('ENABLED'))
    sync_max_diff = (sync_cfg or {}).get('MAX_DIFF_MS', 120)
    
    # 중복 전송 방지용 (마지막 전송한 프레임의 타임스탬프)
    last_sent_timestamps = {
        'rgb': None,
        'ir': None,
        'ir16': None,
        'rgb_det': None
    }
    
    # 마지막 IR hotspots (fusion용)
    last_ir_hotspots = []
    
    # 성능 측정용
    send_times = []
    
    def _b64(data: bytes) -> str:
        return base64.b64encode(data).decode('ascii')

    def _valid_image_entry(name, entry):
        required = REQUIRED_IMAGES.get(name, ())
        return entry is not None and all(k in entry for k in required)

    def _calc_packet_size_bytes(obj) -> int:
        payload_json = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
        raw = payload_json.encode('utf-8')
        comp = zlib.compress(raw, level=6)
        return min(len(raw), len(comp))

    backoff_base = 0.5   # 초, 재연결 초기 대기
    backoff_max = 5.0    # 초, 재연결 최대 대기
    backoff_attempts = 0
    vis_mode = os.getenv("FUSION_VIS_MODE", "test").lower()

    def _backoff_sleep():
        nonlocal backoff_attempts
        delay = min(backoff_max, backoff_base * (2 ** backoff_attempts))
        backoff_attempts = min(backoff_attempts + 1, 8)
        time.sleep(delay)

    try:
        while True:
            if stop_event and stop_event.is_set():
                logger.info("Sender stop requested")
                break

            if not sender.connected:
                logger.warning("Disconnected. Attempting to reconnect (attempt %d)...", backoff_attempts + 1)
                if sender.connect():
                    logger.info("Reconnected successfully")
                    backoff_attempts = 0
                else:
                    _backoff_sleep()
                    continue
            
            # Receiver로부터 제어 명령 확인
            sender.check_control_command()

            if coord_state:
                params, version = coord_state.get()
                if version != coord_version:
                    coord_version = version
                    coord_params = params
                    fire_fusion = build_fusion(coord_params)
                    logger.info("FireFusion calibration updated: %s", coord_params)
            
            timestamp = time.time()
            vis_mode = os.getenv("FUSION_VIS_MODE", vis_mode).lower()
            
            # 각 버퍼에서 데이터 읽기
            rgb_item = d_rgb.read(timeout=0.05) if d_rgb else None
            ir_item = d_ir.read(timeout=0.05) if d_ir else None
            ir16_item = d16_ir.read(timeout=0.05) if d16_ir else None
            rgb_det_item = d_rgb_det.read(timeout=0.05) if d_rgb_det else None
            
            # ===== 독립적 타임스탬프 체크 =====
            ir_updated = False
            rgb_det_updated = False
            
            # IR 업데이트 체크
            if ir_item and len(ir_item) > 1 and ir_item[0] is not None:
                current_ir_ts = ir_item[1]
                if current_ir_ts != last_sent_timestamps['ir']:
                    ir_updated = True
                    last_sent_timestamps['ir'] = current_ir_ts
            
            # RGB_DET 업데이트 체크
            if rgb_det_item and len(rgb_det_item) > 1 and rgb_det_item[0] is not None:
                current_rgb_det_ts = rgb_det_item[1]
                if current_rgb_det_ts != last_sent_timestamps['rgb_det']:
                    rgb_det_updated = True
                    last_sent_timestamps['rgb_det'] = current_rgb_det_ts
            
            # 둘 다 업데이트 없으면 스킵
            if not ir_updated and not rgb_det_updated:
                time.sleep(0.005)
                continue
            
            # 전송할 데이터 패킷 구성
            packet = {
                'timestamp': timestamp,
                'frame_id': frame_count,
                'images': {}
            }
            
            # ===== 저장 모드 확인 =====
            with sender.control_lock:
                is_saving = sender.saving_mode
            
            encode_param = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
            
            # ===== IR 프레임 (항상 최신 프레임 포함) =====
            if ir_item and ir_item[0] is not None:
                ir_frame = ir_item[0]
                # 최고 온도 정보 추출 (ir_item[2]에 저장됨)
                max_temp_info = ir_item[2] if len(ir_item) > 2 else None
                # IR hotspots 추출 (ir_item[3]에 저장됨)
                if len(ir_item) > 3:
                    last_ir_hotspots = ir_item[3]
                tau_val = None
                if isinstance(max_temp_info, dict) and 'tau' in max_temp_info:
                    tau_val = max_temp_info['tau']
                
                packet['images']['ir'] = {
                    'data_b64': _b64(ir_frame.tobytes()),
                    'compressed': False,
                    'shape': ir_frame.shape,
                    'dtype': str(ir_frame.dtype),
                    'timestamp': ir_item[1] if len(ir_item) > 1 else 0,
                    'updated': ir_updated,  # 업데이트 여부 표시
                    'max_temp': max_temp_info,  # 최고 온도 정보 (x, y, temp_raw, temp_corrected)
                    'tau': tau_val,             # 사용된 대기 투과율 (표시용)
                }
                if ir_updated:
                    ir_frame_count += 1
                
                # IR 16bit (저장 모드일 때만)
                if is_saving and ir16_item and ir16_item[0] is not None:
                    ir16_frame = ir16_item[0]
                    packet['images']['ir16'] = {
                        'data_b64': _b64(ir16_frame.tobytes()),
                        'compressed': False,
                        'shape': ir16_frame.shape,
                        'dtype': str(ir16_frame.dtype),
                        'timestamp': ir16_item[1] if len(ir16_item) > 1 else 0
                    }
            
            # ===== RGB Detection 프레임 (항상 최신 프레임 포함) =====
            fusion_result = None
            if rgb_det_item and rgb_det_item[0] is not None:
                rgb_det_frame = rgb_det_item[0].copy()  # 복사본 사용
                
                # detection 결과 추출 (rgb_det_item[2]에 저장됨)
                eo_detections = []
                if len(rgb_det_item) > 2 and rgb_det_item[2]:
                    # fire(class_id=1)만 필터링
                    for det in rgb_det_item[2]:
                        if len(det) > 5 and det[5] == 1:  # class_id == 1 (fire)
                            eo_detections.append(det[:5])  # (x, y, w, h, conf)
                
                # ===== Fire Fusion (IR 게이트키퍼) =====
                fusion_result = fire_fusion.fuse(last_ir_hotspots, eo_detections)
                
                fusion_payload = None
                if fusion_result:
                    if label_state:
                        current_label_scale = label_state.get()
                    else:
                        current_label_scale = sender.get_label_scale()
                    fusion_payload, rgb_det_frame = prepare_fusion_for_output(
                        fusion_result,
                        det_frame=rgb_det_frame,
                        vis_mode=vis_mode,
                        label_scale=current_label_scale,
                        default_label_scale=DEFAULT_LABEL_SCALE,
                        json_safe=True,
                    )
                
                # 리사이즈
                if resize_factor > 1:
                    h, w = rgb_det_frame.shape[:2]
                    rgb_det_frame = cv2.resize(rgb_det_frame, (w//resize_factor, h//resize_factor),
                                               interpolation=cv2.INTER_LINEAR)

                # JPEG 압축
                _, encoded = cv2.imencode('.jpg', rgb_det_frame, encode_param)
                packet['images']['rgb_det'] = {
                    'data_b64': _b64(encoded.tobytes()),
                    'compressed': True,
                    'shape': rgb_det_frame.shape,
                    'dtype': str(rgb_det_frame.dtype),
                    'timestamp': rgb_det_item[1] if len(rgb_det_item) > 1 else 0,
                    'resized': resize_factor > 1,
                    'updated': rgb_det_updated  # 업데이트 여부 표시
                }
                if rgb_det_updated:
                    rgb_frame_count += 1
            
            # 동기화 검사
            if sync_enabled:
                if ir_item and rgb_det_item:
                    t_ir = ts_to_epoch_ms(ir_item[1])
                    t_rgb = ts_to_epoch_ms(rgb_det_item[1])
                    if t_ir and t_rgb:
                        diff_ms = abs(t_ir - t_rgb)
                        if diff_ms > sync_max_diff:
                            logger.debug("Sync skip: diff=%.1fms (max=%s)", diff_ms, sync_max_diff)
                            time.sleep(0.005)
                            continue
                else:
                    time.sleep(0.005)
                    continue

            # ===== Fusion 결과를 패킷에 추가 =====
            if fusion_payload:
                packet['fire_fusion'] = {
                    'fire_detected': fusion_payload.get('fire_detected', False),
                    'confidence': fusion_payload.get('confidence', 0.0),
                    'status': fusion_payload.get('status', 'NO_FIRE'),
                    'confirmed_count': fusion_payload.get('confirmed_count', 0),
                    'ir_only_count': fusion_payload.get('ir_only_count', 0),
                    'eo_annotations': fusion_payload.get('eo_annotations', []),
                }
                
                # RGB 원본 (저장 모드일 때만)
                if is_saving and rgb_item and rgb_item[0] is not None:
                    rgb_frame = rgb_item[0]
                    if resize_factor > 1:
                        h, w = rgb_frame.shape[:2]
                        rgb_frame = cv2.resize(rgb_frame, (w//resize_factor, h//resize_factor), 
                                              interpolation=cv2.INTER_LINEAR)
                    _, encoded = cv2.imencode('.jpg', rgb_frame, encode_param)
                    packet['images']['rgb'] = {
                        'data_b64': _b64(encoded.tobytes()),
                        'compressed': True,
                        'shape': rgb_frame.shape,
                        'dtype': str(rgb_frame.dtype),
                        'timestamp': rgb_item[1] if len(rgb_item) > 1 else 0,
                        'resized': resize_factor > 1
                    }
            
            # 패킷 검증: 필수 이미지 엔트리에 data/shape/dtype 없으면 전송하지 않음
            if any(
                img in packet['images'] and not _valid_image_entry(img, packet['images'][img])
                for img in REQUIRED_IMAGES
            ):
                logger.warning("Invalid packet schema detected; skipping send")
                time.sleep(0.001)
                continue

            # 전송
            send_start = time.perf_counter()
            if sender.send_frame_data(packet):
                send_times.append((time.perf_counter() - send_start) * 1000)
                frame_count += 1
                
                # FPS 출력 (1초마다)
                current_time = time.time()
                if current_time - last_print_time >= 1.0:
                    elapsed = current_time - start_time
                    total_fps = frame_count / elapsed if elapsed > 0 else 0
                    ir_fps = ir_frame_count / elapsed if elapsed > 0 else 0
                    rgb_fps = rgb_frame_count / elapsed if elapsed > 0 else 0
                    
                    avg_send = sum(send_times) / len(send_times) if send_times else 0
                    
                    # 패킷 크기 계산
                    packet_size_kb = _calc_packet_size_bytes(packet) / 1024.0
                    
                    mode_str = "SAVING" if is_saving else "DISPLAY"
                    image_keys = list(packet['images'].keys())
                    logger.info(
                        "[Sender] Packets:%d IR:%.1ffps RGB:%.1ffps Mode:%s Images:%s Size:%.1fKB Send:%.2fms",
                        frame_count, ir_fps, rgb_fps, mode_str, image_keys, packet_size_kb, avg_send
                    )
                    
                    last_print_time = current_time
                    send_times.clear()
            else:
                logger.warning("Failed to send frame, retrying with backoff...")
                _backoff_sleep()
            
            # 짧은 딜레이 (CPU 부하 감소)
            time.sleep(0.001)
            
    except KeyboardInterrupt:
        logger.info("Sender stopped by user")
    except Exception as e:
        logger.exception("Sender error: %s", e)
    finally:
        sender.close()
        elapsed = time.time() - start_time
        if elapsed > 0:
            logger.info(
                "Sender total frames: %d, average FPS: %.2f",
                frame_count, frame_count / elapsed
            )

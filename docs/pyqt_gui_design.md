# PyroVision GUI 설계서

> PyQt6 기반 화재 감지 시스템 GUI 인터페이스

## 1. 목표
- PyQt6 기반 GUI를 유지보수 가능하게 분리하고, CLI와 동일한 파이프라인을 공유한다.
- 영상 표시/제어/모니터링을 역할별 파일로 나눠 코드 가독성과 테스트 용이성을 높인다.

## 2. 분리 후 모듈 구조 (Phase 3.1)
- `gui/main_window.py` : 창 생성/레이아웃 조립/신호 배선, 컨트롤러/버퍼 주입
- `gui/frame_updater.py` : 버퍼 읽기 → 픽셀 변환 → 퓨전/오버레이 → QLabel 업데이트
- `gui/control_panel.py` : 입력/동기화/캡처/IR/탐지 파라미터 UI 및 신호 발행
- `gui/monitor_panel.py` : 로그 뷰, 상태 텍스트, FPS 그래프(옵션)
- 공용 유틸: 기존 `QtLogHandler`, `_cv_to_qpixmap`, `_calc_fps`, `build_overlay` 등은 필요 시 `gui/utils.py`로 이동

## 3. 데이터/신호 흐름
- QTimer → `FrameUpdater.update_frames()`
- ControlPanel 신호 → MainWindow 슬롯 → RuntimeController 메서드 호출
  - 예: `inputChanged(rgb_cfg, ir_cfg)`, `syncChanged(cfg)`, `captureRequested(args)`, `irParamChanged(...)`, `detectorParamChanged(...)`, `labelScaleChanged(delta/reset)`
- FrameUpdater 상태 → MainWindow → MonitorPanel 갱신
  - FPS/TS, SYNC 상태, 좌표/스케일(자동 설정 시) 등
- QtLogHandler → MonitorPanel 로그 append

## 4. 레이아웃 개요
```
MainWindow
 ├─ Top/Status (MonitorPanel의 상태 뷰)
 ├─ VideoGrid (4 QLabel: RGB, Det, IR, Overlay)
 ├─ Bottom Split: ControlPanel | MonitorPanel(Log/FPS)
```

## 5. 단계별 리팩토링 계획
1) 골격 분리: 새 파일 생성, 클래스 선언/생성자 정의, 기존 MainWindow import만 수정 (기능은 일시 중복 허용)
2) `update_frames` 로직을 FrameUpdater로 이전, MainWindow는 호출/레이블 참조만 보유
3) ControlPanel/MonitorPanel UI/신호 분리, MainWindow에서 레이아웃 조립 및 신호 연결
4) 공용 유틸/핸들러 이동(`gui/utils.py`), 불필요한 중복 정리
5) 수동 테스트: GUI 실행 → 영상 표시/제어/로그 정상 동작 확인

## 6. 인터페이스 초안
- `FrameUpdater(buffers, controller, config, labels, plots, sync_cfg)`
  - `update_frames()`: 반환값 없이 레이블/상태 갱신, fusion은 `prepare_fusion_for_output` 사용
- `ControlPanel(QWidget)`
  - 시그널: `inputChanged`, `syncChanged`, `captureRequested`, `irParamChanged`, `detectorParamChanged`, `labelScaleChanged`, `coordChanged`
  - 메서드: `set_coord_params`, `set_input_status`, `set_capture_status`
- `MonitorPanel(QWidget)`
  - 메서드: `append_log(text)`, `update_status(det_fps, rgb_fps, ir_fps, sync_info, timestamps)`

## 7. 남은 과제/주의
- QTimer와 프레임 업데이트 순환참조 방지 (FrameUpdater는 QLabel 참조만 받고 컨트롤러는 MainWindow가 관리)
- Fusion/좌표 자동 설정 로직을 FrameUpdater로 이동 시, ControlPanel과의 scale UI 동기화 신호 필요
- 레이아웃/스타일은 기존 디자인 유지, import 경로 변경에 따른 사이드이펙트 점검

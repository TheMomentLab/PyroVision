# 보드–PC 연결 가이드

보드와 PC를 연결하는 두 가지 기본 방법을 정리했습니다. 상황에 맞게 선택하세요.

---

## 1) 유선 연결 (테스트/비상용 관리 채널)
- **용도**: Wi-Fi 없는 환경에서 임시 테스트, 또는 비상 접속.
- **구성 요약**
  1. **PC → 보드 인터넷 공유**: `scripts/pc/setup_pc_wired_gateway.sh`  
     - PC 유선 인터페이스를 192.168.200.1로 설정하고 DHCP/NAT 제공.
  2. **보드 → DHCP 전환**: `scripts/board/setup_board_dhcp.sh`  
     - `/etc/systemd/network/10-eth0-static.network`를 DHCP로 변경.
  3. **연결 확인**: PC에서 `ping 192.168.200.x`(보드), 보드에서 `ping 8.8.8.8`.
- **워크플로우**
  - PC: `sudo /root/lk_fire/scripts/pc/setup_pc_wired_gateway.sh`
  - 보드: `sudo /root/lk_fire/scripts/board/setup_board_dhcp.sh`
  - SSH: `ssh root@<보드 IP>` (기본 DHCP 범위 192.168.200.50~150, MAC 기반 고정 IP가 있으면 해당 주소 사용)
- **주의**: 실제 운용은 Wi-Fi가 목표라면, 유선은 “있으면 편한 비상 경로”로만 유지하세요.

---

## 2) 무선 연결 (운용/현장용)
- **용도**: 최종 운용; PC와 보드가 같은 Wi-Fi에 접속.
- **구성 요약**
  1. **보드 Wi-Fi 설정**: `scripts/board/setup_board_wifi.sh`  
     - 실행 시 주변 SSID 목록 표시 → 번호 선택 → 비밀번호 입력 → 즉시 연결/저장.  
     - 기존 wpa_supplicant가 있으면 자동으로 붙어서 사용.
  2. **PC Wi-Fi 설정**: 같은 SSID/비밀번호로 접속.
  3. **연결 확인**: PC ↔ 보드 `ping`, `ssh root@<보드 IP>`.  
     - mDNS/호스트네임을 쓰려면 `avahi-daemon` + 고유 호스트네임(`lkfire-1.local` 등) 설정 권장.
  4. **파일 동기화(개발 시)**: PC에서 `scripts/pc/scp_sync.sh`로 보드와 주기적 동기화.
- **주요 변수/옵션**
  - `CTRL_DIR`/`SKIP_START` 등은 자동으로 맞춰지므로 보통 건드릴 필요 없음.
  - 숨김 SSID면 `HIDDEN=true`로 실행 가능.
- **문제 해결**
  - 연결 안 될 때: `wpa_cli -p /run/wpa_supplicant_setup -i mlan0 status/scan_results` 확인.
  - 비밀번호 오류/거부 시 `remove_network all` 후 다시 추가.

---

## 추천 절차 (새 현장 투입 시)
1. **PC**: 현장 Wi-Fi 접속 후 SSID/비밀번호 확인.
2. **보드**: `setup_board_wifi.sh`로 SSID/비밀번호 입력해 연결 확인(`wpa_state=COMPLETED`).
3. **PC↔보드 통신 확인**: `ping`, `ssh`로 접속; 필요한 경우 `scp_sync.sh`로 동기화.
4. **앱/서비스 실행**: `scripts/board/setup_lk_fire.sh`로 서비스 설치 후 `systemctl status lk_fire.service` 확인.

---

## 참고
- (추가 참고 문서 없음)

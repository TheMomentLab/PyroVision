#!/usr/bin/env bash
# Add a link-local IP on PC and test connectivity to the board's 169.254.x.x.

if [ -z "$BASH_VERSION" ]; then
    echo "[linklocal][error] This script requires bash. Run with 'bash $0'." >&2
    exit 1
fi

set -euo pipefail

log() { echo "[linklocal] $*"; }
warn() { echo "[linklocal][warn] $*" >&2; }
error() { echo "[linklocal][error] $*" >&2; exit 1; }

IFACE=${IFACE:-}
BOARD_IP=${BOARD_IP:-}
LOCAL_IP=${LOCAL_IP:-169.254.200.1}

require_root() {
    if [ "$(id -u)" != "0" ]; then
        error "Run as root (sudo)."
    fi
}

choose_iface() {
    if [[ -n "$IFACE" ]]; then
        return
    fi
    local idx=1
    local choices=()
    echo "사용할 유선 인터페이스를 선택하세요:"
    while IFS= read -r line; do
        local name
        name=$(echo "$line" | awk '{print $2}' | tr -d ':')
        [[ "$name" == "lo" ]] && continue
        echo "  $idx) $name"
        choices+=("$name")
        idx=$((idx + 1))
    done < <(ip link show | grep -E "^[0-9]+:")
    if [[ ${#choices[@]} -eq 0 ]]; then
        error "인터페이스를 찾을 수 없습니다."
    fi
    read -rp "선택 [1]: " sel
    sel=${sel:-1}
    if ! [[ "$sel" =~ ^[0-9]+$ ]] || (( sel < 1 || sel > ${#choices[@]} )); then
        error "잘못된 선택입니다."
    fi
    IFACE="${choices[$((sel-1))]}"
}

main() {
    require_root
    choose_iface

    read -rp "보드 링크로컬 IP (예: 169.254.x.x): " input_board
    BOARD_IP=${input_board:-$BOARD_IP}
    if [[ -z "$BOARD_IP" ]]; then
        error "보드 IP는 필수입니다."
    fi

    read -rp "PC에 추가할 링크로컬 IP [${LOCAL_IP}]: " input_local
    LOCAL_IP=${input_local:-$LOCAL_IP}

    # 이미 등록되어 있으면 건너뜀
    if ip addr show "$IFACE" | grep -q "${LOCAL_IP}/16"; then
        log "이미 ${IFACE}에 ${LOCAL_IP}/16 이 설정되어 있습니다."
    else
        log "${IFACE}에 ${LOCAL_IP}/16 추가 중..."
        ip addr add "${LOCAL_IP}/16" dev "$IFACE"
        log "추가 완료."
    fi

    log "보드(${BOARD_IP})에 핑 테스트..."
    if ping -c 3 -W 1 "$BOARD_IP" >/dev/null 2>&1; then
        log "핑 성공. SSH 예시: ssh root@${BOARD_IP}"
    else
        warn "핑 실패. 케이블/보드 IP를 다시 확인하세요."
    fi
}

main "$@"

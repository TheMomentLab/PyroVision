#!/usr/bin/env bash
# 보드 Wi-Fi 프로파일 추가 및 즉시 연결 스크립트

set -euo pipefail

WLAN_IF=${WLAN_IF:-wlan0}
WPA_CONF=${WPA_CONF:-}
PRIORITY=${PRIORITY:-10}
HIDDEN=${HIDDEN:-false}
COUNTRY=${COUNTRY:-KR}
CTRL_GROUP=${CTRL_GROUP:-}
CTRL_DIR=${CTRL_DIR:-/run/wpa_supplicant_setup}
SKIP_START=${SKIP_START:-false}
CTRL_GROUP=${CTRL_GROUP:-}
SCAN_LIMIT=${SCAN_LIMIT:-30}

log() { echo "[wifi] $*"; }
warn() { echo "[wifi][warn] $*" >&2; }
error() { echo "[wifi][error] $*" >&2; exit 1; }

usage() {
    cat <<EOF
사용법: sudo ${0} <SSID> [PSK]

환경변수:
  WLAN_IF   무선 인터페이스 이름 (기본: wlan0)
  WPA_CONF  wpa_supplicant 설정 파일 경로(없으면 자동 탐색/생성)
  PRIORITY  네트워크 우선순위 (기본: 10, 높을수록 우선)
  HIDDEN    숨김 SSID면 true 로 설정 (기본: false)
  COUNTRY   새 설정 파일 생성 시 국가 코드 (기본: KR)
  SCAN_LIMIT 스캔 결과 최대 표시 개수(0이면 제한 없음, 기본: 30)
EOF
    exit 1
}

require_root() {
    if [ "$(id -u)" != "0" ]; then
        error "root 권한으로 실행하세요. (sudo 사용)"
    fi
}

ensure_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "필요한 명령을 찾을 수 없습니다: $1"
    fi
}

detect_ctrl_group() {
    if [[ -n "$CTRL_GROUP" ]]; then
        return
    fi
    if command -v getent >/dev/null 2>&1 && getent group netdev >/dev/null 2>&1; then
        CTRL_GROUP="netdev"
    else
        CTRL_GROUP="root"
        warn "netdev 그룹이 없어 ctrl_interface 그룹을 root로 설정합니다."
    fi
}

detect_wlan_interface() {
    # 지정된 인터페이스가 존재하면 그대로 사용
    if ip link show "$WLAN_IF" >/dev/null 2>&1; then
        return
    fi

    # iw dev로 탐색
    if command -v iw >/dev/null 2>&1; then
        mapfile -t iw_ifs < <(iw dev 2>/dev/null | awk '$1=="Interface"{print $2}')
        if [[ ${#iw_ifs[@]} -gt 0 ]]; then
            WLAN_IF="${iw_ifs[0]}"
            log "인터페이스 자동 선택(iw dev): ${WLAN_IF}"
            return
        fi
    fi

    # ip link 이름 패턴으로 탐색
    mapfile -t candidates < <(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^(wlan|wlp|wl|mlan|p2p)')
    if [[ ${#candidates[@]} -gt 0 ]]; then
        WLAN_IF="${candidates[0]}"
        log "인터페이스 자동 선택(ip link): ${WLAN_IF}"
        return
    fi

    error "무선 인터페이스를 찾을 수 없습니다. WLAN_IF 환경변수로 인터페이스 이름을 지정하세요."
}

auto_select_ctrl_dir() {
    # 이미 동작 중인 wpa_supplicant가 있으면 해당 ctrl_dir을 재사용
    if [[ "$SKIP_START" == "true" ]]; then
        return
    fi
    local candidates=("$CTRL_DIR" "/run/wpa_supplicant" "/run/wpa_supplicant_setup")
    local dir
    for dir in "${candidates[@]}"; do
        [[ -z "$dir" ]] && continue
        if wpa_cli -p "$dir" -i "$WLAN_IF" status >/dev/null 2>&1; then
            CTRL_DIR="$dir"
            SKIP_START=true
            log "이미 실행 중인 wpa_supplicant 감지: ${CTRL_DIR} (재사용)"
            return
        fi
    done
}

create_default_config() {
    local path="$1"
    mkdir -p "$(dirname "$path")"
    cat > "$path" <<EOF
ctrl_interface=DIR=${CTRL_DIR} GROUP=${CTRL_GROUP}
update_config=1
country=${COUNTRY}
EOF
    chmod 600 "$path"
    log "기본 설정 파일 생성: $path"
}

choose_config_file() {
    if [[ -n "$WPA_CONF" ]]; then
        return
    fi
    local candidate="/etc/wpa_supplicant/wpa_supplicant-${WLAN_IF}.conf"
    if [[ -f "$candidate" ]]; then
        WPA_CONF="$candidate"
        return
    fi
    candidate="/etc/wpa_supplicant/wpa_supplicant.conf"
    if [[ -f "$candidate" ]]; then
        WPA_CONF="$candidate"
        return
    fi
    WPA_CONF="/etc/wpa_supplicant/wpa_supplicant-${WLAN_IF}.conf"
    create_default_config "$WPA_CONF"
}

ensure_ctrl_group_setting() {
    [[ -f "$WPA_CONF" ]] || return
    if grep -q "^ctrl_interface=" "$WPA_CONF"; then
        sed -i "s|^ctrl_interface=.*|ctrl_interface=DIR=${CTRL_DIR} GROUP=${CTRL_GROUP}|" "$WPA_CONF"
    else
        printf "ctrl_interface=DIR=%s GROUP=%s\n" "$CTRL_DIR" "$CTRL_GROUP" | cat - "$WPA_CONF" > "${WPA_CONF}.tmp"
        mv "${WPA_CONF}.tmp" "$WPA_CONF"
    fi
}

clean_control_socket() {
    mkdir -p "${CTRL_DIR}"
    chmod 755 "${CTRL_DIR}"
    rm -f "${CTRL_DIR}/${WLAN_IF}" "/run/wpa_supplicant_${WLAN_IF}.pid" 2>/dev/null || true
}

stop_existing_wpa() {
    if command -v systemctl >/dev/null 2>&1; then
        if command -v timeout >/dev/null 2>&1; then
            timeout 5 systemctl stop "wpa_supplicant@${WLAN_IF}" 2>/dev/null || true
        else
            systemctl stop "wpa_supplicant@${WLAN_IF}" 2>/dev/null || true
        fi
    fi
    pkill -f "wpa_supplicant.*-i ${WLAN_IF}" 2>/dev/null || true
}

ensure_wpa_running() {
    if wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status >/dev/null 2>&1; then
        return
    fi
    if [[ "$SKIP_START" == "true" ]]; then
        error "wpa_supplicant가 실행 중이지 않습니다(SKIP_START=true). 프로세스를 기동하거나 SKIP_START를 제거하세요."
    fi

    log "wpa_supplicant 기동 시도..."
    # 인터페이스 up 시도 (없어도 무시)
    if command -v ip >/dev/null 2>&1; then
        ip link set "$WLAN_IF" up 2>/dev/null || true
    fi
    stop_existing_wpa
    clean_control_socket
    if command -v rfkill >/dev/null 2>&1; then
        if rfkill list "$WLAN_IF" 2>/dev/null | grep -q "Soft blocked: yes"; then
            warn "rfkill로 차단됨. 'rfkill unblock wifi' 또는 'rfkill unblock ${WLAN_IF}'을 실행하세요."
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        if command -v timeout >/dev/null 2>&1; then
            timeout 5 systemctl start "wpa_supplicant@${WLAN_IF}" 2>/dev/null || warn "systemd 유닛 기동 실패/타임아웃, 수동 시도"
        else
            systemctl start "wpa_supplicant@${WLAN_IF}" 2>/dev/null || warn "systemd 유닛 기동 실패, 수동 시도"
        fi
    fi
    if ! wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status >/dev/null 2>&1; then
        if ! command -v wpa_supplicant >/dev/null 2>&1; then
            error "wpa_supplicant 명령을 찾을 수 없습니다."
        fi
        mkdir -p "$CTRL_DIR"
        wpa_supplicant -B -i "$WLAN_IF" -c "$WPA_CONF" -p "$CTRL_DIR" -P "/run/wpa_supplicant_${WLAN_IF}.pid" || warn "wpa_supplicant 수동 기동 실패"
        sleep 1
    fi
    if ! wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status >/dev/null 2>&1; then
        # 다른 프로세스가 ctrl_iface를 잡고 있는지 확인
        if ss -xl | grep -q "${CTRL_DIR}/${WLAN_IF}"; then
            warn "이미 다른 wpa_supplicant가 ${CTRL_DIR}/${WLAN_IF}를 사용 중입니다. 그대로 사용을 시도합니다."
            if wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status >/dev/null 2>&1; then
                return
            fi
        fi
        error "wpa_supplicant에 연결하지 못했습니다. 프로세스/소켓을 정리 후 다시 시도하세요."
    fi
}

scan_and_choose_ssid() {
    log "Wi-Fi 스캔 중..."
    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" scan >/dev/null 2>&1 || warn "스캔 시작 실패 (인터페이스 확인 필요)"
    sleep 2

    mapfile -t results < <(wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" scan_results 2>/dev/null | tail -n +3)
    if [[ ${#results[@]} -eq 0 ]]; then
        warn "스캔 결과가 없습니다. 수동으로 SSID를 입력하세요."
        return
    fi

    local limit="$SCAN_LIMIT"
    if ! [[ "$limit" =~ ^[0-9]+$ ]]; then
        warn "SCAN_LIMIT 값이 올바르지 않습니다(${limit}). 기본값 30으로 사용합니다."
        limit=30
    fi

    declare -A seen
    options=()
    flags_list=()
    for line in "${results[@]}"; do
        ssid=$(echo "$line" | awk '{print substr($0, index($0,$5))}')
        [[ -z "$ssid" ]] && continue
        if [[ -n "${seen[$ssid]:-}" ]]; then
            continue
        fi
        seen[$ssid]=1
        signal=$(echo "$line" | awk '{print $3}')
        flags=$(echo "$line" | awk '{print $4}')
        options+=("$ssid")
        flags_list+=("$flags|$signal")
        if (( limit > 0 && ${#options[@]} >= limit )); then
            break
        fi
    done

    if [[ ${#options[@]} -eq 0 ]]; then
        warn "표시할 SSID가 없습니다. 수동으로 SSID를 입력하세요."
        return
    fi

    echo "---- 발견된 SSID ----"
    for i in "${!options[@]}"; do
        ssid="${options[$i]}"
        flags="${flags_list[$i]%|*}"
        signal="${flags_list[$i]##*|}"
        printf "  %2d) %s (신호: %sdBm, flags: %s)\n" "$((i+1))" "$ssid" "$signal" "$flags"
    done
    echo "---------------------"
    read -rp "번호 선택(엔터=수동 입력): " choice
    if [[ -z "$choice" ]]; then
        return
    fi
    if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#options[@]} )); then
        warn "잘못된 선택입니다. 수동으로 SSID를 입력하세요."
        return
    fi
    CHOSEN_SSID="${options[$((choice-1))]}"
    CHOSEN_FLAGS="${flags_list[$((choice-1))]%|*}"
    log "선택된 SSID: $CHOSEN_SSID"
}

needs_psk() {
    local flags="$1"
    if echo "$flags" | grep -Eq "WPA|RSN|WEP"; then
        return 0
    fi
    return 1
}

add_and_connect() {
    local ssid="$1"
    local psk="$2"
    local id

    id=$(wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" add_network | tail -n1 | tr -d '\r')
    if ! [[ "$id" =~ ^[0-9]+$ ]]; then
        error "네트워크 추가 실패 (id: $id)"
    fi

    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" set_network "$id" ssid "\"$ssid\"" >/dev/null
    if [[ -n "$psk" ]]; then
        wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" set_network "$id" psk "\"$psk\"" >/dev/null
        wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" set_network "$id" key_mgmt WPA-PSK >/dev/null
    else
        wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" set_network "$id" key_mgmt NONE >/dev/null
    fi
    if [[ "$HIDDEN" == "true" ]]; then
        wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" set_network "$id" scan_ssid 1 >/dev/null
    fi
    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" set_network "$id" priority "$PRIORITY" >/dev/null

    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" enable_network "$id" >/dev/null
    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" select_network "$id" >/dev/null
    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" save_config >/dev/null

    log "네트워크 추가 및 선택 완료 (id=$id, priority=$PRIORITY)"
}

show_status() {
    log "연결 상태:"
    wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status | grep -E "wpa_state=|ssid=|ip_address=" || true
    ip addr show "$WLAN_IF" | grep "inet " || true
}

print_current_connection() {
    local state ssid ip
    state=$(wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status 2>/dev/null | awk -F= '/^wpa_state=/{print $2}' | head -n1)
    ssid=$(wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status 2>/dev/null | awk -F= '/^ssid=/{print $2}' | head -n1)
    ip=$(wpa_cli -p "$CTRL_DIR" -i "$WLAN_IF" status 2>/dev/null | awk -F= '/^ip_address=/{print $2}' | head -n1)
    if [[ -z "$state" ]]; then
        log "현재 연결 상태를 확인할 수 없습니다."
    else
        log "현재 연결: state=${state:--}, ssid=${ssid:--}, ip=${ip:--}"
    fi
}

main() {
    if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
        usage
    fi

    require_root
    ensure_command wpa_cli

    local ssid="${1:-}"
    local psk="${2:-}"

    detect_wlan_interface
    auto_select_ctrl_dir
    detect_ctrl_group
    choose_config_file
    ensure_ctrl_group_setting
    log "설정 파일: $WPA_CONF"
    log "인터페이스: $WLAN_IF"

    ensure_wpa_running
    print_current_connection
    scan_and_choose_ssid

    if [[ -z "$ssid" ]]; then
        if [[ -n "${CHOSEN_SSID:-}" ]]; then
            ssid="$CHOSEN_SSID"
        else
            read -rp "SSID: " ssid
        fi
    fi
    if [[ -z "$psk" ]]; then
        if [[ -n "${CHOSEN_FLAGS:-}" ]] && needs_psk "$CHOSEN_FLAGS"; then
            read -rp "비밀번호(필수): " psk
        elif [[ -n "${CHOSEN_FLAGS:-}" ]]; then
            log "개방형 네트워크로 감지됨. 비밀번호 없이 진행합니다."
            psk=""
        else
            read -rp "비밀번호(빈칸이면 오픈 네트워크로 설정): " psk
        fi
    fi

    add_and_connect "$ssid" "$psk"
    sleep 2
    show_status

    log "완료. 연결이 안 되면 'wpa_cli -i ${WLAN_IF} status'로 상태를 확인하세요."
}

main "$@"

#!/usr/bin/env bash
# 보드가 실제로 어떤 IP를 쓰고 있는지 진단하는 스크립트
# - ARP 테이블과 dnsmasq lease를 조회해 예약 IP(예: 192.168.200.11)와 실제 IP를 비교
# - SSH가 열려 있는지 간단히 체크

set -euo pipefail

BOARD_MAC="${BOARD_MAC:-}"
EXPECTED_IP="${BOARD_IP:-192.168.200.11}"
PC_IP="${PC_IP:-192.168.200.1}"
WIRED_IF="${WIRED_IF:-}"
LEASE_FILE="${LEASE_FILE:-/var/lib/misc/dnsmasq.leases}"

log() { echo "[diag] $*"; }
warn() { echo "[diag][warn] $*" >&2; }
error() { echo "[diag][error] $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
보드 IP 진단 스크립트

사용법:
  sudo ./diagnose_board_ip.sh -m <보드 MAC> [-e <기대 IP>] [-i <유선 IF>] [-p <PC IP>] [-l <lease 파일>]

옵션/환경변수:
  -m, --mac        보드 MAC 주소 (필수) / BOARD_MAC
  -e, --expected   예약/기대 IP (기본: 192.168.200.11) / BOARD_IP
  -i, --interface  보드가 꽂힌 PC 유선 인터페이스 / WIRED_IF
  -p, --pc-ip      PC 유선 인터페이스 IP (기본: 192.168.200.1) / PC_IP
  -l, --lease      dnsmasq lease 파일 경로 (기본: /var/lib/misc/dnsmasq.leases) / LEASE_FILE
  -h, --help       도움말

예시:
  sudo BOARD_MAC=76:0d:bf:80:e8:91 ./diagnose_board_ip.sh
  sudo ./diagnose_board_ip.sh -m 76:0d:bf:80:e8:91 -e 192.168.200.11
EOF
}

normalize_mac() {
    local mac="${1//\"/}"
    mac="${mac//\'/}"
    echo "${mac//-/:}" | tr '[:upper:]' '[:lower:]'
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -m|--mac) BOARD_MAC="$2"; shift 2;;
            -e|--expected) EXPECTED_IP="$2"; shift 2;;
            -i|--interface) WIRED_IF="$2"; shift 2;;
            -p|--pc-ip) PC_IP="$2"; shift 2;;
            -l|--lease) LEASE_FILE="$2"; shift 2;;
            -h|--help) usage; exit 0;;
            *) error "알 수 없는 인자: $1";;
        esac
    done
}

find_wired_interface() {
    [[ -n "$WIRED_IF" ]] && ip link show "$WIRED_IF" >/dev/null 2>&1 && return 0

    local candidate=""
    while IFS= read -r line; do
        local ifname ip
        ifname=$(echo "$line" | awk '{print $2}' | tr -d ':')
        ip=$(echo "$line" | awk '{print $4}' | cut -d/ -f1)
        [[ "$ifname" =~ ^(wlan|wlp|mlan|wl) ]] && continue
        if [[ "$ip" == "$PC_IP" ]]; then
            WIRED_IF="$ifname"
            return 0
        fi
        if [[ "$ip" =~ ^192\.168\.200\. ]] && [[ -z "$candidate" ]]; then
            candidate="$ifname"
        fi
    done < <(ip -o -4 addr show scope global 2>/dev/null || true)

    if [[ -z "$WIRED_IF" && -n "$candidate" ]]; then
        WIRED_IF="$candidate"
        return 0
    fi

    warn "유선 인터페이스를 자동으로 찾지 못했습니다. -i 옵션이나 WIRED_IF 변수로 지정하세요."
    return 1
}

lookup_arp_by_mac() {
    local mac="$1" iface="$2"
    ip neigh show ${iface:+dev "$iface"} 2>/dev/null | awk -v mac="$mac" 'tolower($0) ~ mac {print $1; exit}'
}

lookup_arp_by_ip() {
    local ip="$1"
    ip neigh show "$ip" 2>/dev/null | awk 'NR==1 {print $0}'
}

lookup_lease() {
    local mac="$1"
    [[ -r "$LEASE_FILE" ]] || return 0
    awk -v mac="$mac" 'tolower($2)==mac {print $3, $1, $4; exit}' "$LEASE_FILE"
}

print_iface_status() {
    [[ -z "$WIRED_IF" ]] && return 0
    log "PC 유선 인터페이스: ${WIRED_IF}"
    ip -4 addr show "$WIRED_IF" 2>/dev/null | sed 's/^/  /' || true
}

check_port_22() {
    local ip="$1"
    if command -v nc >/dev/null 2>&1; then
        if nc -z -w1 "$ip" 22 >/dev/null 2>&1; then
            log "SSH 포트 확인: ${ip}:22 응답 있음 (nc)"
        else
            warn "SSH 포트 확인: ${ip}:22 응답 없음 또는 거부 (nc)"
        fi
    fi
}

main() {
    parse_args "$@"
    [[ -z "$BOARD_MAC" ]] && error "보드 MAC 주소가 필요합니다. -m 옵션 또는 BOARD_MAC 환경변수를 설정하세요."
    BOARD_MAC="$(normalize_mac "$BOARD_MAC")"

    find_wired_interface || true

    log "보드 MAC   : $BOARD_MAC"
    log "기대 IP    : $EXPECTED_IP"
    log "PC IP      : $PC_IP"
    [[ -n "$WIRED_IF" ]] && log "유선 IF    : $WIRED_IF"
    [[ -r "$LEASE_FILE" ]] || warn "lease 파일을 읽을 수 없습니다: $LEASE_FILE (sudo 필요?)"

    print_iface_status

    local arp_ip lease_info lease_ip lease_ts lease_hostname
    arp_ip="$(lookup_arp_by_mac "$BOARD_MAC" "$WIRED_IF")"
    lease_info="$(lookup_lease "$BOARD_MAC")"
    if [[ -n "$lease_info" ]]; then
        lease_ip=$(echo "$lease_info" | awk '{print $1}')
        lease_ts=$(echo "$lease_info" | awk '{print $2}')
        lease_hostname=$(echo "$lease_info" | awk '{print $3}')
    fi

    echo ""
    log "ARP 조회 결과:"
    if [[ -n "$arp_ip" ]]; then
        log "  - MAC=$BOARD_MAC 가 ARP로 응답한 IP: $arp_ip"
    else
        warn "  - ARP 테이블에 MAC=$BOARD_MAC 항목이 없습니다. 보드 연결/전원/케이블 확인."
    fi

    log "dnsmasq lease 조회:"
    if [[ -n "$lease_ip" ]]; then
        local ts_readable
        ts_readable=$(date -d @"$lease_ts" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$lease_ts")
        log "  - lease: IP=$lease_ip, 시각=$ts_readable, 호스트=${lease_hostname:--}"
    else
        warn "  - lease 파일에서 MAC=$BOARD_MAC 항목을 찾지 못했습니다."
    fi

    echo ""
    log "기대 IP 상태 확인 (${EXPECTED_IP}):"
    local expected_arp
    expected_arp="$(lookup_arp_by_ip "$EXPECTED_IP")"
    if [[ -n "$expected_arp" ]]; then
        log "  - ARP 항목: $expected_arp"
    else
        warn "  - ARP 테이블에 ${EXPECTED_IP} 항목이 없습니다."
    fi

    local best_ip=""
    [[ -n "$arp_ip" ]] && best_ip="$arp_ip"
    [[ -z "$best_ip" && -n "$lease_ip" ]] && best_ip="$lease_ip"

    if [[ -n "$best_ip" ]]; then
        echo ""
        if [[ "$best_ip" == "$EXPECTED_IP" ]]; then
            log "보드가 예약 IP(${EXPECTED_IP})로 붙어 있는 것으로 보입니다."
        else
            warn "보드가 다른 IP(${best_ip})로 붙어 있습니다. (기대: ${EXPECTED_IP})"
            warn "현재 IP로 SSH: ssh root@${best_ip}"
            warn "DHCP로 다시 받게 하려면 보드에서 systemd-networkd 재시작 또는 케이블 재연결."
        fi
        check_port_22 "$best_ip"
    else
        warn "ARP/lease 어디에서도 MAC=$BOARD_MAC 정보를 찾지 못했습니다. 보드 전원/케이블/인터페이스를 확인하세요."
    fi

    if [[ -n "$lease_ip" && -n "$arp_ip" && "$lease_ip" != "$arp_ip" ]]; then
        warn "참고: lease 파일 IP(${lease_ip})와 ARP 응답 IP(${arp_ip})가 다릅니다. 보드가 IP를 바꿨는데 dnsmasq에 갱신이 안 된 상황일 수 있습니다."
    fi

    echo ""
    log "빠른 조치 요약:"
    if [[ -n "$best_ip" ]]; then
        log "  1) SSH 시도: ssh root@${best_ip}"
    else
        log "  1) 케이블/전원 확인 후 보드를 한 번 재부팅"
    fi
    log "  2) 예약 IP(${EXPECTED_IP})로 쓰고 싶으면 보드에서 DHCP로 재설정 후 systemd-networkd 재시작"
}

main "$@"

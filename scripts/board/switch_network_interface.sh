#!/usr/bin/env bash
# Toggle wired/wifi interfaces on the board.

if [ -z "$BASH_VERSION" ]; then
    echo "[net-switch][error] This script requires bash. Run with 'bash $0'." >&2
    exit 1
fi

set -euo pipefail

log() { echo "[net-switch] $*"; }
warn() { echo "[net-switch][warn] $*" >&2; }
error() { echo "[net-switch][error] $*" >&2; exit 1; }

require_root() {
    if [ "$(id -u)" != "0" ]; then
        error "Run as root (sudo)."
    fi
}

ensure_iface() {
    local ifname="$1"
    if ! ip link show "$ifname" >/dev/null 2>&1; then
        error "Interface not found: $ifname"
    fi
}

main() {
    require_root
    ensure_iface eth0
    ensure_iface mlan0

    echo "Select mode:"
    echo "  1) wired only  (eth0 up,  mlan0 down)"
    echo "  2) wifi only   (eth0 down, mlan0 up)"
    echo "  3) both up     (eth0 up,  mlan0 up)"
    read -rp "Choice [1]: " choice
    choice=${choice:-1}

    case "$choice" in
        1)
            log "Enabling eth0, disabling mlan0..."
            ip link set mlan0 down
            ip addr flush dev mlan0 || true
            ip link set eth0 up
            ;;
        2)
            log "Enabling mlan0, disabling eth0..."
            ip link set eth0 down
            ip addr flush dev eth0 || true
            ip link set mlan0 up
            ;;
        3)
            log "Enabling both eth0 and mlan0..."
            ip link set eth0 up
            ip link set mlan0 up
            ;;
        *)
            error "Invalid choice."
            ;;
    esac

    log "Current addresses:"
    ip addr show eth0 | sed 's/^/  /'
    ip addr show mlan0 | sed 's/^/  /'
    log "Done."
}

main "$@"

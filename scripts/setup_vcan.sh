#!/usr/bin/env bash
# Idempotent SocketCAN vcan setup. Run as root:
#   sudo scripts/setup_vcan.sh            # vcan0
#   sudo scripts/setup_vcan.sh --vcan1    # vcan0 + vcan1 (2-bus experiments)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root. Run: sudo $0 $*" >&2
    exit 1
fi

ifaces=(vcan0)
if [[ "${1:-}" == "--vcan1" ]]; then
    ifaces+=(vcan1)
fi

modprobe vcan

for ifc in "${ifaces[@]}"; do
    if ! ip link show "$ifc" >/dev/null 2>&1; then
        ip link add dev "$ifc" type vcan
        echo "created $ifc"
    fi
    ip link set up "$ifc"
    echo "$ifc is up"
done

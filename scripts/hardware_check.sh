#!/usr/bin/env bash
# Real-hardware check: start the hotspot, keep it up while you join from a
# phone, stop it, and verify that everything was cleaned up.
#
#   sudo scripts/hardware_check.sh [SECONDS]     # default: keep it up 60 s
#   sudo METHOD=p2p scripts/hardware_check.sh    # test one method (here Wi-Fi Direct)
#
# Runs the apsta in this checkout. Writes a report to $REPORT
# (default /tmp/apsta-hardware-report.txt) that you can attach to an issue.
# The hotspot password is shown on your terminal only, never in the report.
set -uo pipefail

HOLD=${1:-60}
START_ARGS=()
[[ -n ${METHOD:-} ]] && START_ARGS=(--method "$METHOD")
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPORT=${REPORT:-/tmp/apsta-hardware-report.txt}
apsta() { python3 "$REPO/apsta.py" "$@"; }
section() { printf '\n===== %s =====\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

# The password goes to the terminal on fd 3, not into the report.
if ! { exec 3>/dev/tty; } 2>/dev/null; then
    exec 3>&1
fi
exec > >(tee "$REPORT") 2>&1

STARTED=0
cleanup() {
    if [[ $STARTED -eq 1 ]]; then
        section "stop"
        apsta stop
        STARTED=0
    fi
}
trap cleanup EXIT
trap 'echo; echo "Interrupted, stopping the hotspot."; exit 130' INT TERM

snapshot() {
    echo "-- links";        ip -br link | grep -vE '^(lo|docker|veth|br-)' || true
    echo "-- ip_forward";   cat /proc/sys/net/ipv4/ip_forward
    echo "-- iptables rules tagged apsta: $(iptables -S 2>/dev/null | grep -c apsta; iptables -t nat -S 2>/dev/null | grep -c apsta)"
    echo "-- nftables table apsta: $(nft list tables 2>/dev/null | grep -c apsta)"
    echo "-- NM connection apsta-hotspot: $(nmcli -t -f NAME connection show 2>/dev/null | grep -c '^apsta-hotspot$')"
    echo "-- transient units:"; systemctl list-units --no-legend 'apsta-*' 2>/dev/null || true
    echo "-- /run/apsta:";  ls /run/apsta 2>/dev/null || echo "(none)"
    if [[ -e /run/NetworkManager/conf.d/90-apsta-unmanaged.conf ]]; then
        echo "-- NM runtime conf: present"
    else
        echo "-- NM runtime conf: none"
    fi
}

section "system"
grep PRETTY_NAME /etc/os-release
uname -r
apsta --version
echo "commit: $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"

section "detect"
apsta detect --json

section "before"
snapshot > /tmp/apsta-before.$$
cat /tmp/apsta-before.$$

section "start"
if APSTA_DEBUG=1 apsta start "${START_ARGS[@]}"; then
    STARTED=1
else
    echo "START FAILED (exit $?)"
    section "logs"
    journalctl -u apsta-hostapd -u apsta-dnsmasq -u wpa_supplicant -n 60 --no-pager 2>/dev/null
    tail -n 40 /var/log/apsta.log 2>/dev/null
    exit 1
fi

section "status"
apsta status --json
base=$(apsta status --json | python3 -c 'import json,sys; h=json.load(sys.stdin)["hotspot"]; print(h["base_interface"] if h else "")')
if [[ -z $base ]]; then
    echo "HOTSPOT NOT RUNNING right after start"
else
    echo "-- Wi-Fi link still up?"; iw dev "$base" link | grep -E 'SSID|freq' || echo "NOT CONNECTED"
    echo "-- AP interface:"; iw dev "$(apsta status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["hotspot"]["ap_interface"])')" info
fi
echo "-- hostapd.conf (password removed):"; grep -v -E '^wpa_(passphrase|psk)=' /run/apsta/hostapd.conf 2>/dev/null

ssid=$(apsta config --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["settings"]["ssid"])')
password=$(apsta config --json --show-password | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
{
    echo
    echo "  >>> Join \"$ssid\" from your phone now. Password: $password"
    echo "  >>> Then open a website on the phone to check internet access."
    echo "  >>> Keeping the hotspot up for $HOLD seconds (Ctrl+C to stop early)."
    echo
} >&3

section "clients (every 15 s)"
for ((elapsed = 0; elapsed < HOLD; elapsed += 15)); do
    sleep $(( HOLD - elapsed < 15 ? HOLD - elapsed : 15 ))
    echo "-- after $(( elapsed + 15 < HOLD ? elapsed + 15 : HOLD )) s"
    apsta clients --json
done

section "daemon logs"
journalctl -u apsta-hostapd -u apsta-dnsmasq -n 30 --no-pager 2>/dev/null

cleanup

section "after (should match before)"
snapshot > /tmp/apsta-after.$$
cat /tmp/apsta-after.$$
if diff -q /tmp/apsta-before.$$ /tmp/apsta-after.$$ >/dev/null; then
    echo "CLEANUP OK: system state is back to how it was."
else
    echo "CLEANUP DIFFERENCES:"; diff /tmp/apsta-before.$$ /tmp/apsta-after.$$
fi
rm -f /tmp/apsta-before.$$ /tmp/apsta-after.$$

section "done"
echo "Report: $REPORT"

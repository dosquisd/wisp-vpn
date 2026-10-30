#!/usr/bin/env bash
# ==============================================================================
# Wisp In-VM Session Watchdog (Dead Man's Switch)
#
# Cattle vs. Pets Distinction:
# - Cloud VMs (MODE="cloud"): Cattle. Disposable compute instances provisioned
#   solely for this ephemeral WireGuard VPN session. When the session expires or
#   becomes abandoned, the VM is powered off immediately (`systemctl poweroff`
#   or `poweroff`), stopping or terminating the instance so it stops billing.
# - Custom VPS (MODE="vps"): Pets. User-owned, persistent servers hosting other
#   workloads, containers, or personal services. Wisp was granted access only to
#   configure an ephemeral WireGuard interface. Wisp must NEVER power off, reboot,
#   or halt a VPS. Instead, it strictly tears down only Wisp's own WireGuard
#   interface, client configs, parameters, and watchdog units, leaving the host OS
#   and all unrelated services completely undisturbed.
# ==============================================================================

set -u

NIC="${1:-}"
if [ -z "$NIC" ]; then
    exit 0
fi

CONF="/etc/wisp/watchdog-${NIC}.conf"
if [ ! -f "$CONF" ]; then
    exit 0
fi

# shellcheck source=/dev/null
. "$CONF"

# Guard against unset variables with defaults under set -u
: "${NIC:=$1}"
: "${MODE:=cloud}"
: "${IDLE_SECONDS:=900}"  # 900 seconds = 15 minutes
: "${DEADLINE_EPOCH:=0}"
: "${SESSION_STARTED_EPOCH:=0}"

trigger() {
    local reason="$1"

    if [ "$MODE" = "cloud" ]; then
        logger -t wisp-watchdog "Session watchdog triggered for ${NIC} (${MODE}): ${reason}. Action: powering off system." 2>/dev/null || true
        if command -v systemctl >/dev/null 2>&1; then
            systemctl poweroff 2>/dev/null || poweroff 2>/dev/null || true
        else
            poweroff 2>/dev/null || true
        fi
        exit 0
    fi

    if [ "$MODE" = "vps" ]; then
        logger -t wisp-watchdog "Session watchdog triggered for ${NIC} (${MODE}): ${reason}. Action: tearing down Wisp resources." 2>/dev/null || true

        # 1. wg-quick down "$NIC" (runs PostDown and removes the iptables rules).
        wg-quick down "$NIC" 2>/dev/null || true

        # 2. systemctl disable --now "wg-quick@$NIC".
        systemctl disable --now "wg-quick@$NIC" 2>/dev/null || true

        # 3. rm -f "/etc/wireguard/$NIC.conf".
        rm -f "/etc/wireguard/$NIC.conf" 2>/dev/null || true

        # 4. rm -f /root/"$NIC"-client-*.conf and /home/*/"$NIC"-client-*.conf.
        sh -c "rm -f /root/${NIC}-client-*.conf /home/*/${NIC}-client-*.conf" 2>/dev/null || true

        # 5. If /etc/wireguard/params exists and grep -q "^SERVER_WG_NIC=$NIC" matches, rm -f /etc/wireguard/params.
        if [ -f /etc/wireguard/params ] && grep -q "^SERVER_WG_NIC=$NIC" /etc/wireguard/params 2>/dev/null; then
            rm -f /etc/wireguard/params 2>/dev/null || true
        fi

        # 6. Disable and remove its own units (systemctl disable --now "wisp-watchdog@$NIC.timer"),
        #    and rm -f the unit files under /etc/systemd/system/, then systemctl daemon-reload.
        systemctl disable --now "wisp-watchdog@$NIC.timer" 2>/dev/null || true
        rm -f "/etc/systemd/system/wisp-watchdog@$NIC.timer" "/etc/systemd/system/wisp-watchdog@$NIC.service" 2>/dev/null || true

        # 7. rm -f "/etc/wisp/watchdog-$NIC.conf" and /usr/local/bin/wisp-watchdog ONLY IF
        #    no other watchdog-*.conf remains in /etc/wisp (the script is shared across
        #    sessions; do not remove it while another session still uses it).
        #    Remove /etc/wisp directory only if empty.
        rm -f "/etc/wisp/watchdog-$NIC.conf" 2>/dev/null || true

        has_other_confs=false
        for conf_file in /etc/wisp/watchdog-*.conf; do
            if [ -f "$conf_file" ]; then
                has_other_confs=true
                break
            fi
        done

        if [ "$has_other_confs" = false ]; then
            rm -f /etc/systemd/system/wisp-watchdog@.service 2>/dev/null || true
            rm -f /etc/systemd/system/wisp-watchdog@.timer 2>/dev/null || true
            systemctl daemon-reload 2>/dev/null || true
            rm -f /usr/local/bin/wisp-watchdog 2>/dev/null || true
            rmdir /etc/wisp 2>/dev/null || true
        else
            systemctl daemon-reload 2>/dev/null || true
        fi

        exit 0
    fi

    logger -t wisp-watchdog "Session watchdog triggered for ${NIC} with unknown mode '${MODE}': ${reason}." 2>/dev/null || true
    exit 0
}

now=$(date +%s)

# Hard TTL check: if DEADLINE_EPOCH > 0 and now >= DEADLINE_EPOCH, trigger with reason "hard TTL expired".
if [ "$DEADLINE_EPOCH" -gt 0 ] 2>/dev/null && [ "$now" -ge "$DEADLINE_EPOCH" ] 2>/dev/null; then
    trigger "hard TTL expired"
fi

# Idle check: handle missing wg binary gracefully.
if ! command -v wg >/dev/null 2>&1; then
    logger -t wisp-watchdog "Debug: wg binary not found, skipping idle check for ${NIC}" 2>/dev/null || true
else
    latest=$(wg show "$NIC" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -n | tail -1)
    if [ -z "$latest" ] || [ "$latest" -eq 0 ] 2>/dev/null; then
        latest="$SESSION_STARTED_EPOCH"
    fi

    if [ -n "$latest" ] && [ "$latest" -gt 0 ] 2>/dev/null; then
        diff=$((now - latest))
        if [ "$IDLE_SECONDS" -gt 0 ] 2>/dev/null && [ "$diff" -ge "$IDLE_SECONDS" ] 2>/dev/null; then
            trigger "no handshake for ${diff}s"
        fi
    fi
fi

exit 0

#!/usr/bin/env bash
# Wisp uninstaller (Linux).
#
# Self-contained by design: it may be downloaded as a single file, so it must
# never source other repo scripts. The privileged daemon removal is delegated
# to `sudo wisp daemon uninstall` (single source of truth: this script never
# touches systemd units itself).
#
# Recommended invocation (use bash <(...) instead of a pipe so the
# confirmation prompts can read your terminal):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/uninstall.sh)
#
# The install manifest (install-manifest.json in the Wisp data dir) drives
# the split: tools listed under "pre_existing" are left untouched, tools
# under "installed_by_wisp" are removed after asking, and the "group" field
# says whether the wisp group was created by Wisp (removed) or pre-existing
# (left untouched). Wisp-owned artifacts (the wisp-vpn tool, daemon units,
# daemon runtime, data/config/state dirs) are removed without asking; the
# manifest is removed LAST.
#
# The script may be invoked under sudo: the invoking user (SUDO_USER) is
# resolved and their Wisp installation is uninstalled. Plain root (no
# SUDO_USER) is refused — there is no way to tell whose installation it is.
#
# Usage: uninstall.sh [--dry-run] [--yes] [-h | --help]

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

DRY_RUN=false
ASSUME_YES=false

WISP_PKG="wisp-vpn"
PULUMI_PATH_LINE='export PATH="$HOME/.pulumi/bin:$PATH"'

PKG_FAMILY=""
MANIFEST_FILE=""
PRE_EXISTING=""
INSTALLED_BY_WISP=""
GROUP_STATE=""

# Home directory of the user whose Wisp installation is being uninstalled:
# the invoking user, or SUDO_USER's home when run under sudo.
USER_HOME=""

info() { printf '%b\n' "${GREEN}==>${NC} $*"; }
warn() { printf '%b\n' "${YELLOW}WARNING:${NC} $*" >&2; }
fail() { printf '%b\n' "${RED}ERROR:${NC} $*" >&2; exit 1; }
plan() { printf '%b\n' "    [dry-run] $*"; }
step() { printf '\n%b\n' "${GREEN}==>${NC} $*"; }

usage() {
    cat <<'EOF'
Wisp uninstaller (Linux)

Usage: uninstall.sh [options]

Options:
  --dry-run   Print what would be removed without changing anything.
  --yes       Remove tools installed by Wisp without asking.
  -h, --help  Show this help and exit.

The install manifest (if present) decides what is asked about: tools Wisp
installed are offered for removal; tools that were already on the machine
are left untouched. The 'wisp' group is removed only when the manifest
records it as created by Wisp. Wisp-owned artifacts (wisp-vpn tool, daemon,
data dirs) are always removed. If an active cloud session is detected, you
are asked to destroy it first (abort is the default).
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --yes)
                ASSUME_YES=true
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                fail "Unknown argument: $1 (try --help)"
                ;;
        esac
    done
}

# --- Distro detection (for the optional WireGuard package removal) ----------

detect_distro() {
    if [[ ! -r /etc/os-release ]]; then
        PKG_FAMILY=""
        return 0
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    local distro="${ID:-}"
    case "$distro" in
        debian | ubuntu | raspbian | linuxmint | pop | neon | kali | zorin)
            PKG_FAMILY="apt"
            ;;
        fedora | rhel | centos | rocky | almalinux | oracle | ol | amzn)
            if command -v dnf >/dev/null 2>&1; then
                PKG_FAMILY="dnf"
            else
                PKG_FAMILY="yum"
            fi
            ;;
        arch | manjaro | endeavouros | archarm | cachyos | garuda)
            PKG_FAMILY="pacman"
            ;;
        alpine)
            PKG_FAMILY="apk"
            ;;
        *)
            case "${ID_LIKE:-}" in
                *debian*) PKG_FAMILY="apt" ;;
                *fedora* | *rhel*)
                    if command -v dnf >/dev/null 2>&1; then
                        PKG_FAMILY="dnf"
                    else
                        PKG_FAMILY="yum"
                    fi
                    ;;
                *arch*) PKG_FAMILY="pacman" ;;
                *alpine*) PKG_FAMILY="apk" ;;
                *) PKG_FAMILY="" ;;
            esac
            ;;
    esac
    if [[ -n "$PKG_FAMILY" ]]; then
        info "Detected distribution '${distro}' (package family: ${PKG_FAMILY})."
    else
        info "Distribution '${distro}' not recognized; package removal disabled."
    fi
}

run_priv() {
    if $DRY_RUN; then
        plan "sudo $*"
    else
        sudo "$@"
    fi
}

ask() {
    local reply=""
    # `|| reply=""` keeps the script alive when stdin is not a terminal
    # (e.g. the script was piped instead of run via bash <(...)).
    read -r -p "$1" reply || reply=""
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

# --- Invoking-user context ------------------------------------------------------

resolve_user_context() {
    # Resolve whose Wisp installation is being uninstalled. Under sudo,
    # everything user-owned (home, PATH, dirs, manifest) belongs to
    # SUDO_USER, not to root.
    if [[ "$(id -u)" -eq 0 ]]; then
        if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
            fail "Running as root without SUDO_USER: cannot tell whose Wisp installation to uninstall. Run the script as your regular user (sudo is requested when needed), or re-run it with sudo from your account."
        fi
        USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
        if [[ -z "$USER_HOME" ]]; then
            fail "Cannot resolve the home directory for user '${SUDO_USER}'."
        fi
        info "Uninstalling the Wisp installation of user '${SUDO_USER}' (invoked with sudo)."
    else
        USER_HOME="${HOME}"
    fi
    # sudo's secure_path does not include ~/.local/bin, where uv and wisp
    # are installed; make sure this session sees them.
    case ":${PATH}:" in
        *":${USER_HOME}/.local/bin:"*) ;;
        *) export PATH="${USER_HOME}/.local/bin:${PATH}" ;;
    esac
}

# --- Manifest -----------------------------------------------------------------

manifest_path() {
    if [[ -n "${WISP_DATA_DIR:-}" ]]; then
        printf '%s/install-manifest.json' "${WISP_DATA_DIR%/}"
    else
        printf '%s/.local/share/wisp/install-manifest.json' "$USER_HOME"
    fi
}

config_dir() {
    if [[ -n "${WISP_CONFIG_DIR:-}" ]]; then
        printf '%s' "${WISP_CONFIG_DIR%/}"
    else
        printf '%s/.config/wisp' "$USER_HOME"
    fi
}

manifest_get() {
    local file="$1" key="$2" out=""
    [[ -f "$file" ]] || return 0
    out="$(uv run --no-project python -c '
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)
value = data.get(sys.argv[2], [])
if isinstance(value, list):
    print(" ".join(str(item) for item in value))
' "$file" "$key" 2>/dev/null || true)"
    printf '%s' "$out"
}

manifest_get_value() {
    # Prints the string stored under key $2 of the manifest $1 (empty when
    # the file or key is missing, or the value is not a string).
    local file="$1" key="$2" out=""
    [[ -f "$file" ]] || return 0
    out="$(uv run --no-project python -c '
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)
value = data.get(sys.argv[2], "")
if isinstance(value, str):
    print(value)
' "$file" "$key" 2>/dev/null || true)"
    printf '%s' "$out"
}

read_manifest() {
    MANIFEST_FILE="$(manifest_path)"
    if [[ ! -f "$MANIFEST_FILE" ]]; then
        warn "No install manifest at ${MANIFEST_FILE}; proceeding with wisp-owned cleanup only."
        return 0
    fi
    info "Reading manifest: ${MANIFEST_FILE}"
    PRE_EXISTING="$(manifest_get "$MANIFEST_FILE" "pre_existing")"
    INSTALLED_BY_WISP="$(manifest_get "$MANIFEST_FILE" "installed_by_wisp")"
    GROUP_STATE="$(manifest_get_value "$MANIFEST_FILE" "group")"
    if [[ -z "${PRE_EXISTING}${INSTALLED_BY_WISP}" ]]; then
        warn "Manifest exists but could not be parsed (is uv available?); proceeding with wisp-owned cleanup only."
    fi
}

# --- Active cloud session --------------------------------------------------------

check_active_session() {
    # A live session marker means there is billable cloud infrastructure
    # that outlives this uninstall. It must be destroyed first; abort is
    # the default. No marker: no cloud messaging at all.
    local marker
    marker="$(config_dir)/active-session.json"
    [[ -f "$marker" ]] || return 0
    warn "A live cloud session appears active (marker: ${marker})."
    if $ASSUME_YES; then
        info "--yes given: proceeding despite the active session."
        return 0
    fi
    if $DRY_RUN; then
        plan "would ask: 'Destroy it first with wisp destroy, then uninstall. Continue anyway? [y/N]' (default: abort)"
        return 0
    fi
    if ! ask "Destroy it first with wisp destroy, then uninstall. Continue anyway? [y/N]: "; then
        fail "Aborted: destroy the session first with 'wisp destroy <provider>', then uninstall."
    fi
}

# --- Tool removal --------------------------------------------------------------

remove_packages() {
    # Removes the given packages with the distro-appropriate command.
    case "$PKG_FAMILY" in
        apt) run_priv apt-get remove -y "$@" ;;
        dnf) run_priv dnf remove -y "$@" ;;
        yum) run_priv yum remove -y "$@" ;;
        pacman) run_priv pacman -Rs --noconfirm "$@" ;;
        apk) run_priv apk del "$@" ;;
        *)
            warn "Unknown package family; remove manually: $*"
            ;;
    esac
}

remove_wireguard_packages() {
    case "$PKG_FAMILY" in
        apt) remove_packages wireguard wireguard-tools ;;
        dnf | yum | pacman | apk) remove_packages wireguard-tools ;;
        *) remove_packages wireguard-tools ;;
    esac
}

remove_pulumi_path_lines() {
    local profile
    for profile in "$USER_HOME/.profile" "$USER_HOME/.bashrc"; do
        [[ -f "$profile" ]] || continue
        grep -Fqx "$PULUMI_PATH_LINE" "$profile" || continue
        if $DRY_RUN; then
            plan "remove the Pulumi PATH line from ${profile}"
        else
            grep -Fvx "$PULUMI_PATH_LINE" "$profile" >"${profile}.wisp-tmp"
            mv "${profile}.wisp-tmp" "$profile"
        fi
    done
}

remove_tool() {
    local tool="$1"
    case "$tool" in
        uv)
            if command -v uv >/dev/null 2>&1; then
                if $DRY_RUN; then
                    plan "uv self uninstall"
                else
                    uv self uninstall
                fi
            else
                warn "uv is not on PATH; cannot self-uninstall it."
            fi
            ;;
        pulumi)
            if $DRY_RUN; then
                plan "rm -rf ${USER_HOME}/.pulumi"
                remove_pulumi_path_lines
            else
                rm -rf "${USER_HOME}/.pulumi"
                remove_pulumi_path_lines
            fi
            ;;
        wireguard-tools)
            remove_wireguard_packages
            ;;
        curl)
            remove_packages curl
            ;;
        *)
            warn "Do not know how to remove '${tool}'; skipping."
            ;;
    esac
}

handle_tools() {
    step "Tools recorded in the manifest"
    local tool
    for tool in $INSTALLED_BY_WISP; do
        if $ASSUME_YES; then
            info "Removing '${tool}' (installed by Wisp, --yes)."
            remove_tool "$tool"
        elif $DRY_RUN; then
            info "Would ask about removing '${tool}' (installed by Wisp):"
            remove_tool "$tool"
        else
            if ask "Remove '${tool}'? It was installed by the Wisp installer [y/N]: "; then
                remove_tool "$tool"
            else
                info "Kept '${tool}'."
            fi
        fi
    done
    for tool in $PRE_EXISTING; do
        info "Left untouched (pre-existing): ${tool}"
    done
    if [[ -z "${INSTALLED_BY_WISP}${PRE_EXISTING}" ]]; then
        info "No tools recorded in the manifest."
    fi
}

# --- wisp group ------------------------------------------------------------------

remove_group() {
    if $DRY_RUN; then
        plan "sudo groupdel wisp"
        return 0
    fi
    info "Removing the 'wisp' group."
    if ! run_priv groupdel wisp; then
        warn "Could not remove the 'wisp' group (does it still exist?); remove it manually: sudo groupdel wisp"
    fi
}

handle_group() {
    # The group lifecycle belongs to this script: the manifest records
    # whether the group was created by Wisp or pre-existed.
    step "wisp group"
    case "$GROUP_STATE" in
        created_by_wisp)
            remove_group
            ;;
        pre_existing)
            info "The 'wisp' group was pre-existing; left untouched."
            ;;
        *)
            # Missing manifest or field: unknown origin, ask (default: keep).
            if $DRY_RUN; then
                plan "would ask whether to remove the 'wisp' group (origin unknown; default: keep)"
                return 0
            fi
            if ask "Remove the 'wisp' group? Its origin is unknown (no manifest record) [y/N]: "; then
                remove_group
            else
                info "The 'wisp' group was kept."
            fi
            ;;
    esac
}

# --- Wisp-owned artifacts --------------------------------------------------------

data_dir() {
    if [[ -n "${WISP_DATA_DIR:-}" ]]; then
        printf '%s' "${WISP_DATA_DIR%/}"
    else
        printf '%s/.local/share/wisp' "$USER_HOME"
    fi
}

guard_path() {
    local path="$1" label="$2"
    case "$path" in
        "" | "/" | "$USER_HOME" | "$USER_HOME"/)
            fail "Refusing to remove unsafe ${label}: ${path}"
            ;;
    esac
}

remove_wisp_dirs() {
    step "Wisp-owned directories"
    local data config state
    data="$(data_dir)"
    config="$(config_dir)"
    state="${USER_HOME}/.local/state/wisp"

    guard_path "$data" "the Wisp data dir"

    local dir
    # Order matters only for the data dir: it holds the manifest, which must
    # be removed last.
    for dir in "$config" "$state" "$data"; do
        if [[ -e "$dir" ]]; then
            if $DRY_RUN; then
                plan "rm -rf ${dir}"
            else
                rm -rf "$dir"
                info "Removed ${dir}"
            fi
        else
            info "Not present, skipping: ${dir}"
        fi
    done
}

# --- Main flow ---------------------------------------------------------------------

uninstall_daemon() {
    step "Privileged daemon"
    local wisp_bin=""
    if command -v wisp >/dev/null 2>&1; then
        wisp_bin="$(command -v wisp)"
    fi
    if $DRY_RUN; then
        plan "sudo ${wisp_bin:-wisp} daemon uninstall"
        return 0
    fi
    if [[ -z "$wisp_bin" ]]; then
        warn "'wisp' is not on PATH; skipping the daemon uninstall (nothing to do?)."
        return 0
    fi
    # Absolute path on purpose: sudo's secure_path does not include
    # ~/.local/bin, so plain `sudo wisp` would not resolve.
    info "Running: sudo ${wisp_bin} daemon uninstall"
    if ! sudo "$wisp_bin" daemon uninstall; then
        fail "'wisp daemon uninstall' failed (see its output above)."
    fi
}

uninstall_wisp_tool() {
    step "wisp CLI (${WISP_PKG})"
    if ! command -v uv >/dev/null 2>&1; then
        warn "uv is not on PATH; cannot uninstall ${WISP_PKG} (was it installed another way?)."
        return 0
    fi
    if ! uv tool list 2>/dev/null | awk '{print $1}' | grep -Fxq "$WISP_PKG"; then
        info "${WISP_PKG} is not installed as a uv tool; nothing to do."
        return 0
    fi
    if $DRY_RUN; then
        plan "uv tool uninstall ${WISP_PKG}"
    else
        uv tool uninstall "$WISP_PKG"
    fi
}

print_summary() {
    step "Uninstall summary"
    if $DRY_RUN; then
        info "Dry run complete: nothing was changed."
    else
        info "Wisp CLI, daemon units, and data/config/state directories removed."
    fi
    if [[ -n "$PRE_EXISTING" ]]; then
        info "Pre-existing tools left untouched: ${PRE_EXISTING}"
    fi
}

main() {
    parse_args "$@"
    if $DRY_RUN; then
        info "Dry run: nothing will be removed."
    fi

    # Resolve whose installation is being uninstalled BEFORE anything else:
    # every user-owned path (manifest, dirs, tool lookups) depends on it.
    resolve_user_context

    # Read the manifest fully into variables BEFORE anything is deleted (it
    # lives in the data dir, which is removed near the end).
    read_manifest

    # Billable cloud infrastructure outlives this uninstall: gate the whole
    # teardown on the active-session marker BEFORE anything is removed.
    check_active_session

    detect_distro

    step "Sudo credentials"
    if ! $DRY_RUN; then
        info "Requesting sudo credentials (cached for the privileged steps)..."
        sudo -v
    fi

    uninstall_daemon
    uninstall_wisp_tool
    handle_tools
    handle_group
    remove_wisp_dirs
    print_summary
}

main "$@"

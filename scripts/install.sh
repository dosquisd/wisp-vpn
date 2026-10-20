#!/usr/bin/env bash
# Wisp bootstrap installer (Linux).
#
# Self-contained by design: the recommended invocation downloads this single
# file, so it must never source other repo scripts (distro detection and the
# package-manager calls are inlined below).
#
#   curl -fsSL https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/install.sh | bash
#
# What it does, in order:
#   1. Preflight: distro detection, curl.
#   2. uv (which brings its own Python 3.14) — via the astral.sh installer.
#   3. wisp-vpn as a uv tool (upgrades with --force when already installed).
#   4. WireGuard tools via the distro package manager.
#   5. Optional Pulumi CLI, per-user under ~/.pulumi (skipped with
#      --skip-pulumi). A fresh install reminds you to run 'pulumi login'
#      afterwards; a pre-existing install is verified with 'pulumi whoami'.
#   6. Privileged daemon setup, delegated to `sudo wisp daemon install`
#      (single source of truth: this script never touches systemd units or
#      the wisp group itself).
#   7. An install-manifest.json in the Wisp data dir recording which tools
#      were pre-existing and which this script installed, and whether the
#      wisp group pre-existed or was created by Wisp, so the paired
#      uninstall script knows what to ask about and what to remove.
#
# Usage: install.sh [--skip-pulumi] [--dry-run] [-h | --help]

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

DRY_RUN=false
SKIP_PULUMI=false

WISP_PKG="wisp-vpn"
UV_INSTALL_URL="https://astral.sh/uv/0.13.1/install.sh"
PULUMI_INSTALL_URL="https://get.pulumi.com"
PULUMI_PATH_LINE='export PATH="$HOME/.pulumi/bin:$PATH"'

PKG_FAMILY=""
PRE_EXISTING=()
INSTALLED_BY_WISP=()
GROUP_STATE=

info() { printf '%b\n' "${GREEN}==>${NC} $*"; }
warn() { printf '%b\n' "${YELLOW}WARNING:${NC} $*" >&2; }
fail() { printf '%b\n' "${RED}ERROR:${NC} $*" >&2; exit 1; }
plan() { printf '%b\n' "    [dry-run] $*"; }

step() { printf '\n%b\n' "${GREEN}==>${NC} $*"; }

usage() {
    cat <<'EOF'
Wisp bootstrap installer (Linux)

Usage: install.sh [options]

Options:
  --skip-pulumi   Do not install the Pulumi CLI (e.g. it is already
                  available or you deploy only to a Custom VPS).
  --dry-run       Print the full plan (including the manifest that would be
                  written) without changing anything.
  -h, --help      Show this help and exit.

The script installs uv, the wisp-vpn CLI, WireGuard tools, the optional
Pulumi CLI, and delegates the privileged daemon setup to
`sudo wisp daemon install`.
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --skip-pulumi)
                SKIP_PULUMI=true
                shift
                ;;
            --dry-run)
                DRY_RUN=true
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

# --- Distro and package-manager detection ---------------------------------

detect_distro() {
    if [[ ! -r /etc/os-release ]]; then
        fail "Cannot detect the distribution: /etc/os-release is missing."
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
            # Fall back to ID_LIKE before giving up (covers derivatives that
            # set a custom ID).
            case "${ID_LIKE:-}" in
                *debian*)
                    PKG_FAMILY="apt"
                    ;;
                *fedora* | *rhel*)
                    if command -v dnf >/dev/null 2>&1; then
                        PKG_FAMILY="dnf"
                    else
                        PKG_FAMILY="yum"
                    fi
                    ;;
                *arch*)
                    PKG_FAMILY="pacman"
                    ;;
                *alpine*)
                    PKG_FAMILY="apk"
                    ;;
                *)
                    fail "Unsupported distribution '${distro}'. Supported families: apt (Debian/Ubuntu), dnf/yum (Fedora/RHEL and derivatives), pacman (Arch), apk (Alpine)."
                    ;;
            esac
            ;;
    esac
    info "Detected distribution '${distro}' (package family: ${PKG_FAMILY})."
}

# Runs a privileged command; in dry-run mode only prints it.
run_priv() {
    if $DRY_RUN; then
        plan "sudo $*"
    else
        sudo "$@"
    fi
}

pkg_install() {
    case "$PKG_FAMILY" in
        apt)
            run_priv apt-get update
            run_priv apt-get install -y "$@"
            ;;
        dnf) run_priv dnf install -y "$@" ;;
        yum) run_priv yum install -y "$@" ;;
        pacman) run_priv pacman -S --needed --noconfirm "$@" ;;
        apk) run_priv apk add "$@" ;;
    esac
}

# --- Preflight --------------------------------------------------------------

ensure_curl() {
    if command -v curl >/dev/null 2>&1; then
        info "curl is already installed: $(command -v curl)"
        PRE_EXISTING+=("curl")
        return 0
    fi
    info "Installing curl via ${PKG_FAMILY}..."
    pkg_install curl
    INSTALLED_BY_WISP+=("curl")
}

add_local_bin_to_path() {
    case ":${PATH}:" in
        *":${HOME}/.local/bin:"*) ;;
        *) export PATH="${HOME}/.local/bin:${PATH}" ;;
    esac
}

# --- uv ---------------------------------------------------------------------

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        info "uv is already installed: $(command -v uv)"
        PRE_EXISTING+=("uv")
    else
        info "Installing uv (this also brings its own Python 3.14)..."
        if $DRY_RUN; then
            plan "curl -LsSf ${UV_INSTALL_URL} | sh"
        else
            curl -LsSf "$UV_INSTALL_URL" | sh
        fi
        INSTALLED_BY_WISP+=("uv")
    fi
    # The astral installer drops the binary in ~/.local/bin; make sure this
    # session sees it (the installer also wires it into your shell profile).
    add_local_bin_to_path
    if ! $DRY_RUN && ! command -v uv >/dev/null 2>&1; then
        fail "uv is still not on PATH after installation (looked in ~/.local/bin)."
    fi
}

# --- wisp-vpn ---------------------------------------------------------------

install_wisp() {
    if uv tool list 2>/dev/null | awk '{print $1}' | grep -Fxq "$WISP_PKG"; then
        info "${WISP_PKG} is already installed as a uv tool; refreshing it."
        if $DRY_RUN; then
            plan "uv tool install --force ${WISP_PKG}"
        else
            uv tool install --force "$WISP_PKG"
        fi
    else
        info "Installing the wisp CLI (${WISP_PKG})..."
        if $DRY_RUN; then
            plan "uv tool install ${WISP_PKG}"
        else
            uv tool install "$WISP_PKG"
        fi
    fi
    add_local_bin_to_path
    if $DRY_RUN; then
        return 0
    fi
    if command -v wisp >/dev/null 2>&1; then
        info "'wisp' resolves to: $(command -v wisp)"
    else
        fail "'wisp' is not on PATH after installation (looked in ~/.local/bin). Open a new shell or run 'hash -r' and re-run."
    fi
}

# --- WireGuard tools --------------------------------------------------------

ensure_wireguard() {
    if command -v wg >/dev/null 2>&1 && command -v wg-quick >/dev/null 2>&1; then
        info "WireGuard tools are already installed."
        PRE_EXISTING+=("wireguard-tools")
        return 0
    fi
    info "Installing WireGuard tools via ${PKG_FAMILY}..."
    case "$PKG_FAMILY" in
        # The Debian 'wireguard' meta-package pulls in wireguard-tools;
        # resolvconf is needed by wg-quick for DNS updates.
        apt) pkg_install wireguard resolvconf ;;
        dnf | yum | pacman | apk) pkg_install wireguard-tools ;;
    esac
    INSTALLED_BY_WISP+=("wireguard-tools")
    if $DRY_RUN; then
        return 0
    fi
    if ! command -v wg >/dev/null 2>&1 || ! command -v wg-quick >/dev/null 2>&1; then
        fail "WireGuard installation failed: 'wg'/'wg-quick' not found. Check the package-manager output above."
    fi
}

# --- Pulumi CLI (optional, per-user) ----------------------------------------

append_pulumi_path() {
    local profile
    for profile in "$HOME/.profile" "$HOME/.bashrc"; do
        if [[ ! -f "$profile" ]]; then
            touch "$profile"
        fi
        if ! grep -Fqx "$PULUMI_PATH_LINE" "$profile"; then
            printf '\n%s\n' "$PULUMI_PATH_LINE" >>"$profile"
        fi
    done
    case ":${PATH}:" in
        *":${HOME}/.pulumi/bin:"*) ;;
        *) export PATH="${HOME}/.pulumi/bin:${PATH}" ;;
    esac
}

ensure_pulumi() {
    if $SKIP_PULUMI; then
        info "Skipping Pulumi (--skip-pulumi)."
        return 0
    fi
    if command -v pulumi >/dev/null 2>&1 || [[ -x "${HOME}/.pulumi/bin/pulumi" ]]; then
        info "Pulumi CLI is already installed."
        PRE_EXISTING+=("pulumi")
        verify_pulumi_login
        return 0
    fi
    info "Installing the Pulumi CLI for the current user (~/.pulumi)..."
    if $DRY_RUN; then
        plan "curl -fsSL ${PULUMI_INSTALL_URL} | sh -s -- --install-root \${HOME}/.pulumi"
        plan "append '${PULUMI_PATH_LINE}' to ~/.profile and ~/.bashrc"
    else
        curl -fsSL "$PULUMI_INSTALL_URL" | sh -s -- --install-root "${HOME}/.pulumi"
        append_pulumi_path
    fi
    INSTALLED_BY_WISP+=("pulumi")
    if ! $DRY_RUN; then
        info "Remember to run 'pulumi login' before deploying (Wisp drives Pulumi via its Automation API)."
    fi
}

verify_pulumi_login() {
    # Read-only check: 'pulumi whoami' fails when the CLI has no backend
    # configured yet. Not fatal — users deploying only to a Custom VPS never
    # need Pulumi — but the warning avoids a confusing failure later.
    local pulumi_bin out
    if command -v pulumi >/dev/null 2>&1; then
        pulumi_bin="$(command -v pulumi)"
    else
        pulumi_bin="${HOME}/.pulumi/bin/pulumi"
    fi
    out="$("$pulumi_bin" whoami 2>/dev/null | head -n1 || true)"
    if [[ -n "$out" ]]; then
        info "Pulumi is logged in (user: ${out})."
    else
        warn "Pulumi is not logged in ('pulumi whoami' returned nothing). Run 'pulumi login' before deploying with a cloud provider."
    fi
}

# --- Privileged daemon (delegated) -------------------------------------------

# The wisp group is created by `wisp daemon install`, so anything found
# before that step pre-existed. Recorded in the manifest for the paired
# uninstall script (its lifecycle belongs there, not to the daemon CLI).
record_group_state() {
    if getent group wisp >/dev/null 2>&1; then
        GROUP_STATE="pre_existing"
    else
        GROUP_STATE="created_by_wisp"
    fi
    info "wisp group state: ${GROUP_STATE}"
}

install_daemon() {
    if ! $DRY_RUN && ! command -v wisp >/dev/null 2>&1; then
        fail "'wisp' is not on PATH; cannot run 'wisp daemon install'."
    fi
    local wisp_bin=""
    if command -v wisp >/dev/null 2>&1; then
        wisp_bin="$(command -v wisp)"
    fi
    if $DRY_RUN; then
        plan "sudo ${wisp_bin:-wisp} daemon install"
        return 0
    fi
    # Absolute path on purpose: sudo's secure_path does not include
    # ~/.local/bin, so plain `sudo wisp` would not resolve.
    info "Running: sudo ${wisp_bin} daemon install"
    if ! sudo "$wisp_bin" daemon install; then
        fail "'wisp daemon install' failed (see its output above). Nothing privileged was re-implemented here."
    fi
}

# --- Install manifest ---------------------------------------------------------

manifest_path() {
    if [[ -n "${WISP_DATA_DIR:-}" ]]; then
        printf '%s/install-manifest.json' "${WISP_DATA_DIR%/}"
    else
        # platformdirs user_data_path("wisp") default on Linux.
        printf '%s/.local/share/wisp/install-manifest.json' "$HOME"
    fi
}

# Prints the list stored under key $2 of the manifest $1 as space-separated
# tokens (empty when the file or key is missing). Parsed with uv's Python so
# no jq dependency is needed.
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

# Prints the string stored under key $2 of the manifest $1 (empty when the
# file or key is missing, or the value is not a string).
manifest_get_value() {
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

dedupe() {
    # Prints its arguments (each possibly space-separated) deduplicated, as
    # one space-separated line.
    # shellcheck disable=SC2086
    printf '%s\n' "$@" | tr ' ' '\n' | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ $//'
}

list_remove() {
    # $1 = space-separated list; remaining args = tokens to drop from it.
    local drop="$*" keep="" token
    # shellcheck disable=SC2086
    for token in $1; do
        # shellcheck disable=SC2076
        if [[ " $drop " != *" $token "* ]]; then
            keep+="${token} "
        fi
    done
    printf '%s' "${keep% }"
}

json_list() {
    # Space-separated tokens -> JSON array body; rejects tokens that could
    # break out of a JSON string.
    local out="" token
    # shellcheck disable=SC2086
    for token in $1; do
        case "$token" in
            *'"'* | *'\'*) fail "Unsafe character in manifest token: ${token}" ;;
        esac
        out+="\"${token}\", "
    done
    printf '%s' "${out%, }"
}

detect_wisp_version() {
    # The tool installed from PyPI lives in ~/.local/share/uv/tools/wisp-vpn;
    # older source installs may use the legacy tool name 'wisp'.
    local tools_dir="${HOME}/.local/share/uv/tools" venv_py="" out=""
    if command -v wisp >/dev/null 2>&1; then
        out="$(wisp --version 2>/dev/null || true)"
        if [[ -n "$out" ]]; then
            printf '%s' "$out"
            return 0
        fi
    fi
    local candidate
    for candidate in "$WISP_PKG" wisp; do
        venv_py="${tools_dir}/${candidate}/bin/python"
        if [[ -x "$venv_py" ]]; then
            out="$("$venv_py" -c 'from importlib.metadata import version; print(version("wisp-vpn"))' 2>/dev/null || true)"
            if [[ -n "$out" ]]; then
                printf '%s' "$out"
                return 0
            fi
        fi
    done
    printf 'unknown'
}

validate_manifest_text() {
    # Uses uv's Python on purpose: the system python3 may be missing or old.
    # Non-fatal when uv itself is unavailable (a dry run on a bare machine).
    if ! command -v uv >/dev/null 2>&1; then
        warn "uv is not available; skipping manifest JSON validation."
        return 0
    fi
    printf '%s\n' "$1" | uv run --no-project python -m json.tool >/dev/null
}

write_manifest() {
    local manifest_file pre_all installed_all version json group_state existing_group
    step "Recording the install manifest"
    manifest_file="$(manifest_path)"
    pre_all="$(dedupe "$(manifest_get "$manifest_file" "pre_existing")" "${PRE_EXISTING[@]+"${PRE_EXISTING[@]}"}")"
    installed_all="$(dedupe "$(manifest_get "$manifest_file" "installed_by_wisp")" "${INSTALLED_BY_WISP[@]+"${INSTALLED_BY_WISP[@]}"}")"
    # Keep the categories exclusive: a tool detected as pre-existing in this
    # run cannot be a tool Wisp installed, so pre_existing wins.
    installed_all="$(list_remove "$installed_all" "$pre_all")"
    # Sticky group merge: if a prior manifest already recorded the group
    # state, keep it — a recorded created_by_wisp stays created_by_wisp even
    # when the group now exists (it exists BECAUSE Wisp created it earlier).
    # Only a manifest without a group field (first install, or a manifest
    # from before this field existed) records the freshly detected state.
    existing_group="$(manifest_get_value "$manifest_file" "group")"
    if [[ -n "$existing_group" ]]; then
        group_state="$existing_group"
    else
        group_state="$GROUP_STATE"
    fi
    if [[ -f "$manifest_file" ]]; then
        info "Merging with the existing manifest at ${manifest_file}."
    fi
    version="$(detect_wisp_version)"
    json="$(printf '{\n  "wisp_version": "%s",\n  "installed_at": "%s",\n  "pre_existing": [%s],\n  "installed_by_wisp": [%s],\n  "group": "%s"\n}\n' \
        "$version" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        "$(json_list "$pre_all")" "$(json_list "$installed_all")" \
        "$group_state")"

    if $DRY_RUN; then
        plan "would write ${manifest_file}:"
        printf '%s\n' "$json" | sed 's/^/        /'
        validate_manifest_text "$json"
        return 0
    fi

    mkdir -p "$(dirname "$manifest_file")"
    printf '%s\n' "$json" >"$manifest_file"
    uv run --no-project python -m json.tool "$manifest_file" >/dev/null
    info "Manifest written to ${manifest_file}."
}

# --- Summary -------------------------------------------------------------------

print_summary() {
    step "Install summary"
    if ((${#INSTALLED_BY_WISP[@]} > 0)); then
        info "Installed by this script: ${INSTALLED_BY_WISP[*]}"
    else
        info "Nothing new was installed; everything was already present."
    fi
    if ((${#PRE_EXISTING[@]} > 0)); then
        info "Already present (left as-is): ${PRE_EXISTING[*]}"
    fi

    info "Next steps:"
    if [[ "$GROUP_STATE" == "created_by_wisp" ]]; then
        info "  - Log out and back in (or run 'newgrp wisp') so the wisp group"
        info "    membership added by the daemon takes effect."
    else 
        info "  - The wisp group already existed; no logout is needed."
    fi
    if [[ " ${INSTALLED_BY_WISP[*]:-} " == *" pulumi "* ]]; then
        info "  - Run 'pulumi login' to authenticate the Pulumi CLI before"
        info "    deploying to a cloud provider."
    fi

    info "  - Run 'wisp --help' to explore the CLI, or 'wisp' for the TUI."
}

main() {
    parse_args "$@"
    if $DRY_RUN; then
        info "Dry run: nothing will be written or executed."
    fi

    step "Preflight"
    detect_distro
    if ! $DRY_RUN; then
        info "Requesting sudo credentials (cached for the privileged steps)..."
        sudo -v
    fi
    ensure_curl

    step "uv"
    ensure_uv

    step "wisp CLI (${WISP_PKG})"
    install_wisp

    step "WireGuard tools"
    ensure_wireguard

    step "Pulumi CLI"
    ensure_pulumi

    step "Privileged daemon"
    record_group_state
    install_daemon

    write_manifest
    print_summary
}

main "$@"

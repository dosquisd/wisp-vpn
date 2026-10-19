#!/bin/bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

WISP_GROUP="wisp"
WISP_INSTALL_DIR="/usr/local/lib/wisp"
SOCKET_PATH="/run/wisp.sock"
PULUMI_INSTALL_DIR="/usr/local/lib/wisp/pulumi"
SKIP_PULUMI=false

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/" && pwd)"

source ${PROJECT_ROOT}/scripts/utils.sh

function ensureCurl() {
    if command -v curl &>/dev/null; then
        return
    fi

    echo -e "${GREEN}Instalando curl...${NC}"
    case "${OS}" in
        ubuntu|debian) installPackages apt-get install -y curl ;;
        fedora) installPackages dnf install -y curl ;;
        centos|almalinux|rocky) installPackages yum install -y curl ;;
        oracle) installPackages dnf install -y curl ;;
        arch) installPackages pacman -S --needed --noconfirm curl ;;
        alpine) installPackages apk add curl ;;
    esac
}

function configurePulumiPath() {
    local pulumi_user="${SUDO_USER:-root}"
    local pulumi_home

    pulumi_home=$(getent passwd "${pulumi_user}" | cut -d: -f6)

    runuser -u "${pulumi_user}" -- env HOME="${pulumi_home}" sh -s <<'EOF'
set -eu

pulumi_path='export PATH="$HOME/.pulumi/bin:$PATH"'
for profile in "$HOME/.profile" "$HOME/.bashrc"; do
    touch "$profile"
    if ! grep -Fqx "$pulumi_path" "$profile"; then
        printf '\n%s\n' "$pulumi_path" >> "$profile"
    fi
done
EOF
}

function installPulumi() {
    local pulumi_user="${SUDO_USER:-root}"
    local pulumi_home

    pulumi_home=$(getent passwd "${pulumi_user}" | cut -d: -f6)

    if command -v pulumi &>/dev/null \
        || runuser -u "${pulumi_user}" -- sh -lc 'command -v pulumi' &>/dev/null \
        || [[ -x "${pulumi_home}/.pulumi/bin/pulumi" ]] \
        || [[ -x "${PULUMI_INSTALL_DIR}/bin/pulumi" ]]; then
        configurePulumiPath
        echo -e "${GREEN}Pulumi is already installed. Skipping.${NC}"
        return
    fi

    if ! command -v curl &>/dev/null; then
        echo -e "${RED}curl is neccesary to install Pulumi but it is not available.${NC}"
        exit 1
    fi

    echo -e "${GREEN}Installing Pulumi for ${pulumi_user}...${NC}"
    curl -fsSL https://get.pulumi.com | runuser -u "${pulumi_user}" -- \
        env HOME="${pulumi_home}" sh -s -- \
        --install-root "${pulumi_home}/.pulumi"

    configurePulumiPath
    echo -e "${GREEN}Pulumi installed in ${pulumi_home}/.pulumi${NC}"
}

function createWispGroup() {
    echo -e "${GREEN}Creating group '${WISP_GROUP}'...${NC}"
    if ! getent group "${WISP_GROUP}" >/dev/null; then
        groupadd "${WISP_GROUP}"
        echo -e "${GREEN}Group '${WISP_GROUP}' created.${NC}"
    fi

    if [[ -n "${SUDO_USER:-}" ]]; then
        usermod -aG "${WISP_GROUP}" "${SUDO_USER}"
        echo -e "${GREEN}User '${SUDO_USER}' added to group '${WISP_GROUP}'.${NC}"
        echo "Close your session and log back in (or run 'newgrp ${WISP_GROUP}') for the change to take effect."
    else
        echo -e "${RED}No SUDO_USER detected. Add your user manually:${NC}"
        echo "  sudo usermod -aG ${WISP_GROUP} <user>"
    fi
}

function installDaemonFiles() {
    rm -rf "${WISP_INSTALL_DIR}/src" "${WISP_INSTALL_DIR}/venv"
    mkdir -p "${WISP_INSTALL_DIR}"

    # Copy the source code to the installation directory
    cp -r "${PROJECT_ROOT}/src" "${WISP_INSTALL_DIR}/src"

    # Create a virtual environment for the daemon, isolated from the user's development environment
    python3 -m venv "${WISP_INSTALL_DIR}/venv"
    "${WISP_INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip

    # Just meanwhile, install the required dependency for the daemon
    # but this would be changed to use pyproject.toml and uv.lock in the future,
    # but, first we need to use optional-dependencies in uv to avoid installing all the dependencies for the daemon,
    # since some of them are not needed for the daemon to run.
    "${WISP_INSTALL_DIR}/venv/bin/pip" install "platformdirs==4.11.7"

    echo -e "${GREEN}Daemon files installed in ${WISP_INSTALL_DIR}${NC}"
}

function removeExistingInstallation() {
    echo -e "${GREEN}Removing existing Wisp installation...${NC}"

    systemctl stop wisp.service wisp.socket 2>/dev/null || true
    systemctl disable wisp.service wisp.socket 2>/dev/null || true

    rm -f \
        /etc/systemd/system/wisp.service \
        /etc/systemd/system/wisp.socket \
        "${SOCKET_PATH}"

    rm -rf "${WISP_INSTALL_DIR}/src" "${WISP_INSTALL_DIR}/venv"

    systemctl daemon-reload
    systemctl reset-failed wisp.service wisp.socket 2>/dev/null || true
}

function installSystemdUnits() {
    cp "${PROJECT_ROOT}/packaging/wisp.socket" /etc/systemd/system/wisp.socket
    cp "${PROJECT_ROOT}/packaging/wisp.service" /etc/systemd/system/wisp.service

    systemctl daemon-reload
    systemctl enable --now wisp.socket

    echo -e "${GREEN}Socket and service installed and enabled.${NC}"
}

function initialCheck() {
    isRoot
    checkOS
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-pulumi)
            SKIP_PULUMI=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown argument: $1${NC}"
            exit 1
            ;;
    esac
done

initialCheck
ensureCurl
installWireGuardClient
[[ "${SKIP_PULUMI}" == false ]] && installPulumi
removeExistingInstallation
createWispGroup
installDaemonFiles
installSystemdUnits

echo ""
echo -e "${GREEN}Ready. Remember to log out and log back in to use wisp without sudo.${NC}"

#!/bin/bash

# Licensed under MIT License
# Copyright (c) 2019 angristan

RED='\033[0;31m'
ORANGE='\033[0;33m'
GREEN='\033[0;32m'
NC='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

function installPackages() {
    if ! "$@"; then
        echo -e "${RED}Failed to install packages.${NC}"
        echo "Please check your internet connection and package sources."
        exit 1
    fi
}

function isRoot() {
    if [ "${EUID}" -ne 0 ]; then
        echo "You need to run this script as root"
        exit 1
    fi
}

function checkOS() {
    source /etc/os-release
    OS="${ID}"
    if [[ ${OS} == "debian" || ${OS} == "raspbian" ]]; then
        if [[ ${VERSION_ID} -lt 10 ]]; then
            echo "Your version of Debian (${VERSION_ID}) is not supported. Please use Debian 10 Buster or later"
            exit 1
        fi
        OS=debian # overwrite if raspbian
    elif [[ ${OS} == "ubuntu" ]]; then
        RELEASE_YEAR=$(echo "${VERSION_ID}" | cut -d'.' -f1)
        if [[ ${RELEASE_YEAR} -lt 18 ]]; then
            echo "Your version of Ubuntu (${VERSION_ID}) is not supported. Please use Ubuntu 18.04 or later"
            exit 1
        fi
    elif [[ ${OS} == "fedora" ]]; then
        if [[ ${VERSION_ID} -lt 32 ]]; then
            echo "Your version of Fedora (${VERSION_ID}) is not supported. Please use Fedora 32 or later"
            exit 1
        fi
    elif [[ ${OS} == 'centos' ]] || [[ ${OS} == 'almalinux' ]] || [[ ${OS} == 'rocky' ]]; then
        if [[ ${VERSION_ID} == 7* ]]; then
            echo "Your version of CentOS (${VERSION_ID}) is not supported. Please use CentOS 8 or later"
            exit 1
        fi
    elif [[ -e /etc/oracle-release ]]; then
        source /etc/os-release
        OS=oracle
    elif [[ -e /etc/arch-release ]]; then
        OS=arch
    elif [[ -e /etc/alpine-release ]]; then
        OS=alpine
        if ! command -v virt-what &>/dev/null; then
            if ! (apk update && apk add virt-what); then
                echo -e "${RED}Failed to install virt-what. Continuing without virtualization check.${NC}"
            fi
        fi
    else
        echo "Looks like you aren't running this installer on a Debian, Ubuntu, Fedora, CentOS, AlmaLinux, Oracle or Arch Linux system"
        exit 1
    fi
}

function installWireGuardClient() {
    # Install WireGuard tools and module
    if [[ ${OS} == 'ubuntu' ]] || [[ ${OS} == 'debian' && ${VERSION_ID} -gt 10 ]]; then
        apt-get update
        installPackages apt-get install -y wireguard resolvconf
    elif [[ ${OS} == 'debian' ]]; then
        if ! grep -rqs "^deb .* buster-backports" /etc/apt/; then
            echo "deb http://deb.debian.org/debian buster-backports main" >/etc/apt/sources.list.d/backports.list
            apt-get update
        fi
        apt-get update
        installPackages apt-get install -y resolvconf
        installPackages apt-get install -y -t buster-backports wireguard
    elif [[ ${OS} == 'fedora' ]]; then
        if [[ ${VERSION_ID} -lt 32 ]]; then
            installPackages dnf install -y dnf-plugins-core
            dnf copr enable -y jdoss/wireguard
            installPackages dnf install -y wireguard-dkms
        fi
        installPackages dnf install -y wireguard-tools
    elif [[ ${OS} == 'centos' ]] || [[ ${OS} == 'almalinux' ]] || [[ ${OS} == 'rocky' ]]; then
        if [[ ${VERSION_ID} == 8* ]]; then
            installPackages yum install -y epel-release elrepo-release
            installPackages yum install -y kmod-wireguard
        fi
        installPackages yum install -y wireguard-tools
    elif [[ ${OS} == 'oracle' ]]; then
        if [[ ${VERSION_ID} == 8* ]]; then
            # OL8 only: wireguard-tools lives in the developer repo there.
            installPackages dnf install -y oraclelinux-developer-release-el8
            dnf config-manager --disable -y ol8_developer
            dnf config-manager --enable -y ol8_developer_UEKR6
            dnf config-manager --save -y --setopt=ol8_developer_UEKR6.includepkgs='wireguard-tools*'
        fi
        # OL9/OL10 ship wireguard-tools in the base repos; no release package.
        installPackages dnf install -y wireguard-tools iptables
    elif [[ ${OS} == 'arch' ]]; then
        installPackages pacman -S --needed --noconfirm wireguard-tools
    elif [[ ${OS} == 'alpine' ]]; then
        apk update
        installPackages apk add wireguard-tools
    fi

    # Verify WireGuard installation
    if ! command -v wg &>/dev/null; then
        echo -e "${RED}WireGuard installation failed. The 'wg' command was not found.${NC}"
        echo "Please check the installation output above for errors."
        exit 1
    fi

    # Make sure the directory exists (this does not seem the be the case on fedora)
    mkdir -p /etc/wireguard >/dev/null 2>&1

    install -d -m 700 /etc/wireguard
    find /etc/wireguard -type f -exec chmod 600 {} +
}

function uninstallWg() {
    echo ""
    echo -e "\n${RED}WARNING: This will uninstall WireGuard and remove all the configuration files!${NC}"
    echo -e "${ORANGE}Please backup the /etc/wireguard directory if you want to keep your configuration files.\n${NC}"
    read -rp "Do you really want to remove WireGuard? [y/n]: " -e REMOVE
    REMOVE=${REMOVE:-n}
    if [[ $REMOVE == 'y' ]]; then
        checkOS

        if [[ ${OS} == 'alpine' ]]; then
            rc-service "wg-quick.${SERVER_WG_NIC}" stop
            rc-update del "wg-quick.${SERVER_WG_NIC}"
            unlink "/etc/init.d/wg-quick.${SERVER_WG_NIC}"
            rc-update del sysctl
        else
            systemctl stop "wg-quick@${SERVER_WG_NIC}"
            systemctl disable "wg-quick@${SERVER_WG_NIC}"
        fi

        if [[ ${OS} == 'ubuntu' ]] || [[ ${OS} == 'debian' ]]; then
            apt-get remove -y wireguard wireguard-tools qrencode
        elif [[ ${OS} == 'fedora' ]]; then
            dnf remove -y --noautoremove wireguard-tools qrencode
            if [[ ${VERSION_ID} -lt 32 ]]; then
                dnf remove -y --noautoremove wireguard-dkms
                dnf copr disable -y jdoss/wireguard
            fi
        elif [[ ${OS} == 'centos' ]] || [[ ${OS} == 'almalinux' ]] || [[ ${OS} == 'rocky' ]]; then
            yum remove -y --noautoremove wireguard-tools
            if [[ ${VERSION_ID} == 8* ]]; then
                yum remove --noautoremove kmod-wireguard qrencode
            fi
        elif [[ ${OS} == 'oracle' ]]; then
            yum remove --noautoremove wireguard-tools qrencode
        elif [[ ${OS} == 'arch' ]]; then
            pacman -Rs --noconfirm wireguard-tools qrencode
        elif [[ ${OS} == 'alpine' ]]; then
            (cd qrencode-4.1.1 || exit && make uninstall)
            rm -rf qrencode-* || exit
            apk del wireguard-tools libqrencode libqrencode-tools
        fi

        rm -rf /etc/wireguard
        rm -f /etc/sysctl.d/wg.conf

        if [[ ${OS} == 'alpine' ]]; then
            rc-service --quiet "wg-quick.${SERVER_WG_NIC}" status &>/dev/null
        else
            # Reload sysctl
            sysctl --system

            # Check if WireGuard is running
            systemctl is-active --quiet "wg-quick@${SERVER_WG_NIC}"
        fi
        WG_RUNNING=$?

        if [[ ${WG_RUNNING} -eq 0 ]]; then
            echo "WireGuard failed to uninstall properly."
            exit 1
        else
            echo "WireGuard uninstalled successfully."
            exit 0
        fi
    else
        echo ""
        echo "Removal aborted!"
    fi
}



function initialCheck() {
    isRoot
    checkOS
}

initialCheck

if [[ ${1} == "uninstall" ]]; then
    uninstallWg
else
    installWireGuardClient
fi

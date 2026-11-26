#!/bin/bash
# Build the AetherOS ISO inside a privileged Debian 13 container.
# Use this on Ubuntu, WSL, macOS or Windows (Docker Desktop) -- anywhere that is
# not a Debian 13 host. Output: dist/aether-os-x86_64.iso
set -Eeuo pipefail

# Git Bash / MSYS on Windows rewrites "/src" into a Windows path; turn that off.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_ROOT="$ROOT"
if (cd "$ROOT" && pwd -W >/dev/null 2>&1); then
    HOST_ROOT="$(cd "$ROOT" && pwd -W)"   # C:/Users/... form for Docker Desktop
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is required: https://docs.docker.com/get-docker/" >&2
    exit 1
fi

mkdir -p "$ROOT/dist"

# --privileged is needed: debootstrap/live-build mount /proc, /sys and /dev.
# The build workspace lives inside the container (needs ~25 GB of Docker disk).
docker run --rm --privileged \
    -v "$HOST_ROOT:/src" \
    -e AETHER_BUILD_DIST=/src/dist \
    debian:trixie \
    bash -euo pipefail -c '
        if [ ! -f /src/build/build-iso.sh ]; then
            echo "Project folder was not mounted at /src. Run this from WSL/Linux inside the project folder." >&2
            exit 1
        fi
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends \
            live-build debootstrap xorriso squashfs-tools curl unzip mtools dosfstools \
            ca-certificates gnupg debian-archive-keyring python3 procps
        cd /src
        bash ./build/build-iso.sh
    '

echo
echo "ISO: $ROOT/dist/aether-os-x86_64.iso"

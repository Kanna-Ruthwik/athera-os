#!/bin/bash
# Build the AetherOS live ISO (Debian 13 "trixie", amd64, BIOS + UEFI hybrid).
#
# Needs Debian's own live-build (version 2023xxxx or newer) and debootstrap.
# On Ubuntu / WSL the packaged live-build is the ancient 3.0~a57, which does not
# understand these options -- use build/docker-build.sh instead.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${AETHER_BUILD_DIST:-$ROOT/dist}"
AUTO_WORK=0

if [[ "$EUID" -ne 0 ]]; then
    echo "This build must run as root: sudo make iso" >&2
    exit 1
fi

for tool in lb debootstrap xorriso mksquashfs curl unzip sha256sum mformat mcopy; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing build dependency: $tool" >&2
        echo "Install it with: apt-get install live-build debootstrap xorriso squashfs-tools curl unzip mtools dosfstools" >&2
        exit 1
    fi
done

# --- Preflight: the live-build version decides whether this recipe can work. ---
LB_VERSION="$(lb --version 2>/dev/null | head -n1 | tr -d '[:space:]')"
if [[ ! "$LB_VERSION" =~ ^[0-9]{8} ]]; then
    cat >&2 <<EOF
Unsupported live-build version: "${LB_VERSION:-unknown}".

This recipe needs Debian's live-build (a date-style version such as 20250505).
Ubuntu and most WSL distributions ship live-build 3.0~a57, which builds a broken
image (it stops right after "I: Base system installed successfully.").

Use one of:
  * a Debian 13 (trixie) machine or VM:   sudo make iso
  * Docker (works from Linux, macOS, Windows/WSL):   ./build/docker-build.sh
EOF
    exit 1
fi
if [[ ! -e /usr/share/debootstrap/scripts/trixie ]]; then
    echo "This debootstrap has no 'trixie' script. Use Debian 13, or update debootstrap." >&2
    exit 1
fi

if [[ -n "${AETHER_BUILD_WORK:-}" ]]; then
    WORK="$AETHER_BUILD_WORK"
    if [[ -e "$WORK" ]]; then
        echo "Build workspace already exists; choose a fresh path: $WORK" >&2
        exit 1
    fi
    mkdir -p "$WORK"
else
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/aether-live-build.XXXXXXXX")"
    AUTO_WORK=1
fi

BUILD_SUCCEEDED=0
cleanup() {
    if [[ "$AUTO_WORK" -eq 1 && "$BUILD_SUCCEEDED" -eq 1 ]]; then
        rm -rf -- "$WORK"
    fi
}
trap cleanup EXIT
echo "Build workspace: $WORK  (live-build $LB_VERSION)"

avail_kb="$(df -Pk "$WORK" | awk 'NR==2 {print $4}')"
if [[ "${avail_kb:-0}" -lt $((20 * 1024 * 1024)) ]]; then
    echo "Warning: less than 20 GB free in $WORK; the build may run out of space." >&2
fi

mkdir -p "$DIST"
mkdir -p \
    "$WORK/config/package-lists" \
    "$WORK/config/includes.chroot/etc/systemd/system" \
    "$WORK/config/includes.chroot/usr/libexec/aether" \
    "$WORK/config/includes.chroot/usr/bin" \
    "$WORK/config/includes.chroot/usr/share/aether" \
    "$WORK/config/includes.chroot/etc/live/config.conf.d" \
    "$WORK/config/hooks/live"

cat >"$WORK/config/package-lists/aetheros.list.chroot" <<'EOF'
systemd
systemd-sysv
libpam-systemd
python3
python3-venv
bubblewrap
libseccomp2
sqlite3
sudo
sway
foot
fonts-dejavu-core
dbus
dbus-user-session
alsa-utils
espeak-ng
curl
unzip
ca-certificates
network-manager
EOF

cp -a "$ROOT/overlay/etc/." "$WORK/config/includes.chroot/etc/"
# Configs that desktop packages (waybar, fuzzel, ...) also ship in /etc/xdg would
# make dpkg stop at a conffile prompt. Stage them elsewhere; hook 015 copies them
# into /etc/xdg after those packages are installed.
mkdir -p "$WORK/config/includes.chroot/usr/share/aether/xdg-defaults"
cp -a "$WORK/config/includes.chroot/etc/xdg/." "$WORK/config/includes.chroot/usr/share/aether/xdg-defaults/"
rm -rf "$WORK/config/includes.chroot/etc/xdg"
for source in "$ROOT/overlay/usr/libexec/aether/"*.py; do
    install -m 0644 "$source" "$WORK/config/includes.chroot/usr/libexec/aether/$(basename "$source").txt"
done
install -m 0755 "$ROOT/build/install-runtime.sh" "$WORK/config/includes.chroot/usr/libexec/aether/install-runtime.sh"
install -m 0644 "$ROOT/build/requirements-aether.txt" "$WORK/config/includes.chroot/usr/share/aether/requirements-aether.txt"

cat >"$WORK/config/includes.chroot/usr/bin/aetherctl" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/libexec/aether/aetherctl.py.txt "$@"
EOF
chmod 0755 "$WORK/config/includes.chroot/usr/bin/aetherctl"

cp -a "$ROOT/overlay/usr/share/." "$WORK/config/includes.chroot/usr/share/"
for launcher in "$ROOT/overlay/usr/bin/"*; do
    install -m 0755 "$launcher" "$WORK/config/includes.chroot/usr/bin/$(basename "$launcher")"
done

for hook in "$ROOT/build/hooks/"*.hook.chroot; do
    install -m 0755 "$hook" "$WORK/config/hooks/live/$(basename "$hook")"
done

cd "$WORK"
# Option names follow live-build >= 2023 (lb_config(1) in Debian 13):
#   --binary-image (not -images), --bootloaders (not --bootloader grub2),
#   --debian-installer none (not "false").
# BIOS boots via syslinux/isolinux, UEFI via grub-efi, from the same hybrid ISO.
lb config \
    --distribution trixie \
    --architectures amd64 \
    --binary-image iso-hybrid \
    --bootloaders "syslinux,grub-efi" \
    --debian-installer none \
    --bootappend-live "boot=live components username=live hostname=aetheros" \
    --linux-packages linux-image \
    --linux-flavours amd64 \
    --iso-volume AETHEROS \
    --image-name aether-os \
    --mirror-bootstrap http://deb.debian.org/debian \
    --mirror-chroot http://deb.debian.org/debian \
    --mirror-binary http://deb.debian.org/debian \
    --mirror-chroot-security http://deb.debian.org/debian-security \
    --mirror-binary-security http://deb.debian.org/debian-security \
    --security true \
    --archive-areas "main contrib non-free non-free-firmware" \
    --keyring-packages debian-archive-keyring

if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    HOST_SYNC="$(command -v sync)"
    mkdir -p "$WORK/host-tools"
    cp "$HOST_SYNC" "$WORK/host-tools/real-sync"
    cat >"$WORK/host-tools/sync" <<EOF
#!/bin/sh
if [ "\$#" -eq 0 ]; then
    exec "$WORK/host-tools/real-sync" --file-system "$WORK"
fi
exec "$WORK/host-tools/real-sync" "\$@"
EOF
    chmod 0755 "$WORK/host-tools/sync"
    unshare --mount --fork /bin/bash -c '
        set -e
        mount --make-rprivate /
        mount --bind "$1" /usr/bin/sync
        exec lb build
    ' _ "$WORK/host-tools/sync"
else
    lb build
fi

mapfile -t images < <(find "$WORK" -maxdepth 1 -type f -name '*.iso' -print)
if [[ "${#images[@]}" -ne 1 ]]; then
    echo "Expected exactly one ISO in $WORK, found ${#images[@]}" >&2
    exit 1
fi

# Sanity-check that the image carries both boot paths (warning only: the report
# format differs between xorriso versions).
xorriso -indev "${images[0]}" -report_el_torito plain 2>/dev/null | tee "$WORK/eltorito-report.txt" || true
if ! grep -qi "BIOS" "$WORK/eltorito-report.txt" || ! grep -qi "UEFI\|EFI" "$WORK/eltorito-report.txt"; then
    echo "Warning: could not confirm both BIOS and UEFI boot entries in the ISO report." >&2
fi

cp -f "${images[0]}" "$DIST/aether-os-x86_64.iso"
(cd "$DIST" && sha256sum aether-os-x86_64.iso | tee aether-os-x86_64.iso.sha256)
echo "Built $DIST/aether-os-x86_64.iso"
BUILD_SUCCEEDED=1

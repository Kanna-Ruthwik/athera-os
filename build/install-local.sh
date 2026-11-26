#!/bin/bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$EUID" -ne 0 ]]; then
    echo "This installer must run as root: sudo ./build/install-local.sh \"\$USER\"" >&2
    exit 1
fi

if [[ "${1:-}" == "" ]]; then
    echo "Usage: sudo ./build/install-local.sh <existing-desktop-user>" >&2
    exit 1
fi

DESKTOP_USER="$1"
getent passwd "$DESKTOP_USER" >/dev/null || {
    echo "Unknown desktop user: $DESKTOP_USER" >&2
    exit 1
}

apt-get update
apt-get install -y python3 python3-venv bubblewrap libseccomp2 sqlite3 curl unzip ca-certificates alsa-utils espeak-ng python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

for account in aether-ai aether-semfs aether-exec aether-llm; do
    getent group "$account" >/dev/null || groupadd --system "$account"
    getent passwd "$account" >/dev/null || \
        useradd --system --gid "$account" --home-dir /nonexistent --shell /usr/sbin/nologin "$account"
done
getent group aether-clients >/dev/null || groupadd --system aether-clients
usermod -aG aether-clients "$DESKTOP_USER"

install -d -m 0755 /usr/libexec/aether
for source in "$ROOT/overlay/usr/libexec/aether/"*.py; do
    install -m 0644 "$source" "/usr/libexec/aether/$(basename "$source").txt"
done
install -m 0755 "$ROOT/build/install-runtime.sh" /usr/libexec/aether/install-runtime.sh
install -d -m 0755 /usr/share/aether
install -m 0644 "$ROOT/build/requirements-aether.txt" /usr/share/aether/requirements-aether.txt
install -d -m 0755 /usr/share/aether/licenses
install -m 0644 "$ROOT/overlay/usr/share/aether/licenses/MIT.txt" /usr/share/aether/licenses/MIT.txt
install -d -m 0755 /usr/share/doc/aether
install -m 0644 "$ROOT/overlay/usr/share/doc/aether/MODEL_NOTICES.txt" /usr/share/doc/aether/MODEL_NOTICES.txt
"$ROOT/build/install-runtime.sh"
cat > /usr/local/bin/aetherctl <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/libexec/aether/aetherctl.py.txt "$@"
EOF
chmod 0755 /usr/local/bin/aetherctl

for launcher in "$ROOT/overlay/usr/bin/"*; do
    install -m 0755 "$launcher" "/usr/local/bin/$(basename "$launcher")"
done
install -d -m 0755 /usr/share/applications
install -m 0644 "$ROOT/overlay/usr/share/applications/"*.desktop /usr/share/applications/
install -d -m 0755 /etc/aether
install -m 0644 "$ROOT/overlay/etc/aether/llm.conf" /etc/aether/llm.conf

install -d -m 0755 /etc/systemd/system
install -m 0644 "$ROOT/overlay/etc/systemd/system/"*.service /etc/systemd/system/
install -d -m 0755 /etc/tmpfiles.d
install -m 0644 "$ROOT/overlay/etc/tmpfiles.d/aetheros.conf" /etc/tmpfiles.d/aetheros.conf

install -d -m 0755 /run/ai /var/lib/aether /var/lib/semfs /var/lib/execd
chown root:aether-clients /run/ai
chmod 0755 /run/ai
chown root:aether-clients /var/lib/aether
chown aether-semfs:aether-clients /var/lib/semfs
chown aether-exec:aether-clients /var/lib/execd
chmod 0750 /var/lib/aether /var/lib/semfs /var/lib/execd

systemctl daemon-reload
systemctl enable --now aid.service semfsd.service execd.service aether-keygen.service

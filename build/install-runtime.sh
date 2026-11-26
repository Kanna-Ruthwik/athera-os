#!/bin/bash
set -Eeuo pipefail

if [[ "$EUID" -ne 0 ]]; then
    echo "Runtime setup must run as root" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQUIREMENTS="/usr/share/aether/requirements-aether.txt"
if [[ ! -f "$REQUIREMENTS" ]]; then
    REQUIREMENTS="$REPO_ROOT/build/requirements-aether.txt"
fi
VENV="/opt/aether/venv"
MODEL_ROOT="/usr/share/aether/models"
VOSK_MODEL="$MODEL_ROOT/vosk-model-small-en-us-0.15"

install -d -m 0755 /opt/aether "$MODEL_ROOT" /usr/share/aether/licenses
if [[ ! -x "$VENV/bin/python3" ]]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/python3" -m pip install --disable-pip-version-check --no-cache-dir -r "$REQUIREMENTS"

if [[ ! -d "$VOSK_MODEL" ]]; then
    temporary="$(mktemp -d)"
    trap 'rm -rf -- "$temporary"' EXIT
    curl --fail --location --retry 3 --output "$temporary/vosk-model.zip" \
        https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    echo "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498  $temporary/vosk-model.zip" | sha256sum --check --status
    unzip -q "$temporary/vosk-model.zip" -d "$temporary"
    install -d -m 0755 "$VOSK_MODEL"
    cp -a "$temporary/vosk-model-small-en-us-0.15/." "$VOSK_MODEL/"
    rm -rf -- "$temporary"
    trap - EXIT
fi

install -m 0644 /usr/share/common-licenses/Apache-2.0 /usr/share/aether/licenses/Apache-2.0.txt
# Debian has no MIT file in /usr/share/common-licenses, so MIT.txt is shipped
# in the overlay (overlay/usr/share/aether/licenses/MIT.txt) instead.
if [[ ! -f /usr/share/aether/licenses/MIT.txt ]]; then
    echo "Missing /usr/share/aether/licenses/MIT.txt (shipped by the overlay)" >&2
    exit 1
fi
install -m 0644 /usr/share/common-licenses/GPL-3 /usr/share/aether/licenses/GPL-3.0.txt

AETHER_MODEL_CACHE="$MODEL_ROOT" HF_HUB_OFFLINE=0 "$VENV/bin/python3" -c \
    'from fastembed import TextEmbedding; model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", cache_dir="/usr/share/aether/models"); next(model.embed(["AetherOS local semantic search warmup"]))'

chown -R root:root /opt/aether /usr/share/aether
chmod -R a+rX /opt/aether /usr/share/aether

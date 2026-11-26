# AetherOS — local-first Linux prototype

AetherOS in this repository is an **early, security-oriented prototype**, not a finished or production-certified operating system. It is a Debian 13 based live image with a themed Sway desktop, a private on-device assistant (**Aether**) backed by a small local language model, offline speech and semantic file search, a sandboxed command broker, and an experimental installer. Services are plain-text Python sources with hardened systemd units. The Python sources intentionally use `.txt` extensions in the built system.

This repository is focused on the Linux image and daemon sources; the earlier web dashboard and its frontend toolchain have been removed. For the full image-building walkthrough, see [`README_MAKING_ISO.md`](README_MAKING_ISO.md). Short version: `bash build/docker-build.sh` (any OS with Docker) or `sudo make iso` (Debian 13) produces `dist/aether-os-x86_64.iso`.

## Using the desktop

Boot the ISO (log in as `live` / `live` if asked). The desktop starts automatically on the first console. Super is the Windows key (change `$mod` in `/etc/sway/config` if your VM host captures it).

| Keys | Action |
| --- | --- |
| `Super+Space` | Open the Aether assistant |
| `Super+D` | App launcher |
| `Super+Enter` | Terminal |
| `Super+B` / `Super+E` | Browser / file manager |
| `Super+Shift+Q` | Close window |
| `Super+1..6` | Workspaces (`Super+Shift+1..6` moves a window) |
| `Super+Ctrl+L` | Lock screen (password `live`) |
| `Print` | Screenshot to `~/Pictures` |
| `Super+Shift+E` | Exit the desktop (type `sway` to return) |

Run `aetherctl doctor` to check services, the model, and the build report. See [`docs/ASSISTANT.md`](docs/ASSISTANT.md) for what the assistant can do and how it keeps you in control.

## What works in this prototype

- **Aether assistant** (`aether-assistant`, `aetherctl chat`): routes plain requests (create folders and notes, search, open apps, volume, notes to remember, and more) to a small set of home-folder-only tools, asks before anything destructive, logs every action, and chats through the local model for everything else.
- **Local language model**: `aether-llm.service` runs llama.cpp (portable CPU build) with Qwen2.5-1.5B-Instruct on loopback port 8080; `aid` brokers access to it.
- **Desktop**: Sway, Waybar, fuzzel, mako, foot, Firefox ESR, Thunar, PipeWire, NetworkManager applet.
- **Installer (experimental)**: Calamares via `aether-install`; untested.

- **`aid`** serves a bounded newline-delimited JSON protocol on `/run/ai/inference.sock`, authenticates local peers with Linux `SO_PEERCRED`, prioritizes interactive requests over queued background requests, and only proxies to `127.0.0.1:8080` (llama.cpp-compatible `/v1/chat/completions`). It fails closed with an unavailable response when the local model backend is missing.
- **`semfsd`** uses Linux `inotify` events and SQLite to maintain text and embedding data under `/var/lib/semfs`; semantic results are filtered by the calling UID and re-check source ownership, inode, and owner-read permission. It indexes only selected text suffixes and skips common credential/config directories.
- **Offline speech** provides English recognition using the bundled Vosk model and local speech synthesis using eSpeak NG. `aetherctl listen`, `aetherctl say`, and `aetherctl voice` expose the pipeline; `voice` also requires a configured local inference backend.
- **`execd`** accepts single-use HMAC-signed, short-lived capability tokens scoped to one UID, an exact executable/argv prefix, and approved existing directories. Commands run without a shell inside bubblewrap with a new network namespace, read-only filesystem scopes, a 15-second timeout, bounded captured output, and a libseccomp filter that denies socket and high-risk syscalls. Root must explicitly mint each capability. A root-only one-shot service generates the per-machine HMAC key on first broker start under `/var/lib/aether/execd.key`; systemd passes it to `execd` as a private mode-`0400` credential; consumed nonces persist in `/var/lib/execd/replay.sqlite3` across daemon restarts.
- All three services run under separate non-login service accounts, with systemd filesystem, namespace, resource, and capability restrictions.

## Important limitations

This remains a prototype, not a production or certified OS. The live image bundles small English speech-recognition and semantic-embedding models and a 1.5-billion-parameter language model: expect modest answer quality and slow replies on a VM without a GPU. Tool use by the model is best-effort and every action it proposes needs your approval. Speech synthesis uses eSpeak NG rather than a neural TTS model. Semantic vectors are stored and scored in SQLite; this is not a dedicated, large-scale vector database. The Calamares installer is experimental and untested, and there is no immutable-root update/rollback mechanism or Secure Boot support. The execution broker has not received an independent security audit. Treat the ISO as experimental until it passes the checks in [Security and acceptance status](#security-and-acceptance-status).

The model weights are downloaded while the ISO is built (see `/usr/share/doc/aether/MODEL_NOTICES.txt` for sources and licenses), not at runtime. `aid` never contacts a remote provider.

## Run the daemons on a Debian 13 host

Use a disposable Debian 13 (Trixie) VM for first testing. The host installer writes service accounts, systemd unit files, tmpfiles configuration, and daemon sources under `/usr` and `/etc`; inspect `build/install-local.sh` before running it. It requires an existing non-root desktop login and a working systemd host.

```sh
sudo apt update
sudo apt install python3 python3-venv bubblewrap libseccomp2 sqlite3 curl unzip ca-certificates alsa-utils espeak-ng
sudo ./build/install-local.sh "$USER"
```

The script enables and starts the three units. Sign out and in again to apply the `aether-clients` group. Check status and recent structured journal events with:

```sh
sudo systemctl status aid.service semfsd.service execd.service
sudo journalctl -u aid -u semfsd -u execd --since today
/usr/local/bin/aetherctl status
```

`semfsd` only indexes files that its dedicated account can actually read. It does not bypass user DAC permissions. Existing private home directories may therefore yield few or no indexed files; do not weaken home permissions to make the demo appear populated.

### Configure local inference

The ISO already runs `aether-llm.service` (see `/etc/aether/llm.conf`; swap the model by repointing `/usr/share/aether/models/llm/model.gguf`). On a plain Debian host, install a trusted, locally built llama.cpp server and a model you are licensed to use. Configure it to listen only on loopback port 8080 and launch it separately; `aid` never starts a model server and refuses remote hosts. Confirm that `curl http://127.0.0.1:8080/health` responds before using:

```sh
aetherctl ask "Summarize the purpose of AetherOS"
```

If the server is absent, `aid` remains running but reports `local inference backend unavailable` for generation requests.

### Search and sandbox examples

Search ranks readable indexed files by semantic similarity and is scoped to the calling UID:

```sh
aetherctl search "privacy architecture"
```

Try offline speech recognition and synthesis (a microphone is needed for `listen`):

```sh
aetherctl listen --seconds 5
aetherctl say "AetherOS speech output is working"
aetherctl voice --seconds 5
```

The bundled speech recognizer and embedding model are English-only. Model sources, licenses, and the Vosk archive checksum are listed in `/usr/share/doc/aether/MODEL_NOTICES.txt`. The image build needs network access to install runtime packages and fetch the model assets; the daemons do not download models at runtime.

Execution requires a root-minted, one-time capability. Use a disposable test directory. The example grants `/usr/bin/ls` read-only access to that directory for 60 seconds and binds no other home path into the sandbox:

```sh
mkdir -p "$HOME/aether-sandbox-demo"
printf 'local test file\n' > "$HOME/aether-sandbox-demo/notes.txt"
sudo /usr/bin/python3 /usr/libexec/aether/issue-capability.py.txt \
  --uid "$(id -u)" --ttl 60 --root "$HOME/aether-sandbox-demo" \
  -- /usr/bin/ls

aetherctl run --cwd "$HOME/aether-sandbox-demo" -- /usr/bin/ls -la
```

Copy the token printed by the first command into the hidden prompt from `aetherctl`; do not place it in shell history, logs, or a file. It is consumed after one attempt. To inspect a directory, mint a new token. The capability issuer and daemon are small, security-sensitive code and require review before use on valuable data.

## Build a hybrid ISO

Follow [README_MAKING_ISO.md](README_MAKING_ISO.md) for build-host prerequisites, model notices, VirtualBox settings, and acceptance checks. On Debian 13 amd64, run `sudo make iso`. The expected output is `dist/aether-os-x86_64.iso`; verify it with `sha256sum dist/aether-os-x86_64.iso`. Apt package versions are not yet pinned to a signed snapshot.

## Checks in this repository

```sh
make test
```

## Security and acceptance status

The prototype does not claim production readiness. A release still needs independent code review, systemd security scoring, negative tests on a configured Debian VM, deterministic package snapshotting, verified EFI/BIOS and Secure Boot boot tests, model-absence and resource-exhaustion tests, and a threat-model review. The live ISO has no install-to-disk flow. See [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).

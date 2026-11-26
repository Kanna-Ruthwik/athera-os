# AetherOS security boundaries

## Status

The daemons and units in `overlay/` are an early, review-required prototype. No ISO has been VM-boot-validated by this repository; the build recipe was repaired by static review only (see `docs/FIXES.md`). Do not treat these files as a certified operating system or run the installer on a valuable host.

## Service boundaries currently implemented

- `aid.py.txt` runs as `aether-ai`, reads newline-delimited JSON on `/run/ai/inference.sock`, verifies Linux `SO_PEERCRED` is available, bounds requests and work queues, and proxies only to the fixed loopback address `127.0.0.1:8080`. It does not launch a model or contact remote APIs. Interactive work sorts ahead of queued background work, but generation is single-worker and not preemptive once underway.
- `semfsd.py.txt` runs as `aether-semfs`, receives Linux `inotify` events, stores selected readable text and BGE-small embeddings in SQLite, and ranks semantic matches only after filtering by caller UID and rechecking the current inode, owner, and owner-read mode. The service has no network namespace access; model files are read-only and bundled in the image. Since the daemon has no elevated read privileges, user-private files it cannot read are intentionally absent.
- `speech.py.txt` uses the bundled Vosk English model for local transcription and eSpeak NG for local speech synthesis. Audio processing runs on the invoking user's command and is not sent to a remote service. `aetherctl voice` additionally depends on the separately managed local inference backend.
- `execd.py.txt` runs as `aether-exec`, checks peer UID against an HMAC capability, consumes each nonce at most once, validates executable/argv-prefix and working-directory scopes, then starts bubblewrap without a shell. The child has a fresh namespace set, read-only approved directory binds, a 15-second CPU/wall bound, output caps, and a libseccomp BPF policy that denies sockets and selected high-risk syscalls. A missing bubblewrap or libseccomp causes execution to fail closed.
- `issue-capability.py.txt` only mints tokens when run by root. The root-only `aether-keygen.service` creates a per-machine root-owned mode-`0600` HMAC key on first broker start; systemd passes a private mode-`0400` credential to the unprivileged broker. The key is never included in the ISO source tree. Used nonces persist in a private SQLite database, so a daemon restart does not restore a consumed token.
- systemd units give the services separate non-login accounts, empty capability sets, protected system paths, namespace restrictions, and service-level resource limits. Socket access is restricted to the `aether-clients` group.

## Remaining security work

This code has not received an independent security audit. HMAC capabilities prove authorization to the broker, not that the UI obtained meaningful user consent. The issuer is a root command and should be called only after a human reviews the exact UID, executable, arguments, directory, and expiry. Tokens are bearer credentials; never log or store them. The broker grants read-only filesystem scopes only; write access is intentionally not implemented. Bubblewrap's user-namespace availability and libseccomp filter installation must be tested on the target kernel.

The semantic index stores snippets and embeddings as plaintext in `/var/lib/semfs/index.sqlite3` and is not encrypted. It uses cosine similarity over a bounded per-user candidate set in SQLite; it is not an approximate-nearest-neighbor engine. Its inotify watches are limited by the service account's ordinary filesystem permissions. Indexer database consistency, ACL/ACL changes, model provenance, and inode replacement races need dedicated adversarial testing.

## Image and desktop boundaries

The Debian live-build recipe targets an amd64 hybrid ISO that boots on legacy BIOS (isolinux) and UEFI (unsigned GRUB; no Secure Boot support), plus a Sway/Wayland desktop. It bundles small English speech-recognition and semantic-embedding assets, but no general-purpose LLM weights or neural TTS model. The image is live-only and has no installer. It is not a locked/reproducible build: apt package versions are not pinned to a signed snapshot. The image also runs a loopback-only llama.cpp service (`aether-llm`, separate account, no capabilities, network limited to localhost) and a user-level assistant (see below). There is no immutable root/rollback system, Secure Boot support, or independent security audit. Do not call the resulting ISO finished until all acceptance gates pass.

## Required release gates

1. Review daemon protocol parsing, symlink/path races, resource exhaustion, token handling, and seccomp filter behavior.
2. Run the Python contract tests, `systemd-analyze verify`, and `systemd-analyze security` for every unit on Debian 13.
3. Build on a clean Debian 13 host; record source revision, package snapshot, checksums, and SBOM.
4. Test UEFI and legacy BIOS boot in VirtualBox with VMSVGA, 3D disabled, 4 vCPU, and 8 GiB RAM; test Secure Boot separately where supported.
5. Run negative tests: unauthenticated socket peers, cross-UID search, revoked permissions, symlinks, path traversal, expired/replayed token, shell injection attempts, network syscalls, timeout/output limits, unavailable model, and daemon restart.
6. Do not claim the RFC Definition of Done until local inference and broker execution work end-to-end from the desktop and every release gate passes.

## Assistant and local model

- **Trust boundary.** Text produced by the language model is untrusted. The assistant only auto-runs read-only tools (search, list, read, system info, recall, activity) when the model asks; any other action proposed by the model needs the person's approval. Content of files read by a tool is not fed back into the model, which limits prompt-injection reach.
- **Explicit requests.** Low-risk, reversible actions the person typed themselves (create folder or note, open app, volume, screenshot, remember) run directly. Moving, trashing, forgetting everything, and power actions always need approval; trashing moves to `~/.local/share/Trash` rather than deleting.
- **File scope.** Tools resolve symlinks and refuse anything outside the home folder, plus secret locations (`.ssh`, `.gnupg`, `.aws`, `.kube`, `.password-store`, browser profile, `.config/aether`). New files are never overwritten.
- **Accountability.** Every action is appended to `~/.local/state/aether/actions.log` (file contents are never logged). Memory is a plain JSON file (`~/.local/share/aether/memory.json`, mode 0600) that can be read, edited or deleted.
- **Not covered.** The assistant runs as the logged-in user, so an approved action has that user's full rights inside the home folder. The assistant does not use `execd`; arbitrary command execution is not an assistant capability. These tools have unit tests, not an independent review.

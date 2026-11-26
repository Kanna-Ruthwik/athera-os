# Making the AetherOS ISO

This guide builds the Debian live-build prototype. It does **not** claim the result is finished, reproducible, or production-certified. The image includes small English Vosk and BGE model assets for offline speech recognition and semantic file search. It does not include a general-purpose language model or a disk installer.

## 0. Quick start

The ISO must be built with **Debian's** live-build. Pick one:

| Where you are | Command |
| --- | --- |
| Windows (Docker Desktop), macOS, Ubuntu, WSL | `bash build/docker-build.sh` |
| Debian 13 machine or VM | `sudo make iso` |
| No local machine with ~25 GB free | Push the repo to GitHub, then run **Actions > Build AetherOS ISO**; download `aether-os-iso` from the run |

The result is `dist/aether-os-x86_64.iso` (plus a `.sha256` file).

### If a build "stops after `I: Base system installed successfully.`"

That line is the end of debootstrap. The stall that follows happens when the host has the old Ubuntu/WSL
`live-build 3.0~a57`, which cannot build a Debian 13 image with this recipe. `build/build-iso.sh` now checks
the version up front and tells you to use Docker or Debian 13. Check yours with `lb --version`: a date such as
`20250505` is correct, `3.0~a57` is not.

## 1. Build-host requirements

Use a disposable Debian 13 (Trixie) amd64 machine or VM (or the Docker route above). The builder needs:

- A working Debian package mirror connection.
- At least 25 GB free disk space for package caches, the live filesystem, and the ISO.
- Root privileges for live-build's chroot/image assembly.
- The `live-build` (Debian's, date-style version), `debootstrap`, `xorriso`, `squashfs-tools`, `curl`, and `unzip` packages.
- The `mtools` and `dosfstools` packages, which live-build uses for the EFI boot image.
- A network connection during the image build to install pinned Python runtime packages and fetch the model assets.

Do not build or test on a machine containing data you cannot afford to lose. The build script runs package and chroot tooling as root.

## 2. Prepare the checkout

Clone or download this repository and enter its root directory (the directory containing `Makefile`). Install the host packages:

```sh
sudo apt update
sudo apt install live-build debootstrap xorriso squashfs-tools curl unzip mtools dosfstools
```

Run the repository checks before building:

```sh
make test
```

## 3. Build the hybrid image

Start the image build from the repository root:

```sh
sudo make iso
```

The recipe stages package lists and overlays into a fresh temporary workspace and runs live-build with `--binary-image iso-hybrid --bootloaders "syslinux,grub-efi"`, so one ISO boots on both legacy BIOS (isolinux) and UEFI (GRUB). Build hooks then create the service accounts, enable the units, and install the Python runtime and the Vosk/BGE model assets into the image. It does not erase the existing `build/live-build/` cache/workspace.

On WSL, the build uses a private mount namespace and scopes debootstrap's `sync` to its temporary workspace; a global WSL flush can hang on unrelated host-mounted drives.

On success, the expected image is:

```text
dist/aether-os-x86_64.iso
```

By default, the temporary workspace is removed after a successful build. To retain build files for troubleshooting, set `AETHER_BUILD_WORK` to a new, non-existent path before starting. Failed builds leave the workspace intact; the script prints its location.

The live ISO has no install-to-disk flow. Secure Boot is **not** supported by this recipe (live-build's `grub-efi` loader is unsigned); leave Secure Boot off in VirtualBox. Never place private signing keys in the repository or ISO.

The generated ISO is intentionally ignored by Git. Copy it from `dist/` to your VM or removable test media after verifying its checksum:

```sh
sha256sum dist/aether-os-x86_64.iso
```

This recipe currently tracks Debian Trixie but does not pin apt packages to a signed snapshot, so identical inputs are not guaranteed to produce identical bytes.

## 4. First boot in VirtualBox

Use a disposable VM with these initial settings:

- Type: Linux / Debian (64-bit)
- CPUs: 4
- RAM: 8 GiB
- Graphics: VMSVGA
- EFI: enabled for the UEFI pass
- 3D acceleration: disabled
- Audio input: enabled if testing the microphone speech command

Attach `dist/aether-os-x86_64.iso` as the optical disk and boot the live environment (no hard disk is needed). The boot menu offers the live system; the `live` account logs in automatically and the Sway desktop starts on its own (the password, needed for the lock screen and `sudo` prompts, is **`live`**). `Super+Space` opens the assistant, `Super+D` the app launcher and `Super+Enter` a terminal. Run `aetherctl doctor` first: it lists any optional package or the model that failed to install during the build. Test both EFI and legacy BIOS; with EFI enabled, keep Secure Boot off. The ISO is live-only and does not provide an installer, so changes are lost on reboot.

## 5. Check services and basic operation

After logging in, check the local daemon status:

```sh
aetherctl status
sudo systemctl status aid.service semfsd.service execd.service aether-keygen.service
sudo journalctl -b -u aid -u semfsd -u execd -u aether-keygen
```

The daemons communicate over `/run/ai/*.sock`; the live account is configured for the `aether-clients` group. The services run under separate non-login accounts. `execd` requires bubblewrap user namespaces and libseccomp; if sandbox prerequisites are missing it must fail closed.

Try offline speech recognition and speech output:

```sh
aetherctl listen --seconds 5
aetherctl say "AetherOS speech output is working"
```

`aetherctl voice --seconds 5` transcribes a short clip and sends it to the local inference broker. It requires a microphone and a separately installed local llama.cpp-compatible backend. The bundled speech model is English-only. Model provenance and licenses are listed in `/usr/share/doc/aether/MODEL_NOTICES.txt`.

### Local inference backend

`aid` is a loopback-only API broker, not a model runtime. To test generation, separately install a trusted llama.cpp server and a model whose license permits your use. Configure that server to listen only on `127.0.0.1:8080`, then verify its `/health` endpoint and try:

```sh
aetherctl ask "Explain what AetherOS does"
```

No model is bundled, downloaded, or contacted remotely by this repository. If the local server is not running, `aid` should report the inference backend unavailable while remaining alive.

### Permission-aware index

`semfsd` watches `/home` with inotify and indexes selected text formats the service account can already read. It does not bypass normal Unix permissions. Private files that the indexer cannot read should be absent from results. Search uses the bundled BGE-small English embeddings and scores vectors stored in SQLite; this is a small prototype index, not a dedicated vector database.

### Sandboxed execution

`execd` accepts a short-lived, single-use capability minted by the root-only issuer. Test only with a disposable directory and a harmless read-only command. Follow the token workflow in [README.md](README.md); keep the bearer token out of shell history, files, and logs. Do not grant broad paths or run unreviewed commands.

## 6. Acceptance checklist

Before describing an image as bootable or ready for others, verify on the target Debian version:

- `make test` passes.
- `systemd-analyze verify` reports no unit errors; record the `systemd-analyze security` output.
- UEFI and legacy BIOS both reach the Sway desktop in VirtualBox with 3D disabled; Secure Boot is tested separately where the firmware supports it.
- `aid`, `semfsd`, `execd`, and the one-shot key bootstrap start under the intended users and permissions.
- Semantic search ranks relevant text while preserving UID/read-permission filtering; offline speech recognition and synthesis work with the VM's audio input/output.
- The local inference backend works without external network access.
- Cross-UID index searches, unreadable files, changed permissions, symlinks, expired/replayed capabilities, paths outside a granted root, network syscalls, and command timeouts are tested and denied as expected.
- Reboot preserves the index and replay database while the per-machine execution key remains root-only.

The image is still a prototype: no immutable-root update/rollback mechanism, install-to-disk flow, general-purpose model, production Secure Boot key, or independent security audit is included. Review [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) before testing outside a disposable VM.

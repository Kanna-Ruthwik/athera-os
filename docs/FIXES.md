# Build fixes

Problems found in the original image recipe and what changed. None of this has been boot-tested in VirtualBox:
the repair was done by reading the scripts against Debian's live-build documentation and by running the
checks that were possible offline (`make test`, `shellcheck`, pinned-wheel availability on PyPI).

| # | Problem | Effect | Fix |
| - | ------- | ------ | --- |
| 1 | `build-iso.sh` used live-build 3.x options (`--binary-images`, `--bootloader grub2`, `--debian-installer false`). Ubuntu/WSL ship `live-build 3.0~a57`, which accepts them but cannot build Debian 13; Debian 13's live-build rejects them. | Build stops right after `I: Base system installed successfully.` | Rewritten for Debian's live-build (`--binary-image`, `--bootloaders "syslinux,grub-efi"`, `--debian-installer none`); version preflight check; Docker builder (`build/docker-build.sh`) and a GitHub Actions workflow. |
| 2 | `install-runtime.sh` copied `/usr/share/common-licenses/MIT`, which does not exist on Debian/Ubuntu; with `set -e` the runtime hook aborts. | Build fails when installing the Python runtime and models. | MIT text shipped in `overlay/usr/share/aether/licenses/MIT.txt`; scripts check for it. |
| 3 | Hand-rolled UEFI step (signed shim + GRUB, FAT image, `xorriso` remap to `/boot/grub/grub_eltorito`) depended on files only older loaders create. | ISO assembly would fail or produce an unbootable image. | Removed; live-build's own BIOS + UEFI hybrid output is used. **Secure Boot is no longer supported.** |
| 4 | Build scripts were committed non-executable, and `make iso` ran `./build/build-iso.sh`. | `Permission denied` on a fresh Linux clone. | Makefile calls `bash ./build/...`; executable bits set. |
| 5 | `greetd` config runs the greeter as `greeter`, which Debian's package may not create. | No login screen. | Hook 010 creates the `greeter` account if missing. |
| 6 | No font package and no VirtualBox cursor workaround. | Blank terminal text / invisible cursor in Sway on VMSVGA. | Added `fonts-dejavu-core`, `libpam-systemd`; `WLR_NO_HARDWARE_CURSORS=1`. |
| 7 | `.sha256` file recorded a full path. | `sha256sum -c` failed outside the build host. | Checksum written relative to `dist/`. |

Known limits unchanged: live-only (no installer), no bundled LLM, English-only speech model.

## Phases 2-6 (desktop, local LLM, assistant, polish, release)

- Session: greetd removed; tty1 auto-login starts sway via /etc/profile.d/aetheros-session.sh.
- Desktop: sway + waybar + fuzzel + mako + foot, dark theme, wallpaper, GTK settings.
- Local LLM: llama.cpp (pinned b12723, portable CPU build) + Qwen2.5-1.5B-Instruct Q4_K_M,
  served on 127.0.0.1:8080 by aether-llm.service; aid brokers it on /run/ai/inference.sock.
- Assistant: Super+Space window; confirmation before destructive actions, visible memory, audit log.
- Installer: Calamares is experimental.
- Model and desktop packages are installed "softly": a failed download does not fail the build.
  Run `aetherctl doctor` or read /usr/share/aether/build-report.txt to see what was installed.
- Not tested by the author: the full ISO build/boot, model download, end-to-end inference.
- Fixed dpkg conffile prompt (waybar/fuzzel): configs staged in /usr/share/aether/xdg-defaults and copied after package install; --force-confold set.
